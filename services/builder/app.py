"""Builder service. Thin HTTP shim over stages.py.

Trust role: routing only. Every command line lives in stages.py; this
file turns a request body into a call and the answer into JSON, which
FastAPI validates against the response type the contract declares. What it
must get right is that nothing it invents reaches stages.py -- the tree,
the makefile, the targets, the compiler, the flags, and the executables
all come from the gateway, which read them from the code's hashed
manifest and the hashed strategy file. The request and response shapes
are in contract.py, so this file names no key of its own. Bearer token
required.
"""
import importlib.util
import os
import shutil

from fastapi import FastAPI, Header, HTTPException

from . import contract, stages

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


def _workspace(attempt_id: str):
    """The one workspace this attempt owns, under this service's own policy."""
    return stages.workspace_for(attempt_id)


@app.post("/v1/build", response_model=contract.BuildResponse)
def build(req: contract.BuildRequest, authorization: str | None = Header(default=None)):
    _auth(authorization)
    return stages.build(
        _workspace(req.attempt_id), tree=[f.model_dump() for f in req.tree],
        makefile=req.makefile, targets=[t.model_dump() for t in req.targets],
        compiler=req.compiler, flags=req.flags, link_flags=req.link_flags,
        source_patterns=req.source_patterns,
    )


@app.post("/v1/run", response_model=contract.RunResponse)
def run(req: contract.RunRequest, authorization: str | None = Header(default=None)):
    _auth(authorization)
    return stages.run(
        _workspace(req.attempt_id), executable=req.executable, cases=req.cases,
        notify=req.notify, mandatory=req.mandatory,
    )


@app.post("/v1/capture", response_model=contract.CaptureResponse)
def capture(req: contract.CaptureRequest, authorization: str | None = Header(default=None)):
    _auth(authorization)
    return stages.capture(
        _workspace(req.attempt_id), executable=req.executable, args=req.args,
        run_name=req.run_name,
    )


@app.post("/v1/sanitize", response_model=contract.SanitizeResponse)
def sanitize(req: contract.SanitizeRequest, authorization: str | None = Header(default=None)):
    _auth(authorization)
    return stages.sanitize(
        _workspace(req.attempt_id), executable=req.executable, cases=req.cases,
        tools=req.tools,
    )


@app.post("/v1/properties", response_model=contract.PropertiesResponse)
def properties(req: contract.PropertiesRequest, authorization: str | None = Header(default=None)):
    _auth(authorization)
    return stages.properties(
        _workspace(req.attempt_id), executable=req.executable, module=req.module,
        cases=req.cases, seed=req.seed, max_examples=req.max_examples,
    )


@app.post("/v1/mutate", response_model=contract.MutateResponse)
def mutate(req: contract.MutateRequest, authorization: str | None = Header(default=None)):
    _auth(authorization)
    return stages.mutate(
        _workspace(req.attempt_id), makefile=req.makefile,
        replay_target=req.replay_target.model_dump(), files=req.files,
        cases=req.cases, bands=req.bands, compiler=req.compiler, flags=req.flags,
        link_flags=req.link_flags, source_patterns=req.source_patterns,
        jobs=req.jobs, limit=req.limit,
    )


@app.post("/v1/time", response_model=contract.TimeResponse)
def time_run(req: contract.TimeRequest, authorization: str | None = Header(default=None)):
    _auth(authorization)
    return stages.time_run(
        _workspace(req.attempt_id), executable=req.executable, args=req.args,
        env=req.env, outputs=req.outputs, repeats=req.repeats, budget_s=req.budget_s,
        expected_outputs=req.expected_outputs,
    )


@app.get("/v1/artifacts/{attempt_id}", response_model=contract.ArtifactsResponse)
def artifacts(attempt_id: str, authorization: str | None = Header(default=None)):
    """Protected executable identities, reverified against bytes on disk."""
    _auth(authorization)
    return _workspace(attempt_id).artifact_identities()


@app.get("/healthz", response_model=contract.HealthResponse)
def healthz():
    """Liveness, plus what this image can actually run.

    The tool keys are the executable names exactly as a strategy's
    `required_tools` spells them, so the gateway can compare the two
    without translating between two vocabularies. The module keys are the
    same idea for what a property run imports: a strategy asks for one by
    writing `python:pytest`, and this says whether the interpreter that
    would run it can import it.
    """
    isolation = stages.isolation_status()
    return contract.HealthResponse(
        ok=isolation.get("ok") is True,
        tools={name: shutil.which(name) is not None for name in TOOLS},
        python_modules={name: _importable(name) for name in PYTHON_MODULES},
        isolation=isolation,
        executor_identity=isolation.get("executor_identity"),
    )


def _importable(name: str) -> bool:
    """Can this service's own interpreter import that module.

    It is the interpreter the property stage runs pytest with, so this
    answers the question the gateway is actually asking.
    """
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False
