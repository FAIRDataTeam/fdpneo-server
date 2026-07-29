"""Parse an ODRL ``Offer`` graph into the FDP profile model.

The parser is strict: any triple that uses features outside the FDP
profile causes a :class:`~fdpneo_server.shared.errors.SchemaViolation` whose
``details`` payload points at the offending triple. This is the seam at
which the visual editor / API layer rejects bad policies on write.

Supported shape (Turtle illustration)::

    <#offer> a odrl:Offer ;
        odrl:assigner <#catalog-x> ;
        odrl:conflict odrl:perm ;
        odrl:permission [
            a odrl:Permission ;
            odrl:action odrl:read ;
            odrl:constraint [
                odrl:leftOperand odrl:assignee ;
                odrl:operator odrl:eq ;
                odrl:rightOperand <#alice>
            ]
        ] ;
        odrl:prohibition [
            a odrl:Prohibition ;
            odrl:action odrl:delete
        ] .
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from rdflib import RDF, XSD, Literal, URIRef
from rdflib.term import Node

from fdpneo_server.policy.model import (
    ACTION_IRIS,
    CONFLICT_IRIS,
    FDP_PROFILE_NS,
    ODRL_NS,
    OPERATOR_IRIS,
    ConflictStrategy,
    Constraint,
    ConstraintOperator,
    GroupConstraint,
    Offer,
    PartyConstraint,
    Permission,
    Prohibition,
    RoleConstraint,
    Rule,
    TimeConstraint,
)
from fdpneo_server.shared.errors import SchemaViolation

if TYPE_CHECKING:
    from rdflib import Graph

_LEFT_PARTY = ODRL_NS.assignee
_LEFT_TIME = ODRL_NS.dateTime
_LEFT_ROLE = FDP_PROFILE_NS.role
_LEFT_GROUP = FDP_PROFILE_NS.group
_MEMBERSHIP_OPERATORS = {ConstraintOperator.EQ, ConstraintOperator.NEQ}


def parse_offer(graph: Graph, offer_iri: URIRef) -> Offer:
    """Parse the Offer rooted at ``offer_iri`` from ``graph``."""
    if (offer_iri, RDF.type, ODRL_NS.Offer) not in graph:
        raise SchemaViolation(
            f"{offer_iri} is not an odrl:Offer",
            details={"subject": str(offer_iri)},
        )

    assigner_node = graph.value(offer_iri, ODRL_NS.assigner)
    assigner = str(assigner_node) if assigner_node is not None else None

    conflict_node = graph.value(offer_iri, ODRL_NS.conflict)
    conflict_strategy = _parse_conflict(conflict_node, offer_iri)

    permissions = tuple(
        _parse_rule(graph, rule_node, Permission, offer_iri)
        for rule_node in graph.objects(offer_iri, ODRL_NS.permission)
    )
    prohibitions = tuple(
        _parse_rule(graph, rule_node, Prohibition, offer_iri)
        for rule_node in graph.objects(offer_iri, ODRL_NS.prohibition)
    )

    return Offer(
        iri=str(offer_iri),
        assigner=assigner,
        permissions=permissions,
        prohibitions=prohibitions,
        conflict_strategy=conflict_strategy,
    )


def _parse_conflict(node: Node | None, offer_iri: URIRef) -> ConflictStrategy:
    if node is None:
        return ConflictStrategy.DENY_WINS
    strategy = CONFLICT_IRIS.get(str(node))
    if strategy is None:
        raise SchemaViolation(
            f"unsupported odrl:conflict value: {node}",
            details={
                "offer": str(offer_iri),
                "predicate": str(ODRL_NS.conflict),
                "object": str(node),
            },
        )
    return strategy


def _parse_rule[T: Rule](
    graph: Graph,
    rule_node: Node,
    cls: type[T],
    offer_iri: URIRef,
) -> T:
    expected_type = ODRL_NS.Permission if cls is Permission else ODRL_NS.Prohibition
    if (rule_node, RDF.type, expected_type) not in graph:
        raise SchemaViolation(
            f"rule {rule_node} is missing rdf:type {expected_type}",
            details={"offer": str(offer_iri), "rule": str(rule_node)},
        )

    action_node = graph.value(rule_node, ODRL_NS.action)
    if action_node is None:
        raise SchemaViolation(
            f"rule {rule_node} has no odrl:action",
            details={"offer": str(offer_iri), "rule": str(rule_node)},
        )
    action = ACTION_IRIS.get(str(action_node))
    if action is None:
        raise SchemaViolation(
            f"unsupported odrl:action: {action_node}",
            details={"offer": str(offer_iri), "rule": str(rule_node), "action": str(action_node)},
        )

    constraints = tuple(
        _parse_constraint(graph, c, offer_iri, rule_node)
        for c in graph.objects(rule_node, ODRL_NS.constraint)
    )
    return cls(action=action, constraints=constraints)


def _parse_constraint(
    graph: Graph,
    constraint_node: Node,
    offer_iri: URIRef,
    rule_node: Node,
) -> Constraint:
    left = graph.value(constraint_node, ODRL_NS.leftOperand)
    operator_node = graph.value(constraint_node, ODRL_NS.operator)
    right = graph.value(constraint_node, ODRL_NS.rightOperand)

    if left is None or operator_node is None or right is None:
        raise SchemaViolation(
            "constraint is missing leftOperand, operator, or rightOperand",
            details={
                "offer": str(offer_iri),
                "rule": str(rule_node),
                "constraint": str(constraint_node),
            },
        )

    operator = OPERATOR_IRIS.get(str(operator_node))
    if operator is None:
        raise SchemaViolation(
            f"unsupported odrl:operator: {operator_node}",
            details={
                "offer": str(offer_iri),
                "rule": str(rule_node),
                "constraint": str(constraint_node),
                "operator": str(operator_node),
            },
        )

    if left == _LEFT_PARTY:
        if operator not in _MEMBERSHIP_OPERATORS:
            raise SchemaViolation(
                f"odrl:assignee supports only eq/neq, got {operator.value}",
                details={"constraint": str(constraint_node), "operator": operator.value},
            )
        if not isinstance(right, URIRef):
            raise SchemaViolation(
                "odrl:assignee rightOperand must be a URI",
                details={"constraint": str(constraint_node), "rightOperand": str(right)},
            )
        return PartyConstraint(operator=operator, party_uri=str(right))

    if left == _LEFT_ROLE:
        if operator not in _MEMBERSHIP_OPERATORS:
            raise SchemaViolation(
                f"fdp:role supports only eq/neq, got {operator.value}",
                details={"constraint": str(constraint_node), "operator": operator.value},
            )
        return RoleConstraint(operator=operator, role=str(right))

    if left == _LEFT_GROUP:
        if operator not in _MEMBERSHIP_OPERATORS:
            raise SchemaViolation(
                f"fdp:group supports only eq/neq, got {operator.value}",
                details={"constraint": str(constraint_node), "operator": operator.value},
            )
        return GroupConstraint(operator=operator, group=str(right))

    if left == _LEFT_TIME:
        timestamp = _parse_xsd_datetime(right, constraint_node)
        return TimeConstraint(operator=operator, timestamp=timestamp)

    raise SchemaViolation(
        f"unsupported odrl:leftOperand: {left}",
        details={"constraint": str(constraint_node), "leftOperand": str(left)},
    )


def _parse_xsd_datetime(node: Node, constraint_node: Node) -> datetime:
    if not isinstance(node, Literal):
        raise SchemaViolation(
            "time constraint rightOperand must be an xsd:dateTime literal",
            details={"constraint": str(constraint_node), "rightOperand": str(node)},
        )
    if node.datatype not in (XSD.dateTime, XSD.dateTimeStamp):
        raise SchemaViolation(
            f"time constraint datatype must be xsd:dateTime, got {node.datatype}",
            details={"constraint": str(constraint_node), "datatype": str(node.datatype)},
        )
    value = node.toPython()
    if not isinstance(value, datetime):
        raise SchemaViolation(
            "time constraint literal did not parse to a datetime",
            details={"constraint": str(constraint_node), "rightOperand": str(node)},
        )
    return value


__all__ = ["parse_offer"]
