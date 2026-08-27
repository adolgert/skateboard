"""The gateway HTTP service.

Trust role: the reference monitor. This is the only thing an agent's
session can reach. Everything the agent or the person believes about a
region's progress comes from what this module reads and returns; a bug
here can make a bad port look accepted.

Step 5a builds three endpoints: GET /table, GET /status, POST /submit.
POST /run -- the part that reads the precondition table's `requires` and
decides whether to refuse, reuse a duplicate, or dispatch -- is Step 5b.
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

from equivalent.ledger.records import RequestLogLine
from equivalent.ledger.status import compute_status
from equivalent.ledger.store import LedgerStore
from equivalent.ledger.subjects import Subject

from .regions import RegionConfig
from .submit import current_tree_and_frozen, resolve_allow_globs
from .submit import submit as do_submit
from .table import ACTION_TABLE


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class SubmitRequest(BaseModel):
    region: str
    working_copy_dir: str


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

    return app
