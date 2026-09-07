import json
from pathlib import Path

from equivalent.cli import render
from equivalent.ledger.acceptance import (
    ACCEPTANCE_REQUIREMENTS,
    ONBOARDING,
    ONBOARDING_REQUIREMENTS,
    PORTING,
)
from equivalent.ledger.evidence import BUILD_PREDICATE, FOUNDATION_PREDICATES
from equivalent.ledger.records import Predicate
from equivalent.ledger.status import compute_status
from equivalent.ledger.store import LedgerStore
from equivalent.ledger.subjects import Subject
from equivalent.tests.fakes import build_claim_detail

GOLDEN_DIR = Path(__file__).parent / "golden"


# The executable the build in these ledgers produced. A claim that rests
# on a build counts only when it names that executable, so a ledger that
# is meant to read as finished says so.
BINARY = Subject(kind="binary", sha256="e" * 64)


def _file_passing_claims(store, requirements, phase, tree, frozen):
    """A ledger in which every requirement of one phase has passed."""
    for req in requirements:
        sha256 = frozen if req.subject_kind == "frozen" else tree
        subject = Subject(kind=req.subject_kind, sha256=sha256)
        built = req.predicate_type == BUILD_PREDICATE[phase]
        predicate = Predicate(
            tool="t", version="0.1", configHash="cfg", verdict="pass",
            detail=build_claim_detail(phase, BINARY.sha256) if built else {},
        )
        store.record_claim(
            [subject], req.predicate_type, predicate,
            () if req.predicate_type in FOUNDATION_PREDICATES else (BINARY,), "sess-1",
        )


def _all_passing_claims(store, tree, frozen):
    _file_passing_claims(store, ACCEPTANCE_REQUIREMENTS, PORTING, tree, frozen)


def test_status_text_matches_golden_file(tmp_path):
    tree, frozen = "a" * 64, "b" * 64
    store = LedgerStore(tmp_path / "region")
    _all_passing_claims(store, tree, frozen)

    status = compute_status(
        store, ACCEPTANCE_REQUIREMENTS, PORTING,
        required_materials=(), context_verified=True,
    )
    text = render.render_status(status, "ch04:step")

    golden = (GOLDEN_DIR / "status_accepted.txt").read_text()
    assert text == golden


def test_a_finished_onboarding_reads_as_onboarded_rather_than_accepted(tmp_path):
    # The two words mean different things: an onboarded code is ready for
    # a person to review and promote, an accepted port is ready to merge.
    tree, frozen = "a" * 64, "b" * 64
    store = LedgerStore(tmp_path / "region")
    _file_passing_claims(store, ONBOARDING_REQUIREMENTS, ONBOARDING, tree, tree)

    status = compute_status(
        store, ONBOARDING_REQUIREMENTS, ONBOARDING,
        required_materials=(), context_verified=True,
    )
    text = render.render_status(status, "tsunami:onboarding")

    assert text.splitlines()[-1] == f"ONBOARDED on {tree[:12]}"
    assert "ACCEPTED" not in text
    assert frozen[:12] not in text


def test_render_claim_shows_full_detail_even_for_a_verdict_only_predicate():
    # regression/holdout hides detail from the agent (DetailLevel.VERDICT_ONLY),
    # but `ledger show` is for a person and must print it anyway.
    from equivalent.ledger.records import Claim
    from equivalent.ledger.subjects import Subject

    claim = Claim(
        id="c-0007",
        ts="2026-01-01T00:00:07Z",
        subject=(Subject(kind="tree", sha256="a" * 64),),
        predicateType="regression/holdout",
        predicate=Predicate(tool="oracle", version="0.3.1", configHash="cfg", verdict="pass",
                             detail={"case0000": {"field": {"max_rel": 1e-6, "pass": True}}}),
        materials=(),
        session="sess-1",
    )
    text = render.render_claim(claim)
    parsed = json.loads(text)
    assert parsed["predicate"]["detail"] == {"case0000": {"field": {"max_rel": 1e-6, "pass": True}}}


