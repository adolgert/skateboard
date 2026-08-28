"""Line one agent session's transcript up against the gateway's request log.

Two records of the same run exist. The request log is written by the
gateway itself and is the trustworthy half: every line in it is a request
the gateway actually served. The session transcript is written by the
agent's own tooling and is the untrusted half: it is the agent's account
of what it was asked, what it said, and what it called.

Trust role: none over the ledger -- nothing here writes anything. What it
can get wrong is the reader's picture of a run. So it never fills a gap in
the request log from the transcript, and never prefers the transcript's
account of an answer: a tool call the transcript shows with no matching
request line is reported as exactly that, and a request line with no tool
call beside it is reported too. Verdicts shown on the timeline come from
the ledger's own claims, not from the copy of the answer the transcript
happens to hold.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from equivalent.gateway.table import ACTION_TABLE
from equivalent.ledger.acceptance import PORTING, requirements_for
from equivalent.ledger.records import RequestLogLine
from equivalent.ledger.store import LedgerStore

# The tool names that reach the gateway. The action names come from the
# gateway's own table -- an action with no component has nothing to call,
# so it is not a tool -- plus the two endpoints that are not actions.
# Nothing here is hand-listed, so a new row in the table is a new tool
# name here on the same day.
GATEWAY_TOOL_NAMES = frozenset(
    {row.name for row in ACTION_TABLE if row.component is not None} | {"submit", "status"}
)

# A status call asks the gateway what it already knows and changes
# nothing, so the gateway writes no request line for one. It is therefore
# never a candidate when request lines are matched to calls by position.
UNLOGGED_TOOL_NAMES = frozenset({"status"})


@dataclass(frozen=True)
class SessionEvent:
    """One thing that happened in the transcript, in the order it happened.

    `kind` is "user", "assistant_text", "tool_call" (a call to the
    gateway) or "local_tool_call" (a call to the agent's own file and
    shell tools, which the gateway never sees). `result_details` and
    `is_error` are filled in from the matching tool result, and stay None
    and False for a call the transcript records no result for.
    """

    ts: str
    kind: str
    text: str | None = None
    tool_call_id: str | None = None
    tool_name: str | None = None
    arguments: dict | None = None
    result_details: dict | None = None
    is_error: bool = False

    def to_dict(self) -> dict:
        return {
            "ts": self.ts,
            "kind": self.kind,
            "text": self.text,
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
            "arguments": self.arguments,
            "result_details": self.result_details,
            "is_error": self.is_error,
        }


@dataclass(frozen=True)
class TimelineRow:
    """One line of the joined timeline.

    `source` says which record the row was read from: "session" for
    something only the transcript knows (the words, a local tool call),
    "request" for a request line no tool call was found for, and "both"
    for a call and the line it produced.
    """

    ts: str
    source: str
    who: str  # "user", "assistant", or the tool's name
    text: str | None = None
    tool_call_id: str | None = None
    local: bool = False
    request: RequestLogLine | None = None
    verdict: str | None = None  # the ledger's verdict on request.claim_id

    def to_dict(self) -> dict:
        return {
            "ts": self.ts,
            "source": self.source,
            "who": self.who,
            "text": self.text,
            "tool_call_id": self.tool_call_id,
            "local": self.local,
            "request": self.request.to_dict() if self.request is not None else None,
            "verdict": self.verdict,
        }


@dataclass(frozen=True)
class JoinResult:
    rows: tuple  # tuple[TimelineRow, ...], in time order
    unmatched_calls: tuple  # tuple[SessionEvent, ...] -- tool calls with no request line
    unmatched_requests: tuple  # tuple[RequestLogLine, ...] -- request lines with no tool call


@dataclass(frozen=True)
class Summary:
    session_id: str
    submits: int
    refusals: int
    duplicates: int
    errors: int
    claims_by_predicate: dict  # predicate type -> how many claims this session filed
    fail_verdicts: int
    trees: tuple  # every tree this session's requests named, first seen first
    time_to_acceptance: str  # "1m 23s", or "not accepted"
    unmatched_calls: int
    unmatched_requests: int

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "submits": self.submits,
            "refusals": self.refusals,
            "duplicates": self.duplicates,
            "errors": self.errors,
            "claims_by_predicate": dict(self.claims_by_predicate),
            "fail_verdicts": self.fail_verdicts,
            "trees": list(self.trees),
            "time_to_acceptance": self.time_to_acceptance,
            "unmatched_calls": self.unmatched_calls,
            "unmatched_requests": self.unmatched_requests,
        }


# --- reading the transcript ---


def find_session_file(sessions_dir, session_id: str) -> Path | None:
    """The transcript file for this session id, or None if there is none.

    The file is named after the session it holds, so a glob finds it
    without opening anything. A file renamed by hand no longer matches,
    so as a second try every file's header line is read for the id it
    declares.
    """
    sessions_dir = Path(sessions_dir)
    if not sessions_dir.is_dir():
        return None
    for path in sorted(sessions_dir.glob(f"*_{session_id}.jsonl")):
        return path
    for path in sorted(sessions_dir.glob("*.jsonl")):
        if _header_session_id(path) == session_id:
            return path
    return None


def _header_session_id(path: Path) -> str | None:
    with open(path) as f:
        first = f.readline()
    if not first.strip():
        return None
    try:
        return json.loads(first).get("id")
    except json.JSONDecodeError:
        return None


def _leaf_path(entries: list[dict]) -> list[dict]:
    """The entries on the branch the session ended on, oldest first.

    Entries form a tree: each one names its parent, and a session that
    was branched or rewound leaves entries that the final conversation
    never contained. Walking back from the last entry keeps only what the
    session actually ended up holding, so an abandoned branch is not read
    as if the agent had said it.
    """
    by_id = {entry["id"]: entry for entry in entries if "id" in entry}
    path = []
    current = entries[-1] if entries else None
    while current is not None:
        path.append(current)
        parent_id = current.get("parentId")
        current = by_id.get(parent_id) if parent_id is not None else None
    path.reverse()
    return path


def _text_of(content) -> str:
    """A message's words. Content is a plain string or a list of blocks."""
    if isinstance(content, str):
        return content
    return "".join(block.get("text", "") for block in content if block.get("type") == "text")


