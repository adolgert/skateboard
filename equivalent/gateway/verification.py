"""Framework-free checks of live backends and retained build artifacts."""
from __future__ import annotations

from equivalent.components.phase import provenance_for
from equivalent.ledger.artifacts import decode_build_records
from equivalent.ledger.subjects import Subject, is_digest
from equivalent.region.evidence import evidence_materials_for

from .errors import GatewayError


def _healthy_executor(builder) -> str:
    health = builder.healthz()
    if not health.ok:
        raise GatewayError(503, "builder executor is not ready")
    return health.executor_identity


def _verified_identity(fetch, what: str, field: str, pin: str | None) -> str:
    """Read an identity and hold it to the region's reviewed pin."""
    try:
        identity = fetch()
    except GatewayError:
        raise
    except Exception as exc:
        raise GatewayError(503, f"cannot establish {what} identity: {exc}") from exc
    if not identity:
        raise GatewayError(503, f"{what} returned no {field}")
    if not is_digest(identity):
        raise GatewayError(503, f"{what} returned an invalid {field}")
    if pin is not None and identity != pin:
        raise GatewayError(503, f"{what} {field} does not match the reviewed region pin")
    return identity


def runtime_materials(cfg, builder, oracle) -> tuple[tuple[Subject, ...], str | None]:
    """Return unpinned live-service materials and the builder identity."""
    oracle_judges = provenance_for(cfg.phase).oracle_judges
    if builder is None and cfg.executor_identity is not None:
        raise GatewayError(
            503, "region has a reviewed executor_identity but no builder is configured",
        )
    if oracle_judges and oracle is None and cfg.oracle_identity is not None:
        raise GatewayError(
            503, "region has a reviewed oracle_identity but no oracle is configured",
        )
    builder_identity = (
        _verified_identity(
            lambda: _healthy_executor(builder), "builder", "executor_identity", cfg.executor_identity,
        )
        if builder is not None else None
    )
    oracle_identity = (
        _verified_identity(
            lambda: oracle.policy().oracle_identity, "oracle", "oracle_identity", cfg.oracle_identity,
        )
        if oracle is not None and oracle_judges else None
    )
    materials = []
    if builder_identity and cfg.executor_identity is None:
        materials.append(Subject(kind="executor", sha256=builder_identity))
    if oracle_identity and cfg.oracle_identity is None:
        materials.append(Subject(kind="oracle", sha256=oracle_identity))
    return tuple(materials), builder_identity


def current_materials(cfg, strategy, baseline_strategy, builder, oracle):
    """Everything a current claim for a region must name."""
    runtime, builder_identity = runtime_materials(cfg, builder, oracle)
    return (
        *evidence_materials_for(cfg, strategy, baseline_strategy),
        *runtime,
    ), builder_identity


def artifacts_still_held(
    builder, predicate_type: str, detail: dict, executor_identity: str | None,
) -> bool:
    """Whether the protected builder sidecar confirms every build artifact.

    A malformed legacy claim is not an artifact claim the gateway can trust and
    therefore fails closed.  A failed sidecar request is unavailable, rather
    than evidence that the build disappeared.
    """
    records = decode_build_records(predicate_type, detail)
    return records_still_held(builder, records, executor_identity)


def records_still_held(builder, records, executor_identity: str | None) -> bool:
    """Verify fresh typed build records against the builder sidecar."""
    if builder is None:
        return False
    if not records:
        return False
    for record in records:
        if not record.attempt_id or not record.targets:
            return False
        try:
            report = builder.artifacts(record.attempt_id)
        except Exception as exc:
            raise GatewayError(
                503, f"cannot read the builder's record of what it built: {exc}",
            ) from exc
        if not report.ok or report.executor_identity != executor_identity:
            return False
        for target in record.targets:
            still_there = report.executables.get(target.executable)
            if still_there is None or not still_there.matches(target.as_detail()):
                return False
    return True
