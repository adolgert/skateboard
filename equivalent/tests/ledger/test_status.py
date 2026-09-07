import json


from equivalent.ledger.acceptance import (
    ACCEPTANCE_REQUIREMENTS,
    ONBOARDING,
    ONBOARDING_REQUIREMENTS,
    PORTING,
    requirements_for,
)
from equivalent.ledger.status import compute_history, compute_status
from equivalent.ledger.store import LedgerStore
from equivalent.ledger.subjects import Subject


def _claim(claim_id, ts, subject_kind, sha256, predicate_type, verdict):
    return {
        "id": claim_id,
        "ts": ts,
        "subject": [{"kind": subject_kind, "sha256": sha256}],
        "predicateType": predicate_type,
        "predicate": {"tool": "t", "version": "0.1", "configHash": "cfg", "verdict": verdict, "detail": {}},
        "materials": [],
        "session": "sess-1",
        "version": 2,
    }


def _write_claims(store, claims):
    text = "\n".join(json.dumps(c) for c in claims) + "\n"
    store.claims_path.write_text(text)


def _all_passing_claims(tree, frozen):
    claims = []
    for i, req in enumerate(ACCEPTANCE_REQUIREMENTS, start=1):
        sha = frozen if req.subject_kind == "frozen" else tree
        claims.append(_claim(f"c-{i:04d}", f"2026-01-01T00:00:{i:02d}Z", req.subject_kind, sha, req.predicate_type, "pass"))
    return claims


def test_status_reports_the_newer_tree(tmp_path):
    tree_old, tree_new = "1" * 64, "2" * 64
    store = LedgerStore(tmp_path / "region")
    _write_claims(store, [
        _claim("c-0001", "2026-01-01T00:00:00Z", "tree", tree_old, "build/replay", "pass"),
        _claim("c-0002", "2026-01-02T00:00:00Z", "tree", tree_new, "build/replay", "pass"),
    ])

    status = compute_status(store, ACCEPTANCE_REQUIREMENTS, PORTING, context_verified=True)
    assert status["tree"] == tree_new


def test_history_lists_every_tree_and_the_older_trees_claims_only_there(tmp_path):
    tree_old, tree_new = "1" * 64, "2" * 64
    store = LedgerStore(tmp_path / "region")
    _write_claims(store, [
        _claim("c-0001", "2026-01-01T00:00:00Z", "tree", tree_old, "build/replay", "pass"),
        _claim("c-0002", "2026-01-02T00:00:00Z", "tree", tree_new, "build/replay", "pass"),
    ])

    history = compute_history(store)
    by_tree = {h["tree"]: h["claims"] for h in history}
    assert set(by_tree) == {tree_old, tree_new}
    assert [c["claim_id"] for c in by_tree[tree_old]] == ["c-0001"]
    assert [c["claim_id"] for c in by_tree[tree_new]] == ["c-0002"]


def test_status_is_accepted_when_every_requirement_passes_on_one_tree(tmp_path):
    tree, frozen = "a" * 64, "b" * 64
    store = LedgerStore(tmp_path / "region")
    _write_claims(store, _all_passing_claims(tree, frozen))

    status = compute_status(store, ACCEPTANCE_REQUIREMENTS, PORTING, context_verified=True)
    assert status["accepted"] is True
    assert status["context_verified"] is True
    assert "note" not in status
    assert all(row["status"] == "present" and row["verdict"] == "pass" for row in status["rows"])


def test_a_reader_that_cannot_vouch_for_the_executables_is_told_why_it_is_not_accepted(tmp_path):
    # Every requirement passed, and the reading is still withheld: what
    # is missing is anyone able to say the executables these claims were
    # reached against are the ones in place now.
    tree, frozen = "a" * 64, "b" * 64
    store = LedgerStore(tmp_path / "region")
    _write_claims(store, _all_passing_claims(tree, frozen))

    status = compute_status(store, ACCEPTANCE_REQUIREMENTS, PORTING, context_verified=False)

    assert status["accepted"] is False
    assert status["context_verified"] is False
    assert "executables" in status["note"]
    assert all(row["status"] == "present" for row in status["rows"])


def test_status_reports_a_removed_claim_as_missing_with_its_producing_action(tmp_path):
    tree, frozen = "a" * 64, "b" * 64
    store = LedgerStore(tmp_path / "region")
    claims = [c for c in _all_passing_claims(tree, frozen) if c["predicateType"] != "regression/holdout"]
    _write_claims(store, claims)

    status = compute_status(store, ACCEPTANCE_REQUIREMENTS, PORTING, context_verified=True)
    assert status["accepted"] is False
    missing = [row for row in status["rows"] if row["status"] == "missing"]
    assert len(missing) == 1
    assert missing[0]["predicateType"] == "regression/holdout"
    assert missing[0]["producing_action"] == "regression_holdout"


