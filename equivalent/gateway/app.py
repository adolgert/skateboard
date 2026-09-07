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
equivalent.ledger.table.ACTION_TABLE that names a component has real
dispatch; an action whose builder or oracle client isn't configured for
this gateway instance answers that it isn't, rather than crashing.
"""
from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timezone

from fastapi import FastAPI, Header, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from equivalent.components import build_replay, harness_build
from equivalent.components.context import CheckContext, CheckResult
from equivalent.components.errors import ComponentError
from equivalent.ledger.acceptance import (
    ACCEPTANCE_REQUIREMENTS,
    CONDITIONAL_REQUIREMENTS,
    ONBOARDING_REQUIREMENTS,
    PORTING,
    requirements_for,
)
from equivalent.ledger.evidence import (
    BUILD_PREDICATE,
    FOUNDATION_PREDICATES,
    binary_materials,
    current_build_claim,
    required_materials_by_predicate,
)
from equivalent.ledger.capture_sets import SetReader
from equivalent.ledger.predicates import agent_receipt
from equivalent.ledger.records import Predicate, RequestLogLine
from equivalent.ledger.status import compute_status, requirement_status
from equivalent.ledger.store import LedgerStore
from equivalent.ledger.subjects import Subject, hash_bytes
from equivalent.ledger.table import (
    ACTION_TABLE,
    check_config,
    config_params,
    requires_for,
    rows_for,
)
from equivalent.region.config import RegionConfig
from equivalent.region.current import (
    current_commit,
    current_tree_and_frozen,
    resolve_allow_globs,
)
from equivalent.region.evidence import evidence_materials_for
from equivalent.strategy.schema import load_strategy
from equivalent.tree import Tree

from .dispatch import HANDLERS
from .submit import ConcurrentSubmissionError
from .submit import submit as do_submit

ROWS_BY_NAME = {row.name: row for row in ACTION_TABLE}
PRODUCERS = {predicate_type: row.name for row in ACTION_TABLE for predicate_type in row.emits}
# Which subject a predicate type's own claim is recorded against. Read
# from what the rows and the two phases' requirement lists already say,
# rather than a third hand-written copy: a baseline timing is about the
# baseline tree, and everything else about the candidate. Falls back to
# "tree" for a predicate nothing requires yet.
SUBJECT_KIND_OF = {
    **{
        predicate_type: subject_kind
        for row in ACTION_TABLE for predicate_type, subject_kind in row.requires
    },
    **{
        req.predicate_type: req.subject_kind
        for req in (*ACCEPTANCE_REQUIREMENTS, *CONDITIONAL_REQUIREMENTS, *ONBOARDING_REQUIREMENTS)
    },
}

def _reasons(result) -> dict:
    """Why a fail is a fail, in words, beside the claim.

    Only when there are any: a pass has nothing to say here, and neither
    has an answer that is a claim already filed rather than a check just
    run.
    """
    return {"reasons": list(result.reasons)} if result.reasons else {}


def _claim_response(claim) -> dict:
    """One claim as a /run response body, filtered by the receipt policy.

    The agent sees the verdict always, the detail only where the
    predicate registry allows it (regression/holdout is verdict-only).
    The full detail stays in claims.jsonl for the CLI.
    """
    return {"claim_id": claim.id, **agent_receipt(claim.predicateType, claim.predicate)}


def _build_entries(phase: str, detail: dict) -> list[tuple[str, dict]]:
    """The builder workspace and targets asserted by one build claim/result."""
    if phase == PORTING:
        return [(detail.get("attempt_id"), detail.get("targets", {}))]
    return [
        (one.get("attempt_id"), one.get("targets", {}))
        for _, one in sorted(detail.get("strategies", {}).items())
        if isinstance(one, dict)
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
    # A region/tree's actions reuse one stateful builder workspace. Keep
    # concurrent requests handled by this process from mutating or inspecting
    # that workspace at the same time.
    run_locks = {region_id: threading.Lock() for region_id in regions}

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

    def _runtime_materials(cfg) -> tuple[tuple[Subject, ...], str | None]:
        """Identity of the remote services that execute and judge code.

        Answers with those identities as materials, and with the live
        executor identity itself: the artifact recheck below has to know
        which executor is answering right now, and asking the builder a
        second time in the same request could get a different answer.
        """
        builder_identity = None
        oracle_identity = None
        if builder is None and getattr(cfg, "executor_identity", None) is not None:
            raise HTTPException(
                status_code=503,
                detail="region has a reviewed executor_identity but no builder is configured",
            )
        if (
            cfg.phase == "porting"
            and oracle is None
            and getattr(cfg, "oracle_identity", None) is not None
        ):
            raise HTTPException(
                status_code=503,
                detail="region has a reviewed oracle_identity but no oracle is configured",
            )
        if builder is not None:
            try:
                builder_health = builder.healthz()
            except Exception as exc:
                raise HTTPException(
                    status_code=503, detail=f"cannot establish builder identity: {exc}",
                ) from exc
            if builder_health.get("ok") is not True:
                raise HTTPException(status_code=503, detail="builder executor is not ready")
            builder_identity = builder_health.get("executor_identity")
            if not builder_identity:
                raise HTTPException(status_code=503, detail="builder returned no executor_identity")
            if (
                not isinstance(builder_identity, str)
                or re.fullmatch(r"[0-9a-f]{64}", builder_identity) is None
            ):
                raise HTTPException(
                    status_code=503, detail="builder returned an invalid executor_identity",
                )
            expected = getattr(cfg, "executor_identity", None)
            if expected is not None and builder_identity != expected:
                raise HTTPException(
                    status_code=503,
                    detail="builder executor_identity does not match the reviewed region pin",
                )
        if oracle is not None and cfg.phase == "porting":
            try:
                oracle_identity = oracle.policy().get("oracle_identity")
            except Exception as exc:
                raise HTTPException(
                    status_code=503, detail=f"cannot establish oracle identity: {exc}",
                ) from exc
            if not oracle_identity:
                raise HTTPException(status_code=503, detail="oracle returned no oracle_identity")
            if (
                not isinstance(oracle_identity, str)
                or re.fullmatch(r"[0-9a-f]{64}", oracle_identity) is None
            ):
                raise HTTPException(
                    status_code=503, detail="oracle returned an invalid oracle_identity",
                )
            expected = getattr(cfg, "oracle_identity", None)
            if expected is not None and oracle_identity != expected:
                raise HTTPException(
                    status_code=503,
                    detail="oracle oracle_identity does not match the reviewed region pin",
                )
        materials = []
        if builder_identity and getattr(cfg, "executor_identity", None) is None:
            materials.append(Subject(kind="executor", sha256=builder_identity))
        if oracle_identity and getattr(cfg, "oracle_identity", None) is None:
            materials.append(Subject(kind="oracle", sha256=oracle_identity))
        return tuple(materials), builder_identity

    def _current_materials(cfg, strategy, baseline_strategy):
        runtime, builder_identity = _runtime_materials(cfg)
        return (
            *evidence_materials_for(cfg, strategy, baseline_strategy),
            *runtime,
        ), builder_identity

    def _build_claim(
        cfg, store, tree_subject, core_materials,
    ):
        return current_build_claim(store, cfg.phase, tree_subject, core_materials)

    def _artifacts_match_build(cfg, detail: dict, executor_identity) -> bool:
        """Recheck every claimed target against the builder's protected sidecar.

        A False here means the builder answered and its answer disagrees
        with the claim. A builder that cannot be reached has answered
        nothing, so that raises instead: reading it as a disagreement
        would tell the caller its build was lost when all that was lost
        was a request.
        """
        if builder is None:
            return False
        entries = _build_entries(cfg.phase, detail)
        if not entries:
            return False
        for attempt_id, targets in entries:
            if not isinstance(attempt_id, str) or not isinstance(targets, dict) or not targets:
                return False
            try:
                report = builder.artifacts(attempt_id)
            except Exception as exc:
                raise HTTPException(
                    status_code=503,
                    detail=f"cannot read the builder's record of what it built: {exc}",
                ) from exc
            if report.get("ok") is not True:
                return False
            if report.get("executor_identity") != executor_identity:
                return False
            executables = report.get("executables", {})
            for target in targets.values():
                if not isinstance(target, dict):
                    return False
                actual = executables.get(target.get("executable"), {})
                if (
                    actual.get("verified") is not True
                    or actual.get("sha256") != target.get("sha256")
                    or actual.get("size") != target.get("size")
                ):
                    return False
        return True

    def _restore_build(cfg, ctx, expected_detail, executor_identity) -> tuple[Subject, ...]:
        """Rebuild a lost workspace, accepting it only when bytes reproduce."""
        rebuild = build_replay.check if cfg.phase == PORTING else harness_build.check
        rebuilt = rebuild(ctx, {})
        if rebuilt.verdict != "pass":
            raise ComponentError("the cached build was lost and rebuilding it did not pass")
        expected = binary_materials(expected_detail)
        actual = binary_materials(rebuilt.detail)
        if actual != expected:
            raise ComponentError(
                "the cached build was lost and rebuilding produced different executable bytes; "
                "run the build action again before continuing"
            )
        if not _artifacts_match_build(cfg, rebuilt.detail, executor_identity):
            raise ComponentError("the builder did not retain the executables it just rebuilt")
        return expected

    def _current(
        cfg: RegionConfig, store: LedgerStore, strategy, *, required_materials=(), ref=None,
    ) -> tuple[str, str]:
        """The region's current tree and frozen hashes.

        The strategy is one of the answers: an onboarding region's
        allow-list is the strategy's own, and the frozen set is whatever
        that list leaves uncovered.
        """
        return current_tree_and_frozen(
            cfg.repo_dir, cfg.region_id, store, cfg.spec_path, cfg.phase, strategy,
            required_materials=required_materials, ref=ref,
        )

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
        strategy = load_strategy(cfg.strategy_path)
        baseline_strategy = load_strategy(cfg.baseline_strategy_path)
        materials, executor_identity = _current_materials(cfg, strategy, baseline_strategy)
        tree_sha, frozen_sha = _current(
            cfg, store, strategy, required_materials=materials,
        )
        tree_subject = Subject(kind="tree", sha256=tree_sha)
        build_claim = _build_claim(cfg, store, tree_subject, materials)
        build_materials = (
            binary_materials(build_claim.predicate.detail) if build_claim else ()
        )
        build_context_ok = bool(
            build_claim and build_materials
            and _artifacts_match_build(
                cfg, build_claim.predicate.detail, executor_identity,
            )
        )
        status = compute_status(
            store, requirements_for(cfg.phase, cfg.manifest), cfg.phase,
            tree=tree_subject,
            frozen=Subject(kind="frozen", sha256=frozen_sha),
            required_materials=materials,
            required_materials_by_predicate=required_materials_by_predicate(
                store, requirements_for(cfg.phase, cfg.manifest), cfg.phase,
                tree_subject, materials,
            ),
        )
        status["context_verified"] = bool(
            builder is not None and (cfg.phase != "porting" or oracle is not None)
            and build_context_ok
        )
        if not status["context_verified"]:
            status["accepted"] = False
            status["note"] = (
                "live executor/oracle identity or the current build artifacts are unavailable; "
                "status is advisory"
            )
        return status

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
        strategy = load_strategy(cfg.strategy_path)
        baseline_strategy = load_strategy(cfg.baseline_strategy_path)
        # Submission only reuses the analyzer-approved allow-list; it does
        # not execute or compare code, so a temporarily unavailable backend
        # must not prevent the scientist from submitting the next candidate.
        materials = evidence_materials_for(cfg, strategy, baseline_strategy)
        allow_globs = resolve_allow_globs(
            store, cfg.spec_path, cfg.phase, strategy, required_materials=materials,
        )
        try:
            receipt = do_submit(
                cfg.repo_dir, cfg.region_id, cfg.working_copy_dir, allow_globs, x_session_id,
            )
        except ConcurrentSubmissionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

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
        # Authenticate and resolve before locking, so caller input cannot
        # create locks or reveal configured region ids, and so the region
        # is looked up once for the whole request.
        _auth(authorization)
        cfg = _region(req.region)
        with run_locks[req.region]:
            # The ledger-backed lock also covers gateways in other worker
            # processes that share this region and builder workspace.
            with _store(req.region).execution_lock():
                return _post_run(cfg, req, x_session_id, x_model_id, x_tool_call_id)

    def _validated_row(cfg: RegionConfig, req: RunRequest):
        """The row this request names, or the caller's mistake as a 400.

        None of these are refusals: a refusal says the region is not ready
        for an action it has, and each of these says the request was never
        one this endpoint could carry out. Nothing is written to the
        request log for one, because there is no action to log.
        """
        row = ROWS_BY_NAME.get(req.action)
        if row is None:
            raise HTTPException(status_code=400, detail=f"unknown action: {req.action}")
        if row.phase != cfg.phase:
            raise HTTPException(
                status_code=400,
                detail=f"action '{req.action}' belongs to phase '{row.phase}', but region "
                       f"'{req.region}' is in phase '{cfg.phase}'",
            )
        if row.component is None:
            raise HTTPException(
                status_code=400, detail=f"'{req.action}' has no component; see GET /status",
            )
        problem = check_config(row, req.config)
        if problem is not None:
            raise HTTPException(status_code=400, detail=problem)
        return row

    def _absent_backends(row) -> list:
        """Which of the backends this action reaches this gateway has not got."""
        configured = {"builder": builder, "oracle": oracle}
        return [name for name in row.needs if configured[name] is None]

    def _context(cfg: RegionConfig, store, ref, strategy, baseline_strategy, claims):
        """Everything the action's check is allowed to see."""
        return CheckContext(
            region_id=cfg.region_id,
            phase=cfg.phase,
            tree=Tree(cfg.repo_dir, ref),
            baseline=Tree.baseline(cfg.repo_dir),
            strategy=strategy,
            baseline_strategy=baseline_strategy,
            manifest=cfg.manifest,
            spec_path=cfg.spec_path,
            original_reference_path=cfg.original_reference_path,
            visible_dataset=cfg.visible_dataset_dir,
            builder=builder,
            oracle=oracle,
            claims=claims,
            sets=SetReader(store.capture_sets_dir),
        )

    def _post_run(
        cfg: RegionConfig,
        req: RunRequest,
        x_session_id: str,
        x_model_id: str,
        x_tool_call_id: str | None,
    ):
        store = _store(req.region)
        row = _validated_row(cfg, req)

        strategy = load_strategy(cfg.strategy_path)
        baseline_strategy = load_strategy(cfg.baseline_strategy_path)
        evidence_materials, executor_identity = _current_materials(
            cfg, strategy, baseline_strategy,
        )
        # Resolve once.  Every tree read and check dispatch below uses this
        # immutable commit even if a concurrent submit advances the branch.
        ref = current_commit(cfg.repo_dir, cfg.region_id)
        tree_sha, frozen_sha = _current(
            cfg, store, strategy, required_materials=evidence_materials, ref=ref,
        )
        subjects_by_kind = {
            "tree": Subject(kind="tree", sha256=tree_sha),
            "frozen": Subject(kind="frozen", sha256=frozen_sha),
            # A baseline timing is a claim about the pristine baseline, so
            # it is filed against that tree rather than the candidate.
            "baseline_tree": Subject(kind="tree", sha256=Tree.baseline(cfg.repo_dir).sha),
        }
        cfg_hash = config_hash(req.config)
        dependent_action = any(
            predicate_type not in FOUNDATION_PREDICATES for predicate_type in row.emits
        )
        build_predicate = BUILD_PREDICATE[cfg.phase]
        build_claim = _build_claim(
            cfg, store, subjects_by_kind["tree"], evidence_materials,
        )
        build_materials = (
            binary_materials(build_claim.predicate.detail) if build_claim else ()
        )

        def log(outcome, claim_id=None, missing=None):
            store.append_request(RequestLogLine(
                ts=_now(), session=x_session_id, model=x_model_id, endpoint="run", action=req.action,
                region=req.region, tree=tree_sha, config_hash=cfg_hash, outcome=outcome,
                claim_id=claim_id, missing=tuple(missing) if missing else None,
                tool_call_id=x_tool_call_id,
            ))

        if dependent_action and build_claim is not None and not build_materials:
            # Nothing to refuse and nothing to run: the build passed, so
            # asking for it again would name a requirement the session
            # already has, but it named no executable to run against.
            raise HTTPException(
                status_code=503,
                detail="the current passing build claim names no executable; "
                       "run the build action again before continuing",
            )

        if dependent_action and build_claim is not None and not _artifacts_match_build(
            cfg, build_claim.predicate.detail, executor_identity,
        ):
            try:
                build_materials = _restore_build(
                    cfg,
                    _context(cfg, store, ref, strategy, baseline_strategy, {}),
                    build_claim.predicate.detail, executor_identity,
                )
            except ComponentError as exc:
                log("error")
                return {"error": str(exc)}
        dependent_materials = (*evidence_materials, *build_materials)

        def materials_for(predicate_type):
            """The context a claim of this type has to have been reached under."""
            return (
                evidence_materials
                if predicate_type in FOUNDATION_PREDICATES else dependent_materials
            )

        def missing_requirements() -> list:
            """What this action needs and does not have, in words a session can act on.

            The build the action rests on is not on the row -- every
            dependent action rests on it -- so it is asked for here, and
            only when a session could do something about it.
            """
            rows = []
            if dependent_action and build_claim is None:
                build_row = requirement_status(
                    store, build_predicate, subjects_by_kind["tree"],
                    PRODUCERS[build_predicate], required_materials=evidence_materials,
                )
                if build_row["status"] == "missing":
                    rows.append(build_row)
            rows.extend(
                item for predicate_type, subject_kind in row.requires
                if (item := requirement_status(
                    store, predicate_type, subjects_by_kind[subject_kind],
                    PRODUCERS.get(predicate_type),
                    required_materials=materials_for(predicate_type),
                ))["status"] == "missing"
            )
            return rows

        missing = missing_requirements()
        if missing:
            log("refused", missing=missing)
            return {"refused": True, "action": req.action, "tree": tree_sha, "missing": missing}

        def claims_already_filed() -> list:
            """This exact request's claims, if every one of them is already there.

            All of a multi-emit row's claims are written together in one
            dispatch (see sanitize), so an existing claim for any one of
            them means all of them exist -- checking just the first would
            also do, but checking every one is no more expensive and
            doesn't rely on that invariant holding forever.
            """
            if not row.deterministic:
                return []
            existing = [
                store.find_duplicate(
                    emitted, subjects_by_kind[SUBJECT_KIND_OF.get(emitted, "tree")], cfg_hash,
                    required_materials=materials_for(emitted),
                )
                for emitted in row.emits
            ]
            if not (existing and all(existing)):
                return []
            if build_predicate in row.emits and not _artifacts_match_build(
                cfg, existing[0].predicate.detail, executor_identity,
            ):
                # A restart removed the workspace. Build again rather than
                # answer with a claim whose executable no longer exists.
                return []
            return existing

        existing = claims_already_filed()
        if existing:
            duplicate = existing[0] if len(existing) == 1 else None
            if duplicate is not None:
                log("duplicate", claim_id=duplicate.id)
                return _claim_response(duplicate)
            log("duplicate")
            return _claims_response(existing)

        # Every claim the action rests on, read once under the context the
        # refusal above just checked them in. The check reads what it needs
        # from these and never asks the ledger anything itself.
        claims = {
            predicate_type: store.latest(
                predicate_type, subjects_by_kind[subject_kind],
                required_materials=materials_for(predicate_type),
            )
            for predicate_type, subject_kind in row.requires
        }
        if build_claim is not None:
            claims.setdefault(build_predicate, build_claim)

        def record(predicate_type: str, result: CheckResult, tool: str):
            # Every claim names the strategy and the code's manifest in
            # its materials, so no call site lists them (or can forget
            # them); what else a verdict rests on is what the check itself
            # declared.
            measured_binaries = binary_materials(result.detail)
            if (
                result.verdict == "pass"
                and predicate_type not in FOUNDATION_PREDICATES
                and predicate_type != "harness/original"
                and measured_binaries
                and any(binary not in build_materials for binary in measured_binaries)
            ):
                raise ComponentError(
                    "the executable measured by the builder does not match the current "
                    "passing build claim"
                )
            claim_materials = tuple(dict.fromkeys((
                *evidence_materials,
                *(build_materials if predicate_type not in FOUNDATION_PREDICATES else ()),
                *measured_binaries,
                *result.materials,
            )))
            return store.record_claim(
                [subjects_by_kind[result.subject_kind]], predicate_type,
                Predicate(tool=tool, version="0.1", configHash=cfg_hash,
                          verdict=result.verdict, detail=result.detail),
                claim_materials, x_session_id,
            )

        handler = HANDLERS[row.name]
        try:
            # Asked here rather than earlier: a session whose evidence is
            # not ready should be told what is missing, which is something
            # a gateway with no backend can still answer.
            absent = _absent_backends(row)
            if absent:
                raise ComponentError(f"{' or '.join(absent)} not configured")
            answer = handler.check(
                _context(cfg, store, ref, strategy, baseline_strategy, claims), req.config,
            )
            results = (
                {row.emits[0]: answer} if isinstance(answer, CheckResult) else answer
            )
            if build_predicate in row.emits:
                for result in results.values():
                    if result.verdict == "pass" and not _artifacts_match_build(
                        cfg, result.detail, executor_identity,
                    ):
                        raise ComponentError(
                            "the builder did not retain the executables from the passing build"
                        )
            # What the checks packed is filed before any claim names it, so
            # a claim never points at a set the ledger does not hold.
            for result in results.values():
                for packed in result.stores:
                    store.keep(packed)
            claims_filed = [
                (record(predicate_type, result, handler.tool), result)
                for predicate_type, result in results.items()
            ]
        except ComponentError as exc:
            log("error")
            return {"error": str(exc)}

        if len(claims_filed) == 1:
            claim, result = claims_filed[0]
            log("claim", claim_id=claim.id)
            return {**_claim_response(claim), **_reasons(result)}
        log("claim")
        return {"claims": [
            {"predicateType": claim.predicateType, **_claim_response(claim), **_reasons(result)}
            for claim, result in claims_filed
        ]}

    return app
