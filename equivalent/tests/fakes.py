"""Stand-ins the component and dispatch tests build a gateway out of.

Unlike check_sese.py (cheap, pure Python, safe to run for real in
tests), the builder needs nvfortran/compute-sanitizer/a GPU and the
oracle needs its baked capture data -- none of which exist in this
development environment. The builder's fake answers with the same typed
responses the real client parses (equivalent/gateway/backend_client.py),
and equivalent/tests/test_builder_parity.py is what says those are still
the shapes the service writes -- so the gateway dispatch code under test
is exercised the same way it would be against the real thing; only
what's inside the box differs.

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
import hashlib
import json
from pathlib import Path

import numpy as np
import yaml

from equivalent.capture import npy
from equivalent.gateway.backend_client import (
    ArtifactsResponse,
    BuildResponse,
    CaptureResponse,
    HealthResponse,
    MutateResponse,
    PropertiesResponse,
    RunResponse,
    SanitizeResponse,
    TimeResponse,
)
from equivalent.ledger.capture_sets import pack_capture_set, pack_program_set
from equivalent.manifest.schema import IN_TREE_MANIFEST

# The region interface the fixture code declares, and the shape each
# variable has in its dataset. Everything the fakes hand back is built
# from these, so the fixture's arrays always match its own manifest.
FIXTURE_VARIABLES = (
    {"name": "field", "dtype": "f32", "rank": 1},
    {"name": "flux", "dtype": "f64", "rank": 2},
)
FIXTURE_SHAPES = {"field": (4,), "flux": (2, 3)}

# Where the tolerance policy sits inside the source tree. Every path a
# manifest names other than the source root is read from there, so the
# fixture keeps its policy where a real code keeps one.
TOLERANCES_IN_TREE = "harness/tolerances.json"
# And where a code that has one keeps its property module. The fixture
# writes one only when a test asks for it, because a code declaring no
# invariants is the ordinary case.
PROPERTIES_IN_TREE = "harness/properties.py"

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
            "capture": {"target": "capture", "executable": "gen_reference"},
        },
    },
    "interface": {
        "module": "mod_kernel",
        "entry": "step",
        "files": ["src/mod_kernel.f90"],
        "inputs": [dict(v) for v in FIXTURE_VARIABLES],
        "outputs": [dict(v) for v in FIXTURE_VARIABLES],
    },
    "datasets": {
        "visible": {"args": ["100", "5000", "25", "0.02"]},
        "holdout": {"args": ["100", "5000", "60", "0.01"]},
    },
    # The timing run writes two arrays, one of them in a directory of its
    # own, because a program is free to write wherever it likes and the
    # files it writes become the names of a capture set's variables.
    "timing": {"args": [], "outputs": ["field.npy", "results/flux.npy"], "budget_s": 300},
    "tolerances": TOLERANCES_IN_TREE,
    "properties": None,
}

# The fixture code's property module. It is never executed in these tests --
# only the builder runs one -- but the manifest loader insists every path a
# manifest names is a file, so there has to be something here, and what is
# here should read like the real thing.
FIXTURE_PROPERTIES = '''"""Invariants of the fixture code's region, run by the builder."""
import harness_properties as harness


def test_the_same_inputs_give_the_same_outputs():
    for inputs in harness.corpus():
        first = harness.run_replay(inputs)
        second = harness.run_replay(inputs)
        assert set(first) == set(second)
