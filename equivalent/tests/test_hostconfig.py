"""Deriving the host's copy of the gateway configuration from the gateway's own.

The module under test lives in `deploy/`, which is deployment rather than
package code; the test lives here so that running the suite from the
repository root covers it along with everything else.
"""
import pytest
import yaml

from deploy.hostconfig import HEADER, host_config_text, write_host_config

# What the deployment's own gateway.yaml looks like, in the container's
# terms, with a second region so the test can see that regions are copied
# rather than re-typed.
GATEWAY_YAML = """\
version: 1
paths:
  repo: /repo
  ledger_root: /ledger
  working_copy: /working
  programs: /programs
  strategies: /strategies
  seed: /seed
codes:
  tsunami:
    manifest: tsunami/manifest.yaml
regions:
  "ch04:step":
    code: tsunami
    spec_path: notes/regions/ch04-step.sese.yaml
    strategy: stdpar_managed
    visible_dataset: visible
  "n4pes:region":
    code: tsunami
    spec_path: notes/regions/n4pes.sese.yaml
    strategy: omp_target
"""

MOUNTS = {
    "/repo": "/srv/deploy/state/repo",
    "/ledger": "/srv/deploy/state/ledger",
    "/working": "/srv/deploy/state/working",
    "/programs": "/srv/skateboard/programs",
    "/strategies": "/srv/equivalent/strategy/files",
    "/seed": "/srv/deploy/state/seed",
}
SESSIONS = "/srv/deploy/state/sessions"


def _gateway_yaml(tmp_path, text=GATEWAY_YAML):
    path = tmp_path / "gateway.yaml"
    path.write_text(text)
    return path


def test_every_path_becomes_a_host_path_and_sessions_is_added(tmp_path):
    result = yaml.safe_load(host_config_text(_gateway_yaml(tmp_path), MOUNTS, SESSIONS))

    assert result["paths"] == {
        "repo": "/srv/deploy/state/repo",
        "ledger_root": "/srv/deploy/state/ledger",
        "working_copy": "/srv/deploy/state/working",
        "programs": "/srv/skateboard/programs",
        "strategies": "/srv/equivalent/strategy/files",
        "seed": "/srv/deploy/state/seed",
        "sessions": SESSIONS,
    }


def test_both_regions_are_carried_through_unchanged(tmp_path):
    source = yaml.safe_load(GATEWAY_YAML)
    result = yaml.safe_load(host_config_text(_gateway_yaml(tmp_path), MOUNTS, SESSIONS))

    assert result["regions"] == source["regions"]
    assert result["version"] == source["version"]


def test_the_codes_section_is_carried_through_unchanged(tmp_path):
    # A code's manifest path is written relative to the programs directory,
    # so rewriting that one path is enough to make the host's copy resolve;
    # the codes section itself needs no translation.
    source = yaml.safe_load(GATEWAY_YAML)
    result = yaml.safe_load(host_config_text(_gateway_yaml(tmp_path), MOUNTS, SESSIONS))

    assert result["codes"] == source["codes"]


def test_a_path_with_no_mount_point_is_an_error_naming_it(tmp_path):
    text = GATEWAY_YAML.replace("  seed: /seed\n", "  seed: /somewhere-else\n")

    with pytest.raises(ValueError) as excinfo:
        host_config_text(_gateway_yaml(tmp_path, text), MOUNTS, SESSIONS)

    assert "/somewhere-else" in str(excinfo.value)
    assert "seed" in str(excinfo.value)


def test_the_written_file_says_where_it_came_from(tmp_path):
    out = tmp_path / "gateway.host.yaml"
    write_host_config(_gateway_yaml(tmp_path), out, MOUNTS, SESSIONS)

    written = out.read_text()
    assert written.startswith(HEADER)
    assert "Edit gateway.yaml" in written
    assert yaml.safe_load(written)["paths"]["repo"] == "/srv/deploy/state/repo"


def test_a_second_run_over_the_same_file_writes_the_same_bytes(tmp_path):
    # up.sh is safe to run again, so the file it writes has to be stable.
    gateway = _gateway_yaml(tmp_path)
    out = tmp_path / "gateway.host.yaml"

    write_host_config(gateway, out, MOUNTS, SESSIONS)
    first = out.read_bytes()
    write_host_config(gateway, out, MOUNTS, SESSIONS)

    assert out.read_bytes() == first
