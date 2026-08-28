"""Stand-ins the component and dispatch tests build a gateway out of.

Unlike check_sese.py (cheap, pure Python, safe to run for real in
tests), the builder needs nvfortran/compute-sanitizer/a GPU and the
oracle needs its baked capture data -- none of which exist in this
development environment. These fakes match the real services' response
shapes (services/builder/app.py, services/oracle/app.py) exactly, so the
gateway dispatch code under test is exercised the same way it would be
against the real thing; only what's inside the box differs.

`write_program` is not a fake: it writes a small but real code directory,
laid out the way `programs/<code>/` is, so every test that needs a
manifest gets one from the same place. A test that copied its own
manifest would keep passing after the schema changed under it.

The fixture code's variables are deliberately not the worked example's:
two variables of different element types and different ranks, so a test
that passed only because everything was one rank-1 float array would
fail here.
"""
from __future__ import annotations

import base64
import json
from pathlib import Path

import numpy as np
import yaml

from equivalent.capture import npy

# The region interface the fixture code declares, and the shape each
# variable has in its dataset. Everything the fakes hand back is built
# from these, so the fixture's arrays always match its own manifest.
FIXTURE_VARIABLES = (
    {"name": "field", "dtype": "f32", "rank": 1},
    {"name": "flux", "dtype": "f64", "rank": 2},
)
FIXTURE_SHAPES = {"field": (4,), "flux": (2, 3)}

# A code small enough to read here, with every field the manifest schema
# requires. The visible dataset holds one case, which is what the
# dispatch tests replay.
PROGRAM_MANIFEST = {
    "version": 1,
    "name": None,  # filled in with the code's own name
    "source": {"root": "baseline", "patterns": ["**/*.f90", "Makefile"]},
    "build": {
        "makefile": "Makefile",
        "targets": {
            "replay": {"target": "replay", "executable": "replay"},
            "timing": {"target": "timing", "executable": "whole_program"},
        },
    },
    "interface": {
        "module": "mod_kernel",
        "entry": "step",
        "inputs": [dict(v) for v in FIXTURE_VARIABLES],
        "outputs": [dict(v) for v in FIXTURE_VARIABLES],
    },
    "datasets": {
        "visible": {"args": ["100", "5000", "25", "0.02"]},
        "holdout": {"args": ["100", "5000", "60", "0.01"]},
    },
    "timing": {"args": [], "outputs": [], "budget_s": 300},
    "tolerances": "tolerances.json",
    "properties": None,
}

# Bands wide enough that the fixture's arrays compare equal to themselves
# under any of the three metrics.
PROGRAM_TOLERANCES = {
    "policy_version": "fixture-v1",
    "variables": {v["name"]: {"abs": 1e-6, "rel": 1e-5, "ulp": 16} for v in FIXTURE_VARIABLES},
}

VISIBLE_CASE = "case0000"


def fixture_arrays(offset: int = 0) -> dict:
    """One array per fixture variable, of the type and rank it declares."""
    arrays = {}
    for variable in FIXTURE_VARIABLES:
        name = variable["name"]
        shape = FIXTURE_SHAPES[name]
        n = int(np.prod(shape, dtype=int))
        values = np.arange(offset, offset + n)
        arrays[name] = np.asarray(
            values, dtype=npy.NUMPY_DTYPE[variable["dtype"]]
        ).reshape(shape, order="F")
    return arrays


def fixture_case(offset: int = 0) -> dict:
    """One case as it travels on the wire: {variable: base64 of its .npy file}."""
    return {
        name: base64.b64encode(npy.encode(array)).decode()
        for name, array in fixture_arrays(offset).items()
    }


