"""Tests for ``fdp.shared.logging``."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterator
from datetime import UTC, datetime
from io import StringIO

import pytest
import structlog

from fdp.shared.context import RequestContext, bound
from fdp.shared.logging import configure_logging

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


@pytest.fixture
def capture_root_logs() -> Iterator[StringIO]:
    """Replace the root handler's stream with a StringIO buffer."""
    configure_logging("production")
    root = logging.getLogger()
    handler = root.handlers[0]
    assert isinstance(handler, logging.StreamHandler)
    buffer = StringIO()
    original = handler.stream  # pyright: ignore[reportUnknownVariableType, reportUnknownMemberType]
    handler.stream = buffer
    try:
        yield buffer
    finally:
        handler.stream = original


@pytest.mark.unit
def test_production_log_lines_are_json(capture_root_logs: StringIO) -> None:
    log = structlog.get_logger("fdp.test")
    log.info("hello", extra_field="x")

    line = capture_root_logs.getvalue().strip().splitlines()[-1]
    payload = json.loads(line)
    assert payload["event"] == "hello"
    assert payload["extra_field"] == "x"
    assert payload["level"] == "info"


@pytest.mark.unit
def test_request_context_is_merged_into_log_line(capture_root_logs: StringIO) -> None:
    raw_subject = "https://idp.example/realms/fdp#alice"
    ctx = RequestContext(
        subject=raw_subject,
        roles=frozenset({"steward"}),
        request_timestamp=datetime.now(UTC),
        trace_id="trace-xyz",
    )
    log = structlog.get_logger("fdp.test")
    with bound(ctx):
        log.info("hi")
    line = capture_root_logs.getvalue().strip().splitlines()[-1]
    payload = json.loads(line)
    assert payload["trace_id"] == "trace-xyz"
    assert payload["is_anonymous"] is False
    # The raw OIDC subject (PII) is never emitted — it is pseudonymized
    # (audit F-09 / R-10).
    assert payload["subject"] != raw_subject
    assert payload["subject"].startswith("subj_")


@pytest.mark.unit
def test_explicit_subject_kwarg_is_pseudonymized(capture_root_logs: StringIO) -> None:
    raw_subject = "https://idp.example/realms/fdp#bob"
    log = structlog.get_logger("fdp.test")
    # A call site that passes the subject explicitly (the F-09 offenders) is
    # pseudonymized at the same chokepoint, no per-site change required.
    log.info("api_key_minted", subject=raw_subject, owner_subject=raw_subject)
    payload = json.loads(capture_root_logs.getvalue().strip().splitlines()[-1])
    assert payload["subject"] != raw_subject
    assert payload["subject"].startswith("subj_")
    # ``owner_subject`` is covered too, and identical subjects map to the same
    # pseudonym (stable within a process).
    assert payload["owner_subject"] == payload["subject"]


@pytest.mark.unit
def test_configured_salt_makes_pseudonym_stable() -> None:
    from fdp.shared.logging import pseudonymize_subject

    subject = "https://idp.example/realms/fdp#carol"
    first = pseudonymize_subject(subject, "salt-A")
    assert first == pseudonymize_subject(subject, "salt-A")  # deterministic
    assert first != pseudonymize_subject(subject, "salt-B")  # salt-dependent
    assert first != subject  # not the raw identifier


@pytest.mark.unit
def test_log_outside_bound_has_no_context_fields(capture_root_logs: StringIO) -> None:
    log = structlog.get_logger("fdp.test")
    log.info("naked")
    line = capture_root_logs.getvalue().strip().splitlines()[-1]
    payload = json.loads(line)
    assert "trace_id" not in payload
    assert "subject" not in payload
    assert "is_anonymous" not in payload


@pytest.mark.unit
def test_development_renderer_is_not_json() -> None:
    configure_logging("development")
    root = logging.getLogger()
    handler = root.handlers[0]
    assert isinstance(handler, logging.StreamHandler)
    buffer = StringIO()
    original = handler.stream  # pyright: ignore[reportUnknownVariableType, reportUnknownMemberType]
    handler.stream = buffer
    try:
        structlog.get_logger("fdp.test").info("dev-event")
    finally:
        handler.stream = original
    rendered = buffer.getvalue()
    plain = _ANSI_RE.sub("", rendered).strip()
    assert plain, "dev renderer produced no output"
    with pytest.raises(json.JSONDecodeError):
        json.loads(plain.splitlines()[-1])


@pytest.mark.unit
def test_uvicorn_logger_flows_through_same_handler(capture_root_logs: StringIO) -> None:
    logging.getLogger("uvicorn.access").info("served")
    line = capture_root_logs.getvalue().strip().splitlines()[-1]
    payload = json.loads(line)
    assert payload["event"] == "served"
