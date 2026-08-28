"""Oracle service. Holds expected answers (visible + held-out) and the tolerance
policy. Answers exactly one kind of question: do these output arrays match?

Trust properties:
  * No write endpoint exists. Tolerances change only by rebuilding this image.
  * It executes nothing supplied by the agent -- only numpy comparisons.
  * For the held-out dataset, /compare returns pass/fail ONLY -- never per-case
    error detail -- so nothing quantitative about held-out can leak upstream.
  * Bearer token required on every call.

What a variable is called, what type it is, and how many dimensions it has are
read from the capture files and from the code's own manifest, never written
down here. The one thing this service insists on is that every floating-point
output the manifest declares has a tolerance band before it will start: a
variable compared with no band is a comparison nobody chose.

This image installs numpy, yaml, and the web server, and nothing else of this
project -- so this file and compare.py import nothing from `equivalent`, and
read the capture format's few conventions directly.
"""
import base64
import hashlib
import io
import json
import os
from pathlib import Path

import numpy as np
import yaml
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

from . import compare as cmp

# The capture format, spelled out because this service cannot import the
# package that defines it. One directory per case; `case.json` names the
# variables that directory holds; each is an .npy file that says for
# itself what type and shape it has.
CASE_FILE = "case.json"
CASES_FILE = "cases.json"
INPUT_SUFFIX = ".npy"
OUTPUT_SUFFIX = ".out.npy"

# The two datasets every code is judged against.
DATASETS = ("visible", "holdout")

# The manifest dtypes whose comparison consults a tolerance band. Any other
# declared output type is compared exactly and needs no entry.
BANDED_DTYPES = ("f32", "f64")
BAND_FIELDS = ("abs", "rel", "ulp")


def _decode(encoded: str) -> np.ndarray:
    return np.load(io.BytesIO(base64.b64decode(encoded)), allow_pickle=False)


def _encode(array: np.ndarray) -> str:
    buffer = io.BytesIO()
    np.save(buffer, array, allow_pickle=False)
    return base64.b64encode(buffer.getvalue()).decode()


def _declared_outputs(manifest_path) -> list:
    """The region's output variables, as the code's manifest declares them."""
    manifest = yaml.safe_load(Path(manifest_path).read_bytes())
    return list(manifest["interface"]["outputs"])


def _check_policy(policy: dict, outputs: list) -> None:
    """Every output that is compared with a band must have one."""
    bands = policy.get("variables", {})
    for variable in outputs:
        if variable["dtype"] not in BANDED_DTYPES:
            continue
        name = variable["name"]
        band = bands.get(name)
        if band is None:
            raise ValueError(
                f"the tolerance policy has no entry for output variable '{name}', "
                f"which is {variable['dtype']} and so is compared within a band"
            )
        missing = [field for field in BAND_FIELDS if field not in band]
        if missing:
            raise ValueError(
                f"the tolerance policy for output variable '{name}' is missing {missing}"
            )


class CompareReq(BaseModel):
    attempt_id: str = "unknown"
    dataset: str
    # {case: {variable: base64 of that variable's .npy file}}
    outputs: dict


def create_app(captures_dir, tolerances_path, manifest_path, token: str = "") -> FastAPI:
    captures_dir = Path(captures_dir)
    for dataset in DATASETS:
        if not (captures_dir / dataset).is_dir():
            raise ValueError(f"captures directory {captures_dir} holds no '{dataset}' dataset")

    policy_bytes = Path(tolerances_path).read_bytes()
    policy = json.loads(policy_bytes)
    policy_sha = hashlib.sha256(policy_bytes).hexdigest()
    _check_policy(policy, _declared_outputs(manifest_path))
    bands = policy["variables"]

    app = FastAPI(title="skateboard-oracle")

    def _auth(authorization: str | None):
        if not token:
            return
        if authorization != f"Bearer {token}":
            raise HTTPException(status_code=401, detail="bad or missing token")

    def _cases(dataset: str) -> list:
        return list(json.loads((captures_dir / dataset / CASES_FILE).read_text())["cases"])

    def _names(dataset: str, case: str) -> dict:
        return json.loads((captures_dir / dataset / case / CASE_FILE).read_text())

    def _load(dataset: str, case: str, name: str, suffix: str) -> np.ndarray:
        return np.load(captures_dir / dataset / case / f"{name}{suffix}", allow_pickle=False)

    @app.get("/v1/policy")
    def get_policy(authorization: str | None = Header(default=None)):
        _auth(authorization)
        return {"policy_version": policy["policy_version"], "policy_sha256": policy_sha}

    @app.get("/v1/dataset/holdout/inputs")
    def holdout_inputs(authorization: str | None = Header(default=None)):
        """Served once, at acceptance. Inputs only -- never expected outputs."""
        _auth(authorization)
        cases = {}
        for case in _cases("holdout"):
            cases[case] = {
                name: _encode(_load("holdout", case, name, INPUT_SUFFIX))
                for name in _names("holdout", case)["inputs"]
            }
        return {"dataset": "holdout", "cases": cases}

    @app.post("/v1/compare")
    def compare_outputs(req: CompareReq, authorization: str | None = Header(default=None)):
        _auth(authorization)
        if req.dataset not in DATASETS:
            raise HTTPException(status_code=400, detail="unknown dataset")

        per_case = {}
        all_pass = True
        for case in _cases(req.dataset):
            expected = {
                name: _load(req.dataset, case, name, OUTPUT_SUFFIX)
                for name in _names(req.dataset, case)["outputs"]
            }
            submitted = req.outputs.get(case)
            if submitted is None:
                per_case[case] = {"pass": False, "error": "no output for case"}
                all_pass = False
                continue
            got = {name: _decode(blob) for name, blob in submitted.items()}
            result = cmp.compare_case(expected, got, bands)
            per_case[case] = result
            all_pass = all_pass and result["pass"]

        resp = {
            "verdict": "pass" if all_pass else "fail",
            "dataset": req.dataset,
            "policy_sha256": policy_sha,
        }
        # Held-out returns pass/fail ONLY, by design. Visible returns detail for
        # the feedback report the agent will see.
        if req.dataset == "visible":
            resp["per_case"] = per_case
        return resp

    @app.get("/healthz")
    def healthz():
        return {
            "ok": True,
            "policy_sha256": policy_sha,
            "n_visible": len(_cases("visible")),
            "n_holdout": len(_cases("holdout")),
        }

    return app


def from_environment() -> FastAPI:
    """The app this image serves: paths and token from the container's environment."""
    return create_app(
        os.environ.get("CAPTURES_DIR", "/captures"),
        os.environ.get("TOLERANCES", "/tolerances.json"),
        os.environ.get("MANIFEST", "/manifest.yaml"),
        token=os.environ.get("SKATEBOARD_TOKEN", ""),
    )
