"""Workflow recovery, wire compatibility, and admission contract tests."""

import json
import time
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import asdict
from uuid import uuid4

import psycopg
import pytest
from pydantic import ValidationError
from test_api_security import headers
from test_durability import Connector, admit, approval

from pagw.config import Settings
from pagw.connectors import PartnerError, Result, legacy_payload
from pagw.fhir import canonical, claim_response, fixture, parse_submission
from pagw.legacy import LegacyRequest
from pagw.security import callback_key, signature
from pagw.worker import Worker


def make_due(store, op_id):
    with store.db.transaction() as c:
        c.execute("UPDATE operations SET due_at=clock_timestamp() WHERE id=%s", (op_id,))


def pending(rid, sequence):
    return Result("payer-one", str(rid), "PENDED", sequence)


def rejected(_):
    raise PartnerError("PAYER_REJECTED", False)


def test_new_pending_callback_restarts_dead_poll_without_resubmission(store, settings, broker):
    rid, submit = admit(store)
    worker = Worker(store, settings, broker)
    worker.connectors["alpha"] = Connector(lambda _: pending(rid, 1))
    worker.handle(submit["id"])
    poll = store.db.one("SELECT * FROM operations WHERE request_id=%s AND kind='POLL'", (rid,))
    worker.connectors["alpha"] = Connector(rejected)
    make_due(store, poll["id"])
    worker.handle(poll["id"])
    assert store.request("tenant-a", rid)["delivery_state"] == "NEEDS_REVIEW"
    assert store.ingest("alpha", "new-pending", asdict(pending(rid, 2))) == "APPLIED"
    active = store.db.one("SELECT * FROM operations WHERE id=%s", (poll["id"],))
    assert active["state"] == "READY" and active["attempts"] == 0
    assert active["generation"] > poll["generation"]
    assert store.db.one(
        "SELECT 1 FROM outbox WHERE operation_id=%s AND generation=%s AND published_at IS NULL",
        (poll["id"], active["generation"]),
    )
    assert store.request("tenant-a", rid)["delivery_state"] == "SUBMITTED"
    # Equal-sequence replay cannot repeatedly reset the budget or schedule.
    assert store.ingest("alpha", "same-pending", asdict(pending(rid, 2))) == "STALE"
    assert store.db.one("SELECT * FROM operations WHERE id=%s", (poll["id"],)) == active
    assert (
        store.db.one("SELECT attempts FROM operations WHERE id=%s", (submit["id"],))["attempts"]
        == 1
    )
    worker.connectors["alpha"] = Connector(lambda _: approval(rid, 3))
    make_due(store, poll["id"])
    worker.handle(poll["id"])
    assert store.request("tenant-a", rid)["decision"] == "APPROVED"


def test_pending_observation_preserves_live_poll_lease(store):
    rid, _ = admit(store)
    store.ingest("alpha", "initial", asdict(pending(rid, 1)))
    poll = store.db.one("SELECT * FROM operations WHERE request_id=%s AND kind='POLL'", (rid,))
    make_due(store, poll["id"])
    owned = store.claim(poll["id"])
    store.ingest("alpha", "next", asdict(pending(rid, 2)))
    assert store.db.one("SELECT * FROM operations WHERE id=%s", (poll["id"],)) == owned


def test_expired_pending_stays_expired_but_terminal_callback_reconciles(store):
    rid, _ = admit(store)
    store.ingest("alpha", "initial", asdict(pending(rid, 1)))
    with store.db.transaction() as c:
        c.execute(
            "UPDATE requests SET expires_at=clock_timestamp()-interval '1 second' WHERE id=%s",
            (rid,),
        )
    store.expire()
    store.ingest("alpha", "late-pending", asdict(pending(rid, 2)))
    assert store.request("tenant-a", rid)["delivery_state"] == "EXPIRED"
    assert (
        store.db.one("SELECT state FROM operations WHERE request_id=%s AND kind='POLL'", (rid,))[
            "state"
        ]
        == "DEAD"
    )
    store.ingest("alpha", "late-approval", asdict(approval(rid, 3)))
    assert store.request("tenant-a", rid)["decision"] == "APPROVED"


