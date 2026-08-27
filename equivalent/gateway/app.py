"""The gateway HTTP service.

Trust role: the reference monitor. This is the only thing an agent's
session can reach. Everything the agent or the person believes about a
region's progress comes from what this module reads and returns; a bug
here can make a bad port look accepted.

All four endpoints are built now: GET /table, GET /status, POST /submit,
POST /run. POST /run refuses a request whose required claims are missing,
returns an existing claim for a repeated deterministic request, and
otherwise reports that the action's component isn't wired up yet --
Step 6 replaces that placeholder with real dispatch, one action at a time.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

from equivalent.ledger.acceptance import ACCEPTANCE_REQUIREMENTS
from equivalent.ledger.records import RequestLogLine
from equivalent.ledger.status import compute_status, requirement_status
from equivalent.ledger.store import LedgerStore
from equivalent.ledger.subjects import Subject, hash_bytes

from .regions import RegionConfig
from .submit import current_tree_and_frozen, resolve_allow_globs
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


def create_app(regions: dict[str, RegionConfig], token: str) -> FastAPI:
    app = FastAPI(title="skateboard-gateway")
    stores: dict[str, LedgerStore] = {}

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

        missing = [
            item for predicate_type, subject_kind in row.requires
            if (item := requirement_status(
                store, predicate_type, subjects_by_kind[subject_kind], PRODUCERS.get(predicate_type),
            ))["status"] == "missing"
        ]

        duplicate = None
        if not missing and row.deterministic:
            emitted = row.emits[0]
            duplicate_subject = subjects_by_kind[SUBJECT_KIND_OF.get(emitted, "tree")]
            duplicate = store.find_duplicate(emitted, duplicate_subject, cfg_hash)

        if missing:
            outcome = "refused"
        elif duplicate is not None:
            outcome = "duplicate"
        else:
            outcome = "error"

        store.append_request(RequestLogLine(
            ts=_now(), session=x_session_id, model=x_model_id, endpoint="run", action=req.action,
            region=req.region, tree=tree_sha, config_hash=cfg_hash, outcome=outcome,
            claim_id=duplicate.id if duplicate is not None else None,
            missing=tuple(missing) if missing else None,
        ))

        if missing:
            return {"refused": True, "action": req.action, "tree": tree_sha, "missing": missing}
        if duplicate is not None:
            return {"claim_id": duplicate.id, "verdict": duplicate.predicate.verdict, "detail": duplicate.predicate.detail}
        return {"error": f"component '{row.component}' for action '{req.action}' is not implemented yet"}

    return app
