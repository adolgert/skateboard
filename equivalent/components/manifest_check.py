"""Reads the manifest a submitted tree carries and says whether it describes the code.

Trust role: what this returns becomes a claim, and every later check of
that tree reads the file this one approved. If it passed a manifest that
named a driver the tree does not hold, or an output nobody chose a
tolerance band for, the checks after it would measure something other
than what the person reviewing the claim believes.

The manifest is the agent's own submission, so a manifest that is
missing, unreadable, incomplete, or self-contradictory is a `fail` with
the reason in its detail -- not an error. An error would say the harness
could not do its job; here the harness did its job and the answer was no.

Nothing outside the tree is read and nothing is executed: the tree is
written to a scratch directory of the gateway's own, and a YAML load and
a JSON load are the whole of the work.
"""
from __future__ import annotations

import math
from numbers import Integral, Real
from pathlib import Path

import yaml

from equivalent.ledger.vocabulary import PASS
from equivalent.manifest.schema import IN_TREE_MANIFEST, load_tree_manifest

from .context import CheckContext
from .result import CheckResult, failed
from .names import bands

# The declared types whose comparison consults a tolerance band, and what
# a band has to say. This is the same rule the oracle applies to its own
# policy before it will start (services/oracle/app.py): a floating-point
# output compared with no band is a comparison nobody chose. It is stated
# in both places because the oracle cannot import this package -- and
# checking it here means the agent learns it during onboarding rather
# than from an oracle that refuses to come up. A timing output is not on
# this list: nothing declares what type such a file holds, so every one
# of them needs a band.
BANDED_DTYPES = ("f32", "f64")
BAND_FIELDS = ("abs", "rel", "ulp")

# What the timing run may write. The program's outputs are compared with
# the same comparator as the region's, which reads arrays and nothing
# else, so a code whose program prints text needs a timing driver that
# writes arrays instead.
TIMING_OUTPUT_SUFFIX = ".npy"


def _in_tree_words(message: str, scratch) -> str:
    """The same message with the scratch directory taken off the front of paths.

    A verdict the agent reads should name files the way the agent's own
    tree does; where the gateway happened to unpack the tree is noise.
    """
    return message.replace(f"{scratch}/", "").replace(str(scratch), "the tree")


def _policy(manifest) -> tuple:
    """The policy's band per variable and per file, or why there are none to read.

    Returns (variables, files, problems); the two maps are empty when
    there are problems. A region variable is banded under `variables` and
    a file the timing run writes under `files`. They are two maps because
    they band two different measurements: one call of the region, and a
    whole run of the program, which accumulates whatever two compilations
    disagree about over every step it takes.

    The file is read here the way every later check reads it, so a policy
    this check passed is one they can all read. Failing to read it is a
    verdict about the code, because the file is the agent's own.
    """
    try:
        return (*bands(Path(manifest.tolerances).read_bytes()), [])
    except (OSError, ValueError) as exc:
        return {}, {}, [str(exc)]


def _band_problems(bands: dict, name: str, because: str) -> list:
    """Whether this name has a band that says all three numbers."""
    band = bands.get(name)
    if not isinstance(band, dict):
        return [f"{because}, and the tolerance file has no entry for '{name}'"]
    absent = [field for field in BAND_FIELDS if field not in band]
    if absent:
        return [f"the tolerance entry for '{name}' is missing {absent}"]
    problems = []
    for field in ("abs", "rel"):
        value = band[field]
        if (isinstance(value, bool) or not isinstance(value, Real)
                or not math.isfinite(value) or value < 0):
            problems.append(
                f"the tolerance entry for '{name}' has invalid {field}: "
                "it must be a finite nonnegative real number"
            )
    ulp = band["ulp"]
    if isinstance(ulp, bool) or not isinstance(ulp, Integral) or ulp < 0:
        problems.append(
            f"the tolerance entry for '{name}' has invalid ulp: "
            "it must be a nonnegative integer"
        )
    return problems


def _tolerance_problems(manifest, bands: dict) -> list:
    """Every floating-point region output that has no usable tolerance band."""
    problems = []
    for variable in manifest.interface.outputs:
        if variable.dtype in BANDED_DTYPES:
            problems.extend(_band_problems(
                bands, variable.name,
                f"output '{variable.name}' is {variable.dtype} and so is compared within a band",
            ))
    return problems


def _timing_problems(manifest, bands: dict) -> list:
    """Every timing output the program comparator could not read or could not judge.

    A timing output is a file, not a declared variable, so nothing says
    what type it holds until it is read -- and every one of them therefore
    needs a band under `files`, where a region output needs one under
    `variables` only if it is floating-point. The band is looked up under
    the path the manifest declares, so a code may band the file its
    program writes differently from a region variable of the same name.
    """
    problems = []
    for path in manifest.timing.outputs:
        if not path.endswith(TIMING_OUTPUT_SUFFIX):
            problems.append(
                f"timing output '{path}' does not end in '{TIMING_OUTPUT_SUFFIX}'; the timing "
                f"run's outputs are compared as arrays, like the region's"
            )
            continue
        problems.extend(_band_problems(
            bands, path,
            f"the timing run's '{path}' is compared against the baseline program's and "
            f"nothing declares what it holds",
        ))
    return problems


def _described(manifest) -> dict:
    """What the tree says about itself, for the person reading the claim."""
    return {
        "manifest_sha256": manifest.sha256,
        "name": manifest.name,
        "targets": {
            role: target.executable for role, target in sorted(manifest.build.targets.items())
        },
        "inputs": [variable.name for variable in manifest.interface.inputs],
        "outputs": [variable.name for variable in manifest.interface.outputs],
        "datasets": sorted(manifest.datasets),
    }


def check(ctx: CheckContext, config: dict) -> CheckResult:
    """Read the tree's own manifest and judge it.

    The detail of a pass is what the manifest says the code is -- its hash, its name, the
    targets it builds, the variables the region carries, and the datasets
    it declares -- so a person reviewing the ledger reads the description
    that every later claim about this tree was filed under.
    """
    scratch = ctx.tree.directory
    try:
        manifest = load_tree_manifest(scratch)
    except FileNotFoundError:
        reason = f"the tree holds no manifest at {IN_TREE_MANIFEST}"
        return failed({"reason": reason}, [reason])
    except (OSError, ValueError, yaml.YAMLError) as exc:
        reason = _in_tree_words(str(exc), scratch)
        return failed({"reason": reason}, [reason])

    if not manifest.complete:
        reason = (f"the manifest at {IN_TREE_MANIFEST} still lacks "
                  f"{manifest.missing_parts()}; a code is checked against a manifest "
                  f"that says all of it")
        return failed(
            {"manifest_sha256": manifest.sha256, "name": manifest.name,
             "reason": reason},
            [reason],
        )

    variables, files, problems = _policy(manifest)
    if not problems:
        problems = _tolerance_problems(manifest, variables) + _timing_problems(manifest, files)
    described = _described(manifest)

    if problems:
        return failed({**described, "problems": problems}, problems)
    return CheckResult(verdict=PASS, detail=described)
