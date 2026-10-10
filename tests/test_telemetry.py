import json
from uuid import uuid4

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from test_durability import Connector, approval

from pagw.fhir import fixture
from pagw.queueing import Message
from pagw.worker import Worker


def test_trace_context_survives_outbox_and_lost_wakeup(store, settings, broker, monkeypatch):
    provider, exporter = TracerProvider(), InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(trace, "get_tracer", provider.get_tracer)
    with provider.get_tracer("test").start_as_current_span("admit") as span:
        trace_id = span.get_span_context().trace_id
        _, response, _ = store.submit("tenant-a", "traced", fixture())
    op = store.db.one("SELECT * FROM operations WHERE request_id=%s", (response["id"],))
    with store.db.transaction() as c:
        c.execute("UPDATE outbox SET published_at=clock_timestamp()-interval '10 seconds'")
    store.sweep()
    saved = store.db.one("SELECT traceparent FROM outbox ORDER BY created_at DESC LIMIT 1")[
        "traceparent"
    ]
    assert format(trace_id, "032x") in saved
    worker = Worker(store, settings, broker)
    worker.connectors["alpha"] = Connector(lambda row: approval(row["id"]))
    worker.consume(
        Message(
            json.dumps({"operation_id": str(op["id"]), "traceparent": saved}),
            str(uuid4()),
            "submit",
        )
    )
    spans = exporter.get_finished_spans()
    assert {s.context.trace_id for s in spans} == {trace_id}
    assert {s.name for s in spans} == {"admit", "operation.process"}
    provider.shutdown()
