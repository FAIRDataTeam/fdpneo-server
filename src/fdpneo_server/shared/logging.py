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

import hashlib
import logging
import secrets
import sys
from collections.abc import MutableMapping
from typing import Any, Literal

import structlog
from structlog.processors import CallsiteParameter

from fdpneo_server.shared.context import get_current

Environment = Literal["development", "staging", "production"]

_UVICORN_LOGGERS = ("uvicorn", "uvicorn.access", "uvicorn.error")

# Log fields that carry the OIDC subject (an identifier — PII under GDPR). Every
# such field is pseudonymized before a line is emitted (security audit
# 2026-06-07 F-09 / R-10). The *identified* trail lives in the audit log and the
# audit named graph, which do not flow through structlog, so this loses nothing
# for forensics while keeping raw identifiers out of application logs.
_SUBJECT_FIELDS = ("subject", "owner_subject")

# Fallback salt when none is configured: a fresh value per process. Subjects are
# still pseudonymized (fail-safe — a raw subject is never emitted), but the
# pseudonyms aren't stable across restarts. Set ``FDP_LOG_SUBJECT_SALT`` for a
# stable pseudonym that survives restarts (useful for incident correlation).
_DEFAULT_SUBJECT_SALT = secrets.token_hex(16)


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


def pseudonymize_subject(subject: str, salt: str) -> str:
    """Return a stable, salted pseudonym for an OIDC ``subject``.

    Same ``subject`` + ``salt`` always yields the same token, so a principal's
    actions stay correlatable across log lines, but the raw identifier is not
    recoverable from the logs without the salt. Not reversible.
    """
    digest = hashlib.sha256(f"{salt}\x00{subject}".encode()).hexdigest()
    return f"subj_{digest[:16]}"


def _make_subject_pseudonymizer(
    salt: str,
) -> structlog.types.Processor:
    """Build the processor that replaces subject identifiers with pseudonyms.

    Runs after :func:`_request_context_processor` (which injects the active
    subject) so it catches both the auto-injected subject and any subject passed
    explicitly at a call site — one chokepoint, no per-call-site discipline
    required.
    """

    def processor(
        _logger: object,
        _method_name: str,
        event_dict: MutableMapping[str, Any],
    ) -> MutableMapping[str, Any]:
        for field in _SUBJECT_FIELDS:
            value = event_dict.get(field)
            if isinstance(value, str) and value:
                event_dict[field] = pseudonymize_subject(value, salt)
        return event_dict

    return processor


def configure_logging(env: Environment, *, subject_salt: str | None = None) -> None:
    """Configure structlog and the root stdlib logger for ``env``.

    Safe to call multiple times — each invocation rebuilds the processor
    chain and re-attaches the root handler. Tests rely on this idempotence.

    ``subject_salt`` salts the subject pseudonymization (F-09 / R-10). When
    omitted, a per-process random salt is used, so subjects are always
    pseudonymized but the pseudonyms aren't stable across restarts.
    """
    salt = subject_salt or _DEFAULT_SUBJECT_SALT
    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        _request_context_processor,
        _make_subject_pseudonymizer(salt),
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


__all__ = ["Environment", "configure_logging", "pseudonymize_subject"]
