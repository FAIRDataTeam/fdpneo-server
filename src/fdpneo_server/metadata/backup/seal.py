"""Passphrase encryption for backup archives (ADR-0029 §5).

A *sealed* file is any file (a full dump ``.zip``, a stack bundle) encrypted with
a passphrase so the credentials it carries — API-key hashes, Keycloak password
hashes, ``.env`` secrets — are protected at rest.

Format ``FDPNEO-ENC1`` (all integers big-endian)::

    magic        11 bytes  b"FDPNEO-ENC1"
    log2_n        1 byte   scrypt cost N = 2**log2_n
    r             1 byte   scrypt block size
    p             1 byte   scrypt parallelism
    salt         16 bytes  scrypt salt
    nonce_prefix  7 bytes  random per file
    chunk_size    4 bytes  plaintext bytes per chunk
    chunks...              AES-256-GCM(chunk) || 16-byte tag

Key derivation is scrypt over the UTF-8 passphrase. Each chunk is encrypted with
AES-256-GCM under the nonce ``nonce_prefix || counter (4 bytes) || last (1 byte)``
— the STREAM construction — with the 39-byte header as associated data. Chunks
cannot be reordered, dropped or truncated undetected: every chunk is bound to
its position, and only the final chunk carries ``last = 1`` (an empty file is a
single empty final chunk). Tampering with the KDF parameters fails
authentication too, because the header is the associated data.

Only the ``cryptography`` package (already a runtime dependency) is used.
Synchronous file I/O: called from the CLI or via ``asyncio.to_thread``.
"""

from __future__ import annotations

import os
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Final

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

MAGIC: Final = b"FDPNEO-ENC1"
_HEADER = struct.Struct(">11sBBB16s7sI")
HEADER_SIZE: Final = _HEADER.size  # 39
_TAG_SIZE: Final = 16
_KEY_SIZE: Final = 32

DEFAULT_LOG2_N: Final = 17  # N = 131072, r = 8 → 128 MiB, OWASP's scrypt baseline
DEFAULT_R: Final = 8
DEFAULT_P: Final = 1
DEFAULT_CHUNK_SIZE: Final = 1024 * 1024

# Refuse headers that would make key derivation a denial of service.
_MAX_LOG2_N: Final = 20
_MAX_R: Final = 16
_MAX_P: Final = 4
_MAX_CHUNK_SIZE: Final = 16 * 1024 * 1024

MIN_PASSPHRASE_LENGTH: Final = 12


class SealError(Exception):
    """A sealed file could not be opened: wrong passphrase, tampering, or not sealed."""


@dataclass(frozen=True)
class KdfParams:
    """scrypt cost parameters; tests use a cheap ``log2_n`` to stay fast."""

    log2_n: int = DEFAULT_LOG2_N
    r: int = DEFAULT_R
    p: int = DEFAULT_P


def is_sealed(path: Path | str) -> bool:
    """True when ``path`` is a file starting with the ``FDPNEO-ENC1`` magic."""
    p = Path(path)
    if not p.is_file():
        return False
    with p.open("rb") as handle:
        return handle.read(len(MAGIC)) == MAGIC


def validate_passphrase(passphrase: str) -> None:
    """Reject passphrases too short to protect credentials."""
    if len(passphrase) < MIN_PASSPHRASE_LENGTH:
        raise SealError(f"passphrase must be at least {MIN_PASSPHRASE_LENGTH} characters long")


