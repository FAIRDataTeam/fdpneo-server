"""Storage-level backup / restore / migration (ADR-0016).

Admin-operated, adapter-level tooling (same posture as ``fdp pid``): it reads and
writes the triple store directly, never through the LDP layer, so provenance
(``dct:created``/``dct:modified``/creator/state), the record-schema binding
(``dct:conformsTo`` / ``fdp-o:validatedAgainst``, ADR-0019), and the per-record
audit graphs survive a round trip byte-for-byte.

* :func:`dump_store` — export every named graph as N-Quads + a versioned manifest.
* restore / import land in later tasks (18.3+).
"""

from __future__ import annotations

from fdp.metadata.backup.dump import (
    DUMP_FORMAT_VERSION,
    DumpResult,
    dump_store,
)
from fdp.metadata.backup.import_fdp import ImportReport, import_reference_fdp
from fdp.metadata.backup.restore import (
    RestoreError,
    RestoreResult,
    restore_audit,
    restore_store,
)

__all__ = [
    "DUMP_FORMAT_VERSION",
    "DumpResult",
    "ImportReport",
    "RestoreError",
    "RestoreResult",
    "dump_store",
    "import_reference_fdp",
    "restore_audit",
    "restore_store",
]
