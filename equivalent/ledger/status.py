"""Compute a region's status from its ledger: current tree, which claims
are present, which are missing, and whether the region is accepted.

The ledger CLI (Step 3) and the gateway's GET /status (Step 5) must both
render from this function, not two copies of it, or they will drift apart.
"""
from __future__ import annotations

from .acceptance import ACCEPTANCE_REQUIREMENTS
from .store import LedgerStore
from .subjects import Subject


def _current_subject(store: LedgerStore, kind: str):
    """The subject of this kind attached to the most recent claim, if any.

    Nothing before Step 4 (submit) tracks "the current tree" directly, so
    it is read off the claims themselves: the most recently timestamped
    claim that names a subject of this kind.
    """
    best_ts = None
    best_subject = None
    for claim in store.all_claims():
        for s in claim.subject:
            if s.kind == kind and (best_ts is None or claim.ts >= best_ts):
                best_ts = claim.ts
                best_subject = s
    return best_subject


def compute_status(store: LedgerStore) -> dict:
    tree = _current_subject(store, "tree")
    frozen = _current_subject(store, "frozen")

    rows = []
    for req in ACCEPTANCE_REQUIREMENTS:
        subject = tree if req.subject_kind == "tree" else frozen
        claim = store.latest(req.predicate_type, subject) if subject is not None else None
        if claim is not None:
            rows.append({
                "predicateType": req.predicate_type,
                "status": "present",
                "verdict": claim.predicate.verdict,
                "claim_id": claim.id,
            })
        else:
            rows.append({
                "predicateType": req.predicate_type,
                "status": "missing",
                "producing_action": req.producing_action,
            })

    accepted = tree is not None and all(
        row["status"] == "present" and row["verdict"] == "pass" for row in rows
    )
    return {
        "tree": tree.sha256 if tree else None,
        "frozen": frozen.sha256 if frozen else None,
        "rows": rows,
        "accepted": accepted,
    }


def compute_history(store: LedgerStore) -> list[dict]:
    history = []
    for sha256 in store.list_trees():
        claims = store.claims_for(Subject(kind="tree", sha256=sha256))
        history.append({
            "tree": sha256,
            "claims": [
                {"predicateType": c.predicateType, "verdict": c.predicate.verdict, "claim_id": c.id, "ts": c.ts}
                for c in claims
            ],
        })
    return history
