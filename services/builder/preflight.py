"""Qualify the disposable builder boundary with a real CPU build.

Run inside the trusted supervisor image. The supervisor needs the Docker
socket and the same named work volume that it gives to disposable jobs::

    python3 -m services.builder.preflight [--require-gpu]

The CPU result is useful on a host without an NVIDIA driver. ``--require-gpu``
makes the command fail unless both the CPU boundary and GPU prerequisites are
available; either way the complete JSON record is printed.
"""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import json
import os
import subprocess
import time

from . import contract, executor, stages


ATTEMPT = "isolation-preflight"
OTHER_ATTEMPT = "isolation-preflight-other"
GPU_ATTEMPT = "gpu-preflight"


def _encoded(path: str, contents: str) -> dict:
    return {"path": path, "b64": base64.b64encode(contents.encode()).decode()}


def _real_cpu_build(workspace) -> contract.BuildResponse:
    """Compile through the production stage and return reviewable evidence."""
    source = """program qualification
  implicit none
  print '(a)', 'skateboard-cpu-job-ok'
end program qualification
"""
    # The first recipe line is adversarial: submitted uid 65532 must be unable
    # to replace the root-owned observer result before it compiles.
    makefile = """replay:
\t-@printf '{\"ok\":true,\"executions\":[]}' > /run/evidence/result.json
\t$(FC) $(FFLAGS) -o replay src/main.f90 $(LDFLAGS)
"""
    return stages.build(
        workspace,
        [_encoded("src/main.f90", source), _encoded("Makefile", makefile)],
        "Makefile",
        [{"role": "replay", "target": "replay", "executable": "replay"}],
        "nvfortran",
        ["-O1"],
        [],
        ["src/*.f90", "Makefile"],
        timeout=120,
    )


def _real_gpu_build(workspace) -> contract.BuildResponse:
    """Build the kernel used to qualify protected profiler collection."""
    source = """program qualification_gpu
  implicit none
  integer :: i, values(128)
  !$acc parallel loop copyout(values)
  do i = 1, size(values)
    values(i) = i
  end do
  !$acc end parallel loop
  if (sum(values) /= 8256) error stop 1
end program qualification_gpu
"""
    makefile = """gpu_probe:
\t$(FC) $(FFLAGS) -o gpu_probe src/gpu_probe.f90 $(LDFLAGS)
"""
    return stages.build(
        workspace,
        [_encoded("src/gpu_probe.f90", source), _encoded("Makefile", makefile)],
        "Makefile",
        [{"role": "gpu_probe", "target": "gpu_probe", "executable": "gpu_probe"}],
        "nvfortran",
        ["-O1", "-acc=gpu"],
        [],
        ["src/*.f90", "Makefile"],
        timeout=120,
    )


def _no_disposable_containers(jobs: executor.DockerJobExecutor) -> bool:
    listed = subprocess.run(
        [jobs.docker, "ps", "-aq", "--filter", "label=skateboard.disposable-job=true"],
        capture_output=True, text=True, timeout=15,
    )
    return listed.returncode == 0 and not listed.stdout.strip()


# Every observation this qualification makes about the CPU boundary, and
# every one it makes about the GPU. They are named here, and defaulted to
# False before anything runs, so a record that stopped early still says
# what was not established rather than leaving the reader to notice a
# missing key.
CPU_CHECKS = (
    "real_cpu_build", "protected_compiler_audit", "artifact_bound_to_executor",
    "bound_executable_runs", "credentials_and_network_absent",
    "tree_harness_and_root_read_only", "exact_scratch_and_cross_attempt_mounts",
    "normal_descendants_cleaned", "timed_out_descendants_cleaned",
    "nsys_available_in_job",
)
GPU_CHECKS = (
    "nvidia_driver_visible", "nsys_available_in_job",
    "protected_kernel_profile", "required_sanitizers",
)

SCHEMA = "skateboard-builder-qualification-v1"

# What this record does not establish, said in the record itself.
LIMITATIONS = [
    "execve tracing proves the configured compiler ran with the recorded flags and sources; "
    "it does not prove the final executable consists exclusively of those compiler outputs",
]


