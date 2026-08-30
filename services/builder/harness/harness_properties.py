"""What a code's own property module imports, inside the builder.

Trust role: this is the half of a property run that the harness owns. A
code's property module says what must be true; this file is what turns
"run the region on these inputs" into an invocation of the replay binary
the builder built, and what hands back the arrays that came out. If it
ran the wrong executable, or read an output an earlier invocation left
behind, a property would be passing or failing on a program nobody
submitted.

It is baked into the builder image, not sent with the code. A property
module is the code's own text and may be edited between submissions; this
file is not, so what "run the region" means cannot be redefined by the
thing under test.

Everything it needs is in the environment, set by the builder around the
pytest run:

===========================  ================================================
`HARNESS_REPLAY`             absolute path of the replay executable
`HARNESS_CASES`             directory of visible cases, in the dataset layout
`HARNESS_SCRATCH`            where one invocation's case directory is written
`HARNESS_SEED`               the seed this run was given, recorded in the claim
`HARNESS_MAX_EXAMPLES`       how many examples each property draws
===========================  ================================================

A property module therefore names no path, no binary, and no seed of its
own: it asks for the corpus, perturbs it, and calls `run_replay`.
"""
from __future__ import annotations

import itertools
import json
import os
import shutil
import subprocess
from pathlib import Path

import numpy as np
from hypothesis import HealthCheck
from hypothesis import settings as hypothesis_settings

REPLAY_VAR = "HARNESS_REPLAY"
CASES_VAR = "HARNESS_CASES"
SCRATCH_VAR = "HARNESS_SCRATCH"
SEED_VAR = "HARNESS_SEED"
MAX_EXAMPLES_VAR = "HARNESS_MAX_EXAMPLES"

# The capture format on disk, the same spelling the rest of the harness
# uses: one file per variable, `<name>.npy` going in and `<name>.out.npy`
# coming out, with `case.json` naming the two sets and `cases.json` naming
# the cases.
INPUT_SUFFIX = ".npy"
OUTPUT_SUFFIX = ".out.npy"
CASE_FILE = "case.json"
CASES_FILE = "cases.json"

# One invocation of a replay driver is one call of one region, which is
# short. A property run makes thousands of them, so a driver that hangs
# has to end rather than hold the whole run.
REPLAY_TIMEOUT_S = 120

# Keeps each invocation's directory unique within a run.
_invocations = itertools.count()


class PropertyHarnessError(RuntimeError):
    """The run could not be made, as opposed to a property being false."""


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise PropertyHarnessError(
            f"{name} is not set; a property module runs inside the builder, which sets it"
        )
    return value


def seed() -> int:
    """The seed this run was given, which the claim records.

    A property module that draws anything of its own outside Hypothesis
    should draw it from here, so that a failing run can be repeated by
    asking for the same seed.
    """
    return int(_required(SEED_VAR))


def max_examples() -> int:
    """How many examples each property is asked to draw."""
    return int(_required(MAX_EXAMPLES_VAR))


def settings(**overrides):
    """The Hypothesis settings every property in a code's module should share.

    How many examples comes from the run rather than from the module, so
    a person can ask for a long search without editing the code's tree.
    There is no deadline and the too-slow health check is off because
    every example here starts a process: a property that runs a real
    binary is slow by construction, and Hypothesis's defaults are written
    for pure functions.
    """
    options = {
        "max_examples": max_examples(),
        "deadline": None,
        "suppress_health_check": [HealthCheck.too_slow],
    }
    options.update(overrides)
    return hypothesis_settings(**options)


def _scratch() -> Path:
    directory = Path(_required(SCRATCH_VAR))
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def corpus() -> list:
    """The visible cases' inputs, as arrays: [{variable: ndarray}, ...].

    This is what a property module draws from. Perturbing a captured case
    keeps the drawn inputs near the data the code was captured on, which
    is where its own numerics were calibrated; inventing arrays from
    nothing tends to ask the region about states it never sees.
    """
    directory = Path(_required(CASES_VAR))
    names = json.loads((directory / CASES_FILE).read_text())["cases"]
    cases = []
    for name in names:
        case_dir = directory / name
        listed = json.loads((case_dir / CASE_FILE).read_text())["inputs"]
        cases.append({
            variable: np.load(case_dir / f"{variable}{INPUT_SUFFIX}", allow_pickle=False)
            for variable in listed
        })
    return cases


def run_replay(inputs: dict) -> dict:
    """Run the region once on these arrays and return the arrays it wrote.

    `inputs` is {variable: ndarray}, and what comes back is every
    `<name>.out.npy` the driver left, read as arrays. The case directory
    is made fresh for each invocation, so no output of an earlier one can
    be read as this one's; it is removed afterwards, because a long run
    makes many thousands of them, and kept when the driver failed, which
    is the one directory worth having on disk.
    """
    replay = _required(REPLAY_VAR)
    case_dir = _scratch() / f"case_{next(_invocations):08d}"
    case_dir.mkdir(parents=True, exist_ok=True)
    for variable, array in inputs.items():
        np.save(case_dir / f"{variable}{INPUT_SUFFIX}", np.asarray(array), allow_pickle=False)

    try:
        finished = subprocess.run(
            [replay, str(case_dir)], capture_output=True, text=True, timeout=REPLAY_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        raise PropertyHarnessError(
            f"the replay driver did not finish within {REPLAY_TIMEOUT_S} seconds on {case_dir}"
        ) from None
    if finished.returncode != 0:
        raise PropertyHarnessError(
            f"the replay driver exited {finished.returncode} on {case_dir}: "
            f"{(finished.stdout + finished.stderr)[-1000:]}"
        )

    outputs = {
        path.name[: -len(OUTPUT_SUFFIX)]: np.load(path, allow_pickle=False)
        for path in sorted(case_dir.glob(f"*{OUTPUT_SUFFIX}"))
    }
    shutil.rmtree(case_dir, ignore_errors=True)
    return outputs
