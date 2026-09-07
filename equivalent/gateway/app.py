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

One request's work is written as three pieces: the RunContext that says
what is current, the gate that refuses or answers from what is already
filed, and the dispatch that runs a check and records what it said.
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass, replace
from datetime import datetime, timezone

from fastapi import FastAPI, Header, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from equivalent.components.context import CheckContext, CheckResult
from equivalent.components.phase import provenance_for
from equivalent.components.errors import ComponentError
from equivalent.ledger.acceptance import requirements_for
from equivalent.ledger.evidence import (
    FOUNDATION_PREDICATES,
    binary_materials,
    current_build_claim,
    dependent_materials,
)
from equivalent.ledger.capture_sets import SetReader
from equivalent.ledger.predicates import agent_receipt
from equivalent.ledger.records import Predicate, RequestLogLine
from equivalent.ledger.status import compute_status, requirement_status
from equivalent.ledger.store import LedgerStore
from equivalent.ledger.subjects import Subject, hash_bytes, is_digest
from equivalent.ledger.table import (
    ACTION_TABLE,
    ActionRow,
    check_config,
    config_params,
    requires_for,
    rows_for,
    subject_kind_of,
    subjects_by_kind,
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
    # Rejected by name for the same reason: a misspelled "config" that
    # was quietly dropped would run the action with its defaults and
    # report a success the caller never asked for.
    model_config = ConfigDict(extra="forbid")

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


def _validated_row(cfg: RegionConfig, req: "RunRequest") -> ActionRow:
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
    if not row.dispatchable:
        raise HTTPException(
            status_code=400, detail=f"'{req.action}' has no component; see GET /status",
        )
    problem = check_config(row, req.config)
    if problem is not None:
        raise HTTPException(status_code=400, detail=problem)
    return row


def _absent_backends(row: ActionRow, builder, oracle) -> list:
    """Which of the backends this action reaches this gateway has not got."""
    configured = {"builder": builder, "oracle": oracle}
    return [name for name in row.needs if configured[name] is None]


def _healthy_executor(builder) -> str:
    """The identity the builder is answering under, refused unless it is ready."""
    health = builder.healthz()
    if not health.ok:
        raise HTTPException(status_code=503, detail="builder executor is not ready")
    return health.executor_identity


def _verified_identity(fetch, what: str, field: str, pin: str | None) -> str:
    """One backend's identity, read now and held to the region's reviewed pin.

    A claim names the services that reached it, so this has to be the
    identity answering at this moment. An identity that cannot be read,
    is not a digest, or is not the one a person reviewed is not something
    to file a claim against, so each of those stops the request.
    """
    try:
        identity = fetch()
    except HTTPException:
        # The backend's own refusal already says what is wrong with it;
        # wrapping it would bury that behind "cannot establish".
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=503, detail=f"cannot establish {what} identity: {exc}",
        ) from exc
    if not identity:
        raise HTTPException(status_code=503, detail=f"{what} returned no {field}")
    if not is_digest(identity):
        raise HTTPException(status_code=503, detail=f"{what} returned an invalid {field}")
    if pin is not None and identity != pin:
        raise HTTPException(
            status_code=503,
            detail=f"{what} {field} does not match the reviewed region pin",
        )
    return identity


def _runtime_materials(cfg: RegionConfig, builder, oracle) -> tuple[tuple[Subject, ...], str | None]:
    """Identity of the remote services that execute and judge code.

    Answers with those identities as materials, and with the live
    executor identity itself: the artifact recheck below has to know
    which executor is answering right now, and asking the builder a
    second time in the same request could get a different answer.

    A region whose deployment pins an identity already carries it in the
    evidence materials, so only an unpinned one is added here.
    """
    oracle_judges = provenance_for(cfg.phase).oracle_judges
    if builder is None and cfg.executor_identity is not None:
        raise HTTPException(
            status_code=503,
            detail="region has a reviewed executor_identity but no builder is configured",
        )
    if oracle_judges and oracle is None and cfg.oracle_identity is not None:
        raise HTTPException(
            status_code=503,
            detail="region has a reviewed oracle_identity but no oracle is configured",
        )
    builder_identity = (
        _verified_identity(
            lambda: _healthy_executor(builder),
            "builder", "executor_identity", cfg.executor_identity,
        )
        if builder is not None else None
    )
    # An oracle answer that names no identity, or names something that is
    # not a digest, is not an oracle this gateway can pin: reading the
    # policy answer at all is what says so.
    oracle_identity = (
        _verified_identity(
            lambda: oracle.policy().oracle_identity,
            "oracle", "oracle_identity", cfg.oracle_identity,
        )
        if oracle is not None and oracle_judges else None
    )
    materials = []
    if builder_identity and cfg.executor_identity is None:
        materials.append(Subject(kind="executor", sha256=builder_identity))
    if oracle_identity and cfg.oracle_identity is None:
        materials.append(Subject(kind="oracle", sha256=oracle_identity))
    return tuple(materials), builder_identity


