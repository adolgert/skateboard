"""The qualification record, against a boundary that is played here.

Every observation preflight makes is a command sent to the disposable
job executor or a build through the production stage. These tests stand
in a scripted executor and scripted builds, so each rule about the
record -- what a failed check looks like, what a raised one looks like,
what the GPU verdict rests on, what is never left on disk -- is checked
without a container daemon. The record against a real daemon is in
test_qualification.py.
"""
import json
import subprocess

import pytest

from services.builder import contract, executor, preflight, stages
from services.builder.executor import JobResult

IDENTITY = "e" * 64
IMAGE = "sha256:" + "1" * 64
TOOLS = ("memcheck", "racecheck", "initcheck")


class PlayedJobs:
    """A disposable executor whose every answer is decided by the test.

    `script(cmd, details)` may answer a call; when it does not, the
    default answers are the ones a correct boundary would give: the
    built executable prints its line, probes exit zero, a run whose
    timeout is under a second times out, and a profiled run has one
    kernel in its protected evidence.
    """

    docker = "docker"
    volume = "played-work-volume"

    def __init__(self, *, driver=False, script=None):
        self.driver = driver
        self.script = script or (lambda cmd, details: None)
        self.calls = []

    def image_id(self):
        return IMAGE

    def probe(self):
        return {
            "ok": True, "backend": "docker",
            "checks": {"daemon": True, "volume": True, "image": True, "nvidia_driver": self.driver},
            "image_id": IMAGE, "versions": {}, "executor_identity": IDENTITY,
        }

    def run(self, cmd, *, cwd, env, timeout, profile_gpu=False, audit_exec=False, gpu=False):
        details = {"timeout": timeout, "profile_gpu": profile_gpu, "env": env, "cwd": cwd}
        self.calls.append((list(cmd), details))
        answer = self.script(cmd, details)
        if answer is not None:
            return answer
        if timeout < 1:
            raise subprocess.TimeoutExpired(cmd, timeout)
        if cmd[0].endswith("/replay"):
            return JobResult(0, "skateboard-cpu-job-ok\n", "")
        if profile_gpu:
            return JobResult(0, "", "", {"ok": True, "kernels_launched": 1, "kernel_names": ["k"]})
        return JobResult(0, "", "")


def good_build(role="replay"):
    return contract.BuildResponse(
        ok=True, flags_reached_every_compile=True, compiled_only_tree_source=True,
        targets={role: {"executable": role, "built": True, "sha256": "a" * 64, "size": 10}},
        compiles=[{"argv": ["nvfortran", "-O1"], "has_flags": True}],
        compiler_audit={
            "protected": True, "collector": "strace/process+cwd",
            "compiler_invocations": 1,
        },
        image_id=IMAGE, executor_identity=IDENTITY,
    )


def clean_sanitizers(*args, **kwargs):
    return contract.SanitizeResponse(
        ok=True, per_tool={tool: {"ok": True, "errors": 0, "log_tail": ""} for tool in TOOLS},
    )


@pytest.fixture
def boundary(monkeypatch, tmp_path):
    """Stand the played executor and scripted builds behind `qualify`."""
    real_workspace_for = stages.workspace_for

    def stand(*, driver=False, script=None, cpu_build=None, gpu_build=None, sanitize=None,
              containers_gone=True):
        jobs = PlayedJobs(driver=driver, script=script)
        monkeypatch.setattr(executor, "DockerJobExecutor", lambda **kwargs: jobs)
        monkeypatch.setattr(
            stages, "workspace_for",
            lambda attempt_id: real_workspace_for(attempt_id, work_root=tmp_path),
        )
        monkeypatch.setattr(preflight, "_real_cpu_build", cpu_build or (lambda ws: good_build()))
        monkeypatch.setattr(
            preflight, "_real_gpu_build", gpu_build or (lambda ws: good_build("gpu_probe")),
        )
        monkeypatch.setattr(stages, "sanitize", sanitize or clean_sanitizers)
        monkeypatch.setattr(preflight, "_no_disposable_containers", lambda jobs: containers_gone)
        return jobs

    return stand


