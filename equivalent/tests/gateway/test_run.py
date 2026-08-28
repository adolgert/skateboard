from pathlib import Path

from fastapi.testclient import TestClient

from equivalent.gateway.app import config_hash, create_app
from equivalent.gateway.regions import RegionConfig
from equivalent.gateway.submit import current_tree_and_frozen, init_baseline_repo, tracked_files
from equivalent.gateway.table import ACTION_TABLE
from equivalent.ledger.acceptance import PHASES, PORTING
from equivalent.ledger.predicates import PREDICATE_TYPES
from equivalent.ledger.records import Predicate
from equivalent.ledger.store import LedgerStore
from equivalent.ledger.subjects import Subject, frozen_subject
from equivalent.manifest.schema import load_manifest
from equivalent.strategy.schema import load_strategy
from equivalent.tests.fakes import write_program

TOKEN = "test-token"
HEADERS = {"Authorization": f"Bearer {TOKEN}", "X-Session-Id": "sess-1", "X-Model-Id": "claude-sonnet-5"}
SPEC_PATH = "notes/regions/ch04-step.sese.yaml"
STRATEGY_PATH = Path(__file__).resolve().parents[2] / "strategy" / "files" / "stdpar_managed.yaml"
BASELINE_STRATEGY_PATH = STRATEGY_PATH.parent / "cpu_reference.yaml"


def _seed(root, with_makefile=False):
    (root / "src").mkdir(parents=True)
    (root / "src" / "mod_kernel.f90").write_text("subroutine step\nend subroutine\n")
    if with_makefile:
        (root / "Makefile").write_text("all:\n\techo build\n")
    return root


def _region(tmp_path, with_makefile=False):
    repo_dir = tmp_path / "repo"
    init_baseline_repo(repo_dir, _seed(tmp_path / "seed", with_makefile))
    working = tmp_path / "working"
    working.mkdir()
    return RegionConfig(
        region_id="ch04:step", phase=PORTING, repo_dir=repo_dir, spec_path=SPEC_PATH,
        ledger_dir=tmp_path / "ledger",
        strategy_path=STRATEGY_PATH, baseline_strategy_path=BASELINE_STRATEGY_PATH,
        working_copy_dir=working,
        manifest=load_manifest(write_program(tmp_path) / "manifest.yaml"),
    )


def _current(cfg, store):
    """The tree and frozen hashes the gateway itself would compute."""
    return current_tree_and_frozen(
        cfg.repo_dir, cfg.region_id, store, cfg.spec_path, cfg.phase,
        load_strategy(cfg.strategy_path),
    )


def _client(tmp_path, with_makefile=False):
    cfg = _region(tmp_path, with_makefile)
    store = LedgerStore(cfg.ledger_dir)
    client = TestClient(create_app({cfg.region_id: cfg}, TOKEN))
    return client, cfg, store


def _submit_spec_only(client, cfg):
    (cfg.working_copy_dir / "notes" / "regions").mkdir(parents=True, exist_ok=True)
    (cfg.working_copy_dir / "notes" / "regions" / "ch04-step.sese.yaml").write_text("region: ch04:step\n")
    return client.post("/submit", json={"region": cfg.region_id}, headers=HEADERS)


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
    working = cfg.working_copy_dir
    (working / "src").mkdir(parents=True)
    (working / "src" / "mod_kernel.f90").write_text("subroutine step\n  x = 1\nend subroutine\n")
    client.post("/submit", json={"region": cfg.region_id}, headers=HEADERS)
    tree_a, _ = _current(cfg, store)
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
    client.post("/submit", json={"region": cfg.region_id}, headers=HEADERS)

    still_verified = client.post(
        "/run", json={"action": "build_replay", "region": cfg.region_id, "config": {}}, headers=HEADERS,
    ).json()
    assert "refused" not in still_verified

    now_missing_build = client.post(
        "/run", json={"action": "run_replay", "region": cfg.region_id, "config": {}}, headers=HEADERS,
    ).json()
    assert now_missing_build["refused"] is True
    assert now_missing_build["missing"][0]["predicateType"] == "build/replay"


def test_a_failing_requirement_claim_still_refuses_the_dependent_action(tmp_path):
    # A claim that exists but failed must not open the gate; the refusal
    # names the failing claim so the model knows to fix and re-run, not
    # that the check never ran.
    client, cfg, store = _client(tmp_path)
    _submit_spec_only(client, cfg)
    _, frozen_sha = _current(cfg, store)
    failed = store.record_claim(
        [Subject(kind="frozen", sha256=frozen_sha)], "sese/verified",
        Predicate(tool="sese_check", version="0.1", configHash="cfg", verdict="fail", detail={}),
        [], "sess-0",
    )

    body = client.post(
        "/run", json={"action": "build_replay", "region": cfg.region_id, "config": {}}, headers=HEADERS,
    ).json()

    assert body["refused"] is True
    item = next(m for m in body["missing"] if m["predicateType"] == "sese/verified")
    assert item["verdict"] == "fail"
    assert item["claim_id"] == failed.id


