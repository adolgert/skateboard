"""The run service is usable without constructing an HTTP application."""
from __future__ import annotations

import ast
from dataclasses import replace
from pathlib import Path

import pytest

from equivalent.components.errors import ComponentError
from equivalent.components.result import CheckResult
from equivalent.gateway.errors import GatewayError
from equivalent.gateway.run import ROWS_BY_NAME, RunRequest, RunService, validated_row
from equivalent.ledger.artifacts import BinaryArtifact
from equivalent.ledger.store import LedgerStore
from equivalent.ledger.subjects import Subject
from equivalent.tests.fakes import write_program
from equivalent.tests.gateway.conftest import region_config
from equivalent.tree import init_baseline_repo
from equivalent.manifest.schema import load_manifest


def _cfg(tmp_path):
    seed = tmp_path / "seed"
    (seed / "src").mkdir(parents=True)
    (seed / "src" / "mod_kernel.f90").write_text("subroutine step\nend subroutine\n")
    repo_dir = tmp_path / "repo"
    init_baseline_repo(repo_dir, seed)
    working = tmp_path / "working"
    working.mkdir()
    return region_config(
        tmp_path, repo_dir=repo_dir, working_copy_dir=working,
        manifest=load_manifest(write_program(tmp_path) / "manifest.yaml"),
    )


def test_run_service_refuses_missing_evidence_without_an_http_app(tmp_path):
    cfg = _cfg(tmp_path)
    service = RunService()
    context = service.resolve(
        cfg, LedgerStore(cfg.ledger_dir), RunRequest("build_replay", cfg.region_id, {}),
        session="sess-1", model="model-1",
    )

    response = service.execute(context)

    assert response["refused"] is True
    assert response["missing"][0]["predicateType"] == "sese/verified"
    assert LedgerStore(cfg.ledger_dir).all_requests()[-1].outcome == "refused"


def test_domain_validation_has_http_data_but_no_fastapi_dependency(tmp_path):
    cfg = _cfg(tmp_path)

    try:
        validated_row(cfg, RunRequest("not-an-action", cfg.region_id, {}))
    except GatewayError as exc:
        assert (exc.status_code, exc.detail) == (400, "unknown action: not-an-action")
    else:
        raise AssertionError("unknown action must be rejected")

    for module in ("run.py", "verification.py"):
        source = (Path(__file__).parents[2] / "gateway" / module).read_text()
        tree = ast.parse(source)
        imported = {
            alias.name for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        }
        assert not any(name == "fastapi" or name.startswith("fastapi.") for name in imported)


def test_diagnostic_detail_never_becomes_binary_claim_material(tmp_path):
    cfg = _cfg(tmp_path)
    store = LedgerStore(cfg.ledger_dir)
    service = RunService()
    context = service.resolve(
        cfg, store, RunRequest("sese_check", cfg.region_id, {}), session="s", model="m",
    )

    claim = service._record(  # the gateway's sole claim-material construction path
        context, "sese/verified", CheckResult(
            verdict="pass",
            detail={
                "targets": {"diagnostic": {"executable": "not-a-build", "sha256": "d" * 64}},
                "executable_identity": {"executable": "also-not-a-build", "sha256": "e" * 64},
            },
        ), "test",
    )

    assert not [material for material in claim.materials if material.kind == "binary"]


def test_explicit_mismatched_binary_is_rejected_before_recording(tmp_path):
    cfg = _cfg(tmp_path)
    store = LedgerStore(cfg.ledger_dir)
    service = RunService()
    context = service.resolve(
        cfg, store, RunRequest("sese_check", cfg.region_id, {}), session="s", model="m",
    )
    dependent = replace(
        context, row=ROWS_BY_NAME["run_replay"],
        build_materials=(Subject(kind="binary", sha256="a" * 64),),
    )
    result = CheckResult(
        verdict="pass", detail={}, binary_artifacts=(BinaryArtifact("replay", "b" * 64),),
    )

    with pytest.raises(ComponentError, match="does not match the current passing build claim"):
        service._checked_results(dependent, {"gpu/executed": result})
    assert store.all_claims() == []