@pytest.mark.parametrize("sequence,state", [(1, "STALE"), (3, "CONFLICT")])
def test_early_terminal_callback_preserves_http_observation(
    store, settings, broker, sequence, state
):
    rid, op = admit(store)

    def early(_):
        store.ingest("alpha", "early", asdict(approval(rid)))
        return Result("payer-one", str(rid), "DENIED", sequence)

    worker = Worker(store, settings, broker)
    worker.connectors["alpha"] = Connector(early)
    worker.handle(op["id"])
    assert store.request("tenant-a", rid)["decision"] == "APPROVED"
    observation = store.db.one("SELECT * FROM payer_observations WHERE request_id=%s", (rid,))
    assert observation["state"] == state
    assert observation["observation"]["decision"] == "DENIED"
    assert observation["claim_token"] is not None
    assert store.db.one("SELECT state FROM operations WHERE id=%s", (op["id"],))["state"] == "DONE"
    with pytest.raises(psycopg.Error), store.db.transaction() as c:
        c.execute("DELETE FROM payer_observations WHERE request_id=%s", (rid,))


def test_concurrent_duplicate_payer_reference_is_retained_as_conflict(store):
    ids = [admit(store, key)[0] for key in ("one", "two")]
    with ThreadPoolExecutor(max_workers=2) as pool:
        states = list(
            pool.map(lambda rid: store.ingest("alpha", str(rid), asdict(pending(rid, 1))), ids)
        )
    assert sorted(states) == ["APPLIED", "CONFLICT"]
    assert store.db.one("SELECT count(*) AS n FROM inbox")["n"] == 2
    assert store.db.one("SELECT count(*) AS n FROM requests WHERE payer_ref='payer-one'")["n"] == 1


def test_duplicate_payer_reference_callback_is_not_database_unavailable(client, token, settings):
    ids = []
    for key in ("one", "two"):
        response = client.post(
            "/fhir/Claim/$submit",
            json=fixture(business_key=key),
            headers={**headers(token), "Idempotency-Key": key},
        )
        assert response.status_code == 202
        ids.append(response.json()["id"])
    for rid, expected in zip(ids, ["APPLIED", "CONFLICT"], strict=True):
        body = canonical(asdict(pending(rid, 1)) | {"event_id": rid})
        timestamp = int(time.time())
        response = client.post(
            "/payer-callbacks/alpha",
            content=body,
            headers={
                "X-PA-Timestamp": str(timestamp),
                "X-PA-Signature": "v1="
                + signature(callback_key(settings, "alpha"), body, timestamp),
            },
        )
        assert response.status_code == 200
        assert response.json()["state"] == expected


@pytest.mark.parametrize(
    "mutation",
    [
        "modifier",
        "insurance",
        "identifiers",
        "diagnoses",
        "subscriber",
        "identifier-value",
        "coverage-period",
    ],
)
def test_unsupported_mapping_is_rejected_before_durable_acceptance(client, token, mutation):
    b = fixture("beta")
    claim, patient, coverage = [x["resource"] for x in b["entry"][:3]]
    if mutation == "modifier":
        patient["modifierExtension"] = [
            {"url": "https://example.org/meaning", "valueBoolean": True}
        ]
    elif mutation == "insurance":
        claim["insurance"][0]["preAuthRef"] = ["EXISTING-APPROVAL"]
    elif mutation == "identifiers":
        patient["identifier"] = [{"system": "urn:member", "value": str(i)} for i in range(11)]
    elif mutation == "diagnoses":
        claim["diagnosis"] = [
            dict(deepcopy(claim["diagnosis"][0]), sequence=i) for i in range(1, 22)
        ]
    elif mutation == "subscriber":
        coverage["subscriberId"] = "X" * 129
    elif mutation == "identifier-value":
        patient["identifier"][0]["value"] = "X" * 129
    else:
        coverage["period"] = {"end": "2020-01-01"}
    response = client.post(
        "/fhir/Claim/$submit", json=b, headers={**headers(token), "Idempotency-Key": "invalid"}
    )
    assert response.status_code == 422
    assert response.json()["resourceType"] == "OperationOutcome"
    assert client.app.state.store.db.one("SELECT count(*) AS n FROM requests")["n"] == 0


