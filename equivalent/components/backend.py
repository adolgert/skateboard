"""The builder calls more than one check makes, made the same way each time.

Trust role: a check answers with a verdict about the code, and a builder
that could not be reached has said nothing about the code at all. Every
call here turns a transport failure into a ComponentError for exactly
that reason: read as a verdict it would file a claim saying a port is
wrong when all that was wrong was a request.

The calls are here rather than in each check because the same driver is
run by a port's check and by the onboarding check that qualifies it, and
the same capture program by the check that stores a dataset and the one
that asks for it again. What comes back means different things to each of
them, and each of them judges it; how it is asked for is one thing.
"""
from __future__ import annotations

from .context import CheckContext
from .errors import ComponentError


def replay(ctx: CheckContext, attempt_id: str, executable: str, cases: dict,
           *, notify=None, mandatory: bool = False):
    """Run the replay driver over a set of cases in one builder workspace.

    `notify` and `mandatory` are the strategy's device proof, asked for
    only where whether anything offloaded is the question: a harness
    replay is two halves of one CPU harness agreeing.
    """
    try:
        return ctx.builder.run(attempt_id, executable, cases, notify=notify, mandatory=mandatory)
    except Exception as exc:
        raise ComponentError(f"builder /v1/run call failed: {exc}") from exc


def capture(ctx: CheckContext, attempt_id: str, executable: str, args, dataset: str):
    """Run the capture program for one dataset, into a directory of its own."""
    try:
        return ctx.builder.capture(attempt_id, executable, list(args), dataset)
    except Exception as exc:
        raise ComponentError(f"builder /v1/capture call failed: {exc}") from exc
