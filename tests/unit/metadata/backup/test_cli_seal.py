"""CLI tests for ``fdp backup seal|unseal|verify`` and passphrase handling (ADR-0029 §5)."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from fdpneo_server import cli
from fdpneo_server.metadata.backup import seal as seal_module
from fdpneo_server.metadata.backup.seal import KdfParams

pytestmark = pytest.mark.unit

PASS = "cli test passphrase"


@pytest.fixture(autouse=True)
def _cheap_kdf(monkeypatch: pytest.MonkeyPatch) -> None:
    # The CLI uses the default (expensive) scrypt cost; tests only check plumbing.
    original = seal_module.seal_file

    def fast_seal(src: Path, dest: Path, passphrase: str, **_: object) -> None:
        original(src, dest, passphrase, kdf=KdfParams(log2_n=4))

    monkeypatch.setattr(seal_module, "seal_file", fast_seal)


def test_seal_unseal_round_trip_with_passphrase_file(tmp_path: Path) -> None:
    runner = CliRunner()
    secret = tmp_path / "pass.txt"
    secret.write_text(PASS + "\n")
    plain = tmp_path / "plain.txt"
    plain.write_text("credentials")
    sealed = tmp_path / "plain.fdpbk"
    out = tmp_path / "out.txt"

    result = runner.invoke(
        cli.app, ["backup", "seal", str(plain), str(sealed), "--passphrase-file", str(secret)]
    )
    assert result.exit_code == 0, result.output
    assert sealed.read_bytes().startswith(b"FDPNEO-ENC1")

    result = runner.invoke(
        cli.app,
        ["backup", "unseal", str(sealed), str(out), "--passphrase-file", "-"],
        input=PASS + "\n",
    )
    assert result.exit_code == 0, result.output
    assert out.read_text() == "credentials"


def test_unseal_with_wrong_passphrase_fails(tmp_path: Path) -> None:
    runner = CliRunner()
    plain = tmp_path / "p"
    plain.write_text("x")
    sealed = tmp_path / "p.fdpbk"
    runner.invoke(
        cli.app,
        ["backup", "seal", str(plain), str(sealed), "--passphrase-file", "-"],
        input=PASS + "\n",
    )
    result = runner.invoke(
        cli.app,
        ["backup", "unseal", str(sealed), str(tmp_path / "o"), "--passphrase-file", "-"],
        input="definitely the wrong one\n",
    )
    assert result.exit_code == 1
    assert "wrong passphrase" in result.output
    assert not (tmp_path / "o").exists()


def test_short_passphrase_is_refused(tmp_path: Path) -> None:
    plain = tmp_path / "p"
    plain.write_text("x")
    result = CliRunner().invoke(
        cli.app,
        ["backup", "seal", str(plain), str(tmp_path / "s"), "--passphrase-file", "-"],
        input="short\n",
    )
    assert result.exit_code == 1
    assert "at least" in result.output


def test_full_dump_options_require_full(tmp_path: Path) -> None:
    result = CliRunner().invoke(cli.app, ["backup", "dump", str(tmp_path / "d"), "--no-encrypt"])
    assert result.exit_code == 1
    assert "only apply with --full" in result.output
