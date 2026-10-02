"""Durable synthetic partners and mock authorization server. Never real payer integrations."""

import asyncio
import base64
import json
import sqlite3
import time
from pathlib import Path
from urllib.parse import parse_qs
from uuid import UUID, uuid4

import httpx
import jwt
import psycopg
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from psycopg.types.json import Jsonb

from pagw.config import Settings, get_settings
from pagw.db import Database
from pagw.fhir import canonical, claim_response, digest, fixture, parse_submission
from pagw.legacy import LegacyRequest
from pagw.security import (
    CLIENTS,
    AuthenticationError,
    callback_key,
    public_jwk,
    signature,
    verify_signature,
)


def validate_assertion(
    settings: Settings, db: Database, assertion: str, audience: str, clients: set[str]
):
    try:
        key = jwt.PyJWK.from_dict(public_jwk(settings.client_key_path, "demo-client", "RS384")).key
        claims = jwt.decode(
            assertion,
            key,
            algorithms=["RS384"],
            audience=audience,
            options={"require": ["iss", "sub", "aud", "exp", "jti"]},
        )
        if (
            claims["iss"] != claims["sub"]
            or claims["sub"] not in clients
            or claims["exp"] > time.time() + 300
            or not isinstance(claims["jti"], str)
            or len(claims["jti"]) > 128
        ):
            raise ValueError("invalid-assertion")
        with db.transaction() as c:
            c.execute("DELETE FROM assertion_jtis WHERE expires_at<clock_timestamp()")
            c.execute(
                "INSERT INTO assertion_jtis(issuer,jti,expires_at) VALUES(%s,%s,to_timestamp(%s))",
                (audience + ":" + claims["sub"], claims["jti"], claims["exp"]),
            )
        return claims["sub"]
    except (jwt.PyJWTError, ValueError, KeyError, TypeError, psycopg.errors.UniqueViolation) as exc:
        raise HTTPException(401, "INVALID_CLIENT_ASSERTION") from exc


def mint(settings, subject, issuer, audience, scopes):
    now = int(time.time())
    return {
        "access_token": jwt.encode(
            {
                "sub": subject,
                "iss": issuer,
                "aud": audience,
                "iat": now,
                "exp": now + 300,
                "scope": " ".join(sorted(scopes)),
            },
            Path(settings.idp_key_path).read_bytes(),
            algorithm="RS256",
            headers={"kid": "demo-idp"},
        ),
        "token_type": "Bearer",
        "expires_in": 300,
    }


async def form(request):
    body = await request.body()
    if len(body) > 16384:
        raise HTTPException(413, "TOO_LARGE")
    return {k: v[0] for k, v in parse_qs(body.decode()).items()}


def idp_app(settings: Settings | None = None):
    settings = settings or get_settings()
    if settings.mode != "local":
        raise ValueError("mock identity provider is local-only")
    app, db = FastAPI(title="Synthetic IdP"), Database(settings)

    @app.get("/.well-known/jwks.json")
    def jwks():
        return {"keys": [public_jwk(settings.idp_key_path, "demo-idp")]}

    @app.post("/oauth/token")
    async def token(request: Request):
        data = await form(request)
        if (
            data.get("grant_type") != "client_credentials"
            or data.get("client_assertion_type")
            != "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
        ):
            raise HTTPException(400, "UNSUPPORTED_GRANT")
        from starlette.concurrency import run_in_threadpool

        subject = await run_in_threadpool(
            validate_assertion,
            settings,
            db,
            data.get("client_assertion", ""),
            settings.issuer + "/oauth/token",
            set(CLIENTS),
        )
        if subject != data.get("client_id"):
            raise HTTPException(401, "CLIENT_ID_MISMATCH")
        return mint(settings, subject, settings.issuer, settings.audience, CLIENTS[subject][1])

    return app


def normalized(row):
    return {
        "payer_ref": str(row["id"]),
        "correlation_id": str(row["correlation_id"]),
        "decision": row["decision"],
        "sequence": row["sequence"],
        "preauth_ref": "SYNTHETIC-" + str(row["id"])[:8] if row["decision"] == "APPROVED" else None,
    }


def payer_response(row):
    data = normalized(row)
    if row["payer"] == "beta":
        return {
            "authorizationId": data["payer_ref"],
            "externalRequestId": data["correlation_id"],
            "status": {"PENDED": "IN_REVIEW", "APPROVED": "AUTHORIZED", "DENIED": "NOT_AUTHORIZED"}[
                data["decision"]
            ],
            "revision": data["sequence"],
            "approvalCode": data["preauth_ref"],
        }
    result = claim_response(
        {
            **row,
            "delivery_state": "SUBMITTED",
            "preauth_ref": data["preauth_ref"],
            "version": data["sequence"],
        }
    )
    result["extension"] += [
        {
            "url": "https://example.org/fhir/StructureDefinition/correlation",
            "valueString": data["correlation_id"],
        },
        {
            "url": "https://example.org/fhir/StructureDefinition/decision",
            "valueCode": data["decision"],
        },
        {
            "url": "https://example.org/fhir/StructureDefinition/sequence",
            "valueInteger": data["sequence"],
        },
    ]
    return result


