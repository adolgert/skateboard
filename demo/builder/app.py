"""Builder service. Thin HTTP shim over stages.py. Compiles with nvfortran and
runs the GPU gates. All command lines live in stages.py (baked into the image);
this file only routes requests. Bearer token required.
"""
import os

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

import stages

TOKEN = os.environ.get("SKATEBOARD_TOKEN", "")
app = FastAPI(title="skateboard-builder")


def _auth(authorization):
    if TOKEN and authorization != f"Bearer {TOKEN}":
        raise HTTPException(status_code=401, detail="bad or missing token")


class BuildReq(BaseModel):
    attempt_id: str
    source: dict            # {"files": [{"path","content"}]}
    profile: str


class RunReq(BaseModel):
    attempt_id: str
    profile: str
    cases: dict             # {name: {"h_in": b64, "u_in": b64}}
    mandatory: bool = False


class SanitizeReq(BaseModel):
    attempt_id: str
    profile: str
    case: dict              # {name: {"h_in","u_in"}}  (one case)
    tools: list = ["memcheck", "racecheck"]


class TimeReq(BaseModel):
    attempt_id: str
    repeats: int = 5


@app.post("/v1/build")
def build(req: BuildReq, authorization: str | None = Header(default=None)):
    _auth(authorization)
    if req.profile not in stages.PROFILES:
        raise HTTPException(status_code=400, detail="unknown profile")
    return stages.build(req.attempt_id, req.source.get("files", []), req.profile)


@app.post("/v1/run")
def run(req: RunReq, authorization: str | None = Header(default=None)):
    _auth(authorization)
    return stages.run(req.attempt_id, req.profile, req.cases, mandatory=req.mandatory)


@app.post("/v1/sanitize")
def sanitize(req: SanitizeReq, authorization: str | None = Header(default=None)):
    _auth(authorization)
    return stages.sanitize(req.attempt_id, req.profile, req.case, req.tools)


@app.post("/v1/time")
def time_run(req: TimeReq, authorization: str | None = Header(default=None)):
    _auth(authorization)
    return stages.time_run(req.attempt_id, req.repeats)


@app.get("/healthz")
def healthz():
    import shutil
    return {
        "ok": True,
        "nvfortran": shutil.which("nvfortran") is not None,
        "compute_sanitizer": shutil.which("compute-sanitizer") is not None,
        "profiles": list(stages.PROFILES),
    }