def test_duplicate_deterministic_request_returns_existing_claim_and_writes_no_new_one(tmp_path):
    client, cfg, store = _client(tmp_path)
    config = {}
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
    known_component_prefixes = ("analyzer:", "builder:", "oracle:", "gateway:")
    for row in ACTION_TABLE:
        for predicate_type in row.emits:
            assert predicate_type in PREDICATE_TYPES
        for predicate_type, subject_kind in row.requires:
            assert predicate_type in PREDICATE_TYPES
            assert subject_kind in ("tree", "frozen")
        if row.component is None:
            # The one row per phase that names the whole requirement list
            # has nothing to dispatch to.
            assert row.name in ("accept", "onboarded")
        else:
            assert row.component.startswith(known_component_prefixes)
        assert row.phase in PHASES
        if row.emits:
            assert row.deterministic == all(PREDICATE_TYPES[pt].deterministic for pt in row.emits)
        assert isinstance(row.config_keys, tuple)
        assert all(isinstance(k, str) for k in row.config_keys)


def test_a_config_key_the_row_does_not_declare_is_rejected_before_dispatch(tmp_path):
    # An undeclared key would hash into duplicate detection and make an
    # identical request look new, so it is a malformed request, not a
    # run.
    client, cfg, store = _client(tmp_path)

    r = client.post(
        "/run", json={"action": "sese_check", "region": cfg.region_id, "config": {"junk": 1}}, headers=HEADERS,
    )

    assert r.status_code == 400
    assert "junk" in r.json()["detail"]
    assert store.all_requests() == []  # rejected like an unknown action: no log line

    # A declared key passes validation ("repeats" on the timing rows).
    r = client.post(
        "/run", json={"action": "time_baseline", "region": cfg.region_id, "config": {"repeats": 3}}, headers=HEADERS,
    )
    assert r.status_code == 200


def test_unknown_action_and_the_componentless_accept_row_are_rejected(tmp_path):
    client, cfg, store = _client(tmp_path)

    r1 = client.post(
        "/run", json={"action": "not_a_real_action", "region": cfg.region_id, "config": {}}, headers=HEADERS,
    )
    assert r1.status_code == 400

    r2 = client.post("/run", json={"action": "accept", "region": cfg.region_id, "config": {}}, headers=HEADERS)
    assert r2.status_code == 400


def test_ready_action_without_a_configured_builder_reports_that_plainly(tmp_path):
    # time_baseline requires nothing and is fully wired up,
    # but this gateway (like this test's _client()) was never given a
    # builder client -- a real deployment shape where the ledger/analyzer
    # side works before the builder or oracle are reachable.
    client, cfg, store = _client(tmp_path)

    r = client.post("/run", json={"action": "time_baseline", "region": cfg.region_id, "config": {}}, headers=HEADERS)
    body = r.json()

    assert body["error"] == "builder not configured"
    assert store.all_requests()[-1].outcome == "error"


def test_run_records_the_caller_tool_call_id_whatever_the_outcome(tmp_path):
    # The caller's own id for the tool call it is serving, so that a
    # session transcript and the request log can be lined up call by call
    # instead of guessed at from one-second timestamps. Every outcome
    # writes its line the same way, so a refusal is as traceable back to
    # the call that caused it as a claim is.
    client, cfg, store = _client(tmp_path)
    allow_globs = [SPEC_PATH]
    baseline = tracked_files(cfg.repo_dir, "main")
    frozen_sha = frozen_subject([f for f in baseline if f["path"] not in allow_globs]).sha256
    store.record_claim(
        [Subject(kind="frozen", sha256=frozen_sha)], "sese/verified",
        Predicate(tool="sese_check", version="0.1", configHash=config_hash({}), verdict="pass",
                  detail={"allow_globs": allow_globs}),
        [], "sess-0",
    )

    client.post(
        "/run", json={"action": "sese_check", "region": cfg.region_id, "config": {}},
        headers={**HEADERS, "X-Tool-Call-Id": "tool:1:aaa"},
    )
    client.post(
        "/run", json={"action": "run_replay", "region": cfg.region_id, "config": {}},
        headers={**HEADERS, "X-Tool-Call-Id": "tool:1:bbb"},
    )
    client.post(
        "/run", json={"action": "time_baseline", "region": cfg.region_id, "config": {}},
        headers={**HEADERS, "X-Tool-Call-Id": "tool:1:ccc"},
    )

    assert [(line.outcome, line.tool_call_id) for line in store.all_requests()] == [
        ("duplicate", "tool:1:aaa"), ("refused", "tool:1:bbb"), ("error", "tool:1:ccc"),
    ]


def test_a_run_without_the_header_records_no_tool_call_id(tmp_path):
    client, cfg, store = _client(tmp_path)

    client.post("/run", json={"action": "build_replay", "region": cfg.region_id, "config": {}}, headers=HEADERS)

    line = store.all_requests()[-1]
    assert line.tool_call_id is None
    assert "tool_call_id" not in line.to_dict()
