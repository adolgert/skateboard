"""Read and write the on-disk capture/replay case format."""
import base64
import glob
import json
import os
import shutil

INPUT_SUFFIX = ".npy"
OUTPUT_SUFFIX = ".out.npy"
CASE_FILE = "case.json"
CASES_FILE = "cases.json"

def _write_case(cdir, arrs):
    """One case directory holding the inputs the caller sent, and nothing else.

    The variable names come from the request; what each file holds comes
    from the file. The directory is rebuilt from scratch every time, so a
    replay never sees an output an earlier run left behind.
    """
    shutil.rmtree(cdir, ignore_errors=True)
    os.makedirs(cdir, exist_ok=True)
    for variable, encoded in arrs.items():
        if not _plain_name(variable):
            raise ValueError(f"case variable {variable!r} is not a plain name")
        try:
            data = base64.b64decode(encoded)
        except (ValueError, TypeError):
            raise ValueError(
                f"variable {variable!r} did not arrive as base64 array bytes"
            ) from None
        with open(os.path.join(cdir, f"{variable}{INPUT_SUFFIX}"), "wb") as f:
            f.write(data)
    return cdir


def _read_outputs(cdir):
    """Every output file the replay driver left in one case directory.

    The builder is not told which outputs to expect: it returns whatever
    the driver wrote, and the gateway and the oracle are the ones that
    know what the code declares. So a driver that wrote nothing produces
    an empty set here rather than an error about a name this file guessed.
    """
    outputs = {}
    for path in sorted(glob.glob(os.path.join(cdir, f"*{OUTPUT_SUFFIX}"))):
        if os.path.islink(path) or not os.path.isfile(path):
            raise ValueError("a replay output is not a regular file")
        variable = os.path.basename(path)[: -len(OUTPUT_SUFFIX)]
        with open(path, "rb") as f:
            outputs[variable] = base64.b64encode(f.read()).decode()
    return outputs
def _plain_name(name) -> bool:
    """Is this a variable name and not a way out of the case directory."""
    return (
        isinstance(name, str) and name not in ("", ".", "..")
        and "/" not in name and "\\" not in name
    )


def _case_problem(cases, sections=()) -> str | None:
    """Why these cases cannot be written to disk, or None if they can.

    A case name or a variable name that is not a plain name is a path out
    of the directory it belongs in. Every stage that writes cases asks
    this before it writes any of them, so a name like that is one stage's
    refusal, in the shape that stage promised, rather than an exception
    the caller reads as a crashed service.
    """
    if not isinstance(cases, dict):
        return "the cases must be a mapping of case name to its arrays"
    for name, arrays in cases.items():
        if not _plain_name(name):
            return f"case name {name!r} is not a plain name"
        if not isinstance(arrays, dict):
            return f"case '{name}' is not a mapping of variable name to array"
        groups = [arrays.get(section, {}) for section in sections] if sections else [arrays]
        for group in groups:
            if not isinstance(group, dict):
                return f"case '{name}' does not hold a mapping of variable name to array"
            for variable in group:
                if not _plain_name(variable):
                    return f"case '{name}' names a variable {variable!r}, which is not a plain name"
    return None


def _read_captured_case(case_dir):
    """One captured case: the files `case.json` lists, base64 as they are on disk.

    A name the listing gives but the program never wrote is left out
    rather than invented, so the gateway sees a case that is missing a
    variable and can say which one. It is the gateway, holding the code's
    manifest, that knows what the case should have held.
    """
    listing = os.path.join(case_dir, CASE_FILE)
    if os.path.islink(listing) or not os.path.isfile(listing):
        raise ValueError(f"{CASE_FILE} is not a regular file")
    with open(listing) as f:
        listed = json.load(f)
    case = {}
    for section, suffix in (("inputs", INPUT_SUFFIX), ("outputs", OUTPUT_SUFFIX)):
        arrays = {}
        for name in listed.get(section, []):
            if not _plain_name(name):
                raise ValueError(f"{CASE_FILE} lists {name!r}, which is not a variable name")
            path = os.path.join(case_dir, f"{name}{suffix}")
            if os.path.islink(path):
                raise ValueError(f"captured array '{name}' is a symbolic link")
            if os.path.isfile(path):
                with open(path, "rb") as f:
                    arrays[name] = base64.b64encode(f.read()).decode()
        case[section] = arrays
    return case
def _write_dataset(directory, cases):
    """A directory of cases in the layout a property module reads.

    The same case directories `run` writes, plus the two listings that
    turn them into a dataset: `case.json` per case and `cases.json` for
    the set. No outputs are written -- what a property module is given is
    inputs, and what the region does with them is the thing under test.
    """
    shutil.rmtree(directory, ignore_errors=True)
    os.makedirs(directory, exist_ok=True)
    for name, arrs in cases.items():
        case_dir = _write_case(os.path.join(directory, name), arrs)
        with open(os.path.join(case_dir, CASE_FILE), "w") as f:
            json.dump({"inputs": sorted(arrs), "outputs": []}, f, indent=2)
    with open(os.path.join(directory, CASES_FILE), "w") as f:
        json.dump({"cases": sorted(cases)}, f, indent=2)
    return directory
