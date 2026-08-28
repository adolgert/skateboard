from pathlib import Path

import pytest
import yaml

from equivalent.strategy.schema import REQUIRED_FIELDS, load_strategy

FILES_DIR = Path(__file__).resolve().parents[2] / "strategy" / "files"
STRATEGY_FILES = {
    "stdpar_managed": FILES_DIR / "stdpar_managed.yaml",
    "omp_target": FILES_DIR / "omp_target.yaml",
    "cpu_reference": FILES_DIR / "cpu_reference.yaml",
}

# The strategy file is the only place these flags are written down: the
# builder is handed them and passes them through, so there is nothing left
# to compare them against. Spelling them out again here is what makes a
# change to any of them a deliberate edit in two places rather than a
# quiet one -- these are the flags every accepted port was compiled with.
EXPECTED_FLAGS = {
    "stdpar_managed": ("-O2", "-stdpar=gpu", "-gpu=cc89,mem:managed", "-Minfo=accel"),
    "omp_target": ("-O2", "-mp=gpu", "-gpu=cc89,mem:managed", "-Minfo=accel"),
    "cpu_reference": ("-O2", "-stdpar=multicore"),
}

# Which offload runtime is asked to announce its kernel launches, and
# whether a host fallback is a runtime error.
EXPECTED_DEVICE_PROOF = {
    "stdpar_managed": {"notify": "acc", "mandatory": False},
    "omp_target": {"notify": "omp", "mandatory": True},
    "cpu_reference": {"notify": None, "mandatory": False},
}


@pytest.mark.parametrize("name", sorted(STRATEGY_FILES))
def test_the_flags_are_the_ones_the_compiler_will_be_given(name):
    strategy = load_strategy(STRATEGY_FILES[name])
    assert strategy.languages["fortran"].flags == EXPECTED_FLAGS[name]


@pytest.mark.parametrize("name", sorted(STRATEGY_FILES))
def test_the_device_proof_matches_what_the_strategy_offloads(name):
    strategy = load_strategy(STRATEGY_FILES[name])
    expected = EXPECTED_DEVICE_PROOF[name]
    assert strategy.device_proof.notify == expected["notify"]
    assert strategy.device_proof.mandatory == expected["mandatory"]


@pytest.mark.parametrize("name", sorted(STRATEGY_FILES))
def test_every_strategy_requires_its_own_compiler(name):
    strategy = load_strategy(STRATEGY_FILES[name])
    assert strategy.languages["fortran"].compiler in strategy.required_tools


@pytest.mark.parametrize("name", sorted(STRATEGY_FILES))
def test_a_strategy_that_runs_sanitizers_requires_the_sanitizer(name):
    strategy = load_strategy(STRATEGY_FILES[name])
    if strategy.sanitizers:
        assert "compute-sanitizer" in strategy.required_tools


def test_the_baseline_strategy_offloads_nothing_and_sanitizes_nothing():
    # It exists to build the pristine tree for a speedup comparison, so
    # there is no kernel to count and no GPU memory for a tool to watch.
    strategy = load_strategy(STRATEGY_FILES["cpu_reference"])

    assert strategy.device_proof.notify is None
    assert strategy.sanitizers == ()


def _base_dict():
    return yaml.safe_load(STRATEGY_FILES["stdpar_managed"].read_text())


@pytest.mark.parametrize("field", REQUIRED_FIELDS)
def test_deleting_a_required_field_fails_to_load(tmp_path, field):
    d = _base_dict()
    del d[field]
    broken = tmp_path / "broken.yaml"
    broken.write_text(yaml.safe_dump(d))
    with pytest.raises(ValueError, match=field):
        load_strategy(broken)


# One mutation per field group, used to check the hash actually moves when
# the field does. Parametrized so adding a new field means adding one entry
# here, not a new test function.
MUTATIONS = {
    "name": lambda d: d.__setitem__("name", "renamed"),
    "version": lambda d: d.__setitem__("version", d["version"] + 1),
    "allow_globs": lambda d: d["allow_globs"].append("extra/*.f90"),
    "flags": lambda d: d["languages"]["fortran"]["flags"].append("-extra-flag"),
    "link_flags": lambda d: d.__setitem__("link_flags", ["-extra"]),
    "required_tools": lambda d: d["required_tools"].append("extra-tool"),
    "device_proof": lambda d: d["device_proof"].__setitem__("mandatory", not d["device_proof"]["mandatory"]),
    "sanitizers": lambda d: d["sanitizers"].append("extracheck"),
    "analyzer_command": lambda d: d.__setitem__("analyzer_command", "a different command"),
    "sanitize_cases": lambda d: d.__setitem__("sanitize_cases", "all"),
}


