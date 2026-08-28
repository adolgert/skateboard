"""One onboarding region driven through the gateway, as a session would.

The two checks that exist are run in order against a real ledger and a
stand-in builder, so what is under test here is the dispatch: the
refusal before the evidence exists, the claims, the materials they name,
and what `status` says when the checks that are built have passed.
"""
from pathlib import Path

from fastapi.testclient import TestClient

from equivalent.gateway.app import create_app
from equivalent.gateway.regions import RegionConfig
from equivalent.gateway.submit import init_baseline_repo
from equivalent.ledger.acceptance import ONBOARDING
from equivalent.ledger.store import LedgerStore
from equivalent.manifest.schema import load_manifest
from equivalent.tests.fakes import FakeBuilder, write_program, write_tree

TOKEN = "test-token"
HEADERS = {"Authorization": f"Bearer {TOKEN}", "X-Session-Id": "sess-1", "X-Model-Id": "claude-sonnet-5"}
STRATEGY_DIR = Path(__file__).resolve().parents[2] / "strategy" / "files"
REGION = "tsunami:onboarding"


def _client(tmp_path):
    """A gateway holding one onboarding region, seeded from a bare tree."""
    repo_dir = tmp_path / "repo"
    init_baseline_repo(repo_dir, write_tree(tmp_path / "seed"))
    # The agent's working copy is that tree, which is what it edits.
    working = write_tree(tmp_path / "working")
    program = write_program(tmp_path, minimal=True)
    cfg = RegionConfig(
        region_id=REGION,
        phase=ONBOARDING,
        repo_dir=repo_dir,
        spec_path=None,
        ledger_dir=tmp_path / "ledger",
        strategy_path=STRATEGY_DIR / "onboarding.yaml",
        baseline_strategy_path=STRATEGY_DIR / "cpu_reference.yaml",
        working_copy_dir=working,
        manifest=load_manifest(program / "manifest.yaml"),
    )
    builder = FakeBuilder()
    client = TestClient(create_app({REGION: cfg}, TOKEN, builder=builder))
    return client, cfg, LedgerStore(cfg.ledger_dir), builder


def _run(client, action):
    return client.post(
        "/run", json={"action": action, "region": REGION, "config": {}}, headers=HEADERS,
    ).json()


def test_the_build_check_is_refused_until_the_manifest_has_been_read(tmp_path):
    client, _, _, _ = _client(tmp_path)

    body = _run(client, "harness_build")

    assert body["refused"] is True
    assert [item["predicateType"] for item in body["missing"]] == ["manifest/valid"]
    assert body["missing"][0]["producing_action"] == "manifest_check"


def test_submitting_an_onboarding_region_keeps_every_file(tmp_path):
    # The whole tree is being written during onboarding, so nothing the
    # agent sends is turned away.
    client, cfg, _, _ = _client(tmp_path)
    (cfg.working_copy_dir / "harness" / "capture.f90").write_text("end\n")

    body = client.post("/submit", json={"region": REGION}, headers=HEADERS).json()

    assert body["rejected"] == []
    assert body["committed"] is True


def test_the_two_checks_run_in_order_and_leave_claims_naming_the_manifest(tmp_path):
    client, cfg, store, builder = _client(tmp_path)

    manifest_claim = _run(client, "manifest_check")
    build_claim = _run(client, "harness_build")

    assert manifest_claim["verdict"] == "pass"
    assert manifest_claim["detail"]["name"] == "tsunami"
    assert build_claim["verdict"] == "pass"
    # One build per strategy, each in its own workspace.
    assert len(builder.build_calls) == 2

    # Every claim names the strategy and the code's own manifest, the same
    # way a porting claim does -- the minimal manifest is still what the
    # region was configured with.
    for claim in store.all_claims():
        kinds = {subject.kind for subject in claim.materials}
        assert {"strategy", "manifest"} <= kinds


def test_status_reports_the_onboarding_requirements_and_what_is_still_missing(tmp_path):
    client, _, _, _ = _client(tmp_path)
    _run(client, "manifest_check")
    _run(client, "harness_build")

    body = client.get("/status", params={"region": REGION}, headers=HEADERS).json()

    assert body["phase"] == ONBOARDING
    rows = {row["predicateType"]: row for row in body["rows"]}
    assert rows["manifest/valid"]["status"] == "present"
    assert rows["harness/builds"]["status"] == "present"
    assert rows["harness/captured"]["producing_action"] == "harness_capture"
    # Four checks of the six are still to be built, so the region is not
    # onboarded yet.
    assert body["accepted"] is False


def test_a_check_that_is_not_built_yet_says_so_rather_than_failing_the_code(tmp_path):
    client, _, store, _ = _client(tmp_path)
    _run(client, "manifest_check")
    _run(client, "harness_build")

    body = _run(client, "harness_capture")

    assert "not implemented yet" in body["error"]
    # And nothing about the code was recorded for it.
    assert [claim.predicateType for claim in store.all_claims()] == [
        "manifest/valid", "harness/builds",
    ]