def _build_checks(attempt, jobs, readiness, evidence) -> dict:
    """A real compile through the production stage, and what it proved."""
    build = _real_cpu_build(attempt)
    evidence["build"] = build.to_dict()
    target = build.targets.get("replay", {})
    audit = build.compiler_audit
    return {
        "real_cpu_build": bool(
            build.ok is True
            and build.flags_reached_every_compile is True
            and build.compiled_only_tree_source is True
            and target.get("built") is True
            and len(target.get("sha256", "")) == 64
        ),
        "protected_compiler_audit": bool(
            audit.get("protected") is True
            and audit.get("collector") == "strace/execve"
            and audit.get("compiler_invocations", 0) >= 1
            and build.compiles
        ),
        "artifact_bound_to_executor": bool(
            build.image_id == jobs.image_id()
            and build.executor_identity == readiness.get("executor_identity")
        ),
    }


def _bound_executable_runs(jobs, tree, evidence) -> dict:
    """The executable the build produced runs, and is the one that was built."""
    ran = jobs.run([os.path.join(tree, "replay")], cwd=tree, env={}, timeout=30)
    evidence["bound_executable_run"] = {
        "returncode": ran.returncode,
        "stdout_tail": ran.stdout[-1000:],
        "stderr_tail": ran.stderr[-1000:],
    }
    return {"bound_executable_runs": (
        ran.returncode == 0 and ran.stdout.rstrip().endswith("skateboard-cpu-job-ok")
    )}


def _credentials_and_network_absent(jobs, tree, evidence) -> dict:
    """No service credential and no route out reaches a submitted job."""
    secret = "must-not-reach-job"
    isolated = jobs.run(
        ["python3", "-c", (
            "import os,pathlib,socket\n"
            "for name in ['SKATEBOARD_TOKEN','AWS_ACCESS_KEY_ID',"
            "'AWS_SECRET_ACCESS_KEY','DATABASE_PASSWORD','SSH_AUTH_SOCK']:\n"
            " assert os.getenv(name) is None\n"
            "assert not any(pathlib.Path('/work').iterdir())\n"
            "for address in [('gateway',8000),('1.1.1.1',53)]:\n"
            " try:\n  socket.create_connection(address,.2)\n"
            " except OSError:\n  pass\n"
            " else:\n  raise AssertionError('job has a network route')\n"
        )],
        cwd=tree, env={
            "SKATEBOARD_TOKEN": secret,
            "AWS_ACCESS_KEY_ID": secret,
            "AWS_SECRET_ACCESS_KEY": secret,
            "DATABASE_PASSWORD": secret,
            "SSH_AUTH_SOCK": "/run/qualification-agent.sock",
        }, timeout=30,
    )
    evidence["network_and_credentials"] = {
        "returncode": isolated.returncode,
        "stdout_tail": isolated.stdout[-1000:],
        "stderr_tail": isolated.stderr[-1000:],
    }
    return {"credentials_and_network_absent": bool(
        isolated.returncode == 0 and secret not in isolated.stdout + isolated.stderr
    )}


def _read_only_surfaces(jobs, tree) -> dict:
    """A job can read the tree and the harness, and write to neither."""
    readonly = jobs.run(
        ["python3", "-c", (
            "import pathlib\n"
            "for name in ['/job/tree/src/main.f90','/opt/harness/npy_io.f90','/new-file']:\n"
            " try:\n  pathlib.Path(name).open('ab')\n"
            " except OSError:\n  pass\n"
            " else:\n  raise AssertionError(name + ' was writable')\n"
        )],
        cwd=tree, env={}, timeout=30,
    )
    return {"tree_harness_and_root_read_only": readonly.returncode == 0}


