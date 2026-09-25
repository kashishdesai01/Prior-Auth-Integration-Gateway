from dataclasses import asdict
from uuid import UUID, uuid4

from opentelemetry.propagate import inject
from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb

from pagw.config import Settings
from pagw.connectors import Result
from pagw.db import Database
from pagw.fhir import InvalidFHIR, claim_response, digest, parse_submission
from pagw.legacy import map_legacy
from pagw.security import cipher


class Conflict(ValueError):
    pass


class NotFound(ValueError):
    pass


class Store:
    def __init__(self, db: Database, settings: Settings):
        self.db, self.settings = db, settings

    def _wake(self, c, operation: dict):
        carrier: dict[str, str] = {}
        inject(carrier)
        if not carrier.get("traceparent"):
            prior = c.execute(
                "SELECT b.traceparent FROM outbox b JOIN operations o ON o.id=b.operation_id WHERE b.traceparent IS NOT NULL AND (o.id=%s OR (o.request_id IS NOT NULL AND o.request_id=%s)) ORDER BY b.created_at DESC LIMIT 1",
                (operation["id"], operation.get("request_id")),
            ).fetchone()
            if prior:
                carrier["traceparent"] = prior["traceparent"]
        c.execute(
            "INSERT INTO outbox(id,operation_id,generation,queue,traceparent) VALUES(%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
            (
                uuid4(),
                operation["id"],
                operation["generation"],
                "webhook" if operation["kind"] == "WEBHOOK" else "submit",
                carrier.get("traceparent"),
            ),
        )

    def _operation(
        self, c, kind: str, request_id=None, delivery_id=None, delay=0, reactivate=False
    ):
        op = c.execute(
            "INSERT INTO operations(id,request_id,delivery_id,kind,due_at) VALUES(%s,%s,%s,%s,clock_timestamp()+(%s * interval '1 second')) ON CONFLICT DO NOTHING RETURNING *",
            (uuid4(), request_id, delivery_id, kind, delay),
        ).fetchone()
        if not op and reactivate:
            # Request lock is held by the caller. A newer pending observation
            # permits another bounded poll cycle, never another submission.
            op = c.execute(
                "UPDATE operations SET state='READY',attempts=0,generation=generation+1,due_at=clock_timestamp()+(%s * interval '1 second'),last_error=NULL WHERE request_id=%s AND kind='POLL' AND state IN ('DONE','DEAD') RETURNING *",
                (delay, request_id),
            ).fetchone()
        if op:
            self._wake(c, op)
        return op

    def submit(self, tenant: str, key: str, bundle: dict):
        try:
            fingerprint = digest(bundle)
        except (ValueError, TypeError, RecursionError) as exc:
            raise InvalidFHIR("Bundle", "invalid-json-value") from exc
        # Stored receipts are immutable. Replay them before applying today's
        # admission policy to input accepted by an earlier application version.
        previous = self.db.one(
            "SELECT * FROM idempotency WHERE tenant=%s AND key=%s", (tenant, key)
        )
        if previous:
            if previous["digest"] != fingerprint:
                raise Conflict("IDEMPOTENCY_PAYLOAD_MISMATCH")
            return previous["response_code"], previous["response"], True
        parsed = parse_submission(bundle)
        with self.db.transaction() as c:
            locks = sorted(
                {
                    f"idem:{tenant}:{key}",
                    f"business:{tenant}:{parsed['business_system']}:{parsed['business_value']}",
                }
            )
            for lock in locks:
                c.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (lock,))
            previous = c.execute(
                "SELECT * FROM idempotency WHERE tenant=%s AND key=%s", (tenant, key)
            ).fetchone()
            if previous:
                if previous["digest"] != fingerprint:
                    raise Conflict("IDEMPOTENCY_PAYLOAD_MISMATCH")
                return previous["response_code"], previous["response"], True
            row = c.execute(
                "SELECT * FROM requests WHERE tenant=%s AND business_system=%s AND business_value=%s",
                (tenant, parsed["business_system"], parsed["business_value"]),
            ).fetchone()
            code = 200
            if row:
                if row["digest"] != fingerprint:
                    raise Conflict("BUSINESS_PAYLOAD_MISMATCH")
            else:
                code = 202
                request_id = uuid4()
                payload = (
                    {"bundle": bundle, "correlation_id": str(request_id)}
                    if parsed["payer"] == "alpha"
                    else map_legacy(bundle, parsed, request_id)
                )
                row = c.execute(
                    "INSERT INTO requests(id,tenant,business_system,business_value,digest,bundle,payer,submission_payload,expires_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,clock_timestamp()+(%s * interval '1 second')) RETURNING *",
                    (
                        request_id,
                        tenant,
                        parsed["business_system"],
                        parsed["business_value"],
                        fingerprint,
                        Jsonb(bundle),
                        parsed["payer"],
                        Jsonb(payload),
                        self.settings.sla_seconds,
                    ),
                ).fetchone()
                c.execute(
                    "INSERT INTO history(request_id,version,kind,delivery_state,decision,reason) VALUES(%s,0,'admission','RECEIVED','UNKNOWN','DURABLY_ACCEPTED')",
                    (row["id"],),
                )
                self._operation(c, "SUBMIT", request_id=row["id"])
            response = claim_response(row)
            c.execute(
                "INSERT INTO idempotency(tenant,key,digest,request_id,response,response_code) VALUES(%s,%s,%s,%s,%s,%s)",
                (tenant, key, fingerprint, row["id"], Jsonb(response), code),
            )
            return code, response, False

    def request(self, tenant: str, request_id: UUID):
        row = self.db.one("SELECT * FROM requests WHERE tenant=%s AND id=%s", (tenant, request_id))
        if not row:
            raise NotFound("REQUEST_NOT_FOUND")
        return row

    def list_requests(self, tenant: str, limit=50):
        with self.db.transaction() as c:
            return c.execute(
                "SELECT id,payer,business_value,delivery_state,decision,version,last_error,created_at,updated_at FROM requests WHERE tenant=%s ORDER BY created_at DESC LIMIT %s",
                (tenant, limit),
            ).fetchall()

    def _change(self, c, row: dict, reason: str, **changes):
        # Only this method updates durable request state after admission.
        allowed = {
            "delivery_state",
            "decision",
            "payer_ref",
            "preauth_ref",
            "last_sequence",
            "last_error",
        }
        if not changes.keys() <= allowed:
            raise ValueError("invalid-transition-fields")
        actual = {k: v for k, v in changes.items() if row[k] != v}
        if not actual:
            return row
        if (
            row["decision"] in {"APPROVED", "DENIED"}
            and changes.get("decision", row["decision"]) != row["decision"]
        ):
            raise Conflict("TERMINAL_DECISION_CONFLICT")
        updated = dict(row, **actual, version=row["version"] + 1)
        result = c.execute(
            "UPDATE requests SET delivery_state=%s,decision=%s,payer_ref=%s,preauth_ref=%s,last_sequence=%s,last_error=%s,version=%s,updated_at=clock_timestamp() WHERE id=%s RETURNING *",
            (
                updated["delivery_state"],
                updated["decision"],
                updated["payer_ref"],
                updated["preauth_ref"],
                updated["last_sequence"],
                updated["last_error"],
                updated["version"],
                row["id"],
            ),
        ).fetchone()
        c.execute(
            "INSERT INTO history(request_id,version,kind,delivery_state,decision,reason) VALUES(%s,%s,'transition',%s,%s,%s)",
            (row["id"], result["version"], result["delivery_state"], result["decision"], reason),
        )
        for subscription in c.execute(
            "SELECT id FROM subscriptions WHERE tenant=%s AND enabled", (row["tenant"],)
        ).fetchall():
            delivery_id = uuid4()
            payload = {
                "id": str(uuid4()),
                "type": "authorization.updated",
                "request_id": str(row["id"]),
                "version": result["version"],
                "response": claim_response(result),
            }
            c.execute(
                "INSERT INTO deliveries(id,subscription_id,request_id,version,event_id,payload) VALUES(%s,%s,%s,%s,%s,%s)",
                (
                    delivery_id,
                    subscription["id"],
                    row["id"],
                    result["version"],
                    UUID(payload["id"]),
                    Jsonb(payload),
                ),
            )
            self._operation(c, "WEBHOOK", delivery_id=delivery_id)
        return result

    def _apply_result(self, c, row: dict, result: Result, *, observe_only=False) -> str:
        if result.correlation_id != str(row["id"]) or (
            row["payer_ref"] and row["payer_ref"] != result.payer_ref
        ):
            return "CONFLICT"
        if row["delivery_state"] == "CANCELLED":
            return "CONFLICT"
        if result.sequence < row["last_sequence"]:
            return "STALE"
        if result.sequence == row["last_sequence"]:
            return (
                "STALE"
                if result.decision == row["decision"] and result.preauth_ref == row["preauth_ref"]
                else "CONFLICT"
            )
        if row["decision"] in {"APPROVED", "DENIED"} and (
            result.decision != row["decision"] or result.preauth_ref != row["preauth_ref"]
        ):
            return "CONFLICT"
        # All writers take this lock after their own request lock. Do not lock
        # another request: its owner may be waiting on this same identity fence.
        c.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
            (f"payer-reference:{row['payer']}:{result.payer_ref}",),
        )
        if c.execute(
            "SELECT 1 FROM requests WHERE payer=%s AND payer_ref=%s AND id<>%s",
            (row["payer"], result.payer_ref, row["id"]),
        ).fetchone():
            return "CONFLICT"
        if observe_only:
            return "STALE"
        pending = result.decision not in {"APPROVED", "DENIED"}
        expired = pending and (
            row["delivery_state"] == "EXPIRED"
            or c.execute(
                "SELECT %s <= clock_timestamp() AS expired", (row["expires_at"],)
            ).fetchone()["expired"]
        )
        try:
            # Keep the inbox transaction usable if an out-of-band writer races
            # the advisory-lock contract. The unique constraint remains final.
            with c.transaction():
                self._change(
                    c,
                    row,
                    "PAYER_OBSERVATION",
                    delivery_state="EXPIRED" if expired else "SUBMITTED",
                    decision=result.decision,
                    payer_ref=result.payer_ref,
                    preauth_ref=result.preauth_ref,
                    last_sequence=result.sequence,
                    last_error="SLA_EXCEEDED" if expired else None,
                )
        except UniqueViolation as exc:
            if exc.diag.constraint_name != "requests_payer_payer_ref_key":
                raise
            return "CONFLICT"
        if pending and not expired:
            self._operation(
                c, "POLL", request_id=row["id"], delay=self.settings.poll_seconds, reactivate=True
            )
        return "APPLIED"

    def record_http_observation(self, c, row: dict, op: dict, result: Result, state: str):
        # Written only after checking the operation lease in this transaction.
        c.execute(
            "INSERT INTO payer_observations(id,request_id,operation_id,claim_token,source,state,observation) VALUES(%s,%s,%s,%s,%s,%s,%s)",
            (
                uuid4(),
                row["id"],
                op["id"],
                op["lease_token"],
                op["kind"],
                state,
                Jsonb(asdict(result)),
            ),
        )

    def ingest(self, payer: str, event_id: str, payload: dict):
        result = Result(**payload)
        fingerprint = digest(payload)
        with self.db.transaction() as c:
            c.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                (f"event:{payer}:{event_id}",),
            )
            prior = c.execute(
                "SELECT * FROM inbox WHERE payer=%s AND event_id=%s", (payer, event_id)
            ).fetchone()
            if prior:
                if prior["digest"] != fingerprint:
                    raise Conflict("EVENT_PAYLOAD_MISMATCH")
                return prior["state"]
            row = c.execute(
                "SELECT * FROM requests WHERE id=%s AND payer=%s FOR UPDATE",
                (UUID(result.correlation_id), payer),
            ).fetchone()
            state = self._apply_result(c, row, result) if row else "UNMATCHED"
            c.execute(
                "INSERT INTO inbox(payer,event_id,digest,event,state) VALUES(%s,%s,%s,%s,%s)",
                (payer, event_id, fingerprint, Jsonb(payload), state),
            )
            return state

    def reconcile_inbox(self, limit=50):
        with self.db.transaction() as c:
            events = c.execute(
                "SELECT * FROM inbox WHERE state='UNMATCHED' ORDER BY created_at LIMIT %s", (limit,)
            ).fetchall()
            for entry in events:
                row = c.execute(
                    "SELECT * FROM requests WHERE id=%s AND payer=%s FOR UPDATE",
                    (UUID(entry["event"]["correlation_id"]), entry["payer"]),
                ).fetchone()
                if row:
                    state = self._apply_result(c, row, Result(**entry["event"]))
                    c.execute(
                        "UPDATE inbox SET state=%s WHERE payer=%s AND event_id=%s AND state='UNMATCHED'",
                        (state, entry["payer"], entry["event_id"]),
                    )

    def subscribe(self, tenant: str, url: str, secret: str, *, actor_id: str):
        # A configured exact allowlist avoids DNS rebinding and private-network SSRF.
        if url != self.settings.receiver_url:
            raise Conflict("WEBHOOK_DESTINATION_NOT_ALLOWED")
        if self.settings.mode == "production" and not url.startswith("https://"):
            raise Conflict("WEBHOOK_TLS_REQUIRED")
        encrypted = cipher(self.settings).encrypt(secret.encode())
        with self.db.transaction() as c:
            c.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                (f"subscription:{tenant}:{url}",),
            )
            if c.execute(
                "SELECT 1 FROM subscriptions WHERE tenant=%s AND url=%s", (tenant, url)
            ).fetchone():
                raise Conflict("SUBSCRIPTION_ALREADY_EXISTS")
            row = c.execute(
                "INSERT INTO subscriptions(id,tenant,url,secret) VALUES(%s,%s,%s,%s) RETURNING id,url,enabled",
                (uuid4(), tenant, url, encrypted),
            ).fetchone()
            c.execute(
                "INSERT INTO audit(tenant,action,resource_id,actor_id) VALUES(%s,'subscription.create',%s,%s)",
                (tenant, str(row["id"]), actor_id),
            )
            return row

    def redeliver(self, tenant: str, delivery_id: UUID, *, actor_id: str):
        with self.db.transaction() as c:
            delivery = c.execute(
                "SELECT d.*,s.enabled FROM deliveries d JOIN subscriptions s ON s.id=d.subscription_id WHERE d.id=%s AND s.tenant=%s",
                (delivery_id, tenant),
            ).fetchone()
            if not delivery:
                raise NotFound("DELIVERY_NOT_FOUND")
            op = c.execute(
                "SELECT * FROM operations WHERE delivery_id=%s FOR UPDATE", (delivery_id,)
            ).fetchone()
            if not delivery["enabled"] or delivery["state"] != "DEAD" or op["state"] != "DEAD":
                raise Conflict("REDELIVERY_NOT_ALLOWED")
            c.execute("UPDATE deliveries SET state='PENDING' WHERE id=%s", (delivery_id,))
            updated = c.execute(
                "UPDATE operations SET state='READY',attempts=0,generation=generation+1,due_at=clock_timestamp(),last_error=NULL WHERE id=%s RETURNING *",
                (op["id"],),
            ).fetchone()
            self._wake(c, updated)
            c.execute(
                "INSERT INTO audit(tenant,action,resource_id,actor_id) VALUES(%s,'webhook.redeliver',%s,%s)",
                (tenant, str(delivery_id), actor_id),
            )

    def cancel(self, tenant: str, request_id: UUID, *, actor_id: str):
        with self.db.transaction() as c:
            row = c.execute(
                "SELECT * FROM requests WHERE id=%s AND tenant=%s FOR UPDATE", (request_id, tenant)
            ).fetchone()
            if not row:
                raise NotFound("REQUEST_NOT_FOUND")
            op = c.execute(
                "SELECT * FROM operations WHERE request_id=%s AND kind='SUBMIT' FOR UPDATE",
                (request_id,),
            ).fetchone()
            if row["delivery_state"] != "RECEIVED" or op["attempts"] != 0:
                raise Conflict("CANCEL_ONLY_BEFORE_FIRST_DISPATCH")
            self._change(c, row, "CANCELLED_BEFORE_DISPATCH", delivery_state="CANCELLED")
            c.execute(
                "UPDATE operations SET state='DONE',lease_token=NULL,lease_until=NULL WHERE request_id=%s",
                (request_id,),
            )
            c.execute(
                "INSERT INTO audit(tenant,action,resource_id,actor_id) VALUES(%s,'request.cancel',%s,%s)",
                (tenant, str(request_id), actor_id),
            )

    def recover(self, tenant: str, request_id: UUID, *, actor_id: str):
        with self.db.transaction() as c:
            row = c.execute(
                "SELECT * FROM requests WHERE id=%s AND tenant=%s FOR UPDATE", (request_id, tenant)
            ).fetchone()
            if not row:
                raise NotFound("REQUEST_NOT_FOUND")
            if row["delivery_state"] != "NEEDS_REVIEW" or row["decision"] in {"APPROVED", "DENIED"}:
                raise Conflict("RECOVERY_NOT_ALLOWED")
            op = c.execute(
                "SELECT * FROM operations WHERE request_id=%s AND state='DEAD' ORDER BY kind FOR UPDATE",
                (request_id,),
            ).fetchone()
            if not op:
                raise Conflict("NO_RECOVERABLE_OPERATION")
            # Recovery retains request id => SAME payer idempotency identity.
            result = c.execute(
                "UPDATE operations SET state='READY',attempts=0,generation=generation+1,due_at=clock_timestamp(),last_error=NULL WHERE id=%s RETURNING *",
                (op["id"],),
            ).fetchone()
            self._wake(c, result)
            self._change(c, row, "OPERATOR_RECOVERY", delivery_state="RETRYING", last_error=None)
            c.execute(
                "INSERT INTO audit(tenant,action,resource_id,actor_id) VALUES(%s,'request.recover',%s,%s)",
                (tenant, str(request_id), actor_id),
            )

    def claim(self, operation_id: UUID):
        with self.db.transaction() as c:
            # No external call while a DB transaction/row lock remains open.
            row = c.execute(
                "UPDATE operations SET state='RUNNING',attempts=attempts+1,lease_token=%s,lease_until=clock_timestamp()+(%s * interval '1 second') WHERE id=%s AND ((state='READY' AND due_at<=clock_timestamp()) OR (state='RUNNING' AND lease_until<=clock_timestamp())) RETURNING *",
                (uuid4(), self.settings.lease_seconds, operation_id),
            ).fetchone()
            if row:
                c.execute(
                    "INSERT INTO operation_events(operation_id,claim_token,kind,outcome) VALUES(%s,%s,%s,'CLAIMED')",
                    (row["id"], row["lease_token"], row["kind"]),
                )
            return row

    def owned(self, c, operation: dict):
        # Fresh DB clock AFTER obtaining lock; transaction-start time is unsafe.
        row = c.execute(
            "SELECT * FROM operations WHERE id=%s FOR UPDATE", (operation["id"],)
        ).fetchone()
        if row["state"] != "RUNNING" or row["lease_token"] != operation["lease_token"]:
            return None
        if not c.execute(
            "SELECT %s > clock_timestamp() AS valid", (row["lease_until"],)
        ).fetchone()["valid"]:
            return None
        return row

    def finish(self, c, op: dict, state="DONE", delay=0, error=None, reset_attempts=False):
        c.execute(
            "INSERT INTO operation_events(operation_id,claim_token,kind,outcome) VALUES(%s,%s,%s,%s)",
            (op["id"], op["lease_token"], op["kind"], error or state),
        )
        row = c.execute(
            "UPDATE operations SET state=%s,lease_token=NULL,lease_until=NULL,due_at=clock_timestamp()+(%s * interval '1 second'),generation=generation+1,last_error=%s,attempts=CASE WHEN %s THEN 0 ELSE attempts END WHERE id=%s RETURNING *",
            (state, delay, error, reset_attempts, op["id"]),
        ).fetchone()
        if state == "READY":
            self._wake(c, row)

    def sweep(self, limit=100):
        # Queue messages are replaceable hints. Reissue after successful publication
        # goes unclaimed, including lost/deleted/expired broker messages.
        with self.db.transaction() as c:
            rows = c.execute(
                "SELECT o.* FROM operations o WHERE ((o.state='READY' AND o.due_at<=clock_timestamp()) OR (o.state='RUNNING' AND o.lease_until<=clock_timestamp())) AND NOT EXISTS(SELECT 1 FROM outbox b WHERE b.operation_id=o.id AND (b.published_at IS NULL OR b.published_at>clock_timestamp()-interval '5 seconds')) ORDER BY o.due_at LIMIT %s FOR UPDATE SKIP LOCKED",
                (limit,),
            ).fetchall()
            for op in rows:
                current = c.execute(
                    "UPDATE operations SET generation=generation+1 WHERE id=%s RETURNING *",
                    (op["id"],),
                ).fetchone()
                self._wake(c, current)
        self.reconcile_inbox()

    def expire(self):
        with self.db.transaction() as c:
            rows = c.execute(
                "SELECT * FROM requests WHERE expires_at<=clock_timestamp() AND decision NOT IN ('APPROVED','DENIED') AND delivery_state NOT IN ('CANCELLED','EXPIRED','FAILED') ORDER BY expires_at LIMIT 100 FOR UPDATE SKIP LOCKED"
            ).fetchall()
            for row in rows:
                self._change(
                    c,
                    row,
                    "LOCAL_SLA_EXCEEDED",
                    delivery_state="EXPIRED",
                    last_error="SLA_EXCEEDED",
                )
                c.execute(
                    "UPDATE operations SET state='DEAD',lease_token=NULL,lease_until=NULL WHERE request_id=%s AND state NOT IN ('DONE','DEAD')",
                    (row["id"],),
                )
