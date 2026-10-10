from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from threading import Event
from uuid import UUID, uuid4

import pytest

from pagw.connectors import Result
from pagw.fhir import fixture
from pagw.queueing import Message, relay
from pagw.store import Conflict
from pagw.worker import Worker


def admit(store, key="one", payer="alpha", code="70551"):
    _, body, _ = store.submit("tenant-a", key, fixture(payer, code, key))
    rid = UUID(body["id"])
    op = store.db.one("SELECT * FROM operations WHERE request_id=%s AND kind='SUBMIT'", (rid,))
    return rid, op


def approval(rid, sequence=2):
    return Result("payer-one", str(rid), "APPROVED", sequence, "SYNTHETIC-APPROVAL")


class Connector:
    def __init__(self, function):
        self.function = function

    def submit(self, row):
        return self.function(row)

    def poll(self, row):
        return self.function(row)


def test_concurrent_same_key_one_admission(store):
    bundle = fixture()
    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(lambda _: store.submit("tenant-a", "same", bundle), range(50)))
    assert len({r[1]["id"] for r in results}) == 1
    assert sum(not r[2] for r in results) == 1
    assert store.db.one("SELECT count(*) AS n FROM requests")["n"] == 1
    assert store.db.one("SELECT count(*) AS n FROM outbox")["n"] == 1


def test_concurrent_new_keys_same_business_identity(store):
    bundle = fixture()
    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(lambda n: store.submit("tenant-a", f"key-{n}", bundle), range(50)))
    assert len({r[1]["id"] for r in results}) == 1
    assert store.db.one("SELECT count(*) AS n FROM operations")["n"] == 1


@pytest.mark.parametrize("key", ["same", "new"])
def test_payload_mismatch_rejected(store, key):
    bundle = fixture()
    store.submit("tenant-a", "same", bundle)
    bundle["entry"][0]["resource"]["item"][0]["quantity"]["value"] = 2
    with pytest.raises(Conflict):
        store.submit("tenant-a", key, bundle)


def test_atomic_admission_rolls_back_on_failure(store, monkeypatch):
    def fail(*_, **kwargs):
        raise RuntimeError("fault-injection")

    monkeypatch.setattr(store, "_operation", fail)
    with pytest.raises(RuntimeError):
        store.submit("tenant-a", "one", fixture())
    assert store.db.one("SELECT count(*) AS n FROM requests")["n"] == 0
    assert store.db.one("SELECT count(*) AS n FROM history")["n"] == 0


def test_only_one_current_owner(store):
    rid, op = admit(store)
    with ThreadPoolExecutor(max_workers=8) as pool:
        claims = list(pool.map(lambda _: store.claim(op["id"]), range(20)))
    assert sum(x is not None for x in claims) == 1


def test_stale_worker_cannot_overwrite_new_owner(store, settings, broker):
    rid, op = admit(store)
    entered, release = Event(), Event()

    def slow(row):
        entered.set()
        assert release.wait(5)
        return Result("payer-one", str(rid), "PENDED", 1)

    old = Worker(store, settings, broker)
    old.connectors["alpha"] = Connector(slow)
    new = Worker(store, settings, broker)
    new.connectors["alpha"] = Connector(lambda _: approval(rid))
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(old.handle, op["id"])
        assert entered.wait(5)
        with store.db.transaction() as c:
            c.execute(
                "UPDATE operations SET lease_until=clock_timestamp()-interval '1 second' WHERE id=%s",
                (op["id"],),
            )
        assert new.handle(op["id"])
        release.set()
        pending.result(timeout=5)
    row = store.request("tenant-a", rid)
    assert row["decision"] == "APPROVED"
    assert row["last_sequence"] == 2
    assert store.db.one("SELECT attempts FROM operations WHERE id=%s", (op["id"],))["attempts"] == 2


def test_callback_before_submit_response(store, settings, broker):
    rid, op = admit(store)

    def early(row):
        assert store.ingest("alpha", "early", asdict(approval(rid))) == "APPLIED"
        return Result("payer-one", str(rid), "PENDED", 1)

    worker = Worker(store, settings, broker)
    worker.connectors["alpha"] = Connector(early)
    worker.handle(op["id"])
    assert store.request("tenant-a", rid)["decision"] == "APPROVED"


