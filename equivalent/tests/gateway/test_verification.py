"""Every file in a typed build record is reverified before reuse."""
from equivalent.components.answers import ArtifactsResponse
from equivalent.gateway.verification import records_still_held
from equivalent.ledger.artifacts import BuildArtifact, BuildRecord, BuildTarget

EXECUTOR = "e" * 64


class Builder:
    def __init__(self, executables):
        self.executables = executables

    def artifacts(self, attempt_id):
        return ArtifactsResponse.parse({
            "ok": True,
            "executor_identity": EXECUTOR,
            "executables": self.executables,
        })


def _record():
    return BuildRecord("attempt", (BuildTarget(
        role="replay", executable="replay", sha256="1" * 64, size=10,
        runtime_artifacts=(BuildArtifact(
            path="modules/kernel.ptx", kind="gpu_module", sha256="2" * 64, size=20,
        ),),
    ),))


def _identity(sha256, size):
    return {"sha256": sha256, "size": size, "verified": True}


def test_runtime_companion_is_verified_with_the_unchanged_executable():
    builder = Builder({
        "replay": _identity("1" * 64, 10),
        "modules/kernel.ptx": _identity("2" * 64, 20),
    })

    assert records_still_held(builder, (_record(),), EXECUTOR) is True


def test_changed_runtime_companion_refuses_the_unchanged_executable():
    builder = Builder({
        "replay": _identity("1" * 64, 10),
        "modules/kernel.ptx": _identity("3" * 64, 20),
    })

    assert records_still_held(builder, (_record(),), EXECUTOR) is False


def test_missing_runtime_companion_refuses_the_unchanged_executable():
    builder = Builder({"replay": _identity("1" * 64, 10)})

    assert records_still_held(builder, (_record(),), EXECUTOR) is False
