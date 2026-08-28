"""One region, start to acceptance, through every real component
dispatch, run against fakes standing in for the builder and oracle (see
equivalent/tests/fakes.py for why: no nvfortran/compute-sanitizer/GPU in
this environment).
"""
import json
from pathlib import Path

from fastapi.testclient import TestClient

from equivalent.gateway.app import create_app
from equivalent.gateway.regions import RegionConfig
from equivalent.gateway.submit import init_baseline_repo
from equivalent.ledger.store import LedgerStore
from equivalent.manifest.schema import load_manifest
from equivalent.strategy.schema import load_strategy
from equivalent.tests.fakes import FakeBuilder, FakeOracle, write_program

TOKEN = "test-token"
HEADERS = {"Authorization": f"Bearer {TOKEN}", "X-Session-Id": "sess-1", "X-Model-Id": "claude-sonnet-5"}
STRATEGY_PATH = Path(__file__).resolve().parents[2] / "strategy" / "files" / "stdpar_managed.yaml"
BASELINE_STRATEGY_PATH = STRATEGY_PATH.parent / "cpu_reference.yaml"
SPEC_PATH = "notes/regions/ch04-step.sese.yaml"

CLEAN_SOURCE = """\
module mod_kernel
contains
subroutine step(a, b)
  real :: a, b
  a = a + b
end subroutine step
end module mod_kernel
"""

SPEC = (
    "region: ch04:step\n"
    "files:\n"
    "  - src/mod_kernel.f90\n"
    "anchor:\n"
    "  file: src/mod_kernel.f90\n"
    '  pst_node: "step@3-5"\n'
    "  entry_symbol: step\n"
)


def _client(tmp_path):
    seed = tmp_path / "seed"
    (seed / "src").mkdir(parents=True)
    (seed / "src" / "mod_kernel.f90").write_text(CLEAN_SOURCE)
    (seed / "notes" / "regions").mkdir(parents=True)
    (seed / SPEC_PATH).write_text(SPEC)

    repo_dir = tmp_path / "repo"
    init_baseline_repo(repo_dir, seed)
    working = tmp_path / "working"
    working.mkdir()
    program = write_program(tmp_path)
    cfg = RegionConfig(
        region_id="ch04:step", repo_dir=repo_dir, spec_path=SPEC_PATH,
        ledger_dir=tmp_path / "ledger", strategy_path=STRATEGY_PATH,
        baseline_strategy_path=BASELINE_STRATEGY_PATH,
        working_copy_dir=working,
        manifest=load_manifest(program / "manifest.yaml"),
        visible_dataset_dir=program / "datasets" / "visible",
    )
    builder, oracle = FakeBuilder(), FakeOracle()
    client = TestClient(create_app({cfg.region_id: cfg}, TOKEN, builder=builder, oracle=oracle))
    store = LedgerStore(cfg.ledger_dir)
    return client, cfg, store, builder, oracle


def _run(client, cfg, action):
    r = client.post("/run", json={"action": action, "region": cfg.region_id, "config": {}}, headers=HEADERS)
    return r.json()


