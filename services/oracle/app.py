"""Oracle service. Holds expected answers (visible + held-out) and the tolerance
policy. Answers exactly one kind of question: do these output arrays match?

Trust properties:
  * No write endpoint exists. Tolerances change only by rebuilding this image.
  * It executes nothing supplied by the agent -- only numpy comparisons.
  * For the held-out dataset, /compare returns pass/fail ONLY -- never per-case
    error detail -- so nothing quantitative about held-out can leak upstream.
  * Bearer token required on every call.
"""
import base64
import glob
import hashlib
import json
import os

import numpy as np
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

import compare as cmp

CAP_DIR = os.environ.get("CAPTURES_DIR", "/captures")
TOL_PATH = os.environ.get("TOLERANCES", "/tolerances.json")
TOKEN = os.environ.get("SKATEBOARD_TOKEN", "")

with open(TOL_PATH, "rb") as f:
    _tol_bytes = f.read()
POLICY = json.loads(_tol_bytes)
POLICY_SHA = hashlib.sha256(_tol_bytes).hexdigest()
TOLS = POLICY["variables"]

app = FastAPI(title="skateboard-oracle")


def _auth(authorization: str | None):
    if not TOKEN:
        return
    if authorization != f"Bearer {TOKEN}":
        raise HTTPException(status_code=401, detail="bad or missing token")


def _b64_to_f32(s: str) -> np.ndarray:
    return np.frombuffer(base64.b64decode(s), dtype="<f4")


def _load_case(dataset: str, case: str, which) -> dict:
    out = {}
    for var, fname in which.items():
        p = os.path.join(CAP_DIR, dataset, case, fname)
        out[var] = np.fromfile(p, dtype="<f4")
    return out


def _cases(dataset: str) -> list[str]:
    man = os.path.join(CAP_DIR, dataset, "cases.json")
    return json.load(open(man))["cases"]


class CompareReq(BaseModel):
    attempt_id: str = "unknown"
    dataset: str
    outputs: dict  # {case: {"h": b64, "u": b64}}


@app.get("/v1/policy")
def policy(authorization: str | None = Header(default=None)):
    _auth(authorization)
    return {"policy_version": POLICY["policy_version"], "policy_sha256": POLICY_SHA}


@app.get("/v1/dataset/holdout/inputs")
def holdout_inputs(authorization: str | None = Header(default=None)):
    """Served once, at acceptance. Inputs only -- never expected outputs."""
    _auth(authorization)
    cases = {}
    for c in _cases("holdout"):
        ins = _load_case("holdout", c, {"h_in": "h_in.bin", "u_in": "u_in.bin"})
        cases[c] = {
            "h_in": base64.b64encode(ins["h_in"].tobytes()).decode(),
            "u_in": base64.b64encode(ins["u_in"].tobytes()).decode(),
        }
    return {"dataset": "holdout", "cases": cases}


@app.post("/v1/compare")
def compare(req: CompareReq, authorization: str | None = Header(default=None)):
    _auth(authorization)
    if req.dataset not in ("visible", "holdout"):
        raise HTTPException(status_code=400, detail="unknown dataset")

    per_case = {}
    all_pass = True
    for c in _cases(req.dataset):
        expected = _load_case(req.dataset, c, {"h": "h_out.bin", "u": "u_out.bin"})
        got_raw = req.outputs.get(c)
        if got_raw is None:
            per_case[c] = {"pass": False, "error": "no output for case"}
            all_pass = False
            continue
        got = {v: _b64_to_f32(got_raw[v]) for v in ("h", "u") if v in got_raw}
        res = cmp.compare_case(expected, got, TOLS)
        per_case[c] = res
        all_pass = all_pass and res["pass"]

    resp = {
        "verdict": "pass" if all_pass else "fail",
        "dataset": req.dataset,
        "policy_sha256": POLICY_SHA,
    }
    # Held-out returns pass/fail ONLY, by design. Visible returns detail for the
    # feedback report the agent will see.
    if req.dataset == "visible":
        resp["per_case"] = per_case
    return resp


@app.get("/healthz")
def healthz():
    return {"ok": True, "policy_sha256": POLICY_SHA, "n_visible": len(_cases("visible"))}