'''

# A band wide enough that the fixture's arrays compare equal to themselves
# under any of the three metrics.
FIXTURE_BAND = {"abs": 1e-6, "rel": 1e-5, "ulp": 16}

# What is compared under a band, in the policy's two maps: the region's
# output variables, and the files the timing program writes, keyed by the
# paths the manifest declares. A code bands the two separately because
# one call of a region and a whole run of the program are different
# measurements.
PROGRAM_TOLERANCES = {
    "policy_version": "fixture-v1",
    "variables": {v["name"]: dict(FIXTURE_BAND) for v in FIXTURE_VARIABLES},
    "files": {path: dict(FIXTURE_BAND) for path in PROGRAM_MANIFEST["timing"]["outputs"]},
}

VISIBLE_CASE = "case0000"

# How many cases one run of the fixture's capture program writes.
CAPTURED_CASES = 2


def keep_capture_set(store, name: str, cases: dict) -> str:
    """Pack a set and file it, the way the gateway files what a check packed."""
    packed = pack_capture_set(name, cases)
    store.keep(packed)
    return packed.sha256


def keep_program_set(store, arrays: dict) -> str:
    """The same for a timing run's own outputs."""
    packed = pack_program_set(arrays)
    store.keep(packed)
    return packed.sha256


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


def stepped(arrays: dict) -> dict:
    """What the fixture's region does to its inputs: one step on each array.

    The fixture's capture program and its replay driver both do this, so a
    replay of a captured case reproduces the captured outputs exactly --
    which is what the replay check is asking about.
    """
    return {name: array + 1 for name, array in arrays.items()}


def encode_case(arrays: dict) -> dict:
    """One case's arrays as they travel: {variable: base64 of its .npy file}."""
    return {
        name: base64.b64encode(npy.encode(array)).decode() for name, array in arrays.items()
    }


def decode_case(encoded: dict) -> dict:
    return {name: npy.decode(base64.b64decode(data)) for name, data in encoded.items()}


def fixture_case(offset: int = 0) -> dict:
    """The fixture's own arrays as one case travels on the wire."""
    return encode_case(fixture_arrays(offset))


def captured_cases(args) -> dict:
    """The dataset the fixture's capture program writes for one set of arguments.

    Different arguments make a different run, the way a real capture
    program's do, so a visible and a held-out dataset differ; the same
    arguments make the same bytes, so capturing twice is capturing the
    same set.
    """
    seed = int(hashlib.sha256(" ".join(args).encode()).hexdigest()[:6], 16) % 1000
    cases = {}
    for i in range(CAPTURED_CASES):
        inputs = fixture_arrays(seed + i)
        cases[f"case{i:04d}"] = {
            "inputs": encode_case(inputs), "outputs": encode_case(stepped(inputs)),
        }
    return cases


def mutant_row(mid: str, status: str, *, line: int = 7, op: str = "AOR", note: str = "") -> dict:
    """One scored mutant, shaped the way the builder's mutation stage reports one."""
    return {
        "id": mid, "file": "src/mod_kernel.f90", "line": line, "op": op,
        "original": "    a = a * 2.0d0", "mutated": "    a = a / 2.0d0",
        "status": status, "note": note,
    }


def timing_array(name: str):
    """The array the fixture's timing program writes into one declared file."""
    seed = int(hashlib.sha256(name.encode()).hexdigest()[:6], 16) % 1000
    return np.asarray([seed, seed + 1, seed + 2], dtype="<f8")


def program_tolerances(directory) -> Path:
    """The tolerance policy of a code written by `write_program`.

    A manifest's tolerance path is relative to its source tree, so the
    file is inside `baseline/`, not beside the manifest.
    """
    return Path(directory) / "baseline" / TOLERANCES_IN_TREE


def write_program(root, name: str = "tsunami", *, minimal: bool = False,
                  properties: bool = False) -> Path:
    """Write `<root>/programs/<name>/` and return that code's directory.

    The programs directory a deployment mounts is its parent, and the
    manifest is `manifest.yaml` inside it -- so a caller needs no third
    thing told to it.

    `minimal` writes the form a code starts in: the tree and its name,
    and none of what onboarding produces. `properties` writes a code that
    declares a property module, which is what makes the property check
    part of what its ports are judged by.
    """
    directory = Path(root) / "programs" / name
    (directory / "baseline" / "src").mkdir(parents=True, exist_ok=True)
    (directory / "baseline" / "src" / "mod_kernel.f90").write_text(
        "module mod_kernel\ncontains\nsubroutine step\nend subroutine\nend module\n"
    )
    (directory / "baseline" / "Makefile").write_text("replay:\n\techo build\n")
    tolerances = directory / "baseline" / TOLERANCES_IN_TREE
    tolerances.parent.mkdir(parents=True, exist_ok=True)
    tolerances.write_text(json.dumps(PROGRAM_TOLERANCES, indent=2))

    visible = directory / "datasets" / "visible"
    npy.write_case(visible / VISIBLE_CASE, fixture_arrays(), {})
    (visible / npy.CASES_FILE).write_text(json.dumps({"cases": [VISIBLE_CASE]}))

    manifest = {**PROGRAM_MANIFEST, "name": name}
    if properties:
        (directory / "baseline" / PROPERTIES_IN_TREE).write_text(FIXTURE_PROPERTIES)
        manifest["properties"] = PROPERTIES_IN_TREE
    if minimal:
        manifest = {key: manifest[key] for key in ("version", "name", "source")}
    (directory / "manifest.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))
    return directory


def in_tree_manifest(name: str = "tsunami", **overrides) -> dict:
    """The fixture manifest as a tree carries it while the code is being brought in.

    The only difference from the promoted form is where it says its
    source is: inside the tree, the tree itself.
    """
    manifest = {
        **PROGRAM_MANIFEST,
        "name": name,
        "source": {"root": ".", "patterns": PROGRAM_MANIFEST["source"]["patterns"]},
    }
    return {**manifest, **overrides}


def write_tree(root, manifest: dict | None = None, *, properties: bool = False) -> Path:
    """A tree of the shape an onboarding session submits, and returns it.

    It holds what such a tree holds: the code, the makefile that builds
    it, the tolerance policy, and the manifest that names all three.
    Passing `manifest` writes a different one, which is how a test asks
    what happens when the tree describes itself wrongly. `properties`
    writes a tree whose code states invariants of its own, which is what
    makes the property check something to run rather than to record the
    absence of.
    """
    root = Path(root)
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "src" / "mod_kernel.f90").write_text(
        "module mod_kernel\ncontains\nsubroutine step\nend subroutine\nend module\n"
    )
    (root / "Makefile").write_text("replay:\n\techo build\n")
    tolerances = root / TOLERANCES_IN_TREE
    tolerances.parent.mkdir(parents=True, exist_ok=True)
    tolerances.write_text(json.dumps(PROGRAM_TOLERANCES, indent=2))
    if manifest is None:
        manifest = in_tree_manifest(
            **({"properties": PROPERTIES_IN_TREE} if properties else {})
        )
    if properties:
        (root / PROPERTIES_IN_TREE).write_text(FIXTURE_PROPERTIES)
    (root / IN_TREE_MANIFEST).write_text(yaml.safe_dump(manifest, sort_keys=False))
    return root


# What a builder answers with when a test says nothing else. Every one of
# these is what the fixture code's own program would really do, so the
# golden path passes against defaults and a test only says how its
# builder differs.
EXECUTOR_IDENTITY = "e" * 64
IMAGE_ID = "sha256:" + "a" * 64
EXECUTABLE_IDENTITY = {
    "sha256": "b" * 64, "size": 12345, "role": "replay",
    "executor_identity": EXECUTOR_IDENTITY,
}
# The file the fixture's makefile compiles, and one that is not in its
# tree at all -- what a build that reached outside the tree names.
COMPILED_FILE = "src/mod_kernel.f90"
OUTSIDE_FILE = "../elsewhere/sneak.f90"
# Where the runtime said the kernels came from: file, function, line.
LAUNCHES = [["src/mod_kernel.f90", "step", "42"]]
# The wall-clock seconds a timed run reports, cycled for as many
# repetitions as were asked for.
RUN_SECONDS = (0.21, 0.20, 0.22)
# The executable names the real builder reports on, and the modules it
# reports beside them: a strategy asks for pytest as `python:pytest`, and
# an executable and an importable module are looked for in different ways.
TOOLS = ("nvfortran", "compute-sanitizer", "nsys", "make", "cmake", "fpm", "gfortran")
PYTHON_MODULES = ("pytest", "hypothesis", "numpy")


def run_seconds(repeats: int) -> list:
    """What a timed run of `repeats` repetitions reports for each of them."""
    return [RUN_SECONDS[i % len(RUN_SECONDS)] for i in range(repeats)]


def timing_files(paths, run: int) -> dict:
    """The declared files one timing run wrote: real arrays, as a program writes.

    Which run it is makes no difference here -- a program that writes
    something else the second time is the exception a test asks for by
    name -- but the run number is what such a test writes its own version
    of this against, so it is part of the shape.
    """
    return {
        path: base64.b64encode(npy.encode(timing_array(path))).decode() for path in paths
    }


def built(request: dict, *, sha256: str = EXECUTABLE_IDENTITY["sha256"],
          outside: str = OUTSIDE_FILE, **over) -> BuildResponse:
    """What the builder answers a /v1/build with, and what a test changes.

    The compiler record agrees with the two statements above it: a build
    said to have missed the strategy's flags carries a command line
    without them, because that is what a component reads to name the
    offending compile.
    """
    fields = {
        "ok": True, "flags_reached_every_compile": True, "compiled_only_tree_source": True,
        "flags": list(request["flags"]), "minfo_excerpt": "Generating Tesla code",
        "executor_identity": EXECUTOR_IDENTITY, "image_id": IMAGE_ID,
        "log_tail": "" if over.get("ok", True) else "compile error",
        **over,
    }
    fields.setdefault("targets", {
        t["role"]: {
            "executable": t["executable"], "built": fields["ok"],
            "sha256": sha256, "size": 12345,
        }
        for t in request["targets"]
    })
    # One compiler command line, shaped the way the shim log reads once
    # services/builder/compile_log.py has been through it.
    fields.setdefault("compiles", [{
        "argv": [*request["flags"], "-o", request["targets"][0]["executable"], COMPILED_FILE],
        "cwd": ".",
        "inputs": [COMPILED_FILE],
        "output": request["targets"][0]["executable"],
        "has_flags": fields["flags_reached_every_compile"],
        "outside": [] if fields["compiled_only_tree_source"] else [outside],
    }])
    return BuildResponse(**fields)


def replayed(request: dict, *, writes: dict | None = None, **over) -> RunResponse:
    """What the builder answers a /v1/run with.

    Every case comes back stepped the way the fixture's capture program
    stepped it, so a replay of a captured case reproduces the captured
    answers -- which is what a correct replay driver does. A test whose
    driver writes something else hands `writes` the arrays every case
    should come back with instead.
    """
    outputs = {
        name: dict(writes) if writes is not None else encode_case(stepped(decode_case(arrays)))
        for name, arrays in request["cases"].items()
    }
    return RunResponse(**{
        "ok": True, "outputs": outputs, "kernels_launched": 4, "launches": LAUNCHES,
        "executable_identity": dict(EXECUTABLE_IDENTITY), "log_tail": "",
        **over,
    })


def captured(request: dict, *, by_args: dict | None = None, **over) -> CaptureResponse:
    """What the builder answers a /v1/capture with.

    `by_args` gives a test the dataset a particular set of arguments
    captures; anything else captures the fixture program's own for those
    arguments, so two datasets differ exactly as two real ones would.
    """
    args = tuple(request["args"])
    cases = (by_args or {}).get(args) or captured_cases(list(args))
    return CaptureResponse(**{
        "ok": True, "cases": cases, "stdout_tail": "",
        "executable_identity": dict(EXECUTABLE_IDENTITY),
        **over,
    })


def sanitized(request: dict, *, per_tool: dict | None = None, **over) -> SanitizeResponse:
    """What the builder answers a /v1/sanitize with: one result per tool asked for."""
    ok = over.get("ok", True)
    if per_tool is None:
        per_tool = {
            tool: {"ok": ok, "errors": 0 if ok else 3, "log_tail": ""}
            for tool in request["tools"]
        }
    return SanitizeResponse(**{
        "ok": ok, "per_tool": per_tool,
        "executable_identity": dict(EXECUTABLE_IDENTITY),
        **over,
    })


def property_run(request: dict, **over) -> PropertiesResponse:
    """What the builder answers a /v1/properties with: pytest's own summary.

    The seed and the example count are echoed from the request, because a
    builder that answered with different ones is a failed claim rather
    than a passing one, and that is a test of its own.
    """
    return PropertiesResponse(**{
        "ok": True, "seed": request["seed"], "max_examples": request["max_examples"],
        "passed": 3, "collected": 3, "executed": 3, "replays_observed": 3,
        "counts_source": "pytest summary emitted by the submitted property process",
        "executable_identity": dict(EXECUTABLE_IDENTITY), "log_tail": "",
        **over,
    })


def mutated(request: dict, *, results=None, **over) -> MutateResponse:
    """What the builder answers a /v1/mutate with: one row per scored mutant.

    The rows are a harness that works: one mutant the bands caught, and
    one survivor on a line the captured inputs never reach. `generated`
    follows the rows unless a test names it, which is what a limited run
    leaves larger than the scored count.
    """
    if results is None:
        results = [
            mutant_row("m-0001", "KILLED"),
            mutant_row("m-0002", "EQUIVALENT", line=42, note="no output changed"),
        ]
    results = [dict(row) for row in results]
    counts = {}
    for row in results:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    return MutateResponse(**{
        "ok": True, "generated": len(results), "scored": len(results),
        "results": results, "counts": counts,
        "kept_dirs": [f"/work/mutants/{row['id']}" for row in results
                      if row["status"] in ("GAP", "EQUIVALENT")],
        **over,
    })


def timed(request: dict, *, files=timing_files, repetitions: int | None = None,
          **over) -> TimeResponse:
    """What the builder answers a /v1/time with: the clock and the files, per run.

    `files` writes one run's declared outputs and is where a test puts a
    program that writes something else the second time; `repetitions`
    answers with fewer runs than were asked for, which is not a
    measurement at all.
    """
    repeats = request["repeats"] if repetitions is None else repetitions
    return TimeResponse(**{
        "ok": True, "runs_s": run_seconds(repeats), "gpu_exclusive": True,
        "outputs": [files(request["outputs"], run) for run in range(repeats)],
        "executable_identity": dict(EXECUTABLE_IDENTITY), "log_tail": "",
        **over,
    })


def healthy(**over) -> HealthResponse:
    """What the builder answers /healthz with: everything a strategy can ask for."""
    return HealthResponse(**{
        "ok": True, "tools": {name: True for name in TOOLS},
        "python_modules": {name: True for name in PYTHON_MODULES},
        "executor_identity": EXECUTOR_IDENTITY,
        **over,
    })


# Every call a builder answers, which is also every name a test may
# configure. A typo would otherwise be a knob nobody read.
BUILDER_ENDPOINTS = (
    "healthz", "artifacts", "build", "run", "capture", "sanitize", "properties",
    "mutate", "time",
)


class FakeBuilder:
    """A builder that answers without a compiler, a sanitizer, or a GPU.

    Every endpoint answers with the typed response the real builder's
    would, computed from the request so that the golden path passes
    against a `FakeBuilder()` nobody configured. A test that wants a
    different answer hands one in by endpoint name -- a response object, a
    function of the request (the module's own makers take one and take
    the fields to change), or an exception to raise:

        FakeBuilder(run=RunResponse(ok=False, log_tail="runtime crash"))
        FakeBuilder(time=partial(timed, repetitions=1))
        FakeBuilder(artifacts=ConnectionError("connection reset"))

    The same slots are writable afterwards, as `builder.answers[name]`,
    for a test whose builder starts behaving differently partway through
    a run of gates.

    Every request is recorded, both in `calls` and in the endpoint's own
    list, because most of what these tests ask is what the gateway sent.
    """

    def __init__(self, **answers):
        unknown = sorted(set(answers) - set(BUILDER_ENDPOINTS))
        if unknown:
            raise TypeError(f"the builder has no {unknown} to answer")
        self.answers = dict(answers)
        self.calls = []
        self.build_calls = []
        self.run_calls = []
        self.capture_calls = []
        self.sanitize_calls = []
        self.properties_calls = []
        self.mutate_calls = []
        self.time_calls = []
        # What the builder's supervisor still holds, keyed by attempt: a
        # test empties it to be a builder that lost its volume.
        self.artifact_records = {}

    def _answer(self, name: str, request: dict, default):
        """The answer this test asked for, or what the real builder would say."""
        self.calls.append((name, request))
        recorded = getattr(self, f"{name}_calls", None)
        if recorded is not None:
            recorded.append(request)
        answer = self.answers.get(name)
        if isinstance(answer, BaseException):
            raise answer
        if answer is None:
            return default(request)
        return answer(request) if callable(answer) else answer

    def healthz(self):
        return self._answer("healthz", {}, lambda request: healthy())

    def artifacts(self, attempt_id):
        def default(request):
            executables = self.artifact_records.get(attempt_id, {})
            return ArtifactsResponse(
                ok=bool(executables), executor_identity=EXECUTOR_IDENTITY,
                executables={name: {**identity, "verified": True}
                             for name, identity in executables.items()},
            )
        return self._answer("artifacts", {"attempt_id": attempt_id}, default)

    def build(self, attempt_id, tree, makefile, targets, compiler, flags, link_flags,
              source_patterns):
        resp = self._answer("build", {
            "attempt_id": attempt_id, "tree": tree, "makefile": makefile,
            "targets": targets, "compiler": compiler, "flags": flags,
            "link_flags": link_flags, "source_patterns": source_patterns,
        }, built)
        if resp.ok:
            self.artifact_records[attempt_id] = {
                target["executable"]: {
                    "sha256": resp.targets[target["role"]]["sha256"], "size": 12345,
                    "role": target["role"],
                }
                for target in targets
            }
        return resp

    def run(self, attempt_id, executable, cases, notify=None, mandatory=False):
        return self._answer("run", {
            "attempt_id": attempt_id, "executable": executable, "cases": cases,
            "notify": notify, "mandatory": mandatory,
        }, replayed)

    def capture(self, attempt_id, executable, args, run_name):
        return self._answer("capture", {
            "attempt_id": attempt_id, "executable": executable, "args": list(args),
            "run_name": run_name,
        }, captured)

    def sanitize(self, attempt_id, executable, cases, tools):
        return self._answer("sanitize", {
            "attempt_id": attempt_id, "executable": executable, "cases": cases,
            "tools": tools,
        }, sanitized)

    def properties(self, attempt_id, executable, module, cases, seed, max_examples):
        return self._answer("properties", {
            "attempt_id": attempt_id, "executable": executable, "module": module,
            "cases": cases, "seed": seed, "max_examples": max_examples,
        }, property_run)

    def mutate(self, attempt_id, makefile, replay_target, files, cases, bands, compiler,
               flags, link_flags, source_patterns, jobs=None, limit=None):
        return self._answer("mutate", {
            "attempt_id": attempt_id, "makefile": makefile, "replay_target": replay_target,
            "files": list(files), "cases": cases, "bands": bands, "compiler": compiler,
            "flags": flags, "link_flags": link_flags, "source_patterns": source_patterns,
            "jobs": jobs, "limit": limit,
        }, mutated)

    def time(self, attempt_id, executable, args, env, outputs, repeats=5, budget_s=300,
             expected_outputs=None):
        return self._answer("time", {
            "attempt_id": attempt_id, "executable": executable, "args": args, "env": env,
            "outputs": outputs, "repeats": repeats, "budget_s": budget_s,
            "expected_outputs": expected_outputs,
        }, timed)


class FakeOracle:
    def __init__(self):
        self.compare_calls = []
        self.visible_verdict = "pass"
        self.holdout_verdict = "pass"

    def policy(self):
        return {
            "policy_version": "1", "policy_sha256": "f" * 64,
            "oracle_identity": "0" * 64,
        }

    def holdout_inputs(self):
        return {"dataset": "holdout", "cases": {"hcase0": fixture_case(offset=7)}}

    def compare(self, dataset, outputs, attempt_id="unknown"):
        self.compare_calls.append({"dataset": dataset, "outputs": outputs, "attempt_id": attempt_id})
        verdict = self.visible_verdict if dataset == "visible" else self.holdout_verdict
        resp = {
            "verdict": verdict, "dataset": dataset,
            "policy_sha256": "f" * 64, "oracle_identity": "0" * 64,
        }
        if dataset == "visible":
            resp["per_case"] = {name: {"pass": verdict == "pass"} for name in outputs}
        return resp
