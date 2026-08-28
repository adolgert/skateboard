"""Reading an agent's session transcript beside the gateway's request log.

The fixture in fixtures/session-sample.jsonl is a real transcript, kept
exactly as the agent's tooling wrote it.
"""
import json
from pathlib import Path

import yaml

from equivalent.cli import render, session
from equivalent.cli.main import main
from equivalent.gateway.submit import init_baseline_repo
from equivalent.ledger.acceptance import (
    ACCEPTANCE_REQUIREMENTS,
    ONBOARDING,
    ONBOARDING_REQUIREMENTS,
    PORTING,
)
from equivalent.ledger.records import Claim, Predicate, RequestLogLine
from equivalent.ledger.store import LedgerStore
from equivalent.ledger.subjects import Subject
from equivalent.tests.fakes import write_program

FIXTURE_DIR = Path(__file__).parent / "fixtures"
GOLDEN_DIR = Path(__file__).parent / "golden"
SAMPLE = FIXTURE_DIR / "session-sample.jsonl"
STRATEGIES = Path(__file__).resolve().parents[2] / "strategy" / "files"

SAMPLE_SESSION = "01a047f0-5296-7c73-8ab1-364aad7ae166"
SUBMIT_CALL = "tool:1787913327226:mu24lznsjv"
SESE_CALL = "tool:1787913327226:zfppu5ar7hf"


def _sample_requests():
    """The two lines the gateway would have written for the sample's two calls.

    The tree and the claim id are the ones the sample's own tool results
    show the gateway answering with, so the two records describe one run.
    """
    return [
        RequestLogLine(
            ts="2026-08-28T10:35:27Z", session=SAMPLE_SESSION, model="faux-1",
            endpoint="submit", action="submit", region="ch04:step", tree="abc123",
            config_hash=None, outcome="submitted", tool_call_id=SUBMIT_CALL,
        ),
        RequestLogLine(
            ts="2026-08-28T10:35:27Z", session=SAMPLE_SESSION, model="faux-1",
            endpoint="run", action="sese_check", region="ch04:step", tree="abc123",
            config_hash="cfg", outcome="claim", claim_id="c-7", tool_call_id=SESE_CALL,
        ),
    ]


def _claim(claim_id, ts, predicate_type, subject_kind, sha256, verdict, session_id):
    return Claim(
        id=claim_id,
        ts=ts,
        subject=(Subject(kind=subject_kind, sha256=sha256),),
        predicateType=predicate_type,
        predicate=Predicate(tool="t", version="0.1", configHash="cfg", verdict=verdict, detail={}),
        materials=(),
        session=session_id,
    )


def _write_session(path, entries):
    """A transcript file: the header line the agent's tooling writes, then the entries."""
    header = {"type": "session", "version": 3, "id": "hand-built", "timestamp": "2026-01-01T00:00:00.000Z"}
    path.write_text("\n".join(json.dumps(r) for r in [header, *entries]) + "\n")
    return path


def _assistant(entry_id, parent_id, ts, content):
    return {"type": "message", "id": entry_id, "parentId": parent_id, "timestamp": ts,
            "message": {"role": "assistant", "content": content}}


def _user(entry_id, parent_id, ts, text):
    return {"type": "message", "id": entry_id, "parentId": parent_id, "timestamp": ts,
            "message": {"role": "user", "content": [{"type": "text", "text": text}]}}


def _deployment(tmp_path, with_sessions=True):
    """A configuration file laid out the way a deployment writes one, and its store."""
    seed = tmp_path / "seed"
    (seed / "src").mkdir(parents=True)
    (seed / "src" / "mod_kernel.f90").write_text("subroutine step\nend subroutine\n")
    baseline = init_baseline_repo(tmp_path / "repo", seed)
    (tmp_path / "working").mkdir()
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()

    paths = {
        "repo": str(tmp_path / "repo"),
        "ledger_root": str(tmp_path / "ledger"),
        "working_copy": str(tmp_path / "working"),
        "programs": str(write_program(tmp_path).parent),
        "strategies": str(STRATEGIES),
    }
    if with_sessions:
        paths["sessions"] = str(sessions_dir)
    config_path = tmp_path / "gateway.yaml"
    config_path.write_text(yaml.safe_dump({
        "version": 1,
        "paths": paths,
        "codes": {"tsunami": {"manifest": "tsunami/manifest.yaml"}},
        "regions": {"ch04:step": {"code": "tsunami", "phase": "porting",
                                  "spec_path": "notes/regions/ch04-step.sese.yaml",
                                  "strategy": "stdpar_managed",
                                  "baseline_strategy": "cpu_reference"}},
    }))
    store = LedgerStore(tmp_path / "ledger" / baseline / "ch04-step")
    return config_path, store, sessions_dir


