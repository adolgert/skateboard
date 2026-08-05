# regionharness — spec-driven capture-replay for Fortran regions

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

2. **SESE check (VAL-1)** — `./check_sese.py <region.yaml>`: no goto / early
   return / entry / stop in the anchor or its closure.

3. **Generate + instrument** — `gen_harness.py <region.yaml>` emits the capture
   module, replay driver, `build.sh`, and `apply.sh`; `--patch/--restore/--check`
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

## Carry-over

- VAL-4 (concurrency witness) not run — expected to FAIL until OBS-2
  (privatize the six scratch arrays) is fixed; that failure is the point.
- Hypothesis property layer (plain-file driver `input.txt`/`output.txt`,
  corpus-enveloped strategies, permutation invariance of the PES,
  finite-difference gradient check) — planned in
  `~/.claude/plans/i-have-a-goal-snuggly-wreath.md` Phase 5.
