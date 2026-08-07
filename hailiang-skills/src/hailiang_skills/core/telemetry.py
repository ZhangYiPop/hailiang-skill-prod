"""Low-cardinality metrics and request-scoped tracing helpers.

The module intentionally works without an OTLP collector in local development.
When OpenTelemetry/Prometheus are installed it exports native spans/metrics;
otherwise it still preserves correlation data in application events.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
from time import perf_counter
from typing import Any, Iterator
from uuid import uuid4
import json
import os
from pathlib import Path
from threading import Lock
from hailiang_skills.core.deployment import deployment_environment, log_root, node_name, release_version

try:  # pragma: no cover - optional runtime integration
    from opentelemetry import trace
except ImportError:  # pragma: no cover
    trace = None


def configure_otel() -> None:
    """Configure OTLP export once when an endpoint is supplied.

    Collector wiring is opt-in so running the API locally does not require a
    collector.  The standard W3C traceparent propagator is used by the SDK.
    """
    if not trace or not __import__("os").getenv("OTEL_EXPORTER_OTLP_ENDPOINT"):
        return
    try:  # pragma: no cover - integration depends on installed collector
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        provider = TracerProvider(resource=Resource.create({"service.name": "hailiang-skills"}))
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        trace.set_tracer_provider(provider)
        global _tracer
        _tracer = trace.get_tracer("hailiang-skills")
    except Exception:
        # Telemetry export cannot make the counseling API unavailable.
        return

try:  # pragma: no cover - optional runtime integration
    from prometheus_client import Counter, Gauge, Histogram, generate_latest
except ImportError:  # pragma: no cover
    Counter = Gauge = Histogram = None
    generate_latest = None


@dataclass(frozen=True, slots=True)
class TelemetryContext:
    request_id: str
    trace_id: str = ""
    span_id: str = ""
    session_id: str = ""
    run_id: str = ""
    profile_id: str = ""
    user_id: str = ""
    route: str = ""
    started_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


_telemetry_context: ContextVar[TelemetryContext | None] = ContextVar("hailiang_telemetry", default=None)
_tracer = trace.get_tracer("hailiang-skills") if trace else None
_telemetry_file_lock = Lock()

if Histogram:
    REQUEST_DURATION = Histogram(
        "hailiang_request_duration_seconds",
        "End-to-end API request duration.",
        ("route", "method", "status_code"),
    )
    NODE_DURATION = Histogram(
        "hailiang_node_duration_seconds",
        "Execution node duration.",
        ("node", "outcome", "skill_id"),
    )
    SSE_ACTIVE = Gauge("hailiang_sse_active_connections", "Current active SSE streams.")
    SSE_TTFT = Histogram("hailiang_sse_time_to_first_token_seconds", "Time to first response token.", ("skill_id",))
    ERRORS = Counter("hailiang_errors_total", "Application errors.", ("node", "error_type"))
else:  # pragma: no cover
    REQUEST_DURATION = NODE_DURATION = SSE_ACTIVE = SSE_TTFT = ERRORS = None


def current_telemetry() -> TelemetryContext | None:
    return _telemetry_context.get()


def bind_telemetry(**values: str) -> tuple[TelemetryContext, Any]:
    previous = current_telemetry()
    base = asdict(previous) if previous else {}
    base.update({key: str(value or "") for key, value in values.items()})
    base.setdefault("request_id", f"req_{uuid4().hex[:16]}")
    base["trace_id"] = base.get("trace_id") or uuid4().hex
    base["span_id"] = base.get("span_id") or uuid4().hex[:16]
    context = TelemetryContext(**base)
    return context, _telemetry_context.set(context)


def reset_telemetry(token: Any) -> None:
    _telemetry_context.reset(token)


def _write_local_span(record: dict[str, Any]) -> None:
    """Write a body-free span record for local request debugging."""
    if os.getenv("HAILIANG_TELEMETRY_FILE_ENABLED", "true").lower() not in {"1", "true", "yes", "on"}:
        return
    trace_id = str(record.get("trace_id") or "unknown")
    root = log_root() / "telemetry"
    try:
        root.mkdir(parents=True, exist_ok=True)
        with _telemetry_file_lock:
            with (root / f"{trace_id}.{os.getpid()}.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        # Observability must never break the user request.
        return


def enrich_payload(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    enriched = dict(payload or {})
    context = current_telemetry()
    if context:
        enriched.setdefault("telemetry", {
            "request_id": context.request_id,
            "trace_id": context.trace_id,
            "span_id": context.span_id,
            "session_id": context.session_id,
            "run_id": context.run_id,
            "profile_id": context.profile_id,
            "user_id": context.user_id,
            "route": context.route,
        })
    return enriched


@contextmanager
def span(
    name: str,
    *,
    node: str | None = None,
    skill_id: str = "",
    attributes: dict[str, Any] | None = None,
) -> Iterator[dict[str, Any]]:
    """Measure a node with UTC wall timestamps and a monotonic duration."""
    span_node_name = node or name
    started_at = datetime.now(timezone.utc).isoformat()
    started = perf_counter()
    outcome = "ok"
    active_span = _tracer.start_as_current_span(name) if _tracer else None
    span_cm = active_span if active_span else _NullSpan()
    metadata: dict[str, Any] = {
        "node": span_node_name,
        "start_at": started_at,
        "skill_id": skill_id,
        **(attributes or {}),
    }
    try:
        with span_cm as otel_span:
            if attributes and hasattr(otel_span, "set_attributes"):
                otel_span.set_attributes({key: str(value) for key, value in attributes.items()})
            yield metadata
    except Exception as exc:
        outcome = "error"
        metadata["error_type"] = type(exc).__name__
        if ERRORS:
            ERRORS.labels(node=span_node_name, error_type=type(exc).__name__).inc()
        raise
    finally:
        duration_s = perf_counter() - started
        metadata["end_at"] = datetime.now(timezone.utc).isoformat()
        metadata["duration_ms"] = round(duration_s * 1000, 3)
        metadata["outcome"] = outcome
        context = current_telemetry()
        _write_local_span({
            "record_type": "span",
            "request_id": context.request_id if context else "",
            "trace_id": context.trace_id if context else "",
            "span_id": context.span_id if context else "",
            "session_id": context.session_id if context else "",
            "run_id": context.run_id if context else "",
            "profile_id": context.profile_id if context else "",
            "route": context.route if context else "",
            "environment": deployment_environment(),
            "version": release_version(),
            "node": node_name(),
            **metadata,
        })
        if NODE_DURATION:
            NODE_DURATION.labels(node=span_node_name, outcome=outcome, skill_id=skill_id or "none").observe(duration_s)


def text_fingerprint(value: str | None, *, preview_chars: int = 160) -> dict[str, Any]:
    text = str(value or "")
    return {
        "sha256": sha256(text.encode("utf-8")).hexdigest(),
        "length": len(text),
        "preview": text[:preview_chars] + ("…" if len(text) > preview_chars else ""),
    }


def redact_log_payload(value: Any, *, key: str = "") -> Any:
    """Remove body text from ordinary events while retaining diagnosability."""
    sensitive_tokens = ("prompt", "input", "content", "message", "response", "body", "text", "query")
    if isinstance(value, str) and any(token in key.lower() for token in sensitive_tokens):
        return text_fingerprint(value, preview_chars=0)
    if isinstance(value, dict):
        return {str(item_key): redact_log_payload(item, key=str(item_key)) for item_key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_log_payload(item, key=key) for item in value]
    return value


def prometheus_payload() -> bytes | None:
    return generate_latest() if generate_latest else None


class _NullSpan:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False
