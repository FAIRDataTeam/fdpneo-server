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

from fdp.metadata.backup.admin_router import JobView, build_backup_admin_router
from fdp.metadata.backup.dump import (
    DUMP_FORMAT_VERSION,
    DumpResult,
    dump_store,
)
from fdp.metadata.backup.import_fdp import ImportReport, import_reference_fdp
from fdp.metadata.backup.jobs import BackupJob, BackupJobRegistry, JobState
from fdp.metadata.backup.orchestrate import (
    RestoreOutcome,
    dump_to_archive,
    extract_archive,
    orchestrate_restore,
)
from fdp.metadata.backup.restore import (
    RestoreError,
    RestoreResult,
    restore_audit,
    restore_store,
)

__all__ = [
    "DUMP_FORMAT_VERSION",
    "BackupJob",
    "BackupJobRegistry",
    "DumpResult",
    "ImportReport",
    "JobState",
    "JobView",
    "RestoreError",
    "RestoreOutcome",
    "RestoreResult",
    "build_backup_admin_router",
    "dump_store",
    "dump_to_archive",
    "extract_archive",
    "import_reference_fdp",
    "orchestrate_restore",
    "restore_audit",
    "restore_store",
]
