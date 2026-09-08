"""Build a submitted tree and record protected compiler evidence."""
import json
import os
import shutil
from dataclasses import dataclass, field

from . import compile_log
from .contract import BuildResponse
from .stage_config import HARNESS
from .workspace import ExecutionFailed

LOG_NAME = "fc.jsonl"
BUILD_TIMEOUT_S = 1800

def _read_log(path: str) -> str:
    """The shim's log, or nothing at all if the Makefile never called it."""
    if not os.path.exists(path):
        return ""
    with open(path) as log:
        return log.read()


def _accel_lines(text: str) -> str:
    """The compiler's own account of what it offloaded, for a reader of the claim."""
    lines = [
        line for line in text.splitlines()
        if "Generating" in line or "Loop" in line or "GPU" in line
    ]
    return "\n".join(lines[:40])


@dataclass
class Made:
    """One `make` invocation and what the compiler was observed to do in it."""

    command: list
    returncode: int
    output: str
    compiles: list = field(default_factory=list)
    audit: dict = field(default_factory=dict)
    # Evidence that was required and was not there. A build whose account
    # of itself is missing is a failed build, not a quiet success.
    problem: str | None = None


def _observed_compiler_log(evidence, compiler, cwd, *, toolchains=None):
    """The protected observer's account of the compile, in the shim's own format.

    Reading execve records rather than the shim's log removes the
    writable log file as a trust root: what is reported is what the
    kernel saw the configured compiler run with.
    """
    if not evidence or evidence.get("ok") is not True:
        return "", {}, "protected compiler execution evidence was unavailable"
    configured = compile_log.normalize_toolchains(
        toolchains, compiler=compiler, flags=(),
    )
    by_path = {}
    for language, specification in configured.items():
        compiler_path = shutil.which(specification["compiler"])
        if compiler_path is None:
            return "", {}, (
                f"configured {language} compiler {specification['compiler']!r} is not installed"
            )
        by_path.setdefault(os.path.realpath(compiler_path), []).append(language)

    # A compiler driver may invoke another declared tool internally: nvcc, for
    # example, launches ptxas on generated PTX. Only configured tools entered
    # from make/the recipe are build invocations; configured descendants are
    # implementation details of that entry compiler.
    observed = []
    active_path = {}
    parent = {}
    for entry in evidence.get("executions", []):
        if not isinstance(entry, dict):
            continue
        pid = entry.get("pid")
        if pid is not None:
            parent[pid] = entry.get("ppid")
        real = os.path.realpath(entry.get("path", ""))
        languages = by_path.get(real)
        if languages and entry.get("audit_error"):
            named = ", ".join(languages)
            return "", {}, (
                f"protected execution of configured compiler for {named} could not be "
                f"audited: {entry['audit_error']}"
            )
        if not entry.get("argv"):
            continue
        nested = False
        ancestor = pid
        while ancestor is not None:
            if active_path.get(ancestor) in by_path:
                nested = True
                break
            ancestor = parent.get(ancestor)
        if languages and not nested:
            observed.append((entry, languages))
        if pid is not None:
            active_path[pid] = real

    if not observed:
        named = ", ".join(
            f"{language}={specification['compiler']}"
            for language, specification in configured.items()
        )
        return "", {}, (
            f"protected exec tracing observed no invocation of a configured compiler ({named})"
        )

    initial = evidence.get("initial_cwd")
    def host_cwd(entry):
        actual = entry.get("cwd")
        if not actual or not initial:
            return cwd
        initial_real = os.path.normpath(initial)
        actual_real = os.path.normpath(actual)
        if actual_real == initial_real:
            return cwd
        if actual_real.startswith(initial_real + os.sep):
            return os.path.join(cwd, os.path.relpath(actual_real, initial_real))
        return actual_real

    rows = []
    language_counts = {}
    for entry, languages in observed:
        row = {"argv": entry["argv"][1:], "cwd": host_cwd(entry)}
        if len(languages) == 1:
            row["language"] = languages[0]
            language_counts[languages[0]] = language_counts.get(languages[0], 0) + 1
        else:
            row["languages"] = languages
        rows.append(row)
    log = "\n".join(json.dumps(row) for row in rows)
    return log, {
        "protected": True,
        "collector": "strace/process+cwd",
        "observed_executions": len(evidence.get("executions", [])),
        "compiler_invocations": len(observed),
        "language_invocations": language_counts,
    }, None


def _make(workspace, *, cwd, makefile, targets, env, timeout, compiler, flags,
          source_patterns, harness_dir, log_path, what, toolchains=None) -> Made:
    """Run a code's own makefile, and read back what the compiler was asked to do.

    The one place a submitted build is invoked. The tree's build and each
    mutant's build come through here together, so a mutant is built,
    watched and read the same way the tree was -- a mutant built some
    other way would say nothing about the build a port faces.
    """
    command = ["make", "-f", makefile, *targets]
    job = workspace.execute(
        command, cwd=cwd, env=env, timeout=timeout, mode="build", what=what,
    )
    if workspace.records_executions:
        log, audit, problem = _observed_compiler_log(
            job.evidence, compiler, cwd, toolchains=toolchains,
        )
    else:
        log, problem = _read_log(log_path), None
        audit = {"protected": False, "collector": "explicit-local-test-runner/fc-shim"}
    made = Made(command, job.returncode, job.stdout + job.stderr, audit=audit, problem=problem)
    if problem is None:
        try:
            made.compiles = compile_log.compile_records(
                log, cwd, flags, source_patterns, harness_dir=harness_dir,
                toolchains=toolchains,
            )
        except ValueError as exc:
            made.problem = str(exc)
    return made