def test_a_correct_cpu_boundary_qualifies_and_a_gpu_that_is_not_there_is_unavailable(boundary):
    boundary()
    answer = preflight.qualify()
    assert answer["checks"] == {name: True for name in preflight.CPU_CHECKS}
    assert answer["cpu_isolation_ok"] is True
    assert answer["ok"] is True
    assert answer["errors"] == {}
    assert answer["gpu_status"] == "unavailable"
    assert answer["gpu_ready"] is False
    assert answer["isolation"]["executor_identity"] == IDENTITY
    assert answer["finished_at"] is not None


def test_requiring_a_gpu_that_is_not_there_is_not_ok(boundary):
    boundary()
    answer = preflight.qualify(require_gpu=True)
    assert answer["cpu_isolation_ok"] is True
    assert answer["ok"] is False


def test_the_record_is_complete_when_the_executor_cannot_even_be_created(boundary, monkeypatch):
    boundary()

    def refuse(**kwargs):
        raise executor.IsolationUnavailable("no daemon here")

    monkeypatch.setattr(executor, "DockerJobExecutor", refuse)
    answer = preflight.qualify(require_gpu=True)
    assert set(answer) >= {
        "schema", "started_at", "finished_at", "require_gpu", "ok", "cpu_isolation_ok",
        "gpu_ready", "gpu_status", "gpu_checks", "isolation", "checks", "evidence",
        "errors", "limitations",
    }
    assert answer["checks"] == {name: False for name in preflight.CPU_CHECKS}
    assert answer["gpu_checks"] == {name: False for name in preflight.GPU_CHECKS}
    assert answer["isolation"] == {"ok": False, "executor_identity": None, "image_id": None}
    assert answer["ok"] is False
    assert "no daemon here" in answer["errors"]["qualification"]


def test_a_check_that_raises_is_not_established_and_keeps_its_reason(boundary):
    def script(cmd, details):
        if "SKATEBOARD_TOKEN" in details["env"]:
            raise executor.IsolationUnavailable("the probe could not be started")
        return None

    boundary(script=script)
    answer = preflight.qualify()
    assert answer["checks"]["credentials_and_network_absent"] is False
    assert "could not be started" in answer["errors"]["credentials_and_network_absent"]
    # The checks after it were still made.
    assert answer["checks"]["tree_harness_and_root_read_only"] is True
    assert answer["checks"]["timed_out_descendants_cleaned"] is True
    assert answer["cpu_isolation_ok"] is False


def test_a_build_that_did_not_count_is_never_run(boundary):
    jobs = boundary(cpu_build=lambda ws: contract.BuildResponse.failure("nvfortran: no such file"))
    answer = preflight.qualify()
    assert answer["checks"]["real_cpu_build"] is False
    assert answer["checks"]["bound_executable_runs"] is False
    assert not any(cmd[0].endswith("/replay") for cmd, _ in jobs.calls)
    assert answer["evidence"]["build"]["log_tail"] == "nvfortran: no such file"


def test_a_build_counts_only_when_bound_to_this_executor_and_image(boundary):
    def elsewhere(ws):
        build = good_build()
        return build.model_copy(update={"executor_identity": "f" * 64})

    boundary(cpu_build=elsewhere)
    answer = preflight.qualify()
    assert answer["checks"]["real_cpu_build"] is True
    assert answer["checks"]["artifact_bound_to_executor"] is False


def test_a_secret_that_reaches_a_job_fails_the_credentials_check(boundary):
    def script(cmd, details):
        if "SKATEBOARD_TOKEN" in details["env"]:
            return JobResult(0, "I can see must-not-reach-job", "")
        return None

    boundary(script=script)
    assert preflight.qualify()["checks"]["credentials_and_network_absent"] is False


def test_a_job_that_did_not_time_out_when_told_to_fails_the_cleanup_check(boundary):
    def script(cmd, details):
        if details["timeout"] < 1:
            return JobResult(0, "", "")
        return None

    boundary(script=script)
    assert preflight.qualify()["checks"]["timed_out_descendants_cleaned"] is False


def test_a_container_that_outlives_its_job_fails_both_cleanup_checks(boundary):
    boundary(containers_gone=False)
    answer = preflight.qualify()
    assert answer["checks"]["normal_descendants_cleaned"] is False
    assert answer["checks"]["timed_out_descendants_cleaned"] is False


