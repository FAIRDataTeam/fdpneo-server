"""Unit tests for offline archive verification (``fdp backup verify``, ADR-0029)."""

from __future__ import annotations

import hashlib
import json
import shutil
import tarfile
from pathlib import Path

import pytest

from fdpneo_server.metadata.backup.seal import KdfParams, SealError, seal_file
from fdpneo_server.metadata.backup.verify import VerifyError, verify_archive

pytestmark = pytest.mark.unit

PASS = "a perfectly fine passphrase"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _full_dump_dir(root: Path) -> Path:
    root.mkdir(parents=True)
    records = b'<urn:s> <urn:p> "o" <urn:g> .\n'
    (root / "records.nq").write_bytes(records)
    (root / "postgres").mkdir()
    settings = b'{"key": "access.control", "value_json": {"mode": "open"}}\n'
    (root / "postgres" / "runtime_settings.jsonl").write_bytes(settings)
    manifest = {
        "dump_format_version": "2",
        "scope": "full",
        "identifier_base": "http://localhost:8000",
        "application_version": "0.19.0",
        "alembic_revision": "0010_index_targets",
        "graph_count": 1,
        "quad_count": 1,
        "files": {"records.nq": _sha(records)},
        "postgres": {
            "runtime_settings": {
                "file": "postgres/runtime_settings.jsonl",
                "rows": 1,
                "sha256": _sha(settings),
            }
        },
    }
    (root / "manifest.json").write_text(json.dumps(manifest))
    return root


def test_verifies_a_full_dump_directory(tmp_path: Path) -> None:
    report = verify_archive(_full_dump_dir(tmp_path / "dump"))
    assert any("runtime_settings=1" in line for line in report)


def test_detects_a_tampered_state_table(tmp_path: Path) -> None:
    dump = _full_dump_dir(tmp_path / "dump")
    (dump / "postgres" / "runtime_settings.jsonl").write_text('{"key": "evil"}\n')
    with pytest.raises(VerifyError, match="checksum mismatch"):
        verify_archive(dump)


def test_rejects_a_future_dump_format(tmp_path: Path) -> None:
    dump = _full_dump_dir(tmp_path / "dump")
    manifest = json.loads((dump / "manifest.json").read_text())
    manifest["dump_format_version"] = "99"
    (dump / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(VerifyError, match="unsupported dump format"):
        verify_archive(dump)


def test_verifies_a_sealed_zip(tmp_path: Path) -> None:
    dump = _full_dump_dir(tmp_path / "dump")
    archive = Path(shutil.make_archive(str(tmp_path / "backup"), "zip", root_dir=dump))
    sealed = tmp_path / "backup.fdpbk"
    seal_file(archive, sealed, PASS, kdf=KdfParams(log2_n=4))
    report = verify_archive(sealed, passphrase=PASS)
    assert report[0].startswith("passphrase accepted")
    with pytest.raises(SealError):
        verify_archive(sealed, passphrase="the wrong passphrase!")
    with pytest.raises(VerifyError, match="passphrase is required"):
        verify_archive(sealed)


def test_verifies_a_sealed_stack_bundle(tmp_path: Path) -> None:
    dump = _full_dump_dir(tmp_path / "dump")
    staging = tmp_path / "staging" / "fdpneo-backup"
    (staging / "keycloak").mkdir(parents=True)
    (staging / "config").mkdir()
    shutil.make_archive(str(staging / "app"), "zip", root_dir=dump)
    (staging / "keycloak" / "fdp-realm.json").write_text("{}")
    (staging / "config" / ".env").write_text("PUBLIC_HOST=fdp.example.org\n")
    (staging / "bundle.json").write_text(
        json.dumps(
            {
                "bundle_format": 1,
                "compose_project": "fdpneo-prod",
                "created_at": "2026-10-29T12:00:00Z",
                "images": {"server": "ghcr.io/fairdatateam/fdpneo-server:0.19.0"},
            }
        )
    )
    tar_path = tmp_path / "bundle.tar"
    with tarfile.open(tar_path, "w") as tf:
        tf.add(staging, arcname="fdpneo-backup")
    sealed = tmp_path / "bundle.fdpbk"
    seal_file(tar_path, sealed, PASS, kdf=KdfParams(log2_n=4))
    report = verify_archive(sealed, passphrase=PASS)
    joined = "\n".join(report)
    assert "fdpneo-prod" in joined
    assert "image server: ghcr.io/fairdatateam/fdpneo-server:0.19.0" in joined
    assert "runtime_settings=1" in joined
    assert "keycloak export: fdp-realm.json" in joined
    assert "configuration: .env" in joined
