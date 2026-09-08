"""The precondition table: one row per action the gateway knows about.

Trust role: this is what POST /run checks before it will dispatch to
anything. A row with the wrong `requires` lets a check run on evidence
it should not trust, or blocks one that is actually ready.

Every row belongs to one phase. A region is in one phase for its whole
life, so the rows a session ever sees are the rows of its region's
phase; the table holds both because one gateway serves regions of both
kinds, and because a reader of an old ledger has to be able to look up
an action of either.

It lives beside the acceptance list rather than in the gateway: what an
action needs before it may run is the same fact whether a gateway is
about to dispatch it or a session summary is reading back what was run,
and the summary has to be readable on a host with no gateway installed.
"""
from __future__ import annotations

from dataclasses import dataclass

from equivalent.ledger.acceptance import (
    ACCEPTANCE_REQUIREMENTS,
    ONBOARDING,
    ONBOARDING_REQUIREMENTS,
    PORTING,
    acceptance_requirements,
)
from equivalent.ledger.subjects import Subject
from equivalent.ledger.workflow import ACTIONS, SUBJECT_KIND_OF

# The row that names a phase's whole requirement list rather than an
# action to dispatch. The porting one is the only row whose preconditions
# depend on the code, so it is spelled here for `requires_for` to find.
ACCEPT = "accept"

# What a config key means and what shape its value has, written once.
# GET /table serves this beside the row that declares the key, so the pi
# extension can offer the key as a typed tool parameter without keeping a
# second copy of the wording, and POST /run checks a value against the
# same type before it hashes the config.
CONFIG_KEY_SPECS = {
    "repeats": {
        "type": "integer",
        "minimum": 5,
        "maximum": 100,
        "description": "How many timed runs to make. Five when left out.",
    },
    "seed": {
        "type": "integer",
        "minimum": -(2 ** 63),
        "maximum": 2 ** 63 - 1,
        "description": (
            "The Hypothesis seed for the search. One is drawn for the run "
            "when left out, and the claim records which."
        ),
    },
    "max_examples": {
        "type": "integer",
        "minimum": 100,
        "maximum": 10000,
        "description": (
            "The ceiling on examples Hypothesis may generate per property. "
            "A hundred when left out; actual replay executions are reported separately."
        ),
    },
    "limit": {
        "type": "integer",
        "minimum": 1,
        "maximum": 10000,
        "description": "Score at most this many mutants, rather than all of them.",
    },
}


# The subjects one request has to hand, by the names a row's `requires`
# and a check's answer call them. Written here, once, because a request
# resolves all three before it decides anything and three readers -- the
# precondition gate, the duplicate lookup, and the claim a check's
# verdict is filed as -- each look one of them up by name. A name only
# one of them knew would file a claim against a subject the gate never
# checked.
REQUEST_SUBJECT_KINDS = ("tree", "frozen", "baseline_tree")


@dataclass(frozen=True)
class ActionRow:
    name: str
    emits: tuple  # tuple[str, ...] -- predicate types this action can produce
    # (predicate_type, subject_kind) pairs, each subject_kind one of
    # REQUEST_SUBJECT_KINDS above.
    requires: tuple
    deterministic: bool
    # None only for the row that names a phase's whole requirement list --
    # "accept" and "onboarded" -- which has nothing to dispatch to.
    component: str | None
    # Which kind of session this action belongs to. It is written on every
    # row rather than inferred from the name so that GET /table can serve
    # one phase's rows without a second list saying which those are.
    phase: str
    # The config keys POST /run accepts for this action; anything else is
    # rejected before dispatch. This keeps the config canonical, so
    # duplicate detection (which hashes the config) can't be defeated by
    # padding a request with junk keys until it hashes differently. GET
    # /table serves these keys with the row, so a client can offer them
    # without knowing the table; every one of them needs an entry in
    # CONFIG_KEY_SPECS above to say what it is.
    config_keys: tuple = ()
    # Which remote backends this action reaches. A gateway can be brought
    # up with the ledger and the analyzer working before the builder or
    # the oracle are reachable, and an action that needs one which isn't
    # configured answers that it isn't rather than crashing. It is written
    # here so the answer is one sentence written once, rather than a
    # raise inside every branch of a dispatch.
    needs: tuple = ()

    @property
    def dispatchable(self) -> bool:
        """Whether this row is an action to run rather than a whole list.

        Every row but the one per phase that names the phase's entire
        requirement list has a component to dispatch to. Readers ask this
        rather than each spelling out what a missing component means.
        """
        return self.component is not None


