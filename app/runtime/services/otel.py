"""OpenTelemetry bootstrap -- configure Azure Monitor export."""

from __future__ import annotations

import logging
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

logger = logging.getLogger(__name__)

_otel_active = False

# Azure SDK loggers that flood the console at INFO level.
_NOISY_LOGGERS = (
    "azure.core.pipeline.policies.http_logging_policy",
    "azure.monitor.opentelemetry.exporter.export._base",
    "azure.identity",
)


def _quiet_noisy_loggers() -> None:
    """Suppress verbose Azure SDK loggers to WARNING."""
    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


def _reset_otel_state() -> None:
    """Reset module-level OTel state -- for test isolation only."""
    global _otel_active
    _otel_active = False


from ..util.singletons import register_singleton  # noqa: E402

register_singleton(_reset_otel_state)


def configure_otel(
    connection_string: str,
    *,
    sampling_ratio: float = 1.0,
    enable_live_metrics: bool = False,
) -> bool:
    """Initialise the Azure Monitor OpenTelemetry distro.

    Returns ``True`` if initialisation succeeded, ``False`` otherwise.
    This is deliberately defensive: monitoring is optional and must never
    prevent the application from starting.
    """
    global _otel_active

    if _otel_active:
        logger.info("[otel.configure] OTel already active, skipping re-init")
        return True

    if not connection_string:
        logger.info("[otel.configure] No connection string provided, skipping")
        return False

    try:
        from azure.monitor.opentelemetry import configure_azure_monitor

        configure_azure_monitor(
            connection_string=connection_string,
            sampling_ratio=sampling_ratio,
            enable_live_metrics=enable_live_metrics,
        )
        _otel_active = True

        _quiet_noisy_loggers()

        logger.info(
            "[otel.configure] Azure Monitor OpenTelemetry configured "
            "(sampling=%.2f, live_metrics=%s)",
            sampling_ratio,
            enable_live_metrics,
        )
        return True
    except ImportError:
        logger.warning(
            "[otel.configure] azure-monitor-opentelemetry is not installed. "
            "Run: pip install azure-monitor-opentelemetry"
        )
        return False
    except Exception:
        logger.error("[otel.configure] Failed to configure OTel", exc_info=True)
        return False


def shutdown_otel() -> None:
    """Gracefully flush and shut down OTel providers."""
    global _otel_active

    if not _otel_active:
        return

    try:
        from opentelemetry import trace, metrics
        from opentelemetry._logs import get_logger_provider

        tp = trace.get_tracer_provider()
        if hasattr(tp, "shutdown"):
            tp.shutdown()

        mp = metrics.get_meter_provider()
        if hasattr(mp, "shutdown"):
            mp.shutdown()

        lp = get_logger_provider()
        if hasattr(lp, "shutdown"):
            lp.shutdown()

        _otel_active = False
        logger.info("[otel.shutdown] OpenTelemetry providers shut down")
    except Exception:
        logger.warning("[otel.shutdown] Error during OTel shutdown", exc_info=True)


def is_active() -> bool:
    """Return whether OTel is currently configured and active."""
    return _otel_active


def get_status() -> dict[str, object]:
    """Return a status dict for the monitoring API."""
    status: dict[str, object] = {"active": _otel_active}
    if _otel_active:
        try:
            from opentelemetry import trace
            tp = trace.get_tracer_provider()
            status["tracer_provider"] = type(tp).__name__
        except Exception:
            pass
    return status


# ---------------------------------------------------------------------------
# Custom span helpers -- instrument agent-specific operations
# ---------------------------------------------------------------------------

_TRACER_NAME = "polyclaw"


@contextmanager
def agent_span(
    name: str,
    *,
    attributes: dict[str, Any] | None = None,
) -> Generator[Any, None, None]:
    """Create a custom OTel span for an agent operation.

    Usage::

        with agent_span("chat.turn", attributes={"user": user_id}):
            ...  # the operation is timed and traced

    When OTel is not active the context manager is a no-op and yields
    ``None``.
    """
    if not _otel_active:
        yield None
        return

    try:
        from opentelemetry import trace

        tracer = trace.get_tracer(_TRACER_NAME)
        with tracer.start_as_current_span(name, attributes=attributes) as span:
            yield span
    except Exception:
        logger.debug("[otel.agent_span] Failed to create span %s", name, exc_info=True)
        yield None


@contextmanager
def invoke_agent_span(
    agent_name: str,
    *,
    model: str = "",
) -> Generator[Any, None, None]:
    """Create an ``invoke_agent`` dependency span for the Grafana dashboard.

    The span uses ``SpanKind.CLIENT`` so it lands in the ``dependencies``
    table in Application Insights.  The dashboard queries filter on
    ``name contains 'invoke_agent'`` and reads attributes:

    * ``gen_ai.agent.name`` -- agent display name
    * ``gen_ai.usage.input_tokens`` -- input token count (set after completion)
    * ``gen_ai.usage.output_tokens`` -- output token count (set after completion)
    * ``error.type`` -- error class (set on failure)

    Callers should set token attributes on the yielded span after getting
    a response::

        with invoke_agent_span("polyclaw", model="gpt-4.1") as span:
            response = await agent.send(prompt)
            if span:
                span.set_attribute("gen_ai.usage.input_tokens", 100)
                span.set_attribute("gen_ai.usage.output_tokens", 50)
    """
    if not _otel_active:
        yield None
        return

    try:
        from opentelemetry import trace
        from opentelemetry.trace import SpanKind, StatusCode

        tracer = trace.get_tracer(_TRACER_NAME)
        attrs: dict[str, Any] = {"gen_ai.agent.name": agent_name}
        if model:
            attrs["gen_ai.request.model"] = model
        with tracer.start_as_current_span(
            "invoke_agent",
            kind=SpanKind.CLIENT,
            attributes=attrs,
        ) as span:
            try:
                yield span
            except Exception as exc:
                if span.is_recording():
                    span.set_attribute("error.type", type(exc).__name__)
                    span.set_status(StatusCode.ERROR, str(exc)[:200])
                raise
    except Exception:
        logger.debug(
            "[otel.invoke_agent_span] Failed to create span for %s",
            agent_name,
            exc_info=True,
        )
        yield None


def record_event(name: str, attributes: dict[str, Any] | None = None) -> None:
    """Record an event on the current active span (if any)."""
    if not _otel_active:
        return
    try:
        from opentelemetry import trace

        span = trace.get_current_span()
        if span and span.is_recording():
            span.add_event(name, attributes=attributes)
    except Exception:
        pass


def set_span_attribute(key: str, value: Any) -> None:
    """Set an attribute on the current active span (if any)."""
    if not _otel_active:
        return
    try:
        from opentelemetry import trace

        span = trace.get_current_span()
        if span and span.is_recording():
            span.set_attribute(key, value)
    except Exception:
        pass
