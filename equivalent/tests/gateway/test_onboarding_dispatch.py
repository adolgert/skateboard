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
from equivalent.tree import init_baseline_repo
from equivalent.ledger.table import rows_for
from equivalent.ledger.acceptance import ONBOARDING
from equivalent.ledger.store import LedgerStore
from equivalent.manifest.schema import load_manifest
from equivalent.tests.gateway.conftest import ONBOARDING_STRATEGY_PATH, region_config
from equivalent.tests.fakes import write_program, write_tree
from equivalent.tests.components.test_original_check import ReferenceBuilder, reference

TOKEN = "test-token"
# Every action of the onboarding phase that has something to dispatch to,
# read from the gateway's own table rather than listed again here.
ONBOARDING_ACTIONS = [row.name for row in rows_for(ONBOARDING) if row.component is not None]
HEADERS = {"Authorization": f"Bearer {TOKEN}", "X-Session-Id": "sess-1", "X-Model-Id": "claude-sonnet-5"}
STRATEGY_DIR = Path(__file__).resolve().parents[2] / "strategy" / "files"
REGION = "tsunami:onboarding"


def _client(tmp_path, *, oracle=None):
    """A gateway holding one onboarding region, seeded from a bare tree."""
    repo_dir = tmp_path / "repo"
    init_baseline_repo(repo_dir, write_tree(tmp_path / "seed"))
    # The agent's working copy is that tree, which is what it edits.
    working = write_tree(tmp_path / "working")
    program = write_program(tmp_path, minimal=True)
    cfg = region_config(
        tmp_path, region_id=REGION, phase=ONBOARDING, repo_dir=repo_dir,
        spec_path=None, strategy_path=ONBOARDING_STRATEGY_PATH,
        working_copy_dir=working,
        manifest=load_manifest(program / "manifest.yaml"),
        original_reference_path=reference(tmp_path),
    )
    builder = ReferenceBuilder()
    # The tree's own replay driver reproduces what its capture program
    # recorded, which is what an onboarding that is going well looks like.
    builder.replays_capture = True
    client = TestClient(create_app({REGION: cfg}, TOKEN, builder=builder, oracle=oracle))
    return client, cfg, LedgerStore(cfg.ledger_dir), builder


def _run(client, action):
    return client.post(
        "/run", json={"action": action, "region": REGION, "config": {}}, headers=HEADERS,
    ).json()


def _onboard(client) -> dict:
    """Every onboarding action, in the order the table lists them."""
    return {action: _run(client, action) for action in ONBOARDING_ACTIONS}


def test_the_onboarding_phase_offers_every_check_a_code_has_to_pass():
    # Written out once, here, so that adding a row to the table without
    # deciding it belongs in an onboarding session fails a test.
    assert ONBOARDING_ACTIONS == [
        "manifest_check", "harness_build", "harness_capture", "harness_replay",
        "harness_determinism", "harness_timing", "harness_original",
        "harness_self_check", "harness_property",
    ]


def test_the_build_check_is_refused_until_the_manifest_has_been_read(tmp_path):
    client, _, _, _ = _client(tmp_path)

    body = _run(client, "harness_build")

    assert body["refused"] is True
    assert [item["predicateType"] for item in body["missing"]] == ["manifest/valid"]
    assert body["missing"][0]["producing_action"] == "manifest_check"


def test_onboarding_never_queries_the_porting_oracle_policy(tmp_path):
    class PortingOnlyOracle:
        def policy(self):
            raise AssertionError("onboarding must not query /v1/policy")

    client, _, _, _ = _client(tmp_path, oracle=PortingOnlyOracle())

    body = _run(client, "manifest_check")

    assert body["verdict"] == "pass"


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
    # Seven checks of the nine have not run, so the region is not onboarded.
    assert body["accepted"] is False


def test_every_onboarding_check_passing_leaves_the_region_onboarded(tmp_path):
    client, _, _, _ = _client(tmp_path)

    claims = _onboard(client)

    assert [body.get("verdict") for body in claims.values()] == ["pass"] * len(ONBOARDING_ACTIONS)
    body = client.get("/status", params={"region": REGION}, headers=HEADERS).json()
    assert [row["status"] for row in body["rows"]] == ["present"] * len(ONBOARDING_ACTIONS)
    assert body["accepted"] is True
    # And what a person reading that status sees is the word for a code
    # that is ready to be reviewed and promoted.
    assert "ONBOARDED" in render.render_status(body, REGION)


def test_what_the_original_comparison_kept_is_in_the_ledger_beside_its_claim(tmp_path):
    # The claim says two programs agreed; a person reading it later has to
    # be able to look at what they agreed on. The check hands those bytes
    # back with its verdict and the gateway files them, so nothing is in
    # the artifacts directory that no recorded verdict rests on.
    client, cfg, store, _ = _client(tmp_path)

    claims = _onboard(client)

    claim = store.get_claim(claims["harness_original"]["claim_id"])
    named = {
        sha
        for run in claim.predicate.detail["runs"]
        for output in run["outputs"]
        for sha in (*output["original_artifacts"], *output["candidate_artifacts"])
    }
    assert named
    for sha in named:
        assert (store.region_dir / "artifacts" / sha).is_file()


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


def test_the_self_check_claim_rests_on_the_captures_and_the_bands_it_used(tmp_path):
    client, _, store, _ = _client(tmp_path)

    _onboard(client)

    by_predicate = {claim.predicateType: claim for claim in store.all_claims()}
    claim = by_predicate["harness/self_check"]
    visible = by_predicate["harness/captured"].predicate.detail["datasets"]["visible"]
    kinds = {subject.kind: subject.sha256 for subject in claim.materials}
    assert kinds["capture_set"] == visible["capture_set"]
    # The bands are what decided whether a changed answer counted, so the
    # policy is a material and not a note in the detail.
    assert kinds["policy"] == claim.predicate.detail["policy_sha256"]


def test_a_limit_the_session_asks_for_reaches_the_mutation_run(tmp_path):
    client, _, _, builder = _client(tmp_path)
    for action in ONBOARDING_ACTIONS[:-2]:
        _run(client, action)

    body = client.post(
        "/run", json={"action": "harness_self_check", "region": REGION, "config": {"limit": 5}},
        headers=HEADERS,
    ).json()

    assert body["verdict"] == "pass"
    assert builder.mutate_calls[0]["limit"] == 5


def test_a_code_that_states_no_invariants_still_files_the_property_claim(tmp_path):
    # The fixture code declares `properties: null`, and the ledger should
    # say so rather than hold a row nobody filed.
    client, _, _, builder = _client(tmp_path)

    claims = _onboard(client)

    assert claims["harness_property"]["verdict"] == "pass"
    assert claims["harness_property"]["detail"]["module"] is None
    assert builder.properties_calls == []


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