def _exact_mounts(attempt, other, jobs, tree) -> dict:
    """A job sees the scratch it was given, and no other attempt's files."""
    cases = attempt.path("cases")
    selected = os.path.join(cases, "case-a")
    hidden = os.path.join(cases, "case-b")
    os.makedirs(selected, exist_ok=True)
    os.makedirs(hidden, exist_ok=True)
    with open(os.path.join(selected, "selected"), "w", encoding="utf-8") as out:
        out.write("visible")
    with open(os.path.join(hidden, "hidden"), "w", encoding="utf-8") as out:
        out.write("must stay hidden")
    os.makedirs(other.tree_dir, exist_ok=True)
    other_secret = os.path.join(other.tree_dir, "other-attempt-secret")
    with open(other_secret, "w", encoding="utf-8") as out:
        out.write("must stay hidden")
    attempt.prepare_job_files()
    exact = jobs.run(
        ["python3", "-c", (
            "import pathlib,sys\n"
            "assert pathlib.Path(sys.argv[1], 'selected').read_text() == 'visible'\n"
            "assert not pathlib.Path('/job/cases/case-b').exists()\n"
            "assert not pathlib.Path(sys.argv[2]).exists()\n"
        ), selected, other_secret],
        cwd=tree, env={}, timeout=30,
    )
    return {"exact_scratch_and_cross_attempt_mounts": exact.returncode == 0}


def _descendants_cleaned(jobs, tree) -> dict:
    """A job that leaves a background process behind does not outlive its container."""
    before = time.monotonic()
    descendant = jobs.run(
        ["sh", "-c", "sleep 300 >/tmp/qualification-descendant.log 2>&1 & exit 0"],
        cwd=tree, env={}, timeout=30,
    )
    return {"normal_descendants_cleaned": bool(
        descendant.returncode == 0
        and time.monotonic() - before < 10
        and _no_disposable_containers(jobs)
    )}


def _timed_out_descendants_cleaned(jobs, tree) -> dict:
    """And neither does one that had to be cut off at its timeout."""
    timed_out = False
    try:
        jobs.run(["sh", "-c", "sleep 300 & wait"], cwd=tree, env={}, timeout=0.5)
    except subprocess.TimeoutExpired:
        timed_out = True
    return {"timed_out_descendants_cleaned": bool(timed_out and _no_disposable_containers(jobs))}


def _nsys_available(jobs, tree, errors) -> dict:
    """The profiler a device proof rests on is present inside a job."""
    nsys = jobs.run(["nsys", "--version"], cwd=tree, env={}, timeout=30)
    if nsys.returncode != 0:
        errors["nsys_available_in_job"] = (nsys.stderr or nsys.stdout)[-1000:]
    return {"nsys_available_in_job": nsys.returncode == 0}


def _gpu_qualification(gpu, jobs, evidence) -> dict:
    """A real offloaded build, profiled and sanitized the way a port would be."""
    gpu_build = _real_gpu_build(gpu)
    evidence["gpu_build"] = gpu_build.to_dict()
    gpu_tree = gpu.tree_dir
    profiled = jobs.run(
        [os.path.join(gpu_tree, "gpu_probe")], cwd=gpu_tree, env={},
        timeout=120, profile_gpu=True,
    )
    evidence["gpu_profile"] = {
        "returncode": profiled.returncode,
        "stdout_tail": profiled.stdout[-1000:],
        "stderr_tail": profiled.stderr[-1000:],
        "protected_result": profiled.evidence,
    }
    sanitized = stages.sanitize(
        gpu, "gpu_probe", {"probe": {}},
        ["memcheck", "racecheck", "initcheck"], timeout=120,
    )
    evidence["gpu_sanitizers"] = sanitized.to_dict()
    return {
        "protected_kernel_profile": bool(
            gpu_build.ok is True
            and gpu_build.compiler_audit.get("protected") is True
            and gpu_build.flags_reached_every_compile is True
            and profiled.returncode == 0
            and profiled.evidence
            and profiled.evidence.get("ok") is True
            and profiled.evidence.get("kernels_launched", 0) >= 1
        ),
        "required_sanitizers": bool(
            sanitized.ok is True
            and set(sanitized.per_tool) == {"memcheck", "racecheck", "initcheck"}
            and all(
                result.get("ok") is True and result.get("errors") == 0
                for result in sanitized.per_tool.values()
            )
        ),
    }