def _current_materials(cfg, strategy, baseline_strategy, builder, oracle):
    """Everything a claim filed for this region right now has to carry."""
    runtime, builder_identity = _runtime_materials(cfg, builder, oracle)
    return (
        *evidence_materials_for(cfg, strategy, baseline_strategy),
        *runtime,
    ), builder_identity


def _artifacts_still_held(builder, cfg, detail: dict, executor_identity) -> bool:
    """Recheck every claimed target against the builder's protected sidecar.

    A False here means the builder answered and its answer disagrees
    with the claim. A builder that cannot be reached has answered
    nothing, so that raises instead: reading it as a disagreement
    would tell the caller its build was lost when all that was lost
    was a request.
    """
    if builder is None:
        return False
    entries = provenance_for(cfg.phase).build_entries(detail)
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
        if not report.ok:
            return False
        if report.executor_identity != executor_identity:
            return False
        for target in targets.values():
            if not isinstance(target, dict):
                return False
            still_there = report.executables.get(target.get("executable"))
            if still_there is None or not still_there.matches(target):
                return False
    return True


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


@dataclass(frozen=True)
class RunContext:
    """One POST /run, resolved: the action it names and what is current.

    Everything here is read before the request decides anything, so the
    gate that refuses a request, the lookup that answers a repeat of one,
    and the dispatch that follows all read one and the same answer to
    "what is current right now". Reading it again halfway through would
    be a second answer, and the claim filed at the end would name a
    context nothing had been checked against -- so this is frozen, and
    the one thing a request can change about it, the build it rests on,
    is replaced as a whole when a lost build is rebuilt.
    """

    cfg: RegionConfig
    row: ActionRow
    store: LedgerStore
    # Who asked, in the words the request log records them under.
    session: str
    model: str
    tool_call_id: str | None
    config: dict
    config_hash: str
    strategy: object
    baseline_strategy: object
    # The commit every tree read and every dispatch uses, resolved once so
    # that a concurrent submit cannot move it mid-request.
    ref: str
    # The subjects this request's claims and preconditions are about, by
    # the names the table writes them in.
    subjects: dict
    # The backends, or None where this gateway has none configured.
    builder: object | None
    oracle: object | None
    # The executor answering right now, and the materials every claim of
    # this request has to carry.
    executor_identity: str | None
    evidence_materials: tuple
    # The current passing build claim and the executables it named.
    build_claim: object | None
    build_materials: tuple = ()

    @property
    def tree(self) -> Subject:
        return self.subjects["tree"]

    @property
    def build_predicate(self) -> str:
        return provenance_for(self.cfg.phase).build_predicate

    @property
    def rests_on_a_build(self) -> bool:
        """Whether what this action files is judged against the current build."""
        return any(
            predicate_type not in FOUNDATION_PREDICATES for predicate_type in self.row.emits
        )

    def materials_for(self, predicate_type: str) -> tuple:
        """The context a claim of this type has to have been reached under."""
        if predicate_type in FOUNDATION_PREDICATES:
            return self.evidence_materials
        return dependent_materials(self.evidence_materials, self.build_materials)

    def artifacts_match(self, detail: dict) -> bool:
        """Whether the builder still holds the executables this detail names."""
        return _artifacts_still_held(
            self.builder, self.cfg, detail, self.executor_identity,
        )

    def log(self, outcome: str, claim_id=None, missing=None) -> None:
        self.store.append_request(RequestLogLine(
            ts=_now(), session=self.session, model=self.model, endpoint="run",
            action=self.row.name, region=self.cfg.region_id, tree=self.tree.sha256,
            config_hash=self.config_hash, outcome=outcome, claim_id=claim_id,
            missing=tuple(missing) if missing else None, tool_call_id=self.tool_call_id,
        ))


