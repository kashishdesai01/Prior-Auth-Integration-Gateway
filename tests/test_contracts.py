import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
import jwt
import pytest
from test_api_security import headers, submit
from test_durability import admit

from pagw.connectors import legacy_payload
from pagw.fhir import InvalidFHIR, claim_resource, fixture, parse_submission
from pagw.http import bounded_request, retry_after
from pagw.legacy import LegacyRequest


def test_legacy_mapping_preserves_supported_clinical_fields():
    bundle = fixture("beta")
    claim = bundle["entry"][0]["resource"]
    claim["item"][0]["quantity"] = {"value": 3, "unit": "sessions"}
    claim["diagnosis"].append(
        {
            "sequence": 2,
            "diagnosisCodeableConcept": {
                "coding": [{"system": "http://hl7.org/fhir/sid/icd-10-cm", "code": "M17.9"}]
            },
        }
    )
    claim["item"][0]["diagnosisSequence"] = [1, 2]
    rid = uuid4()
    mapped = legacy_payload({"id": rid, "bundle": bundle})
    contract = LegacyRequest.model_validate_json(json.dumps(mapped))
    assert contract.externalRequestId == rid
    assert "sourceBundle" not in mapped and "resourceType" not in mapped
    assert contract.member.subscriberId == "FAKE-MEMBER"
    assert contract.member.identifiers[0].value == "FAKE-001"
    assert contract.services[0].quantity == {"value": 3, "unit": "sessions"}
    assert contract.services[0].diagnosisLines == [1, 2]
    assert contract.diagnoses[1].codes[0]["code"] == "M17.9"
    assert contract.claimType == claim["type"]


def test_standalone_claim_references_are_contained(store):
    rid, _ = admit(store)
    result = claim_resource(store.request("tenant-a", rid))
    assert result["id"] == str(rid)
    targets = {"#" + r["id"] for r in result["contained"]} | {"#"}

    def check(value):
        if isinstance(value, dict):
            if "reference" in value:
                assert value["reference"] in targets
            for child in value.values():
                check(child)
        elif isinstance(value, list):
            for child in value:
                check(child)

    check(result)


class Chunks(httpx.SyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks

    def __iter__(self):
        yield from self.chunks


def test_bounded_partner_read_rejects_oversize_without_consuming_rest():
    consumed = []

    def stream():
        for _ in range(20):
            consumed.append(1)
            yield b"x" * 100

    transport = httpx.MockTransport(lambda request: httpx.Response(200, stream=Chunks(stream())))
    with httpx.Client(transport=transport) as client, pytest.raises(httpx.ReadError):
        bounded_request(client, "GET", "https://payer.example", limit=150)
    assert len(consumed) == 2


def test_bounded_partner_read_rejects_slow_trickle(monkeypatch):
    clock = [0]
    monkeypatch.setattr("pagw.http.time.monotonic", lambda: clock[0])

    def stream():
        clock[0] = 1
        yield b"a"
        clock[0] = 10
        yield b"b"

    transport = httpx.MockTransport(lambda request: httpx.Response(200, stream=Chunks(stream())))
    with httpx.Client(transport=transport) as client, pytest.raises(httpx.ReadError):
        bounded_request(client, "GET", "https://payer.example", seconds=5)


@pytest.mark.parametrize("value", [[], None, {"resourceType": []}])
def test_malformed_resource_type_is_validation_error(value):
    from pagw.fhir import validate_resource

    with pytest.raises(InvalidFHIR):
        validate_resource(value)


def test_malformed_signed_scope_is_unauthorized(client, token, settings):
    claims = jwt.decode(token(), options={"verify_signature": False})
    claims["scope"] = ["ops"]
    signed = jwt.encode(
        claims,
        Path(settings.idp_key_path).read_bytes(),
        algorithm="RS256",
        headers={"kid": "demo-idp"},
    )
    assert (
        client.get("/api/requests", headers={"Authorization": "Bearer " + signed}).status_code
        == 401
    )


def test_metrics_reflect_database_state_and_tenant(client, token):
    submit(client, token)
    response = client.get("/metrics", headers=headers(token))
    assert response.status_code == 200
    assert 'pagw_durable_operations{kind="SUBMIT",state="READY"} 1.0' in response.text
    assert 'pagw_authorizations{decision="UNKNOWN",delivery="RECEIVED"} 1.0' in response.text
    assert client.get("/metrics", headers=headers(token, "provider-b")).status_code == 403


@pytest.mark.parametrize(
    "mutation",
    [
        lambda claim: claim.update(
            supportingInfo=[{"sequence": 1, "category": {"text": "unsupported"}}]
        ),
        lambda claim: claim["item"][0].update(net={"value": 50}),
        lambda claim: claim["item"][0].update(servicedPeriod={"start": "2027-01-01"}),
        lambda claim: claim["item"][0]["quantity"].update(value=-1),
    ],
)
def test_unsupported_or_ambiguous_clinical_fields_rejected(mutation):
    bundle = fixture()
    mutation(bundle["entry"][0]["resource"])
    with pytest.raises(InvalidFHIR):
        parse_submission(bundle)


def test_retry_after_supports_http_dates():
    from datetime import timedelta
    from email.utils import format_datetime

    assert retry_after("60") == 60
    assert 58 < retry_after(format_datetime(datetime.now(UTC) + timedelta(seconds=60))) <= 60
    assert retry_after("not-a-date") == 5


def test_submission_worker_does_not_consume_webhook_queue(store, settings):
    from pagw.worker import Worker

    visited = []

    class Broker:
        def receive(self, queue):
            visited.append(queue)
            return []

    Worker(store, settings, Broker()).once(("submit",))
    assert visited == ["submit"]


def test_local_process_identity_handles_workspace_spaces(monkeypatch):
    import runpy
    import subprocess
    import sys
    from types import SimpleNamespace

    module = runpy.run_path("scripts/dev.py", run_name="test_dev")
    root = module["ROOT"]

    def run(command, **kwargs):
        if command[0] == "ps":
            return SimpleNamespace(stdout=f"{sys.executable} -m pagw.cli worker --queue submit\n")
        return SimpleNamespace(stdout=f"p123\nn{root}\n")

    monkeypatch.setattr(subprocess, "run", run)
    assert module["registered"](2_000_000_000, "worker")
    assert not module["registered"](2_000_000_000, "webhooks")


def test_readiness_requires_schema_version(client, monkeypatch):
    monkeypatch.setattr(client.app.state.store.db, "one", lambda *args: {"version": 1})
    assert client.get("/readyz").status_code == 503
