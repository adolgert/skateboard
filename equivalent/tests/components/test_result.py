"""Fresh check results declare evidence independently of claim detail."""
from equivalent.components.result import CheckResult
from equivalent.ledger.artifacts import BinaryArtifact, BuildRecord, BuildTarget
from equivalent.ledger.subjects import Subject


def test_incidental_nested_detail_keys_are_not_binary_materials():
    result = CheckResult(
        verdict="pass",
        detail={
            "diagnostic": {
                "targets": {"spoof": {"sha256": "b" * 64}},
                "executable_identity": {"sha256": "c" * 64},
            },
        },
        binary_artifacts=(BinaryArtifact("replay", "a" * 64, 10),),
    )

    assert result.binary_materials == (Subject(kind="binary", sha256="a" * 64),)


def test_build_targets_are_explicit_binary_materials_without_detail_scanning():
    result = CheckResult(
        verdict="pass",
        detail={},
        build_records=(BuildRecord("attempt", (
            BuildTarget("replay", "replay", "a" * 64, 10),
            BuildTarget("timing", "program", "b" * 64, 20),
        )),),
    )

    assert result.binary_materials == (
        Subject(kind="binary", sha256="a" * 64),
        Subject(kind="binary", sha256="b" * 64),
    )
