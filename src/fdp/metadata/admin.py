"""Factory-reset admin surface (Phase 10.4, mirrors the RI's
``ResetService`` / ``FactoryDefaults``).

One destructive, admin-only endpoint:

* ``POST /admin/reset`` — truncate the Postgres ``runtime_settings`` table
  and **force re-apply** the bundled deployment profile, restoring the
  schemas, ODRL Offers, resource definitions and seed records to exactly
  what the profile bundle ships. After the re-apply the runtime caches that
  live on ``app.state`` (offer-resolver fallback, resource-definition cache,
  OpenAPI, SHACL warm-up, anonymous-authz warm-up) are republished, so the
  reset takes effect without a restart.

**Scope.** "Factory defaults" here means the deployment is reset to exactly
what the profile bundle ships. It matches ``fdp profile apply --force`` (CLI):
both now **wipe the triple store** with a portable SPARQL 1.1 ``DROP ALL``
(``TripleStoreAdapter.clear_all``) before re-applying, so graphs from a
previous profile — or operator-created records — do not linger and collide
with the re-applied bundle. Runtime resource-definition and schema edits
(ADR-0009 / 10.x) are therefore reverted along with everything else.

**Authorization.** Like the runtime-settings and resource-definition admin
surfaces, this is *deployment configuration*, not a metadata record, so it
requires the ``admin`` role rather than going through the ODRL PDP.

**Confirmation.** Because the operation is destructive and irreversible, the
request body must carry a fixed confirmation token
(:data:`RESET_CONFIRMATION_TOKEN`); a missing or wrong token is rejected with
``400`` before anything is touched.

**Audit.** The reset is logged via structlog (``admin_reset_completed`` with
the acting subject and the re-applied profile), consistent with the
runtime-settings audit approach (a persistent settings-audit table is
deferred — see :mod:`fdp.metadata.settings`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Final

import structlog
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from fdp.identity.deps import require_auth
from fdp.metadata.profiles import (
    ProfileStateRepository,
    apply_profile,
    load_profile,
)
from fdp.shared.context import RequestContext
from fdp.shared.errors import BadRequest, Conflict, Forbidden

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from fdp.config import Settings
    from fdp.metadata.profiles import ResourceDefinitionCache
    from fdp.metadata.repository import MetadataRepository
    from fdp.metadata.settings import SettingsRepository

log = structlog.get_logger(__name__)

_ADMIN_ROLE: Final = "admin"

RESET_CONFIRMATION_TOKEN: Final = "reset-to-factory-defaults"  # noqa: S105 (a public confirmation phrase, not a secret)
"""Literal token the caller must echo in the request body to authorize a reset.

Deliberately verbose so a reset can never be triggered by an empty or
accidental POST; the client surfaces it as an explicit "type this to confirm"
field."""


# --- DTOs ------------------------------------------------------------------


class ResetRequest(BaseModel):
    """Body for ``POST /admin/reset``."""

    confirmation: str = Field(
        description="Must equal the server's reset confirmation token.",
    )


class ResetResponse(BaseModel):
    """Outcome of a successful reset."""

    profile_name: str = Field(serialization_alias="profileName")
    profile_version: str = Field(serialization_alias="profileVersion")
    settings_cleared: int = Field(serialization_alias="settingsCleared")
    schemas: int
    offers: int
    resource_definitions: int = Field(serialization_alias="resourceDefinitions")
    seed_records: int = Field(serialization_alias="seedRecords")

    model_config = {"populate_by_name": True}


# --- service ---------------------------------------------------------------


class ResetService:
    """Coordinates a factory reset: truncate settings, force re-apply the profile.

    Holds the same collaborators the auto-bootstrap path uses. ``on_published``
    is the ``main`` hook that swaps the profile-derived runtime state onto
    ``app.state`` (offer-resolver fallback + resource-definition cache, with the
    OpenAPI/validator/authz refresh that piggybacks on it), so the reset is
    visible without a restart.
    """

    __slots__ = (
        "_on_published",
        "_repository",
        "_session_factory",
        "_settings",
        "_settings_repository",
    )

    def __init__(
        self,
        *,
        settings: Settings,
        settings_repository: SettingsRepository,
        repository: MetadataRepository,
        on_published: Callable[[str | None, ResourceDefinitionCache | None], Awaitable[None]],
    ) -> None:
        self._settings = settings
        self._settings_repository = settings_repository
        self._repository = repository
        self._session_factory = settings_repository.session_factory
        self._on_published = on_published

    async def reset(self, *, subject: str | None) -> ResetResponse:
        """Truncate runtime settings and force re-apply the bundled profile.

        Raises :class:`Conflict` when no profile bundle is configured — without
        a bundle there is nothing to reset *to*.
        """
        bundle = self._settings.profile.path
        if bundle is None:
            raise Conflict(
                "no bundled profile is configured; reset requires FDP_PROFILE_PATH",
                details={},
            )
        # Load + structurally validate the bundle BEFORE mutating anything, so a
        # broken bundle fails the request without having wiped settings.
        profile = load_profile(bundle)

        settings_cleared = await self._settings_repository.clear_all(subject=subject)

        # Wipe the triple store so graphs from a previous profile don't linger
        # and collide with the re-applied one (a portable SPARQL 1.1 DROP ALL).
        await self._repository.clear_all()

        async with self._session_factory() as session:
            state = ProfileStateRepository(session)
            await state.clear()
            await session.commit()
            report = await apply_profile(
                profile,
                repository=self._repository,
                state=state,
                session=session,
                settings=self._settings,
                force=True,
            )

        await self._on_published(report.system_default_offer_iri, report.resource_definitions)

        rd_count = (
            len(report.resource_definitions.all()) if report.resource_definitions is not None else 0
        )
        log.info(
            "admin_reset_completed",
            subject=subject,
            profile=profile.name,
            version=profile.version,
            settings_cleared=settings_cleared,
            graphs_written=report.total_written,
        )
        return ResetResponse(
            profile_name=profile.name,
            profile_version=profile.version,
            settings_cleared=settings_cleared,
            schemas=len(report.schemas_written),
            offers=len(report.offers_written),
            resource_definitions=rd_count,
            seed_records=len(report.seed_records_written),
        )


# --- router ----------------------------------------------------------------


def build_admin_router(
    *,
    service: ResetService,
    confirmation_token: str = RESET_CONFIRMATION_TOKEN,
) -> APIRouter:
    """Construct the factory-reset admin router (``POST /admin/reset``)."""
    router = APIRouter(prefix="/admin", tags=["admin"])

    @router.post("/reset", response_model=ResetResponse, name="admin_reset")
    async def reset(  # pyright: ignore[reportUnusedFunction]
        body: ResetRequest,
        ctx: Annotated[RequestContext, Depends(require_auth)],
    ) -> ResetResponse:
        """Reset the FDP to the bundled profile's factory defaults (admin only).

        Destructive and irreversible: the ``runtime_settings`` table is
        truncated and the deployment profile is force re-applied. The request
        body must carry the confirmation token.
        """
        if _ADMIN_ROLE not in ctx.roles:
            raise Forbidden(
                "admin role required to reset the FDP",
                details={"required_role": _ADMIN_ROLE},
            )
        if body.confirmation != confirmation_token:
            raise BadRequest(
                "confirmation token mismatch; reset not performed",
                details={"expected": confirmation_token},
            )
        return await service.reset(subject=ctx.subject)

    return router


__all__ = [
    "RESET_CONFIRMATION_TOKEN",
    "ResetRequest",
    "ResetResponse",
    "ResetService",
    "build_admin_router",
]
