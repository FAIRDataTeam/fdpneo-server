"""Read-visibility port (publication state, ADR-0010).

Read PEPs outside the ``metadata`` module — notably the SPARQL access endpoint —
must narrow a caller's authorized graphs to those *visible* under the
publication-state rule, without depending on the module that implements that
rule. They depend on this structural port; ``metadata.lifecycle.StateGate``
satisfies it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from fdpneo_server.shared.context import RequestContext


class StateVisibility(Protocol):
    """Resolves which named graphs a caller may read once state is layered on.

    All three resolutions are *deterministic*: candidates come from the store
    and missing ODRL decisions are evaluated (and cached) on demand, so the
    answer never depends on which resources the subject fetched before.
    """

    async def visible_read_graphs(self, ctx: RequestContext) -> set[str]: ...

    async def updatable_graphs(self, ctx: RequestContext) -> set[str]: ...

    async def update_read_scope(self, ctx: RequestContext) -> set[str]: ...


__all__ = ["StateVisibility"]
