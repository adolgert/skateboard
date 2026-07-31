"""Agent-runner service. The ONLY untrusted component.

Contract (edge 1): POST /v1/attempt with the source snapshot, the strategy, and
a sanitized failure report; returns the complete new content of the one file the
agent is allowed to edit. It has no volumes, no route to the oracle or builder,
and its only egress is to the chosen LLM backend. It returns data, never actions.

Backend is pluggable via AGENT_BACKEND=anthropic|openai (config only).
"""
import os
import re

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

from adapters import anthropic_adapter, openai_adapter

TOKEN = os.environ.get("SKATEBOARD_TOKEN", "")
BACKEND = os.environ.get("AGENT_BACKEND", "anthropic")
EDITABLE = "src/mod_kernel.f90"
BEGIN = f"===BEGIN FILE {EDITABLE}==="
END = "===END FILE==="

app = FastAPI(title="skateboard-agent-runner")

SYSTEM = f"""You are an expert Fortran/GPU engineer working inside an automated porting harness.

Your task: port a numerical kernel so it runs on an NVIDIA GPU, WITHOUT changing
what it computes (within a calibrated floating-point tolerance).

Hard rules:
- You may modify ONLY the file {EDITABLE}. Do not touch any other file.
- Preserve the module name (mod_kernel), the public subroutine name and signature
  (`subroutine step(h, u)` with `real(real32), intent(inout) :: h(:), u(:)`), and
  the SEMANTIC CONTRACT documented in the file.
- Return the COMPLETE new contents of {EDITABLE}, and nothing else, wrapped exactly
  between a line containing `{BEGIN}` and a line containing `{END}`.
  Do not put a Markdown code fence inside the markers. Output the raw Fortran only.
- After the END marker you may add a short NOTES: line explaining what you changed.
"""


class AttemptReq(BaseModel):
    attempt_id: str = "unknown"
    strategy: str = "stdpar_managed"
    files: list = []                 # [{path, content}] snapshot (read-only context)
    failure_report: dict | None = None


STRATEGY_CARDS = {
    "stdpar_managed": (
        "STRATEGY: standard-parallelism offload.\n"
        "Rewrite `step` so nvfortran compiled with `-stdpar=gpu -gpu=cc89,mem:managed` "
        "offloads it to the GPU. Replace the whole-array assignments with explicit "
        "`do concurrent (i = 1:n)` loops. Inline the 2nd-order centered difference "
        "(periodic boundaries) from mod_diff::diff_centered directly into the loops. "
        "Because the stencil reads neighbours, use temporary arrays for the differences "
        "so the update is not corrupted in place.\n"
        "ORDERING (must preserve): compute the new u first from the OLD u and OLD h; "
        "then compute the new h using the FRESHLY-UPDATED u and the OLD h."
    ),
    "omp_target": (
        "STRATEGY: OpenMP target offload.\n"
        "Rewrite `step` using OpenMP `!$omp target teams distribute parallel do` so "
        "nvfortran compiled with `-mp=gpu -gpu=cc89` offloads it to the GPU. Same "
        "numerical structure, temporaries, and u-then-h ordering as above. Map the "
        "arrays to the device as needed."
    ),
}


def _auth(authorization):
    if TOKEN and authorization != f"Bearer {TOKEN}":
        raise HTTPException(status_code=401, detail="bad or missing token")


def _build_user(req: AttemptReq) -> str:
    by_path = {f["path"]: f["content"] for f in req.files}
    parts = [STRATEGY_CARDS.get(req.strategy, STRATEGY_CARDS["stdpar_managed"]), ""]
    parts.append("=== CURRENT FILE TO PORT ===")
    parts.append(by_path.get(EDITABLE, "(missing)"))
    for ctx in ("src/mod_diff.f90", "src/mod_params.f90"):
        if ctx in by_path:
            parts.append(f"\n=== CONTEXT (read-only): {ctx} ===")
            parts.append(by_path[ctx])
    if req.failure_report:
        parts.append("\n=== PREVIOUS ATTEMPT FAILED ===")
        parts.append("Stage failed: " + str(req.failure_report.get("stage_failed")))
        detail = req.failure_report.get("detail", {})
        parts.append("Details:\n" + _fmt(detail))
        parts.append("Fix the specific problem above. Do not regress what already passed.")
    parts.append(f"\nNow output the complete new {EDITABLE} between the markers.")
    return "\n".join(parts)


def _fmt(d, indent=0):
    out = []
    pad = "  " * indent
    if isinstance(d, dict):
        for k, v in d.items():
            if isinstance(v, (dict, list)):
                out.append(f"{pad}{k}:")
                out.append(_fmt(v, indent + 1))
            else:
                out.append(f"{pad}{k}: {v}")
    elif isinstance(d, list):
        for v in d:
            out.append(_fmt(v, indent))
    else:
        out.append(f"{pad}{d}")
    return "\n".join(out)


def _parse_file(text: str) -> str:
    if BEGIN in text and END in text:
        body = text.split(BEGIN, 1)[1].split(END, 1)[0]
    else:
        body = text  # fall back to whole response
    body = body.strip("\n")
    # strip an accidental ```fortran / ``` fence if the model added one
    body = re.sub(r"^```[a-zA-Z0-9]*\n", "", body)
    body = re.sub(r"\n```$", "", body)
    return body.strip("\n") + "\n"


@app.post("/v1/attempt")
def attempt(req: AttemptReq, authorization: str | None = Header(default=None)):
    _auth(authorization)
    user = _build_user(req)
    adapter = anthropic_adapter if BACKEND == "anthropic" else openai_adapter
    result = adapter.complete(SYSTEM, user)
    content = _parse_file(result["text"])
    notes = ""
    if END in result["text"]:
        notes = result["text"].split(END, 1)[1].strip()[:500]
    return {
        "attempt_id": req.attempt_id,
        "backend": BACKEND,
        "model_id": result.get("model_id"),
        "files": [{"path": EDITABLE, "content": content}],
        "notes": notes,
        "usage": result.get("usage", {}),
    }


@app.get("/healthz")
def healthz():
    return {"ok": True, "backend": BACKEND}
