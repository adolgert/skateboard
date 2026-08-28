from pathlib import Path

from fastapi.testclient import TestClient

from equivalent.gateway.app import config_hash, create_app
from equivalent.gateway.regions import RegionConfig
from equivalent.gateway.submit import current_tree_and_frozen, init_baseline_repo, tracked_files
from equivalent.gateway.table import ACTION_TABLE
from equivalent.ledger.predicates import PREDICATE_TYPES
from equivalent.ledger.records import Predicate
from equivalent.ledger.store import LedgerStore
from equivalent.ledger.subjects import Subject, frozen_subject

TOKEN = "test-token"
HEADERS = {"Authorization": f"Bearer {TOKEN}", "X-Session-Id": "sess-1", "X-Model-Id": "claude-sonnet-5"}
SPEC_PATH = "notes/regions/ch04-step.sese.yaml"
STRATEGY_PATH = Path(__file__).resolve().parents[2] / "strategy" / "files" / "stdpar_managed.yaml"


def _seed(root, with_makefile=False):
    (root / "src").mkdir(parents=True)
    (root / "src" / "mod_kernel.f90").write_text("subroutine step\nend subroutine\n")
    if with_makefile:
        (root / "Makefile").write_text("all:\n\techo build\n")
    return root


def _region(tmp_path, with_makefile=False):
    repo_dir = tmp_path / "repo"
    init_baseline_repo(repo_dir, _seed(tmp_path / "seed", with_makefile))
    return RegionConfig(
        region_id="ch04:step", repo_dir=repo_dir, spec_path=SPEC_PATH, ledger_dir=tmp_path / "ledger",
        strategy_path=STRATEGY_PATH,
    )


def _client(tmp_path, with_makefile=False):
    cfg = _region(tmp_path, with_makefile)
    store = LedgerStore(cfg.ledger_dir)
    client = TestClient(create_app({cfg.region_id: cfg}, TOKEN))
    return client, cfg, store


def _submit_spec_only(client, cfg):
    working = cfg.repo_dir.parent / "working"
    (working / "notes" / "regions").mkdir(parents=True, exist_ok=True)
    (working / "notes" / "regions" / "ch04-step.sese.yaml").write_text("region: ch04:step\n")
    return client.post("/submit", json={"region": cfg.region_id, "working_copy_dir": str(working)}, headers=HEADERS)


def test_refused_action_names_the_current_tree(tmp_path):
    client, cfg, store = _client(tmp_path)
    _submit_spec_only(client, cfg)

    run_body = client.post(
        "/run", json={"action": "build_replay", "region": cfg.region_id, "config": {}}, headers=HEADERS,
    ).json()
    status_body = client.get("/status", params={"region": cfg.region_id}, headers=HEADERS).json()

    assert run_body["refused"] is True
    assert run_body["tree"] == status_body["tree"]


def test_refusal_item_matches_status_item_for_the_same_missing_predicate(tmp_path):
    client, cfg, store = _client(tmp_path)
    _submit_spec_only(client, cfg)

    run_body = client.post(
        "/run", json={"action": "build_replay", "region": cfg.region_id, "config": {}}, headers=HEADERS,
    ).json()
    status_body = client.get("/status", params={"region": cfg.region_id}, headers=HEADERS).json()

    refused_item = next(m for m in run_body["missing"] if m["predicateType"] == "sese/verified")
    status_item = next(r for r in status_body["rows"] if r["predicateType"] == "sese/verified")
    assert refused_item == status_item


def test_frozen_requirement_survives_an_allowed_edit_but_tree_requirement_does_not(tmp_path):
    cfg = _region(tmp_path, with_makefile=True)
    store = LedgerStore(cfg.ledger_dir)
    allow_globs = ["src/mod_kernel.f90"]
    baseline = tracked_files(cfg.repo_dir, "main")
    frozen_files = [f for f in baseline if f["path"] not in allow_globs]
    frozen_sha = frozen_subject(frozen_files).sha256

    store.record_claim(
        [Subject(kind="frozen", sha256=frozen_sha)], "sese/verified",
        Predicate(tool="sese_check", version="0.1", configHash="cfg", verdict="pass",
                  detail={"allow_globs": allow_globs}),
        [], "sess-0",
    )

    client = TestClient(create_app({cfg.region_id: cfg}, TOKEN))
    working = tmp_path / "working"
    (working / "src").mkdir(parents=True)
    (working / "src" / "mod_kernel.f90").write_text("subroutine step\n  x = 1\nend subroutine\n")
    client.post("/submit", json={"region": cfg.region_id, "working_copy_dir": str(working)}, headers=HEADERS)
    tree_a, _ = current_tree_and_frozen(cfg.repo_dir, cfg.region_id, store, cfg.spec_path)
    store.record_claim(
        [Subject(kind="tree", sha256=tree_a)], "build/replay",
        Predicate(tool="builder", version="0.1", configHash="cfg", verdict="pass", detail={}),
        [], "sess-0",
    )

    before = client.post(
        "/run", json={"action": "run_replay", "region": cfg.region_id, "config": {}}, headers=HEADERS,
    ).json()
    assert "refused" not in before

    (working / "src" / "mod_kernel.f90").write_text("subroutine step\n  x = 2\nend subroutine\n")
    client.post("/submit", json={"region": cfg.region_id, "working_copy_dir": str(working)}, headers=HEADERS)

    still_verified = client.post(
        "/run", json={"action": "build_replay", "region": cfg.region_id, "config": {}}, headers=HEADERS,
    ).json()
    assert "refused" not in still_verified

    now_missing_build = client.post(
        "/run", json={"action": "run_replay", "region": cfg.region_id, "config": {}}, headers=HEADERS,
    ).json()
    assert now_missing_build["refused"] is True
    assert now_missing_build["missing"][0]["predicateType"] == "build/replay"


