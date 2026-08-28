"""One onboarding region driven through the gateway, as a session would.

Every onboarding check is run in order against a real ledger and a
stand-in builder, so what is under test here is the dispatch: the
refusal before the evidence exists, the claims, the materials they name,
and what `status` says when all of them have passed.
"""
from pathlib import Path

from fastapi.testclient import TestClient

from equivalent.cli import render
from equivalent.gateway.app import create_app
from equivalent.gateway.regions import RegionConfig
from equivalent.gateway.submit import init_baseline_repo
from equivalent.gateway.table import rows_for
from equivalent.ledger.acceptance import ONBOARDING
from equivalent.ledger.store import LedgerStore
from equivalent.manifest.schema import load_manifest
from equivalent.tests.fakes import FakeBuilder, write_program, write_tree

TOKEN = "test-token"
# Every action of the onboarding phase that has something to dispatch to,
# read from the gateway's own table rather than listed again here.
ONBOARDING_ACTIONS = [row.name for row in rows_for(ONBOARDING) if row.component is not None]
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
        code="tsunami",
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
    # The tree's own replay driver reproduces what its capture program
    # recorded, which is what an onboarding that is going well looks like.
    builder.replays_capture = True
    client = TestClient(create_app({REGION: cfg}, TOKEN, builder=builder))
    return client, cfg, LedgerStore(cfg.ledger_dir), builder


def _run(client, action):
    return client.post(
        "/run", json={"action": action, "region": REGION, "config": {}}, headers=HEADERS,
    ).json()


def _onboard(client) -> dict:
    """Every onboarding action, in the order the table lists them."""
    return {action: _run(client, action) for action in ONBOARDING_ACTIONS}


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
    assert rows["harness/captured"]["status"] == "missing"
    assert rows["harness/captured"]["producing_action"] == "harness_capture"
    # Four checks of the six have not run, so the region is not onboarded.
    assert body["accepted"] is False


def test_every_onboarding_check_passing_leaves_the_region_onboarded(tmp_path):
    client, _, _, _ = _client(tmp_path)

    claims = _onboard(client)

    assert [body.get("verdict") for body in claims.values()] == ["pass"] * 6
    body = client.get("/status", params={"region": REGION}, headers=HEADERS).json()
    assert [row["status"] for row in body["rows"]] == ["present"] * 6
    assert body["accepted"] is True
    # And what a person reading that status sees is the word for a code
    # that is ready to be reviewed and promoted.
    assert "ONBOARDED" in render.render_status(body, REGION)


def test_a_check_that_reads_a_capture_set_names_it_in_the_claims_materials(tmp_path):
    client, _, store, _ = _client(tmp_path)

    _onboard(client)

    by_predicate = {claim.predicateType: claim for claim in store.all_claims()}
    visible = by_predicate["harness/captured"].predicate.detail["datasets"]["visible"]
    named = {
        subject.sha256 for subject in by_predicate["harness/replays"].materials
        if subject.kind == "capture_set"
    }
    assert visible["capture_set"] in named
    # The timing claim rests on the one set it wrote, the program's own.
    program = by_predicate["harness/times"].predicate.detail["datasets"]["program"]
    assert [subject.sha256 for subject in by_predicate["harness/times"].materials
            if subject.kind == "capture_set"] == [program["capture_set"]]


def test_a_check_that_would_run_before_its_evidence_exists_is_refused(tmp_path):
    client, _, store, _ = _client(tmp_path)
    _run(client, "manifest_check")
    _run(client, "harness_build")

    body = _run(client, "harness_replay")

    assert body["refused"] is True
    assert [item["predicateType"] for item in body["missing"]] == ["harness/captured"]
    assert [claim.predicateType for claim in store.all_claims()] == [
        "manifest/valid", "harness/builds",
    ]