def seal_file(
    src: Path | str,
    dest: Path | str,
    passphrase: str,
    *,
    kdf: KdfParams | None = None,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> None:
    """Encrypt ``src`` into ``dest`` (``dest`` is written atomically)."""
    validate_passphrase(passphrase)
    params = kdf or KdfParams()
    salt = os.urandom(16)
    nonce_prefix = os.urandom(7)
    header = _HEADER.pack(MAGIC, params.log2_n, params.r, params.p, salt, nonce_prefix, chunk_size)
    aead = AESGCM(_derive_key(passphrase, salt, params))
    out = Path(dest)
    tmp = out.with_name(out.name + ".partial")
    with Path(src).open("rb") as reader, tmp.open("wb") as writer:
        writer.write(header)
        _encrypt_stream(reader, writer, aead, nonce_prefix, header, chunk_size)
    tmp.replace(out)


def unseal_file(src: Path | str, dest: Path | str, passphrase: str) -> None:
    """Decrypt ``src`` into ``dest``; raise :class:`SealError` on any failure.

    The plaintext is written to a temporary sibling and only moved into place
    once every chunk authenticated, so a wrong passphrase or a tampered file
    never leaves partial plaintext behind.
    """
    out = Path(dest)
    tmp = out.with_name(out.name + ".partial")
    try:
        with Path(src).open("rb") as reader, tmp.open("wb") as writer:
            header = reader.read(HEADER_SIZE)
            nonce_prefix, chunk_size, aead = _open_header(header, passphrase)
            _decrypt_stream(reader, writer, aead, nonce_prefix, header, chunk_size)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    tmp.replace(out)


def _open_header(header: bytes, passphrase: str) -> tuple[bytes, int, AESGCM]:
    if len(header) != HEADER_SIZE or not header.startswith(MAGIC):
        raise SealError("not a sealed FDPneo archive (missing FDPNEO-ENC1 header)")
    _, log2_n, r, p, salt, nonce_prefix, chunk_size = _HEADER.unpack(header)
    if not (
        1 <= log2_n <= _MAX_LOG2_N
        and 1 <= r <= _MAX_R
        and 1 <= p <= _MAX_P
        and 1 <= chunk_size <= _MAX_CHUNK_SIZE
    ):
        raise SealError("sealed archive header has out-of-range parameters")
    key = _derive_key(passphrase, salt, KdfParams(log2_n=log2_n, r=r, p=p))
    return nonce_prefix, chunk_size, AESGCM(key)


def _derive_key(passphrase: str, salt: bytes, params: KdfParams) -> bytes:
    kdf = Scrypt(salt=salt, length=_KEY_SIZE, n=2**params.log2_n, r=params.r, p=params.p)
    return kdf.derive(passphrase.encode("utf-8"))


def _nonce(prefix: bytes, counter: int, last: bool) -> bytes:
    return prefix + struct.pack(">IB", counter, 1 if last else 0)


def _encrypt_stream(
    reader: BinaryIO,
    writer: BinaryIO,
    aead: AESGCM,
    nonce_prefix: bytes,
    header: bytes,
    chunk_size: int,
) -> None:
    counter = 0
    current = reader.read(chunk_size)
    while True:
        following = reader.read(chunk_size)
        last = not following
        writer.write(aead.encrypt(_nonce(nonce_prefix, counter, last), current, header))
        if last:
            return
        counter += 1
        if counter >= 2**32:
            raise SealError("file too large for one sealed archive")
        current = following


def _decrypt_stream(
    reader: BinaryIO,
    writer: BinaryIO,
    aead: AESGCM,
    nonce_prefix: bytes,
    header: bytes,
    chunk_size: int,
) -> None:
    block = chunk_size + _TAG_SIZE
    counter = 0
    current = reader.read(block)
    if not current:
        raise SealError("sealed archive is truncated (no data)")
    while True:
        following = reader.read(block)
        last = not following
        try:
            plaintext = aead.decrypt(_nonce(nonce_prefix, counter, last), current, header)
        except InvalidTag as err:
            if counter == 0:
                raise SealError("wrong passphrase, or the archive is corrupted") from err
            raise SealError("the archive is corrupted or truncated") from err
        writer.write(plaintext)
        if last:
            return
        counter += 1
        current = following


__all__ = [
    "DEFAULT_CHUNK_SIZE",
    "HEADER_SIZE",
    "MAGIC",
    "MIN_PASSPHRASE_LENGTH",
    "KdfParams",
    "SealError",
    "is_sealed",
    "seal_file",
    "unseal_file",
    "validate_passphrase",
]
