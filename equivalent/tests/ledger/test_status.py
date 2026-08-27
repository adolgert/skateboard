import json

import pytest

from equivalent.ledger.acceptance import ACCEPTANCE_REQUIREMENTS
from equivalent.ledger.status import compute_history, compute_status
from equivalent.ledger.store import LedgerStore


def _claim(claim_id, ts, subject_kind, sha256, predicate_type, verdict):
    return {
        "id": claim_id,
        "ts": ts,
        "subject": [{"kind": subject_kind, "sha256": sha256}],
        "predicateType": predicate_type,
        "predicate": {"tool": "t", "version": "0.1", "configHash": "cfg", "verdict": verdict, "detail": {}},
        "materials": [],
        "session": "sess-1",
        "version": 1,
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

    status = compute_status(store)
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

    status = compute_status(store)
    assert status["accepted"] is True
    assert all(row["status"] == "present" and row["verdict"] == "pass" for row in status["rows"])


def test_status_reports_a_removed_claim_as_missing_with_its_producing_action(tmp_path):
    tree, frozen = "a" * 64, "b" * 64
    store = LedgerStore(tmp_path / "region")
    claims = [c for c in _all_passing_claims(tree, frozen) if c["predicateType"] != "regression/holdout"]
    _write_claims(store, claims)

    status = compute_status(store)
    assert status["accepted"] is False
    missing = [row for row in status["rows"] if row["status"] == "missing"]
    assert len(missing) == 1
    assert missing[0]["predicateType"] == "regression/holdout"
    assert missing[0]["producing_action"] == "regression_holdout"


def test_status_on_an_empty_ledger_has_no_tree_and_is_not_accepted(tmp_path):
    store = LedgerStore(tmp_path / "region")
    status = compute_status(store)
    assert status["tree"] is None
    assert status["accepted"] is False
    assert all(row["status"] == "missing" for row in status["rows"])