def qualify(*, require_gpu: bool = False) -> dict:
    started_at = datetime.now(timezone.utc).isoformat()
    checks = {name: False for name in CPU_CHECKS}
    gpu_checks = {name: False for name in GPU_CHECKS}
    evidence: dict[str, object] = {}
    errors: dict[str, str] = {}
    # The whole record, complete before anything is attempted. Whatever
    # happens below, an operator and the tooling that reads this file find
    # every key they were told to read, saying what was not established
    # rather than not saying anything.
    answer = {
        "schema": SCHEMA,
        "started_at": started_at,
        "finished_at": None,
        "require_gpu": require_gpu,
        "ok": False,
        "cpu_isolation_ok": False,
        "gpu_ready": False,
        "gpu_status": "unavailable",
        "gpu_checks": gpu_checks,
        "isolation": {"ok": False, "executor_identity": None, "image_id": None},
        "checks": checks,
        "evidence": evidence,
        "errors": errors,
        "limitations": LIMITATIONS,
    }

    attempt = stages.workspace_for(ATTEMPT)
    other = stages.workspace_for(OTHER_ATTEMPT)
    gpu = stages.workspace_for(GPU_ATTEMPT)

    def _check(name, observe) -> None:
        """One observation of the boundary, or the reason it could not be made.

        A check that raised is a check that did not pass, and its reason
        is kept beside it: a record that lost the reason would say a
        boundary failed without saying how.
        """
        try:
            checks.update({key: bool(value) for key, value in observe().items()})
        except Exception as exc:
            errors[name] = str(exc)

    # These exact, fixed qualification attempts are disposable. No other
    # attempt or volume content is touched.
    for disposable in (attempt, other, gpu):
        disposable.reset()
    try:
        jobs = executor.DockerJobExecutor(work_root=stages.WORK_ROOT)
        readiness = jobs.probe()
        answer["isolation"] = readiness
        tree = attempt.tree_dir

        _check("build", lambda: _build_checks(attempt, jobs, readiness, evidence))
        if checks["real_cpu_build"]:
            _check("bound_executable_runs", lambda: _bound_executable_runs(jobs, tree, evidence))
        _check(
            "credentials_and_network_absent",
            lambda: _credentials_and_network_absent(jobs, tree, evidence),
        )
        _check("tree_harness_and_root_read_only", lambda: _read_only_surfaces(jobs, tree))
        _check(
            "exact_scratch_and_cross_attempt_mounts",
            lambda: _exact_mounts(attempt, other, jobs, tree),
        )
        _check("normal_descendants_cleaned", lambda: _descendants_cleaned(jobs, tree))
        _check("timed_out_descendants_cleaned", lambda: _timed_out_descendants_cleaned(jobs, tree))
        _check("nsys_available_in_job", lambda: _nsys_available(jobs, tree, errors))

        gpu_checks["nvidia_driver_visible"] = (
            readiness.get("checks", {}).get("nvidia_driver") is True
        )
        gpu_checks["nsys_available_in_job"] = checks["nsys_available_in_job"]
        if gpu_checks["nvidia_driver_visible"] and gpu_checks["nsys_available_in_job"]:
            try:
                gpu_checks.update(_gpu_qualification(gpu, jobs, evidence))
            except Exception as exc:
                errors["protected_kernel_profile"] = str(exc)

        base_checks = readiness.get("checks", {})
        # nsys is a GPU prerequisite rather than part of the CPU boundary.
        cpu_checks = {
            name: value for name, value in checks.items() if name != "nsys_available_in_job"
        }
        cpu_isolation_ok = bool(
            all(base_checks.get(name) is True for name in ("daemon", "volume", "image"))
            and cpu_checks and all(cpu_checks.values())
        )
        gpu_ready = bool(cpu_isolation_ok and all(gpu_checks.values()))
        if gpu_ready:
            gpu_status = "qualified"
        elif not gpu_checks["nvidia_driver_visible"]:
            gpu_status = "unavailable"
        else:
            gpu_status = "failed"
        answer.update({
            "ok": gpu_ready if require_gpu else cpu_isolation_ok,
            "cpu_isolation_ok": cpu_isolation_ok,
            "gpu_ready": gpu_ready,
            "gpu_status": gpu_status,
        })
    except Exception as exc:
        errors["qualification"] = str(exc)
    finally:
        for disposable in (attempt, other, gpu):
            disposable.reset()
    answer["finished_at"] = datetime.now(timezone.utc).isoformat()
    return answer


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--require-gpu", action="store_true")
    args = parser.parse_args(argv)
    answer = qualify(require_gpu=args.require_gpu)
    print(json.dumps(answer, indent=2, sort_keys=True))
    return 0 if answer["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
