"""Disposable execution jobs for code supplied to the builder.

The HTTP service is a trusted supervisor.  It never executes a submitted
Makefile or executable in its own process/container: every command runs in a
fresh Docker container which can see only that attempt's volume subdirectory.
There is deliberately no automatic local fallback.  Tests inject ``local_run``
explicitly; a deployed builder without Docker isolation fails closed.
"""
from __future__ import annotations

import json
import hashlib
import os
import shutil
import subprocess
import uuid
from dataclasses import dataclass


class IsolationUnavailable(RuntimeError):
    """The configured hard isolation boundary cannot be established."""


@dataclass
class JobResult:
    returncode: int
    stdout: str
    stderr: str
    evidence: dict | None = None


_SECRET_NAMES = {
    "SKATEBOARD_TOKEN", "EQUIVALENT_TOKEN", "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
    "SSH_AUTH_SOCK", "DOCKER_CONFIG", "KUBECONFIG",
}

_SECRET_FRAGMENTS = (
    "TOKEN", "SECRET", "CREDENTIAL", "PASSWORD", "PASSWD", "PRIVATE_KEY",
    "ACCESS_KEY",
)


def _safe_environment(env: dict | None) -> dict[str, str]:
    """Pass requested build/run settings without service credentials."""
    if not env:
        return {}
    clean = {}
    for key, value in env.items():
        key = str(key)
        upper = key.upper()
        if key in _SECRET_NAMES or any(fragment in upper for fragment in _SECRET_FRAGMENTS):
            continue
        if upper in {"LD_PRELOAD", "LD_AUDIT", "PYTHONHOME", "PYTHONINSPECT", "PYTHONSTARTUP"}:
            continue
        if upper.startswith(("NSYS_", "CUDA_INJECTION", "NVTX_INJECTION")):
            continue
        if not key or "=" in key or "\x00" in key or "\x00" in str(value):
            raise ValueError(f"invalid environment variable name {key!r}")
        clean[key] = str(value)
    return clean


