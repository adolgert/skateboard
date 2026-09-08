"""Framework-free POST /run request execution."""
from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import datetime, timezone

from equivalent.components.context import CheckContext
from equivalent.components.errors import ComponentError
from equivalent.components.phase import provenance_for
from equivalent.components.result import CheckResult
from equivalent.ledger.acceptance import requirements_for
from equivalent.ledger.artifacts import build_binary_materials
from equivalent.ledger.capture_sets import SetReader
from equivalent.ledger.evidence import (
    FOUNDATION_PREDICATES, current_build_claim, dependent_materials, performance_materials,
)
from equivalent.ledger.predicates import agent_receipt
from equivalent.ledger.records import Predicate, RequestLogLine
from equivalent.ledger.status import requirement_status
from equivalent.ledger.store import LedgerStore
from equivalent.ledger.subjects import Subject, hash_bytes
from equivalent.ledger.table import ACTION_TABLE, ActionRow, check_config, subject_kind_of, subjects_by_kind
from equivalent.ledger.workflow import PRODUCERS
from equivalent.region.config import RegionConfig
from equivalent.region.current import current_commit, current_tree_and_frozen
from equivalent.strategy.schema import load_strategy
from equivalent.tree import Tree

from .dispatch import HANDLERS
from .errors import GatewayError
from .verification import artifacts_still_held, current_materials, records_still_held


ROWS_BY_NAME = {row.name: row for row in ACTION_TABLE}


@dataclass(frozen=True)
class RunRequest:
    action: str
    region: str
    config: dict


def config_hash(config: dict) -> str:
    return hash_bytes(json.dumps(config, sort_keys=True).encode("utf-8"))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def claim_response(claim) -> dict:
    return {"claim_id": claim.id, **agent_receipt(claim.predicateType, claim.predicate)}


def claims_response(claims) -> dict:
    return {"claims": [
        {"predicateType": claim.predicateType, **claim_response(claim)} for claim in claims
    ]}


def _reasons(result) -> dict:
    return {"reasons": list(result.reasons)} if result.reasons else {}


def validated_row(cfg, request: RunRequest) -> ActionRow:
    row = ROWS_BY_NAME.get(request.action)
    if row is None:
        raise GatewayError(400, f"unknown action: {request.action}")
    if row.phase != cfg.phase:
        raise GatewayError(
            400, f"action '{request.action}' belongs to phase '{row.phase}', but region "
            f"'{request.region}' is in phase '{cfg.phase}'",
        )
    if not row.dispatchable:
        raise GatewayError(400, f"'{request.action}' has no component; see GET /status")
    problem = check_config(row, request.config)
    if problem is not None:
        raise GatewayError(400, problem)
    return row


def _absent_backends(row: ActionRow, builder, oracle) -> list[str]:
    configured = {"builder": builder, "oracle": oracle}
    return [name for name in row.needs if configured[name] is None]


def current(cfg, store, strategy, *, required_materials=(), ref=None) -> tuple[str, str]:
    return current_tree_and_frozen(
        cfg.repo_dir, cfg.region_id, store, cfg.spec_path, cfg.phase, strategy,
        required_materials=required_materials, ref=ref,
    )


@dataclass(frozen=True)
class RunContext:
    cfg: RegionConfig
    row: ActionRow
    store: LedgerStore
    session: str
    model: str
    tool_call_id: str | None
    config: dict
    config_hash: str
    strategy: object
    baseline_strategy: object
    ref: str
    subjects: dict
    builder: object | None
    oracle: object | None
    executor_identity: str | None
    evidence_materials: tuple
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
        return any(predicate_type not in FOUNDATION_PREDICATES for predicate_type in self.row.emits)

    def materials_for(self, predicate_type: str) -> tuple:
        if predicate_type in FOUNDATION_PREDICATES:
            return self.evidence_materials
        materials = dependent_materials(self.evidence_materials, self.build_materials)
        if predicate_type == "performance/speedup":
            return (*materials, *performance_materials(
                self.store, self.tree, self.subjects["baseline_tree"], self.evidence_materials,
                port_materials=materials,
            ))
        return materials

    def artifacts_match(self, detail: dict) -> bool:
        return artifacts_still_held(
            self.builder, self.build_predicate, detail, self.executor_identity,
        )

    def records_match(self, records) -> bool:
        """Whether freshly declared build records are still retained."""
        return records_still_held(self.builder, records, self.executor_identity)

    def log(self, outcome: str, claim_id=None, missing=None) -> None:
        self.store.append_request(RequestLogLine(
            ts=_now(), session=self.session, model=self.model, endpoint="run",
            action=self.row.name, region=self.cfg.region_id, tree=self.tree.sha256,
            config_hash=self.config_hash, outcome=outcome, claim_id=claim_id,
            missing=tuple(missing) if missing else None, tool_call_id=self.tool_call_id,
        ))


