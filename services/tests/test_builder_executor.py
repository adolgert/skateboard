"""Adversarial checks for the boundary around submitted programs."""
import base64
import os
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from services.builder import audit_run, executor, profile_run, stages, workspace


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


def test_each_language_toolchain_gets_its_make_variables(tmp_path):
    env = workspace.build_env(
        None,
        [],
        ["-lcuda"],
        str(tmp_path / "compiler.jsonl"),
        "/opt/harness",
        toolchains={
            "fortran": {"compiler": "nvfortran", "flags": ["-O2"]},
            "c": {"compiler": "nvc", "flags": ["-O1"]},
            "cxx": {"compiler": "nvc++", "flags": ["-O3"]},
            "cuda": {"compiler": "nvcc", "flags": ["-arch=sm_89"]},
            "ptx": {"compiler": "ptxas", "flags": ["-arch=sm_89"]},
        },
    )

    assert env["FC"].endswith("fc-shim --skateboard-language=fortran")
    assert env["CC"].endswith("fc-shim --skateboard-language=c")
    assert env["CXX"].endswith("fc-shim --skateboard-language=cxx")
    assert env["NVCC"].endswith("fc-shim --skateboard-language=cuda")
    assert env["PTXAS"].endswith("fc-shim --skateboard-language=ptx")
    assert (env["FFLAGS"], env["CFLAGS"], env["CXXFLAGS"]) == (
        "-O2", "-O1", "-O3",
    )
    assert env["NVCCFLAGS"] == env["PTXASFLAGS"] == "-arch=sm_89"
    assert env["LDFLAGS"] == "-lcuda"


def test_worker_preparation_changes_only_its_private_subtree(tmp_path, monkeypatch):
    policy = SimpleNamespace(owns_files=True)
    attempt = workspace.Workspace(tmp_path, "attempt-local", policy)
    tree_file = Path(attempt.tree_dir) / "main.f90"
    worker_file = Path(attempt.path("mutants", "one")) / "main.f90"
    sibling_file = Path(attempt.path("mutants", "two")) / "main.f90"
    for path in (tree_file, worker_file, sibling_file):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("source\n")
        path.chmod(0o444)
    touched = []
    monkeypatch.setattr(os, "chown", lambda path, *args, **kwargs: touched.append(Path(path)))

    attempt.prepare_writable_job_subtree(worker_file.parent)

    assert worker_file.stat().st_mode & 0o777 == 0o644
    assert tree_file.stat().st_mode & 0o777 == 0o444
    assert sibling_file.stat().st_mode & 0o777 == 0o444
    assert touched and all(path == worker_file.parent or worker_file.parent in path.parents
                           for path in touched)
    with pytest.raises(ValueError, match="private attempt directory"):
        attempt.prepare_writable_job_subtree(attempt.tree_dir)


def test_exec_observer_keeps_actual_path_separate_from_spoofable_argv_zero():
    row = audit_run._argv(
        '431 execve("/bin/true", ["nvfortran", "-O2", "src/main.f90"], 0x123) = 0\n'
    )
    assert row == {
        "path": "/bin/true", "argv": ["nvfortran", "-O2", "src/main.f90"],
    }


def test_exec_observer_tracks_child_directory_and_parent_process():
    executions = audit_run.parse_trace([
        '10 execve("/usr/bin/make", ["make"], 0x123) = 0\n',
        '10 clone(child_stack=NULL, flags=SIGCHLD) = 11\n',
        '11 chdir("src") = 0\n',
        '11 execve("/usr/bin/gcc", ["gcc", "-c", "kernel.c"], 0x123) = 0\n',
    ], "/job/tree")

    assert executions[-1]["cwd"] == "/job/tree/src"
    assert executions[-1]["pid"] == 11
    assert executions[-1]["ppid"] == 10


def test_exec_observer_reassembles_interleaved_unfinished_clone_and_exec():
    executions = audit_run.parse_trace([
        '13 clone(child_stack=0x1, flags=CLONE_VM|SIGCHLD <unfinished ...>\n',
        '14 execve("/opt/nvidia/bin/nvfortran", ["nvfortran", "-O1", "main.f90"], '
        '0x123 /* 23 vars */ <unfinished ...>\n',
        '13 <... clone resumed>) = 14\n',
        '14 <... execve resumed>) = 0\n',
    ], "/job/tree")

    assert executions == [{
        "path": "/opt/nvidia/bin/nvfortran",
        "argv": ["nvfortran", "-O1", "main.f90"],
        "cwd": "/job/tree",
        "pid": 14,
        "ppid": 13,
    }]


def test_exec_observer_keeps_unparseable_execveat_as_fail_closed_evidence():
    executions = audit_run.parse_trace([
        '22 execveat(AT_FDCWD, "/usr/bin/gcc", ["gcc", ...], 0x123, 0) = 0\n',
    ], "/job/tree")

    assert executions == [{
        "path": "/usr/bin/gcc", "argv": None,
        "audit_error": "execveat arguments could not be audited",
        "cwd": "/job/tree", "pid": 22, "ppid": None,
    }]


def test_exec_observer_repairs_child_cwd_when_clone_resumes_after_child_exec():
    executions = audit_run.parse_trace([
        '10 chdir("src") = 0\n',
        '10 clone(child_stack=0x1, flags=CLONE_VM|SIGCHLD <unfinished ...>\n',
        '11 execve("/usr/bin/gcc", ["gcc", "-c", "a.c"], 0x123) = 0\n',
        '10 <... clone resumed>) = 11\n',
    ], "/job/tree")

    assert executions[-1]["cwd"] == "/job/tree/src"
    assert executions[-1]["ppid"] == 10


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
    # Commands still run in this process, but an executable must match the
    # record kept for it, which is the production check and needs no
    # container: the supervisor writes what it built and refuses a file
    # whose bytes no longer answer to it.
    attempt = stages.workspace_for(
        "attempt-1", work_root=tmp_path,
        policy=workspace.InProcessJobs(records_artifacts=True),
    )
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


