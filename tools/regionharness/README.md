# regionharness — spec-driven capture-replay for Fortran regions

This is earlier, standalone work, kept for the n4pes case it was built
for. Nothing in it is on the gateway path and nothing in it is extended:
the capture-replay a code gets when it is brought in through the gateway
uses the NPY format and the contracts in `docs/onboarding.md`, not
Serialbox. The one file that crossed over is `check_sese.py`, which is
the analyzer the gateway runs; it now lives at
`equivalent/analyzers/check_sese.py` and is run as
`python3 -m equivalent.analyzers.check_sese <region.yaml>`. Its tests
travelled with it, so plain `pytest` no longer collects this directory's
CoarseAIR-bound ones; run them by naming the file.

Turns a region spec (`notes/regions/*.yaml`) into Serialbox capture
instrumentation, a standalone replay harness, and a set of validation gates.
Everything is deterministic code generation from the spec; no AI at run time.

Worked instance: `n4pes` (N4 UMN PES, CoarseAIR) under `cases/n4_umn_pes/`.
All artifacts dated 2026-08-05; gate results recorded in the spec's
`validation:` stanza.

## Pipeline

1. **Static footprint check (VAL-5)** — derive the used-globals set with
   FortranCallGraph from `gfortran -O0 -g` assembler and diff it against the
   spec's `live_in ∪ live_out ∪ clobbers`:
   ```
   cases/n4_umn_pes/fcg/make_asm.sh
   python3 ../fortrancallgraph/FortranCallGraph.py -cf cases/n4_umn_pes/fcg/config_fortrancallgraph.py \
       -a globals n4_umn_pes_class n4pes > fcg_globals.txt
   ./check_footprint.py notes/regions/n4_umn_pes.yaml fcg_globals.txt
   ```
   `parameter` constants are constant-folded and invisible to assembler-level
   analysis; the checker explains them rather than failing.

2. **SESE check (VAL-1)** — `python3 -m equivalent.analyzers.check_sese <region.yaml>`: no goto / early
   return / entry / stop in the anchor or its closure.

3. **Generate + instrument** — `gen_harness.py <region.yaml>` emits the capture
   module, replay driver, plain-file driver, `build.sh`, and `apply.sh`;
   `--patch/--restore/--check`
   manage four marker-delimited (`!FTG-BEGIN/END`) insertions in the anchor
   file (CRLF-preserving, idempotent, content-anchored).
   `cases/n4_umn_pes/replay/apply.sh` documents the full instrument-the-tree
   sequence (patch, register the capture module with CMake, reconfigure with
   Serialbox include/libs, rebuild).

4. **Capture** — the instrumented binary is inert unless `FTG_CAPTURE_N > 0`:
   ```
   FTG_DATA_DIR=.../ftgdata FTG_CAPTURE_ROUND=200000 FTG_CAPTURE_N=50 <run workload>
   ```
   Cases land in `$FTG_DATA_DIR/ftg_<entry>_test/rNNNN/{input,output}` —
   one directory per invocation, the same `driver <case_dir>` shape as
   `tools/fmutate`.

5. **Replay + gates** —
   ```
   MODE=tree cases/n4_umn_pes/replay/build.sh          # builds test + poison binaries
   build/ftg_n4pes_test   <case_dir>                   # tolerance-0 compare, NaN-guarded
   build/ftg_n4pes_poison <case_dir> poison            # VAL-3: sNaN-fill scratch, -ffpe-trap=invalid
   ./check_captured.py <region.yaml> <data_dir>        # VAL-2: captured fields == spec footprint
   ```

## Notes and sharp edges

- Serialbox is built with `-DSERIALBOX_ENABLE_FORTRAN=ON -DSERIALBOX_ENABLE_FTG=ON`
  (install at `tools/serialbox/install`). Never pass `rperturb`/`tolerance`
  positional REALs from `-fdefault-real-8` code — those dummies are kind-4;
  set the module knobs (`ftg_cmp_default_tolerance`) instead. The replay driver
  sets `ignore_not_existing = .FALSE.` so a typo'd field errors.
- `ftg_compare` treats NaN-vs-anything as equal; the generated driver adds a
  NaN guard counting `IEEE_IS_NAN` in replayed fields as failures.
