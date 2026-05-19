"""Structured logging configuration.

**Responsibilities**

* Configure ``structlog`` once at process start: console renderer in
  development, JSON renderer for staging and production.
* Inject the active :class:`RequestContext` (subject, trace id, anonymous
  flag) into every log line via a processor that reads the ContextVar.
* Bridge the stdlib ``logging`` module so that third-party logs (Uvicorn,
  SQLAlchemy, etc.) flow through the same handler with the same format.

**Non-responsibilities**

* OpenTelemetry tracing. Configured separately at the OTLP exporter level;
  this module just carries the trace id through.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import MutableMapping
from typing import Any, Literal

import structlog
from structlog.processors import CallsiteParameter

from fdp.shared.context import get_current

Environment = Literal["development", "staging", "production"]

_UVICORN_LOGGERS = ("uvicorn", "uvicorn.access", "uvicorn.error")


def _request_context_processor(
    _logger: object,
    _method_name: str,
    event_dict: MutableMapping[str, Any],
) -> MutableMapping[str, Any]:
    """Merge the active RequestContext fields into every log line."""
    ctx = get_current()
    if ctx is not None:
        event_dict.setdefault("trace_id", ctx.trace_id)
        event_dict.setdefault("subject", ctx.subject)
        event_dict.setdefault("is_anonymous", ctx.is_anonymous)
    return event_dict


def configure_logging(env: Environment) -> None:
    """Configure structlog and the root stdlib logger for ``env``.

    Safe to call multiple times — each invocation rebuilds the processor
    chain and re-attaches the root handler. Tests rely on this idempotence.
    """
    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        _request_context_processor,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.CallsiteParameterAdder(
            parameters={
                CallsiteParameter.FILENAME,
                CallsiteParameter.FUNC_NAME,
                CallsiteParameter.LINENO,
            }
        ),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    renderer: structlog.types.Processor = (
        structlog.dev.ConsoleRenderer(colors=True)
        if env == "development"
        else structlog.processors.JSONRenderer()
    )

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.DEBUG if env == "development" else logging.INFO)

    for name in _UVICORN_LOGGERS:
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers = []
        uvicorn_logger.propagate = True


__all__ = ["Environment", "configure_logging"]
