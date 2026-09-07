from dataclasses import replace

from fastapi.testclient import TestClient

from equivalent.gateway.app import config_hash, create_app
from equivalent.gateway.dispatch import HANDLERS
from equivalent.region.evidence import evidence_materials_for
from equivalent.region.current import current_tree_and_frozen
from equivalent.tree import init_baseline_repo
from equivalent.ledger.table import (
    ACTION_TABLE,
    CONFIG_KEY_SPECS,
    REQUEST_SUBJECT_KINDS,
    subject_kind_of,
    subjects_by_kind,
)
from equivalent.ledger.acceptance import PHASES
from equivalent.ledger.predicates import PREDICATE_TYPES
from equivalent.ledger.records import Predicate
from equivalent.ledger.store import LedgerStore
from equivalent.ledger.subjects import Subject
from equivalent.manifest.schema import load_manifest
from equivalent.strategy.schema import load_strategy
from equivalent.tests.gateway.conftest import SPEC_PATH, STRATEGY_PATH, region_config
from equivalent.tests.fakes import write_program

TOKEN = "test-token"
HEADERS = {"Authorization": f"Bearer {TOKEN}", "X-Session-Id": "sess-1", "X-Model-Id": "claude-sonnet-5"}


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
    return region_config(
        tmp_path, repo_dir=repo_dir, working_copy_dir=working,
        manifest=load_manifest(write_program(tmp_path) / "manifest.yaml"),
    )


