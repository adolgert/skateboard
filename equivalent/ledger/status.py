"""Compute a region's status from its ledger: current tree, which claims
are present, which are missing, and whether the region has met every
requirement of its phase.

The ledger CLI and the gateway's GET /status must both render from this
function, not two copies of it, or they will drift apart.
"""
from __future__ import annotations

from .evidence import claim_matches_context
from .store import LedgerStore
from .subjects import Subject

# What a reader is told when nobody could confirm the executable context.
# Acceptance is withheld in that case, so the note says why rather than
# leaving a reader to wonder which of the rows below is the problem.
ADVISORY_NOTE = (
    "Advisory only: nothing here confirmed that the executables these claims were "
    "reached against are the ones in place now, so acceptance is withheld."
)


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


def requirement_status(
    store: LedgerStore, predicate_type: str, subject: Subject | None,
    producing_action: str, *, required_materials=(),
) -> dict:
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
    claim = (
        store.latest(predicate_type, subject, required_materials=required_materials)
        if subject is not None else None
    )
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
    # Preserve old and superseded claims as explicit history without letting
    # them silently satisfy the current policy.  This is especially useful
    # after a strategy, manifest, dataset, or evidence-policy upgrade.
    stale = store.latest_unchecked(predicate_type, subject) if subject is not None else None
    if stale is not None:
        return {
            "predicateType": predicate_type,
            "status": "missing",
            "evidence_status": "stale",
            "verdict": stale.predicate.verdict,
            "claim_id": stale.id,
            "producing_action": producing_action,
            "reason": "claim was produced under a different or legacy evidence context",
        }
    return {"predicateType": predicate_type, "status": "missing", "producing_action": producing_action}


def compute_status(
    store: LedgerStore, requirements, phase: str,
    tree: Subject | None = None, frozen: Subject | None = None,
    *, required_materials=(), required_materials_by_predicate=None,
    context_verified: bool,
) -> dict:
    """Status for the region's current tree, against one phase's requirements.

    Whether the region is accepted is decided here and nowhere else. A
    caller renders this answer; it does not recompute it, or two readers
    of one ledger would disagree about whether a port is done.

    `requirements` is the list the region's phase is judged by
    (`equivalent.ledger.acceptance.requirements_for`), and `phase` is that
    phase's name, which travels with the answer so a reader knows which
    list it is looking at without matching the rows against both.

    `tree` and `frozen` are the caller's answer to "what is current right
    now"; pass them when you have a better source than the ledger itself
    (see `_current_subject`). Leave them out to fall back to the guess.

    `context_verified` is the one thing the caller knows that the ledger
    cannot: whether the executables these claims were reached against
    have been confirmed to be the ones in place now. The ledger holds
    only the identities a claim named; nothing in it can say those
    identities still exist. It is a required argument because a caller
    that never thought about the question has no business being read as
    having answered it, and acceptance needs a yes.
    """
    if tree is None:
        tree = _current_subject(store, "tree")
    if frozen is None:
        frozen = _current_subject(store, "frozen")

    rows = []
    required_materials_by_predicate = required_materials_by_predicate or {}
    for req in requirements:
        subject = tree if req.subject_kind == "tree" else frozen
        rows.append(requirement_status(
            store, req.predicate_type, subject, req.producing_action,
            required_materials=required_materials_by_predicate.get(
                req.predicate_type, required_materials,
            ),
        ))

    status = {
        "tree": tree.sha256 if tree else None,
        "frozen": frozen.sha256 if frozen else None,
        "phase": phase,
        "rows": rows,
        "accepted": context_verified and tree is not None and all(
            row["status"] == "present" and row["verdict"] == "pass" for row in rows
        ),
        "context_verified": context_verified,
    }
    if not context_verified:
        status["note"] = ADVISORY_NOTE
    return status


def accepted_by(claims, requirements, *, required_materials=()) -> bool:
    """Would these claims, and no others, finish some tree?

    This is the same acceptance rule `compute_status` applies, asked of a
    hand-held list of claims rather than of everything a ledger holds: the
    session summary replays one session's claims to say when in the run
    acceptance was reached. A claim reached outside the current evidence
    contract does not count here either, so the summary and the status
    table cannot tell a reader two different stories about one ledger.

    Every requirement is read from the phase's own list rather than named
    here, so a requirement added there counts here without an edit. A
    requirement on the frozen files is met by any frozen value that
    passes, because a session's claims cannot say which frozen value was
    current at the time.
    """
    latest = {}
    for claim in claims:
        if not claim_matches_context(claim, required_materials):
            continue
        for subject in claim.subject:
            latest[(claim.predicateType, subject)] = claim.predicate.verdict
    trees = {subject for (_, subject) in latest if subject.kind == "tree"}
    frozen = {subject for (_, subject) in latest if subject.kind == "frozen"}
    for tree in trees:
        subjects_of = {"tree": [tree], "frozen": sorted(frozen, key=lambda s: s.sha256)}
        if all(
            any(latest.get((req.predicate_type, s)) == "pass" for s in subjects_of[req.subject_kind])
            for req in requirements
        ):
            return True
    return False


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
