"""Adversarial checks for the boundary around submitted programs."""
import base64
import importlib.util
from pathlib import Path

import pytest

from services.builder import executor, stages, workspace

_AUDIT_SPEC = importlib.util.spec_from_file_location(
    "builder_audit_run", Path(__file__).resolve().parents[1] / "builder/audit-run.py",
)
audit_run = importlib.util.module_from_spec(_AUDIT_SPEC)
_AUDIT_SPEC.loader.exec_module(audit_run)


class ChecksIdentities(workspace.InProcessJobs):
    """Commands still run here, but an executable must match the record kept for it.

    That check is the production one, and it needs no container to
    exercise: the supervisor writes what it built and refuses a file
    whose bytes no longer answer to it.
    """

    audited = True


def test_service_credentials_are_not_forwarded_to_jobs():
    clean = executor._safe_environment({
        "FFLAGS": "-O2", "SKATEBOARD_TOKEN": "service-secret",
        "OPENAI_API_KEY": "model-secret", "SOME_CREDENTIAL": "also-secret",
        "AWS_ACCESS_KEY_ID": "identifier", "AWS_SECRET_ACCESS_KEY": "cloud-secret",
        "DATABASE_PASSWORD": "database-secret", "SSH_AUTH_SOCK": "/run/agent.sock",
    })
    assert clean == {"FFLAGS": "-O2"}


def test_dynamic_loader_and_profiler_controls_are_not_forwarded_to_trusted_wrappers():
    clean = executor._safe_environment({
        "LD_PRELOAD": "/job/attack.so", "LD_AUDIT": "/job/audit.so",
        "NSYS_CONFIG_FILE": "/job/fake", "PYTHONHOME": "/job/python",
        "OMP_NUM_THREADS": "4", "PYTHONPATH": "/opt/harness",
    })
    assert clean == {"OMP_NUM_THREADS": "4", "PYTHONPATH": "/opt/harness"}


def test_exec_observer_keeps_actual_path_separate_from_spoofable_argv_zero():
    row = audit_run._argv(
        '431 execve("/bin/true", ["nvfortran", "-O2", "src/main.f90"], 0x123) = 0\n'
    )
    assert row == {
        "path": "/bin/true", "argv": ["nvfortran", "-O2", "src/main.f90"],
    }


def test_job_path_translation_exposes_only_one_attempt(tmp_path):
    work = tmp_path / "work"
    tree = work / "attempt-a" / "tree"
    tree.mkdir(parents=True)
    job = executor.DockerJobExecutor(
        work_root=str(work), image="builder@sha256:abc", volume="work-volume",
        docker="/usr/bin/docker",
    )
    attempt, cwd, cmd, env = job._job_paths(
        str(tree), [str(tree / "replay"), str(work / "attempt-b" / "secret")],
        {"CASE": str(work / "attempt-a" / "case")},
    )
    assert attempt == "attempt-a"
    assert cwd == "/job/tree"
    assert cmd == ["/job/tree/replay", str(work / "attempt-b" / "secret")]
    assert env == {"CASE": "/job/case"}


def test_job_without_an_attempt_directory_is_refused(tmp_path):
    job = executor.DockerJobExecutor(
        work_root=str(tmp_path), image="builder", volume="work", docker="docker",
    )
    with pytest.raises(executor.IsolationUnavailable):
        job._job_paths(None, ["true"], {})


def test_sanitized_attempt_cannot_collide_with_a_directly_supplied_id(tmp_path):
    encoded = Path(workspace.attempt_directory(tmp_path, "code:tree")).name
    assert encoded.startswith("@")
    assert (
        workspace.attempt_directory(tmp_path, encoded)
        != workspace.attempt_directory(tmp_path, "code:tree")
    )


def test_artifact_identity_detects_executable_replacement(tmp_path):
    attempt = stages.workspace_for("attempt-1", work_root=tmp_path, policy=ChecksIdentities())
    tree = Path(attempt.tree_dir)
    tree.mkdir(parents=True)
    executable = tree / "replay"
    executable.write_bytes(b"first executable")
    executable.chmod(0o755)
    attempt.write_artifacts([{"role": "replay", "executable": "replay"}])

    assert attempt.artifact_identities().ok is True
    executable.write_bytes(b"replacement code")
    assert attempt.artifact_identities().ok is False
    assert attempt.executable("replay")[0] is None


def _without_evidence(cmd, **kwargs):
    """A job that ran and left no protected account of what it executed."""
    return executor.JobResult(0, "", "", None)


def test_a_build_with_no_protected_account_of_the_compiler_is_a_failed_build(tmp_path):
    # A build is a claim about which compiler ran with which flags. Where
    # that claim rests on the observer, an absent observation is a failed
    # build rather than a build nobody watched.
    attempt = stages.workspace_for(
        "attempt-1", work_root=tmp_path,
        policy=ChecksIdentities(runner=_without_evidence),
    )
    makefile = base64.b64encode(b"replay:\n\ttrue\n").decode()

    result = stages.build(
        attempt, [{"path": "Makefile", "b64": makefile}], "Makefile",
        [{"role": "replay", "target": "replay", "executable": "replay"}],
        "gfortran", [], [], [],
    )

    assert result.ok is False
    assert "evidence was unavailable" in result.log_tail


def test_timing_refuses_zero_repetitions_before_running(attempt):
    result = stages.time_run(attempt, "anything", repeats=0)
    assert result.ok is False
    assert result.runs_s == []
    assert "positive integer" in result.log_tail


def test_timing_checks_each_run_against_parent_expected_bytes(attempt):
    tree = Path(attempt.tree_dir)
    tree.mkdir(parents=True)
    program = tree / "timing"
    program.write_text("#!/bin/sh\nprintf wrong > result.dat\n")
    program.chmod(0o755)
    expected = {"result.dat": base64.b64encode(b"right").decode()}
    result = stages.time_run(
        attempt, "timing", outputs=["result.dat"], repeats=2, expected_outputs=expected,
    )
    assert result.ok is False
    assert len(result.runs_s) == 1
    assert "unexpected bytes" in result.log_tail


def test_program_stderr_is_not_accepted_as_gpu_profiler_evidence(attempt):
    tree = Path(attempt.tree_dir)
    tree.mkdir(parents=True)
    replay = tree / "replay"
    replay.write_text(
        "#!/bin/sh\necho 'launch CUDA kernel file=fake.f90 function=fake line=1 device=0' >&2\n"
    )
    replay.chmod(0o755)
    result = stages.run(attempt, "replay", {"case": {}}, notify="acc", mandatory=True)
    assert result.ok is False
    assert "protected nsys evidence" in result.log_tail