def test_duplicate_deterministic_request_returns_existing_claim_and_writes_no_new_one(tmp_path):
    client, cfg, store = _client(tmp_path)
    config = {"x": 1}
    allow_globs = [SPEC_PATH]
    baseline = tracked_files(cfg.repo_dir, "main")
    frozen_sha = frozen_subject([f for f in baseline if f["path"] not in allow_globs]).sha256
    existing = store.record_claim(
        [Subject(kind="frozen", sha256=frozen_sha)], "sese/verified",
        Predicate(tool="sese_check", version="0.1", configHash=config_hash(config), verdict="pass",
                  detail={"allow_globs": allow_globs}),
        [], "sess-0",
    )

    r = client.post("/run", json={"action": "sese_check", "region": cfg.region_id, "config": config}, headers=HEADERS)
    body = r.json()

    assert body["claim_id"] == existing.id
    assert len(store.all_claims()) == 1
    assert store.all_requests()[-1].outcome == "duplicate"


def test_nondeterministic_action_is_never_treated_as_a_duplicate(tmp_path):
    client, cfg, store = _client(tmp_path)
    config = {"repeats": 5}

    first = client.post(
        "/run", json={"action": "time_baseline", "region": cfg.region_id, "config": config}, headers=HEADERS,
    ).json()
    second = client.post(
        "/run", json={"action": "time_baseline", "region": cfg.region_id, "config": config}, headers=HEADERS,
    ).json()

    assert "error" in first and "error" in second
    outcomes = [r.outcome for r in store.all_requests()]
    assert "duplicate" not in outcomes


def test_every_run_call_writes_exactly_one_request_log_line_with_the_session_id(tmp_path):
    client, cfg, store = _client(tmp_path)

    client.post("/run", json={"action": "build_replay", "region": cfg.region_id, "config": {}}, headers=HEADERS)

    assert len(store.all_requests()) == 1
    assert store.all_requests()[0].session == "sess-1"


def test_every_row_references_real_predicate_types_and_agrees_with_the_registry_on_determinism():
    known_component_prefixes = ("analyzer:", "builder:", "oracle:")
    for row in ACTION_TABLE:
        for predicate_type in row.emits:
            assert predicate_type in PREDICATE_TYPES
        for predicate_type, subject_kind in row.requires:
            assert predicate_type in PREDICATE_TYPES
            assert subject_kind in ("tree", "frozen")
        if row.component is None:
            assert row.name == "accept"
        else:
            assert row.component.startswith(known_component_prefixes)
        if row.emits:
            assert row.deterministic == all(PREDICATE_TYPES[pt].deterministic for pt in row.emits)


def test_unknown_action_and_the_componentless_accept_row_are_rejected(tmp_path):
    client, cfg, store = _client(tmp_path)

    r1 = client.post(
        "/run", json={"action": "not_a_real_action", "region": cfg.region_id, "config": {}}, headers=HEADERS,
    )
    assert r1.status_code == 400

    r2 = client.post("/run", json={"action": "accept", "region": cfg.region_id, "config": {}}, headers=HEADERS)
    assert r2.status_code == 400


def test_ready_action_with_no_component_yet_reports_that_plainly(tmp_path):
    # time_baseline requires nothing and is real in the table, but its
    # builder component isn't wired up until a later 6-series step.
    client, cfg, store = _client(tmp_path)

    r = client.post("/run", json={"action": "time_baseline", "region": cfg.region_id, "config": {}}, headers=HEADERS)
    body = r.json()

    assert "not implemented" in body["error"]
    assert store.all_requests()[-1].outcome == "error"
