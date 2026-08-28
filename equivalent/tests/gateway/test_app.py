from pathlib import Path

from fastapi.testclient import TestClient

from equivalent.gateway.app import create_app
from equivalent.gateway.regions import RegionConfig
from equivalent.gateway.submit import init_baseline_repo
from equivalent.ledger.status import compute_history, compute_status
from equivalent.ledger.store import LedgerStore

TOKEN = "test-token"
HEADERS = {"Authorization": f"Bearer {TOKEN}", "X-Session-Id": "sess-1", "X-Model-Id": "claude-sonnet-5"}
STRATEGY_PATH = Path(__file__).resolve().parents[2] / "strategy" / "files" / "stdpar_managed.yaml"


def _seed(root):
    (root / "src").mkdir(parents=True)
    (root / "src" / "mod_kernel.f90").write_text("subroutine step\nend subroutine\n")
    return root


def _region(tmp_path, region_id="ch04:step"):
    repo_dir = tmp_path / "repo"
    init_baseline_repo(repo_dir, _seed(tmp_path / "seed"))
    cfg = RegionConfig(
        region_id=region_id,
        repo_dir=repo_dir,
        spec_path="notes/regions/ch04-step.sese.yaml",
        ledger_dir=tmp_path / "ledger",
        strategy_path=STRATEGY_PATH,
    )
    return cfg


def _client(tmp_path, region_id="ch04:step"):
    cfg = _region(tmp_path, region_id)
    app = create_app({region_id: cfg}, TOKEN)
    return TestClient(app), cfg


def test_get_table_returns_the_action_rows(tmp_path):
    client, _ = _client(tmp_path)

    r = client.get("/table", headers=HEADERS)

    assert r.status_code == 200
    names = {row["name"] for row in r.json()}
    assert {"sese_check", "build_replay", "accept"} <= names


def test_get_table_matches_the_pi_extension_fixture(tmp_path):
    # pi-extension/test/fixtures/table.json is the TS tests' hand copy of
    # the real table (its golden tool descriptions are generated from it).
    # Pinning it to GET /table here means a table change fails this test
    # until the fixture -- and so the golden descriptions the person
    # reviews -- is regenerated. Without this, a table change would only
    # ever be tested against the stale copy.
    import json

    client, _ = _client(tmp_path)
    fixture_path = Path(__file__).resolve().parents[3] / "pi-extension" / "test" / "fixtures" / "table.json"
    fixture = json.loads(fixture_path.read_text())

    r = client.get("/table", headers=HEADERS)

    assert r.json() == fixture


def test_get_status_reports_the_real_current_tree_before_any_check_has_run(tmp_path):
    client, cfg = _client(tmp_path)
    store = LedgerStore(cfg.ledger_dir)

    r = client.get("/status", params={"region": cfg.region_id}, headers=HEADERS)

    assert r.status_code == 200
    body = r.json()
    assert body["tree"] is not None
    assert body["accepted"] is False
    # The gateway's answer matches what compute_status itself would say
    # given the same tree/frozen -- one rendering, not two.
    from equivalent.gateway.submit import current_tree_and_frozen
    from equivalent.ledger.subjects import Subject
    tree_sha, frozen_sha = current_tree_and_frozen(cfg.repo_dir, cfg.region_id, store, cfg.spec_path)
    expected = compute_status(
        store, tree=Subject(kind="tree", sha256=tree_sha), frozen=Subject(kind="frozen", sha256=frozen_sha),
    )
    assert body == expected


def test_post_submit_wraps_step4_submit_and_returns_its_receipt(tmp_path):
    client, cfg = _client(tmp_path)

    working = tmp_path / "working"
    (working / "notes" / "regions").mkdir(parents=True)
    (working / "notes" / "regions" / "ch04-step.sese.yaml").write_text("region: ch04:step\n")

    r = client.post(
        "/submit", json={"region": cfg.region_id, "working_copy_dir": str(working)}, headers=HEADERS,
    )

    assert r.status_code == 200
    body = r.json()
    assert body["committed"] is True
    assert len(body["tree"]) == 64
    assert body["rejected"] == []
    assert body["not_sent"] == []  # no baseline file matches the bootstrap allow-list here


def test_submit_writes_exactly_one_request_log_line_with_the_session_id(tmp_path):
    client, cfg = _client(tmp_path)
    store = LedgerStore(cfg.ledger_dir)

    working = tmp_path / "working"
    (working / "notes" / "regions").mkdir(parents=True)
    (working / "notes" / "regions" / "ch04-step.sese.yaml").write_text("region: ch04:step\n")

    client.post("/submit", json={"region": cfg.region_id, "working_copy_dir": str(working)}, headers=HEADERS)

    requests = store.all_requests()
    assert len(requests) == 1
    assert requests[0].session == "sess-1"
    assert requests[0].endpoint == "submit"
    assert requests[0].outcome == "submitted"  # a submit yields no claim, so not "claim"


def test_request_without_a_valid_token_is_rejected(tmp_path):
    client, cfg = _client(tmp_path)

    r = client.get("/table", headers={"X-Session-Id": "sess-1", "X-Model-Id": "m"})
    assert r.status_code == 401

    r = client.get("/table", headers={**HEADERS, "Authorization": "Bearer wrong"})
    assert r.status_code == 401


def test_compute_status_and_history_without_repo_info_are_unchanged(tmp_path):
    # The CLI's own behaviour and golden file must keep working: no tree
    # or frozen argument means fall back to the claims-based guess.
    store = LedgerStore(tmp_path / "region")
    status = compute_status(store)
    history = compute_history(store)
    assert status["tree"] is None
    assert status["accepted"] is False
    assert history == []
