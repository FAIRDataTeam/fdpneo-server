"""Offline verification of backup archives (ADR-0029 §5, ``fdp backup verify``).

Checks an archive without restoring it: the passphrase of a sealed file (every
chunk must authenticate), the dump format, and every checksum and row count in
the manifest. Understands four shapes:

* a dump directory or ``.zip`` (ADR-0016 / ADR-0029 application archive);
* a sealed application archive (``fdp backup dump --full``);
* a sealed stack bundle (``deploy/stack/backup.sh``): a tar holding
  ``bundle.json``, the application archive ``app.zip``, the Keycloak export and
  the deployment configuration. Its application archive is verified in turn.
"""

from __future__ import annotations

import hashlib
import json
import tarfile
import tempfile
import zipfile
from pathlib import Path

from fdpneo_server.metadata.backup.dump import AUDIT_FILE, RECORDS_FILE, SCOPE_FULL
from fdpneo_server.metadata.backup.restore import RestoreError, read_manifest
from fdpneo_server.metadata.backup.seal import is_sealed, unseal_file

BUNDLE_MANIFEST = "bundle.json"
BUNDLE_APP_ARCHIVE = "app.zip"


class VerifyError(Exception):
    """The archive is unreadable, incomplete, or does not match its manifest."""


def verify_archive(archive: Path, *, passphrase: str | None = None) -> list[str]:
    """Verify ``archive``; return human-readable report lines or raise :class:`VerifyError`."""
    with tempfile.TemporaryDirectory(prefix="fdp-verify-") as tmp:
        work = Path(tmp)
        source = archive
        report: list[str] = []
        if archive.is_file() and is_sealed(archive):
            if passphrase is None:
                raise VerifyError("the archive is encrypted: a passphrase is required")
            source = work / "unsealed"
            unseal_file(archive, source, passphrase)
            report.append("passphrase accepted; every encrypted chunk authenticated")
        if source.is_dir():
            return report + _verify_app_dir(source)
        # Tar first: a tar that *contains* app.zip also passes is_zipfile, because
        # the zip's end-of-central-directory record sits near the tar's end.
        if tarfile.is_tarfile(source):
            return report + _verify_bundle(source, work / "bundle")
        if zipfile.is_zipfile(source):
            return report + _verify_app_dir(_unzip(source, work / "app"))
        raise VerifyError("not a backup archive (expected a directory, .zip, tar or sealed file)")


def _unzip(source: Path, dest: Path) -> Path:
    with zipfile.ZipFile(source) as zf:
        bad = zf.testzip()
        if bad is not None:
            raise VerifyError(f"corrupted member in zip archive: {bad}")
        zf.extractall(dest)  # noqa: S202 - zipfile drops absolute paths and ".." parts
    return dest


def _verify_bundle(source: Path, dest: Path) -> list[str]:
    dest.mkdir(parents=True)
    with tarfile.open(source) as tf:
        tf.extractall(dest, filter="data")
    root = dest
    if not (root / BUNDLE_MANIFEST).is_file():
        children = [p for p in root.iterdir() if p.is_dir()]
        if len(children) == 1 and (children[0] / BUNDLE_MANIFEST).is_file():
            root = children[0]
        else:
            raise VerifyError(f"stack bundle without {BUNDLE_MANIFEST}")
    bundle = json.loads((root / BUNDLE_MANIFEST).read_text(encoding="utf-8"))
    report = [
        f"stack bundle format {bundle.get('bundle_format')} of project "
        f"{bundle.get('compose_project')!r}, taken {bundle.get('created_at')}",
    ]
    images = bundle.get("images") or {}
    if isinstance(images, dict):
        report.extend(f"image {name}: {ref}" for name, ref in sorted(images.items()))  # pyright: ignore[reportUnknownArgumentType, reportUnknownVariableType]
    app = root / BUNDLE_APP_ARCHIVE
    if not app.is_file():
        raise VerifyError(f"stack bundle without {BUNDLE_APP_ARCHIVE}")
    report.extend(_verify_app_dir(_unzip(app, dest.parent / "bundle-app")))
    keycloak = sorted(p.name for p in (root / "keycloak").glob("*.json"))
    report.append(
        f"keycloak export: {', '.join(keycloak)}" if keycloak else "keycloak export: none"
    )
    config = sorted(p.name for p in (root / "config").glob("*") if p.is_file())
    report.append(f"configuration: {', '.join(config) if config else 'none'}")
    return report


def _verify_app_dir(src: Path) -> list[str]:
    try:
        manifest = read_manifest(src)
    except RestoreError as err:
        raise VerifyError(str(err)) from err
    files: dict[str, str] = manifest.get("files") or {}
    for name in (RECORDS_FILE, AUDIT_FILE):
        expected = files.get(name)
        if expected is None:
            continue
        _check_sha(src / name, expected, name)
    report = [
        f"dump format {manifest.get('dump_format_version')}, scope "
        f"{manifest.get('scope', 'store')}, server {manifest.get('application_version')}, "
        f"identifier base {manifest.get('identifier_base')}",
        f"{manifest.get('graph_count')} graph(s), {manifest.get('quad_count')} quad(s): "
        "checksum OK",
    ]
    if manifest.get("scope") == SCOPE_FULL:
        tables: dict[str, dict[str, object]] = manifest.get("postgres") or {}
        for name, entry in sorted(tables.items()):
            path = src / str(entry.get("file"))
            _check_sha(path, str(entry.get("sha256")), name)
            rows = sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
            if rows != int(str(entry.get("rows"))):
                raise VerifyError(
                    f"{name}: manifest says {entry.get('rows')} rows, file has {rows}"
                )
        report.append(
            f"Postgres schema revision {manifest.get('alembic_revision')}: "
            + ", ".join(f"{name}={entry.get('rows')}" for name, entry in sorted(tables.items()))
            + " (checksums OK)"
        )
    return report


def _check_sha(path: Path, expected: str, name: str) -> None:
    if not path.is_file():
        raise VerifyError(f"{name} is missing from the archive")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    if digest.hexdigest() != expected:
        raise VerifyError(f"checksum mismatch for {name}")


__all__ = ["BUNDLE_APP_ARCHIVE", "BUNDLE_MANIFEST", "VerifyError", "verify_archive"]