def _restored_build(run: RunContext) -> RunContext:
    """The same request, with a build the builder still holds.

    The builder keeps one workspace per build and can lose it to a
    restart. Rebuilding is only accepted when it reproduces the very
    bytes the current claim named: anything else is a different
    executable, and letting a check run against it would file a verdict
    about a program nobody agreed was the one that passed.
    """
    if not run.rests_on_a_build or run.build_claim is None:
        return run
    detail = run.build_claim.predicate.detail
    if not run.build_materials:
        # Nothing to refuse and nothing to run: the build passed, so
        # asking for it again would name a requirement the session
        # already has, but it named no executable to run against.
        raise HTTPException(
            status_code=503,
            detail="the current passing build claim names no executable; "
                   "run the build action again before continuing",
        )
    if run.artifacts_match(detail):
        return run

    build_row = ROWS_BY_NAME[PRODUCERS[run.build_predicate]]
    # Asked before anything is rebuilt: a gateway with no builder cannot
    # rebuild and must say so rather than dispatch to a client it has not
    # got.
    absent = _absent_backends(build_row, run.builder, run.oracle)
    if absent:
        raise ComponentError(f"{' or '.join(absent)} not configured")
    claims = _claims_to_rest_on(run, build_row)
    if any(claim is None for claim in claims.values()):
        raise ComponentError(
            "the cached build was lost and what it rested on is no longer current; "
            "run the build action again before continuing"
        )
    rebuilt = HANDLERS[build_row.name].check(_check_context(run, claims), {})
    if rebuilt.verdict != "pass":
        raise ComponentError("the cached build was lost and rebuilding it did not pass")
    expected = binary_materials(detail)
    if binary_materials(rebuilt.detail) != expected:
        raise ComponentError(
            "the cached build was lost and rebuilding produced different executable bytes; "
            "run the build action again before continuing"
        )
    if not run.artifacts_match(rebuilt.detail):
        raise ComponentError("the builder did not retain the executables it just rebuilt")
    return replace(run, build_materials=expected)


def _missing_requirements(run: RunContext) -> list:
    """What this action needs and does not have, in words a session can act on.

    The build the action rests on is not on the row -- every dependent
    action rests on it -- so it is asked for here, and only when a
    session could do something about it.
    """
    rows = []
    if run.rests_on_a_build and run.build_claim is None:
        build_row = requirement_status(
            run.store, run.build_predicate, run.tree, PRODUCERS[run.build_predicate],
            required_materials=run.evidence_materials,
        )
        if build_row["status"] == "missing":
            rows.append(build_row)
    rows.extend(
        item for predicate_type, subject_kind in run.row.requires
        if (item := requirement_status(
            run.store, predicate_type, run.subjects[subject_kind],
            PRODUCERS.get(predicate_type),
            required_materials=run.materials_for(predicate_type),
        ))["status"] == "missing"
    )
    return rows


def _claims_already_filed(run: RunContext) -> list:
    """This exact request's claims, if every one of them is already there.

    All of a multi-emit row's claims are written together in one
    dispatch (see sanitize), so an existing claim for any one of them
    means all of them exist -- checking just the first would also do,
    but checking every one is no more expensive and doesn't rely on that
    invariant holding forever.
    """
    if not run.row.deterministic:
        return []
    existing = [
        run.store.find_duplicate(
            emitted, run.subjects[subject_kind_of(emitted)], run.config_hash,
            required_materials=run.materials_for(emitted),
        )
        for emitted in run.row.emits
    ]
    if not (existing and all(existing)):
        return []
    if run.build_predicate in run.row.emits and not run.artifacts_match(
        existing[0].predicate.detail,
    ):
        # A restart removed the workspace. Build again rather than answer
        # with a claim whose executable no longer exists.
        return []
    return existing


