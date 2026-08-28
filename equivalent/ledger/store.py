"""The ledger store: append-only claims and requests, content-addressed artifacts.

This module assumes it is the only writer for a given region directory
and serializes its own writes with an in-process lock; it does not defend
against a second OS process writing the same files concurrently.
"""
from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

from .records import Claim, RequestLogLine
from .subjects import Subject

CLAIM_ID_RE = re.compile(r"^c-(\d+)$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class LedgerStore:
    def __init__(self, region_dir):
        self.region_dir = Path(region_dir)
        self.region_dir.mkdir(parents=True, exist_ok=True)
        (self.region_dir / "artifacts").mkdir(exist_ok=True)
        self._lock = threading.Lock()

    @property
    def claims_path(self) -> Path:
        return self.region_dir / "claims.jsonl"

    @property
    def requests_path(self) -> Path:
        return self.region_dir / "requests.jsonl"

    def _read_jsonl(self, path: Path) -> list[dict]:
        if not path.exists():
            return []
        text = path.read_text()
        complete, _, partial = text.rpartition("\n")
        out = [json.loads(line) for line in complete.splitlines() if line.strip()]
        if partial.strip():
            # Every append writes "...json...\n" in one call, so a final
            # line with no newline is a write another process (the
            # gateway) hasn't finished flushing. A reader (the CLI) skips
            # it if it doesn't parse rather than crashing on it.
            try:
                out.append(json.loads(partial))
            except json.JSONDecodeError:
                pass
        return out

    def _read_claims(self) -> list[Claim]:
        return [Claim.from_dict(d) for d in self._read_jsonl(self.claims_path)]

    def _read_requests(self) -> list[RequestLogLine]:
        return [RequestLogLine.from_dict(d) for d in self._read_jsonl(self.requests_path)]

    # --- writes ---

    def next_claim_id(self) -> str:
        n = 0
        for c in self._read_claims():
            m = CLAIM_ID_RE.match(c.id)
            if m:
                n = max(n, int(m.group(1)))
        return f"c-{n + 1:04d}"

    def append_claim(self, claim: Claim) -> Claim:
        """Append a fully-formed Claim. Never rewrites or removes a line."""
        with self._lock:
            with open(self.claims_path, "a") as f:
                f.write(json.dumps(claim.to_dict(), sort_keys=True) + "\n")
        return claim

    def record_claim(self, subject, predicateType: str, predicate, materials, session: str) -> Claim:
        """Build a Claim with an auto-assigned id and timestamp, and append it."""
        with self._lock:
            claim = Claim(
                id=self.next_claim_id(),
                ts=_now(),
                subject=tuple(subject),
                predicateType=predicateType,
                predicate=predicate,
                materials=tuple(materials),
                session=session,
            )
            with open(self.claims_path, "a") as f:
                f.write(json.dumps(claim.to_dict(), sort_keys=True) + "\n")
        return claim

    def append_request(self, line: RequestLogLine) -> RequestLogLine:
        with self._lock:
            with open(self.requests_path, "a") as f:
                f.write(json.dumps(line.to_dict(), sort_keys=True) + "\n")
        return line

    def put_artifact(self, sha256: str, data: bytes) -> Path:
        """Store a blob under its content hash. Writing the same hash twice is a no-op."""
        path = self.region_dir / "artifacts" / sha256
        if path.exists():
            return path
        with self._lock:
            if not path.exists():
                tmp = path.with_suffix(".tmp")
                tmp.write_bytes(data)
                tmp.replace(path)
        return path

    # --- queries ---

    def all_claims(self) -> list[Claim]:
        return self._read_claims()

    def all_requests(self) -> list[RequestLogLine]:
        return self._read_requests()

    def get_claim(self, claim_id: str) -> Claim | None:
        return next((c for c in self._read_claims() if c.id == claim_id), None)

    def claims_for(self, subject: Subject) -> list[Claim]:
        return [c for c in self._read_claims() if subject in c.subject]

    def latest(self, predicate_type: str, subject: Subject, config_hash: str | None = None):
        matches = [
            c for c in self._read_claims()
            if c.predicateType == predicate_type
            and subject in c.subject
            and (config_hash is None or c.predicate.configHash == config_hash)
        ]
        # Timestamps have one-second resolution, so ties happen; sorted()
        # is stable, so [-1] is the last-appended claim among the newest,
        # matching find_duplicate's file-order rule.
        return sorted(matches, key=lambda c: c.ts)[-1] if matches else None

    def exists_pass(self, predicate_type: str, subject: Subject) -> bool:
        return any(
            c.predicateType == predicate_type and subject in c.subject and c.predicate.verdict == "pass"
            for c in self._read_claims()
        )

    def find_duplicate(self, predicate_type: str, tree: Subject, config_hash: str):
        """Most recent claim for this (predicate type, tree, config), if any.

        A Claim records a predicateType, never an action name, so a caller
        asking "did this action already run?" passes the predicate type
        the action emits.
        """
        matches = [
            c for c in self._read_claims()
            if c.predicateType == predicate_type
            and tree in c.subject
            and c.predicate.configHash == config_hash
        ]
        return matches[-1] if matches else None

    def list_trees(self) -> list[str]:
        seen = []
        for c in self._read_claims():
            for s in c.subject:
                if s.kind == "tree" and s.sha256 not in seen:
                    seen.append(s.sha256)
        return seen