class DockerJobExecutor:
    """Create, attach to, and destroy one restricted container per command."""

    def __init__(self, *, work_root: str, image: str | None = None,
                 volume: str | None = None, docker: str | None = None):
        self.work_root = os.path.abspath(work_root)
        self.image = image or os.environ.get("SKATEBOARD_JOB_IMAGE", "")
        self.volume = volume or os.environ.get("SKATEBOARD_WORK_VOLUME", "")
        self.docker = docker or shutil.which("docker")
        if not self.docker or not self.image or not self.volume:
            raise IsolationUnavailable(
                "disposable Docker jobs require docker, SKATEBOARD_JOB_IMAGE, and "
                "SKATEBOARD_WORK_VOLUME"
            )
        self._image_id = None

    def image_id(self) -> str:
        if self._image_id is None:
            p = subprocess.run(
                [self.docker, "image", "inspect", "--format", "{{.Id}}", self.image],
                capture_output=True, text=True, timeout=15,
            )
            if p.returncode != 0 or not p.stdout.strip().startswith("sha256:"):
                raise IsolationUnavailable(f"could not resolve immutable job image: {p.stderr[-500:]}")
            self._image_id = p.stdout.strip()
        return self._image_id

    def probe(self) -> dict:
        checks = {}
        versions = {}
        for name, args in (
            ("daemon", ["version", "--format", "{{.Server.Version}}"]),
            ("volume", ["volume", "inspect", self.volume]),
        ):
            p = subprocess.run(
                [self.docker, *args], capture_output=True, text=True, timeout=15,
            )
            checks[name] = p.returncode == 0
            if name == "daemon" and p.returncode == 0:
                versions["docker_server"] = p.stdout.strip()
        try:
            image_id = self.image_id()
            checks["image"] = True
        except IsolationUnavailable:
            image_id = None
            checks["image"] = False
        try:
            driver = subprocess.run(
                ["nvidia-smi", "--query-gpu=driver_version", "--format=csv,noheader"],
                capture_output=True, text=True, timeout=15,
            )
            versions["nvidia_driver"] = (
                sorted(set(driver.stdout.split())) if driver.returncode == 0 else []
            )
            checks["nvidia_driver"] = driver.returncode == 0 and bool(versions["nvidia_driver"])
        except (OSError, subprocess.TimeoutExpired):
            versions["nvidia_driver"] = []
            checks["nvidia_driver"] = False
        context = {
            "contract": "skateboard-disposable-executor-v2", "image_id": image_id,
            **versions,
        }
        identity = hashlib.sha256(
            json.dumps(context, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return {
            "ok": all(checks.values()), "backend": "docker", "checks": checks,
            "image_id": image_id, "versions": versions, "executor_identity": identity,
        }

    def _job_paths(self, cwd: str | None, cmd: list[str], env: dict[str, str]):
        if cwd is None:
            raise IsolationUnavailable("an untrusted job must have an attempt working directory")
        absolute_cwd = os.path.abspath(cwd)
        try:
            relative = os.path.relpath(absolute_cwd, self.work_root)
        except ValueError as exc:
            raise IsolationUnavailable("job working directory is outside /work") from exc
        parts = relative.split(os.sep)
        if relative.startswith("..") or not parts or parts[0] in ("", ".", ".artifacts"):
            raise IsolationUnavailable("job working directory is not inside an attempt")
        attempt = parts[0]
        job_cwd = "/job" + ("/" + "/".join(parts[1:]) if len(parts) > 1 else "")
        attempt_root = os.path.join(self.work_root, attempt)

        def translated(value: str) -> str:
            value = str(value)
            if value == attempt_root:
                return "/job"
            if value.startswith(attempt_root + os.sep):
                return "/job/" + os.path.relpath(value, attempt_root).replace(os.sep, "/")
            return value

        return attempt, job_cwd, [translated(v) for v in cmd], {
            k: translated(v) for k, v in _safe_environment(env).items()
        }

    def _mount_paths(self, attempt_root: str, values) -> list[str]:
        """Existing attempt subtrees this one command actually names."""
        selected = set()
        tree = os.path.join(attempt_root, "tree")
        for raw in values:
            value = os.path.abspath(str(raw)) if os.path.isabs(str(raw)) else None
            if not value or (value != attempt_root and not value.startswith(attempt_root + os.sep)):
                continue
            if value == tree or value.startswith(tree + os.sep):
                selected.add("tree")
                continue
            candidate = value if os.path.isdir(value) else os.path.dirname(value)
            if candidate != attempt_root and os.path.isdir(candidate):
                selected.add(os.path.relpath(candidate, attempt_root))
        ordered = sorted(selected, key=lambda item: (item.count(os.sep), item))
        minimal = []
        for item in ordered:
            if not any(item == parent or item.startswith(parent + os.sep) for parent in minimal):
                minimal.append(item)
        return minimal

    def run(self, cmd: list[str], *, cwd: str | None, env: dict | None,
            timeout: float, profile_gpu: bool = False, audit_exec: bool = False,
            gpu: bool = False) -> JobResult:
        attempt, job_cwd, job_cmd, job_env = self._job_paths(cwd, cmd, env or {})
        attempt_root = os.path.join(self.work_root, attempt)
        mounts = self._mount_paths(attempt_root, [cwd, *cmd, *(env or {}).values()])
        if "tree" not in mounts:
            mounts.insert(0, "tree")
        evidence_name = "job-" + uuid.uuid4().hex
        evidence_host = os.path.join(self.work_root, ".evidence", evidence_name)
        os.makedirs(evidence_host, mode=0o700)
        os.chmod(evidence_host, 0o700)
        create = [
            self.docker, "create", "--network", "none", "--read-only",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
            "--pids-limit", os.environ.get("SKATEBOARD_JOB_PIDS", "512"),
            "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=1g,mode=1777",
            "--mount", f"type=volume,src={self.volume},dst=/run/evidence,volume-subpath=.evidence/{evidence_name}",
            "--workdir", job_cwd,
            "--label", "skateboard.disposable-job=true",
        ]
        for relative in mounts:
            option = (
                f"type=volume,src={self.volume},dst=/job/{relative},"
                f"volume-subpath={attempt}/{relative}"
            )
            build_trace = audit_exec and job_cmd and os.path.basename(job_cmd[0]) == "make"
            if relative == "tree" and not build_trace:
                option += ",readonly"
            create.extend(["--mount", option])
        for key, value in sorted(job_env.items()):
            create.extend(["--env", f"{key}={value}"])
        if profile_gpu or audit_exec:
            # The profiler remains root while profile-run drops only the submitted
            # child to the job uid.  Its report lives in root-only /run/evidence.
            create.extend(["--cap-add", "SETUID", "--cap-add", "SETGID"])
            if profile_gpu:
                job_cmd = ["python3", "-I", "/opt/sandbox/profile-run.py", "--", *job_cmd]
            else:
                create.extend(["--cap-add", "SYS_PTRACE"])
                job_cmd = ["python3", "-I", "/opt/sandbox/audit-run.py", "--", *job_cmd]
        else:
            create.extend(["--user", "65532:65532"])
        if gpu or profile_gpu:
            create.extend(["--gpus", "all"])
        create.extend([self.image_id(), *job_cmd])

        made = subprocess.run(create, capture_output=True, text=True, timeout=30)
        if made.returncode != 0:
            shutil.rmtree(evidence_host, ignore_errors=True)
            raise IsolationUnavailable(f"could not create disposable job: {made.stderr[-1000:]}")
        cid = made.stdout.strip()
        if not cid:
            shutil.rmtree(evidence_host, ignore_errors=True)
            raise IsolationUnavailable("Docker created no identifiable disposable job")
        evidence = None
        try:
            try:
                ran = subprocess.run(
                    [self.docker, "start", "--attach", cid], capture_output=True,
                    text=True, timeout=timeout,
                )
            except subprocess.TimeoutExpired:
                subprocess.run([self.docker, "kill", cid], capture_output=True, timeout=15)
                raise
            if profile_gpu or audit_exec:
                evidence_path = os.path.join(evidence_host, "result.json")
                if os.path.isfile(evidence_path) and not os.path.islink(evidence_path):
                    with open(evidence_path, encoding="utf-8") as f:
                        evidence = json.load(f)
            return JobResult(ran.returncode, ran.stdout, ran.stderr, evidence)
        finally:
            subprocess.run([self.docker, "rm", "--force", cid], capture_output=True, timeout=30)
            shutil.rmtree(evidence_host, ignore_errors=True)


def local_run(cmd, *, cwd=None, env=None, timeout=300, profile_gpu=False,
              audit_exec=False, gpu=False) -> JobResult:
    """Explicit test seam; never selected by deployment configuration."""
    p = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)
    return JobResult(p.returncode, p.stdout, p.stderr, None)
