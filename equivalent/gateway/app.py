"""The gateway HTTP service.

Trust role: the reference monitor. This is the only thing an agent's
session can reach. Everything the agent or the person believes about a
region's progress comes from what this module reads and returns; a bug
here can make a bad port look accepted.

The endpoints are GET /table, GET /status, GET /claims/{claim_id},
POST /submit, POST /run, and an unauthenticated GET /healthz for
container healthchecks. Both /table and /status name a region, because
what a session may do and what it must still do are both properties of
the region's phase; so does a claim read, which is a read of that
region's ledger. GET /claims/{claim_id} answers with the same receipt a
check's own result carries, so a session told only a verdict can go and
read why. POST /run
refuses a request whose required claims are missing, returns an existing
claim for a repeated deterministic request, and otherwise dispatches to
the action's component. Every row in
equivalent.gateway.table.ACTION_TABLE that names a component has real
dispatch; an action whose builder or oracle client isn't configured for
this gateway instance answers that it isn't, rather than crashing.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import FastAPI, Header, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from equivalent.components import (
    build_replay,
    harness_build,
    harness_capture,
    harness_determinism,
    harness_property,
    harness_replay,
    harness_self_check,
    harness_timing,
    manifest_check,
    program_regression,
    property_check,
    regression,
    run_replay,
    sanitize,
    sese_check,
    timing,
)
from equivalent.components.errors import ComponentError
from equivalent.gateway.datasets import load_visible_cases
from equivalent.ledger.acceptance import (
    ACCEPTANCE_REQUIREMENTS,
    CONDITIONAL_REQUIREMENTS,
    ONBOARDING_REQUIREMENTS,
    requirements_for,
)
from equivalent.ledger.predicates import agent_receipt
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
from .table import ACTION_TABLE, CONFIG_KEY_SPECS, config_params, requires_for, rows_for

ROWS_BY_NAME = {row.name: row for row in ACTION_TABLE}
PRODUCERS = {predicate_type: row.name for row in ACTION_TABLE for predicate_type in row.emits}
# Which subject a predicate type's own claim is recorded against -- e.g.
# sese/verified is scoped to "frozen", everything else in these lists to
# "tree". Reused from the two phases' own requirement lists rather than a
# third hand-written copy. Falls back to "tree" for anything not listed
# there: timing/baseline (nondeterministic, so it never reaches the
# duplicate check that uses this) and sanitize/initcheck (recorded on
# "tree", which is what the fallback says).
SUBJECT_KIND_OF = {
    req.predicate_type: req.subject_kind
    for req in (*ACCEPTANCE_REQUIREMENTS, *CONDITIONAL_REQUIREMENTS, *ONBOARDING_REQUIREMENTS)
}


def _claim_response(claim) -> dict:
    """One claim as a /run response body, filtered by the receipt policy.

    The agent sees the verdict always, the detail only where the
    predicate registry allows it (regression/holdout is verdict-only).
    The full detail stays in claims.jsonl for the CLI.
    """
    return {"claim_id": claim.id, **agent_receipt(claim.predicateType, claim.predicate)}


def _capture_set_materials(detail: dict) -> list:
    """The capture sets an onboarding claim rests on, as materials.

    Each of the four harness checks that reads or writes a dataset names
    it the same way in its detail, so this reads all four. A verdict
    reached against one set of captured arrays must not read as a verdict
    against another, which is what putting them in materials says.
    """
    return [
        Subject(kind="capture_set", sha256=entry["capture_set"])
        for _, entry in sorted(detail.get("datasets", {}).items())
        if entry.get("capture_set")
    ]


def _claim_read_response(claim) -> dict:
    """One claim read back by id, filtered by the same receipt policy.

    A read is the /run receipt plus what the claim is about: the
    predicate type, the subject it is a verdict on, and the materials it
    was reached against. Nothing here widens what the agent may see --
    a verdict-only predicate is still verdict-only when read back.
    """
    return {
        "predicateType": claim.predicateType,
        "subject": [s.to_dict() for s in claim.subject],
        "materials": [s.to_dict() for s in claim.materials],
        **_claim_response(claim),
    }


def _claims_response(claims) -> dict:
    return {"claims": [
        {"predicateType": c.predicateType, **_claim_response(c)} for c in claims
    ]}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def config_hash(config: dict) -> str:
    return hash_bytes(json.dumps(config, sort_keys=True).encode("utf-8"))


class SubmitRequest(BaseModel):
    # A submit names only the region. Which directory on the gateway's
    # side is read for it comes from the region's own configuration, so
    # a caller cannot point the gateway at a path of its choosing. An
    # extra field is a caller working from a stale idea of this endpoint,
    # so it is rejected by name rather than quietly ignored.
    model_config = ConfigDict(extra="forbid")

    region: str


class RunRequest(BaseModel):
    action: str
    region: str
    config: dict = {}


def _validation_detail(exc: RequestValidationError) -> str:
    """Name the offending fields, so a caller reads which part of its body was wrong."""
    parts = []
    for error in exc.errors():
        field = ".".join(str(item) for item in error["loc"][1:]) or "body"
        parts.append(f"{field}: {error['msg']}")
    return "; ".join(parts) or "malformed request body"


def create_app(regions: dict[str, RegionConfig], token: str, *, builder=None, oracle=None) -> FastAPI:
    """`builder` and `oracle` are BuilderClient/OracleClient-shaped objects
    (equivalent.gateway.backend_client), or None. An action whose
    component needs one that isn't configured answers with an error
    saying so, and files no claim -- a gateway can be brought up with the
    ledger and analyzer side working before the builder or the oracle are
    reachable.
    """
    app = FastAPI(title="equivalent-gateway")
    stores: dict[str, LedgerStore] = {}
    visible_cases: dict[str, dict] = {}

    @app.exception_handler(RequestValidationError)
    def malformed_request(request, exc: RequestValidationError):
        """A body that doesn't fit the endpoint is the caller's mistake, reported as 400.

        The default answer would be 422, which reads as "the request was
        understood but rejected"; a body with a field this endpoint does
        not have was never understood. Nothing is written to the request
        log for one of these: the handler never runs, so there is no
        region and no tree to log it against.
        """
        return JSONResponse(status_code=400, content={"detail": _validation_detail(exc)})

    @app.get("/healthz")
    def get_healthz():
        """Liveness only, and deliberately unauthenticated.

        Container healthchecks and the isolation checks need something to
        reach without a token. It reports nothing about any region.
        """
        return {"ok": True}

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

    def _current(cfg: RegionConfig, store: LedgerStore, strategy) -> tuple[str, str]:
        """The region's current tree and frozen hashes.

        The strategy is one of the answers: an onboarding region's
        allow-list is the strategy's own, and the frozen set is whatever
        that list leaves uncovered.
        """
        return current_tree_and_frozen(
            cfg.repo_dir, cfg.region_id, store, cfg.spec_path, cfg.phase, strategy,
        )

    def _visible_cases(cfg: RegionConfig) -> dict:
        if cfg.region_id not in visible_cases:
            if cfg.visible_dataset_dir is None:
                raise ComponentError(f"no visible dataset configured for region {cfg.region_id}")
            visible_cases[cfg.region_id] = load_visible_cases(cfg.visible_dataset_dir, cfg.manifest)
        return visible_cases[cfg.region_id]

    @app.get("/table")
    def get_table(region: str | None = None, authorization: str | None = Header(default=None)):
        """The action rows of one region's phase.

        The region is required: an onboarding session and a porting
        session have different actions, and a table served without one
        would have to be either both at once or a guess.
        """
        _auth(authorization)
        if region is None:
            raise HTTPException(
                status_code=400,
                detail="GET /table needs a region: the actions a session has are the "
                       "actions of its region's phase",
            )
        cfg = _region(region)
        return [
            {
                "name": row.name,
                "emits": list(row.emits),
                "requires": [list(pair) for pair in requires_for(row, cfg.manifest)],
                "deterministic": row.deterministic,
                "component": row.component,
                # The settings this action takes, and what each one means.
                # A client that offers them to a model reads the wording
                # from here rather than repeating it, and POST /run checks
                # a request against the same list.
                "config_keys": list(row.config_keys),
                "config_params": config_params(row),
            }
            for row in rows_for(cfg.phase)
        ]

    @app.get("/status")
    def get_status(region: str, authorization: str | None = Header(default=None)):
        _auth(authorization)
        cfg = _region(region)
        store = _store(region)
        tree_sha, frozen_sha = _current(cfg, store, load_strategy(cfg.strategy_path))
        return compute_status(
            store, requirements_for(cfg.phase, cfg.manifest), cfg.phase,
            tree=Subject(kind="tree", sha256=tree_sha),
            frozen=Subject(kind="frozen", sha256=frozen_sha),
        )

    @app.get("/claims/{claim_id}")
    def get_claim(
        claim_id: str,
        region: str,
        authorization: str | None = Header(default=None),
        x_session_id: str = Header(...),
        x_model_id: str = Header(...),
        x_tool_call_id: str | None = Header(default=None),
    ):
        """One claim of one region, read back by id.

        A verdict on its own is not something a session can act on: the
        reason a check failed is in the claim's detail. This reads it
        back through the same receipt policy the check's own answer went
        through, so reading a claim can never show more than the answer
        did. The claim is looked up in the named region's ledger, so an
        id from another region is simply not found here.
        """
        _auth(authorization)
        _region(region)
        store = _store(region)
        claim = store.get_claim(claim_id)

        store.append_request(RequestLogLine(
            ts=_now(), session=x_session_id, model=x_model_id, endpoint="claim", action="claim",
            region=region, tree=None, config_hash=None,
            outcome="read" if claim is not None else "error",
            claim_id=claim_id, tool_call_id=x_tool_call_id,
        ))
        if claim is None:
            raise HTTPException(status_code=404, detail=f"unknown claim: {claim_id}")
        return _claim_read_response(claim)

    @app.post("/submit")
    def post_submit(
        req: SubmitRequest,
        authorization: str | None = Header(default=None),
        x_session_id: str = Header(...),
        x_model_id: str = Header(...),
        x_tool_call_id: str | None = Header(default=None),
    ):
        _auth(authorization)
        cfg = _region(req.region)
        store = _store(req.region)
        allow_globs = resolve_allow_globs(
            store, cfg.spec_path, cfg.phase, load_strategy(cfg.strategy_path),
        )
        receipt = do_submit(cfg.repo_dir, cfg.region_id, cfg.working_copy_dir, allow_globs, x_session_id)

        store.append_request(RequestLogLine(
            ts=_now(), session=x_session_id, model=x_model_id, endpoint="submit", action="submit",
            region=req.region, tree=receipt.tree, config_hash=None, outcome="submitted",
            tool_call_id=x_tool_call_id,
        ))
        return {
            "tree": receipt.tree,
            "frozen": receipt.frozen,
            "rejected": list(receipt.rejected),
            "not_sent": list(receipt.not_sent),
            "committed": receipt.committed,
        }

    @app.post("/run")
    def post_run(
        req: RunRequest,
        authorization: str | None = Header(default=None),
        x_session_id: str = Header(...),
        x_model_id: str = Header(...),
        x_tool_call_id: str | None = Header(default=None),
    ):
        _auth(authorization)
        cfg = _region(req.region)
        store = _store(req.region)

        row = ROWS_BY_NAME.get(req.action)
        if row is None:
            raise HTTPException(status_code=400, detail=f"unknown action: {req.action}")
        if row.component is None:
            raise HTTPException(status_code=400, detail=f"'{req.action}' has no component; see GET /status")
        unknown_keys = sorted(set(req.config) - set(row.config_keys))
        if unknown_keys:
            # Rejected like a malformed request (no log line), not refused:
            # an unvalidated config key would hash into duplicate detection
            # and let an identical request look new.
            raise HTTPException(
                status_code=400,
                detail=f"'{req.action}' does not accept config key(s) {unknown_keys}; "
                       f"allowed: {sorted(row.config_keys)}",
            )
        for key in sorted(req.config):
            # Values are checked as well as names: a "repeats" of "lots"
            # would hash into duplicate detection and then fail deep in a
            # component, where the caller reads a traceback instead of
            # which key it got wrong.
            value = req.config[key]
            if CONFIG_KEY_SPECS[key]["type"] == "integer" and (
                isinstance(value, bool) or not isinstance(value, int)
            ):
                raise HTTPException(
                    status_code=400,
                    detail=f"config key '{key}' of '{req.action}' must be an integer; "
                           f"got {value!r}",
                )

        strategy = load_strategy(cfg.strategy_path)
        tree_sha, frozen_sha = _current(cfg, store, strategy)
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
                tool_call_id=x_tool_call_id,
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
            # dispatch (see sanitize), so an existing claim for any one
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
                    return _claim_response(duplicate)
                log("duplicate")
                return _claims_response(existing)

        def record(predicate_type: str, subject: Subject, tool: str, result: dict, materials=()):
            # Every claim names the strategy and the code's manifest in
            # its materials, so no call site lists them (or can forget
            # them). `strategy` is loaded below, before any dispatch
            # branch calls this; the manifest came with the region.
            return store.record_claim(
                [subject], predicate_type,
                Predicate(tool=tool, version="0.1", configHash=cfg_hash, verdict=result["verdict"], detail=result["detail"]),
                (strategy.as_subject(), cfg.manifest.as_subject(), *materials), x_session_id,
            )

        try:
            ref = current_ref(cfg.repo_dir, cfg.region_id)

            if req.action == "sese_check":
                result = sese_check.check(cfg.repo_dir, ref, cfg.spec_path, strategy)
                # A pass widens the region's allow-list, which changes its
                # own frozen value. The claim must be filed against that
                # new frozen value, not the one computed before this check
                # ran -- otherwise resolve_allow_globs would report the
                # claim's own existence as changing "current frozen" out
                # from under it, and the claim would immediately look
                # missing again.
                subject_sha = (
                    frozen_for_allow_globs(cfg.repo_dir, result["allow_globs"])
                    if result["allow_globs"] is not None else frozen_sha
                )
                claim = record("sese/verified", Subject(kind="frozen", sha256=subject_sha), "sese_check", result)
                log("claim", claim_id=claim.id)
                return _claim_response(claim)

            if req.action == "build_replay":
                if builder is None:
                    raise ComponentError("builder not configured")
                result = build_replay.check(
                    cfg.repo_dir, ref, cfg.region_id, tree_sha, strategy, cfg.manifest, builder,
                )
                claim = record("build/replay", subjects_by_kind["tree"], "builder", result)
                log("claim", claim_id=claim.id)
                return _claim_response(claim)

            if req.action == "run_replay":
                if builder is None:
                    raise ComponentError("builder not configured")
                result = run_replay.check(
                    cfg.region_id, tree_sha, strategy, cfg.manifest, _visible_cases(cfg), builder,
                )
                claim = record("gpu/executed", subjects_by_kind["tree"], "builder", result)
                log("claim", claim_id=claim.id)
                return _claim_response(claim)

            if req.action == "sanitize":
                if builder is None:
                    raise ComponentError("builder not configured")
                results = sanitize.check(
                    cfg.region_id, tree_sha, strategy, cfg.manifest, _visible_cases(cfg), builder,
                )
                claims = [
                    record(f"sanitize/{tool}", subjects_by_kind["tree"], "compute-sanitizer", result)
                    for tool, result in results.items()
                ]
                log("claim", claim_id=claims[0].id if len(claims) == 1 else None)
                return _claims_response(claims)

            if req.action == "regression_visible":
                if oracle is None:
                    raise ComponentError("oracle not configured")
                result = regression.check_visible(store, subjects_by_kind["tree"], oracle)
                # The tolerance policy shaped this verdict, so it is a
                # formal material, not just a note in detail.
                policy = Subject(kind="policy", sha256=result["detail"]["policy_sha256"])
                claim = record("regression/visible", subjects_by_kind["tree"], "oracle", result, materials=[policy])
                log("claim", claim_id=claim.id)
                return _claim_response(claim)

            if req.action == "property_check":
                if builder is None:
                    raise ComponentError("builder not configured")
                # A seed the request names is the same search again, and
                # the config hash carries it, so a repeat at that seed
                # comes back as the claim already filed. A request that
                # names none has one drawn in the component and written
                # into the claim, which is how a person reads back the
                # search that failed and asks for it again.
                result = property_check.check(
                    cfg.region_id, tree_sha, cfg.manifest, _visible_cases(cfg), builder,
                    seed=req.config.get("seed"),
                    max_examples=req.config.get(
                        "max_examples", property_check.DEFAULT_MAX_EXAMPLES,
                    ),
                )
                claim = record("regression/property", subjects_by_kind["tree"], "builder", result)
                log("claim", claim_id=claim.id)
                return _claim_response(claim)

            if req.action == "regression_holdout":
                if builder is None or oracle is None:
                    raise ComponentError("builder or oracle not configured")
                result = regression.check_holdout(
                    cfg.region_id, tree_sha, strategy, cfg.manifest, oracle, builder,
                )
                policy = Subject(kind="policy", sha256=result["detail"]["policy_sha256"])
                claim = record("regression/holdout", subjects_by_kind["tree"], "oracle", result, materials=[policy])
                log("claim", claim_id=claim.id)
                return _claim_response(claim)

            if req.action == "program_regression":
                if builder is None:
                    raise ComponentError("builder not configured")
                result = program_regression.check(
                    store, Subject(kind="tree", sha256=baseline_tree_sha(cfg.repo_dir)),
                    cfg.region_id, tree_sha, cfg.manifest, builder,
                )
                # The two things this verdict rests on, as formal
                # materials: the bands it was judged within, and the
                # baseline run it was judged against.
                claim = record(
                    "program/regression", subjects_by_kind["tree"], "builder", result,
                    materials=[
                        Subject(kind="policy", sha256=result["detail"]["policy_sha256"]),
                        Subject(kind="capture_set", sha256=result["detail"]["program_set"]),
                    ],
                )
                log("claim", claim_id=claim.id)
                return _claim_response(claim)

            if req.action == "time_port":
                if builder is None:
                    raise ComponentError("builder not configured")
                result = timing.check_port(
                    store, subjects_by_kind["tree"], cfg.region_id, tree_sha, cfg.manifest,
                    builder, repeats=int(req.config.get("repeats", 5)),
                )
                claim = record("timing/port", subjects_by_kind["tree"], "builder", result)
                log("claim", claim_id=claim.id)
                return _claim_response(claim)

            if req.action == "time_baseline":
                if builder is None:
                    raise ComponentError("builder not configured")
                base_tree = baseline_tree_sha(cfg.repo_dir)
                # The floor a speedup is measured against is a strategy
                # file of the region's own choosing, loaded here so the
                # claim says which one it was.
                result = timing.check_baseline(
                    store, cfg.repo_dir, cfg.region_id, base_tree, cfg.manifest,
                    load_strategy(cfg.baseline_strategy_path), builder,
                    repeats=int(req.config.get("repeats", 5)),
                )
                # The program set this run stored is what a port's own
                # program run is compared against, so it is a formal
                # material rather than a note in the detail.
                program_set = result["detail"].get("program_set")
                claim = record(
                    "timing/baseline", Subject(kind="tree", sha256=base_tree), "builder", result,
                    materials=(
                        [Subject(kind="capture_set", sha256=program_set)] if program_set else []
                    ),
                )
                log("claim", claim_id=claim.id)
                return _claim_response(claim)

            # The onboarding actions.
            if req.action == "manifest_check":
                # No builder: the manifest is read on this side, where the
                # gateway already has the tree and the loader that knows
                # what a manifest has to say.
                result = manifest_check.check(cfg.repo_dir, ref)
                claim = record("manifest/valid", subjects_by_kind["tree"], "manifest_check", result)
                log("claim", claim_id=claim.id)
                return _claim_response(claim)

            if req.action == "harness_build":
                if builder is None:
                    raise ComponentError("builder not configured")
                result = harness_build.check(
                    cfg.repo_dir, ref, cfg.region_id, tree_sha, strategy,
                    load_strategy(cfg.baseline_strategy_path), builder,
                )
                claim = record("harness/builds", subjects_by_kind["tree"], "builder", result)
                log("claim", claim_id=claim.id)
                return _claim_response(claim)

            if req.action == "harness_capture":
                if builder is None:
                    raise ComponentError("builder not configured")
                result = harness_capture.check(
                    store, cfg.repo_dir, ref, cfg.region_id, tree_sha,
                    load_strategy(cfg.baseline_strategy_path), builder,
                )
                # The sets this check stored are what every later claim
                # about this tree compares against, so they are formal
                # materials rather than a note in the detail.
                claim = record(
                    "harness/captured", subjects_by_kind["tree"], "builder", result,
                    materials=_capture_set_materials(result["detail"]),
                )
                log("claim", claim_id=claim.id)
                return _claim_response(claim)

            if req.action == "harness_replay":
                if builder is None:
                    raise ComponentError("builder not configured")
                result = harness_replay.check(
                    store, subjects_by_kind["tree"], cfg.repo_dir, ref, cfg.region_id, tree_sha,
                    load_strategy(cfg.baseline_strategy_path), builder,
                )
                claim = record(
                    "harness/replays", subjects_by_kind["tree"], "builder", result,
                    materials=_capture_set_materials(result["detail"]),
                )
                log("claim", claim_id=claim.id)
                return _claim_response(claim)

            if req.action == "harness_determinism":
                if builder is None:
                    raise ComponentError("builder not configured")
                result = harness_determinism.check(
                    store, subjects_by_kind["tree"], cfg.repo_dir, ref, cfg.region_id, tree_sha,
                    load_strategy(cfg.baseline_strategy_path), builder,
                )
                claim = record(
                    "harness/deterministic", subjects_by_kind["tree"], "builder", result,
                    materials=_capture_set_materials(result["detail"]),
                )
                log("claim", claim_id=claim.id)
                return _claim_response(claim)

            if req.action == "harness_timing":
                if builder is None:
                    raise ComponentError("builder not configured")
                result = harness_timing.check(
                    store, cfg.repo_dir, ref, cfg.region_id, tree_sha,
                    load_strategy(cfg.baseline_strategy_path), builder,
                )
                claim = record(
                    "harness/times", subjects_by_kind["tree"], "builder", result,
                    materials=_capture_set_materials(result["detail"]),
                )
                log("claim", claim_id=claim.id)
                return _claim_response(claim)

            if req.action == "harness_self_check":
                if builder is None:
                    raise ComponentError("builder not configured")
                # `limit` scores only the first mutants. It is for a
                # session finding its feet on a large region; the claim
                # says how many there were, so a limited run cannot be
                # mistaken for a whole one.
                result = harness_self_check.check(
                    store, subjects_by_kind["tree"], cfg.repo_dir, ref, cfg.region_id,
                    tree_sha, load_strategy(cfg.baseline_strategy_path), builder,
                    limit=req.config.get("limit"),
                )
                # The two things this verdict rests on: the answers the
                # mutants were scored against, and the bands that decided
                # whether a changed answer counted.
                claim = record(
                    "harness/self_check", subjects_by_kind["tree"], "builder", result,
                    materials=[
                        *_capture_set_materials(result["detail"]),
                        Subject(kind="policy", sha256=result["detail"]["policy_sha256"]),
                    ],
                )
                log("claim", claim_id=claim.id)
                return _claim_response(claim)

            if req.action == "harness_property":
                if builder is None:
                    raise ComponentError("builder not configured")
                # As in the porting check, a seed the request names is the
                # same search again and the config hash carries it; a
                # request that names none has one drawn and written into
                # the claim.
                result = harness_property.check(
                    store, subjects_by_kind["tree"], cfg.repo_dir, ref, cfg.region_id,
                    tree_sha, load_strategy(cfg.baseline_strategy_path), builder,
                    seed=req.config.get("seed"),
                    max_examples=req.config.get(
                        "max_examples", property_check.DEFAULT_MAX_EXAMPLES,
                    ),
                )
                claim = record("harness/properties", subjects_by_kind["tree"], "builder", result)
                log("claim", claim_id=claim.id)
                return _claim_response(claim)
        except ComponentError as exc:
            log("error")
            return {"error": str(exc)}

        log("error")
        return {"error": f"component '{row.component}' for action '{req.action}' is not implemented yet"}

    return app