def advance(c, row):
    if row["decision"] == "PENDED":
        updated = c.execute(
            "UPDATE mock_authorizations SET decision=CASE WHEN code='27447' THEN 'DENIED' ELSE 'APPROVED' END,sequence=2 WHERE id=%s AND ready_at<=clock_timestamp() RETURNING *",
            (row["id"],),
        ).fetchone()
        return updated or row
    return row


def payers_app(settings: Settings | None = None):
    settings = settings or get_settings()
    if settings.mode != "local":
        raise ValueError("synthetic payer server is local-only")
    app, db = FastAPI(title="Synthetic payers"), Database(settings)

    def require_payer(request: Request, payer: str):
        if payer not in {"alpha", "beta"}:
            raise HTTPException(404)
        try:
            token = request.headers.get("Authorization", "").removeprefix("Bearer ")
            key = jwt.PyJWK.from_dict(public_jwk(settings.idp_key_path, "demo-idp")).key
            claims = jwt.decode(
                token,
                key,
                algorithms=["RS256"],
                audience=payer,
                issuer=settings.payer_base_url + "/" + payer,
                options={"require": ["sub", "exp"]},
            )
            if claims["sub"] != "gateway":
                raise ValueError()
        except (jwt.PyJWTError, ValueError) as exc:
            raise HTTPException(401, "INVALID_PAYER_TOKEN") from exc

    @app.post("/{payer}/oauth/token")
    async def token(payer: str, request: Request):
        if payer not in {"alpha", "beta"}:
            raise HTTPException(404)
        data = await form(request)
        if data.get("grant_type") != "client_credentials":
            raise HTTPException(400, "UNSUPPORTED_GRANT")
        if payer == "alpha":
            import hmac

            expected = (
                "Basic "
                + base64.b64encode(
                    ("gateway:" + settings.payer_client_secret.get_secret_value()).encode()
                ).decode()
            )
            if not hmac.compare_digest(request.headers.get("Authorization", ""), expected):
                raise HTTPException(401, "INVALID_CLIENT")
        else:
            from starlette.concurrency import run_in_threadpool

            await run_in_threadpool(
                validate_assertion,
                settings,
                db,
                data.get("client_assertion", ""),
                settings.payer_base_url + "/beta/oauth/token",
                {"gateway"},
            )
        return mint(
            settings, "gateway", settings.payer_base_url + "/" + payer, payer, {"submit", "poll"}
        )

    async def send_callback(row):
        data = {"event_id": f"{row['id']}:{row['sequence']}", **normalized(row)}
        body, timestamp = canonical(data), int(time.time())
        try:
            async with httpx.AsyncClient(timeout=3, trust_env=False) as client:
                response = await client.post(
                    settings.callback_base_url + "/payer-callbacks/" + row["payer"],
                    content=body,
                    headers={
                        "Content-Type": "application/json",
                        "X-PA-Timestamp": str(timestamp),
                        "X-PA-Signature": "v1="
                        + signature(callback_key(settings, row["payer"]), body, timestamp),
                    },
                )
                return response.is_success
        except httpx.HTTPError:
            return False

    async def submit(payer: str, request: Request):
        require_payer(request, payer)
        raw = await request.body()
        if len(raw) > 1024 * 1024:
            raise HTTPException(413)
        try:
            payload = json.loads(raw)
            if payer == "alpha":
                bundle = payload["bundle"]
            else:
                legacy = LegacyRequest.model_validate_json(raw)
                bundle = fixture(
                    "beta", legacy.services[0].procedure["code"], legacy.claimNumber.value
                )
            correlation = UUID(
                payload["correlation_id"] if payer == "alpha" else payload["externalRequestId"]
            )
            parsed = parse_submission(bundle)
            if parsed["payer"] != payer:
                raise ValueError("payer-mismatch")
        except (ValueError, KeyError, TypeError) as exc:
            raise HTTPException(422, "INVALID_SYNTHETIC_REQUEST") from exc
        key = request.headers.get("Idempotency-Key")
        if key != str(correlation):
            raise HTTPException(422, "INVALID_SUBMISSION_KEY")
        with db.transaction() as c:
            mode = c.execute(
                "SELECT * FROM mock_modes WHERE payer=%s FOR UPDATE", (payer,)
            ).fetchone()
            active = mode["mode"] if mode["remaining"] > 0 else "normal"
            c.execute(
                "UPDATE mock_modes SET remaining=greatest(remaining-1,0) WHERE payer=%s", (payer,)
            )
        if active == "flaky_5xx":
            raise HTTPException(503, "SYNTHETIC_UNAVAILABLE")
        if active == "rate_limit":
            return JSONResponse(
                {"error": "RATE_LIMITED"}, status_code=429, headers={"Retry-After": "1"}
            )
        if parsed["code"] == "99999":
            raise HTTPException(422, "SYNTHETIC_PERMANENT_REJECTION")
        fingerprint = digest(bundle if payer == "alpha" else payload)
        with db.transaction() as c:
            c.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (f"mock:{payer}:{key}",)
            )
            row = c.execute(
                "SELECT * FROM mock_authorizations WHERE payer=%s AND submission_key=%s FOR UPDATE",
                (payer, key),
            ).fetchone()
            if row and row["digest"] != fingerprint:
                raise HTTPException(409, "SUBMISSION_KEY_MISMATCH")
            if not row:
                row = c.execute(
                    "INSERT INTO mock_authorizations(id,payer,submission_key,digest,correlation_id,bundle,code,decision,sequence,ready_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,1,clock_timestamp()+(%s * interval '1 second')) RETURNING *",
                    (
                        uuid4(),
                        payer,
                        key,
                        fingerprint,
                        correlation,
                        Jsonb(bundle),
                        parsed["code"],
                        "APPROVED" if parsed["code"] == "J0585" else "PENDED",
                        settings.poll_seconds * 2,
                    ),
                ).fetchone()
            row = advance(c, row)
        if active == "early_callback" and payer == "alpha":
            await send_callback(row)
        if active == "timeout_after_accept":
            await asyncio.sleep(settings.http_timeout_seconds + 1)
        return payer_response(row)

    @app.post("/alpha/fhir/Claim/$submit")
    async def alpha(request: Request):
        return await submit("alpha", request)

    @app.post("/beta/api/v2/authorizations")
    async def beta(request: Request):
        return await submit("beta", request)

    @app.get("/{payer}/by-key/{key}")
    def poll(payer: str, key: UUID, request: Request):
        require_payer(request, payer)
        with db.transaction() as c:
            row = c.execute(
                "SELECT * FROM mock_authorizations WHERE payer=%s AND submission_key=%s FOR UPDATE",
                (payer, str(key)),
            ).fetchone()
            if not row:
                raise HTTPException(404, "AUTHORIZATION_NOT_FOUND")
            return payer_response(advance(c, row))

    @app.post("/mock/{payer}/chaos")
    def chaos(payer: str, body: dict, request: Request):
        # Mock controls require the configured secret and are never in the gateway API.
        import hmac

        if not hmac.compare_digest(
            request.headers.get("X-Mock-Key", ""), settings.payer_client_secret.get_secret_value()
        ):
            raise HTTPException(401)
        if (
            payer not in {"alpha", "beta"}
            or body.get("mode")
            not in {"normal", "flaky_5xx", "rate_limit", "timeout_after_accept", "early_callback"}
            or type(body.get("count", 0)) is not int
            or not 0 <= body.get("count", 0) <= 100
        ):
            raise HTTPException(422, "INVALID_CHAOS_MODE")
        with db.transaction() as c:
            c.execute(
                "UPDATE mock_modes SET mode=%s,remaining=%s WHERE payer=%s",
                (body["mode"], body.get("count", 0), payer),
            )
        return {"configured": True}

    @app.post("/mock/callback-tick")
    async def tick(request: Request):
        import hmac

        if not hmac.compare_digest(
            request.headers.get("X-Mock-Key", ""), settings.payer_client_secret.get_secret_value()
        ):
            raise HTTPException(401)
        with db.transaction() as c:
            rows = c.execute(
                "SELECT * FROM mock_authorizations WHERE payer='alpha' AND NOT callback_sent AND ready_at<=clock_timestamp() ORDER BY ready_at LIMIT 30"
            ).fetchall()
            rows = [advance(c, row) for row in rows]
        sent = 0
        for row in rows:
            if await send_callback(row):
                with db.transaction() as c:
                    c.execute(
                        "UPDATE mock_authorizations SET callback_sent=true WHERE id=%s",
                        (row["id"],),
                    )
                sent += 1
        return {"sent": sent}

    return app


