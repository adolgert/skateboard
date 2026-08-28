"""Builder service. Thin HTTP shim over stages.py.

Trust role: routing only. Every command line lives in stages.py; this
file turns a request body into a call and the answer into JSON. What it
must get right is that nothing it invents reaches stages.py -- the tree,
the makefile, the targets, the compiler, the flags, and the executables
all come from the gateway, which read them from the code's hashed
manifest and the hashed strategy file. Bearer token required.
"""
import importlib.util
import os
import shutil

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

from . import stages

TOKEN = os.environ.get("SKATEBOARD_TOKEN", "")

# The executables a strategy may name in its `required_tools`. Reported
# present or absent, never installed on demand: the image is what it is,
# and the gateway refuses to start against a builder that is missing
# something a strategy needs.
TOOLS = ("nvfortran", "compute-sanitizer", "nsys", "make", "cmake", "fpm", "gfortran")

# The importable modules a strategy may ask for, spelled `python:<module>`
# in its `required_tools`. They are reported separately from TOOLS because
# they are found differently: pytest is a module this service imports, not
# an executable on PATH, and looking for a `pytest` binary would answer a
# different question from the one the property stage asks.
PYTHON_MODULES = ("pytest", "hypothesis", "numpy")
app = FastAPI(title="skateboard-builder")


def _auth(authorization):
    if TOKEN and authorization != f"Bearer {TOKEN}":
        raise HTTPException(status_code=401, detail="bad or missing token")


class TreeFile(BaseModel):
    path: str    # relative to the tree root, directories included
    b64: str     # the file's bytes; a tree holds namelists and data, not only text


class BuildTarget(BaseModel):
    role: str        # what the manifest calls this target: replay, timing, capture
    target: str      # what `make` is asked for
    executable: str  # what that target must leave in the tree, relative to its root


class BuildReq(BaseModel):
    attempt_id: str
    # The WHOLE tracked tree, not a filtered source list: a code's build
    # reads include files, namelists, and small data files that no
    # extension test would recognize.
    tree: list[TreeFile]
    makefile: str
    targets: list[BuildTarget]
    compiler: str
    flags: list[str] = []
    link_flags: list[str] = []
    # What the code calls its own source, used to say whether the build
    # compiled anything the manifest never described.
    source_patterns: list[str] = []


class RunReq(BaseModel):
    attempt_id: str
    executable: str  # the manifest's replay target
    # {name: {variable: base64 of that variable's .npy file}}. The file
    # says what type and shape the array is; nothing else has to.
    cases: dict
    # The strategy's device proof: which offload runtime should be asked
    # to announce its kernel launches, or none at all.
    notify: str | None = None
    mandatory: bool = False


class CaptureReq(BaseModel):
    attempt_id: str
    executable: str  # the manifest's capture target
    # The dataset's own arguments, from the manifest. The directory to
    # write into is added after them by the builder, which is the one
    # thing the capture contract fixes.
    args: list[str] = []
    run_name: str  # what to call this run's output directory


class SanitizeReq(BaseModel):
    attempt_id: str
    executable: str
    # One entry per case to sanitize, shaped like RunReq.cases. How many
    # cases that is comes from the gateway's hashed strategy file; the
    # builder runs whatever it is sent.
    cases: dict
    tools: list = ["memcheck", "racecheck"]


class PropertiesReq(BaseModel):
    attempt_id: str
    executable: str  # the manifest's replay target, which the properties call
    module: str      # the manifest's properties module, relative to the tree root
    # The visible cases, shaped like RunReq.cases. They become the corpus
    # the code's own properties draw from.
    cases: dict
    seed: int
    max_examples: int


class TimeReq(BaseModel):
    attempt_id: str
    executable: str  # the manifest's timing target
    args: list[str] = []
    env: dict = {}
    outputs: list[str] = []  # files the run must write, collected and returned
    repeats: int = 5
    budget_s: int = 300


@app.post("/v1/build")
def build(req: BuildReq, authorization: str | None = Header(default=None)):
    _auth(authorization)
    return stages.build(
        req.attempt_id, [f.model_dump() for f in req.tree], req.makefile,
        [t.model_dump() for t in req.targets], req.compiler,
        req.flags, req.link_flags, req.source_patterns,
    )


@app.post("/v1/run")
def run(req: RunReq, authorization: str | None = Header(default=None)):
    _auth(authorization)
    return stages.run(
        req.attempt_id, req.executable, req.cases,
        notify=req.notify, mandatory=req.mandatory,
    )


@app.post("/v1/capture")
def capture(req: CaptureReq, authorization: str | None = Header(default=None)):
    _auth(authorization)
    return stages.capture(req.attempt_id, req.executable, req.args, req.run_name)


@app.post("/v1/sanitize")
def sanitize(req: SanitizeReq, authorization: str | None = Header(default=None)):
    _auth(authorization)
    return stages.sanitize(req.attempt_id, req.executable, req.cases, req.tools)


@app.post("/v1/properties")
def properties(req: PropertiesReq, authorization: str | None = Header(default=None)):
    _auth(authorization)
    return stages.properties(
        req.attempt_id, req.executable, req.module, req.cases, req.seed, req.max_examples,
    )


@app.post("/v1/time")
def time_run(req: TimeReq, authorization: str | None = Header(default=None)):
    _auth(authorization)
    return stages.time_run(
        req.attempt_id, req.executable, args=req.args, env=req.env,
        outputs=req.outputs, repeats=req.repeats, budget_s=req.budget_s,
    )


@app.get("/healthz")
def healthz():
    """Liveness, plus what this image can actually run.

    The tool keys are the executable names exactly as a strategy's
    `required_tools` spells them, so the gateway can compare the two
    without translating between two vocabularies. The module keys are the
    same idea for what a property run imports: a strategy asks for one by
    writing `python:pytest`, and this says whether the interpreter that
    would run it can import it.
    """
    return {
        "ok": True,
        "tools": {name: shutil.which(name) is not None for name in TOOLS},
        "python_modules": {name: _importable(name) for name in PYTHON_MODULES},
    }


def _importable(name: str) -> bool:
    """Can this service's own interpreter import that module.

    It is the interpreter the property stage runs pytest with, so this
    answers the question the gateway is actually asking.
    """
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False