def test_callback_deduplication_conflict_and_stale(store):
    rid, _ = admit(store)
    body = asdict(approval(rid))
    assert store.ingest("alpha", "a", body) == "APPLIED"
    assert store.ingest("alpha", "a", body) == "APPLIED"
    with pytest.raises(Conflict):
        store.ingest("alpha", "a", dict(body, sequence=3))
    assert (
        store.ingest("alpha", "older", dict(body, decision="PENDED", sequence=1, preauth_ref=None))
        == "STALE"
    )
    assert (
        store.ingest(
            "alpha", "contradiction", dict(body, decision="DENIED", sequence=3, preauth_ref=None)
        )
        == "CONFLICT"
    )
    assert store.request("tenant-a", rid)["decision"] == "APPROVED"
    assert store.db.one("SELECT count(*) AS n FROM history")["n"] == 2


def test_unmatched_callback_durable_then_reconciled(store, monkeypatch):
    rid = uuid4()
    assert store.ingest("alpha", "unmatched", asdict(approval(rid))) == "UNMATCHED"
    # Reserve a known future UUID at the admission boundary; never mutate inbox
    # contents or its hash to simulate the race.
    identities = iter([rid, *[uuid4() for _ in range(10)]])
    monkeypatch.setattr("pagw.store.uuid4", lambda: next(identities))
    admitted, _ = admit(store)
    assert admitted == rid
    store.reconcile_inbox()
    assert store.request("tenant-a", admitted)["decision"] == "APPROVED"


def test_partial_batch_success_and_ten_entry_limit(store):
    for n in range(23):
        admit(store, str(n))
    batches = []

    class Partial:
        def publish(self, queue, entries):
            batches.append(entries)
            return {x["Id"] for x in entries[:3]}

    assert relay(store.db, Partial()) == 3
    assert max(map(len, batches)) <= 10
    assert store.db.one("SELECT count(*) AS n FROM outbox WHERE published_at IS NULL")["n"] == 20


def test_lost_broker_message_recreated_from_database(store, broker):
    _, op = admit(store)
    assert relay(store.db, broker) == 1
    with store.db.transaction() as c:
        c.execute("DELETE FROM local_messages")
        c.execute("UPDATE outbox SET published_at=clock_timestamp()-interval '10 seconds'")
    store.sweep()
    assert relay(store.db, broker) == 1
    assert broker.receive("submit")


def test_duplicate_message_has_one_committed_effect(store, settings, broker):
    rid, op = admit(store)
    calls = []
    worker = Worker(store, settings, broker)
    worker.connectors["alpha"] = Connector(lambda row: calls.append(1) or approval(rid))
    worker.handle(op["id"])
    worker.handle(op["id"])
    assert calls == [1]
    assert store.request("tenant-a", rid)["decision"] == "APPROVED"


def test_cancel_only_before_dispatch(store):
    rid, op = admit(store)
    store.cancel("tenant-a", rid, actor_id="test-operator")
    assert store.claim(op["id"]) is None
    rid2, op2 = admit(store, "two")
    store.claim(op2["id"])
    with pytest.raises(Conflict):
        store.cancel("tenant-a", rid2, actor_id="test-operator")


def test_expiry_does_not_require_poll_message(store):
    rid, op = admit(store)
    with store.db.transaction() as c:
        c.execute(
            "UPDATE requests SET expires_at=clock_timestamp()-interval '1 second' WHERE id=%s",
            (rid,),
        )
    store.expire()
    assert store.request("tenant-a", rid)["delivery_state"] == "EXPIRED"
    assert store.claim(op["id"]) is None


def test_poison_quarantined_without_corrupting_request(store, broker, settings):
    rid, _ = admit(store)
    Worker(store, settings, broker).consume(Message("not-json", str(uuid4()), "submit"))
    assert store.db.one("SELECT count(*) AS n FROM quarantine")["n"] == 1
    assert store.request("tenant-a", rid)["delivery_state"] == "RECEIVED"
