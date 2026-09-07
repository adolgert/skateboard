"""The predicate type registry.

Trust role: this table decides:
 * whether a check's result may be reused instead of re-run, and
 * how much of a claim's detail an agent is allowed to see.

A human, via the ledger CLI, always sees a claim's full detail regardless
of this table — that is a property of the CLI reading claims.jsonl
directly, not a per-predicate policy, so it is not a field here.
"""
from __future__ import annotations

from dataclasses import dataclass

from .records import Predicate
from .workflow import ACTIONS, DetailLevel


@dataclass(frozen=True)
class PredicateType:
    name: str
    deterministic: bool
    agent_detail: DetailLevel
    description: str


PREDICATE_TYPES = {
    predicate.name: PredicateType(
        predicate.name, action.deterministic, predicate.agent_detail, predicate.description,
    )
    for action in ACTIONS for predicate in action.predicates
}


def get(name: str) -> PredicateType:
    return PREDICATE_TYPES[name]


def is_deterministic(name: str) -> bool:
    return get(name).deterministic


def agent_receipt(predicate_type: str, predicate: Predicate) -> dict:
    """What the agent's tool-call result may contain for this predicate."""
    if get(predicate_type).agent_detail is DetailLevel.FULL:
        return {"verdict": predicate.verdict, "detail": predicate.detail}
    return {"verdict": predicate.verdict}
