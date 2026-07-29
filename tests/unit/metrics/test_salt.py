"""Unit tests for :mod:`fdpneo_server.metrics.salt`."""

from __future__ import annotations

import pytest

from fdpneo_server.metrics.salt import SaltRotator


class _FakeMonotonic:
    """Injectable monotonic-clock stub."""

    def __init__(self, start: float = 0.0) -> None:
        self._now = start

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


@pytest.mark.unit
def test_initial_salt_is_sixteen_bytes() -> None:
    rotator = SaltRotator(clock=_FakeMonotonic())
    salt = rotator.current_salt()
    assert isinstance(salt, bytes)
    assert len(salt) == 16


@pytest.mark.unit
def test_salt_is_stable_within_24h_window() -> None:
    clock = _FakeMonotonic()
    rotator = SaltRotator(clock=clock)
    first = rotator.current_salt()
    clock.advance(seconds=23 * 60 * 60 + 59 * 60)  # 23h59m
    assert rotator.current_salt() == first


@pytest.mark.unit
def test_salt_rotates_exactly_after_24h() -> None:
    clock = _FakeMonotonic()
    rotator = SaltRotator(clock=clock)
    first = rotator.current_salt()
    clock.advance(seconds=24 * 60 * 60)
    second = rotator.current_salt()
    assert second != first
    assert len(second) == 16


@pytest.mark.unit
def test_salt_rotates_again_on_next_window() -> None:
    clock = _FakeMonotonic()
    rotator = SaltRotator(clock=clock)
    seen: set[bytes] = {rotator.current_salt()}
    for _ in range(3):
        clock.advance(seconds=24 * 60 * 60)
        seen.add(rotator.current_salt())
    # Four distinct salts (initial + three rotations).
    assert len(seen) == 4


@pytest.mark.unit
def test_two_rotators_have_independent_salts() -> None:
    a = SaltRotator(clock=_FakeMonotonic())
    b = SaltRotator(clock=_FakeMonotonic())
    assert a.current_salt() != b.current_salt()
