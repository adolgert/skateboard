"""The profiler wrapper's account of who was still running when it read its report."""
from pathlib import Path

import pytest

from services.builder import profile_run


def _proc(tmp_path, entries):
    """A directory shaped like /proc: one numbered entry per process."""
    proc = tmp_path / "proc"
    proc.mkdir()
    (proc / "self").mkdir()
    for pid, uid, state in entries:
        entry = proc / str(pid)
        entry.mkdir()
        (entry / "status").write_text(
            f"Name:\tsomething\nState:\t{state}\nPid:\t{pid}\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\n"
        )
    return proc


def test_only_live_processes_of_the_job_uid_count(tmp_path):
    # A zombie has already exited and runs nothing; it is waiting to be
    # collected by its parent, which under pid 1 is this wrapper. Another
    # uid's processes are the supervisor's own.
    proc = _proc(tmp_path, [
        (10, 65532, "S (sleeping)"),
        (11, 65532, "Z (zombie)"),
        (12, 0, "S (sleeping)"),
        (13, 65532, "R (running)"),
    ])
    assert profile_run.processes_of(65532, str(proc)) == [10, 13]


def test_a_program_that_left_nothing_behind_is_read_as_evidence(tmp_path):
    proc = _proc(tmp_path, [(12, 0, "S (sleeping)")])
    kills = []
    assert profile_run.survivors_stopped(65532, proc=str(proc), kill=lambda: kills.append(1)) == 0
    assert kills == []


def test_survivors_are_counted_and_then_killed(tmp_path):
    proc = _proc(tmp_path, [(10, 65532, "S (sleeping)"), (13, 65532, "R (running)")])

    def kill():
        for pid in (10, 13):
            (proc / str(pid) / "status").write_text(
                f"Name:\tx\nState:\tZ (zombie)\nPid:\t{pid}\nUid:\t65532\t65532\t65532\t65532\n"
            )

    assert profile_run.survivors_stopped(65532, proc=str(proc), kill=kill) == 2


def test_a_survivor_that_cannot_be_stopped_is_an_error_not_a_count(tmp_path):
    proc = _proc(tmp_path, [(10, 65532, "S (sleeping)")])
    with pytest.raises(RuntimeError, match="could not be stopped"):
        profile_run.survivors_stopped(65532, proc=str(proc), kill=lambda: None, attempts=2)


def test_the_profiler_and_the_program_run_as_the_job_uid():
    command = profile_run.as_job_uid(["nsys", "profile"])
    assert command[0] == "/usr/bin/setpriv"
    assert f"--reuid={profile_run.JOB_UID}" in command
    assert f"--regid={profile_run.JOB_UID}" in command
    assert command[-2:] == ["nsys", "profile"]