def _current(cfg, store):
    """The tree and frozen hashes the gateway itself would compute."""
    return current_tree_and_frozen(
        cfg.repo_dir, cfg.region_id, store, cfg.spec_path, cfg.phase,
        load_strategy(cfg.strategy_path), required_materials=evidence_materials_for(cfg),
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


def test_sese_requirement_is_invalidated_by_each_candidate_edit(tmp_path):
    cfg = _region(tmp_path, with_makefile=True)
    store = LedgerStore(cfg.ledger_dir)
    allow_globs = ["src/mod_kernel.f90", SPEC_PATH]
    tree_sha, _ = _current(cfg, store)
    materials = evidence_materials_for(cfg)

    store.record_claim(
        [Subject(kind="tree", sha256=tree_sha)], "sese/verified",
        Predicate(tool="sese_check", version="0.1", configHash="cfg", verdict="pass",
                  detail={"allow_globs": allow_globs}),
        materials, "sess-0",
    )

    client = TestClient(create_app({cfg.region_id: cfg}, TOKEN))
    working = cfg.working_copy_dir
    (working / "src").mkdir(parents=True)
    (working / "src" / "mod_kernel.f90").write_text("subroutine step\n  x = 1\nend subroutine\n")
    client.post("/submit", json={"region": cfg.region_id}, headers=HEADERS)
    tree_a, _ = _current(cfg, store)
    missing_after_first_edit = client.post(
        "/run", json={"action": "build_replay", "region": cfg.region_id, "config": {}}, headers=HEADERS,
    ).json()
    assert missing_after_first_edit["refused"] is True
    assert missing_after_first_edit["missing"][0]["predicateType"] == "sese/verified"

    store.record_claim(
        [Subject(kind="tree", sha256=tree_a)], "sese/verified",
        Predicate(tool="sese_check", version="0.1", configHash="cfg", verdict="pass",
                  detail={"allow_globs": allow_globs}),
        materials, "sess-0",
    )

    (working / "src" / "mod_kernel.f90").write_text("subroutine step\n  x = 2\nend subroutine\n")
    client.post("/submit", json={"region": cfg.region_id}, headers=HEADERS)

    now_missing_sese = client.post(
        "/run", json={"action": "build_replay", "region": cfg.region_id, "config": {}}, headers=HEADERS,
    ).json()
    assert now_missing_sese["refused"] is True
    assert now_missing_sese["missing"][0]["predicateType"] == "sese/verified"


def test_a_failing_requirement_claim_still_refuses_the_dependent_action(tmp_path):
    # A claim that exists but failed must not open the gate; the refusal
    # names the failing claim so the model knows to fix and re-run, not
    # that the check never ran.
    client, cfg, store = _client(tmp_path)
    _submit_spec_only(client, cfg)
    tree_sha, _ = _current(cfg, store)
    failed = store.record_claim(
        [Subject(kind="tree", sha256=tree_sha)], "sese/verified",
        Predicate(tool="sese_check", version="0.1", configHash="cfg", verdict="fail", detail={}),
        evidence_materials_for(cfg), "sess-0",
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
    tree_sha, _ = _current(cfg, store)
    existing = store.record_claim(
        [Subject(kind="tree", sha256=tree_sha)], "sese/verified",
        Predicate(tool="sese_check", version="0.1", configHash=config_hash(config), verdict="pass",
                  detail={"allow_globs": allow_globs}),
        evidence_materials_for(cfg), "sess-0",
    )

    r = client.post("/run", json={"action": "sese_check", "region": cfg.region_id, "config": config}, headers=HEADERS)
    body = r.json()

    assert body["claim_id"] == existing.id
    assert len(store.all_claims()) == 1
    assert store.all_requests()[-1].outcome == "duplicate"


def test_strategy_change_invalidates_status_and_duplicate_evidence(tmp_path):
    cfg = _region(tmp_path)
    strategy_path = tmp_path / "strategy.yaml"
    strategy_path.write_bytes(STRATEGY_PATH.read_bytes())
    cfg = replace(cfg, strategy_path=strategy_path)
    store = LedgerStore(cfg.ledger_dir)
    tree_sha, _ = _current(cfg, store)
    old = store.record_claim(
        [Subject(kind="tree", sha256=tree_sha)], "sese/verified",
        Predicate(tool="sese_check", version="0.1", configHash=config_hash({}),
                  verdict="pass", detail={"allow_globs": [SPEC_PATH]}),
        evidence_materials_for(cfg), "sess-0",
    )
    client = TestClient(create_app({cfg.region_id: cfg}, TOKEN))

    text = strategy_path.read_text()
    strategy_path.write_text(text.replace("-O2", "-O3"))
    status = client.get("/status", params={"region": cfg.region_id}, headers=HEADERS).json()
    row = next(item for item in status["rows"] if item["predicateType"] == "sese/verified")

    assert row["status"] == "missing"
    assert row["evidence_status"] == "stale"
    assert row["claim_id"] == old.id
    assert status["accepted"] is False


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
            assert subject_kind in REQUEST_SUBJECT_KINDS
        if not row.dispatchable:
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
        # Every key a row takes has to say what it is: that wording is
        # what a session is offered, and what POST /run types the value
        # against.
        for key in row.config_keys:
            assert key in CONFIG_KEY_SPECS
            assert CONFIG_KEY_SPECS[key]["type"] == "integer"
            assert CONFIG_KEY_SPECS[key]["description"]

    build = next(row for row in ACTION_TABLE if row.name == "build_replay")
    assert ("sese/verified", "tree") in build.requires
    visible = next(row for row in ACTION_TABLE if row.name == "regression_visible")
    assert set(visible.requires) == {
        ("sanitize/memcheck", "tree"),
        ("sanitize/racecheck", "tree"),
        ("sanitize/initcheck", "tree"),
        # The outputs this compares are the ones the run claim recorded.
        ("gpu/executed", "tree"),
    }


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
        "/run", json={"action": "time_baseline", "region": cfg.region_id, "config": {"repeats": 5}}, headers=HEADERS,
    )
    assert r.status_code == 200


def test_a_config_value_of_the_wrong_type_is_rejected_naming_the_key(tmp_path):
    # The value is checked as well as the name: a repeat count that is
    # not a number would hash into duplicate detection and then fail
    # inside a component, where the caller reads a traceback instead of
    # which setting it got wrong.
    client, cfg, store = _client(tmp_path)

    r = client.post(
        "/run",
        json={"action": "time_baseline", "region": cfg.region_id, "config": {"repeats": "lots"}},
        headers=HEADERS,
    )

    assert r.status_code == 400
    assert "repeats" in r.json()["detail"]
    assert store.all_requests() == []  # malformed, so no log line

    # A boolean is not a repeat count either, though Python counts it as
    # an int.
    r = client.post(
        "/run",
        json={"action": "time_baseline", "region": cfg.region_id, "config": {"repeats": True}},
        headers=HEADERS,
    )
    assert r.status_code == 400
    assert "repeats" in r.json()["detail"]


def test_config_values_below_acceptance_minimum_or_above_bound_are_rejected(tmp_path):
    client, cfg, store = _client(tmp_path)

    for value in (0, 4, 101):
        response = client.post(
            "/run",
            json={"action": "time_baseline", "region": cfg.region_id,
                  "config": {"repeats": value}},
            headers=HEADERS,
        )
        assert response.status_code == 400
        assert "repeats" in response.json()["detail"]
    assert store.all_requests() == []


def test_action_from_another_phase_is_rejected_before_dispatch(tmp_path):
    cfg = replace(_region(tmp_path), phase="onboarding", spec_path=None)
    client = TestClient(create_app({cfg.region_id: cfg}, TOKEN))

    response = client.post(
        "/run", json={"action": "sese_check", "region": cfg.region_id, "config": {}},
        headers=HEADERS,
    )

    assert response.status_code == 400
    assert "belongs to phase 'porting'" in response.json()["detail"]


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
    tree_sha, _ = _current(cfg, store)
    store.record_claim(
        [Subject(kind="tree", sha256=tree_sha)], "sese/verified",
        Predicate(tool="sese_check", version="0.1", configHash=config_hash({}), verdict="pass",
                  detail={"allow_globs": allow_globs}),
        evidence_materials_for(cfg), "sess-0",
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


def test_a_build_claim_that_names_no_executable_is_an_error_not_a_refusal(tmp_path):
    # A passing build claim whose detail names no executable cannot be
    # depended on, but neither is it a missing requirement: the session
    # has nothing to run and would be told to run a check that is already
    # present. It is reported as the unusable precondition it is.
    client, cfg, store = _client(tmp_path)
    _submit_spec_only(client, cfg)
    tree_sha, _ = _current(cfg, store)
    materials = evidence_materials_for(cfg)
    store.record_claim(
        [Subject(kind="tree", sha256=tree_sha)], "sese/verified",
        Predicate(tool="sese_check", version="0.1", configHash="cfg", verdict="pass",
                  detail={"allow_globs": [SPEC_PATH]}),
        materials, "sess-0",
    )
    store.record_claim(
        [Subject(kind="tree", sha256=tree_sha)], "build/replay",
        Predicate(tool="builder", version="0.1", configHash="cfg", verdict="pass",
                  detail={"attempt_id": "att-1", "targets": {}}),
        materials, "sess-0",
    )

    response = client.post(
        "/run", json={"action": "run_replay", "region": cfg.region_id, "config": {}},
        headers=HEADERS,
    )

    assert response.status_code == 503
    assert "names no executable" in response.json()["detail"]


def test_a_body_with_a_field_the_endpoint_does_not_have_is_rejected(tmp_path):
    # A misspelled "config" that was quietly dropped would run the action
    # with its defaults and report a success nobody asked for, so an
    # unknown field is the caller's mistake and is named as one. Nothing
    # about it reaches the region's request log: no action ever ran.
    client, cfg, store = _client(tmp_path)

    r = client.post(
        "/run",
        json={"action": "sese_check", "region": cfg.region_id, "configuration": {"repeats": 5}},
        headers=HEADERS,
    )

    assert r.status_code == 400
    assert "configuration" in r.json()["detail"]
    assert store.all_requests() == []


def _passing_claim(store, cfg, predicate_type, detail):
    tree_sha, _ = _current(cfg, store)
    return store.record_claim(
        [Subject(kind="tree", sha256=tree_sha)], predicate_type,
        Predicate(tool="t", version="0.1", configHash="cfg", verdict="pass", detail=detail),
        evidence_materials_for(cfg), "sess-0",
    )


def test_a_lost_build_that_no_configured_builder_could_remake_is_reported_plainly(
    tmp_path, monkeypatch,
):
    # A gateway with no builder cannot ask whether the executables are
    # still there, so the build reads as lost. What it must not do is
    # dispatch a rebuild at a client it has not got: it answers that the
    # backend is not configured, files no claim, and runs no check.
    def never(ctx, config):
        raise AssertionError("no check may run without the backend it needs")

    monkeypatch.setitem(HANDLERS, "build_replay", replace(HANDLERS["build_replay"], check=never))
    client, cfg, store = _client(tmp_path)
    _submit_spec_only(client, cfg)
    _passing_claim(store, cfg, "sese/verified", {"allow_globs": [SPEC_PATH]})
    _passing_claim(store, cfg, "build/replay", {
        "attempt_id": "att-1", "targets": {"replay": {"executable": "replay", "sha256": "e" * 64}},
    })

    body = client.post(
        "/run", json={"action": "run_replay", "region": cfg.region_id, "config": {}},
        headers=HEADERS,
    ).json()

    assert body == {"error": "builder not configured"}
    assert [claim.predicateType for claim in store.all_claims()] == [
        "sese/verified", "build/replay",
    ]
    assert store.all_requests()[-1].outcome == "error"


def test_every_action_files_its_claims_against_the_subject_its_row_records():
    # The gateway looks for a repeat of a request under the subject the
    # table records for the predicate, and files the new claim under the
    # subject the check's own answer names. The two disagreeing would
    # file a claim the duplicate lookup could never find again, so every
    # handler declares which subject it files against.
    for row in ACTION_TABLE:
        if not row.dispatchable:
            continue
        declared = HANDLERS[row.name].subject_kind
        assert declared in REQUEST_SUBJECT_KINDS
        for predicate_type in row.emits:
            assert subject_kind_of(predicate_type) == declared
    # The one measurement that is not about the candidate tree.
    assert HANDLERS["time_baseline"].subject_kind == "baseline_tree"


def test_a_request_resolves_a_subject_for_every_kind_the_rows_name():
    subjects = subjects_by_kind(tree="a" * 64, frozen="b" * 64, baseline_tree="c" * 64)

    assert set(subjects) == set(REQUEST_SUBJECT_KINDS)
    # The baseline is a tree like any other; only the name it is looked
    # up by says which tree it is.
    assert subjects["baseline_tree"].kind == "tree"
    assert subjects["baseline_tree"].sha256 == "c" * 64