def read_session(path) -> tuple[str | None, str | None, list[SessionEvent]]:
    """Read one transcript: its session id, the model that ran it, and its events.

    The model id is the last one the session switched to, because that is
    the model that produced the calls at the end of it.
    """
    path = Path(path)
    records = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    header = records[0] if records and records[0].get("type") == "session" else {}
    entries = [r for r in records if r.get("type") != "session"]

    model_id = None
    events: list[SessionEvent] = []
    index_of_call: dict[str, int] = {}

    for entry in _leaf_path(entries):
        ts = entry.get("timestamp")
        if entry["type"] == "model_change":
            model_id = entry.get("modelId")
            continue
        if entry["type"] != "message":
            continue
        message = entry["message"]
        role = message.get("role")

        if role == "user":
            events.append(SessionEvent(ts=ts, kind="user", text=_text_of(message.get("content", []))))
        elif role == "assistant":
            for block in message.get("content", []):
                if block.get("type") == "text":
                    events.append(SessionEvent(ts=ts, kind="assistant_text", text=block.get("text", "")))
                elif block.get("type") == "toolCall":
                    name = block.get("name")
                    index_of_call[block["id"]] = len(events)
                    events.append(SessionEvent(
                        ts=ts,
                        kind="tool_call" if name in GATEWAY_TOOL_NAMES else "local_tool_call",
                        tool_call_id=block["id"],
                        tool_name=name,
                        arguments=block.get("arguments"),
                    ))
        elif role == "toolResult":
            index = index_of_call.get(message.get("toolCallId"))
            if index is not None:
                events[index] = replace(
                    events[index],
                    result_details=message.get("details"),
                    is_error=bool(message.get("isError")),
                )

    return header.get("id"), model_id, events


# --- joining ---


def parse_ts(ts: str) -> datetime:
    """Both logs write UTC ISO 8601; only the transcript's has milliseconds."""
    return datetime.fromisoformat(ts)


def claim_verdicts(store: LedgerStore) -> dict:
    """Claim id to the verdict the ledger recorded for it."""
    return {claim.id: claim.predicate.verdict for claim in store.all_claims()}


def _line_matches_call(line: RequestLogLine, event: SessionEvent) -> bool:
    """Is this request line the one that call would have produced?

    A submit tool calls the submit endpoint; every other tool is an action
    name on the run endpoint. Nothing else lines up.
    """
    if event.tool_name == "submit":
        return line.endpoint == "submit"
    return line.endpoint == "run" and line.action == event.tool_name


