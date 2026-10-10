import time
from pathlib import Path
from uuid import uuid4

import jwt
import pytest

from pagw.fhir import canonical, fixture
from pagw.mocks import validate_assertion
from pagw.security import callback_key, client_assertion, signature
from pagw.telemetry import event


def headers(token, subject="operator"):
    return {"Authorization": "Bearer " + token(subject)}


def submit(client, token):
    response = client.post(
        "/fhir/Claim/$submit",
        headers={**headers(token), "Idempotency-Key": str(uuid4())},
        json=fixture(),
    )
    assert response.status_code == 202
    return response.json()["id"]


def test_authentication_required(client):
    response = client.get("/api/requests")
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


@pytest.mark.parametrize(
    "path,method",
    [
        ("/fhir/ClaimResponse/{id}", "GET"),
        ("/fhir/Claim/{id}", "GET"),
        ("/api/requests/{id}", "GET"),
        ("/api/requests/{id}/cancel", "POST"),
        ("/api/requests/{id}/recover", "POST"),
    ],
)
def test_cross_tenant_access_denied(client, token, path, method):
    rid = submit(client, token)
    # provider-b cannot escalate to ops even if it requests that token scope.
    response = client.request(method, path.format(id=rid), headers=headers(token, "provider-b"))
    assert response.status_code == (403 if path.endswith("recover") else 404)
    listing = client.get("/api/requests", headers=headers(token, "provider-b")).json()
    assert listing == []


@pytest.mark.parametrize(
    "claim,value",
    [
        ("aud", "other-service"),
        ("iss", "https://untrusted.invalid"),
        ("exp", 1),
        ("sub", "unknown"),
    ],
)
def test_invalid_token_claims(client, token, settings, claim, value):
    original = jwt.decode(token(), options={"verify_signature": False})
    original[claim] = value
    forged = jwt.encode(
        original,
        Path(settings.idp_key_path).read_bytes(),
        algorithm="RS256",
        headers={"kid": "demo-idp"},
    )
    assert (
        client.get("/api/requests", headers={"Authorization": "Bearer " + forged}).status_code
        == 401
    )


def test_wrong_scope_rejected(client, token):
    token_value = token(scopes={"read"})
    response = client.post(
        "/fhir/Claim/$submit",
        json=fixture(),
        headers={"Authorization": "Bearer " + token_value, "Idempotency-Key": "a"},
    )
    assert response.status_code == 403


def test_fhir_error_does_not_echo_patient_data(client, token):
    bundle = fixture()
    bundle["entry"][0]["resource"]["patient"]["reference"] = "Patient/SECRET-PATIENT-NAME"
    response = client.post(
        "/fhir/Claim/$submit", json=bundle, headers={**headers(token), "Idempotency-Key": "a"}
    )
    assert response.status_code == 422
    assert response.json()["resourceType"] == "OperationOutcome"
    assert "SECRET-PATIENT-NAME" not in response.text


def test_oversized_body_rejected(client, token):
    response = client.post(
        "/fhir/Claim/$submit", content=b"x" * (1024 * 1024 + 1), headers=headers(token)
    )
    assert response.status_code == 413


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data/",
        "http://127.0.0.1:5432",
        "https://example.org",
        "http://127.0.0.1:8083/events?extra=1",
    ],
)
def test_webhook_exact_allowlist(client, token, url):
    response = client.post("/webhooks/subscriptions", json={"url": url}, headers=headers(token))
    assert response.status_code == 409


def test_callback_signature_and_replay_window(client, token, settings):
    rid = submit(client, token)
    body = canonical(
        {
            "event_id": "event-1",
            "payer_ref": "payer-one",
            "correlation_id": rid,
            "decision": "APPROVED",
            "sequence": 2,
            "preauth_ref": "SYNTHETIC",
        }
    )
    timestamp = int(time.time())
    signed = {
        "X-PA-Timestamp": str(timestamp),
        "X-PA-Signature": "v1=" + signature(callback_key(settings, "alpha"), body, timestamp),
    }
    assert client.post("/payer-callbacks/alpha", content=body, headers=signed).status_code == 200
    assert (
        client.post("/payer-callbacks/alpha", content=body, headers=signed).json()["state"]
        == "APPLIED"
    )
    assert client.post("/payer-callbacks/beta", content=body, headers=signed).status_code == 401
    signed["X-PA-Timestamp"] = str(timestamp - 600)
    assert client.post("/payer-callbacks/alpha", content=body, headers=signed).status_code == 401


def test_assertion_jti_replay_rejected(settings, store):
    from fastapi import HTTPException

    url = settings.issuer + "/oauth/token"
    assertion = client_assertion(settings, "provider-a", url)
    assert validate_assertion(settings, store.db, assertion, url, {"provider-a"}) == "provider-a"
    with pytest.raises(HTTPException):
        validate_assertion(settings, store.db, assertion, url, {"provider-a"})


def test_logs_allowlist_excludes_payloads(caplog):
    import logging

    with caplog.at_level(logging.INFO, logger="pagw"):
        event(
            "operation.failed",
            request_id="safe-id",
            error_code="PAYER_TIMEOUT",
            bundle={"name": "SECRET-PATIENT"},
            exception="SECRET-ACCESS-TOKEN",
        )
    assert "safe-id" in caplog.text
    assert "SECRET-PATIENT" not in caplog.text
    assert "SECRET-ACCESS-TOKEN" not in caplog.text