def _action_rows(phase: str) -> tuple[ActionRow, ...]:
    return tuple(
        ActionRow(
            name=action.name, emits=action.emits,
            requires=tuple((name, SUBJECT_KIND_OF[name]) for name in action.requires),
            deterministic=action.deterministic, component=action.component,
            phase=action.phase, config_keys=action.config_keys, needs=action.needs,
        )
        for action in ACTIONS if action.phase == phase
    )


ACTION_TABLE = (
    *_action_rows(PORTING),
    ActionRow(
        "accept", (), tuple((r.predicate_type, r.subject_kind) for r in ACCEPTANCE_REQUIREMENTS),
        True, None, PORTING,
    ),
    *_action_rows(ONBOARDING),
    ActionRow(
        "onboarded", (), tuple((r.predicate_type, r.subject_kind) for r in ONBOARDING_REQUIREMENTS),
        True, None, ONBOARDING,
    ),
)


def subject_kind_of(predicate_type: str) -> str:
    """Which of a request's subjects a claim of this type is filed against."""
    return SUBJECT_KIND_OF.get(predicate_type, "tree")


def subjects_by_kind(*, tree: str, frozen: str, baseline_tree: str) -> dict:
    """One request's three subjects, under the names everything looks them up by.

    The hashes come from the caller, which is the one thing that knows
    what is current; naming them is this module's, because the names are
    what the rows are written in.
    """
    return {
        "tree": Subject(kind="tree", sha256=tree),
        "frozen": Subject(kind="frozen", sha256=frozen),
        # A baseline timing is a claim about the pristine baseline, so it
        # is filed against that tree rather than the candidate.
        "baseline_tree": Subject(kind="tree", sha256=baseline_tree),
    }


def _subject_kinds_are_declared() -> None:
    """No row and no requirement may name a subject a request cannot resolve.

    Checked when this module is imported, because a kind nobody can
    resolve is not a wrong answer at the point of use -- it is a lookup
    that fails in the middle of a request that has already run a check.
    """
    unknown = sorted({
        kind for kind in SUBJECT_KIND_OF.values() if kind not in REQUEST_SUBJECT_KINDS
    } | {
        kind for row in ACTION_TABLE for _, kind in row.requires
        if kind not in REQUEST_SUBJECT_KINDS
    })
    if unknown:
        raise RuntimeError(
            f"the action table names subject kinds {unknown}, which a request has no "
            f"subject for; it has {list(REQUEST_SUBJECT_KINDS)}"
        )


_subject_kinds_are_declared()


def rows_for(phase: str) -> tuple:
    """The rows a region in this phase may run, in the order they are listed."""
    return tuple(row for row in ACTION_TABLE if row.phase == phase)


def requires_for(row: ActionRow, manifest=None) -> tuple:
    """One row's preconditions for a particular code.

    Every row but one has the preconditions written on it. The `accept`
    row is the exception: what finishes a port depends on whether the code
    declares a module of invariants, so its list is read from the code's
    manifest rather than frozen into the table.
    """
    if row.name != ACCEPT:
        return row.requires
    return tuple(
        (req.predicate_type, req.subject_kind) for req in acceptance_requirements(manifest)
    )


def config_params(row: ActionRow) -> dict:
    """The type and wording of every config key this row accepts."""
    return {key: dict(CONFIG_KEY_SPECS[key]) for key in row.config_keys}


def check_config(row: ActionRow, config: dict) -> str | None:
    """What is wrong with this request's settings, or None if nothing is.

    Names and values are both checked, and both before the config is
    hashed. An unvalidated key would hash into duplicate detection and
    let an identical request look new; a "repeats" of "lots" would hash
    the same way and then fail deep inside a check, where the caller
    reads a traceback instead of which setting it got wrong.
    """
    unknown_keys = sorted(set(config) - set(row.config_keys))
    if unknown_keys:
        return (
            f"'{row.name}' does not accept config key(s) {unknown_keys}; "
            f"allowed: {sorted(row.config_keys)}"
        )
    for key in sorted(config):
        value = config[key]
        spec = CONFIG_KEY_SPECS[key]
        if spec["type"] == "integer" and (isinstance(value, bool) or not isinstance(value, int)):
            return f"config key '{key}' of '{row.name}' must be an integer; got {value!r}"
        minimum, maximum = spec.get("minimum"), spec.get("maximum")
        if minimum is not None and value < minimum:
            return (
                f"config key '{key}' of '{row.name}' must be at least {minimum}; got {value!r}"
            )
        if maximum is not None and value > maximum:
            return (
                f"config key '{key}' of '{row.name}' must be at most {maximum}; got {value!r}"
            )
    return None
