"""Compute a region's status from its ledger: current tree, which claims
are present, which are missing, and whether the region has met every
requirement of its phase.

The ledger CLI and the gateway's GET /status must both render from this
function, not two copies of it, or they will drift apart.
"""
from __future__ import annotations

from .store import LedgerStore
from .subjects import Subject


def _current_subject(store: LedgerStore, kind: str):
    """The subject of this kind attached to the most recent claim, if any.

    This is a guess, used only when the caller has no better source: it
    reports no current tree at all until some check has actually run,
    which is wrong for a region that has been submitted but not yet
    checked. The gateway knows the real current tree from its own
    git repo and passes it in as `compute_status`'s `tree` argument
    instead of relying on this. The ledger CLI, which has no repo to read,
    still falls back to this.
    """
    best_ts = None
    best_subject = None
    for claim in store.all_claims():
        for s in claim.subject:
            if s.kind == kind and (best_ts is None or claim.ts >= best_ts):
                best_ts = claim.ts
                best_subject = s
    return best_subject


def requirement_status(store: LedgerStore, predicate_type: str, subject: Subject | None, producing_action: str) -> dict:
    """Is this one requirement met? Present (with its claim) or missing (with what would produce it).

    Only a claim whose latest verdict is "pass" satisfies a requirement.
    A latest claim that failed still reports as "missing" -- with its
    verdict and claim id, so the reader knows a run happened and failed
    rather than never ran -- because a failing sese/verified must not let
    build_replay dispatch.

    This is the one place that decides what a missing or present claim
    looks like. `compute_status`'s per-row loop below and the gateway's
    /run refusal both call this, so a claim renders the same way in both
    places.
    """
    claim = store.latest(predicate_type, subject) if subject is not None else None
    if claim is not None and claim.predicate.verdict == "pass":
        return {
            "predicateType": predicate_type,
            "status": "present",
            "verdict": claim.predicate.verdict,
            "claim_id": claim.id,
        }
    if claim is not None:
        return {
            "predicateType": predicate_type,
            "status": "missing",
            "verdict": claim.predicate.verdict,
            "claim_id": claim.id,
            "producing_action": producing_action,
        }
    return {"predicateType": predicate_type, "status": "missing", "producing_action": producing_action}


def compute_status(
    store: LedgerStore, requirements, phase: str,
    tree: Subject | None = None, frozen: Subject | None = None,
) -> dict:
    """Status for the region's current tree, against one phase's requirements.

    `requirements` is the list the region's phase is judged by
    (`equivalent.ledger.acceptance.requirements_for`), and `phase` is that
    phase's name, which travels with the answer so a reader knows which
    list it is looking at without matching the rows against both.

    `tree` and `frozen` are the caller's answer to "what is current right
    now"; pass them when you have a better source than the ledger itself
    (see `_current_subject`). Leave them out to fall back to the guess.
    """
    if tree is None:
        tree = _current_subject(store, "tree")
    if frozen is None:
        frozen = _current_subject(store, "frozen")

    rows = []
    for req in requirements:
        subject = tree if req.subject_kind == "tree" else frozen
        rows.append(requirement_status(store, req.predicate_type, subject, req.producing_action))

    accepted = tree is not None and all(
        row["status"] == "present" and row["verdict"] == "pass" for row in rows
    )
    return {
        "tree": tree.sha256 if tree else None,
        "frozen": frozen.sha256 if frozen else None,
        "phase": phase,
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
