"""The strategy file: everything that depends on the strategy for porting.

Loading a wrong or stale strategy silently changes what gets compiled, what
counts as proof the code ran on the GPU, and which files a region is allowed
to touch.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from equivalent.ledger.loading import check_keys
from equivalent.ledger.subjects import Subject, glob_matches, hash_bytes

REQUIRED_FIELDS = (
    "name", "version", "allow_globs", "languages", "link_flags",
    "required_tools", "device_proof", "sanitizers", "sanitize_cases",
    "analyzer_command",
)
REQUIRED_DEVICE_PROOF_FIELDS = ("notify", "mandatory")
# Which of the visible cases the sanitizers are run over. "first" is one
# case, which is what the sanitizers cost; "all" is every visible case.
# There is no third choice: a value outside this pair would leave the
# component guessing what was meant.
SANITIZE_CASE_CHOICES = ("first", "all")
SUPPORTED_LANGUAGES = ("fortran", "c", "cxx", "cuda", "ptx")


@dataclass(frozen=True)
class Language:
    compiler: str
    flags: tuple[str, ...]

    @classmethod
    def from_dict(cls, d: dict, where: str) -> "Language":
        check_keys(d, ("compiler", "flags"), where)
        return cls(
            compiler=_text(d["compiler"], f"{where} compiler"),
            flags=_string_list(d["flags"], f"{where} flags"),
        )


@dataclass(frozen=True)
class DeviceProof:
    notify: str | None  # "acc" | "omp" | None
    mandatory: bool

    @classmethod
    def from_dict(cls, d: dict, where: str) -> "DeviceProof":
        check_keys(d, REQUIRED_DEVICE_PROOF_FIELDS, where)
        if d["notify"] is not None and not isinstance(d["notify"], str):
            raise ValueError(
                f"{where} notify is {d['notify']!r}; it names an offload runtime, or is empty"
            )
        return cls(notify=d["notify"], mandatory=bool(d["mandatory"]))


@dataclass(frozen=True)
class Strategy:
    name: str
    version: int
    allow_globs: tuple[str, ...]
    languages: dict[str, Language]
    link_flags: tuple[str, ...]
    required_tools: tuple[str, ...]
    device_proof: DeviceProof
    sanitizers: tuple[str, ...]
    sanitize_cases: str  # one of SANITIZE_CASE_CHOICES
    analyzer_command: str
    sha256: str

    def allows(self, path: str) -> bool:
        """Would a region under this strategy be allowed to submit this path.

        The pattern rule is the one everything else matches by
        (`equivalent.ledger.subjects.glob_matches`). It has to be: the
        submit that accepts an edit and the frozen-set hash that records
        which files were held still read the same allow-list, and a path
        this said yes to and one of those said no to would be an edit
        nobody could file a claim about.
        """
        return any(glob_matches(path, pattern) for pattern in self.allow_globs)

    def rejected_paths(self, paths) -> list:
        """Of the given concrete paths, the ones this strategy does not allow."""
        return [p for p in paths if not self.allows(p)]

    def as_subject(self) -> Subject:
        return Subject(kind="strategy", sha256=self.sha256)


def _text(value, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{where} is {value!r}; it must be a non-empty string")
    return value


def _string_list(value, where: str) -> tuple:
    """A list of strings, refused when it is one string.

    A single string is the mistake this catches: `allow_globs: "src/*"`
    reads as a list of its own characters, and a region whose allow-list
    is the letters of a pattern would let nothing through while looking
    like it had been written down.
    """
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{where} is {value!r}; it must be a list of strings")
    return tuple(value)


def load_strategy(path) -> Strategy:
    """Read one strategy file, refusing anything it cannot mean.

    Every failure names the file and the field, because the person
    reading the message is editing that file. A field that loaded as the
    wrong shape would decide what gets compiled or what a region may
    edit, so none of them is taken on trust.
    """
    path = Path(path)
    raw = path.read_bytes()
    where = f"strategy file {path}"
    d = yaml.safe_load(raw)
    check_keys(d, REQUIRED_FIELDS, where)

    if not isinstance(d["version"], int) or isinstance(d["version"], bool):
        raise ValueError(f"{where} version is {d['version']!r}; it must be an integer")

    languages_raw = d["languages"]
    if not isinstance(languages_raw, dict) or not languages_raw:
        raise ValueError(
            f"{where} languages is {languages_raw!r}; it must name at least one language"
        )
    unknown_languages = sorted(set(languages_raw) - set(SUPPORTED_LANGUAGES))
    if unknown_languages:
        raise ValueError(
            f"{where} names unsupported language(s) {unknown_languages}; "
            f"supported languages are {list(SUPPORTED_LANGUAGES)}"
        )
    languages = {
        lang: Language.from_dict(spec, f"{where} language '{lang}'")
        for lang, spec in languages_raw.items()
    }

    if d["sanitize_cases"] not in SANITIZE_CASE_CHOICES:
        raise ValueError(
            f"{where} has sanitize_cases {d['sanitize_cases']!r}; "
            f"it must be one of {list(SANITIZE_CASE_CHOICES)}"
        )

    required_tools = _string_list(d["required_tools"], f"{where} required_tools")
    missing_compilers = sorted({
        language.compiler for language in languages.values()
        if language.compiler not in required_tools
    })
    if missing_compilers:
        raise ValueError(
            f"{where} required_tools does not include language compiler(s) "
            f"{missing_compilers}"
        )

    return Strategy(
        name=_text(d["name"], f"{where} name"),
        version=d["version"],
        allow_globs=_string_list(d["allow_globs"], f"{where} allow_globs"),
        languages=languages,
        link_flags=_string_list(d["link_flags"], f"{where} link_flags"),
        required_tools=required_tools,
        device_proof=DeviceProof.from_dict(d["device_proof"], f"{where} device_proof"),
        sanitizers=_string_list(d["sanitizers"], f"{where} sanitizers"),
        sanitize_cases=d["sanitize_cases"],
        analyzer_command=_text(d["analyzer_command"], f"{where} analyzer_command"),
        sha256=hash_bytes(raw),
    )