def test_a_failing_latest_claim_does_not_satisfy_a_requirement(tmp_path):
    # A fail newer than a pass means the requirement is unmet again; the
    # row reports missing but names the failing claim so the reader knows
    # a run happened and failed rather than never ran.
    tree, frozen = "a" * 64, "b" * 64
    store = LedgerStore(tmp_path / "region")
    claims = _all_passing_claims(tree, frozen)
    claims.append(_claim("c-0099", "2026-01-02T00:00:00Z", "tree", tree, "build/replay", "fail"))
    _write_claims(store, claims)

    status = compute_status(store, ACCEPTANCE_REQUIREMENTS, PORTING, context_verified=True)
    assert status["accepted"] is False
    row = next(r for r in status["rows"] if r["predicateType"] == "build/replay")
    assert row["status"] == "missing"
    assert row["verdict"] == "fail"
    assert row["claim_id"] == "c-0099"
    assert row["producing_action"] == "build_replay"


def test_status_on_an_empty_ledger_has_no_tree_and_is_not_accepted(tmp_path):
    store = LedgerStore(tmp_path / "region")
    status = compute_status(store, ACCEPTANCE_REQUIREMENTS, PORTING, context_verified=True)
    assert status["tree"] is None
    assert status["accepted"] is False
    assert all(row["status"] == "missing" for row in status["rows"])


def test_legacy_claim_is_reported_stale_and_cannot_satisfy_requirement(tmp_path):
    tree = "a" * 64
    store = LedgerStore(tmp_path / "region")
    legacy = _claim("c-0001", "2026-01-01T00:00:00Z", "tree", tree, "build/replay", "pass")
    legacy["version"] = 1
    _write_claims(store, [legacy])

    status = compute_status(
        store,
        [next(r for r in ACCEPTANCE_REQUIREMENTS if r.predicate_type == "build/replay")],
        PORTING,
        context_verified=True,
    )

    assert status["accepted"] is False
    assert status["rows"][0]["status"] == "missing"
    assert status["rows"][0]["evidence_status"] == "stale"
    assert status["rows"][0]["claim_id"] == "c-0001"


def test_changed_strategy_material_invalidates_an_otherwise_passing_claim(tmp_path):
    tree = "a" * 64
    store = LedgerStore(tmp_path / "region")
    old_strategy = {"kind": "strategy", "sha256": "1" * 64}
    new_strategy = Subject(kind="strategy", sha256="2" * 64)
    claim = _claim("c-0001", "2026-01-01T00:00:00Z", "tree", tree, "build/replay", "pass")
    claim["materials"] = [old_strategy]
    _write_claims(store, [claim])

    status = compute_status(
        store,
        [next(r for r in ACCEPTANCE_REQUIREMENTS if r.predicate_type == "build/replay")],
        PORTING,
        tree=Subject(kind="tree", sha256=tree),
        required_materials=[new_strategy],
        context_verified=True,
    )

    assert status["accepted"] is False
    assert status["rows"][0]["evidence_status"] == "stale"


def test_an_onboarding_region_is_judged_by_the_onboarding_list(tmp_path):
    tree = "3" * 64
    store = LedgerStore(tmp_path / "region")
    _write_claims(store, [
        _claim(f"c-{i:04d}", f"2026-01-01T00:00:{i:02d}Z", "tree", tree, req.predicate_type, "pass")
        for i, req in enumerate(ONBOARDING_REQUIREMENTS, start=1)
    ])

    status = compute_status(store, requirements_for(ONBOARDING), ONBOARDING, context_verified=True)

    assert status["phase"] == ONBOARDING
    assert [row["predicateType"] for row in status["rows"]] == [
        req.predicate_type for req in ONBOARDING_REQUIREMENTS
    ]
    assert status["accepted"] is True
    # None of the porting requirements is asked for: an onboarding session
    # has no region to run the analyzer on and no port to time.
    assert "sese/verified" not in json.dumps(status)


def test_the_same_ledger_read_as_a_port_is_missing_everything(tmp_path):
    tree = "3" * 64
    store = LedgerStore(tmp_path / "region")
    _write_claims(store, [
        _claim(f"c-{i:04d}", f"2026-01-01T00:00:{i:02d}Z", "tree", tree, req.predicate_type, "pass")
        for i, req in enumerate(ONBOARDING_REQUIREMENTS, start=1)
    ])

    status = compute_status(store, requirements_for(PORTING), PORTING, context_verified=True)

    assert status["phase"] == PORTING
    assert all(row["status"] == "missing" for row in status["rows"])