def join(events, requests, verdicts: dict | None = None) -> JoinResult:
    """Pair each request line with the tool call that made it.

    A request line that carries the caller's tool-call id pairs with that
    exact call, which survives calls the agent made in parallel and the
    request log's one-second timestamps. A line without one -- an older
    line, or a caller that is not a model -- can only be placed by
    position, so it is paired with the next gateway call that is still
    free and whose name agrees. When the name disagrees, the position is
    no longer trustworthy and the line is reported unmatched rather than
    paired with a call it may not belong to.

    `verdicts` maps claim id to verdict, read from the ledger, so the
    timeline can say how a claim came out without believing the
    transcript's own copy of the answer.
    """
    verdicts = verdicts or {}
    events = list(events)
    requests = list(requests)

    paired: dict[int, int] = {}  # index in requests -> index in events
    calls_by_id = {
        event.tool_call_id: i for i, event in enumerate(events) if event.kind == "tool_call"
    }

    for i, line in enumerate(requests):
        if line.tool_call_id is not None:
            index = calls_by_id.get(line.tool_call_id)
            if index is not None and index not in paired.values():
                paired[i] = index

    cursor = 0
    for i, line in enumerate(requests):
        if line.tool_call_id is not None:
            continue
        while cursor < len(events) and (
            events[cursor].kind != "tool_call"
            or cursor in paired.values()
            or events[cursor].tool_name in UNLOGGED_TOOL_NAMES
        ):
            cursor += 1
        if cursor < len(events) and _line_matches_call(line, events[cursor]):
            paired[i] = cursor
            cursor += 1

    unmatched_requests = [line for i, line in enumerate(requests) if i not in paired]
    event_to_line = {event_index: line_index for line_index, event_index in paired.items()}
    rows = []
    unmatched_calls = []
    for i, event in enumerate(events):
        line = requests[event_to_line[i]] if i in event_to_line else None
        if event.kind == "user":
            rows.append(TimelineRow(ts=event.ts, source="session", who="user", text=event.text))
            continue
        if event.kind == "assistant_text":
            rows.append(TimelineRow(ts=event.ts, source="session", who="assistant", text=event.text))
            continue
        if line is None:
            unmatched_calls.append(event)
        rows.append(TimelineRow(
            ts=event.ts,
            source="both" if line is not None else "session",
            who=event.tool_name,
            tool_call_id=event.tool_call_id,
            local=event.kind == "local_tool_call",
            request=line,
            verdict=verdicts.get(line.claim_id) if line is not None else None,
        ))

    for line in unmatched_requests:
        rows.append(TimelineRow(
            ts=line.ts,
            source="request",
            who="submit" if line.endpoint == "submit" else line.action,
            request=line,
            verdict=verdicts.get(line.claim_id),
        ))

    # A stable sort, so calls that share a timestamp keep the order the
    # two logs wrote them in rather than being shuffled by the clock.
    rows.sort(key=lambda row: parse_ts(row.ts))
    return JoinResult(
        rows=tuple(rows),
        unmatched_calls=tuple(unmatched_calls),
        unmatched_requests=tuple(unmatched_requests),
    )


# --- summarising ---


def _elapsed(start: str, end: str) -> str:
    seconds = int((parse_ts(end) - parse_ts(start)).total_seconds())
    minutes, seconds = divmod(seconds, 60)
    return f"{minutes}m {seconds:02d}s" if minutes else f"{seconds}s"


def _accepted_after(claims, requirements) -> bool:
    """Would these claims, and no others, finish some tree?

    Every requirement is read from the phase's own list rather than named
    here, so a requirement added there counts here without an edit. A
    requirement on the frozen files is met by any frozen value that
    passes, because the transcript cannot say which frozen value was
    current at the time.
    """
    latest = {}
    for claim in claims:
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


def time_to_acceptance(requests, claims, phase: str = PORTING) -> str:
    """How long from the session's first request to the claim that finished a tree.

    Replays the session's claims oldest first and stops at the one that
    leaves every requirement of the region's phase met on a single tree.
    An onboarding session and a porting session are finished by different
    lists, so which list is used comes from the region the session ran
    against; a caller that knows only a ledger directory reads it as a
    port, which is what the rest of this tool does with one.

    A session whose claims never add up to that gets "not accepted",
    which is also the honest answer for a session that finished a port
    someone else had already half-done: what is measured is this
    session's own claims.
    """
    if not requests:
        return "not accepted"
    requirements = requirements_for(phase)
    ordered = sorted(claims, key=lambda c: c.ts)
    for i in range(len(ordered)):
        if _accepted_after(ordered[: i + 1], requirements):
            return _elapsed(requests[0].ts, ordered[i].ts)
    return "not accepted"


def summarize(store: LedgerStore, session_id: str, requests, events, joined: JoinResult,
              phase: str = PORTING) -> Summary:
    """Count what this session did, reading the ledger for anything about claims.

    `requests` are already this session's lines; the claims are picked out
    of the ledger by the same session id, because a request line names a
    claim id but never a predicate type, and one request can file several
    claims. `phase` is the region's, and decides which list of
    requirements the session is judged to have finished.
    """
    outcomes = [line.outcome for line in requests]
    claims = [claim for claim in store.all_claims() if claim.session == session_id]

    claims_by_predicate: dict[str, int] = {}
    for claim in claims:
        claims_by_predicate[claim.predicateType] = claims_by_predicate.get(claim.predicateType, 0) + 1

    trees = []
    for line in requests:
        if line.tree is not None and line.tree not in trees:
            trees.append(line.tree)

    return Summary(
        session_id=session_id,
        submits=sum(1 for line in requests if line.endpoint == "submit"),
        refusals=outcomes.count("refused"),
        duplicates=outcomes.count("duplicate"),
        errors=outcomes.count("error"),
        claims_by_predicate=claims_by_predicate,
        fail_verdicts=sum(1 for claim in claims if claim.predicate.verdict == "fail"),
        trees=tuple(trees),
        time_to_acceptance=time_to_acceptance(requests, claims, phase),
        unmatched_calls=len(joined.unmatched_calls),
        unmatched_requests=len(joined.unmatched_requests),
    )