def write_program(root, name: str = "tsunami") -> Path:
    """Write `<root>/programs/<name>/` and return that code's directory.

    The programs directory a deployment mounts is its parent, and the
    manifest is `manifest.yaml` inside it -- so a caller needs no third
    thing told to it.
    """
    directory = Path(root) / "programs" / name
    (directory / "baseline" / "src").mkdir(parents=True, exist_ok=True)
    (directory / "baseline" / "src" / "mod_kernel.f90").write_text(
        "module mod_kernel\ncontains\nsubroutine step\nend subroutine\nend module\n"
    )
    (directory / "baseline" / "Makefile").write_text("replay:\n\techo build\n")
    (directory / "tolerances.json").write_text(json.dumps(PROGRAM_TOLERANCES, indent=2))

    visible = directory / "datasets" / "visible"
    npy.write_case(visible / VISIBLE_CASE, fixture_arrays(), {})
    (visible / npy.CASES_FILE).write_text(json.dumps({"cases": [VISIBLE_CASE]}))

    manifest = {**PROGRAM_MANIFEST, "name": name}
    (directory / "manifest.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))
    return directory


class FakeBuilder:
    def __init__(self):
        self.build_calls = []
        self.run_calls = []
        self.sanitize_calls = []
        self.time_calls = []
        self.build_ok = True
        # The two statements the real builder reads out of its compiler
        # log. A test that wants a makefile which ignored the flags, or
        # one which compiled something from outside the tree, turns the
        # matching one off.
        self.flags_reached = True
        self.only_tree_source = True
        self.outside_file = "../elsewhere/sneak.f90"
        self.compiled_file = "src/mod_kernel.f90"
        self.run_ok = True
        self.run_kernels = 4
        self.run_launches = [["src/mod_kernel.f90", "step", "42"]]
        # What one replay writes back. A test that wants an output missing
        # or of the wrong type replaces this.
        self.run_outputs = fixture_case()
        self.sanitize_ok = True
        self.time_ok = True
        self.runs_s = [0.21, 0.20, 0.22]
        # The executable names the real builder reports on, all present.
        # A test that wants a builder missing something drops a key here.
        self.tools = {
            name: True for name in
            ("nvfortran", "compute-sanitizer", "nsys", "make", "cmake", "fpm", "gfortran")
        }

    def healthz(self):
        return {"ok": True, "tools": dict(self.tools)}

    def build(self, attempt_id, tree, makefile, targets, compiler, flags, link_flags,
              source_patterns):
        self.build_calls.append({
            "attempt_id": attempt_id, "tree": tree, "makefile": makefile,
            "targets": targets, "compiler": compiler, "flags": flags,
            "link_flags": link_flags, "source_patterns": source_patterns,
        })
        # One compiler command line, shaped the way the real shim log
        # reads once contract.py has been through it.
        record = {
            "argv": [*flags, "-o", targets[0]["executable"], self.compiled_file],
            "cwd": ".",
            "inputs": [self.compiled_file],
            "output": targets[0]["executable"],
            "has_flags": self.flags_reached,
            "outside": [] if self.only_tree_source else [self.outside_file],
        }
        result = {
            "stage": "build",
            "targets": {
                t["role"]: {"executable": t["executable"], "built": self.build_ok}
                for t in targets
            },
            "compiles": [record],
            "flags": list(flags),
            "link_flags": list(link_flags),
            "flags_reached_every_compile": self.flags_reached,
            "compiled_only_tree_source": self.only_tree_source,
            "minfo_excerpt": "Generating Tesla code",
        }
        if not self.build_ok:
            return {**result, "ok": False, "log_tail": "compile error"}
        return {**result, "ok": True, "log_tail": ""}

    def run(self, attempt_id, executable, cases, notify=None, mandatory=False):
        self.run_calls.append({
            "attempt_id": attempt_id, "executable": executable, "cases": cases,
            "notify": notify, "mandatory": mandatory,
        })
        if not self.run_ok:
            return {"ok": False, "stage": "run", "log_tail": "runtime crash"}
        outputs = {name: dict(self.run_outputs) for name in cases}
        return {"ok": True, "stage": "run", "outputs": outputs, "kernels_launched": self.run_kernels,
                "launches": self.run_launches, "log_tail": ""}

    def sanitize(self, attempt_id, executable, cases, tools):
        self.sanitize_calls.append({
            "attempt_id": attempt_id, "executable": executable, "cases": cases, "tools": tools,
        })
        per_tool = {t: {"ok": self.sanitize_ok, "errors": 0 if self.sanitize_ok else 3, "log_tail": ""} for t in tools}
        return {"ok": self.sanitize_ok, "stage": "sanitize", "per_tool": per_tool}

    def time(self, attempt_id, executable, args, env, outputs, repeats=5, budget_s=300):
        self.time_calls.append({
            "attempt_id": attempt_id, "executable": executable, "args": args, "env": env,
            "outputs": outputs, "repeats": repeats, "budget_s": budget_s,
        })
        if not self.time_ok:
            return {"ok": False, "stage": "time", "log_tail": "timing binary not built"}
        return {
            "ok": True, "stage": "time", "runs_s": self.runs_s, "gpu_exclusive": True,
            "outputs": {
                name: base64.b64encode(f"{name} from the timing run".encode()).decode()
                for name in outputs
            },
            "stdout_tail": "",
        }


class FakeOracle:
    def __init__(self):
        self.compare_calls = []
        self.visible_verdict = "pass"
        self.holdout_verdict = "pass"

    def policy(self):
        return {"policy_version": "1", "policy_sha256": "policyabc"}

    def holdout_inputs(self):
        return {"dataset": "holdout", "cases": {"hcase0": fixture_case(offset=7)}}

    def compare(self, dataset, outputs, attempt_id="unknown"):
        self.compare_calls.append({"dataset": dataset, "outputs": outputs, "attempt_id": attempt_id})
        verdict = self.visible_verdict if dataset == "visible" else self.holdout_verdict
        resp = {"verdict": verdict, "dataset": dataset, "policy_sha256": "policyabc"}
        if dataset == "visible":
            resp["per_case"] = {name: {"pass": verdict == "pass"} for name in outputs}
        return resp
