"""FastAPI adapter for the gateway's framework-free services."""
from __future__ import annotations

import threading
from datetime import datetime, timezone

from fastapi import FastAPI, Header, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from equivalent.components.phase import provenance_for
from equivalent.ledger.acceptance import requirements_for
from equivalent.ledger.artifacts import build_binary_materials
from equivalent.ledger.evidence import current_build_claim
from equivalent.ledger.records import RequestLogLine
from equivalent.ledger.status import compute_status
from equivalent.ledger.store import LedgerStore
from equivalent.ledger.subjects import Subject
from equivalent.ledger.table import config_params, requires_for, rows_for
from equivalent.region.current import resolve_allow_globs
from equivalent.region.evidence import evidence_materials_for
from equivalent.strategy.schema import load_strategy
from equivalent.tree import Tree

from .errors import GatewayError
from .run import RunRequest, RunService, claim_response, current
from .submit import ConcurrentSubmissionError
from .submit import submit as do_submit
from .verification import artifacts_still_held, current_materials


class SubmitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    region: str


class RunRequestBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: str
    region: str
    config: dict = {}


def _validation_detail(exc: RequestValidationError) -> str:
    parts = []
    for error in exc.errors():
        field = ".".join(str(item) for item in error["loc"][1:]) or "body"
        parts.append(f"{field}: {error['msg']}")
    return "; ".join(parts) or "malformed request body"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _claim_read_response(claim) -> dict:
    return {
        "predicateType": claim.predicateType,
        "subject": [subject.to_dict() for subject in claim.subject],
        "materials": [material.to_dict() for material in claim.materials],
        **claim_response(claim),
    }


