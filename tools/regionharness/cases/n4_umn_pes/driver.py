#!/usr/bin/env python3
"""Python side of the generated plain-file driver `n4pes_driver`.

One region evaluation = one process. That is not a performance choice: the six
module-scope scratch arrays in N4_UMN_PES_Class have implicit SAVE (OBS-2 in
notes/regions/n4_umn_pes.yaml), so anything that reuses a process reuses that
state. A fresh process per case is the only configuration in which a result can
be attributed to its input alone.

The driver writes each output twice -- ES24.16E3 decimal and the raw IEEE-754
bit pattern as Z16.16 -- so `hexes` supports byte-exact comparison without ever
depending on decimal round-tripping.
"""
import os
import struct
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent
DRIVER = Path(os.environ.get("N4PES_DRIVER", HERE / "replay/build/n4pes_driver"))

NR = 6                      # length of the R argument, per the region spec


class DriverError(RuntimeError):
    pass


class Result:
    """Outputs of one n4pes evaluation.

    values : {'V': float, 'dVdR(1)': float, ...}
    hexes  : {'V': '407C96667B352D3A', ...}  -- raw bits, uppercase, 16 chars
    text   : output.txt verbatim (for the determinism property)
    """

    __slots__ = ("values", "hexes", "text")

    def __init__(self, values, hexes, text):
        self.values, self.hexes, self.text = values, hexes, text

    @property
    def V(self):
        return self.values["V"]

    @property
    def V_hex(self):
        return self.hexes["V"]

    @property
    def dVdR(self):
        return [self.values["dVdR(%d)" % (i + 1)] for i in range(NR)]

    @property
    def dVdR_hex(self):
        return [self.hexes["dVdR(%d)" % (i + 1)] for i in range(NR)]

    def __repr__(self):
        return "Result(V=%.17g, dVdR=%s)" % (
            self.V, "n/a" if "dVdR(1)" not in self.values else self.dVdR)


def write_input(case_dir, R, igrad):
    case_dir = Path(case_dir)
    case_dir.mkdir(parents=True, exist_ok=True)
    # %.17e round-trips an IEEE-754 double exactly through gfortran's
    # list-directed read; the driver's hex column is the check on that claim.
    text = " ".join("%.17e" % float(x) for x in R) + "\n%d\n" % int(igrad)
    (case_dir / "input.txt").write_text(text)
    return case_dir


def parse_output(case_dir):
    path = Path(case_dir) / "output.txt"
    if not path.is_file():
        raise DriverError("driver produced no %s" % path)
    text = path.read_text()
    values, hexes = {}, {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) != 3:
            raise DriverError("unparsable output line %r in %s" % (line, path))
        label, dec, bits = parts
        values[label] = float(dec)
        hexes[label] = bits
        # The decimal and the bits are two encodings of one number; if they ever
        # disagree the file is not what it claims to be.
        if struct.unpack("<d", struct.pack("<Q", int(bits, 16)))[0] != float(dec):
            raise DriverError("%s: decimal %s does not match bits %s"
                              % (label, dec, bits))
    return Result(values, hexes, text)


def run(case_dir, R, igrad, driver=None):
    """Evaluate n4pes(R, ..., igrad) in a fresh process rooted at case_dir."""
    exe = Path(driver) if driver else DRIVER
    if not exe.is_file():
        raise DriverError("driver not built: %s\n"
                          "  MODE=tree tools/regionharness/cases/n4_umn_pes/"
                          "replay/build.sh" % exe)
    case_dir = write_input(case_dir, R, igrad)
    proc = subprocess.run([str(exe), str(case_dir)],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise DriverError("driver exited %d for %s\nstdout: %s\nstderr: %s"
                          % (proc.returncode, case_dir, proc.stdout, proc.stderr))
    return parse_output(case_dir)


def bits_of(x):
    """The hex form the driver would print for a Python float."""
    return format(struct.unpack("<Q", struct.pack("<d", float(x)))[0], "016X")
