"""The gateway HTTP service.

Trust role: the reference monitor. This is the only thing an agent's
session can reach. Everything the agent or the person believes about a
region's progress comes from what this module reads and returns; a bug
here can make a bad port look accepted.

All four endpoints are built now: GET /table, GET /status, POST /submit,
POST /run. POST /run refuses a request whose required claims are missing,
returns an existing claim for a repeated deterministic request, and
otherwise dispatches to the action's component. Every row in
equivalent.gateway.table.ACTION_TABLE now has real dispatch (Step 6a-6f);
an action whose builder or oracle client isn't configured for this
gateway instance still falls back to the "not implemented" response
rather than crashing.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

from equivalent.components import build_replay, regression, run_replay, sanitize, sese_check, timing
from equivalent.components.errors import ComponentError
from equivalent.gateway.datasets import load_visible_cases
from equivalent.ledger.acceptance import ACCEPTANCE_REQUIREMENTS
from equivalent.ledger.records import Predicate, RequestLogLine
from equivalent.ledger.status import compute_status, requirement_status
from equivalent.ledger.store import LedgerStore
from equivalent.ledger.subjects import Subject, hash_bytes
from equivalent.strategy.schema import load_strategy

from .regions import RegionConfig
from .submit import (
    baseline_tree_sha,
    current_ref,
    current_tree_and_frozen,
    frozen_for_allow_globs,
    resolve_allow_globs,
)
from .submit import submit as do_submit
from .table import ACTION_TABLE

ROWS_BY_NAME = {row.name: row for row in ACTION_TABLE}
PRODUCERS = {predicate_type: row.name for row in ACTION_TABLE for predicate_type in row.emits}
# Which subject a predicate type's own claim is recorded against -- e.g.
# sese/verified is scoped to "frozen", everything else in this list to
# "tree". Reused from the accept row's own requirements rather than a
# second hand-written copy. Falls back to "tree" for anything not listed
# there (currently just timing/baseline, which is nondeterministic and so
# never reaches the duplicate check that uses this).
SUBJECT_KIND_OF = {req.predicate_type: req.subject_kind for req in ACCEPTANCE_REQUIREMENTS}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def config_hash(config: dict) -> str:
    return hash_bytes(json.dumps(config, sort_keys=True).encode("utf-8"))


class SubmitRequest(BaseModel):
    region: str
    working_copy_dir: str


class RunRequest(BaseModel):
    action: str
    region: str
    config: dict = {}


def create_app(regions: dict[str, RegionConfig], token: str, *, builder=None, oracle=None) -> FastAPI:
    """`builder` and `oracle` are BuilderClient/OracleClient-shaped objects
    (equivalent.gateway.backend_client), or None. An action whose
    component needs one that isn't configured falls through to the same
    "not implemented yet" response as an action with no component wiring
    at all -- a gateway can be brought up with the ledger/analyzer side
    working before the builder or oracle are reachable.
    """
    app = FastAPI(title="skateboard-gateway")
    stores: dict[str, LedgerStore] = {}
    visible_cases: dict[str, dict] = {}

    def _auth(authorization):
        if token and authorization != f"Bearer {token}":
            raise HTTPException(status_code=401, detail="bad or missing token")

    def _region(region_id: str) -> RegionConfig:
        cfg = regions.get(region_id)
        if cfg is None:
            raise HTTPException(status_code=404, detail=f"unknown region: {region_id}")
        return cfg

    def _store(region_id: str) -> LedgerStore:
        if region_id not in stores:
            stores[region_id] = LedgerStore(regions[region_id].ledger_dir)
        return stores[region_id]

    def _visible_cases(cfg: RegionConfig) -> dict:
        if cfg.region_id not in visible_cases:
            if cfg.visible_dataset_dir is None:
                raise ComponentError(f"no visible dataset configured for region {cfg.region_id}")
            visible_cases[cfg.region_id] = load_visible_cases(cfg.visible_dataset_dir)
        return visible_cases[cfg.region_id]

    @app.get("/table")
    def get_table(authorization: str | None = Header(default=None)):
        _auth(authorization)
        return [
            {
                "name": row.name,
                "emits": list(row.emits),
                "requires": [list(pair) for pair in row.requires],
                "deterministic": row.deterministic,
                "component": row.component,
            }
            for row in ACTION_TABLE
        ]

    @app.get("/status")
    def get_status(region: str, authorization: str | None = Header(default=None)):
        _auth(authorization)
        cfg = _region(region)
        store = _store(region)
        tree_sha, frozen_sha = current_tree_and_frozen(cfg.repo_dir, cfg.region_id, store, cfg.spec_path)
        return compute_status(
            store,
            tree=Subject(kind="tree", sha256=tree_sha),
            frozen=Subject(kind="frozen", sha256=frozen_sha),
        )

    @app.post("/submit")
    def post_submit(
        req: SubmitRequest,
        authorization: str | None = Header(default=None),
        x_session_id: str = Header(...),
        x_model_id: str = Header(...),
    ):
        _auth(authorization)
        cfg = _region(req.region)
        store = _store(req.region)
        allow_globs = resolve_allow_globs(store, cfg.spec_path)
        receipt = do_submit(cfg.repo_dir, cfg.region_id, req.working_copy_dir, allow_globs, x_session_id)

        store.append_request(RequestLogLine(
            ts=_now(), session=x_session_id, model=x_model_id, endpoint="submit", action="submit",
            region=req.region, tree=receipt.tree, config_hash=None, outcome="claim",
        ))
        return {
            "tree": receipt.tree,
            "frozen": receipt.frozen,
            "rejected": list(receipt.rejected),
            "committed": receipt.committed,
        }

    @app.post("/run")
    def post_run(
        req: RunRequest,
        authorization: str | None = Header(default=None),
        x_session_id: str = Header(...),
        x_model_id: str = Header(...),
    ):
        _auth(authorization)
        cfg = _region(req.region)
        store = _store(req.region)

        row = ROWS_BY_NAME.get(req.action)
        if row is None:
            raise HTTPException(status_code=400, detail=f"unknown action: {req.action}")
        if row.component is None:
            raise HTTPException(status_code=400, detail=f"'{req.action}' has no component; see GET /status")

        tree_sha, frozen_sha = current_tree_and_frozen(cfg.repo_dir, cfg.region_id, store, cfg.spec_path)
        subjects_by_kind = {
            "tree": Subject(kind="tree", sha256=tree_sha),
            "frozen": Subject(kind="frozen", sha256=frozen_sha),
        }
        cfg_hash = config_hash(req.config)

        def log(outcome, claim_id=None, missing=None):
            store.append_request(RequestLogLine(
                ts=_now(), session=x_session_id, model=x_model_id, endpoint="run", action=req.action,
                region=req.region, tree=tree_sha, config_hash=cfg_hash, outcome=outcome,
                claim_id=claim_id, missing=tuple(missing) if missing else None,
            ))

        missing = [
            item for predicate_type, subject_kind in row.requires
            if (item := requirement_status(
                store, predicate_type, subjects_by_kind[subject_kind], PRODUCERS.get(predicate_type),
            ))["status"] == "missing"
        ]
        if missing:
            log("refused", missing=missing)
            return {"refused": True, "action": req.action, "tree": tree_sha, "missing": missing}

        if row.deterministic:
            # All of a multi-emit row's claims are written together in one
            # dispatch (see sanitize, 6d), so an existing claim for any one
            # of them means all of them exist -- checking just the first
            # would also do, but checking every one is no more expensive
            # and doesn't rely on that invariant holding forever.
            existing = [
                store.find_duplicate(emitted, subjects_by_kind[SUBJECT_KIND_OF.get(emitted, "tree")], cfg_hash)
                for emitted in row.emits
            ]
            if existing and all(existing):
                duplicate = existing[0] if len(existing) == 1 else None
                if duplicate is not None:
                    log("duplicate", claim_id=duplicate.id)
                    return {"claim_id": duplicate.id, "verdict": duplicate.predicate.verdict, "detail": duplicate.predicate.detail}
                log("duplicate")
                return {"claims": [
                    {"predicateType": c.predicateType, "claim_id": c.id, "verdict": c.predicate.verdict, "detail": c.predicate.detail}
                    for c in existing
                ]}

        def record(predicate_type: str, subject: Subject, tool: str, result: dict, materials=()):
            return store.record_claim(
                [subject], predicate_type,
                Predicate(tool=tool, version="0.1", configHash=cfg_hash, verdict=result["verdict"], detail=result["detail"]),
                materials, x_session_id,
            )

        try:
            # row.component is never None here -- that case already raised
            # above -- so every action reaching this point has a strategy.
            strategy = load_strategy(cfg.strategy_path)
            ref = current_ref(cfg.repo_dir, cfg.region_id)

            if req.action == "sese_check":
                result = sese_check.check(cfg.repo_dir, ref, cfg.spec_path, strategy)
                # A pass widens the region's allow-list, which changes its
                # own frozen value (D5). The claim must be filed against
                # that new frozen value, not the one computed before this
                # check ran -- otherwise resolve_allow_globs would report
                # the claim's own existence as changing "current frozen"
                # out from under it, and the claim would immediately look
                # missing again. See the pi-ledger-plan-progress memory
                # note this resolves.
                subject_sha = (
                    frozen_for_allow_globs(cfg.repo_dir, result["allow_globs"])
                    if result["allow_globs"] is not None else frozen_sha
                )
                claim = record("sese/verified", Subject(kind="frozen", sha256=subject_sha), "sese_check", result)
                log("claim", claim_id=claim.id)
                return {"claim_id": claim.id, "verdict": claim.predicate.verdict, "detail": claim.predicate.detail}

            if req.action == "build_replay":
                if builder is None:
                    raise ComponentError("builder not configured")
                result = build_replay.check(cfg.repo_dir, ref, cfg.region_id, tree_sha, strategy, builder)
                claim = record("build/replay", subjects_by_kind["tree"], "builder", result, materials=[strategy.as_subject()])
                log("claim", claim_id=claim.id)
                return {"claim_id": claim.id, "verdict": claim.predicate.verdict, "detail": claim.predicate.detail}

            if req.action == "run_replay":
                if builder is None:
                    raise ComponentError("builder not configured")
                result = run_replay.check(cfg.region_id, tree_sha, strategy, _visible_cases(cfg), builder)
                claim = record("gpu/executed", subjects_by_kind["tree"], "builder", result, materials=[strategy.as_subject()])
                log("claim", claim_id=claim.id)
                return {"claim_id": claim.id, "verdict": claim.predicate.verdict, "detail": claim.predicate.detail}

            if req.action == "sanitize":
                if builder is None:
                    raise ComponentError("builder not configured")
                results = sanitize.check(cfg.region_id, tree_sha, strategy, _visible_cases(cfg), builder)
                claims = [
                    record(f"sanitize/{tool}", subjects_by_kind["tree"], "compute-sanitizer", result, materials=[strategy.as_subject()])
                    for tool, result in results.items()
                ]
                log("claim", claim_id=claims[0].id if len(claims) == 1 else None)
                return {"claims": [
                    {"predicateType": c.predicateType, "claim_id": c.id, "verdict": c.predicate.verdict, "detail": c.predicate.detail}
                    for c in claims
                ]}

            if req.action == "regression_visible":
                if oracle is None:
                    raise ComponentError("oracle not configured")
                result = regression.check_visible(store, subjects_by_kind["tree"], oracle)
                claim = record("regression/visible", subjects_by_kind["tree"], "oracle", result, materials=[strategy.as_subject()])
                log("claim", claim_id=claim.id)
                return {"claim_id": claim.id, "verdict": claim.predicate.verdict, "detail": claim.predicate.detail}

            if req.action == "regression_holdout":
                if builder is None or oracle is None:
                    raise ComponentError("builder or oracle not configured")
                result = regression.check_holdout(cfg.region_id, tree_sha, strategy, oracle, builder)
                claim = record("regression/holdout", subjects_by_kind["tree"], "oracle", result, materials=[strategy.as_subject()])
                log("claim", claim_id=claim.id)
                return {"claim_id": claim.id, "verdict": claim.predicate.verdict, "detail": claim.predicate.detail}

            if req.action == "time_port":
                if builder is None:
                    raise ComponentError("builder not configured")
                result = timing.check_port(cfg.region_id, tree_sha, builder)
                claim = record("timing/port", subjects_by_kind["tree"], "builder", result)
                log("claim", claim_id=claim.id)
                return {"claim_id": claim.id, "verdict": claim.predicate.verdict, "detail": claim.predicate.detail}

            if req.action == "time_baseline":
                if builder is None:
                    raise ComponentError("builder not configured")
                base_tree = baseline_tree_sha(cfg.repo_dir)
                result = timing.check_baseline(cfg.repo_dir, cfg.region_id, base_tree, builder)
                claim = record("timing/baseline", Subject(kind="tree", sha256=base_tree), "builder", result)
                log("claim", claim_id=claim.id)
                return {"claim_id": claim.id, "verdict": claim.predicate.verdict, "detail": claim.predicate.detail}
        except ComponentError as exc:
            log("error")
            return {"error": str(exc)}

        log("error")
        return {"error": f"component '{row.component}' for action '{req.action}' is not implemented yet"}

    return app
