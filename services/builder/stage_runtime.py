"""Workspace policy used by every builder stage."""
import os

from .workspace import WORK_ROOT, DisposableJobs, Workspace

POLICY = None
_PRODUCTION = None


def _production_policy(work_root=WORK_ROOT) -> DisposableJobs:
    global _PRODUCTION
    if _PRODUCTION is None or _PRODUCTION.work_root != os.path.abspath(str(work_root)):
        _PRODUCTION = DisposableJobs(work_root)
    return _PRODUCTION


def workspace_for(attempt_id, *, work_root=WORK_ROOT, policy=None) -> Workspace:
    """The one workspace an attempt owns, under the policy this service runs."""
    return Workspace(work_root, attempt_id, policy or POLICY or _production_policy(work_root))


def isolation_status() -> dict:
    """Deployment readiness; absence of the boundary makes health fail closed."""
    return (POLICY or _production_policy()).status()
