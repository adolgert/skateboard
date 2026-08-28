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

It also starts when it has nothing to answer with. A deployment can be
brought up while its code is still being brought in -- before any capture
exists, and while the manifest is still minimal -- and this service comes
up saying it is not ready, answering every question about a comparison
with 409 and the name of what is missing. Coming up in that state is the
point: the alternative is a container that will not start, in a
deployment whose whole purpose is to produce the thing it is missing.

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

# Where this image keeps the one code it answers for, and what is inside
# that directory. The whole code directory is copied in, so these are the
# same names the repository uses.
PROGRAM_DIR_VAR = "PROGRAM_DIR"
DEFAULT_PROGRAM_DIR = "/program"
MANIFEST_NAME = "manifest.yaml"
CAPTURES_NAME = "captures"

# What a request needs before it can be answered, named the way the
# not-ready reply names it.
CAPTURES = "captures"
TOLERANCES = "tolerances"
MANIFEST = "manifest"

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


def _manifest(manifest_path):
    """The code's manifest, or None if this image was built without one."""
    manifest_path = Path(manifest_path)
    if not manifest_path.is_file():
        return None
    return yaml.safe_load(manifest_path.read_bytes())


def _declared_outputs(manifest_path) -> list | None:
    """The region's output variables, or None while the manifest is still minimal.

    A minimal manifest is the form a code arrives in: it names the tree
    and nothing about the region yet. There is then no list of outputs to
    hold to a tolerance band, and nothing to compare either.
    """
    manifest = _manifest(manifest_path)
    if manifest is None or "interface" not in manifest:
        return None
    return list(manifest["interface"]["outputs"])


def policy_path_for(program_dir, manifest_path):
    """Where the code's manifest says its tolerance policy is, if it says.

    Every path a manifest names other than its source root is relative to
    the source tree, so the policy is read from inside the tree rather
    than beside the manifest.
    """
    manifest = _manifest(manifest_path)
    if manifest is None or "tolerances" not in manifest or "source" not in manifest:
        return None
    return Path(program_dir) / manifest["source"]["root"] / manifest["tolerances"]


def _what_is_missing(captures_dir: Path, tolerances_path, outputs) -> list:
    """What this oracle would need before it could answer a comparison.

    A captures directory that exists but holds only half a capture set is
    a different thing from one that was never written, and is an error:
    somebody built this image from a directory that was being written at
    the time.
    """
    missing = []
    if not captures_dir.is_dir():
        missing.append(CAPTURES)
    else:
        absent = [name for name in DATASETS if not (captures_dir / name).is_dir()]
        if absent and len(absent) < len(DATASETS):
            raise ValueError(
                f"captures directory {captures_dir} holds no '{absent[0]}' dataset, "
                f"although it holds the other one"
            )
        if absent:
            missing.append(CAPTURES)
    if tolerances_path is None or not Path(tolerances_path).is_file():
        missing.append(TOLERANCES)
    if outputs is None:
        missing.append(MANIFEST)
    return missing


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
    outputs = _declared_outputs(manifest_path)
    missing = _what_is_missing(captures_dir, tolerances_path, outputs)
    ready = not missing

    policy = None
    policy_sha = None
    bands = {}
    if ready:
        policy_bytes = Path(tolerances_path).read_bytes()
        policy = json.loads(policy_bytes)
        policy_sha = hashlib.sha256(policy_bytes).hexdigest()
        # Only when there is something to compare. Until then there is no
        # list of outputs to insist on a band for.
        _check_policy(policy, outputs)
        bands = policy["variables"]

    app = FastAPI(title="skateboard-oracle")

    def _auth(authorization: str | None):
        if not token:
            return
        if authorization != f"Bearer {token}":
            raise HTTPException(status_code=401, detail="bad or missing token")

    def _ready():
        """Refuse anything about a comparison this oracle cannot make.

        409 rather than 500: nothing went wrong, the answer simply does
        not exist yet, and the reply names what would have to exist.
        """
        if not ready:
            raise HTTPException(
                status_code=409,
                detail=f"this oracle has no answers to compare against: {missing} "
                       f"missing. It holds one code's captures and tolerance policy, "
                       f"baked in when the image was built.",
            )

    def _cases(dataset: str) -> list:
        return list(json.loads((captures_dir / dataset / CASES_FILE).read_text())["cases"])

    def _names(dataset: str, case: str) -> dict:
        return json.loads((captures_dir / dataset / case / CASE_FILE).read_text())

    def _load(dataset: str, case: str, name: str, suffix: str) -> np.ndarray:
        return np.load(captures_dir / dataset / case / f"{name}{suffix}", allow_pickle=False)

    @app.get("/v1/policy")
    def get_policy(authorization: str | None = Header(default=None)):
        _auth(authorization)
        _ready()
        return {"policy_version": policy["policy_version"], "policy_sha256": policy_sha}

    @app.get("/v1/dataset/holdout/inputs")
    def holdout_inputs(authorization: str | None = Header(default=None)):
        """Served once, at acceptance. Inputs only -- never expected outputs."""
        _auth(authorization)
        _ready()
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
        _ready()
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
        """Alive either way, and honest about whether it can answer anything."""
        return {
            "ok": True,
            "ready": ready,
            "missing": list(missing),
            "policy_sha256": policy_sha,
            "n_visible": len(_cases("visible")) if ready else 0,
            "n_holdout": len(_cases("holdout")) if ready else 0,
        }

    return app


def from_environment() -> FastAPI:
    """The app this image serves: one code's whole directory, and the token.

    The image copies `programs/<code>/` in as it stands, so everything
    this service reads is found the way the repository lays it out --
    the manifest at the top, the captures beside it, and the tolerance
    policy wherever inside the source tree the manifest says.
    """
    program_dir = Path(os.environ.get(PROGRAM_DIR_VAR, DEFAULT_PROGRAM_DIR))
    manifest_path = program_dir / MANIFEST_NAME
    return create_app(
        program_dir / CAPTURES_NAME,
        policy_path_for(program_dir, manifest_path),
        manifest_path,
        token=os.environ.get("SKATEBOARD_TOKEN", ""),
    )
