#!/bin/bash
# Generate reference capture datasets with the pristine CPU kernel, then split
# the files across the trust boundary:
#   - visible INPUTS      -> datasets/visible      (fed to the builder's /run)
#   - visible EXPECTED    -> captures/visible      (oracle-only)
#   - held-out EVERYTHING -> captures/holdout      (oracle-only)
#
# Also runs a self-test: replaying the visible inputs through the SAME pristine
# kernel must reproduce the reference outputs bit-for-bit.
#
# This script still drives the compiler by hand. A later change turns it into
# the capture target of this code's own Makefile, which is what the manifest's
# build section already names, so that the builder runs it under the same
# contract as every other target. Do not run it casually: it regenerates the
# reference data, and a different compiler than the one that made the tracked
# captures would move every expected answer.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
CODE="$(cd "$HERE/.." && pwd)"
WORK="$CODE/baseline/src"
CAPTURE="$(cd "$HERE/../../../services/builder/harness" && pwd)"
BUILD="$HERE/.build"
FC="${FC:-gfortran}"
FFLAGS="-O2 -ffree-line-length-none"

GRID=100
STEPS=5000

rm -rf "$BUILD"; mkdir -p "$BUILD"
cd "$BUILD"

echo "=== compiling gen_reference and replay (pristine kernel, $FC) ==="
KMODS="$WORK/mod_params.f90 $WORK/mod_diff.f90 $WORK/mod_initial.f90 $WORK/mod_kernel.f90"
$FC $FFLAGS -o gen_reference $KMODS "$CAPTURE/mod_capture.f90" "$HERE/gen_reference.f90"
$FC $FFLAGS -o replay        $KMODS "$CAPTURE/mod_capture.f90" "$CAPTURE/replay.f90"

echo "=== generating VISIBLE dataset (icenter=25 decay=0.02) ==="
./gen_reference $GRID $STEPS 25 0.02 "$BUILD/visible"
echo "=== generating HELD-OUT dataset (icenter=60 decay=0.01) ==="
./gen_reference $GRID $STEPS 60 0.01 "$BUILD/holdout"

# --- self-test: replay pristine inputs must reproduce reference outputs exactly
echo "=== self-test: replay(pristine) == reference ==="
fail=0
for c in "$BUILD"/visible/case*; do
  tmp="$BUILD/selftest/$(basename "$c")"; mkdir -p "$tmp"
  cp "$c/h_in.bin" "$c/u_in.bin" "$tmp/"
  ./replay "$tmp" >/dev/null
  if ! cmp -s "$tmp/h_out.bin" "$c/h_out.bin" || ! cmp -s "$tmp/u_out.bin" "$c/u_out.bin"; then
    echo "  MISMATCH in $(basename "$c")"; fail=1
  fi
done
[ "$fail" = 0 ] && echo "  self-test PASSED (replay reproduces reference)" || { echo "  self-test FAILED"; exit 1; }

# --- distribute across the trust boundary
VIS_IN="$CODE/datasets/visible"
VIS_EXP="$CODE/captures/visible"
HLD="$CODE/captures/holdout"
rm -rf "$VIS_IN" "$VIS_EXP" "$HLD"
mkdir -p "$VIS_IN" "$VIS_EXP" "$HLD"

cases=()
for c in "$BUILD"/visible/case*; do
  name="$(basename "$c")"; cases+=("$name")
  mkdir -p "$VIS_IN/$name" "$VIS_EXP/$name"
  cp "$c/h_in.bin"  "$c/u_in.bin"  "$VIS_IN/$name/"
  cp "$c/h_out.bin" "$c/u_out.bin" "$VIS_EXP/$name/"
done
hcases=()
for c in "$BUILD"/holdout/case*; do
  name="$(basename "$c")"; hcases+=("$name")
  mkdir -p "$HLD/$name"
  cp "$c"/*.bin "$HLD/$name/"
done

# --- cases.json manifests
manifest() {  # $1=dir  $2..=case names
  local dir="$1"; shift
  python3 - "$dir" "$GRID" "$@" <<'PY'
import json, sys
d, grid, *cases = sys.argv[1:]
grid = int(grid)
json.dump({"grid_size": grid, "cases": sorted(cases)}, open(d + "/cases.json", "w"), indent=2)
PY
}
manifest "$VIS_IN"  "${cases[@]}"
manifest "$VIS_EXP" "${cases[@]}"
manifest "$HLD"     "${hcases[@]}"

echo
echo "=== done ==="
echo "visible inputs   -> $VIS_IN   (${#cases[@]} cases)"
echo "visible expected -> $VIS_EXP  (${#cases[@]} cases)"
echo "held-out         -> $HLD      (${#hcases[@]} cases)"