def _refusal_or_receipt(run: RunContext) -> dict | None:
    """This request's answer without dispatching anything, if it has one.

    A request whose evidence is not ready is refused with what is
    missing, and a repeat of a deterministic request is answered with
    the claims it already filed. Either way no check runs, which is why
    both are settled before the backends are asked for.
    """
    missing = _missing_requirements(run)
    if missing:
        run.log("refused", missing=missing)
        return {
            "refused": True, "action": run.row.name, "tree": run.tree.sha256, "missing": missing,
        }
    existing = _claims_already_filed(run)
    if not existing:
        return None
    if len(existing) == 1:
        run.log("duplicate", claim_id=existing[0].id)
        return _claim_response(existing[0])
    run.log("duplicate")
    return _claims_response(existing)


def _claims_to_rest_on(run: RunContext, row: ActionRow) -> dict:
    """Every claim this action rests on, read once under the current context.

    The check reads what it needs from these and never asks the ledger
    anything itself, which is what lets it trust that a claim it reads is
    a claim the gateway just checked.
    """
    claims = {
        predicate_type: run.store.latest(
            predicate_type, run.subjects[subject_kind],
            required_materials=run.materials_for(predicate_type),
        )
        for predicate_type, subject_kind in row.requires
    }
    if run.build_claim is not None:
        claims.setdefault(run.build_predicate, run.build_claim)
    return claims


def _check_context(run: RunContext, claims) -> CheckContext:
    """Everything the action's check is allowed to see."""
    return CheckContext(
        region_id=run.cfg.region_id,
        phase=run.cfg.phase,
        tree=Tree(run.cfg.repo_dir, run.ref),
        baseline=Tree.baseline(run.cfg.repo_dir),
        strategy=run.strategy,
        baseline_strategy=run.baseline_strategy,
        manifest=run.cfg.manifest,
        spec_path=run.cfg.spec_path,
        original_reference_path=run.cfg.original_reference_path,
        visible_dataset=run.cfg.visible_dataset_dir,
        builder=run.builder,
        oracle=run.oracle,
        claims=claims,
        sets=SetReader(run.store.capture_sets_dir),
    )


def _checked_results(run: RunContext, results: dict) -> None:
    """What is wrong with a check's answers, before anything is kept or filed.

    Both of these are settled before the first set is kept, so that an
    answer the gateway will not record leaves no bytes behind that no
    claim stands for. A claim filed against a subject this request does
    not have is the check and the table disagreeing about what the
    verdict is about, which is a mistake in the code and not a 500 for
    the session to read as a crash.
    """
    for predicate_type, result in results.items():
        if result.subject_kind not in run.subjects:
            raise ComponentError(
                f"the check for '{run.row.name}' answered about subject kind "
                f"'{result.subject_kind}', which this request has no subject for"
            )
        if (
            result.verdict == "pass"
            and predicate_type not in FOUNDATION_PREDICATES
            # A check that built and ran a program of its own measured a
            # binary the region's build never produced, and says so.
            and not result.measures_other_binaries
            and any(
                binary not in run.build_materials
                for binary in binary_materials(result.detail)
            )
        ):
            raise ComponentError(
                "the executable measured by the builder does not match the current "
                "passing build claim"
            )


def _record(run: RunContext, predicate_type: str, result: CheckResult, tool: str):
    """One claim, with everything the verdict rests on named in its materials.

    Every claim names the strategy and the code's manifest, so no call
    site lists them (or can forget them); what else a verdict rests on is
    the current build and what the check itself declared.
    """
    claim_materials = tuple(dict.fromkeys((
        *run.evidence_materials,
        *(run.build_materials if predicate_type not in FOUNDATION_PREDICATES else ()),
        *binary_materials(result.detail),
        *result.materials,
    )))
    return run.store.record_claim(
        [run.subjects[result.subject_kind]], predicate_type,
        Predicate(tool=tool, version="0.1", configHash=run.config_hash,
                  verdict=result.verdict, detail=result.detail),
        claim_materials, run.session,
    )


