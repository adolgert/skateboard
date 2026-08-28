"""The predicate type registry.

Trust role: this table decides:
 * whether a check's result may be reused instead of re-run, and
 * how much of a claim's detail an agent is allowed to see.

A human, via the ledger CLI, always sees a claim's full detail regardless
of this table — that is a property of the CLI reading claims.jsonl
directly, not a per-predicate policy, so it is not a field here.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass

from .records import Predicate


class DetailLevel(enum.Enum):
    VERDICT_ONLY = "verdict_only"
    FULL = "full"


@dataclass(frozen=True)
class PredicateType:
    name: str
    deterministic: bool
    agent_detail: DetailLevel
    description: str


PREDICATE_TYPES: dict[str, PredicateType] = {}


def _register(name: str, deterministic: bool, agent_detail: DetailLevel, description: str) -> None:
    PREDICATE_TYPES[name] = PredicateType(name, deterministic, agent_detail, description)


_register(
    "sese/verified", True, DetailLevel.FULL,
    "Static analyzer confirms the region and its closure are single-entry/"
    "single-exit (no goto, early return, entry, or stop), on the frozen "
    "set. Does not check that the spec's declared footprint matches the "
    "code -- that needs real static-analysis tooling this repository "
    "doesn't yet run generically; see equivalent/components/sese_check.py.",
)
_register(
    "build/replay", True, DetailLevel.FULL,
    "The replay harness compiles against the strategy's flags, on the tree.",
)
_register(
    "gpu/executed", True, DetailLevel.FULL,
    "The device-proof mechanism observed at least one kernel launch, on the tree.",
)
_register(
    "sanitize/memcheck", True, DetailLevel.FULL,
    "compute-sanitizer memcheck ran clean, on the tree.",
)
_register(
    "sanitize/racecheck", True, DetailLevel.FULL,
    "compute-sanitizer racecheck ran clean, on the tree.",
)
_register(
    "sanitize/initcheck", True, DetailLevel.FULL,
    "compute-sanitizer initcheck ran clean, on the tree.",
)
_register(
    "regression/visible", True, DetailLevel.FULL,
    "Oracle comparison against the visible capture set, on the tree; detail is the per-case breakdown.",
)
_register(
    "regression/holdout", True, DetailLevel.VERDICT_ONLY,
    "Oracle comparison against the held-out capture set, on the tree; no per-case detail ever leaves the oracle.",
)
_register(
    "program/regression", True, DetailLevel.FULL,
    "The code's own program, run at the size its manifest declares, wrote what the "
    "baseline program wrote, under the code's tolerance policy; detail is the "
    "per-output breakdown. On the tree.",
)
_register(
    "regression/property", True, DetailLevel.FULL,
    "The code's own property module passed, on the tree, at the recorded seed. Where the "
    "regression checks compare a port against captured answers, this one searches for an "
    "input on which an invariant the code states does not hold; detail is the seed, how "
    "many examples were drawn, and what the run printed.",
)
_register(
    "timing/port", False, DetailLevel.FULL,
    "Wall-clock timing of the ported binary, on the tree.",
)
_register(
    "timing/baseline", False, DetailLevel.FULL,
    "Wall-clock timing of the pristine baseline build, on the baseline tree; its "
    "program's own outputs are stored as the capture set a port's program run is "
    "compared against.",
)

# What an onboarding session files. All of it is the agent's own work
# being checked -- the manifest, the makefile, the drivers it wrote -- so
# the agent sees the whole detail: it is the only way it can fix what
# failed.
_register(
    "manifest/valid", True, DetailLevel.FULL,
    "The manifest the tree carries describes the code completely: every path it "
    "names is in the tree, every floating-point output has a tolerance band, and "
    "the two datasets are different runs. On the tree.",
)
_register(
    "harness/builds", True, DetailLevel.FULL,
    "Every target the tree's manifest declares builds under both the baseline "
    "strategy and the port strategy, with each strategy's flags proven to have "
    "reached every compile. On the tree.",
)
_register(
    "harness/captured", True, DetailLevel.FULL,
    "The capture program wrote every dataset the manifest declares; each case holds "
    "exactly the variables the region declares, of the declared type and rank; and "
    "the visible and held-out inputs are not the same run. The sets it wrote are "
    "stored in the ledger and named in this claim's materials. On the tree.",
)
_register(
    "harness/replays", True, DetailLevel.FULL,
    "The replay driver, built the way the baseline is built, reproduces every "
    "captured output bitwise from the captured inputs. On the tree.",
)
_register(
    "harness/deterministic", True, DetailLevel.FULL,
    "Capturing each dataset again writes the set already stored, and replaying the "
    "visible inputs twice writes the same outputs twice. On the tree.",
)
_register(
    "harness/times", True, DetailLevel.FULL,
    "The timing program runs twice inside its declared budget and writes the same "
    "declared outputs both times; the last run's outputs are stored as the code's "
    "program capture set. On the tree.",
)


def get(name: str) -> PredicateType:
    return PREDICATE_TYPES[name]


def is_deterministic(name: str) -> bool:
    return get(name).deterministic


def agent_receipt(predicate_type: str, predicate: Predicate) -> dict:
    """What the agent's tool-call result may contain for this predicate."""
    if get(predicate_type).agent_detail is DetailLevel.FULL:
        return {"verdict": predicate.verdict, "detail": predicate.detail}
    return {"verdict": predicate.verdict}
