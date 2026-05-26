"""Unit tests for :mod:`fdp.access.rewriter`."""

from __future__ import annotations

from typing import cast

import pytest

from fdp.access.parser import ParsedRead, ParsedUpdate, QueryForm
from fdp.access.rewriter import RewrittenRead, authorize_update, rewrite_read
from fdp.shared.errors import PolicyViolation

G1 = "https://example.org/g1"
G2 = "https://example.org/g2"
G3 = "https://example.org/g3"


def _read(
    *,
    default: tuple[str, ...] = (),
    named: tuple[str, ...] = (),
    inline: tuple[str, ...] = (),
) -> ParsedRead:
    return ParsedRead(
        form=QueryForm.SELECT,
        explicit_default_graphs=default,
        explicit_named_graphs=named,
        inline_graph_iris=inline,
    )


# --- rewrite_read: dataset projection ---------------------------------------


@pytest.mark.unit
def test_no_explicit_refs_projects_full_authorized_set() -> None:
    result = rewrite_read(_read(), authorized_read={G2, G1})
    assert isinstance(result, RewrittenRead)
    # Sorted for deterministic outbound protocol params.
    assert result.named_graph_uris == (G1, G2)
    assert result.default_graph_uris == ()


@pytest.mark.unit
def test_no_explicit_refs_with_empty_authorized_returns_empty_projection() -> None:
    result = rewrite_read(_read(), authorized_read=set())
    assert result.named_graph_uris == ()
    assert result.default_graph_uris == ()


@pytest.mark.unit
def test_inline_only_projects_just_those_graphs() -> None:
    result = rewrite_read(
        _read(inline=(G1,)),
        authorized_read={G1, G2, G3},
    )
    assert result.named_graph_uris == (G1,)
    assert result.default_graph_uris == ()


@pytest.mark.unit
def test_from_clause_in_query_skips_protocol_params() -> None:
    # User wrote `FROM <G1>` — protocol params would be ignored by spec,
    # so we send none.
    result = rewrite_read(
        _read(default=(G1,)),
        authorized_read={G1, G2},
    )
    assert result.default_graph_uris == ()
    assert result.named_graph_uris == ()


@pytest.mark.unit
def test_from_named_clause_in_query_skips_protocol_params() -> None:
    result = rewrite_read(
        _read(named=(G1, G2)),
        authorized_read={G1, G2, G3},
    )
    assert result.default_graph_uris == ()
    assert result.named_graph_uris == ()


@pytest.mark.unit
def test_from_named_plus_inline_still_skips_protocol_params_because_dataset_clause_exists() -> None:
    # FROM NAMED is a dataset clause; inline GRAPH is not. The presence
    # of either FROM / FROM NAMED makes the query's dataset authoritative.
    result = rewrite_read(
        _read(named=(G1,), inline=(G2,)),
        authorized_read={G1, G2},
    )
    assert result.named_graph_uris == ()


# --- rewrite_read: authorization --------------------------------------------


@pytest.mark.unit
def test_unauthorized_from_clause_raises_policy_violation() -> None:
    with pytest.raises(PolicyViolation) as excinfo:
        rewrite_read(_read(default=(G1,)), authorized_read={G2})
    details = cast(dict[str, str], excinfo.value.details)
    assert details["graph"] == G1
    assert details["action"] == "read"


@pytest.mark.unit
def test_unauthorized_from_named_clause_raises_policy_violation() -> None:
    with pytest.raises(PolicyViolation, match=G1):
        rewrite_read(_read(named=(G1,)), authorized_read={G2})


@pytest.mark.unit
def test_unauthorized_inline_graph_raises_policy_violation() -> None:
    with pytest.raises(PolicyViolation, match=G1):
        rewrite_read(_read(inline=(G1,)), authorized_read={G2})


@pytest.mark.unit
def test_mixed_explicit_refs_reject_first_unauthorized() -> None:
    # G1 authorized; G2 not. Rejection should fire even though G1 is fine.
    with pytest.raises(PolicyViolation, match=G2):
        rewrite_read(
            _read(default=(G1,), named=(G2,)),
            authorized_read={G1, G3},
        )


@pytest.mark.unit
def test_all_explicit_refs_authorized_pass_through() -> None:
    # No raise expected.
    result = rewrite_read(
        _read(default=(G1,), named=(G2,), inline=(G3,)),
        authorized_read={G1, G2, G3},
    )
    assert result.named_graph_uris == ()
    assert result.default_graph_uris == ()


# --- authorize_update -------------------------------------------------------


@pytest.mark.unit
def test_authorize_update_passes_when_every_target_is_authorized() -> None:
    parsed = ParsedUpdate(targets=(G1, G2))
    authorize_update(parsed, authorized_modify={G1, G2, G3})  # no raise


@pytest.mark.unit
def test_authorize_update_raises_on_any_unauthorized_target() -> None:
    parsed = ParsedUpdate(targets=(G1, G2))
    with pytest.raises(PolicyViolation) as excinfo:
        authorize_update(parsed, authorized_modify={G1})
    details = cast(dict[str, str], excinfo.value.details)
    assert details["graph"] == G2
    assert details["action"] == "modify"


@pytest.mark.unit
def test_authorize_update_with_empty_targets_is_a_noop() -> None:
    authorize_update(ParsedUpdate(targets=()), authorized_modify=set())


@pytest.mark.unit
def test_authorize_update_with_duplicate_target_only_needs_one_authorization() -> None:
    # The parser may produce duplicates (e.g. user repeats the same graph
    # across operations); authorization still passes if the IRI is in the
    # set.
    parsed = ParsedUpdate(targets=(G1, G1, G1))
    authorize_update(parsed, authorized_modify={G1})  # no raise