def test_reading_the_sample_gives_the_session_the_model_and_every_event():
    session_id, model_id, events = session.read_session(SAMPLE)

    assert session_id == SAMPLE_SESSION
    assert model_id == "faux-1"
    assert [(e.kind, e.tool_name) for e in events] == [
        ("user", None),
        ("tool_call", "submit"),
        ("assistant_text", None),
        ("user", None),
        ("tool_call", "sese_check"),
        ("assistant_text", None),
    ]
    assert [e.text for e in events if e.kind == "user"] == [
        "submit the region and run the sese check", "continue",
    ]
    assert [e.text for e in events if e.kind == "assistant_text"] == [
        "Submitted. Now the SESE check.", "The region is verified.",
    ]
    submit, sese = [e for e in events if e.kind == "tool_call"]
    assert submit.tool_call_id == SUBMIT_CALL
    assert submit.result_details["tree"] == "abc123"
    assert sese.tool_call_id == SESE_CALL
    assert sese.result_details == {"claim_id": "c-7", "verdict": "pass", "detail": {"note": "ok"}}
    assert not sese.is_error


def test_a_branch_the_session_left_behind_is_not_read(tmp_path):
    # The third entry's parent is the first message, not the entry before
    # it: the session was rewound and answered again. Only the answer the
    # session ended on is part of what was said.
    path = _write_session(tmp_path / "branched.jsonl", [
        {"type": "model_change", "id": "a", "parentId": None,
         "timestamp": "2026-01-01T00:00:00.000Z", "provider": "faux", "modelId": "faux-1"},
        _user("b", "a", "2026-01-01T00:00:01.000Z", "port the region"),
        _assistant("c", "b", "2026-01-01T00:00:02.000Z", [{"type": "text", "text": "abandoned answer"}]),
        _assistant("d", "b", "2026-01-01T00:00:03.000Z", [{"type": "text", "text": "the answer that stayed"}]),
    ])

    _, _, events = session.read_session(path)

    assert [e.text for e in events] == ["port the region", "the answer that stayed"]


