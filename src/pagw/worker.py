import hashlib
import json
import random
import time
from uuid import UUID, uuid4

import httpx
from opentelemetry import trace
from opentelemetry.propagate import extract

from pagw.config import Settings
from pagw.connectors import HTTPConnector, PartnerError
from pagw.fhir import canonical
from pagw.http import retry_after
from pagw.queueing import Broker, Message
from pagw.security import TokenManager, cipher, signature
from pagw.store import Store
from pagw.telemetry import ATTEMPTS, RESULTS, event


class Worker:
    def __init__(self, store: Store, settings: Settings, broker: Broker):
        self.store, self.settings, self.broker = store, settings, broker
        tokens = TokenManager(settings)
        self.connectors = {
            payer: HTTPConnector(settings, payer, tokens) for payer in ("alpha", "beta")
        }

    def handle(self, operation_id: UUID):
        op = self.store.claim(operation_id)
        if not op:
            return False
        ATTEMPTS.labels(op["kind"]).inc()
        event(
            "operation.claimed",
            operation_id=op["id"],
            request_id=op.get("request_id"),
            kind=op["kind"],
        )
        try:
            if op["attempts"] > self.settings.max_attempts:
                raise RuntimeError("ATTEMPT_BUDGET_EXHAUSTED")
            if op["kind"] == "WEBHOOK":
                self.webhook(op)
            else:
                self.partner(op)
        except Exception:
            event(
                "operation.unexpected_failure", operation_id=op["id"], error_code="INTERNAL_ERROR"
            )
            with self.store.db.transaction() as c:
                row = None
                if op["request_id"]:
                    row = c.execute(
                        "SELECT * FROM requests WHERE id=%s FOR UPDATE", (op["request_id"],)
                    ).fetchone()
                if self.store.owned(c, op):
                    dead = op["attempts"] >= self.settings.max_attempts
                    if dead and row and row["decision"] not in {"APPROVED", "DENIED"}:
                        self.store._change(
                            c,
                            row,
                            "INTERNAL_ERROR",
                            delivery_state="NEEDS_REVIEW",
                            last_error="INTERNAL_ERROR",
                        )
                    if dead and op["delivery_id"]:
                        c.execute(
                            "UPDATE deliveries SET state='DEAD' WHERE id=%s", (op["delivery_id"],)
                        )
                    self.store.finish(
                        c,
                        op,
                        "DEAD" if dead else "READY",
                        delay=self.settings.retry_base_seconds,
                        error="INTERNAL_ERROR",
                    )
        return True

    def partner(self, op: dict):
        row = self.store.db.one("SELECT * FROM requests WHERE id=%s", (op["request_id"],))
        if not row:
            raise RuntimeError("operation-request-missing")
        result = None
        error = None
        skip = (
            row["decision"] in {"APPROVED", "DENIED"}
            or row["delivery_state"] in {"CANCELLED", "EXPIRED", "FAILED"}
            or (op["kind"] == "SUBMIT" and row["delivery_state"] == "SUBMITTED")
        )
        if not skip:
            try:
                connector = self.connectors[row["payer"]]
                result = connector.submit(row) if op["kind"] == "SUBMIT" else connector.poll(row)
            except PartnerError as exc:
                error = exc
        with self.store.db.transaction() as c:
            # Consistent lock order with cancellation, callbacks and expiry.
            current = c.execute(
                "SELECT * FROM requests WHERE id=%s FOR UPDATE", (row["id"],)
            ).fetchone()
            owned = self.store.owned(c, op)
            if not owned:
                RESULTS.labels(op["kind"], "stale_owner").inc()
                return
            terminal = current["decision"] in {"APPROVED", "DENIED"}
            stopped = current["delivery_state"] in {
                "CANCELLED",
                "EXPIRED",
                "FAILED",
            }
            if result:
                state = (
                    "IGNORED"
                    if stopped
                    else self.store._apply_result(c, current, result, observe_only=terminal)
                )
                self.store.record_http_observation(c, current, op, result, state)
                if terminal or stopped:
                    self.store.finish(
                        c, op, error="PAYER_CONFLICT" if state == "CONFLICT" else None
                    )
                elif state == "CONFLICT":
                    self.store._change(
                        c,
                        current,
                        "PAYER_CONFLICT",
                        delivery_state="NEEDS_REVIEW",
                        last_error="PAYER_CONFLICT",
                    )
                    self.store.finish(c, op, "DEAD", error="PAYER_CONFLICT")
                elif op["kind"] == "POLL" and result.decision == "PENDED":
                    self.store.finish(
                        c, op, "READY", delay=self.settings.poll_seconds, reset_attempts=True
                    )
                else:
                    self.store.finish(c, op)
            elif terminal or stopped or skip:
                self.store.finish(c, op, error=error.code if error else None)
            elif error:
                exhausted = op["attempts"] >= self.settings.max_attempts
                if not error.retryable or exhausted:
                    # After a possible external effect, stop and reconcile; never
                    # turn absence of a response into a clinical rejection.
                    state = (
                        "FAILED"
                        if not error.retryable
                        and not error.ambiguous
                        and op["attempts"] == 1
                        and op["kind"] == "SUBMIT"
                        else "NEEDS_REVIEW"
                    )
                    self.store._change(
                        c, current, error.code, delivery_state=state, last_error=error.code
                    )
                    self.store.finish(c, op, "DEAD", error=error.code)
                else:
                    delay = max(
                        error.retry_after,
                        random.uniform(
                            0.1,
                            min(
                                900,
                                self.settings.retry_base_seconds * 2 ** min(op["attempts"] - 1, 10),
                            ),
                        ),
                    )
                    if op["kind"] == "SUBMIT" and current["delivery_state"] != "SUBMITTED":
                        self.store._change(
                            c, current, error.code, delivery_state="RETRYING", last_error=error.code
                        )
                    self.store.finish(c, op, "READY", delay=delay, error=error.code)
        RESULTS.labels(op["kind"], error.code if error else "ok").inc()

    def webhook(self, op: dict):
        row = self.store.db.one(
            "SELECT d.*,s.url,s.secret,s.enabled FROM deliveries d JOIN subscriptions s ON s.id=d.subscription_id WHERE d.id=%s",
            (op["delivery_id"],),
        )
        if not row:
            raise RuntimeError("operation-delivery-missing")
        status = 0
        requested_delay = 0.0
        success = row["state"] == "DELIVERED"
        if not success and row["enabled"]:
            if row["url"] != self.settings.receiver_url:
                raise RuntimeError("unconfigured-webhook-destination")
            body = canonical(row["payload"])
            secret = cipher(self.settings).decrypt(bytes(row["secret"])).decode()
            timestamp = int(time.time())
            try:
                with httpx.Client(
                    timeout=self.settings.http_timeout_seconds,
                    follow_redirects=False,
                    trust_env=False,
                ) as client:
                    with client.stream(
                        "POST",
                        row["url"],
                        content=body,
                        headers={
                            "Content-Type": "application/json",
                            "X-PA-Timestamp": str(timestamp),
                            "X-PA-Signature": "v1=" + signature(secret, body, timestamp),
                            "X-PA-Event-Id": str(row["event_id"]),
                        },
                    ) as response:
                        status = response.status_code
                        success = 200 <= status < 300
                        if status == 429:
                            requested_delay = retry_after(response.headers.get("Retry-After"))
            except httpx.HTTPError:
                status = 0
        with self.store.db.transaction() as c:
            if not self.store.owned(c, op):
                return
            if success:
                c.execute(
                    "UPDATE deliveries SET state='DELIVERED',delivered_at=clock_timestamp() WHERE id=%s",
                    (row["id"],),
                )
                self.store.finish(c, op)
            elif (
                not row["enabled"]
                or op["attempts"] >= self.settings.max_attempts
                or (400 <= status < 500 and status not in {408, 429})
            ):
                c.execute("UPDATE deliveries SET state='DEAD' WHERE id=%s", (row["id"],))
                self.store.finish(c, op, "DEAD", error="WEBHOOK_DELIVERY_FAILED")
            else:
                self.store.finish(
                    c,
                    op,
                    "READY",
                    delay=max(
                        requested_delay,
                        min(900, self.settings.retry_base_seconds * 2 ** min(op["attempts"], 10)),
                    ),
                    error="WEBHOOK_UNAVAILABLE",
                )

    def consume(self, message: Message):
        try:
            if len(message.body) > 4096:
                raise ValueError("oversized")
            body = json.loads(message.body)
            operation_id = UUID(body["operation_id"])
        except (ValueError, KeyError, TypeError):
            with self.store.db.transaction() as c:
                c.execute(
                    "INSERT INTO quarantine(id,queue,body_hash,reason) VALUES(%s,%s,%s,'MALFORMED_MESSAGE')",
                    (uuid4(), message.queue, hashlib.sha256(message.body.encode()).hexdigest()),
                )
            self.broker.ack(message)
            return
        context = extract({"traceparent": body.get("traceparent") or ""})
        with trace.get_tracer("pagw.worker").start_as_current_span(
            "operation.process", context=context
        ):
            self.handle(operation_id)
        # Durable result/schedule has committed before acknowledging the hint.
        self.broker.ack(message)

    def once(self, queues=("submit", "webhook")):
        count = 0
        for queue in queues:
            for message in self.broker.receive(queue):
                self.consume(message)
                count += 1
        return count
