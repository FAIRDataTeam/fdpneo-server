"""Unit tests for the FDPNEO-ENC1 passphrase encryption (ADR-0029 §5)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from fdpneo_server.metadata.backup.seal import (
    HEADER_SIZE,
    MAGIC,
    KdfParams,
    SealError,
    is_sealed,
    seal_file,
    unseal_file,
    validate_passphrase,
)

pytestmark = pytest.mark.unit

PASS = "correct horse battery staple"
FAST = KdfParams(log2_n=4, r=8, p=1)  # cheap KDF: the format, not scrypt, is under test


def _seal(tmp_path: Path, payload: bytes, *, chunk_size: int = 64) -> Path:
    src = tmp_path / "plain.bin"
    src.write_bytes(payload)
    sealed = tmp_path / "sealed.fdpbk"
    seal_file(src, sealed, PASS, kdf=FAST, chunk_size=chunk_size)
    return sealed


@pytest.mark.parametrize("size", [0, 1, 63, 64, 65, 640, 1000])
def test_round_trip_any_size(tmp_path: Path, size: int) -> None:
    payload = os.urandom(size)
    sealed = _seal(tmp_path, payload)
    out = tmp_path / "out.bin"
    unseal_file(sealed, out, PASS)
    assert out.read_bytes() == payload


def test_sealed_file_starts_with_magic_and_hides_plaintext(tmp_path: Path) -> None:
    payload = b"api_keys key_hash secret-material " * 20
    sealed = _seal(tmp_path, payload)
    data = sealed.read_bytes()
    assert data.startswith(MAGIC)
    assert b"secret-material" not in data
    assert is_sealed(sealed)
    assert not is_sealed(tmp_path / "plain.bin")
    assert not is_sealed(tmp_path)


def test_same_input_seals_differently(tmp_path: Path) -> None:
    a = _seal(tmp_path, b"x" * 100).read_bytes()
    b = _seal(tmp_path, b"x" * 100).read_bytes()
    assert a != b  # fresh salt and nonce prefix per file


def test_wrong_passphrase_fails_and_writes_nothing(tmp_path: Path) -> None:
    sealed = _seal(tmp_path, b"hello" * 50)
    out = tmp_path / "out.bin"
    with pytest.raises(SealError, match="wrong passphrase"):
        unseal_file(sealed, out, "not the passphrase at all")
    assert not out.exists()
    assert not (tmp_path / "out.bin.partial").exists()


def test_flipped_byte_is_detected(tmp_path: Path) -> None:
    sealed = _seal(tmp_path, os.urandom(500))
    data = bytearray(sealed.read_bytes())
    data[HEADER_SIZE + 200] ^= 0x01
    sealed.write_bytes(bytes(data))
    with pytest.raises(SealError):
        unseal_file(sealed, tmp_path / "out.bin", PASS)


def test_truncation_at_a_chunk_boundary_is_detected(tmp_path: Path) -> None:
    sealed = _seal(tmp_path, os.urandom(64 * 4), chunk_size=64)
    data = sealed.read_bytes()
    # Drop the final chunk entirely: the previous chunk was not sealed as "last".
    sealed.write_bytes(data[: -(64 + 16)])
    with pytest.raises(SealError, match="corrupted or truncated"):
        unseal_file(sealed, tmp_path / "out.bin", PASS)


def test_tampered_kdf_parameters_fail_authentication(tmp_path: Path) -> None:
    sealed = _seal(tmp_path, b"payload" * 10)
    data = bytearray(sealed.read_bytes())
    data[len(MAGIC)] = 5  # log2_n 4 → 5: a different key and different AAD
    sealed.write_bytes(bytes(data))
    with pytest.raises(SealError):
        unseal_file(sealed, tmp_path / "out.bin", PASS)


def test_out_of_range_parameters_are_refused_before_kdf(tmp_path: Path) -> None:
    sealed = _seal(tmp_path, b"payload")
    data = bytearray(sealed.read_bytes())
    data[len(MAGIC)] = 40  # N = 2**40 would be a denial of service
    sealed.write_bytes(bytes(data))
    with pytest.raises(SealError, match="out-of-range"):
        unseal_file(sealed, tmp_path / "out.bin", PASS)


def test_not_a_sealed_file(tmp_path: Path) -> None:
    plain = tmp_path / "plain.zip"
    plain.write_bytes(b"PK\x03\x04 not sealed")
    with pytest.raises(SealError, match="not a sealed"):
        unseal_file(plain, tmp_path / "out.bin", PASS)


def test_short_passphrase_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(SealError, match="at least"):
        validate_passphrase("short")
    src = tmp_path / "p"
    src.write_bytes(b"x")
    with pytest.raises(SealError):
        seal_file(src, tmp_path / "s", "short", kdf=FAST)
