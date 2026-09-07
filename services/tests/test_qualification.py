"""The builder's boundary, qualified for real through the container daemon.

This runs what deploy/qualify.sh runs: the builder image is built from
this tree, and the supervisor is started the way the deployment starts
it -- read-only, no capabilities beyond owning files, the daemon socket
and a work volume of its own -- to compile, run, probe and profile in
disposable jobs. The record it writes is then read here.

It needs a usable Docker daemon; without one every test here is skipped.
On a machine that has never built the image, the first run pulls the
NVIDIA HPC SDK base image, which is large. With a driver and the
container toolkit present the GPU half is qualified too; without them
the record says the GPU is unavailable, and that is checked as such.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from services.builder import preflight

ROOT = Path(__file__).resolve().parents[2]
IMAGE = "skateboard/builder:qualification-test"
VOLUME = "skateboard-qualification-test-work"
CONTAINER = "skateboard-qualification-test"


def _docker_usable() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return subprocess.run(
            ["docker", "info"], capture_output=True, timeout=30,
        ).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _gpu_visible() -> bool:
    try:
        return subprocess.run(
            ["nvidia-smi", "-L"], capture_output=True, timeout=30,
        ).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


pytestmark = pytest.mark.skipif(not _docker_usable(), reason="no usable Docker daemon")


@pytest.fixture(scope="module")
def record():
    """The qualification record of this tree's builder image, made once."""
    built = subprocess.run(
        ["docker", "build", "-q", "-f", str(ROOT / "services/builder/Dockerfile"),
         "-t", IMAGE, str(ROOT)],
        capture_output=True, text=True, timeout=3600,
    )
    assert built.returncode == 0, built.stderr[-2000:]
    subprocess.run(["docker", "volume", "create", VOLUME], check=True, capture_output=True)
    gpu = _gpu_visible()
    command = [
        "docker", "run", "--rm", "--name", CONTAINER,
        "--read-only", "--cap-drop", "ALL",
        "--cap-add", "CHOWN", "--cap-add", "DAC_OVERRIDE", "--cap-add", "FOWNER",
        "--security-opt", "no-new-privileges:true",
        "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=256m,mode=1777",
        "--tmpfs", "/run:rw,noexec,nosuid,nodev,size=16m,mode=755",
        "--mount", "type=bind,src=/var/run/docker.sock,dst=/var/run/docker.sock",
        "--mount", f"type=volume,src={VOLUME},dst=/work",
        "-e", f"SKATEBOARD_JOB_IMAGE={IMAGE}",
        "-e", f"SKATEBOARD_WORK_VOLUME={VOLUME}",
        *(["--gpus", "all", "-e", "NVIDIA_DRIVER_CAPABILITIES=compute,utility"] if gpu else []),
        "--entrypoint", "python3", IMAGE,
        "-m", "services.builder.preflight", *(["--require-gpu"] if gpu else []),
    ]
    try:
        ran = subprocess.run(command, capture_output=True, text=True, timeout=900)
    finally:
        subprocess.run(["docker", "volume", "rm", "-f", VOLUME], capture_output=True)
    assert ran.stdout.strip(), ran.stderr[-2000:]
    answer = json.loads(ran.stdout)
    answer["_exit_code"] = ran.returncode
    answer["_gpu_visible"] = gpu
    return answer


def test_the_cpu_boundary_qualifies(record):
    assert record["errors"] == {}, record["errors"]
    assert record["checks"] == {name: True for name in preflight.CPU_CHECKS}, record["checks"]
    assert record["cpu_isolation_ok"] is True
    assert record["isolation"]["ok"] is True


def test_the_gpu_qualifies_when_a_driver_is_present_and_is_unavailable_otherwise(record):
    if record["_gpu_visible"]:
        assert record["gpu_checks"] == {name: True for name in preflight.GPU_CHECKS}, record["gpu_checks"]
        assert record["gpu_status"] == "qualified"
        assert record["gpu_ready"] is True
        profile = record["evidence"]["gpu_profile"]["protected_result"]
        assert profile["kernels_launched"] >= 1
        assert profile["kernel_names"]
        sanitizers = record["evidence"]["gpu_sanitizers"]["per_tool"]
        assert {tool: result["errors"] for tool, result in sanitizers.items()} == {
            "memcheck": 0, "racecheck": 0, "initcheck": 0,
        }
    else:
        assert record["gpu_status"] == "unavailable"
        assert record["gpu_checks"]["nvidia_driver_visible"] is False


def test_the_command_exits_by_its_verdict(record):
    assert record["ok"] is True
    assert record["_exit_code"] == 0


def test_the_record_carries_what_the_install_guide_reads(record):
    # docs/pi-install.md tells the operator to copy the executor identity
    # out of this record and pin it in the gateway's configuration.
    identity = record["isolation"]["executor_identity"]
    assert isinstance(identity, str) and len(identity) == 64
    assert record["isolation"]["image_id"].startswith("sha256:")
    assert record["schema"] == preflight.SCHEMA
    assert record["limitations"] == preflight.LIMITATIONS


def test_the_real_build_was_audited_by_the_root_owned_observer(record):
    build = record["evidence"]["build"]
    assert build["compiler_audit"]["protected"] is True
    assert build["compiler_audit"]["collector"] == "strace/execve"
    assert build["compiles"], "no compiler invocation was recorded"
    assert build["targets"]["replay"]["built"] is True
