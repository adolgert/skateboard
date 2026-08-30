"""Times the code's own program twice and keeps the files it wrote.

Trust role: what this returns becomes a claim, and the capture set it
stores is the answer a ported program's whole-program run is later
compared against. Two things have to hold, and both are about the
program rather than about the clock. It has to finish inside the budget
its manifest declares and write every file that manifest declares --
otherwise there is no measurement and nothing to compare. And it has to
write the same files twice: a program whose output drifts from run to
run cannot be the reference for anything, and the drift is far easier to
see now, on the baseline, than later in a port's failing comparison.

What is timed is the code's own program at the size its manifest
declares -- the executable, its arguments, its environment, the files it
writes, and the budget are all manifest fields -- built the way the
baseline is built. The timings themselves are recorded for a reader;
nothing here judges them. How fast a port has to be is a question for
the port.

The last run's files are stored as a capture set of one case, whose
variables are the files themselves: `h.npy` is stored as the variable
`h`, so the program's outputs are compared later by the same comparator
that compares the region's.
"""
from __future__ import annotations

from equivalent.gateway.submit import attempt_id_for_strategy
from equivalent.ledger.capture_sets import PROGRAM_SET, program_arrays, store_program_set
from equivalent.ledger.store import LedgerStore
from equivalent.strategy.schema import Strategy

from . import tree_manifest
from .errors import ComponentError

# The manifest role of the program a timing run measures.
TIMING_ROLE = "timing"
# How many times the program is run. Two is what the question needs: one
# run to measure and a second to disagree with it.
REPEATS = 2


def _fail(problems: list, detail=None) -> dict:
    return {"verdict": "fail", "detail": {**(detail or {}), "problems": problems}}


def _drifted(first: dict, second: dict, declared) -> list:
    """The declared files the two runs did not write identically."""
    problems = []
    for path in declared:
        if first.get(path) != second.get(path):
            problems.append(
                f"the timing run wrote a different '{path}' the second time; a program "
                f"whose outputs drift from run to run cannot be a reference"
            )
    return problems


def check(store: LedgerStore, repo_dir, ref: str, region_id: str, tree_sha: str,
          baseline_strategy: Strategy, builder) -> dict:
    """Run the timing program twice and store what its last run wrote.

    Returns {"verdict": "pass" | "fail", "detail": {...}}: the two runs'
    wall-clock seconds, whether the GPU was to itself, the files the
    program declared, and the capture set the ledger now holds them
    under. Raises ComponentError if the builder could not be reached.
    """
    manifest = tree_manifest.manifest_of(repo_dir, ref)
    described = {"manifest_sha256": manifest.sha256}

    target = manifest.build.targets.get(TIMING_ROLE)
    if target is None:
        return _fail(
            [f"code '{manifest.name}' declares no '{TIMING_ROLE}' build target, so there "
             f"is no program to time"],
            described,
        )

    timing = manifest.timing
    attempt_id = attempt_id_for_strategy(region_id, tree_sha, baseline_strategy.name)
    try:
        resp = builder.time(
            attempt_id, target.executable, list(timing.args), dict(timing.env),
            list(timing.outputs), REPEATS, timing.budget_s,
        )
    except Exception as exc:
        raise ComponentError(f"builder /v1/time call failed: {exc}") from exc

    if not resp.get("ok"):
        # An exceeded budget and a declared file the program never wrote
        # both arrive this way, and the builder's own words say which.
        return {
            "verdict": "fail",
            "detail": {
                **described, "runs_s": resp.get("runs_s", []),
                "log_tail": resp.get("log_tail", ""),
            },
        }

    runs = resp.get("outputs", [])
    measured = {
        **described,
        "runs_s": resp.get("runs_s", []),
        "gpu_exclusive": resp.get("gpu_exclusive"),
        "outputs": list(timing.outputs),
    }
    if len(runs) < REPEATS:
        return _fail(
            [f"the builder reported {len(runs)} run(s) of collected files, and what is "
             f"being asked is whether {REPEATS} of them agree"],
            measured,
        )

    problems = _drifted(runs[0], runs[1], timing.outputs)
    arrays, unreadable = program_arrays(runs[-1], timing.outputs)
    problems.extend(unreadable.values())
    if problems:
        return _fail(problems, measured)

    subject = store_program_set(store, arrays)
    return {
        "verdict": "pass",
        "detail": {
            **measured,
            "datasets": {PROGRAM_SET: {"cases": 1, "capture_set": subject.sha256}},
        },
    }