def test_render_requests_prints_one_line_per_request_in_order():
    from equivalent.ledger.records import RequestLogLine

    lines_in = [
        RequestLogLine(ts="2026-01-01T00:00:00Z", session="s1", model="m", endpoint="run",
                        action="build_replay", region="ch04:step", tree="a" * 64, config_hash=None,
                        outcome="refused", missing=({"predicateType": "sese/verified"},)),
        RequestLogLine(ts="2026-01-01T00:00:01Z", session="s1", model="m", endpoint="run",
                        action="build_replay", region="ch04:step", tree="a" * 64, config_hash="cfg",
                        outcome="claim", claim_id="c-0001"),
    ]
    text = render.render_requests(lines_in)
    out_lines = text.strip("\n").split("\n")
    assert len(out_lines) == 2
    assert "refused" in out_lines[0]
    assert "missing=" in out_lines[0]
    assert "c-0001" in out_lines[1]


def test_a_requirement_whose_check_failed_reads_as_a_fail_with_its_claim_id(tmp_path):
    # The requirement is unmet either way, but "never ran" and "ran and
    # failed" are different situations, and the claim id is what has the
    # reason in it. The pi extension's renderStatus prints the same row
    # in the same words (pi-extension/src/status.ts).
    tree = "a" * 64
    store = LedgerStore(tmp_path / "region")
    store.record_claim(
        [Subject(kind="tree", sha256=tree)], "gpu/executed",
        Predicate(tool="builder", version="0.1", configHash="cfg", verdict="fail", detail={}),
        [], "sess-1",
    )

    status = compute_status(
        store, ACCEPTANCE_REQUIREMENTS, PORTING,
        required_materials=(), context_verified=True,
    )
    lines = render.render_status(status, "ch04:step").splitlines()
    row = next(line for line in lines if "gpu/executed" in line)

    assert "fail" in row
    assert "MISSING" not in row
    assert "(fix and run run_replay again)" in row


def test_a_requirement_no_check_has_run_for_still_reads_as_missing(tmp_path):
    store = LedgerStore(tmp_path / "region")

    status = compute_status(
        store, ACCEPTANCE_REQUIREMENTS, PORTING,
        required_materials=(), context_verified=True,
    )
    row = next(
        line for line in render.render_status(status, "ch04:step").splitlines()
        if "gpu/executed" in line
    )

    assert "MISSING" in row
    assert "(run: run_replay)" in row


def test_a_claim_read_is_not_reported_as_a_claim_filed():
    # The claim endpoint reads back a verdict already recorded. Wording
    # it like a filing would put a claim on the timeline that nothing
    # produced.
    from equivalent.cli.session import TimelineRow
    from equivalent.ledger.records import RequestLogLine

    line = RequestLogLine(
        ts="2026-01-01T00:00:03Z", session="s1", model="m", endpoint="claim", action="claim",
        region="ch04:step", tree=None, config_hash=None, outcome="read", claim_id="c-0007",
    )
    row = TimelineRow(ts=line.ts, source="both", who="claim", request=line, verdict="fail")

    assert render._outcome(row) == "-> read claim c-0007 fail"


def test_an_advisory_reading_prints_the_sentence_that_says_so(tmp_path):
    # Nothing could confirm the executables, so acceptance is withheld
    # and the reading says why. The sentence is printed here, with the
    # rows, so a reader is never shown the rows without it.
    tree, frozen = "a" * 64, "b" * 64
    store = LedgerStore(tmp_path / "region")
    _all_passing_claims(store, tree, frozen)

    status = compute_status(
        store, ACCEPTANCE_REQUIREMENTS, PORTING,
        required_materials=(), context_verified=False,
    )
    text = render.render_status(status, "ch04:step")

    assert text.splitlines()[0] == status["note"]
    assert "ACCEPTED" not in text