- FortranCallGraph configs are `exec`'d inside FCG — paths in
  `config_fortrancallgraph.py` must be absolute (`__file__` lies).
- FCG reports one combined used-set (no read/write split) and misses
  subroutine-local SAVE variables; module-scope SAVE is the case it handles.
- Un-instrument: `gen_harness.py <spec> --restore`, delete
  `src/PESs/n4pes_capture_mod.F90` + its CMakeLists line; `git -C codes/CoarseAIR diff`
  must be empty. The replay binaries need the instrumented tree to link.

## Phase 5 — Hypothesis property layer

Capture-replay says the region reproduces 50 recorded points bitwise. It says
nothing about the 51st. This layer states what should hold *for all* inputs in
the region's envelope and lets Hypothesis hunt for a counterexample.

```
MODE=tree cases/n4_umn_pes/replay/build.sh      # also builds build/n4pes_driver
./export_corpus.py                              # -> cases/n4_umn_pes/corpus.csv
python cases/n4_umn_pes/permutations.py --rows 50   # measure permutation spread
python test_n4pes_properties.py --calibrate     # measure the FD tolerances
N4PES_MAX_EXAMPLES=1000 pytest test_n4pes_properties.py -v
```

- `n4pes_driver <case-dir>` reads `<case>/input.txt` (line 1: the six R, line 2:
  igrad) and writes `<case>/output.txt`, one line per live_out element as
  `<label> <ES24.16E3 decimal> <Z16.16 raw bits>`. The hex column makes byte-exact
  comparison possible from Python without trusting decimal round-tripping. It is
  emitted by `gen_harness.py` from the same spec as everything else; the shapes
  and the `defined_when` guards come from the yaml. Cross-validated against the
  Serialbox captures: 50/50 cases bitwise identical in V and all six dVdR.
- One evaluation = one process, always. The six scratch arrays have implicit
  SAVE (OBS-2), so process reuse would mean state reuse.
- Strategies perturb a captured R component-wise by ±10%, clamped to
  `[0.9*min, 1.1*max]` per component over the corpus. Arbitrary 6-tuples are not
  realizable as four points in R³ and would test extrapolation, not the code.
- Env knobs: `N4PES_MAX_EXAMPLES`, `N4PES_SEED` (the run prints the seed it drew;
  set it to replay), `N4PES_SCRATCH`, `N4PES_FD_H`, `N4PES_DRIVER`.

Measured tolerances (all reproducible with the two commands above):

| quantity | measured | in force |
|---|---|---|
| `V(piR)` vs `V(R)`, rel to max(\|V\|,1) | 2.30e-13 (244/1200 images differ in bits) | 2.5e-12 |
| `dVdR(piR)` vs `pi dVdR(R)`, rel to max(‖dVdR‖∞,1) | 8.81e-13 | 1.0e-11 |
| central difference vs `dVdR`, h = 3e-5·max(\|R\|,1) | worst \|fd−g\| is 0.052 of tolerance | `1e-7 + 1e-6·\|g\| + 5e-12·\|V\|/(2h)` |

Permutation invariance is a floating-point statement here, not a bitwise one:
the basis is symmetric but the summation order over the 276 basis functions is
not.

The gradient tolerance carries a third term because the first two do not
describe the error. Two runs failed before it did: a 1000-example run at
h = 1e-6 and a 5000-example run at h = 1e-5, both shrinking to compressed
geometries, both by less than 1.5x, and neither a gradient bug — the residual is
the roundoff noise of `V` amplified by 1/2h. `V` is a 276-term sum with
coefficients up to 1e5 cancelling to ~1e3; its implied relative noise is one ulp
for a typical corpus geometry and ~3000 ulp near the compressed corner of the
envelope, which is exactly where Hypothesis goes. Calibrating on the corpus
alone misses this by an order of magnitude, twice. Full measurements in the
`FD_*` block at the top of `test_n4pes_properties.py`.

## Carry-over

- VAL-4 (concurrency witness) not run — expected to FAIL until OBS-2
  (privatize the six scratch arrays) is fixed; that failure is the point.
- `contract.tolerances` in the spec is still `TBD`. The permutation measurement
  above is a lower bound on any GPU tolerance: a reassociation-invariant port
  cannot be tighter than the reassociation the CPU code already exhibits.