def test_full_pipeline_reaches_acceptance(tmp_path):
    client, cfg, store, builder, oracle = _client(tmp_path)

    working = cfg.working_copy_dir
    (working / "notes" / "regions").mkdir(parents=True)
    (working / SPEC_PATH).write_text(SPEC)
    client.post("/submit", json={"region": cfg.region_id}, headers=HEADERS)

    for action in (
        "sese_check", "build_replay", "run_replay", "sanitize",
        "regression_visible", "regression_holdout", "time_port", "time_baseline",
    ):
        body = _run(client, cfg, action)
        assert "error" not in body, f"{action}: {body}"
        assert body.get("refused") is not True, f"{action}: {body}"
        verdicts = [c["verdict"] for c in body["claims"]] if "claims" in body else [body["verdict"]]
        assert all(v == "pass" for v in verdicts), f"{action}: {body}"

    status = client.get("/status", params={"region": cfg.region_id}, headers=HEADERS).json()
    assert status["accepted"] is True

    # regression_visible must not have re-run the replay binary -- one run
    # call for run_replay's own visible execution, one more for holdout.
    assert len(builder.run_calls) == 2
    assert len(oracle.compare_calls) == 2

    # Every claim, whatever the action, names the strategy and the code's
    # own manifest in its materials: a pass under one description of the
    # code must not read as a pass under another.
    strategy = load_strategy(STRATEGY_PATH)
    for claim in store.all_claims():
        assert any(
            m.kind == "strategy" and m.sha256 == strategy.sha256 for m in claim.materials
        ), claim.predicateType
        assert any(
            m.kind == "manifest" and m.sha256 == cfg.manifest.sha256 for m in claim.materials
        ), claim.predicateType

    # Regression claims carry the tolerance policy as a formal material,
    # matching the hash the oracle reported.
    for predicate_type in ("regression/visible", "regression/holdout"):
        claim = next(c for c in store.all_claims() if c.predicateType == predicate_type)
        assert any(m.kind == "policy" and m.sha256 == "policyabc" for m in claim.materials)

    # The build claim records the strategy's own flags as what was
    # compiled, and the timing claim carries the same flags read back
    # from it.
    expected_flags = list(strategy.languages["fortran"].flags)
    build_claim = next(c for c in store.all_claims() if c.predicateType == "build/replay")
    port_claim = next(c for c in store.all_claims() if c.predicateType == "timing/port")
    assert build_claim.predicate.detail["flags"] == expected_flags
    assert port_claim.predicate.detail["flags"] == expected_flags


def test_sanitize_dispatch_writes_three_claims_and_is_a_duplicate_on_repeat(tmp_path):
    client, cfg, store, builder, oracle = _client(tmp_path)
    working = cfg.working_copy_dir
    (working / "notes" / "regions").mkdir(parents=True)
    (working / SPEC_PATH).write_text(SPEC)
    client.post("/submit", json={"region": cfg.region_id}, headers=HEADERS)
    _run(client, cfg, "sese_check")
    _run(client, cfg, "build_replay")
    _run(client, cfg, "run_replay")

    first = _run(client, cfg, "sanitize")
    assert len(first["claims"]) == 3
    assert len(store.all_claims()) == 6  # sese + build + run + 3 sanitize

    second = _run(client, cfg, "sanitize")
    assert second["claims"] == first["claims"]
    assert len(builder.sanitize_calls) == 1  # not called again
    assert store.all_requests()[-1].outcome == "duplicate"


def test_holdout_receipt_is_verdict_only_but_the_stored_claim_keeps_its_detail(tmp_path):
    # The receipt policy (predicate registry: regression/holdout is
    # VERDICT_ONLY) filters what /run returns to the agent; the ledger
    # line itself keeps everything the component recorded, for the CLI.
    client, cfg, store, builder, oracle = _client(tmp_path)
    working = cfg.working_copy_dir
    (working / "notes" / "regions").mkdir(parents=True)
    (working / SPEC_PATH).write_text(SPEC)
    client.post("/submit", json={"region": cfg.region_id}, headers=HEADERS)
    for action in ("sese_check", "build_replay", "run_replay", "sanitize", "regression_visible"):
        _run(client, cfg, action)

    first = _run(client, cfg, "regression_holdout")
    assert first["verdict"] == "pass"
    assert "detail" not in first
    assert store.get_claim(first["claim_id"]).predicate.detail  # full record stays in the ledger

    # The duplicate path is filtered by the same policy.
    second = _run(client, cfg, "regression_holdout")
    assert second == first


def test_time_baseline_is_filed_against_the_baseline_tree_not_the_current_tree(tmp_path):
    client, cfg, store, builder, oracle = _client(tmp_path)
    working = cfg.working_copy_dir
    (working / "notes" / "regions").mkdir(parents=True)
    (working / SPEC_PATH).write_text(SPEC)
    client.post("/submit", json={"region": cfg.region_id}, headers=HEADERS)
    _run(client, cfg, "sese_check")  # widens the allow-list to include mod_kernel.f90

    (working / "src").mkdir(parents=True)
    (working / "src" / "mod_kernel.f90").write_text(CLEAN_SOURCE.replace("a + b", "a + b + 0"))
    client.post("/submit", json={"region": cfg.region_id}, headers=HEADERS)

    result = _run(client, cfg, "time_baseline")

    status = client.get("/status", params={"region": cfg.region_id}, headers=HEADERS).json()
    claim = store.get_claim(result["claim_id"])
    assert claim.subject[0].sha256 != status["tree"]