@pytest.mark.parametrize("change", ["replace", "remove"])
def test_artifact_identity_detects_changed_or_missing_runtime_module(tmp_path, change):
    attempt = stages.workspace_for(
        "attempt-runtime", work_root=tmp_path,
        policy=workspace.InProcessJobs(records_artifacts=True),
    )
    tree = Path(attempt.tree_dir)
    (tree / "modules").mkdir(parents=True)
    executable = tree / "replay"
    executable.write_bytes(b"unchanged executable")
    executable.chmod(0o755)
    module = tree / "modules" / "kernel.ptx"
    module.write_bytes(b"first gpu module")
    attempt.write_artifacts([{
        "role": "replay", "executable": "replay",
        "runtime_artifacts": [{"path": "modules/kernel.ptx", "kind": "gpu_module"}],
    }])

    assert attempt.artifact_identities().ok is True
    assert attempt.executable("modules/kernel.ptx") == (None, None)
    if change == "replace":
        module.write_bytes(b"replacement gpu module")
    else:
        module.unlink()

    report = attempt.artifact_identities()
    assert report.ok is False
    assert report.executables["replay"]["verified"] is True
    assert report.executables["modules/kernel.ptx"]["verified"] is False
    assert attempt.executable("replay")[0] is None


def test_timing_scratch_has_no_mutable_copy_of_a_runtime_companion(tmp_path):
    attempt = stages.workspace_for(
        "attempt-runtime-copy", work_root=tmp_path,
        policy=workspace.InProcessJobs(records_artifacts=True),
    )
    tree = Path(attempt.tree_dir)
    (tree / "modules").mkdir(parents=True)
    (tree / "replay").write_bytes(b"executable")
    (tree / "modules" / "kernel.ptx").write_bytes(b"gpu module")
    attempt.write_artifacts([{
        "role": "replay", "executable": "replay",
        "runtime_artifacts": [{"path": "modules/kernel.ptx", "kind": "gpu_module"}],
    }])

    scratch = Path(attempt.run_directory("run-0001"))

    assert not (scratch / "modules" / "kernel.ptx").exists()
    assert (tree / "modules" / "kernel.ptx").read_bytes() == b"gpu module"


def _without_evidence(cmd, **kwargs):
    """A job that ran and left no protected account of what it executed."""
    return executor.JobResult(0, "", "", None)


def test_a_build_with_no_protected_account_of_the_compiler_is_a_failed_build(tmp_path):
    # A build is a claim about which compiler ran with which flags. Where
    # that claim rests on the observer, an absent observation is a failed
    # build rather than a build nobody watched.
    attempt = stages.workspace_for(
        "attempt-1", work_root=tmp_path,
        policy=workspace.InProcessJobs(audited=True, runner=_without_evidence),
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


def _watched_nothing(cmd, **kwargs):
    """A job whose protected account names no invocation of the replay driver."""
    return executor.JobResult(
        0, "1 passed", "",
        {"ok": True, "executions": [{"path": "/usr/bin/true", "argv": ["true"]}]},
    )


def test_a_property_run_the_observer_never_saw_replay_in_is_a_failed_run(tmp_path):
    # The counts come from the submitted process's own output, so what
    # says they are about the built binary is the observer having seen it
    # run. None seen means nothing was measured, whatever pytest printed.
    attempt = stages.workspace_for(
        "attempt-1", work_root=tmp_path,
        policy=workspace.InProcessJobs(audited=True, runner=_watched_nothing),
    )
    tree = Path(attempt.tree_dir)
    (tree / "harness").mkdir(parents=True)
    (tree / "harness" / "properties.py").write_text("def test_nothing():\n    pass\n")
    replay = tree / "replay"
    replay.write_text("#!/bin/sh\nexit 0\n")
    replay.chmod(0o755)

    result = stages.properties(
        attempt, "replay", "harness/properties.py", {}, seed=1, max_examples=2,
    )

    assert result.ok is False
    assert result.replays_observed == 0
    assert "no replay invocation" in result.log_tail


def test_the_profiler_reports_the_kernels_its_own_export_recorded(tmp_path):
    # The launch count in a claim is read from the profiler's SQLite
    # export, so this is the query that decides whether a device proof
    # says anything ran at all.
    export = tmp_path / "profile.sqlite"
    with sqlite3.connect(export) as db:
        db.execute("CREATE TABLE StringIds (id INTEGER PRIMARY KEY, value TEXT)")
        db.execute("CREATE TABLE CUPTI_ACTIVITY_KIND_KERNEL (shortName INTEGER)")
        db.executemany(
            "INSERT INTO StringIds VALUES (?, ?)", [(1, "step_kernel"), (2, "diff_kernel")],
        )
        db.executemany(
            "INSERT INTO CUPTI_ACTIVITY_KIND_KERNEL VALUES (?)", [(1,), (1,), (2,)],
        )

    evidence = profile_run.kernel_evidence(str(export))

    assert evidence["ok"] is True
    assert evidence["kernels_launched"] == 3
    assert evidence["kernel_names"] == ["diff_kernel", "step_kernel"]


def test_a_profile_export_without_the_kernel_table_is_not_kernel_evidence(tmp_path):
    empty = tmp_path / "empty.sqlite"
    sqlite3.connect(empty).close()

    with pytest.raises(sqlite3.DatabaseError):
        profile_run.kernel_evidence(str(empty))
