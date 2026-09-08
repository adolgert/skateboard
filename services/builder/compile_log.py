"""Pure parsing and validation of compiler invocation evidence."""
from __future__ import annotations

import fnmatch
import json
import os

HARNESS = "/opt/harness"

LANGUAGE_SPECS = {
    "fortran": {
        "compiler_env": "FC", "flags_env": "FFLAGS",
        "extensions": (".f", ".for", ".f90", ".f95", ".f03", ".f08"),
    },
    "c": {
        "compiler_env": "CC", "flags_env": "CFLAGS",
        "extensions": (".c",),
    },
    "cxx": {
        "compiler_env": "CXX", "flags_env": "CXXFLAGS",
        "extensions": (".cc", ".cpp", ".cxx", ".c++"),
    },
    "cuda": {
        "compiler_env": "NVCC", "flags_env": "NVCCFLAGS",
        "extensions": (".cu",),
    },
    "ptx": {
        "compiler_env": "PTXAS", "flags_env": "PTXASFLAGS",
        "extensions": (".ptx",),
    },
}
SUPPORTED_LANGUAGES = tuple(LANGUAGE_SPECS)
MODULE_FLAG = {"nvfortran": "-module", "gfortran": "-J"}
LINK_INPUT_EXTENSIONS = (".o", ".obj", ".lo", ".a", ".so", ".dylib", ".cubin")
OPTIONS_WITH_VALUE = {
    "-o", "-I", "-L", "-J", "-module", "-include", "-isystem",
    "-MF", "-MT", "-MQ", "-D", "-U", "-Xcompiler", "-Xlinker",
}
RESPONSE_OPTIONS = {"--options-file", "-optf"}
SOURCE_FORCING_OPTIONS = {"-x"}


def module_flag(compiler: str) -> str | None:
    """How this compiler wants the module output directory named, or None."""
    return MODULE_FLAG.get(os.path.basename(compiler))


def normalize_toolchains(toolchains=None, *, compiler=None, flags=()) -> dict:
    """Canonical language -> compiler/flags mapping, including the legacy wire."""
    if toolchains is None:
        if not isinstance(compiler, str) or not compiler.strip():
            raise ValueError("a legacy build must name its Fortran compiler")
        if not isinstance(flags, (list, tuple)) or not all(isinstance(f, str) for f in flags):
            raise ValueError("legacy Fortran flags must be a list of strings")
        return {"fortran": {"compiler": compiler, "flags": list(flags)}}
    if compiler is not None or list(flags):
        raise ValueError("compiler/flags and toolchains cannot both describe one build")
    if not isinstance(toolchains, dict) or not toolchains:
        raise ValueError("toolchains must name at least one supported language")

    unknown = sorted(set(toolchains) - set(SUPPORTED_LANGUAGES))
    if unknown:
        raise ValueError(
            f"unsupported build language(s): {', '.join(unknown)}; "
            f"supported languages are {', '.join(SUPPORTED_LANGUAGES)}"
        )
    normalized = {}
    for language, raw in toolchains.items():
        if not isinstance(raw, dict) or set(raw) != {"compiler", "flags"}:
            raise ValueError(
                f"toolchain '{language}' must contain exactly compiler and flags"
            )
        named = raw["compiler"]
        language_flags = raw["flags"]
        if not isinstance(named, str) or not named.strip():
            raise ValueError(f"toolchain '{language}' must name a compiler")
        if (
            not isinstance(language_flags, (list, tuple))
            or not all(isinstance(flag, str) for flag in language_flags)
        ):
            raise ValueError(f"toolchain '{language}' flags must be a list of strings")
        normalized[language] = {"compiler": named, "flags": list(language_flags)}
    return normalized


def compiler_environment(language: str) -> tuple[str, str]:
    spec = LANGUAGE_SPECS[language]
    return spec["compiler_env"], spec["flags_env"]


def _normalized(path: str) -> str:
    path = path.replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    return path


def _matches(path: str, pattern: str) -> bool:
    lowered = _normalized(path).lower()
    pattern = pattern.lower()
    if pattern.startswith("**/"):
        rest = pattern[3:]
        return fnmatch.fnmatchcase(lowered, rest) or fnmatch.fnmatchcase(lowered, f"*/{rest}")
    return fnmatch.fnmatchcase(lowered, pattern)


def is_tree_source(path: str, source_patterns) -> bool:
    return any(_matches(path, pattern) for pattern in source_patterns)


def source_language(argument: str) -> str | None:
    # Unix toolchains conventionally reserve uppercase .C for C++ while
    # lowercase .c is C; lowercasing before this check would misattribute it.
    if argument.endswith(".C"):
        return "cxx"
    lowered = argument.lower()
    for language, spec in LANGUAGE_SPECS.items():
        if lowered.endswith(spec["extensions"]):
            return language
    return None


def _under(path: str, directory: str) -> bool:
    return path == directory or path.startswith(directory + os.sep)


def _display(path: str, tree_root: str) -> str:
    if _under(path, tree_root):
        return os.path.relpath(path, tree_root)
    return path


def _entry_language(entry: dict, inputs: list[tuple[str, str]], toolchains: dict) -> tuple[str | None, list[str]]:
    problems = []
    explicit = entry.get("language")
    candidates = entry.get("languages")
    if explicit is not None:
        if explicit not in toolchains:
            problems.append(f"compiler evidence names undeclared language {explicit!r}")
            return None, problems
        return explicit, problems
    if candidates is not None:
        if not isinstance(candidates, list) or not candidates:
            problems.append("compiler evidence has no usable language candidates")
            return None, problems
        candidates = [language for language in candidates if language in toolchains]
    elif len(toolchains) == 1:
        candidates = list(toolchains)
    else:
        candidates = []

    source_languages = {language for _, language in inputs}
    inferred = source_languages & set(candidates)
    if len(inferred) == 1:
        return inferred.pop(), problems
    if len(candidates) == 1:
        return candidates[0], problems
    problems.append("compiler invocation is ambiguous between declared languages")
    return None, problems


