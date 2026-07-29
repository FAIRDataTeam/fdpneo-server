"""Daily-rotated salt for unique-visitor hashing.

The salt is held only in memory. It is never written to disk, never
serialized, and never logged. Each FDP server process has its own salt;
restarting the server forces a fresh salt and effectively starts a new
counting window.

Architecture §11.2 + ADR-0002: the salt rotates every 24 h so the same
visitor's daily hash changes across days, blocking longitudinal
tracking. We use :func:`time.monotonic` to count elapsed time so a
wall-clock adjustment (e.g. NTP step, daylight-saving) cannot extend or
shorten a counting window.
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Callable

_ROTATION_INTERVAL_SECONDS: float = 24 * 60 * 60


class SaltRotator:
    """Holds an in-memory salt, rotating after every full 24 h interval.

    The ``clock`` argument is injectable for tests; it must return a
    monotonically non-decreasing float, like :func:`time.monotonic`.
    """

    __slots__ = ("_clock", "_last_rotation", "_salt")

    def __init__(self, *, clock: Callable[[], float] | None = None) -> None:
        self._clock = clock or time.monotonic
        self._salt = secrets.token_bytes(16)
        self._last_rotation = self._clock()

    def current_salt(self) -> bytes:
        """Return the salt, rotating first if 24 h has elapsed.

        Callers should not hold the returned salt past one call: rotation
        is lazy and only triggered here.
        """
        if self._clock() - self._last_rotation >= _ROTATION_INTERVAL_SECONDS:
            self._salt = secrets.token_bytes(16)
            self._last_rotation = self._clock()
        return self._salt


__all__ = ["SaltRotator"]
