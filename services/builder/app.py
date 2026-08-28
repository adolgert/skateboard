"""Builder service. Thin HTTP shim over stages.py. Compiles with nvfortran and
runs the GPU gates. All command lines live in stages.py (baked into the image);
this file only routes requests. Bearer token required.
"""
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
TOOLS = ("nvfortran", "compute-sanitizer", "nsys", "make", "cmake", "gfortran")
app = FastAPI(title="skateboard-builder")


def _auth(authorization):
    if TOKEN and authorization != f"Bearer {TOKEN}":
        raise HTTPException(status_code=401, detail="bad or missing token")


class BuildReq(BaseModel):
    attempt_id: str
    source: dict            # {"files": [{"path","content"}]}
    profile: str
    # Optional explicit compile/link flags from the gateway's strategy file.
    # When present they replace the profile's baked flag list for this
    # build; the profile still names the workspace/notify behavior.
    # Absent (the demo orchestrator's case), the profile table applies.
    flags: list | None = None
    link_flags: list | None = None


class RunReq(BaseModel):
    attempt_id: str
    profile: str
    # {name: {variable: base64 of that variable's .npy file}}. The file
    # says what type and shape the array is; nothing else has to.
    cases: dict
    mandatory: bool = False


class SanitizeReq(BaseModel):
    attempt_id: str
    profile: str
    # One entry per case to sanitize, shaped like RunReq.cases. How many
    # cases that is comes from the gateway's hashed strategy file; the
    # builder runs whatever it is sent.
    cases: dict
    tools: list = ["memcheck", "racecheck"]


class TimeReq(BaseModel):
    attempt_id: str
    repeats: int = 5


@app.post("/v1/build")
def build(req: BuildReq, authorization: str | None = Header(default=None)):
    _auth(authorization)
    if req.profile not in stages.PROFILES:
        raise HTTPException(status_code=400, detail="unknown profile")
    return stages.build(req.attempt_id, req.source.get("files", []), req.profile,
                        flags=req.flags, link_flags=req.link_flags)


@app.post("/v1/run")
def run(req: RunReq, authorization: str | None = Header(default=None)):
    _auth(authorization)
    return stages.run(req.attempt_id, req.profile, req.cases, mandatory=req.mandatory)


@app.post("/v1/sanitize")
def sanitize(req: SanitizeReq, authorization: str | None = Header(default=None)):
    _auth(authorization)
    return stages.sanitize(req.attempt_id, req.profile, req.cases, req.tools)


@app.post("/v1/time")
def time_run(req: TimeReq, authorization: str | None = Header(default=None)):
    _auth(authorization)
    return stages.time_run(req.attempt_id, req.repeats)


@app.get("/healthz")
def healthz():
    """Liveness, plus which of the known executables this image actually has.

    The keys are the executable names exactly as a strategy's
    `required_tools` spells them, so the gateway can compare the two
    without translating between two vocabularies.
    """
    return {
        "ok": True,
        "tools": {name: shutil.which(name) is not None for name in TOOLS},
        "profiles": list(stages.PROFILES),
    }
