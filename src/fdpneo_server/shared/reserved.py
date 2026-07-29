"""The single reserved first path segment for all fixed FDP endpoints.

Every fixed, FDP-specific endpoint (operational probes, ``/config``, SPARQL,
search, the user/policy/license/schema admin surfaces, the LDP read-extension
and lifecycle verbs, …) and every server-owned record namespace lives under
this one reserved segment. The root namespace is therefore free for
user-defined resource types: a resource definition may claim any URL prefix
except :data:`RESERVED_API_PREFIX`.

``main.py`` mounts every fixed router under :data:`RESERVED_API_PATH`;
``shared.graphs`` mints the server-owned record IRIs under it; and the
resource-definition validator rejects it as a user URL prefix. Keep this the
single source of truth so the routing, the minted IRIs, and the guard never
drift apart.
"""

from __future__ import annotations

#: The reserved first path segment (no leading slash) — e.g. for URI minting.
RESERVED_API_PREFIX = "fdp-api"

#: The reserved segment as an absolute path prefix — e.g. for router mounting.
RESERVED_API_PATH = f"/{RESERVED_API_PREFIX}"

__all__ = ["RESERVED_API_PATH", "RESERVED_API_PREFIX"]