class RunService:
    """Resolve and execute one action while keeping its current context fixed."""

    def __init__(self, *, builder=None, oracle=None, handlers=None):
        self.builder = builder
        self.oracle = oracle
        self.handlers = HANDLERS if handlers is None else handlers

    def resolve(self, cfg, store, request: RunRequest, *, session: str, model: str,
                tool_call_id: str | None = None) -> RunContext:
        row = validated_row(cfg, request)
        strategy = load_strategy(cfg.strategy_path)
        baseline_strategy = load_strategy(cfg.baseline_strategy_path)
        evidence_materials, executor_identity = current_materials(
            cfg, strategy, baseline_strategy, self.builder, self.oracle,
        )
        ref = current_commit(cfg.repo_dir, cfg.region_id)
        tree_sha, frozen_sha = current(
            cfg, store, strategy, required_materials=evidence_materials, ref=ref,
        )
        subjects = subjects_by_kind(
            tree=tree_sha, frozen=frozen_sha, baseline_tree=Tree.baseline(cfg.repo_dir).sha,
        )
        build_claim = current_build_claim(store, cfg.phase, subjects["tree"], evidence_materials)
        build_materials = (
            build_binary_materials(provenance_for(cfg.phase).build_predicate, build_claim.predicate.detail)
            if build_claim else ()
        )
        return RunContext(
            cfg=cfg, row=row, store=store, session=session, model=model, tool_call_id=tool_call_id,
            config=request.config, config_hash=config_hash(request.config),
            strategy=strategy, baseline_strategy=baseline_strategy, ref=ref, subjects=subjects,
            builder=self.builder, oracle=self.oracle, executor_identity=executor_identity,
            evidence_materials=evidence_materials, build_claim=build_claim,
            build_materials=build_materials,
        )

    def execute(self, run: RunContext) -> dict:
        try:
            run = self._restored_build(run)
        except ComponentError as exc:
            run.log("error")
            return {"error": str(exc)}
        answered = self._refusal_or_receipt(run)
        return answered if answered is not None else self._dispatch_and_record(run)

    def _restored_build(self, run: RunContext) -> RunContext:
        if not run.rests_on_a_build or run.build_claim is None:
            return run
        detail = run.build_claim.predicate.detail
        if not run.build_materials:
            raise GatewayError(
                503, "the current passing build claim names no executable; "
                "run the build action again before continuing",
            )
        if run.artifacts_match(detail):
            return run
        build_row = ROWS_BY_NAME[PRODUCERS[run.build_predicate]]
        absent = _absent_backends(build_row, run.builder, run.oracle)
        if absent:
            raise ComponentError(f"{' or '.join(absent)} not configured")
        claims = self._claims_to_rest_on(run, build_row)
        if any(claim is None for claim in claims.values()):
            raise ComponentError(
                "the cached build was lost and what it rested on is no longer current; "
                "run the build action again before continuing",
            )
        rebuilt = self.handlers[build_row.name].check(self._check_context(run, claims), {})
        if rebuilt.verdict != "pass":
            raise ComponentError("the cached build was lost and rebuilding it did not pass")
        expected = run.build_materials
        if rebuilt.binary_materials != expected:
            raise ComponentError(
                "the cached build was lost and rebuilding produced different executable bytes; "
                "run the build action again before continuing",
            )
        if not run.records_match(rebuilt.build_records):
            raise ComponentError("the builder did not retain the executables it just rebuilt")
        return replace(run, build_materials=expected)

    def _missing_requirements(self, run: RunContext) -> list:
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
                run.store, predicate_type, run.subjects[subject_kind], PRODUCERS.get(predicate_type),
                required_materials=run.materials_for(predicate_type),
            ))["status"] == "missing"
        )
        return rows

    def _claims_already_filed(self, run: RunContext) -> list:
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
        if run.build_predicate in run.row.emits and not run.artifacts_match(existing[0].predicate.detail):
            return []
        return existing

    def _refusal_or_receipt(self, run: RunContext) -> dict | None:
        missing = self._missing_requirements(run)
        if missing:
            run.log("refused", missing=missing)
            return {"refused": True, "action": run.row.name, "tree": run.tree.sha256, "missing": missing}
        existing = self._claims_already_filed(run)
        if not existing:
            return None
        if len(existing) == 1:
            run.log("duplicate", claim_id=existing[0].id)
            return claim_response(existing[0])
        run.log("duplicate")
        return claims_response(existing)

    def _claims_to_rest_on(self, run: RunContext, row: ActionRow) -> dict:
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

    @staticmethod
    def _check_context(run: RunContext, claims) -> CheckContext:
        return CheckContext(
            region_id=run.cfg.region_id, phase=run.cfg.phase,
            tree=Tree(run.cfg.repo_dir, run.ref), baseline=Tree.baseline(run.cfg.repo_dir),
            strategy=run.strategy, baseline_strategy=run.baseline_strategy, manifest=run.cfg.manifest,
            spec_path=run.cfg.spec_path, original_reference_path=run.cfg.original_reference_path,
            visible_dataset=run.cfg.visible_dataset_dir, builder=run.builder, oracle=run.oracle,
            claims=claims, sets=SetReader(run.store.capture_sets_dir),
        )

    @staticmethod
    def _checked_results(run: RunContext, results: dict) -> None:
        for predicate_type, result in results.items():
            if result.subject_kind not in run.subjects:
                raise ComponentError(
                    f"the check for '{run.row.name}' answered about subject kind "
                    f"'{result.subject_kind}', which this request has no subject for",
                )
            if (
                result.verdict == "pass" and predicate_type not in FOUNDATION_PREDICATES
                and not result.measures_other_binaries
                and any(binary not in run.build_materials for binary in result.binary_materials)
            ):
                raise ComponentError(
                    "the executable measured by the builder does not match the current "
                    "passing build claim",
                )

    @staticmethod
    def _record(run: RunContext, predicate_type: str, result: CheckResult, tool: str):
        claim_materials = tuple(dict.fromkeys((
            *run.evidence_materials,
            *(run.build_materials if predicate_type not in FOUNDATION_PREDICATES else ()),
            *result.binary_materials,
            *result.materials,
        )))
        return run.store.record_claim(
            [run.subjects[result.subject_kind]], predicate_type,
            Predicate(tool=tool, version="0.1", configHash=run.config_hash,
                      verdict=result.verdict, detail=result.detail),
            claim_materials, run.session,
        )

    def _dispatch_and_record(self, run: RunContext) -> dict:
        handler = self.handlers[run.row.name]
        try:
            absent = _absent_backends(run.row, run.builder, run.oracle)
            if absent:
                raise ComponentError(f"{' or '.join(absent)} not configured")
            answer = handler.check(self._check_context(run, self._claims_to_rest_on(run, run.row)), run.config)
            results = {run.row.emits[0]: answer} if isinstance(answer, CheckResult) else answer
            if run.build_predicate in run.row.emits:
                for result in results.values():
                    if result.verdict == "pass" and not run.records_match(result.build_records):
                        raise ComponentError("the builder did not retain the executables from the passing build")
            self._checked_results(run, results)
            for result in results.values():
                for packed in result.stores:
                    run.store.keep(packed)
            filed = [
                (self._record(run, predicate_type, result, handler.tool), result)
                for predicate_type, result in results.items()
            ]
        except ComponentError as exc:
            run.log("error")
            return {"error": str(exc)}
        if len(filed) == 1:
            claim, result = filed[0]
            run.log("claim", claim_id=claim.id)
            return {**claim_response(claim), **_reasons(result)}
        run.log("claim")
        return {"claims": [
            {"predicateType": claim.predicateType, **claim_response(claim), **_reasons(result)}
            for claim, result in filed
        ]}