def build(
    workspace, tree, makefile, targets, compiler=None, flags=(), link_flags=(),
    source_patterns=(), *, toolchains=None, harness_dir=HARNESS,
    timeout=BUILD_TIMEOUT_S,
) -> BuildResponse:
    """Build the submitted tree with its own makefile, and say what that did.

    `targets` is [{"role", "target", "executable"}] straight from the
    code's manifest: `make` is asked for each `target`, and each
    `executable` must exist in the tree root afterwards.

    The strategy's flags are put in the environment rather than on make's
    command line. A command-line assignment would override a Makefile
    that sets FFLAGS itself, which sounds safer and is worse: the flags
    would appear to have been used no matter what the Makefile does, and
    the shim log would have nothing to prove. In the environment, a
    Makefile that ignores FFLAGS wins -- and is then visible.
    """
    try:
        configured = compile_log.normalize_toolchains(
            toolchains, compiler=compiler, flags=flags,
        )
    except ValueError as exc:
        return BuildResponse.failure(str(exc))
    workspace.reset()
    try:
        tree_dir = workspace.write_tree(tree)
    except ValueError as exc:
        return BuildResponse.failure(str(exc))
    log_path = workspace.path(LOG_NAME)

    makefile_path = workspace.in_tree(makefile)
    if makefile_path is None or not os.path.isfile(makefile_path):
        return BuildResponse.failure(
            f"the makefile '{makefile}' is not a regular file inside the tree"
        )
    for target in targets:
        if workspace.in_tree(target["executable"]) is None:
            return BuildResponse.failure(
                f"target executable '{target['executable']}' leaves the tree"
            )
        for artifact in target.get("runtime_artifacts", []):
            if workspace.in_tree(artifact["path"]) is None:
                return BuildResponse.failure(
                    f"runtime artifact '{artifact['path']}' leaves the tree"
                )

    env = workspace.build_env(
        compiler, flags, link_flags, log_path, harness_dir, toolchains=toolchains,
    )
    workspace.prepare_job_files(include_tree=True)
    try:
        made = _make(
            workspace, cwd=tree_dir, makefile=makefile,
            targets=[t["target"] for t in targets], env=env, timeout=timeout,
            compiler=compiler, flags=flags, source_patterns=source_patterns,
            harness_dir=harness_dir, log_path=log_path, what="the build",
            toolchains=toolchains,
        )
    except ExecutionFailed as exc:
        return BuildResponse.failure(str(exc))
    built = {}
    for target in targets:
        path = workspace.in_tree(target["executable"])
        runtime_artifacts = []
        for artifact in target.get("runtime_artifacts", []):
            artifact_path = workspace.in_tree(artifact["path"])
            runtime_artifacts.append({
                **artifact,
                "built": bool(
                    artifact_path and os.path.isfile(artifact_path)
                    and not os.path.islink(artifact_path)
                ),
            })
        target_result = {
            "executable": target["executable"],
            "built": bool(
                path and os.path.isfile(path) and not os.path.islink(path)
                and os.access(path, os.X_OK)
            ),
        }
        if runtime_artifacts:
            target_result["runtime_artifacts"] = runtime_artifacts
        built[target["role"]] = target_result
    complete_languages = compile_log.declared_languages_compiled(
        made.compiles, configured,
    )
    audit_problems = compile_log.compilation_audit_problems(made.compiles)
    result = BuildResponse(
        command=made.command,
        targets=built,
        compiles=made.compiles,
        compiler_audit=made.audit,
        flags=list(flags),
        link_flags=list(link_flags),
        toolchains=configured,
        languages_compiled=compile_log.languages_compiled(made.compiles),
        flags_reached_every_compile=(
            compile_log.flags_reached_every_compile(made.compiles)
            and complete_languages and not audit_problems
        ),
        compiled_only_tree_source=compile_log.compiled_only_tree_source(made.compiles),
        minfo_excerpt=_accel_lines(made.output),
    )

    if made.problem is not None:
        result.log_tail = made.problem
        return result
    if made.returncode != 0:
        result.log_tail = made.output[-4000:]
        return result

    missing = sorted(
        [
            role for role, target in built.items() if not target["built"]
        ] + [
            f"{role}:{artifact['path']}"
            for role, target in built.items()
            for artifact in target.get("runtime_artifacts", [])
            if not artifact["built"]
        ]
    )
    if missing:
        # make said it succeeded and the executable is not there: almost
        # always a manifest naming a different file than the rule writes.
        named = ", ".join(missing)
        result.missing_targets = missing
        result.log_tail = (
            f"make succeeded but left no executable for: {named}\n{made.output[-3000:]}"
        )
        return result
    if audit_problems:
        result.log_tail = "compiler evidence could not be audited: " + "; ".join(audit_problems)
        return result
    if not complete_languages:
        missing_languages = sorted(set(configured) - set(result.languages_compiled))
        result.log_tail = (
            "the build did not compile every declared language: "
            + ", ".join(missing_languages)
        )
        return result

    identities = workspace.write_artifacts(targets)
    workspace.freeze_tree(identities)
    for target in built.values():
        target.update(identities[target["executable"]])
        if target.get("runtime_artifacts"):
            target["runtime_artifacts"] = [
                {
                    "path": artifact["path"], "kind": artifact["kind"],
                    "sha256": identities[artifact["path"]]["sha256"],
                    "size": identities[artifact["path"]]["size"],
                }
                for artifact in target["runtime_artifacts"]
            ]
    record = workspace.artifact_identities()
    result.ok = True
    result.log_tail = made.output[-2000:]
    result.executor_identity = record.executor_identity
    result.image_id = record.image_id
    return result