def compile_records(
    log_text: str, tree_dir, flags=(), source_patterns=(), *, harness_dir=HARNESS,
    toolchains=None,
) -> list[dict]:
    """Parse compiler entries and attribute every source and required flag.

    A response file is refused because its arguments cannot be audited from the
    recorded argv. A file matched by the manifest's source patterns must use a
    suffix owned by one of the five supported languages.
    """
    tree_root = os.path.realpath(str(tree_dir))
    harness_root = os.path.realpath(str(harness_dir))
    patterns = list(source_patterns)
    configs = (
        normalize_toolchains(toolchains)
        if toolchains is not None
        else {"fortran": {"compiler": None, "flags": list(flags)}}
    )

    records = []
    for number, line in enumerate(log_text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
            argv = [str(argument) for argument in entry["argv"]]
            cwd = str(entry["cwd"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"compiler log line {number} is not a compiler record: {line[:200]}") from exc

        problems = []
        discovered = []
        link_inputs = []
        skip_value = False
        configured_tokens = {
            token for specification in configs.values()
            for token in specification["flags"]
        }
        for index, argument in enumerate(argv):
            if "@" in argument:
                problems.append(f"response file argument {argument!r} hides compiler arguments")
            if skip_value:
                skip_value = False
                continue
            if argument in RESPONSE_OPTIONS or any(
                argument.startswith(option + "=") for option in RESPONSE_OPTIONS
            ):
                problems.append(f"response-file option {argument!r} hides compiler arguments")
                skip_value = argument in RESPONSE_OPTIONS
                continue
            if argument in SOURCE_FORCING_OPTIONS:
                problems.append(
                    f"source-forcing option {argument!r} prevents suffix attribution"
                )
                skip_value = True
                continue
            if argument in OPTIONS_WITH_VALUE:
                skip_value = True
                continue
            language = source_language(argument)
            real = os.path.realpath(
                argument if os.path.isabs(argument) else os.path.join(cwd, argument)
            )
            shown = _display(real, tree_root)
            if language is not None:
                if not os.path.isfile(real):
                    problems.append(f"source input {argument!r} was not a regular file after the build")
                discovered.append((shown, language, real))
                continue
            suffix = os.path.splitext(argument)[1]
            if suffix.lower() in LINK_INPUT_EXTENSIONS:
                link_inputs.append(shown)
                continue
            if argument == "-" or argument.startswith(("/dev/", "/proc/")):
                problems.append(f"source input {argument!r} cannot be attributed to a tree file")
                continue
            if argument in configured_tokens or argument.startswith("-") or argument == "--":
                continue
            if suffix or os.path.isfile(real) or any(_matches(shown, pattern) for pattern in patterns):
                problems.append(
                    f"compiler input {shown!r} has unsupported source suffix "
                    f"{suffix or '<none>'!r}"
                )

        language, language_problems = _entry_language(
            entry, [(shown, kind) for shown, kind, _ in discovered], configs,
        )
        problems.extend(language_problems)
        inputs = [shown for shown, _, _ in discovered]
        outside = []
        for shown, source_kind, real in discovered:
            if language is not None and source_kind != language:
                problems.append(
                    f"{language} compiler invocation received {source_kind} source {shown!r}"
                )
            if _under(real, harness_root):
                continue
            if not _under(real, tree_root) or not any(_matches(shown, p) for p in patterns):
                outside.append(shown)

        required_flags = configs.get(language, {}).get("flags", [])
        option_argv = argv[:argv.index("--")] if "--" in argv else argv
        if not discovered and not link_inputs:
            problems.append("compiler invocation has no attributable source or link input")
        records.append({
            "argv": argv,
            "cwd": _display(os.path.realpath(cwd), tree_root),
            "language": language,
            "compiler": configs.get(language, {}).get("compiler"),
            "inputs": inputs,
            "link_inputs": link_inputs,
            "kind": "compile" if inputs else "link",
            "output": argv[argv.index("-o") + 1] if "-o" in argv[:-1] else None,
            "required_flags": list(required_flags),
            "has_flags": all(flag in option_argv for flag in required_flags),
            "outside": outside,
            "audit_errors": problems,
        })
    return records


def _compiles(records) -> list[dict]:
    return [record for record in records if record["inputs"]]


def flags_reached_every_compile(records) -> bool:
    compiles = _compiles(records)
    return bool(compiles) and all(record["has_flags"] for record in compiles)


def declared_languages_compiled(records, toolchains) -> bool:
    compiled = {
        record.get("language") for record in _compiles(records)
        if not record.get("audit_errors")
    }
    return compiled == set(toolchains)


def languages_compiled(records) -> list[str]:
    return sorted({
        record.get("language") for record in _compiles(records)
        if record.get("language") is not None
    })


def compilation_audit_problems(records) -> list[str]:
    return [
        problem
        for record in records
        for problem in record.get("audit_errors", ())
    ]


def compiles_without_flags(records) -> list[list]:
    return [record["argv"] for record in _compiles(records) if not record["has_flags"]]


def compiled_only_tree_source(records) -> bool:
    return not any(record["outside"] for record in records)


def files_outside_tree(records) -> list[str]:
    seen = []
    for record in records:
        for path in record["outside"]:
            if path not in seen:
                seen.append(path)
    return seen