def test_a_session_id_with_no_file_prints_the_request_log_alone(tmp_path, capsys):
    config_path, store, sessions_dir = _deployment(tmp_path)
    store.append_request(RequestLogLine(
        ts="2026-01-01T00:00:00Z", session="walkthrough", model="none", endpoint="submit",
        action="submit", region="ch04:step", tree="a" * 64, config_hash=None, outcome="submitted",
    ))

    assert session.find_session_file(sessions_dir, "walkthrough") is None

    rc = main(["session", "walkthrough", "--config", str(config_path), "--region-id", "ch04:step"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "no session file for walkthrough" in out
    assert "submitted tree aaaaaaaaaaaa" in out


def test_a_deployment_that_names_no_sessions_directory_says_so(tmp_path, capsys):
    config_path, _, _ = _deployment(tmp_path, with_sessions=False)

    rc = main(["session", "any-session", "--config", str(config_path), "--region-id", "ch04:step"])

    assert rc == 1
    assert "no sessions directory" in capsys.readouterr().err


def test_a_line_with_a_tool_call_id_pairs_with_that_call_and_one_without_pairs_by_position():
    _, _, events = session.read_session(SAMPLE)
    with_ids = _sample_requests()
    # The same two lines as an older gateway would have written them,
    # before requests carried the caller's tool-call id.
    without_ids = [
        RequestLogLine(**{**line.to_dict(), "tool_call_id": None, "missing": None})
        for line in with_ids
    ]

    exact = session.join(events, with_ids)
    positional = session.join(events, without_ids)

    paired = [(row.who, row.request.action) for row in exact.rows if row.source == "both"]
    assert paired == [("submit", "submit"), ("sese_check", "sese_check")]
    assert [(row.who, row.request.action) for row in positional.rows if row.source == "both"] == paired
    assert exact.unmatched_calls == () and exact.unmatched_requests == ()


def test_a_status_call_a_local_call_and_an_unlogged_gateway_call_are_all_reported(tmp_path):
    path = _write_session(tmp_path / "mixed.jsonl", [
        _assistant("a", None, "2026-01-01T00:00:00.000Z", [
            {"type": "toolCall", "id": "t1", "name": "status", "arguments": {}},
            {"type": "toolCall", "id": "t2", "name": "bash", "arguments": {"command": "ls"}},
            {"type": "toolCall", "id": "t3", "name": "sese_check", "arguments": {}},
        ]),
    ])
    _, _, events = session.read_session(path)

    joined = session.join(events, [])

    assert [(e.tool_name, e.kind) for e in joined.unmatched_calls] == [
        ("status", "tool_call"), ("bash", "local_tool_call"), ("sese_check", "tool_call"),
    ]
    text = render.render_timeline(joined)
    assert "(local)" in text
    assert "(no request line)" in text
    assert "no request line for status" in text


def test_an_onboarding_action_is_a_gateway_call_too(tmp_path):
    # One reader reads both kinds of transcript, so the tool names it
    # knows are every action in the table, not one phase's.
    path = _write_session(tmp_path / "onboarding.jsonl", [
        _assistant("a", None, "2026-01-01T00:00:00.000Z", [
            {"type": "toolCall", "id": "t1", "name": "manifest_check", "arguments": {}},
            {"type": "toolCall", "id": "t2", "name": "harness_build", "arguments": {}},
        ]),
    ])
    _, _, events = session.read_session(path)

    joined = session.join(events, [])

    assert [(e.tool_name, e.kind) for e in joined.unmatched_calls] == [
        ("manifest_check", "tool_call"), ("harness_build", "tool_call"),
    ]


def test_a_request_line_with_no_tool_call_beside_it_is_reported():
    _, _, events = session.read_session(SAMPLE)
    extra = RequestLogLine(
        ts="2026-08-28T10:35:28Z", session=SAMPLE_SESSION, model="faux-1", endpoint="run",
        action="build_replay", region="ch04:step", tree="abc123", config_hash="cfg",
        outcome="refused", missing=({"predicateType": "sese/verified"},),
    )

    joined = session.join(events, [*_sample_requests(), extra])

    assert [line.action for line in joined.unmatched_requests] == ["build_replay"]
    assert [row.source for row in joined.rows][-1] == "request"


def test_the_timeline_is_time_ordered_across_both_logs_and_renders_to_the_golden_file():
    _, _, events = session.read_session(SAMPLE)

    joined = session.join(events, _sample_requests(), {"c-7": "pass"})

    times = [session.parse_ts(row.ts) for row in joined.rows]
    assert times == sorted(times)
    # A paired row is shown at the call's own millisecond, not at the
    # request log's whole second.
    assert [row.ts for row in joined.rows if row.source == "both"] == [
        "2026-08-28T10:35:27.279Z", "2026-08-28T10:35:27.282Z",
    ]
    assert render.render_timeline(joined) == (GOLDEN_DIR / "session-timeline.txt").read_text()


def test_a_session_whose_claims_finish_a_tree_reports_how_long_it_took(tmp_path):
    store = LedgerStore(tmp_path / "region")
    tree, frozen = "a" * 64, "b" * 64
    requests = [RequestLogLine(
        ts="2026-01-01T00:00:00Z", session="sess-1", model="m", endpoint="submit", action="submit",
        region="ch04:step", tree=tree, config_hash=None, outcome="submitted",
    )]
    for i, req in enumerate(ACCEPTANCE_REQUIREMENTS, start=1):
        sha = frozen if req.subject_kind == "frozen" else tree
        store.append_claim(_claim(
            f"c-{i:04d}", f"2026-01-01T00:00:{i:02d}Z", req.predicate_type, req.subject_kind,
            sha, "pass", "sess-1",
        ))

    summary = session.summarize(store, "sess-1", requests, [], session.join([], requests))

    assert summary.submits == 1
    assert summary.trees == (tree,)
    assert summary.claims_by_predicate == {req.predicate_type: 1 for req in ACCEPTANCE_REQUIREMENTS}
    assert summary.fail_verdicts == 0
    # The last requirement lands one second per requirement after the
    # first request, which is when the tree became acceptable.
    assert summary.time_to_acceptance == f"{len(ACCEPTANCE_REQUIREMENTS)}s"


def test_a_session_that_never_finishes_a_tree_says_it_is_not_accepted(tmp_path):
    store = LedgerStore(tmp_path / "region")
    tree, frozen = "a" * 64, "b" * 64
    requests = [RequestLogLine(
        ts="2026-01-01T00:00:00Z", session="sess-1", model="m", endpoint="run", action="sese_check",
        region="ch04:step", tree=tree, config_hash="cfg", outcome="claim", claim_id="c-0001",
    )]
    for i, req in enumerate(ACCEPTANCE_REQUIREMENTS, start=1):
        if req.predicate_type == "timing/port":
            continue
        sha = frozen if req.subject_kind == "frozen" else tree
        store.append_claim(_claim(
            f"c-{i:04d}", f"2026-01-01T00:00:{i:02d}Z", req.predicate_type, req.subject_kind,
            sha, "pass", "sess-1",
        ))
    store.append_claim(_claim(
        "c-0009", "2026-01-01T00:00:09Z", "timing/port", "tree", tree, "fail", "sess-1",
    ))

    summary = session.summarize(store, "sess-1", requests, [], session.join([], requests))

    assert summary.time_to_acceptance == "not accepted"
    assert summary.fail_verdicts == 1


def test_an_onboarding_session_is_measured_against_the_onboarding_requirements(tmp_path):
    # The same claims are a finished onboarding and not a finished port,
    # so which list a session is read against comes from its region.
    store = LedgerStore(tmp_path / "region")
    tree = "a" * 64
    requests = [RequestLogLine(
        ts="2026-01-01T00:00:00Z", session="sess-1", model="m", endpoint="submit", action="submit",
        region="tsunami:onboarding", tree=tree, config_hash=None, outcome="submitted",
    )]
    for i, req in enumerate(ONBOARDING_REQUIREMENTS, start=1):
        store.append_claim(_claim(
            f"c-{i:04d}", f"2026-01-01T00:00:{i:02d}Z", req.predicate_type, req.subject_kind,
            tree, "pass", "sess-1",
        ))

    onboarding = session.summarize(
        store, "sess-1", requests, [], session.join([], requests), phase=ONBOARDING,
    )
    porting = session.summarize(
        store, "sess-1", requests, [], session.join([], requests), phase=PORTING,
    )

    assert onboarding.time_to_acceptance == "6s"
    assert porting.time_to_acceptance == "not accepted"


def test_the_summary_for_the_sample_session_matches_the_golden_file(tmp_path):
    store = LedgerStore(tmp_path / "region")
    store.append_claim(_claim(
        "c-7", "2026-08-28T10:35:27Z", "sese/verified", "frozen", "def456", "pass", SAMPLE_SESSION,
    ))
    _, _, events = session.read_session(SAMPLE)
    requests = _sample_requests()

    joined = session.join(events, requests, session.claim_verdicts(store))
    summary = session.summarize(store, SAMPLE_SESSION, requests, events, joined)

    assert render.render_session_summary(summary) == (GOLDEN_DIR / "session-summary.txt").read_text()


def test_a_transcript_renamed_by_hand_is_still_found_by_the_id_it_declares(tmp_path):
    # The file is named after the session it holds, so the glob normally
    # finds it. A file someone renamed still says which session it is on
    # its first line.
    renamed = _write_session(tmp_path / "notes-from-tuesday.jsonl", [
        _user("a", None, "2026-01-01T00:00:01.000Z", "port the region"),
    ])

    assert session.find_session_file(tmp_path, "hand-built") == renamed
    assert session.find_session_file(tmp_path, "some-other-session") is None
    assert session.find_session_file(tmp_path / "not-here", "hand-built") is None