def test_cleanup_probe_is_scoped_to_this_executors_work_volume(monkeypatch):
    calls = []

    def record(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(preflight.subprocess, "run", record)
    jobs = PlayedJobs()

    assert preflight._no_disposable_containers(jobs) is True
    assert calls == [[
        "docker", "ps", "-aq",
        "--filter", f"label={executor.DISPOSABLE_LABEL}=true",
        "--filter", f"label={executor.WORK_VOLUME_LABEL}={jobs.volume}",
    ]]


def test_a_missing_profiler_is_recorded_with_what_it_said(boundary):
    def script(cmd, details):
        if cmd[:2] == ["nsys", "--version"]:
            return JobResult(127, "", "nsys: not found")
        return None

    boundary(script=script)
    answer = preflight.qualify()
    assert answer["checks"]["nsys_available_in_job"] is False
    assert answer["errors"]["nsys_available_in_job"] == "nsys: not found"
    # The profiler is a GPU prerequisite, not part of the CPU boundary.
    assert answer["cpu_isolation_ok"] is True


def test_a_gpu_qualifies_when_the_driver_the_profile_and_the_sanitizers_all_answer(boundary):
    boundary(driver=True)
    answer = preflight.qualify(require_gpu=True)
    assert answer["gpu_checks"] == {name: True for name in preflight.GPU_CHECKS}
    assert answer["gpu_status"] == "qualified"
    assert answer["gpu_ready"] is True
    assert answer["ok"] is True
    assert answer["evidence"]["gpu_profile"]["protected_result"]["kernels_launched"] == 1


def test_a_profile_with_no_kernel_in_it_is_a_failed_gpu_not_an_absent_one(boundary):
    def script(cmd, details):
        if details["profile_gpu"]:
            return JobResult(0, "", "", {"ok": False, "kernels_launched": 0, "kernel_names": []})
        return None

    boundary(driver=True, script=script)
    answer = preflight.qualify(require_gpu=True)
    assert answer["gpu_checks"]["protected_kernel_profile"] is False
    assert answer["gpu_status"] == "failed"
    assert answer["ok"] is False
    assert answer["cpu_isolation_ok"] is True


def test_a_sanitizer_that_counted_an_error_fails_the_gpu(boundary):
    def one_error(*args, **kwargs):
        per_tool = {tool: {"ok": True, "errors": 0, "log_tail": ""} for tool in TOOLS}
        per_tool["memcheck"]["errors"] = 1
        return contract.SanitizeResponse(ok=True, per_tool=per_tool)

    boundary(driver=True, sanitize=one_error)
    answer = preflight.qualify(require_gpu=True)
    assert answer["gpu_checks"]["required_sanitizers"] is False
    assert answer["gpu_status"] == "failed"


def test_a_gpu_step_that_raises_leaves_every_gpu_check_named_with_the_reason(boundary, monkeypatch):
    boundary(driver=True)

    def broken(*args, **kwargs):
        raise RuntimeError("the GPU build could not be started")

    monkeypatch.setattr(preflight, "_gpu_qualification", broken)
    answer = preflight.qualify(require_gpu=True)
    assert set(answer["gpu_checks"]) == set(preflight.GPU_CHECKS)
    assert answer["gpu_checks"]["protected_kernel_profile"] is False
    assert "could not be started" in answer["errors"]["protected_kernel_profile"]
    assert answer["gpu_status"] == "failed"


def test_the_qualification_attempts_leave_nothing_on_the_volume(boundary, tmp_path):
    boundary(driver=True)
    stale = stages.workspace_for(preflight.ATTEMPT)
    stale.reset()
    (tmp_path / "unrelated").mkdir()
    preflight.qualify(require_gpu=True)
    for attempt in (preflight.ATTEMPT, preflight.OTHER_ATTEMPT, preflight.GPU_ATTEMPT):
        assert not (tmp_path / attempt).exists()
    assert (tmp_path / "unrelated").is_dir()


def test_main_prints_the_record_and_exits_by_its_verdict(monkeypatch, capsys):
    monkeypatch.setattr(preflight, "qualify", lambda *, require_gpu: {"ok": require_gpu, "seen": 1})
    assert preflight.main([]) == 1
    assert json.loads(capsys.readouterr().out) == {"ok": False, "seen": 1}
    assert preflight.main(["--require-gpu"]) == 0
    assert json.loads(capsys.readouterr().out) == {"ok": True, "seen": 1}