@pytest.mark.parametrize("group", ["item", "diagnosis", "insurance"])
def test_absent_sequence_returns_422_instead_of_key_error(client, token, group):
    b = fixture()
    element = b["entry"][0]["resource"][group][0]
    element.pop("sequence")
    element["_sequence"] = {
        "extension": [
            {
                "url": "http://hl7.org/fhir/StructureDefinition/data-absent-reason",
                "valueCode": "unknown",
            }
        ]
    }
    response = client.post(
        "/fhir/Claim/$submit", json=b, headers={**headers(token), "Idempotency-Key": "invalid"}
    )
    assert response.status_code == 422


@pytest.mark.parametrize(
    "field,value",
    [
        ("versionId", "7"),
        ("lastUpdated", "2026-10-20T00:00:00Z"),
        ("security", [{"system": "urn:security", "code": "R"}]),
    ],
)
@pytest.mark.parametrize("index", [0, 1, 2, 3, 4])
def test_unrepresentable_contained_metadata_rejected(client, token, index, field, value):
    b = fixture()
    b["entry"][index]["resource"]["meta"] = {field: value}
    response = client.post(
        "/fhir/Claim/$submit", json=b, headers={**headers(token), "Idempotency-Key": "invalid"}
    )
    assert response.status_code == 422


def test_supported_metadata_and_legacy_boundary_values_are_preserved():
    b = fixture("beta")
    patient = b["entry"][1]["resource"]
    patient["identifier"] = [{"system": "urn:member", "value": str(i)} for i in range(10)]
    patient["meta"] = {"tag": [{"system": "urn:test", "code": "synthetic"}]}
    parse_submission(b)
    contract = LegacyRequest.model_validate_json(
        json.dumps(legacy_payload({"bundle": b, "id": uuid4()}))
    )
    assert len(contract.member.identifiers) == 10
    assert contract.member.gender == "unknown"
    result = claim_response(
        dict(
            bundle=b,
            id=uuid4(),
            delivery_state="RECEIVED",
            decision="UNKNOWN",
            created_at="2026-10-20T00:00:00Z",
            version=0,
            preauth_ref=None,
        )
    )
    assert (
        next(r for r in result["contained"] if r["resourceType"] == "Patient")["meta"]
        == patient["meta"]
    )


def test_audit_actor_comes_from_verified_principal(client, token, settings):
    response = client.post(
        "/webhooks/subscriptions",
        json={"url": settings.receiver_url},
        headers=headers(token, "provider-a"),
    )
    assert response.status_code == 201
    response = client.post(
        "/fhir/Claim/$submit",
        json=fixture(),
        headers={**headers(token), "Idempotency-Key": "cancel"},
    )
    rid = response.json()["id"]
    assert (
        client.post(
            f"/api/requests/{rid}/cancel", headers={**headers(token), "X-Actor-Id": "forged"}
        ).status_code
        == 204
    )
    with client.app.state.store.db.transaction() as c:
        audit = c.execute("SELECT action,actor_id FROM audit ORDER BY id").fetchall()
    assert audit == [
        {"action": "subscription.create", "actor_id": "provider-a"},
        {"action": "request.cancel", "actor_id": "operator"},
    ]


@pytest.mark.parametrize(
    "role,fields",
    [
        ("api", {"callback_secret", "encryption_key"}),
        ("worker", {"payer_client_secret", "client_private_key"}),
        ("webhooks", {"encryption_key"}),
        ("relay", set()),
        ("scheduler", set()),
        ("migrate", set()),
    ],
)
def test_production_service_starts_with_only_its_credentials(settings, role, fields):
    base = settings.model_dump()
    for name in (
        "callback_secret",
        "encryption_key",
        "webhook_secret",
        "payer_client_secret",
        "client_private_key",
    ):
        base[name] = ""
    for name in fields:
        base[name] = getattr(settings, name) if name != "client_private_key" else "test-key"
    base.update(
        mode="production",
        service_role=role,
        database_url="postgresql://gateway@db.example/pagw?sslmode=verify-full",
        broker="sqs",
        issuer="https://issuer.example",
        jwks_url="https://issuer.example/keys",
        payer_base_url="https://payer.example",
        callback_base_url="https://gateway.example",
        receiver_url="https://receiver.example/events",
        client_registry={"operator": {"tenant": "tenant-a", "scopes": ["read"]}},
    )
    configured = Settings(_env_file=None, **base)
    assert configured.service_role == role
    for name in fields:
        with pytest.raises(ValidationError):
            Settings(_env_file=None, **(base | {name: ""}))


