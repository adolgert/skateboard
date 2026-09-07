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


def qualify(*, require_gpu: bool = False) -> dict:
    started_at = datetime.now(timezone.utc).isoformat()
    checks: dict[str, bool] = {}
    evidence: dict[str, object] = {}
    errors: dict[str, str] = {}
    attempt = stages.workspace_for(ATTEMPT)
    other = stages.workspace_for(OTHER_ATTEMPT)
    gpu = stages.workspace_for(GPU_ATTEMPT)

    # These exact, fixed qualification attempts are disposable. No other
    # attempt or volume content is touched.
    for disposable in (attempt, other, gpu):
        disposable.reset()
    try:
        jobs = executor.DockerJobExecutor(work_root=stages.WORK_ROOT)
        readiness = jobs.probe()

        try:
            build = _real_cpu_build(attempt)
            evidence["build"] = build.to_dict()
            target = build.targets.get("replay", {})
            audit = build.compiler_audit
            checks["real_cpu_build"] = bool(
                build.ok is True
                and build.flags_reached_every_compile is True
                and build.compiled_only_tree_source is True
                and target.get("built") is True
                and len(target.get("sha256", "")) == 64
            )
            checks["protected_compiler_audit"] = bool(
                audit.get("protected") is True
                and audit.get("collector") == "strace/execve"
                and audit.get("compiler_invocations", 0) >= 1
                and build.compiles
            )
            checks["artifact_bound_to_executor"] = bool(
                build.image_id == jobs.image_id()
                and build.executor_identity == readiness.get("executor_identity")
            )
        except Exception as exc:  # preserve the other boundary observations
            errors["build"] = str(exc)
            checks["real_cpu_build"] = False
            checks["protected_compiler_audit"] = False
            checks["artifact_bound_to_executor"] = False

        tree = attempt.tree_dir
        if checks["real_cpu_build"]:
            try:
                ran = jobs.run([os.path.join(tree, "replay")], cwd=tree, env={}, timeout=30)
                evidence["bound_executable_run"] = {
                    "returncode": ran.returncode,
                    "stdout_tail": ran.stdout[-1000:],
                    "stderr_tail": ran.stderr[-1000:],
                }
                checks["bound_executable_runs"] = (
                    ran.returncode == 0
                    and ran.stdout.rstrip().endswith("skateboard-cpu-job-ok")
                )
            except Exception as exc:
                errors["bound_executable_runs"] = str(exc)
                checks["bound_executable_runs"] = False
        else:
            checks["bound_executable_runs"] = False

        try:
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
            checks["credentials_and_network_absent"] = bool(
                isolated.returncode == 0 and secret not in isolated.stdout + isolated.stderr
            )
        except Exception as exc:
            errors["credentials_and_network_absent"] = str(exc)
            checks["credentials_and_network_absent"] = False

        try:
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
            checks["tree_harness_and_root_read_only"] = readonly.returncode == 0
        except Exception as exc:
            errors["tree_harness_and_root_read_only"] = str(exc)
            checks["tree_harness_and_root_read_only"] = False

        try:
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
            checks["exact_scratch_and_cross_attempt_mounts"] = exact.returncode == 0
        except Exception as exc:
            errors["exact_scratch_and_cross_attempt_mounts"] = str(exc)
            checks["exact_scratch_and_cross_attempt_mounts"] = False

        try:
            before = time.monotonic()
            descendant = jobs.run(
                ["sh", "-c", "sleep 300 >/tmp/qualification-descendant.log 2>&1 & exit 0"],
                cwd=tree, env={}, timeout=30,
            )
            checks["normal_descendants_cleaned"] = bool(
                descendant.returncode == 0
                and time.monotonic() - before < 10
                and _no_disposable_containers(jobs)
            )
        except Exception as exc:
            errors["normal_descendants_cleaned"] = str(exc)
            checks["normal_descendants_cleaned"] = False

        try:
            timed_out = False
            try:
                jobs.run(["sh", "-c", "sleep 300 & wait"], cwd=tree, env={}, timeout=0.5)
            except subprocess.TimeoutExpired:
                timed_out = True
            checks["timed_out_descendants_cleaned"] = bool(
                timed_out and _no_disposable_containers(jobs)
            )
        except Exception as exc:
            errors["timed_out_descendants_cleaned"] = str(exc)
            checks["timed_out_descendants_cleaned"] = False

        try:
            nsys = jobs.run(["nsys", "--version"], cwd=tree, env={}, timeout=30)
            checks["nsys_available_in_job"] = nsys.returncode == 0
            if not checks["nsys_available_in_job"]:
                errors["nsys_available_in_job"] = (nsys.stderr or nsys.stdout)[-1000:]
        except Exception as exc:
            errors["nsys_available_in_job"] = str(exc)
            checks["nsys_available_in_job"] = False

        gpu_checks = {
            "nvidia_driver_visible": readiness.get("checks", {}).get("nvidia_driver") is True,
            "nsys_available_in_job": checks["nsys_available_in_job"],
            "protected_kernel_profile": False,
            "required_sanitizers": False,
        }
        if gpu_checks["nvidia_driver_visible"] and gpu_checks["nsys_available_in_job"]:
            try:
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
                gpu_checks["protected_kernel_profile"] = bool(
                    gpu_build.ok is True
                    and gpu_build.compiler_audit.get("protected") is True
                    and gpu_build.flags_reached_every_compile is True
                    and profiled.returncode == 0
                    and profiled.evidence
                    and profiled.evidence.get("ok") is True
                    and profiled.evidence.get("kernels_launched", 0) >= 1
                )
                sanitized = stages.sanitize(
                    gpu, "gpu_probe", {"probe": {}},
                    ["memcheck", "racecheck", "initcheck"], timeout=120,
                )
                evidence["gpu_sanitizers"] = sanitized.to_dict()
                gpu_checks["required_sanitizers"] = bool(
                    sanitized.ok is True
                    and set(sanitized.per_tool) == {"memcheck", "racecheck", "initcheck"}
                    and all(
                        result.get("ok") is True and result.get("errors") == 0
                        for result in sanitized.per_tool.values()
                    )
                )
            except Exception as exc:
                errors["protected_kernel_profile"] = str(exc)

        base_checks = readiness.get("checks", {})
        cpu_checks = {name: value for name, value in checks.items() if name != "nsys_available_in_job"}
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
        answer = {
            "schema": "skateboard-builder-qualification-v1",
            "started_at": started_at,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "require_gpu": require_gpu,
            "ok": gpu_ready if require_gpu else cpu_isolation_ok,
            "cpu_isolation_ok": cpu_isolation_ok,
            "gpu_ready": gpu_ready,
            "gpu_status": gpu_status,
            "gpu_checks": gpu_checks,
            "isolation": readiness,
            "checks": checks,
            "evidence": evidence,
            "errors": errors,
            "limitations": [
                "execve tracing proves the configured compiler ran with the recorded flags and sources; "
                "it does not prove the final executable consists exclusively of those compiler outputs",
            ],
        }
    except Exception as exc:
        answer = {
            "schema": "skateboard-builder-qualification-v1",
            "started_at": started_at,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "require_gpu": require_gpu,
            "ok": False,
            "cpu_isolation_ok": False,
            "gpu_ready": False,
            "gpu_status": "unavailable",
            "checks": checks,
            "evidence": evidence,
            "errors": {**errors, "qualification": str(exc)},
        }
    finally:
        for disposable in (attempt, other, gpu):
            disposable.reset()
            try:
                os.unlink(disposable.artifact_file)
            except FileNotFoundError:
                pass
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