@pytest.mark.parametrize("field", sorted(MUTATIONS))
def test_hash_changes_when_a_field_changes(tmp_path, field):
    original = tmp_path / "original.yaml"
    original.write_text(yaml.safe_dump(_base_dict()))
    original_hash = load_strategy(original).sha256

    mutated_dict = _base_dict()
    MUTATIONS[field](mutated_dict)
    mutated = tmp_path / "mutated.yaml"
    mutated.write_text(yaml.safe_dump(mutated_dict))
    mutated_hash = load_strategy(mutated).sha256

    assert mutated_hash != original_hash


def test_identical_content_hashes_identically(tmp_path):
    a = tmp_path / "a.yaml"
    b = tmp_path / "b.yaml"
    text = STRATEGY_FILES["stdpar_managed"].read_text()
    a.write_text(text)
    b.write_text(text)
    assert load_strategy(a).sha256 == load_strategy(b).sha256


def test_every_strategy_file_hashes_differently():
    hashes = {name: load_strategy(path).sha256 for name, path in STRATEGY_FILES.items()}

    assert len(set(hashes.values())) == len(STRATEGY_FILES)


@pytest.mark.parametrize("name", sorted(STRATEGY_FILES))
def test_bootstrap_region_allow_list_is_accepted(name):
    # The bootstrap rule: before sese/verified exists, a region's allow-list
    # is the spec file alone.
    strategy = load_strategy(STRATEGY_FILES[name])
    assert strategy.rejected_paths(["notes/regions/ch04-step.yaml"]) == []


@pytest.mark.parametrize("name", sorted(STRATEGY_FILES))
def test_a_path_outside_allow_globs_is_rejected(name):
    strategy = load_strategy(STRATEGY_FILES[name])
    rejected = strategy.rejected_paths(["src/mod_kernel.f90", "Makefile"])
    assert rejected == ["Makefile"]


@pytest.mark.parametrize("name", sorted(STRATEGY_FILES))
def test_an_uppercase_extension_is_allowed_by_a_lowercase_pattern(name):
    # The submit path and the builder payload have to agree on which files
    # belong to a region: a tree holding src/PESs/N4_UMN_PES_Class.F90 is
    # sent to the compiler, so "src/*.f90" has to accept it too.
    strategy = load_strategy(STRATEGY_FILES[name])
    assert strategy.rejected_paths([
        "src/PESs/N4_UMN_PES_Class.F90",
        "src/MOD_KERNEL.F90",
        "src/mod_kernel.f90",
    ]) == []


def test_an_uppercase_pattern_matches_a_lowercase_path(tmp_path):
    d = _base_dict()
    d["allow_globs"] = ["SRC/*.F90"]
    upper = tmp_path / "upper.yaml"
    upper.write_text(yaml.safe_dump(d))

    assert load_strategy(upper).rejected_paths(["src/mod_kernel.f90"]) == []


@pytest.mark.parametrize("name", sorted(STRATEGY_FILES))
def test_ignoring_case_does_not_let_an_unrelated_path_through(name):
    strategy = load_strategy(STRATEGY_FILES[name])
    assert strategy.rejected_paths(["MAKEFILE", "src/mod_kernel.c"]) == [
        "MAKEFILE", "src/mod_kernel.c",
    ]


@pytest.mark.parametrize("name", sorted(STRATEGY_FILES))
def test_every_strategy_file_sanitizes_the_first_case_only(name):
    strategy = load_strategy(STRATEGY_FILES[name])
    assert strategy.sanitize_cases == "first"


@pytest.mark.parametrize("value", ["all", "first"])
def test_both_case_selections_load(tmp_path, value):
    d = _base_dict()
    d["sanitize_cases"] = value
    path = tmp_path / "strategy.yaml"
    path.write_text(yaml.safe_dump(d))

    assert load_strategy(path).sanitize_cases == value


def test_any_other_case_selection_is_rejected_by_name(tmp_path):
    d = _base_dict()
    d["sanitize_cases"] = "some"
    path = tmp_path / "strategy.yaml"
    path.write_text(yaml.safe_dump(d))

    with pytest.raises(ValueError) as excinfo:
        load_strategy(path)

    assert "sanitize_cases" in str(excinfo.value)
    assert "some" in str(excinfo.value)
