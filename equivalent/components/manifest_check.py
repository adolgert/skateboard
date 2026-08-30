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

import json
import tempfile
from pathlib import Path

import yaml

from equivalent.gateway.submit import materialize_tree
from equivalent.manifest.schema import IN_TREE_MANIFEST, load_tree_manifest

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

# The two band maps a policy holds: one per region output variable, and
# one per file the timing run writes. They are separate because they band
# separate measurements -- one call of the region, and a whole run of the
# program -- and a band calibrated for one says nothing about the other.
VARIABLE_BANDS = "variables"
FILE_BANDS = "files"

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


def _fail(reason: str, detail=None) -> dict:
    return {"verdict": "fail", "detail": {**(detail or {}), "reason": reason}}


def _policy(manifest) -> tuple:
    """The policy's band per variable and per file, or why there are none to read.

    Returns (variables, files, problems); the two maps are empty when
    there are problems. A region variable is banded under `variables` and
    a file the timing run writes under `files`. They are two maps because
    they band two different measurements: one call of the region, and a
    whole run of the program, which accumulates whatever two compilations
    disagree about over every step it takes.
    """
    try:
        policy = json.loads(Path(manifest.tolerances).read_text())
    except (OSError, ValueError) as exc:
        return {}, {}, [f"the tolerance file does not read as JSON: {exc}"]
    if not isinstance(policy, dict):
        return {}, {}, ["the tolerance file is not a policy"]
    problems = [
        f"the tolerance file has no '{section}' map naming a band per {what}"
        for section, what in ((VARIABLE_BANDS, "output variable"), (FILE_BANDS, "timing output"))
        if not isinstance(policy.get(section), dict)
    ]
    if problems:
        return {}, {}, problems
    return policy[VARIABLE_BANDS], policy[FILE_BANDS], []


def _band_problems(bands: dict, name: str, because: str) -> list:
    """Whether this name has a band that says all three numbers."""
    band = bands.get(name)
    if not isinstance(band, dict):
        return [f"{because}, and the tolerance file has no entry for '{name}'"]
    absent = [field for field in BAND_FIELDS if field not in band]
    return [f"the tolerance entry for '{name}' is missing {absent}"] if absent else []


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


def check(repo_dir, ref: str) -> dict:
    """Read the tree's own manifest and judge it.

    Returns {"verdict": "pass" | "fail", "detail": {...}}. The detail of a
    pass is what the manifest says the code is -- its hash, its name, the
    targets it builds, the variables the region carries, and the datasets
    it declares -- so a person reviewing the ledger reads the description
    that every later claim about this tree was filed under.
    """
    with tempfile.TemporaryDirectory() as scratch:
        materialize_tree(repo_dir, ref, scratch)
        try:
            manifest = load_tree_manifest(scratch)
        except FileNotFoundError:
            return _fail(f"the tree holds no manifest at {IN_TREE_MANIFEST}")
        except (OSError, ValueError, yaml.YAMLError) as exc:
            return _fail(_in_tree_words(str(exc), scratch))

        if not manifest.complete:
            return _fail(
                f"the manifest at {IN_TREE_MANIFEST} still lacks {manifest.missing_parts()}; "
                f"a code is checked against a manifest that says all of it",
                {"manifest_sha256": manifest.sha256, "name": manifest.name},
            )

        variables, files, problems = _policy(manifest)
        if not problems:
            problems = _tolerance_problems(manifest, variables) + _timing_problems(manifest, files)
        described = _described(manifest)

    if problems:
        return {"verdict": "fail", "detail": {**described, "problems": problems}}
    return {"verdict": "pass", "detail": described}