def _dispatch_and_record(run: RunContext) -> dict:
    """Run the action's check, keep what it packed, and file what it said."""
    handler = HANDLERS[run.row.name]
    try:
        # Asked here rather than earlier: a session whose evidence is not
        # ready should be told what is missing, which is something a
        # gateway with no backend can still answer.
        absent = _absent_backends(run.row, run.builder, run.oracle)
        if absent:
            raise ComponentError(f"{' or '.join(absent)} not configured")
        answer = handler.check(
            _check_context(run, _claims_to_rest_on(run, run.row)), run.config,
        )
        results = {run.row.emits[0]: answer} if isinstance(answer, CheckResult) else answer
        if run.build_predicate in run.row.emits:
            for result in results.values():
                if result.verdict == "pass" and not run.artifacts_match(result.detail):
                    raise ComponentError(
                        "the builder did not retain the executables from the passing build"
                    )
        _checked_results(run, results)
        # What the checks packed is filed before any claim names it, so a
        # claim never points at a set the ledger does not hold, and
        # nothing is kept for an answer that is about to be refused.
        for result in results.values():
            for packed in result.stores:
                run.store.keep(packed)
        filed = [
            (_record(run, predicate_type, result, handler.tool), result)
            for predicate_type, result in results.items()
        ]
    except ComponentError as exc:
        run.log("error")
        return {"error": str(exc)}

    if len(filed) == 1:
        claim, result = filed[0]
        run.log("claim", claim_id=claim.id)
        return {**_claim_response(claim), **_reasons(result)}
    run.log("claim")
    return {"claims": [
        {"predicateType": claim.predicateType, **_claim_response(claim), **_reasons(result)}
        for claim, result in filed
    ]}


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
        materials, executor_identity = _current_materials(
            cfg, strategy, baseline_strategy, builder, oracle,
        )
        tree_sha, frozen_sha = _current(
            cfg, store, strategy, required_materials=materials,
        )
        tree_subject = Subject(kind="tree", sha256=tree_sha)
        build_claim = current_build_claim(store, cfg.phase, tree_subject, materials)
        build_materials = (
            binary_materials(build_claim.predicate.detail) if build_claim else ()
        )
        # A build the builder still holds also says a builder answered:
        # nothing can match without one.
        build_context_ok = bool(
            build_claim and build_materials
            and _artifacts_still_held(
                builder, cfg, build_claim.predicate.detail, executor_identity,
            )
        )
        return compute_status(
            store, requirements_for(cfg.phase, cfg.manifest), cfg.phase,
            tree=tree_subject,
            frozen=Subject(kind="frozen", sha256=frozen_sha),
            required_materials=materials,
            # The gateway can answer the question the ledger cannot: it
            # has just asked the builder whether it still holds the
            # executables the current build claim named, and the live
            # executor and oracle identities are the reviewed pins.
            context_verified=bool(
                (not provenance_for(cfg.phase).oracle_judges or oracle is not None)
                and build_context_ok
            ),
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

    def _resolved_run(cfg, req, session, model, tool_call_id) -> RunContext:
        """Read what is current for this request, once, before it decides anything."""
        store = _store(req.region)
        row = _validated_row(cfg, req)
        strategy = load_strategy(cfg.strategy_path)
        baseline_strategy = load_strategy(cfg.baseline_strategy_path)
        evidence_materials, executor_identity = _current_materials(
            cfg, strategy, baseline_strategy, builder, oracle,
        )
        ref = current_commit(cfg.repo_dir, cfg.region_id)
        tree_sha, frozen_sha = _current(
            cfg, store, strategy, required_materials=evidence_materials, ref=ref,
        )
        subjects = subjects_by_kind(
            tree=tree_sha, frozen=frozen_sha, baseline_tree=Tree.baseline(cfg.repo_dir).sha,
        )
        build_claim = current_build_claim(
            store, cfg.phase, subjects["tree"], evidence_materials,
        )
        return RunContext(
            cfg=cfg, row=row, store=store,
            session=session, model=model, tool_call_id=tool_call_id,
            config=req.config, config_hash=config_hash(req.config),
            strategy=strategy, baseline_strategy=baseline_strategy, ref=ref,
            subjects=subjects, builder=builder, oracle=oracle,
            executor_identity=executor_identity, evidence_materials=evidence_materials,
            build_claim=build_claim,
            build_materials=(
                binary_materials(build_claim.predicate.detail) if build_claim else ()
            ),
        )

    def _post_run(
        cfg: RegionConfig,
        req: RunRequest,
        x_session_id: str,
        x_model_id: str,
        x_tool_call_id: str | None,
    ):
        run = _resolved_run(cfg, req, x_session_id, x_model_id, x_tool_call_id)
        try:
            run = _restored_build(run)
        except ComponentError as exc:
            run.log("error")
            return {"error": str(exc)}
        answered = _refusal_or_receipt(run)
        return answered if answered is not None else _dispatch_and_record(run)

    return app