def test_new_submission_retries_use_frozen_payload(store, settings, monkeypatch):
    from pagw.connectors import HTTPConnector
    from pagw.security import TokenManager

    rid, _ = admit(store, payer="beta")
    row = store.request("tenant-a", rid)
    expected = deepcopy(row["submission_payload"])
    assert expected["member"]["gender"] == "unknown"

    # The retry path must not invoke the current admission parser or mapper.
    def fail(*args, **kwargs):
        raise AssertionError("retry remapped admitted data")

    monkeypatch.setattr("pagw.connectors.map_legacy", fail)
    monkeypatch.setattr("pagw.connectors.parse_submission", fail)
    connector = HTTPConnector(settings, "beta", TokenManager(settings))
    captured = []
    monkeypatch.setattr(
        connector, "_call", lambda method, path, row, payload: captured.append(payload)
    )
    connector.submit(row)
    connector.submit(row)
    assert captured == [expected, expected]


def test_pre_upgrade_beta_retries_keep_v1_wire_payload(settings, monkeypatch):
    from pagw.connectors import HTTPConnector
    from pagw.legacy import map_legacy
    from pagw.security import TokenManager

    b = fixture("beta")
    parsed = parse_submission(b)
    # This was accepted by v1 but is outside the new admission subset.
    b["entry"][1]["resource"]["active"] = True
    rid = uuid4()
    row = {
        "id": rid,
        "bundle": b,
        "business_system": parsed["business_system"],
        "business_value": parsed["business_value"],
        "submission_payload": None,
    }
    expected_v1 = map_legacy(b, parsed, rid, version=1)
    assert "gender" not in expected_v1["member"]
    connector = HTTPConnector(settings, "beta", TokenManager(settings))
    captured = []
    monkeypatch.setattr(
        connector, "_call", lambda method, path, row, payload: captured.append(payload)
    )
    connector.submit(row)
    assert captured == [expected_v1]


def test_snapshot_is_not_disclosed_in_operator_metadata(client, token):
    response = client.post(
        "/fhir/Claim/$submit",
        json=fixture(),
        headers={**headers(token), "Idempotency-Key": "snapshot"},
    )
    detail = client.get("/api/requests/" + response.json()["id"], headers=headers(token))
    assert detail.status_code == 200
    assert "submission_payload" not in detail.json()["request"]


def test_new_pending_after_sla_does_not_reactivate_before_scheduler_tick(store):
    rid, _ = admit(store)
    with store.db.transaction() as c:
        c.execute(
            "UPDATE requests SET expires_at=clock_timestamp()-interval '1 second' WHERE id=%s",
            (rid,),
        )
    store.ingest("alpha", "after-sla", asdict(pending(rid, 1)))
    assert store.request("tenant-a", rid)["delivery_state"] == "EXPIRED"
    assert (
        store.db.one("SELECT 1 FROM operations WHERE request_id=%s AND kind='POLL'", (rid,)) is None
    )


def test_existing_receipt_replays_before_new_admission_policy(store, monkeypatch):
    b = fixture("beta")
    original = store.submit("tenant-a", "original", b)

    def reject(*args, **kwargs):
        raise AssertionError("existing receipt revalidated under a new policy")

    monkeypatch.setattr("pagw.store.parse_submission", reject)
    replay = store.submit("tenant-a", "original", b)
    assert replay == (original[0], original[1], True)
    changed = deepcopy(b)
    changed["entry"][0]["resource"]["item"][0]["quantity"]["value"] = 2
    from pagw.store import Conflict

    with pytest.raises(Conflict, match="IDEMPOTENCY_PAYLOAD_MISMATCH"):
        store.submit("tenant-a", "original", changed)
