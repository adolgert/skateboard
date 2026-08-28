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

# The types whose comparison consults a tolerance band, and what a band
# has to say. This is the same rule the oracle applies to its own policy
# before it will start (services/oracle/app.py): a floating-point output
# compared with no band is a comparison nobody chose. It is stated in
# both places because the oracle cannot import this package -- and
# checking it here means the agent learns it during onboarding rather
# than from an oracle that refuses to come up.
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


def _fail(reason: str, detail=None) -> dict:
    return {"verdict": "fail", "detail": {**(detail or {}), "reason": reason}}


def _tolerance_problems(manifest) -> list:
    """Every floating-point output that has no usable tolerance band."""
    try:
        policy = json.loads(Path(manifest.tolerances).read_text())
    except (OSError, ValueError) as exc:
        return [f"the tolerance file does not read as JSON: {exc}"]
    if not isinstance(policy, dict) or not isinstance(policy.get("variables"), dict):
        return ["the tolerance file has no 'variables' map naming a band per output variable"]

    bands = policy["variables"]
    problems = []
    for variable in manifest.interface.outputs:
        if variable.dtype not in BANDED_DTYPES:
            continue
        band = bands.get(variable.name)
        if not isinstance(band, dict):
            problems.append(
                f"output '{variable.name}' is {variable.dtype} and so is compared within a "
                f"band, and the tolerance file has no entry for it"
            )
            continue
        absent = [field for field in BAND_FIELDS if field not in band]
        if absent:
            problems.append(f"the tolerance entry for output '{variable.name}' is missing {absent}")
    return problems


def _timing_problems(manifest) -> list:
    """Timing outputs the program comparator could not read."""
    return [
        f"timing output '{name}' does not end in '{TIMING_OUTPUT_SUFFIX}'; the timing run's "
        f"outputs are compared as arrays, like the region's"
        for name in manifest.timing.outputs
        if not name.endswith(TIMING_OUTPUT_SUFFIX)
    ]


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

        problems = _tolerance_problems(manifest) + _timing_problems(manifest)
        described = _described(manifest)

    if problems:
        return {"verdict": "fail", "detail": {**described, "problems": problems}}
    return {"verdict": "pass", "detail": described}
