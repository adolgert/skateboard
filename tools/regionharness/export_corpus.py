#!/usr/bin/env python3
"""Export the captured region inputs to a CSV corpus.

The captured cases are Serialbox binary fields, one directory per invocation.
The property suite needs them as plain numbers, and it needs the per-component
range: strategies that draw arbitrary 6-tuples of "plausible" distances produce
geometrically unrealizable configurations (six pairwise distances of four points
in R^3 satisfy Cayley-Menger constraints), and a PES evaluated there is
meaningless. Staying inside an envelope derived from real trajectory data is the
only honest generator.

Usage:
  export_corpus.py [<region.yaml>] [--data-dir D] [--out F]

Prints per-component min/max -- those are the envelopes.
"""
import argparse
import csv
import sys
from pathlib import Path

import numpy as np
import yaml

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]


def parse_extent(extent):
    """'scalar'/None -> 1 element; '1:6' -> 6."""
    if extent is None or str(extent).strip().lower() == "scalar":
        return 1
    n = 1
    for part in str(extent).split(","):
        part = part.strip()
        if ":" in part:
            lo, hi = part.split(":")
            n *= int(hi) - int(lo) + 1
        else:
            n *= int(part)
    return n


def load_spec(spec_path):
    spec = yaml.safe_load(Path(spec_path).read_text())
    entry = spec["anchor"]["entry_symbol"]
    fields = []
    for f in spec.get("live_in", []):
        if f.get("src") != "argument":
            continue
        is_int = str(f.get("type", "")).strip().lower().startswith("integer")
        fields.append((f["name"], parse_extent(f.get("extent")), is_int))
    return entry, fields


def case_dirs(data_dir, entry):
    root = Path(data_dir) / ("ftg_%s_test" % entry)
    if not root.is_dir():
        sys.exit("no captured cases under %s" % root)
    return sorted(d for d in root.iterdir() if d.is_dir())


def read_case(case, entry, fields):
    row, names = [], []
    for name, count, is_int in fields:
        path = case / "input" / ("%s_%s.dat" % (entry, name))
        if not path.is_file():
            sys.exit("missing captured field %s" % path)
        vals = np.fromfile(str(path), dtype="<i4" if is_int else "<f8")
        if vals.size != count:
            sys.exit("%s: expected %d element(s), found %d" % (path, count, vals.size))
        if count == 1:
            row.append(vals[0])
            names.append(name)
        else:
            row.extend(vals.tolist())
            names.extend("%s%d" % (name.lower(), i + 1) for i in range(count))
    return names, row


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("region_yaml", nargs="?",
                    default=str(REPO / "notes/regions/n4_umn_pes.yaml"))
    ap.add_argument("--data-dir", default=str(REPO / "ftgdata"))
    ap.add_argument("--out", default=None,
                    help="default cases/<region>/corpus.csv")
    args = ap.parse_args()

    spec_path = Path(args.region_yaml).resolve()
    entry, fields = load_spec(spec_path)
    region = yaml.safe_load(spec_path.read_text())["region"].split(".")[0]
    out = Path(args.out).resolve() if args.out else HERE / "cases" / region / "corpus.csv"

    header, rows = None, []
    for case in case_dirs(args.data_dir, entry):
        names, row = read_case(case, entry, fields)
        if header is None:
            header = names
        elif names != header:
            sys.exit("%s: field layout differs from the first case" % case)
        rows.append(row)

    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", newline="") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(header)
        for row in rows:
            w.writerow(["%d" % v if isinstance(v, (int, np.integer)) else "%.17e" % v
                        for v in row])
    print("wrote %s  (%d rows)" % (out, len(rows)))

    arr = np.array(rows, dtype=float)
    print()
    print("%-8s %24s %24s" % ("column", "min", "max"))
    for j, name in enumerate(header):
        print("%-8s %24.17g %24.17g" % (name, arr[:, j].min(), arr[:, j].max()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
