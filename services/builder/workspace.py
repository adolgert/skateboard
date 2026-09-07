"""Where one attempt's files live, who may touch them, and how a command runs.

Trust role: this is the builder's memory of one attempt. Every path a
stage names comes from here, so no stage can reach another attempt's
files or write outside the directory it was given, and every stage that
measures an executable asks the same question of it: are these still the
bytes the build produced. The identity of a built executable is written
once, by the supervisor, and verified against the bytes on disk before
and after every measurement -- a claim about a run is worth exactly what
the identity of the thing that ran is worth.

How a command runs is injected rather than decided here. Production runs
every submitted command in a fresh container, hands the attempt's files
to the unprivileged job uid, freezes the tree once it is built, and
requires protected evidence of what executed. Tests run commands in this
process with none of that and say so in what they write down, so a
deployment cannot end up on the test path by accident.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Callable

from . import contract, executor

# One workspace per attempt, rebuilt from scratch by each build.
WORK_ROOT = "/work"

# The fixed unprivileged uid a submitted job runs as. It owns an attempt's
# scratch and nothing else; the built tree is taken back off it.
JOB_UID = 65532


class ExecutionFailed(Exception):
    """A command that never reached a result, in the words a stage reports.

    Every way a command can fail to produce an exit status -- a timeout,
    a boundary that could not be established, a program that is not
    there, evidence that could not be read -- arrives at a stage as this
    one exception, so no stage has to guess which subset of the
    executor's failures it should be catching.
    """

    def __init__(self, message, *, timed_out=False, unavailable=False):
        super().__init__(message)
        self.timed_out = timed_out
        self.unavailable = unavailable


class DisposableJobs:
    """Production: every submitted command runs in a fresh, isolated container.

    The supervisor owns the attempt's files, gives them to the job uid
    for the length of a job, freezes the tree once it is built, and reads
    the protected observer's account of what executed. There is
    deliberately no fallback: a builder that cannot establish the
    boundary fails closed rather than running submitted code beside the
    service.
    """

    owns_files = True
    audited = True

    def __init__(self, work_root=WORK_ROOT):
        self.work_root = os.path.abspath(str(work_root))
        self._jobs = None

    def jobs(self) -> executor.DockerJobExecutor:
        if self.work_root != os.path.abspath(WORK_ROOT):
            raise executor.IsolationUnavailable(
                "a non-production work root requires an explicitly injected test job runner"
            )
        if self._jobs is None:
            self._jobs = executor.DockerJobExecutor(work_root=self.work_root)
        return self._jobs

    def run(self, cmd, *, cwd, env, timeout, mode="plain", gpu=False):
        return self.jobs().run(
            cmd, cwd=cwd, env=env, timeout=timeout, gpu=gpu,
            profile_gpu=mode == "profiled", audit_exec=mode == "audited",
        )

    def status(self) -> dict:
        try:
            return self.jobs().probe()
        except Exception as exc:
            return {"ok": False, "backend": "docker", "error": str(exc)}

    def identity(self) -> dict:
        status = self.status()
        return {
            "executor_identity": status.get("executor_identity"),
            "image_id": status.get("image_id"),
        }

    # A mutation worker is handed this policy in its job payload, and a
    # worker that was started rather than forked rebuilds its own client
    # for the container daemon.
    def __getstate__(self):
        return {"work_root": self.work_root}

    def __setstate__(self, state):
        self.work_root = state["work_root"]
        self._jobs = None


@dataclass(frozen=True)
class InProcessJobs:
    """An explicit test seam: commands run here, with no boundary at all.

    Nothing changes ownership, nothing is frozen, and there is no
    protected evidence -- so what this policy writes into an artifact
    record names itself rather than an executor identity a reader could
    mistake for a deployed one. It is never selected by configuration; a
    test constructs it and passes it in.
    """

    owns_files = False
    audited = False
    runner: Callable = field(default=executor.local_run)

    def run(self, cmd, *, cwd, env, timeout, mode="plain", gpu=False):
        return self.runner(
            cmd, cwd=cwd, env=env, timeout=timeout, gpu=gpu,
            profile_gpu=mode == "profiled", audit_exec=mode == "audited",
        )

    def status(self) -> dict:
        return {"ok": True, "backend": "injected-test-runner"}

    def identity(self) -> dict:
        return {"executor_identity": "explicit-local-test-runner", "image_id": None}


def attempt_directory(work_root, attempt_id) -> str:
    """The one directory an attempt owns, named so two ids cannot share one."""
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", attempt_id)
    if safe != attempt_id or safe in ("", ".", "..", ".artifacts"):
        # ``@`` cannot occur on the unchanged path above.  Reserving it for
        # encoded names prevents an attacker from supplying the sanitized name
        # of another attempt directly and landing in the same workspace.
        digest = hashlib.sha256(str(attempt_id).encode()).hexdigest()
        safe = f"@{(safe or 'attempt')[:80]}-{digest}"
    return os.path.join(str(work_root), safe)


def path_inside(directory, relative) -> str | None:
    """The absolute path of a file a manifest named, or None if it left `directory`.

    A properties module, a timing output and a target executable are all
    paths out of the code's own manifest, so they get the same treatment
    as a submitted tree path: one that climbs out, or is absolute, names
    a file this service will not touch.
    """
    if not isinstance(relative, (str, os.PathLike)) or os.path.isabs(relative):
        return None
    directory = os.path.abspath(str(directory))
    path = os.path.normpath(os.path.join(directory, relative))
    if path != directory and not path.startswith(directory + os.sep):
        return None
    real_root = os.path.realpath(directory)
    real_path = os.path.realpath(path)
    if real_path != real_root and not real_path.startswith(real_root + os.sep):
        return None
    return path


def write_tree(tree_dir, tree) -> str:
    """Write the submitted files under `tree_dir`, directories and all.

    `tree` is [{"path": str, "b64": str}] -- the whole tracked tree, not a
    filtered source list, because a code's build reads namelists, include
    files, and data the harness has no way to recognize. Paths are
    relative to the tree root and may not climb out of it: a path that
    would write outside the workspace is refused by name rather than
    written somewhere surprising.
    """
    tree_dir = os.path.abspath(str(tree_dir))
    for entry in tree:
        path = entry["path"]
        if os.path.isabs(path) or not path or ".." in path.replace("\\", "/").split("/"):
            raise ValueError(f"tree path {path!r} does not stay inside the tree")
        destination = os.path.join(tree_dir, path)
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        with open(destination, "wb") as out:
            out.write(base64.b64decode(entry["b64"]))
    return tree_dir


def sha256_file(path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class Workspace:
    """One attempt's directory, its built artifacts, and how to run against them.

    A stage is handed one of these instead of an attempt id and a work
    root, so the rules about where an attempt's files are, who owns them
    while a job runs, and what proves an executable is the one that was
    built are written once here rather than once per stage.
    """

    def __init__(self, work_root, attempt_id, policy):
        self.work_root = str(work_root)
        self.attempt_id = attempt_id
        self.policy = policy
        self.root = os.path.abspath(attempt_directory(self.work_root, attempt_id))
        self.tree_dir = os.path.join(self.root, "tree")

    # ---------------------------------------------------------------- paths

    def path(self, *parts) -> str:
        return os.path.join(self.root, *parts)

    def in_tree(self, relative) -> str | None:
        return path_inside(self.tree_dir, relative)

    @property
    def artifact_file(self) -> str:
        return os.path.join(
            self.work_root, ".artifacts", os.path.basename(self.root) + ".json",
        )

    def reset(self) -> None:
        """Start the attempt from nothing, so nothing an earlier one left is read."""
        shutil.rmtree(self.root, ignore_errors=True)

    def write_tree(self, tree) -> str:
        return write_tree(self.tree_dir, tree)

    # ------------------------------------------------------------ identity

    def identity_of(self, path) -> dict:
        return {"sha256": sha256_file(path), "size": os.path.getsize(path)}

    def matches(self, path, identity) -> bool:
        """Are these still the bytes that were recorded for this file."""
        return bool(
            identity and path and os.path.isfile(path) and not os.path.islink(path)
            and os.path.getsize(path) == identity.get("size")
            and sha256_file(path) == identity.get("sha256")
        )

    def write_artifacts(self, targets) -> dict:
        """Record what the build produced, where only the supervisor can write it."""
        records = {}
        for target in targets:
            path = self.in_tree(target["executable"])
            if path is None or not os.path.isfile(path) or os.path.islink(path):
                continue
            records[target["executable"]] = {
                **self.identity_of(path), "role": target["role"],
            }
        destination = self.artifact_file
        os.makedirs(os.path.dirname(destination), mode=0o700, exist_ok=True)
        execution = self.policy.identity()
        temporary = destination + ".new"
        with open(temporary, "w", encoding="utf-8") as out:
            json.dump({
                "attempt_id": self.attempt_id, "executables": records,
                "executor_identity": execution.get("executor_identity"),
                "image_id": execution.get("image_id"),
            }, out, sort_keys=True)
        os.replace(temporary, destination)
        return records

    def artifact_identities(self) -> contract.ArtifactsResponse:
        """The recorded identities, each reverified against the bytes on disk."""
        try:
            with open(self.artifact_file, encoding="utf-8") as source:
                record = json.load(source)
        except (OSError, ValueError):
            return contract.ArtifactsResponse.failure(attempt_id=self.attempt_id)
        valid = {}
        for relative, identity in record.get("executables", {}).items():
            valid[relative] = {
                **identity, "verified": self.matches(self.in_tree(relative), identity),
            }
        return contract.ArtifactsResponse(
            ok=bool(valid) and all(item["verified"] for item in valid.values()),
            attempt_id=self.attempt_id, executables=valid,
            executor_identity=record.get("executor_identity"),
            image_id=record.get("image_id"),
        )

    def executable(self, relative):
        """(path, identity) of a built executable, or (None, None).

        Answers with nothing at all unless the file is there and its
        supervisor-held identity still matches, so a stage that got a
        path has already been told the bytes are the ones the build
        produced.
        """
        path = self.in_tree(relative)
        if path is None or not os.path.isfile(path) or os.path.islink(path):
            return None, None
        if not self.policy.audited:
            # A test builds tiny fixture trees without going through build(),
            # so there is no supervisor record to check them against.
            return path, self.identity_of(path)
        record = self.artifact_identities()
        identity = record.executables.get(relative)
        if not identity or not identity.get("verified"):
            return None, None
        answer = {k: identity[k] for k in ("sha256", "size", "role") if k in identity}
        answer["executor_identity"] = record.executor_identity
        return path, answer

    # ----------------------------------------------------------- ownership

    def prepare_job_files(self, *, include_tree=False) -> None:
        """Give the fixed unprivileged job uid ownership of this attempt only."""
        if not self.policy.owns_files:
            return
        workspace = os.path.abspath(self.root)
        protected_parent = os.path.isdir(self.tree_dir)
        for root, directories, files in os.walk(workspace):
            if not include_tree and root == self.tree_dir:
                directories[:] = []
                continue
            if not include_tree and root == workspace and "tree" in directories:
                directories.remove("tree")
            if protected_parent and root == workspace:
                os.chown(root, 0, 0, follow_symlinks=False)
                os.chmod(root, 0o555)
            else:
                os.chown(root, JOB_UID, JOB_UID, follow_symlinks=False)
            for name in directories:
                os.chown(os.path.join(root, name), JOB_UID, JOB_UID, follow_symlinks=False)
            for name in files:
                os.chown(os.path.join(root, name), JOB_UID, JOB_UID, follow_symlinks=False)

    def freeze_tree(self, identities) -> None:
        """After build, submitted jobs can read code and execute bound binaries only."""
        if not self.policy.owns_files:
            return
        executables = {os.path.normpath(name) for name in identities}
        for root, directories, files in os.walk(self.tree_dir):
            os.chown(root, 0, 0, follow_symlinks=False)
            os.chmod(root, 0o555)
            for name in directories:
                path = os.path.join(root, name)
                os.chown(path, 0, 0, follow_symlinks=False)
                if not os.path.islink(path):
                    os.chmod(path, 0o555)
            for name in files:
                path = os.path.join(root, name)
                relative = os.path.relpath(path, self.tree_dir)
                os.chown(path, 0, 0, follow_symlinks=False)
                if not os.path.islink(path):
                    os.chmod(path, 0o555 if relative in executables else 0o444)

    def writable_copy(self, path) -> None:
        """Make a disposable copied tree usable as an application work directory."""
        if not self.policy.owns_files:
            return
        for root, directories, files in os.walk(path):
            os.chmod(root, 0o755)
            for name in directories:
                child = os.path.join(root, name)
                if not os.path.islink(child):
                    os.chmod(child, 0o755)
            for name in files:
                child = os.path.join(root, name)
                if not os.path.islink(child):
                    mode = os.stat(child).st_mode
                    os.chmod(child, 0o755 if mode & 0o111 else 0o644)

    # ----------------------------------------------------------- execution

    def execute(self, cmd, *, cwd=None, env=None, timeout=300, mode="plain",
                gpu=False, what="the command") -> executor.JobResult:
        """Run one submitted command under this workspace's execution policy.

        `mode` is how much the run is watched: plain, `audited` for the
        protected account of what executed, `profiled` for the
        profiler's account of what the GPU ran. A command that never
        reaches an exit status raises ExecutionFailed, so a stage
        reports one sentence rather than catching whichever exception
        the executor happened to raise.
        """
        try:
            return self.policy.run(
                cmd, cwd=cwd, env=env, timeout=timeout, mode=mode, gpu=gpu,
            )
        except subprocess.TimeoutExpired:
            raise ExecutionFailed(
                f"{what} did not finish within {timeout} seconds", timed_out=True,
            ) from None
        except executor.IsolationUnavailable as exc:
            raise ExecutionFailed(str(exc), unavailable=True) from None
        except OSError as exc:
            raise ExecutionFailed(f"{what} could not be run: {exc}", unavailable=True) from None
        except ValueError as exc:
            raise ExecutionFailed(f"{what} left evidence that could not be read: {exc}") from None
