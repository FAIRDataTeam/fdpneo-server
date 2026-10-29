#!/usr/bin/env python3
"""Validate a release.json (ADR-0028 §2) against the version being released.

Usage: check_release_json.py <release.json> <version> <component>

Stdlib only, so the release workflow can run it before installing anything.
Exits non-zero with a GitHub Actions ``::error::`` line on any mismatch.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

_VERSION = re.compile(r"^\d+\.\d+\.\d+$")
_REQUIREMENT = re.compile(r"^(>=|==)?\d+\.\d+\.\d+$")


def problems(data: object, version: str, component: str) -> list[str]:
    if not isinstance(data, dict):
        return ["release.json must be a JSON object"]
    out: list[str] = []
    if data.get("schema") != 1:
        out.append("schema must be 1")
    if data.get("component") != component:
        out.append(f"component must be {component!r}")
    if data.get("version") != version:
        out.append(f"version {data.get('version')!r} does not match {version!r}")
    minimum = data.get("min_upgrade_from")
    if not isinstance(minimum, str) or not _VERSION.match(minimum):
        out.append("min_upgrade_from must be an X.Y.Z version")
    for flag in ("manual_steps", "security"):
        if not isinstance(data.get(flag), bool):
            out.append(f"{flag} must be true or false")
    requires = data.get("requires", {})
    if not isinstance(requires, dict):
        out.append("requires must be an object")
    else:
        server = requires.get("server")
        if server is not None and (not isinstance(server, str) or not _REQUIREMENT.match(server)):
            out.append("requires.server must look like '>=X.Y.Z'")
    notes = data.get("upgrade_notes")
    if notes is not None and (not isinstance(notes, str) or not notes.startswith("https://")):
        out.append("upgrade_notes must be an https URL")
    return out


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        print(__doc__, file=sys.stderr)
        return 2
    path, version, component = Path(argv[1]), argv[2].removeprefix("v"), argv[3]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"::error::cannot read {path}: {exc}")
        return 1
    found = problems(data, version, component)
    for problem in found:
        print(f"::error file={path}::{problem}")
    if not found:
        print(f"{path}: OK for {component} {version}")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