def create_app(regions: dict, token: str, *, builder=None, oracle=None) -> FastAPI:
    """Build the HTTP edge around the shared gateway domain services."""
    app = FastAPI(title="equivalent-gateway")
    stores: dict[str, LedgerStore] = {}
    run_locks = {region_id: threading.Lock() for region_id in regions}
    runs = RunService(builder=builder, oracle=oracle)

    @app.exception_handler(RequestValidationError)
    def malformed_request(request, exc: RequestValidationError):
        return JSONResponse(status_code=400, content={"detail": _validation_detail(exc)})

    def as_http(call):
        try:
            return call()
        except GatewayError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    def auth(authorization):
        if token and authorization != f"Bearer {token}":
            raise HTTPException(status_code=401, detail="bad or missing token")

    def resolve_region(region_id: str):
        cfg = regions.get(region_id)
        if cfg is None:
            raise HTTPException(status_code=404, detail=f"unknown region: {region_id}")
        return cfg

    def store(region_id: str) -> LedgerStore:
        if region_id not in stores:
            stores[region_id] = LedgerStore(regions[region_id].ledger_dir)
        return stores[region_id]

    @app.get("/healthz")
    def get_healthz():
        return {"ok": True}

    @app.get("/table")
    def get_table(region: str | None = None, authorization: str | None = Header(default=None)):
        auth(authorization)
        if region is None:
            raise HTTPException(
                status_code=400,
                detail="GET /table needs a region: the actions a session has are the "
                       "actions of its region's phase",
            )
        cfg = resolve_region(region)
        return [
            {
                "name": row.name,
                "emits": list(row.emits),
                "requires": [list(pair) for pair in requires_for(row, cfg.manifest)],
                "deterministic": row.deterministic,
                "component": row.component,
                "config_keys": list(row.config_keys),
                "config_params": config_params(row),
            }
            for row in rows_for(cfg.phase)
        ]

    @app.get("/status")
    def get_status(region: str, authorization: str | None = Header(default=None)):
        auth(authorization)
        cfg = resolve_region(region)
        ledger = store(region)

        def status():
            strategy = load_strategy(cfg.strategy_path)
            baseline_strategy = load_strategy(cfg.baseline_strategy_path)
            materials, executor_identity = current_materials(
                cfg, strategy, baseline_strategy, builder, oracle,
            )
            tree_sha, frozen_sha = current(
                cfg, ledger, strategy, required_materials=materials,
            )
            tree_subject = Subject(kind="tree", sha256=tree_sha)
            build_claim = current_build_claim(ledger, cfg.phase, tree_subject, materials)
            build_materials = (
                build_binary_materials(build_claim.predicateType, build_claim.predicate.detail)
                if build_claim else ()
            )
            build_context_ok = bool(
                build_claim and build_materials and artifacts_still_held(
                    builder, build_claim.predicateType, build_claim.predicate.detail, executor_identity,
                )
            )
            return compute_status(
                ledger, requirements_for(cfg.phase, cfg.manifest), cfg.phase,
                tree=tree_subject, frozen=Subject(kind="frozen", sha256=frozen_sha),
                baseline_tree=Subject(kind="tree", sha256=Tree.baseline(cfg.repo_dir).sha),
                required_materials=materials,
                context_verified=bool(
                    (not provenance_for(cfg.phase).oracle_judges or oracle is not None)
                    and build_context_ok
                ),
            )

        return as_http(status)

    @app.get("/claims/{claim_id}")
    def get_claim(
        claim_id: str, region: str, authorization: str | None = Header(default=None),
        x_session_id: str = Header(...), x_model_id: str = Header(...),
        x_tool_call_id: str | None = Header(default=None),
    ):
        auth(authorization)
        resolve_region(region)
        ledger = store(region)
        claim = ledger.get_claim(claim_id)
        ledger.append_request(RequestLogLine(
            ts=_now(), session=x_session_id, model=x_model_id, endpoint="claim", action="claim",
            region=region, tree=None, config_hash=None, outcome="read" if claim else "error",
            claim_id=claim_id, tool_call_id=x_tool_call_id,
        ))
        if claim is None:
            raise HTTPException(status_code=404, detail=f"unknown claim: {claim_id}")
        return _claim_read_response(claim)

    @app.post("/submit")
    def post_submit(
        request: SubmitRequest, authorization: str | None = Header(default=None),
        x_session_id: str = Header(...), x_model_id: str = Header(...),
        x_tool_call_id: str | None = Header(default=None),
    ):
        auth(authorization)
        cfg = resolve_region(request.region)
        ledger = store(request.region)
        strategy = load_strategy(cfg.strategy_path)
        baseline_strategy = load_strategy(cfg.baseline_strategy_path)
        materials = evidence_materials_for(cfg, strategy, baseline_strategy)
        allow_globs = resolve_allow_globs(
            ledger, cfg.spec_path, cfg.phase, strategy, required_materials=materials,
        )
        try:
            receipt = do_submit(
                cfg.repo_dir, cfg.region_id, cfg.working_copy_dir, allow_globs, x_session_id,
            )
        except ConcurrentSubmissionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        ledger.append_request(RequestLogLine(
            ts=_now(), session=x_session_id, model=x_model_id, endpoint="submit", action="submit",
            region=request.region, tree=receipt.tree, config_hash=None, outcome="submitted",
            tool_call_id=x_tool_call_id,
        ))
        return {
            "tree": receipt.tree, "frozen": receipt.frozen, "rejected": list(receipt.rejected),
            "not_sent": list(receipt.not_sent), "committed": receipt.committed,
        }

    @app.post("/run")
    def post_run(
        request: RunRequestBody, authorization: str | None = Header(default=None),
        x_session_id: str = Header(...), x_model_id: str = Header(...),
        x_tool_call_id: str | None = Header(default=None),
    ):
        auth(authorization)
        cfg = resolve_region(request.region)
        domain_request = RunRequest(request.action, request.region, request.config)
        with run_locks[request.region]:
            with store(request.region).execution_lock():
                return as_http(lambda: runs.execute(runs.resolve(
                    cfg, store(request.region), domain_request, session=x_session_id,
                    model=x_model_id, tool_call_id=x_tool_call_id,
                )))

    return app
