"""Service-stage tests explicitly opt out of the production Docker executor."""
import pytest

from services.builder import executor, stages


@pytest.fixture(autouse=True)
def local_builder_job_runner(monkeypatch):
    monkeypatch.setattr(stages, "_JOB_RUNNER", executor.local_run)
    monkeypatch.setattr(stages, "_DOCKER_EXECUTOR", None)
