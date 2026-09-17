import json
import logging
import sys

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from prometheus_client import CollectorRegistry, Counter, Histogram, generate_latest
from prometheus_client.core import GaugeMetricFamily

from pagw.config import Settings

ATTEMPTS = Counter("pagw_operation_attempts_total", "Durable operation claims", ["kind"])
RESULTS = Counter("pagw_operation_results_total", "Operation results", ["kind", "outcome"])
LATENCY = Histogram("pagw_partner_seconds", "Partner HTTP latency", ["payer"])
LOGGER = logging.getLogger("pagw")


def configure(settings: Settings):
    logging.basicConfig(level=settings.log_level, stream=sys.stdout, format="%(message)s")
    # Partner URLs and exception strings may contain sensitive values.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    if settings.otlp_endpoint and not isinstance(trace.get_tracer_provider(), TracerProvider):
        provider = TracerProvider(resource=Resource.create({"service.name": "prior-auth-gateway"}))
        provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=settings.otlp_endpoint))
        )
        trace.set_tracer_provider(provider)


def durable_metrics(db, tenant: str) -> bytes:
    class Collector:
        def collect(self):
            operations = GaugeMetricFamily(
                "pagw_durable_operations",
                "Current durable operation states",
                labels=["kind", "state"],
            )
            with db.transaction() as c:
                rows = c.execute(
                    "SELECT o.kind,o.state,count(*) AS n FROM operations o LEFT JOIN requests r ON r.id=o.request_id LEFT JOIN deliveries d ON d.id=o.delivery_id LEFT JOIN requests dr ON dr.id=d.request_id WHERE coalesce(r.tenant,dr.tenant)=%s GROUP BY o.kind,o.state",
                    (tenant,),
                ).fetchall()
                for row in rows:
                    operations.add_metric([row["kind"], row["state"]], row["n"])
                requests = GaugeMetricFamily(
                    "pagw_authorizations",
                    "Current authorization states",
                    labels=["delivery", "decision"],
                )
                for row in c.execute(
                    "SELECT delivery_state,decision,count(*) AS n FROM requests WHERE tenant=%s GROUP BY delivery_state,decision",
                    (tenant,),
                ).fetchall():
                    requests.add_metric([row["delivery_state"], row["decision"]], row["n"])
                lag = c.execute(
                    "SELECT coalesce(max(extract(epoch FROM clock_timestamp()-o.due_at)),0) AS lag FROM operations o LEFT JOIN requests r ON r.id=o.request_id LEFT JOIN deliveries d ON d.id=o.delivery_id LEFT JOIN requests dr ON dr.id=d.request_id WHERE coalesce(r.tenant,dr.tenant)=%s AND o.state='READY' AND o.due_at<=clock_timestamp()",
                    (tenant,),
                ).fetchone()["lag"]
            yield operations
            yield requests
            yield GaugeMetricFamily(
                "pagw_due_operation_lag_seconds", "Oldest overdue ready operation", value=float(lag)
            )

    registry = CollectorRegistry()
    registry.register(Collector())
    return generate_latest(registry)


def event(name: str, **fields):
    # Only fixed metadata fields may reach logs; no caller bodies/error messages.
    allowed = {
        "request_id",
        "operation_id",
        "kind",
        "outcome",
        "payer",
        "version",
        "error_code",
        "trace_id",
    }
    context = trace.get_current_span().get_span_context()
    if context.is_valid:
        fields["trace_id"] = format(context.trace_id, "032x")
    LOGGER.info(
        json.dumps({"event": name, **{k: str(v) for k, v in fields.items() if k in allowed}})
    )
