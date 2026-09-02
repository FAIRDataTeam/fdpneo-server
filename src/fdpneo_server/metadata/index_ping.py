"""Outbound Index ping (Phase 8.1 / ADR-0020/0021).

Announces this FDP to one or more FDP **Index** instances so they can harvest its
metadata and keep it discoverable (e.g. ``https://home.fairdatapoint.org``). The
intake/harvest side is a separate product (FAIR Discovery); this is the outbound
side only.

Wire protocol (verified against the reference implementation's
``IndexPingController``): ``POST {index}/`` with body ``{"clientUrl": "<our base>"}``
and ``Content-Type: application/json``; a ``204 No Content`` means accepted, ``429``
means rate-limited (the index throttles per IP and per URL and expects pings at
least weekly). Nothing but the client URL is sent.

The :class:`IndexPinger` runs an in-process loop (first ping at startup = the
deployment announce, then every ``ping_interval_seconds``) and, when
``ping_on_publish``, also pings on record changes — throttled by
``ping_min_interval_seconds`` so a burst of writes cannot trip the index's rate
limit. It mirrors the start/stop shape of the metrics scheduler + audit log.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx
import structlog

from fdpneo_server.metadata.events import (
    RecordCreated,
    RecordDeleted,
    RecordModified,
    RecordStateChanged,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from fdpneo_server.config import IndexSettings
    from fdpneo_server.shared.events import Event, EventBus, Subscription

log = structlog.get_logger(__name__)

_ACCEPTED = frozenset({200, 204})


@dataclass(frozen=True)
class PingResult:
    """The outcome of one ping to one index."""

    target: str
    status: int | None  # HTTP status, or None when the request never completed
    ok: bool
    detail: str | None = None


async def ping_indexes(
    http_client: httpx.AsyncClient,
    *,
    client_url: str,
    targets: Sequence[str],
    timeout_seconds: float = 10.0,
) -> list[PingResult]:
    """POST ``{"clientUrl": client_url}`` to each index; one result per target.

    Per-target errors are captured, never raised, so one unreachable index does
    not stop the others. ``204``/``200`` is success; anything else (incl. ``429``
    rate-limited) is recorded as not-ok with the status/detail.
    """
    payload = {"clientUrl": client_url}
    results: list[PingResult] = []
    for target in targets:
        url = target.rstrip("/") + "/"  # reference receives at the index root
        try:
            response = await http_client.post(url, json=payload, timeout=timeout_seconds)
        except httpx.HTTPError as err:
            results.append(PingResult(target=target, status=None, ok=False, detail=repr(err)))
            continue
        ok = response.status_code in _ACCEPTED
        detail = None if ok else f"HTTP {response.status_code}"
        results.append(PingResult(target=target, status=response.status_code, ok=ok, detail=detail))
    return results


class IndexPinger:
    """Announces this FDP to configured indexes: a startup + periodic + on-change loop."""

    __slots__ = (
        "_client_url",
        "_http",
        "_last",
        "_lock",
        "_on_results",
        "_provider",
        "_settings",
        "_subs",
        "_task",
    )

    def __init__(
        self,
        *,
        settings: IndexSettings,
        client_url: str,
        http_client: httpx.AsyncClient,
        targets_provider: Callable[[], Awaitable[Sequence[str]]] | None = None,
        on_results: Callable[[list[PingResult]], Awaitable[None]] | None = None,
    ) -> None:
        self._settings = settings
        self._client_url = settings.ping_client_url.strip() or client_url.rstrip("/")
        self._http = http_client
        # ADR-0025: when a provider is injected, targets are runtime data
        # (env union admin-registered rows) resolved fresh on every ping — targets
        # added through the admin API take effect with no restart. Without one
        # (CLI, tests), env settings drive everything as before.
        self._provider = targets_provider
        self._on_results = on_results
        self._task: asyncio.Task[None] | None = None
        self._subs: list[Subscription] = []
        self._lock = asyncio.Lock()
        # Monotonic time of the last ping, or None before the first one. The
        # sentinel matters: a 0.0 baseline reads as "pinged at boot" on a
        # freshly started machine (small monotonic clock), throttling the
        # very first on-change ping — visible on CI runners.
        self._last: float | None = None

    def start(self, bus: EventBus) -> None:
        """Launch the periodic loop and subscribe to changes.

        With a ``targets_provider`` the loop + subscriptions ALWAYS start, even
        when the deployment boots with zero targets: the provider is re-read on
        every ping, so the first admin-added target starts announcing without a
        restart (an empty scheduled iteration is a free no-op). Env-only
        construction keeps the old disabled-when-empty behavior.
        """
        if self._provider is None and not self._settings.enabled:
            return
        if self._settings.ping_in_process and self._task is None:
            self._task = asyncio.create_task(self._loop(), name="index-ping")
        if self._settings.ping_on_publish and not self._subs:
            for event_type in (RecordCreated, RecordModified, RecordDeleted, RecordStateChanged):
                self._subs.append(bus.subscribe(event_type, self._on_change))
        log.info(
            "index_pinger_started",
            targets=self._settings.targets,
            dynamic_targets=self._provider is not None,
            client_url=self._client_url,
            in_process=self._settings.ping_in_process,
            on_publish=self._settings.ping_on_publish,
        )

    async def stop(self) -> None:
        """Cancel the loop and drop subscriptions. Idempotent."""
        for sub in self._subs:
            sub.unsubscribe()
        self._subs.clear()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def ping_now(self, reason: str = "manual") -> list[PingResult]:
        """Ping every effective index once; log the outcome. Serialized by a lock."""
        async with self._lock:
            targets: Sequence[str] = (
                await self._provider() if self._provider is not None else self._settings.targets
            )
            if not targets:
                # Deliberately does NOT arm the throttle: otherwise a scheduled
                # empty no-op would suppress the first on-change ping right
                # after an admin adds the deployment's first target.
                return []
            self._last = time.monotonic()
            results = await ping_indexes(
                self._http,
                client_url=self._client_url,
                targets=targets,
                timeout_seconds=self._settings.ping_timeout_seconds,
            )
        if self._on_results is not None:
            try:
                await self._on_results(results)
            except Exception:
                log.warning("index_ping_status_record_failed", reason=reason)
        for result in results:
            if result.ok:
                log.info("index_ping_ok", target=result.target, reason=reason)
            else:
                log.warning(
                    "index_ping_failed", target=result.target, detail=result.detail, reason=reason
                )
        return results

    async def _loop(self) -> None:
        interval = max(60, self._settings.ping_interval_seconds)
        while True:
            await self.ping_now("scheduled")
            await asyncio.sleep(interval)

    async def _on_change(self, event: Event) -> None:
        del event
        if (
            self._last is not None
            and time.monotonic() - self._last < self._settings.ping_min_interval_seconds
        ):
            return  # throttled: a recent ping already covers this change
        await self.ping_now("on-change")


__all__ = ["IndexPinger", "PingResult", "ping_indexes"]
