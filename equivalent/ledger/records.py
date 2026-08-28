"""Claim and request-log-line records.

These are what is written to claims.jsonl and requests.jsonl.

Trust role: the on-disk shape of all evidence. A field silently dropped
or renamed here makes old ledger lines unreadable or, worse, readable
with a different meaning; `from_dict` rejecting unknown Claim fields is
what keeps a hand-edited or corrupted line from loading as if it were
evidence.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .subjects import Subject

SCHEMA_VERSION = 1

_CLAIM_FIELDS = frozenset(
    {"id", "ts", "subject", "predicateType", "predicate", "materials", "session", "version"}
)


@dataclass(frozen=True)
class Predicate:
    tool: str
    version: str
    configHash: str
    verdict: str
    detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "tool": self.tool,
            "version": self.version,
            "configHash": self.configHash,
            "verdict": self.verdict,
            "detail": self.detail,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Predicate":
        return cls(
            tool=d["tool"],
            version=d["version"],
            configHash=d["configHash"],
            verdict=d["verdict"],
            detail=d.get("detail", {}),
        )


@dataclass(frozen=True)
class Claim:
    id: str
    ts: str
    subject: tuple  # tuple[Subject, ...]
    predicateType: str
    predicate: Predicate
    materials: tuple  # tuple[Subject, ...]
    session: str
    version: int = SCHEMA_VERSION

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "ts": self.ts,
            "subject": [s.to_dict() for s in self.subject],
            "predicateType": self.predicateType,
            "predicate": self.predicate.to_dict(),
            "materials": [s.to_dict() for s in self.materials],
            "session": self.session,
            "version": self.version,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Claim":
        unknown = set(d) - _CLAIM_FIELDS
        if unknown:
            raise ValueError(f"unknown Claim field(s): {sorted(unknown)}")
        return cls(
            id=d["id"],
            ts=d["ts"],
            subject=tuple(Subject.from_dict(s) for s in d["subject"]),
            predicateType=d["predicateType"],
            predicate=Predicate.from_dict(d["predicate"]),
            materials=tuple(Subject.from_dict(s) for s in d.get("materials", [])),
            session=d["session"],
            version=d.get("version", SCHEMA_VERSION),
        )


@dataclass(frozen=True)
class RequestLogLine:
    ts: str
    session: str
    model: str
    endpoint: str
    action: str
    region: str
    tree: str | None
    config_hash: str | None
    outcome: str  # one of: claim, refused, duplicate, error, submitted
    claim_id: str | None = None
    missing: tuple | None = None  # present when outcome == "refused"
    version: int = SCHEMA_VERSION

    def to_dict(self) -> dict:
        d = {
            "ts": self.ts,
            "session": self.session,
            "model": self.model,
            "endpoint": self.endpoint,
            "action": self.action,
            "region": self.region,
            "tree": self.tree,
            "config_hash": self.config_hash,
            "outcome": self.outcome,
            "version": self.version,
        }
        if self.claim_id is not None:
            d["claim_id"] = self.claim_id
        if self.missing is not None:
            d["missing"] = list(self.missing)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "RequestLogLine":
        missing = d.get("missing")
        return cls(
            ts=d["ts"],
            session=d["session"],
            model=d["model"],
            endpoint=d["endpoint"],
            action=d["action"],
            region=d["region"],
            tree=d.get("tree"),
            config_hash=d.get("config_hash"),
            outcome=d["outcome"],
            claim_id=d.get("claim_id"),
            missing=tuple(missing) if missing is not None else None,
            version=d.get("version", SCHEMA_VERSION),
        )
