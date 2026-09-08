"""Every check has the one signature, and every action has a check.

A check is a function of a context and a request's settings, and nothing
else. That is what lets the gateway hold the whole dispatch in one line
and what keeps a check testable without a deployment around it, so it is
asserted here rather than left to nineteen call sites to agree about.

The parity between the action table and the handler table is asserted at
import of the handler table as well, so a gateway with a hole in it does
not come up; this says the same thing where a person reads it.
"""
from __future__ import annotations

import inspect
import pkgutil
from importlib import import_module

from equivalent import components
from equivalent.gateway.dispatch import HANDLERS
from equivalent.ledger.table import ACTION_TABLE

# The parameters every check takes: what it is judging, and what the
# request asked for.
SIGNATURE = ("ctx", "config")


def _checks() -> dict:
    """Every public `check...` function in the components package, by module."""
    found = {}
    for module_info in pkgutil.iter_modules(components.__path__):
        module = import_module(f"{components.__name__}.{module_info.name}")
        for name, value in vars(module).items():
            if (
                name.startswith("check")
                and inspect.isfunction(value)
                and value.__module__ == module.__name__
            ):
                found[f"{module_info.name}.{name}"] = value
    return found


def test_every_check_takes_a_context_and_the_requests_settings():
    wrong = {
        name: list(inspect.signature(function).parameters)
        for name, function in sorted(_checks().items())
        if list(inspect.signature(function).parameters) != list(SIGNATURE)
    }

    assert wrong == {}, (
        f"these checks do not take {list(SIGNATURE)}: {wrong}. A check reads what it "
        f"needs from the context; anything else it asked for would be a second way "
        f"into the same facts"
    )


def test_every_check_is_reachable_from_the_handler_table():
    dispatched = {handler.check for handler in HANDLERS.values()}

    unreachable = sorted(
        name for name, function in _checks().items() if function not in dispatched
    )

    assert unreachable == [], (
        f"these checks are not dispatched to by any action: {unreachable}. A check "
        f"nothing runs is a check nobody runs"
    )


def test_every_action_with_a_component_has_a_check_and_every_check_a_row():
    dispatchable = {row.name for row in ACTION_TABLE if row.dispatchable}

    assert set(HANDLERS) == dispatchable
