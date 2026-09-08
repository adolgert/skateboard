"""An invalid workflow fails at definition time, before a session starts."""
from dataclasses import replace

import pytest

from equivalent.ledger.workflow import ACTIONS, ONBOARDING, _validate


def test_duplicate_actions_and_predicate_producers_are_rejected():
    with pytest.raises(ValueError, match="unique"):
        _validate((*ACTIONS, ACTIONS[0]))
    with pytest.raises(ValueError, match="unique"):
        _validate((*ACTIONS, replace(ACTIONS[0], name="another_analyzer")))


def test_unknown_prerequisites_cannot_leave_an_action_permanently_blocked():
    with pytest.raises(ValueError, match="unknown predicate"):
        _validate((replace(ACTIONS[0], requires=("no/such/evidence",)),))


def test_cross_phase_prerequisites_are_rejected():
    first, second = ACTIONS[:2]
    with pytest.raises(ValueError, match="another phase"):
        _validate((first, replace(second, phase=ONBOARDING)))


def test_prerequisite_cycles_are_rejected_even_when_every_predicate_exists():
    first, second, third = ACTIONS[:3]
    with pytest.raises(ValueError, match="prerequisite cycle"):
        _validate((replace(first, requires=third.emits), second, third))
