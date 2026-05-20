"""Parser tests — hand-rolled Offers in Turtle round-trip through the model."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from rdflib import Graph, URIRef

from fdp.policy.model import (
    Action,
    ConflictStrategy,
    ConstraintOperator,
    GroupConstraint,
    PartyConstraint,
    RoleConstraint,
    TimeConstraint,
)
from fdp.policy.parser import parse_offer
from fdp.shared.errors import SchemaViolation

OFFER_IRI = URIRef("https://example.org/offer/1")
PREFIXES = """\
@prefix odrl: <http://www.w3.org/ns/odrl/2/> .
@prefix fdpx: <https://specs.fairdatapoint.org/odrl-profile#> .
@prefix xsd:  <http://www.w3.org/2001/XMLSchema#> .
@prefix ex:   <https://example.org/> .
"""


def _graph(turtle: str) -> Graph:
    g = Graph()
    g.parse(data=PREFIXES + turtle, format="turtle")
    return g


@pytest.mark.unit
def test_minimal_offer_with_one_permission_parses() -> None:
    offer = parse_offer(
        _graph(
            """
        <https://example.org/offer/1> a odrl:Offer ;
            odrl:assigner <https://example.org/catalog/x> ;
            odrl:permission [
                a odrl:Permission ;
                odrl:action odrl:read
            ] .
        """
        ),
        OFFER_IRI,
    )
    assert offer.iri == str(OFFER_IRI)
    assert offer.assigner == "https://example.org/catalog/x"
    assert offer.conflict_strategy == ConflictStrategy.DENY_WINS
    assert len(offer.permissions) == 1
    assert offer.permissions[0].action == Action.READ
    assert offer.permissions[0].constraints == ()


@pytest.mark.unit
def test_party_constraint_round_trip() -> None:
    offer = parse_offer(
        _graph(
            """
        <https://example.org/offer/1> a odrl:Offer ;
            odrl:permission [
                a odrl:Permission ;
                odrl:action odrl:modify ;
                odrl:constraint [
                    odrl:leftOperand odrl:assignee ;
                    odrl:operator odrl:eq ;
                    odrl:rightOperand ex:alice
                ]
            ] .
        """
        ),
        OFFER_IRI,
    )
    constraint = offer.permissions[0].constraints[0]
    assert isinstance(constraint, PartyConstraint)
    assert constraint.operator == ConstraintOperator.EQ
    assert constraint.party_uri == "https://example.org/alice"


@pytest.mark.unit
def test_role_constraint_round_trip() -> None:
    offer = parse_offer(
        _graph(
            """
        <https://example.org/offer/1> a odrl:Offer ;
            odrl:permission [
                a odrl:Permission ;
                odrl:action odrl:modify ;
                odrl:constraint [
                    odrl:leftOperand fdpx:role ;
                    odrl:operator odrl:eq ;
                    odrl:rightOperand "steward"
                ]
            ] .
        """
        ),
        OFFER_IRI,
    )
    constraint = offer.permissions[0].constraints[0]
    assert isinstance(constraint, RoleConstraint)
    assert constraint.role == "steward"


@pytest.mark.unit
def test_group_constraint_round_trip() -> None:
    offer = parse_offer(
        _graph(
            """
        <https://example.org/offer/1> a odrl:Offer ;
            odrl:permission [
                a odrl:Permission ;
                odrl:action odrl:read ;
                odrl:constraint [
                    odrl:leftOperand fdpx:group ;
                    odrl:operator odrl:eq ;
                    odrl:rightOperand "biobank-x"
                ]
            ] .
        """
        ),
        OFFER_IRI,
    )
    constraint = offer.permissions[0].constraints[0]
    assert isinstance(constraint, GroupConstraint)
    assert constraint.group == "biobank-x"


@pytest.mark.unit
def test_time_constraint_round_trip() -> None:
    offer = parse_offer(
        _graph(
            """
        <https://example.org/offer/1> a odrl:Offer ;
            odrl:permission [
                a odrl:Permission ;
                odrl:action odrl:read ;
                odrl:constraint [
                    odrl:leftOperand odrl:dateTime ;
                    odrl:operator odrl:gteq ;
                    odrl:rightOperand "2026-01-01T00:00:00Z"^^xsd:dateTime
                ]
            ] .
        """
        ),
        OFFER_IRI,
    )
    constraint = offer.permissions[0].constraints[0]
    assert isinstance(constraint, TimeConstraint)
    assert constraint.operator == ConstraintOperator.GTEQ
    assert constraint.timestamp == datetime(2026, 1, 1, tzinfo=UTC)


@pytest.mark.unit
def test_prohibition_and_conflict_strategy_round_trip() -> None:
    offer = parse_offer(
        _graph(
            """
        <https://example.org/offer/1> a odrl:Offer ;
            odrl:conflict odrl:perm ;
            odrl:permission [ a odrl:Permission ; odrl:action odrl:read ] ;
            odrl:prohibition [ a odrl:Prohibition ; odrl:action odrl:delete ] .
        """
        ),
        OFFER_IRI,
    )
    assert offer.conflict_strategy == ConflictStrategy.PERM_WINS
    assert len(offer.permissions) == 1
    assert len(offer.prohibitions) == 1
    assert offer.prohibitions[0].action == Action.DELETE


@pytest.mark.unit
def test_non_offer_subject_rejected() -> None:
    g = _graph("<https://example.org/offer/1> a ex:NotAnOffer .")
    with pytest.raises(SchemaViolation, match="not an odrl:Offer"):
        parse_offer(g, OFFER_IRI)


@pytest.mark.unit
def test_unsupported_action_rejected() -> None:
    g = _graph(
        """
    <https://example.org/offer/1> a odrl:Offer ;
        odrl:permission [ a odrl:Permission ; odrl:action odrl:transfer ] .
    """
    )
    with pytest.raises(SchemaViolation, match="unsupported odrl:action"):
        parse_offer(g, OFFER_IRI)


@pytest.mark.unit
def test_unsupported_conflict_value_rejected() -> None:
    g = _graph(
        """
    <https://example.org/offer/1> a odrl:Offer ;
        odrl:conflict ex:weirdStrategy .
    """
    )
    with pytest.raises(SchemaViolation, match="unsupported odrl:conflict"):
        parse_offer(g, OFFER_IRI)


@pytest.mark.unit
def test_unsupported_left_operand_rejected() -> None:
    g = _graph(
        """
    <https://example.org/offer/1> a odrl:Offer ;
        odrl:permission [
            a odrl:Permission ;
            odrl:action odrl:read ;
            odrl:constraint [
                odrl:leftOperand odrl:purpose ;
                odrl:operator odrl:eq ;
                odrl:rightOperand "research"
            ]
        ] .
    """
    )
    with pytest.raises(SchemaViolation, match="unsupported odrl:leftOperand"):
        parse_offer(g, OFFER_IRI)


@pytest.mark.unit
def test_role_constraint_with_unsupported_operator_rejected() -> None:
    g = _graph(
        """
    <https://example.org/offer/1> a odrl:Offer ;
        odrl:permission [
            a odrl:Permission ;
            odrl:action odrl:read ;
            odrl:constraint [
                odrl:leftOperand fdpx:role ;
                odrl:operator odrl:gt ;
                odrl:rightOperand "steward"
            ]
        ] .
    """
    )
    with pytest.raises(SchemaViolation, match="fdp:role supports only eq/neq"):
        parse_offer(g, OFFER_IRI)


@pytest.mark.unit
def test_time_constraint_must_be_xsd_datetime() -> None:
    g = _graph(
        """
    <https://example.org/offer/1> a odrl:Offer ;
        odrl:permission [
            a odrl:Permission ;
            odrl:action odrl:read ;
            odrl:constraint [
                odrl:leftOperand odrl:dateTime ;
                odrl:operator odrl:gt ;
                odrl:rightOperand "2026-01-01"
            ]
        ] .
    """
    )
    with pytest.raises(SchemaViolation, match="xsd:dateTime"):
        parse_offer(g, OFFER_IRI)
