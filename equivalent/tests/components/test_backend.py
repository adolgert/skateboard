"""Every call to a backend fails the same way, and none of them is a verdict.

A check answers with a statement about the code. A builder or an oracle
that could not be reached has made no statement about the code at all, so
every call goes through one place that turns a transport failure into an
error and never into a claim.
"""
from __future__ import annotations

import inspect

import pytest

from equivalent.components import backend
from equivalent.components.errors import ComponentError

# One call per backend function, and the endpoint whose name the failure
# has to carry, so a person reading the error knows what was asked for.
CALLS = {
    "build": ("build", lambda client: backend.build(
        client, "attempt-1", [], "Makefile", [], "nvfortran", [], [], [])),
    "replay": ("run", lambda client: backend.replay(client, "attempt-1", "replay", {})),
    "capture": ("capture", lambda client: backend.capture(
        client, "attempt-1", "gen_reference", [], "visible")),
    "sanitize": ("sanitize", lambda client: backend.sanitize(
        client, "attempt-1", "replay", {}, ["memcheck"])),
    "properties": ("properties", lambda client: backend.properties(
        client, "attempt-1", "replay", "harness/properties.py", {}, 7, 25)),
    "mutate": ("mutate", lambda client: backend.mutate(
        client, "attempt-1", "Makefile", {}, [], {}, {}, "nvfortran", [], [], [])),
    "time": ("time", lambda client: backend.time(
        client, "attempt-1", "whole_program", [], {}, [], 2, 60)),
    "compare": ("compare", lambda client: backend.compare(client, "visible", {})),
}


class Unreachable:
    """A backend every call to which is a connection that did not happen."""

    def __getattr__(self, name):
        def call(*args, **kwargs):
            raise ConnectionError("connection reset")
        return call


@pytest.mark.parametrize("name", sorted(CALLS))
def test_a_backend_that_could_not_be_reached_is_an_error_naming_what_was_asked(name):
    endpoint, call = CALLS[name]

    with pytest.raises(ComponentError) as caught:
        call(Unreachable())

    assert f"/v1/{endpoint}" in str(caught.value)
    assert "connection reset" in str(caught.value)


def test_every_backend_call_the_checks_make_is_one_of_these():
    # A call added here without a row above would be a call with no rule
    # about what its failure means, which is the whole point of the module.
    public = {
        name for name, function in inspect.getmembers(backend, inspect.isfunction)
        if not name.startswith("_") and inspect.getmodule(function) is backend
    }

    assert public == set(CALLS)