def receiver_app(settings: Settings | None = None):
    settings = settings or get_settings()
    if settings.mode != "local":
        raise ValueError("synthetic receiver is local-only")
    app = FastAPI(title="Synthetic provider webhook receiver")
    Path(settings.receiver_db_path).parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(settings.receiver_db_path) as c:
        c.execute(
            "CREATE TABLE IF NOT EXISTS events(id text PRIMARY KEY, digest text NOT NULL, version int NOT NULL)"
        )

    @app.post("/events")
    async def receive(request: Request):
        raw = await request.body()
        if len(raw) > 1024 * 1024:
            raise HTTPException(413)
        try:
            verify_signature(
                settings.webhook_secret.get_secret_value(),
                raw,
                request.headers.get("X-PA-Timestamp", ""),
                request.headers.get("X-PA-Signature", ""),
            )
        except AuthenticationError as exc:
            raise HTTPException(401) from exc
        data = json.loads(raw)
        with sqlite3.connect(settings.receiver_db_path) as c:
            old = c.execute("SELECT digest FROM events WHERE id=?", (data["id"],)).fetchone()
            if old and old[0] != digest(data):
                raise HTTPException(409, "EVENT_CONFLICT")
            c.execute(
                "INSERT OR IGNORE INTO events VALUES(?,?,?)",
                (data["id"], digest(data), data["version"]),
            )
        return {"accepted": True}

    return app
