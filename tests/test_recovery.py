from dataclasses import asdict

import pytest
from test_durability import Connector, admit, approval

from pagw.connectors import PartnerError
from pagw.store import Conflict
from pagw.worker import Worker


def due(store, op_id):
    with store.db.transaction() as c:
        c.execute("UPDATE operations SET due_at=clock_timestamp() WHERE id=%s", (op_id,))


def test_retry_exhaustion_needs_review_then_preserves_identity(store, settings, broker):
    rid, op = admit(store)
    ids = []

    def fail(row):
        ids.append(row["id"])
        raise PartnerError("PAYER_UNCERTAIN", True, ambiguous=True)

    worker = Worker(store, settings, broker)
    worker.connectors["alpha"] = Connector(fail)
    for _ in range(settings.max_attempts):
        due(store, op["id"])
        worker.handle(op["id"])
    assert store.request("tenant-a", rid)["delivery_state"] == "NEEDS_REVIEW"
    assert not worker.handle(op["id"])
    store.recover("tenant-a", rid, actor_id="test-operator")
    worker.connectors["alpha"] = Connector(lambda row: ids.append(row["id"]) or approval(rid))
    worker.handle(op["id"])
    assert set(ids) == {rid}
    assert store.request("tenant-a", rid)["decision"] == "APPROVED"
    assert store.db.one("SELECT count(*) AS n FROM audit WHERE action='request.recover'")["n"] == 1
    assert (
        store.db.one("SELECT count(*) AS n FROM operation_events WHERE outcome='CLAIMED'")["n"]
        == settings.max_attempts + 1
    )


def test_unknown_exception_is_bounded_and_not_logged_as_payload(store, settings, broker, caplog):
    rid, op = admit(store)

    def fail(row):
        raise RuntimeError("SECRET-PATIENT-DATA")

    worker = Worker(store, settings, broker)
    worker.connectors["alpha"] = Connector(fail)
    for _ in range(settings.max_attempts):
        due(store, op["id"])
        worker.handle(op["id"])
    assert store.request("tenant-a", rid)["delivery_state"] == "NEEDS_REVIEW"
    assert "SECRET-PATIENT-DATA" not in caplog.text


def test_repeated_crashes_have_bounded_external_attempts(store, settings, broker):
    rid, op = admit(store)
    for _ in range(settings.max_attempts):
        assert store.claim(op["id"])
        with store.db.transaction() as c:
            c.execute(
                "UPDATE operations SET lease_until=clock_timestamp()-interval '1 second' WHERE id=%s",
                (op["id"],),
            )
    worker = Worker(store, settings, broker)
    calls = []
    worker.connectors["alpha"] = Connector(lambda row: calls.append(1) or approval(rid))
    worker.handle(op["id"])
    assert not calls
    assert store.request("tenant-a", rid)["delivery_state"] == "NEEDS_REVIEW"


def test_poll_schedule_commits_with_payer_acceptance(store, settings, broker):
    from pagw.connectors import Result

    rid, op = admit(store)
    worker = Worker(store, settings, broker)
    worker.connectors["alpha"] = Connector(lambda row: Result("payer-one", str(rid), "PENDED", 1))
    worker.handle(op["id"])
    poll = store.db.one("SELECT * FROM operations WHERE request_id=%s AND kind='POLL'", (rid,))
    assert poll["state"] == "READY"
    assert store.db.one("SELECT 1 FROM outbox WHERE operation_id=%s", (poll["id"],))


def test_approval_reference_conflict_is_retained(store):
    rid, _ = admit(store)
    store.ingest("alpha", "first", asdict(approval(rid)))
    state = store.ingest(
        "alpha", "conflict", asdict(approval(rid, 3)) | {"preauth_ref": "CHANGED-REFERENCE"}
    )
    assert state == "CONFLICT"
    assert store.request("tenant-a", rid)["preauth_ref"] == "SYNTHETIC-APPROVAL"


def test_webhook_redelivery_keeps_event_identity(store, settings):
    store.subscribe(
        "tenant-a",
        settings.receiver_url,
        settings.webhook_secret.get_secret_value(),
        actor_id="test-operator",
    )
    rid, _ = admit(store)
    store.ingest("alpha", "a", asdict(approval(rid)))
    before = store.db.one("SELECT * FROM deliveries WHERE request_id=%s", (rid,))
    with store.db.transaction() as c:
        c.execute("UPDATE deliveries SET state='DEAD' WHERE id=%s", (before["id"],))
        c.execute("UPDATE operations SET state='DEAD' WHERE delivery_id=%s", (before["id"],))
    store.redeliver("tenant-a", before["id"], actor_id="test-operator")
    after = store.db.one("SELECT * FROM deliveries WHERE id=%s", (before["id"],))
    assert after["event_id"] == before["event_id"]
    assert after["payload"] == before["payload"]
    assert after["state"] == "PENDING"
    with pytest.raises(Conflict):
        store.redeliver("tenant-a", before["id"], actor_id="test-operator")


def test_history_is_append_only(store):
    import psycopg

    rid, _ = admit(store)
    with pytest.raises(psycopg.Error), store.db.transaction() as c:
        c.execute("UPDATE history SET reason='rewritten' WHERE request_id=%s", (rid,))
