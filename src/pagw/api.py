import secrets
from uuid import UUID, uuid4

import httpx
import psycopg
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from opentelemetry import trace
from prometheus_client import CONTENT_TYPE_LATEST
from pydantic import BaseModel, ConfigDict, Field

from pagw.config import Settings, get_settings
from pagw.db import Database
from pagw.fhir import InvalidFHIR, claim_resource, claim_response, fixture
from pagw.security import (
    AuthenticationError,
    Principal,
    TokenVerifier,
    callback_key,
    client_assertion,
    verify_signature,
)
from pagw.store import Conflict, NotFound, Store
from pagw.telemetry import configure, durable_metrics, event


class SubscriptionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(max_length=2048)


class PayerEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    event_id: str = Field(min_length=1, max_length=128)
    payer_ref: str = Field(min_length=1, max_length=128)
    correlation_id: str = Field(min_length=36, max_length=36)
    decision: str
    sequence: int = Field(ge=0)
    preauth_ref: str | None = Field(default=None, max_length=128)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure(settings)
    app = FastAPI(
        title="Prior Auth Integration Gateway",
        version="0.1.0",
        description="Synthetic data. Bounded FHIR R4 API; PAS-inspired, not PAS-conformant.",
    )
    store = Store(Database(settings), settings)
    verifier = TokenVerifier(settings)
    app.state.store = store
    app.state.verifier = verifier
    bearer = HTTPBearer(auto_error=False)

    def authorize(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)) -> Principal:
        if credentials is None:
            raise HTTPException(
                401, "BEARER_TOKEN_REQUIRED", headers={"WWW-Authenticate": "Bearer"}
            )
        try:
            return verifier.verify(credentials.credentials)
        except AuthenticationError as exc:
            raise HTTPException(
                401, "INVALID_TOKEN", headers={"WWW-Authenticate": "Bearer"}
            ) from exc

    def scope(name):
        def check(principal: Principal = Depends(authorize)):
            if name not in principal.scopes:
                raise HTTPException(403, "INSUFFICIENT_SCOPE")
            return principal

        return check

    submit_auth = Depends(scope("submit"))
    read_auth = Depends(scope("read"))
    webhook_auth = Depends(scope("webhooks"))
    ops_auth = Depends(scope("ops"))

    @app.middleware("http")
    async def boundary(request: Request, call_next):
        request_id = str(uuid4())
        chunks, size = [], 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > 1024 * 1024:
                return JSONResponse({"detail": "REQUEST_TOO_LARGE"}, status_code=413)
            chunks.append(chunk)
        request._body = b"".join(chunks)
        try:
            response = await call_next(request)
        except psycopg.Error:
            event("database.unavailable", error_code="DATABASE_UNAVAILABLE")
            response = JSONResponse(
                {"type": "about:blank", "title": "DATABASE_UNAVAILABLE", "status": 503},
                status_code=503,
            )
        response.headers["X-Request-Id"] = request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        return response

    def error(request: Request, code: str, status: int, expression="", headers=None):
        if request.url.path.startswith("/fhir/"):
            body = {
                "resourceType": "OperationOutcome",
                "issue": [
                    {
                        "severity": "error",
                        "code": "invalid" if status < 500 else "exception",
                        "diagnostics": code,
                        "expression": [expression] if expression else [],
                    }
                ],
            }
            return JSONResponse(
                body, status_code=status, media_type="application/fhir+json", headers=headers
            )
        return JSONResponse(
            {"type": "about:blank", "title": code, "status": status},
            status_code=status,
            media_type="application/problem+json",
            headers=headers,
        )

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        return error(request, str(exc.detail), exc.status_code, headers=exc.headers)

    @app.exception_handler(InvalidFHIR)
    async def fhir_error(request, exc):
        return error(request, exc.code, 422, exc.location)

    @app.exception_handler(Conflict)
    async def conflict(request, exc):
        return error(request, str(exc), 409)

    @app.exception_handler(NotFound)
    async def missing(request, exc):
        return error(request, str(exc), 404)

    @app.exception_handler(RequestValidationError)
    async def validation(request, exc):
        return error(request, "INVALID_REQUEST", 422)

    @app.get("/healthz")
    def health():
        return {"status": "ok"}

    @app.get("/readyz")
    def ready():
        migration = store.db.one("SELECT max(version) AS version FROM schema_migrations")
        if not migration or (migration["version"] or 0) < 3:
            raise HTTPException(503, "MIGRATIONS_REQUIRED")
        return {"database": "ready", "broker": settings.broker}

    @app.get("/metrics")
    def metrics(principal: Principal = ops_auth):
        return Response(durable_metrics(store.db, principal.tenant), media_type=CONTENT_TYPE_LATEST)

    @app.get("/fhir/metadata")
    def metadata():
        return {
            "resourceType": "CapabilityStatement",
            "status": "active",
            "date": "2026-10-20",
            "kind": "instance",
            "fhirVersion": "4.0.1",
            "format": ["json"],
            "description": "Bounded synthetic gateway. Custom submit operation; no PAS profile conformance claim.",
            "rest": [
                {
                    "mode": "server",
                    "resource": [
                        {
                            "type": "Claim",
                            "interaction": [{"code": "read"}],
                            "operation": [
                                {
                                    "name": "submit",
                                    "definition": "https://example.org/fhir/OperationDefinition/gateway-submit",
                                }
                            ],
                        },
                        {"type": "ClaimResponse", "interaction": [{"code": "read"}]},
                    ],
                }
            ],
        }

    @app.post(
        "/fhir/Claim/$submit",
        responses={
            202: {"description": "Durably accepted"},
            409: {"description": "Identity conflict"},
            422: {"description": "Invalid FHIR"},
        },
    )
    def submit(request: Request, bundle: dict, principal: Principal = submit_auth):
        key = request.headers.get("Idempotency-Key", "")
        if not 1 <= len(key) <= 128 or not key.isascii() or any(ord(x) < 33 for x in key):
            raise HTTPException(400, "VALID_IDEMPOTENCY_KEY_REQUIRED")
        with trace.get_tracer("pagw.api").start_as_current_span("authorization.admit"):
            code, body, replay = store.submit(principal.tenant, key, bundle)
        return JSONResponse(
            body,
            status_code=code,
            media_type="application/fhir+json",
            headers={
                "Content-Location": f"/fhir/ClaimResponse/{body['id']}",
                "Idempotent-Replayed": str(replay).lower(),
            },
        )

    @app.get("/fhir/ClaimResponse/{request_id}")
    def response(request_id: UUID, principal: Principal = read_auth):
        return JSONResponse(
            claim_response(store.request(principal.tenant, request_id)),
            media_type="application/fhir+json",
        )

    @app.get("/fhir/Claim/{request_id}")
    def claim(request_id: UUID, principal: Principal = read_auth):
        row = store.request(principal.tenant, request_id)
        return JSONResponse(claim_resource(row), media_type="application/fhir+json")

    @app.get("/api/requests")
    def requests(limit: int = 50, principal: Principal = read_auth):
        return store.list_requests(principal.tenant, max(1, min(limit, 100)))

    @app.get("/api/requests/{request_id}")
    def detail(request_id: UUID, principal: Principal = read_auth):
        row = store.request(principal.tenant, request_id)
        with store.db.transaction() as c:
            history = c.execute(
                "SELECT * FROM history WHERE request_id=%s ORDER BY version", (request_id,)
            ).fetchall()
            operations = c.execute(
                "SELECT id,kind,state,attempts,due_at,last_error FROM operations WHERE request_id=%s ORDER BY kind",
                (request_id,),
            ).fetchall()
            deliveries = c.execute(
                "SELECT id,version,state FROM deliveries WHERE request_id=%s ORDER BY version",
                (request_id,),
            ).fetchall()
            observations = c.execute(
                "SELECT event_id,state,created_at,'callback' AS source FROM inbox WHERE payer=%s AND event->>'correlation_id'=%s UNION ALL SELECT id::text,state,created_at,source FROM payer_observations WHERE request_id=%s ORDER BY created_at",
                (row["payer"], str(request_id), request_id),
            ).fetchall()
            attempts = c.execute(
                "SELECT e.kind,e.outcome,e.created_at FROM operation_events e JOIN operations o ON o.id=e.operation_id WHERE o.request_id=%s ORDER BY e.id LIMIT 200",
                (request_id,),
            ).fetchall()
        return {
            "request": {
                k: v
                for k, v in row.items()
                if k not in {"bundle", "submission_payload", "digest", "tenant"}
            },
            "history": history,
            "operations": operations,
            "deliveries": deliveries,
            "observations": observations,
            "attempts": attempts,
            "response": claim_response(row),
        }

    @app.post("/api/requests/{request_id}/cancel", status_code=204)
    def cancel(request_id: UUID, principal: Principal = submit_auth):
        store.cancel(principal.tenant, request_id, actor_id=principal.client_id)

    @app.post("/api/requests/{request_id}/recover", status_code=204)
    def recover(request_id: UUID, principal: Principal = ops_auth):
        store.recover(principal.tenant, request_id, actor_id=principal.client_id)

    @app.post("/webhooks/subscriptions", status_code=201)
    def subscribe(body: SubscriptionInput, principal: Principal = webhook_auth):
        # Local receiver is provisioned with the shared demo secret. Production
        # subscribers receive their generated secret once and configure it themselves.
        secret = (
            settings.webhook_secret.get_secret_value()
            if settings.mode == "local"
            else secrets.token_urlsafe(32)
        )
        return {
            **store.subscribe(principal.tenant, body.url, secret, actor_id=principal.client_id),
            "secret": secret,
        }

    @app.post("/payer-callbacks/{payer}")
    async def callback(payer: str, request: Request):
        if payer not in {"alpha", "beta"}:
            raise HTTPException(404, "UNKNOWN_PAYER")
        body = await request.body()
        try:
            verify_signature(
                callback_key(settings, payer),
                body,
                request.headers.get("X-PA-Timestamp", ""),
                request.headers.get("X-PA-Signature", ""),
            )
        except AuthenticationError as exc:
            raise HTTPException(401, "INVALID_SIGNATURE") from exc
        try:
            parsed = PayerEvent.model_validate_json(body)
            UUID(parsed.correlation_id)
            if parsed.decision not in {"PENDED", "APPROVED", "DENIED"} or (
                parsed.decision == "APPROVED" and not parsed.preauth_ref
            ):
                raise ValueError("invalid-decision")
        except ValueError as exc:
            raise HTTPException(422, "INVALID_PAYER_EVENT") from exc
        event_id = parsed.event_id
        from starlette.concurrency import run_in_threadpool

        state = await run_in_threadpool(
            store.ingest, payer, event_id, parsed.model_dump(exclude={"event_id"})
        )
        return {"state": state}

    @app.get("/webhooks/deliveries")
    def deliveries(principal: Principal = webhook_auth):
        with store.db.transaction() as c:
            return c.execute(
                "SELECT d.id,d.request_id,d.version,d.state,d.created_at FROM deliveries d JOIN subscriptions s ON s.id=d.subscription_id WHERE s.tenant=%s ORDER BY d.created_at DESC LIMIT 100",
                (principal.tenant,),
            ).fetchall()

    @app.post("/webhooks/deliveries/{delivery_id}/redeliver", status_code=204)
    def redeliver(delivery_id: UUID, principal: Principal = webhook_auth):
        store.redeliver(principal.tenant, delivery_id, actor_id=principal.client_id)

    @app.get("/admin/summary")
    def summary(principal: Principal = ops_auth):
        with store.db.transaction() as c:
            requests = c.execute(
                "SELECT delivery_state,count(*) AS count FROM requests WHERE tenant=%s GROUP BY delivery_state",
                (principal.tenant,),
            ).fetchall()
            dead = c.execute(
                "SELECT count(*) AS count FROM operations o JOIN requests r ON r.id=o.request_id WHERE o.state='DEAD' AND r.tenant=%s",
                (principal.tenant,),
            ).fetchone()
        return {"requests": requests, "dead_operations": dead["count"], "broker": settings.broker}

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def console():
        from importlib.resources import files

        return HTMLResponse(
            files("pagw")
            .joinpath("console.html")
            .read_text()
            .replace('data-demo="local"', f'data-demo="{settings.mode}"'),
            headers={
                "Content-Security-Policy": "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"
            },
        )

    if settings.mode == "local":

        @app.post("/demo/token", include_in_schema=False)
        def demo_token():
            url = settings.issuer + "/oauth/token"
            with httpx.Client(timeout=3, trust_env=False) as client:
                response = client.post(
                    url,
                    data={
                        "grant_type": "client_credentials",
                        "client_id": "operator",
                        "client_assertion": client_assertion(settings, "operator", url),
                        "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
                    },
                )
                response.raise_for_status()
                return response.json()

        @app.get("/demo/fixture", include_in_schema=False)
        def demo_fixture(payer: str = "alpha", code: str = "70551"):
            if payer not in {"alpha", "beta"} or code not in {"70551", "27447", "J0585", "99999"}:
                raise HTTPException(422, "UNKNOWN_SCENARIO")
            return fixture(payer, code, "demo-" + str(uuid4()))

    return app
