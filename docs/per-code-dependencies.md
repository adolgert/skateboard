# Where the tooling depends on the current Fortran code

Written 2026-08-28 on `feature/generalize`, before any generalization work.
Purpose: list every place the harness relies on details of the code it was
built against, so that a procedure for onboarding a second code can be
written from it. This is findings only; it proposes no procedure.

**This is a dated snapshot and is left as it was written.** Most of what
it describes has since been replaced; every file path and line number
below is the tree as it stood that day. The closing section, "What was
resolved", says what each finding became and what is still open. Read
that first if you are here to know the state of the harness rather than
the state of the audit.

## Summary

The repository has three tooling layers built against two different codes,
and no per-code configuration layer connecting them:

| layer | code it was built against | how the code is identified |
|---|---|---|
| `demo/` batch pipeline (builder, oracle, capture, orchestrator, agent-runner) | tsunami ch04 (`demo/work/src`) | constants spread across three container images |
| `equivalent/` + `deploy/` + `pi-extension/` gateway/ledger | tsunami ch04, by reusing the `demo/` builder and oracle images | `deploy/seed.py` constants, `deploy/gateway.yaml`, `EQUIVALENT_REGION` |
| `tools/regionharness/` capture-replay for a real region | CoarseAIR `n4pes` (external clone at `codes/CoarseAIR`) | region spec YAML plus hard-coded shell/Python under `cases/n4_umn_pes/` |

Config exists on two axes — per-strategy (`equivalent/strategy/files/*.yaml`,
fully designed) and per-region (`gateway.yaml` region entries and the region
spec YAML, designed once) — but the facts that are genuinely per-code have
no home. They live in Python constants, baked container images, and one
shell script. The closest existing model of a per-code config file is
`tools/fmutate/targets/demo.json`.

The two halves have never met: `equivalent/` has never run against a
CoarseAIR region, and `tools/regionharness` has never produced captures in
the format `demo/oracle` reads.

## 1. Per-code facts and where each one lives

Grouped by the kind of fact, because an onboarding procedure will need to
supply each kind once. Class: (a) a value that could move to a per-code
config; (b) a structural assumption that needs code redesign; (c) prose.

### 1.1 Which source tree, and what "baseline" means

- `deploy/seed.py:28-29` — `BASELINE_DIR = "demo/work"`, `BASELINE_REF = "HEAD"`. The baseline is read with `git ls-tree`/`git show` from *this* repository's HEAD, so the target must be a tracked subdirectory of skateboard. (b)
- `deploy/up.sh:32` — `python3 seed.py state/seed`, no argument for which code. (b)
- `demo/orchestrator/Dockerfile:11` — `COPY work /seed`; one code per image. (a/b)
- `equivalent/gateway/submit.py:70-79,160-161,211-223` — baseline files are read and written as UTF-8 text; a non-UTF-8 source or binary fixture is rejected as `binary`. (b)
- `equivalent/gateway/submit.py:241-252` and `components/build_replay.py:28-30` — only `.f90`/`.F90` files reach the builder; no `.f`, `.F`, `.for`, `.inc`, `.c`, headers, data files, or Makefile. (b)
- `equivalent/strategy/files/*.yaml` `allow_globs: ["src/*.f90", "notes/regions/*.yaml"]` — assumes a flat `src/` with lowercase `.f90`. `fnmatch` is case-sensitive, so `.F90` files are rejected by `submit` despite the comment claiming otherwise. Also: the allow-list is a per-code fact filed in a per-strategy file. (a value, b placement)
- `codes/` is `.gitignore`d (`.gitignore:35`); each subdirectory is an independent clone. `codes/CoarseAIR` is dirty: three modified files and one new file from the regionharness instrumentation, plus a hand edit (`pause` → `continue` in `N4_UMN_PIPNN_PES_Class.F90`) needed just to compile, recorded nowhere. (b)
- `.gitignore:43` ignores `notes/`, so the CoarseAIR region spec `notes/regions/n4_umn_pes.yaml` — the input everything else is generated from — is not under version control, while its generated outputs are. (b)

### 1.2 The build recipe

The harness has no notion of a project build system. It builds with one
compiler command over a hand-listed set of files.

- `demo/builder/stages.py:28` `REPLAY_PAYLOAD = ["mod_params.f90","mod_diff.f90","mod_kernel.f90"]`; `:30` `TSUNAMI_PAYLOAD = [..., "tsunami.f90"]` — fixed, dependency-ordered module lists. (b)
- `stages.py:38-48` — every submitted path is flattened to its basename into `ws/src`; nested directories and basename collisions are lost. (b)
- `stages.py:74-78` — one `nvfortran` invocation, all sources at once, no `-I`/`-L`, no libraries, no Makefile. (b)
- `stages.py:20-23` `PROFILES` — the four legal profile names and their flags, including `-gpu=cc89` (this machine's GPU). `equivalent/components/build_replay.py:39-42` passes the strategy *name* as the profile key, so strategy names must match this dict. (a)
- `stages.py:81-85, 188-190` — the timing executable is literally named `tsunami`. (a)
- `stages.py:194-198` — `MALLOC_TRIM_THRESHOLD_`/`MALLOC_MMAP_THRESHOLD_` tuning justified by this kernel returning arrays by value. (a)
- `demo/capture/generate.sh:26`, `demo/capture/calibrate.py:30-31`, `demo/work/Makefile:14`, `tools/fmutate/targets/demo.json:7-12` — three more copies of the same module list. (a, duplicated)
- Regionharness side: `tools/regionharness/gen_harness.py:708-711` `CA_FLAGS` is CoarseAIR's gfortran release flag string copied by hand; `:746-747` probes `parameters_module.mod` and `libcoarseair.a` to find the build; `:762-763` `-I "$CA/src/PESs"` for `#include "../qct.inc"`; `:790` `-llapack -lblas`. gfortran appears in six places with no `FC` variable. (a values, b compiler)
- `gen_harness.py:198-203` `cmake_root` requires `CMakeLists.txt` plus a `src/` dir; `:840-848` registers the capture module by regex on an `add_sources(` macro; `:855-861` hard-codes `-DCMAKE_BUILD_TYPE=release`, `CMAKE_Fortran_STANDARD_LIBRARIES`, and `make -C build`. Of the nine codes under `codes/`, build systems are CMake (3), fpm (1), plain make (2), a shell script (1), and none (2). (b)
- `tools/regionharness/cases/n4_umn_pes/fcg/make_asm.sh:6-7,11,18,24-25` and `fcg/config_fortrancallgraph.py:6,10-11` — absolute `/home/adolgert/...` paths, CoarseAIR flags, two source dirs, one `.s` file for one module. FortranCallGraph is gfortran-assembler-only. (a values, b single-file)

### 1.3 The region: what may be edited, and its shape

- `demo/orchestrator/orchestrator.py:41` and `demo/agent-runner/app.py:19` — `EDITABLE = "src/mod_kernel.f90"`; `orchestrator.py:152,158-163` restores and rejects on exactly that one path. (b: exactly one editable file)
- `equivalent/components/sese_check.py:76` — `candidate_globs = sorted({analysis["src_file"], spec_path})`. After a passing analyzer verdict the allow-list is exactly one source file plus the spec. A region spanning two files can never be submitted. This is the largest single structural assumption in the gateway layer. (b)
- `sese_check.py:59` — the analyzer must emit `src_file` (singular). `tools/regionharness/check_sese.py:75-81,93` reads one `anchor.file` and one `pst_node` line range. (b)
- `deploy/walkthrough.py:34-40,155,173` — spec template with `file: src/mod_kernel.f90`, `pst_node: "step@34-43"` (disagrees with the live working-copy spec, which says `@34-66` and uses `entry:` not `entry_symbol:`); copies one good/bad kernel file from `docs/examples/`. (a values, b single file and needs recorded good/bad ports)
- `demo/agent-runner/app.py:54-55` — the kernel signature `subroutine step(h, u)` with `real(real32), intent(inout) :: h(:), u(:)` is written into the system prompt; `:118` hard-codes `mod_diff.f90` and `mod_params.f90` as the read-only context files. (b)
- `demo/work/src/mod_params.f90:12-15` — `g, dx, dt, hmean` are compile-time parameters so the kernel takes no scalars, only two arrays. (b)
- Regionharness generator assumptions about the anchor routine (`gen_harness.py`): free-form only, `!` comments (`:45-55, 893-894`); first `module` line is the anchor's module (`:168-174`); entry must be `subroutine <name>(` — a `function` or empty argument list dies (`:177-195`); dummy set must equal live_in-arguments ∪ live_out exactly, so `optional` or unused dummies are errors; exactly one `public ::` before `contains` (`:932`); single `end` in range (`:935`); anchor is a pair of line numbers that go stale after patching — the spec says `@373-420` but the patched file has the routine at 376 (`:144-147`). (b)
- `gen_harness.py:84-89, 264, 412, 456-459` — the type model is INTEGER or `REAL(KIND=8)`; CoarseAIR's `-fdefault-real-8` is frozen in as `wp = 8`; every clobber is assumed REAL. No `real32`, logical, character, complex, derived types. (b)
- `gen_harness.py:91-95, 119-131, 541-555` — compile-time integer bounds only (no assumed-shape, allocatable, runtime extents); plain driver handles rank ≤ 2. (b)
- `gen_harness.py:939` — the patcher forcibly exports module-private scratch via `public ::`; works only because the scratch is module-scope. `tools/regionharness/README.md:62-63`: FCG misses subroutine-local SAVE variables. (b)

### 1.4 Capture and replay format

The demo layer and the regionharness layer use two incompatible formats.

Demo layer (raw float32 streams, names = variable names):
- `demo/capture/replay.f90:17,32-33,38,40-41` — `use mod_kernel, only: step`; grid size from `h_in.bin` size; assumes `h` and `u` have the same length; `call step(h, u)`; writes `h_out.bin`, `u_out.bin`. Invoked as `replay <case_dir>`. (b)
- `demo/capture/mod_capture.f90:5-6,12,31` — rank-1 `real32` only. (b)
- `demo/capture/gen_reference.f90` — the entire program: `set_gaussian` initial condition, exactly 5 cases at evenly spaced steps, capture unit is one call to a 2-argument subroutine. (b)
- `demo/builder/stages.py:125,140-142,153-157,166-172` — `h_in`/`u_in` written, `h_out`/`u_out` read; exactly two inputs and two outputs. (b)
- `demo/oracle/app.py:44,47-52,72-83,95,101` — `<f4` dtype; flat `.bin` per variable; `{"h_in","u_in"}` and `{"h","u"}` hard-coded; an extra output variable is silently dropped. (b)
- `equivalent/gateway/datasets.py:22-23` — `"h_in"`/`"u_in"` hard-coded in the gateway. (b)
- `equivalent/tests/fakes.py:44,55,69`, `tests/gateway/test_golden_path_dispatch.py:42-48` — fakes and fixtures pin the same shape, so the suite cannot detect a dataset-shape change. (b)
- `tools/fmutate/fmutate.py:377,389-390,402` — float32 only. (b)
- `builder/Dockerfile:17` bakes `mod_capture.f90` and `replay.f90` into the image, so the driver contract is fixed at image build. (b)

Regionharness layer (Serialbox, one archive per case per stage):
- `ftgdata/ftg_n4pes_test/rNNNNNN/{input,output}/` — 50 cases, 700 files, 5.1 MB, gitignored. Directory name `ftg_<entry>_test`, round tag `r<N>`, stage names `input`/`output` are generator conventions read by `check_captured.py:36,43-44` and `export_corpus.py:123,131`. (a)
- `gen_harness.py:41-42` — `DATA_DIR_DEFAULT` and `SB_DEFAULT` are absolute `/home/adolgert/...` paths compiled into the generated capture module. (a)
- `gen_harness.py:434` — `ftg_cmp_default_tolerance = 0.0` is compiled into the replay; the spec's `contract.tolerances` (`n4_umn_pes.yaml:171`, value `TBD`) is read by nothing. There is no path from a spec tolerance to the comparison a GPU port needs. (b)
- `export_corpus.py:131-135` — reads raw Serialbox `.dat` at offset 0 with `<f8`/`<i4`, ignoring `ArchiveMetaData`; correct only because each archive holds one savepoint. (b)
- `cases/n4_umn_pes/driver.py:22,44,52,56,63-70` — `NR = 6`, output labels `V`/`dVdR(i)`, and the `input.txt` line layout duplicated from `gen_harness.gen_plain_driver:640-644` with no shared definition. (a/b)
- `docs/architecture.tex:163-165` — "Arrays of numbers are small here, so we send them inline" over HTTP. 700 Serialbox files do not fit that transport. (c, but a real limit)

### 1.5 Reference data, datasets, and the oracle

- `demo/capture/generate.sh:19-20,30-33,59-60,66` — `GRID=100`, `STEPS=5000`; the visible/held-out split is "different Gaussian centre and decay"; the four file names define the trust split; held-out gets all files. `demo/README.md:68` is the only documentation of how reference data is produced. (b)
- `demo/capture/calibrate.py:35-38,42,73,80-102,94-95,105` — tolerances calibrated from gfortran `-O2` vs `-ffast-math` spread (not nvfortran), `h`/`u` in five places, float32 floors, `MARGIN=8`. (a/b)
- `demo/oracle/tolerances.json:26-37` — keys are the variable names `h` and `u`. (a)
- `demo/oracle/Dockerfile:11-13` — captures and tolerances baked into the image; `demo/README.md:48-50` "Tolerances change only by rebuilding its image". One code's oracle per image. (b)
- `demo/oracle/app.py:89` and `equivalent/components/regression.py:32,53` — exactly two datasets named `visible` and `holdout`. `regression.py:45` — one global held-out set per oracle, not per code or region. (a/b)
- `deploy/docker-compose.yml:86` — `../demo/orchestrator/datasets:/datasets:ro`; `deploy/gateway.yaml:9` `datasets_root` has no way to say which code a dataset belongs to. (b)
- `equivalent/components/sanitize.py:28-29` — sanitizes the first case only. (b, mild)
- CoarseAIR captures have no visible/holdout split (`docs/inventory.md:86-94`); all 50 are the regression set (`notes/workflow_example.md:96-101`). (b)
- `tools/regionharness/check_captured.py:49` compares field-name sets only, not shape, type, or count. (b)
- `notes/code-survey/*.md` records per code whether any reference output or test suite exists: none for tsunami, CoarseAIR, SWMM5plus; de-facto golden output for pcsaft-titan; PASS assertions in fusion-physics-suite; a working suite only in thermotwin; CMB_HeatFlow has a known physics bug that a regression suite would certify. (c)

### 1.6 Timing and baselines

- `equivalent/components/timing.py:24` `BASELINE_PROFILE = "cpu_best"`; `:3-6` relies on the builder having built a whole-program timing binary as a side effect of `build_replay`; nothing in `equivalent/` names that binary. (a/b)
- `demo/orchestrator/orchestrator.py:43-49,124` — exactly two baselines (`cpu_best`, `cpu_naive_stdpar`) in the ledger columns. (b)
- `demo/work/src/tsunami.f90:23-27` — `default_tiles=20000`, `num_time_steps=5000` etc. are compile-time; `stages.py:203` runs the binary with no arguments, so the problem size lives in agent-visible source. (b)
- The tiling trick (`demo/README.md:103-106`, `docs/early_trials.tex:117-128`) — one 100-point capture set serves both correctness and a 2M-point timing run because every tile evolves identically under the periodic scheme. Specific to this kernel; `notes/code-survey/tsunami.md:359-362` says so. (b/c)
- `timing.py:39,55` — `repeats=5`, `stages.py:203` 300 s cap, `:146` 120 s per replay case. (a)

### 1.7 Device proof, sanitizers, and strategy files

These are per-compiler and per-machine rather than per-code, but an
onboarding procedure has to touch them.

- `equivalent/strategy/files/stdpar_managed.yaml:19-23`, `omp_target.yaml:18-22` — `-gpu=cc89`; `docs/pi-install.md:39-41` says to edit these for a different GPU. (a)
- `stages.py:107-117` — `NVCOMPILER_ACC_NOTIFY=1`, `OMP_TARGET_OFFLOAD=MANDATORY`, and "ran on GPU" means the substring `launch ` appears in stderr. `experiments/README.md:185-195` records that the `omp_target` rung's device proof was structurally incapable of passing until first exercised — a precedent for what a second code will hit. (b)
- `stages.py:176-179` — `compute-sanitizer` tool list and error regex. (a)
- `equivalent/components/run_replay.py:40` — hint text names `do concurrent`/`omp target`/nvfortran. (a)
- `equivalent/tests/strategy/test_schema.py:14-24` — asserts the strategy YAML equals `stages.py PROFILES` exactly, including `cc89`. (b)
- Strategy comments (`stdpar_managed.yaml:17-18,25-26,33-34`) say the values are pinned to `demo/builder/stages.py`. (c)
- The regionharness layer uses gfortran everywhere and nvfortran nowhere (`notes/serialbox_fortran.md:656-667` notes a two-toolchain Serialbox build would be needed). (b)

### 1.8 Prompt text

- `demo/agent-runner/app.py:46-60` — system prompt: the one editable file, the module name, the exact signature, whole-file replacement protocol. `:71-105` `STRATEGY_CARDS`: `mod_diff::diff_centered`, the u-then-h ordering rule (the tsunami physics contract), `map(tofrom: h, u)`, and compiler flags restated in prose that have drifted from `stages.py:21` (`omp_target` card says no managed memory; the build enables it). (a/b)
- `demo/orchestrator/prompts/` is empty; all prompt text lives in the untrusted container's image.
- `pi-extension/src/` contains no prompt text and no code-specific value; every tool description comes from `GET /table`. (already generic)

### 1.9 Tests, fixtures, golden files

- `equivalent/tests/test_seed.py:18-25` — asserts the seed is exactly the six tsunami files. (b)
- `docs/examples/*.f90` — twelve files, all `mod_kernel.f90` variants; `deploy/walkthrough.py:45,54` and the component-test pattern depend on them. (b)
- `equivalent/tests/cli/golden/status_accepted.txt:1` and roughly forty test files embed `ch04:step`/`mod_kernel`/`h`/`u` (full list in the audit transcript: `tests/components/*`, `tests/gateway/*`, `tests/cli/*`, `tests/ledger/*`). (c, but numerous)
- `pyproject.toml:41` — `testpaths` includes `tools/regionharness`, so `test_n4pes_properties.py` (which needs a built CoarseAIR driver) is collected by plain `pytest`. (a)
- `test_n4pes_properties.py` — permutation invariance, gradient-vs-finite-difference, and `igrad` independence are physics of a 4-atom PES; tolerances at `:75-76,137-140` were measured for this PES; only the determinism test (`:343-355`) transfers. `cases/n4_umn_pes/permutations.py` is the S₄ action on six pair distances. There is no spec key or plugin point for "a symmetry of this region". (b)
- `tools/fmutate/targets/demo.json:19-28` — mutation-tests the gfortran build while the gate uses nvfortran. (b)

### 1.10 Documentation

Every process document is written in terms of tsunami (`docs/pi-users-manual.md`,
`docs/pi-install.md`, `docs/early_trials.tex`, `docs/run_examples.md`,
`docs/coverage-testing.md`, `demo/README.md`, `experiments/README.md`) or
n4pes (`notes/workflow_example.md`, `tools/regionharness/README.md`). The
process-definition statements, as opposed to examples, are:

- `docs/pi-users-manual.md:69-74,120-124,314-318` — the allow-list is "the spec plus the one source file the spec names".
- `docs/pi-users-manual.md:188-201` — `regression_*` and `time_port` presume an oracle image and a timing driver already exist, with no statement of where they come from.
- `docs/pi-install.md:78-80` — the seed is "the six files git tracks under `demo/work`"; `:92-100` the walkthrough is a canned tsunami port.
- `docs/pi-install.md:245-252` "Changing the region or the strategy" — the only prose on adding something new: "A new region is a new entry in `gateway.yaml`… a new strategy is a new YAML file." Silent on baseline, captures, tolerances, holdout, replay harness, timing driver, or a new code.
- `docs/early_trials.tex:102-128,130-183` — the acceptance criterion and the prompt are defined wholly in tsunami quantities.
- `docs/skateboard.tex:92,111,113,115` — explicit scope decisions: plain-array regions only, no multi-code auditability, no portability claim, "the code is the battery, no cross-code generality claim".
- `notes/pi-ledger-architecture.md:218-221` — oracle "captures, tolerances, held-out set baked into the image".
- `notes/workflow_example.md:22-25` — "Get the workload running" is two sentences; `:61-62` states the harness generator "doesn't generalize to other examples yet" and rejects allocatables-with-bounds and `PRESENT`-guarded optionals.
- `notes/ui_questions.md:1-28` — the only code-agnostic statement of the intended ten-step process.
- `notes/code-survey/tsunami.md:394-463` — a 27-item rubric for admitting a second code (region span, call depth, capture volume, non-bitwise reference, real build system, external dependency, documented validation case). Not referenced from any process document, and it never mentions SESE. The survey recommends CoarseAIR's O₃/UMN path, not the N4 region the harness was built on.

Inconsistencies found along the way: `gateway.yaml:15` and the users' manual name `notes/regions/ch04-step.sese.yaml`, which exists only inside a session's working copy; `up.sh:70-87` re-types the region block instead of reading `gateway.yaml`, so edits to `gateway.yaml` do not reach the host copy; `walkthrough.py` and the live spec disagree on `pst_node` and on `entry:` vs `entry_symbol:`; `check_footprint.py:143` documents `-ml` while the README uses `-cf`; `experiments/README.md:36-39` claims an Opus run that `docs/run_examples.md:400-404` shows never happened; `demo/.env:4-5` sets variables no code reads.

## 2. The same fact in many places

An onboarding procedure will have to update each of these in lockstep
until they are consolidated.

1. The dependency-ordered module list — `stages.py:28`, `:30`, `generate.sh:26`, `calibrate.py:30-31`, `work/Makefile:14`, `targets/demo.json:7-12`.
2. The variable names `h`/`u` and files `{h,u}_{in,out}.bin` — `orchestrator.py:105-107`, `stages.py:140,153-157,171`, `oracle/app.py:78-82,95,101`, `tolerances.json:27,32`, `replay.f90`, `gen_reference.f90`, `generate.sh:41-42,59-60`, `calibrate.py` (five places), `datasets.py:22-23`, `fakes.py`, `targets/demo.json:34-38`, prompt `app.py:91`.
3. `src/mod_kernel.f90` as the editable file — `orchestrator.py:41`, `agent-runner/app.py:19,52,56`, `walkthrough.py:29`, users' manual.
4. Compiler flags — `stages.py:20-23` (authoritative), strategy YAMLs (asserted equal by test), prompt cards (drifted).
5. The region block — `gateway.yaml:13-17`, `up.sh:70-87`, `state/gateway.host.yaml`, `.env`, `agent/Dockerfile:43`.
6. Regionharness `input.txt` layout — `gen_harness.py:640-644` and `cases/n4_umn_pes/driver.py:63-70`; case-dir stem — `gen_harness.py:219-221` and `export_corpus.py:123`.
7. Absolute `/home/adolgert/dev/skateboard/...` paths — `gen_harness.py:41-42`, `replay/build.sh:19-25`, `replay/apply.sh:16-21`, `fcg/make_asm.sh:6-7`, `fcg/config_fortrancallgraph.py:6,10-11`.

## 3. Structural assumptions (the class-(b) short list)

These cannot be fixed by adding a config file; each is a design change.

1. Exactly one editable source file per region (`sese_check.py:76`, `orchestrator.py:158-163`, `walkthrough.py:155`).
2. Exactly two inputs and two outputs, named `h` and `u`, rank-1 float32 (builder, oracle, gateway datasets, capture driver, fmutate).
3. The build is one compiler command over a flat, hand-ordered file list; no project build system, no libraries, no includes, no non-`.f90` files (`stages.py`, `build_replay.py`, `submit.py`).
4. The target tree must be a tracked subdirectory of this repo, UTF-8 text only (`seed.py`, `submit.py`).
5. Captures, tolerances, and the held-out set are baked into one oracle image; datasets are deployment-global, not per code.
6. Timing requires a whole-program driver whose problem size is compiled in, and a correctness-to-timing relationship (tiling) that holds only for this kernel.
7. Two capture formats that do not interoperate: raw float32 `.bin` (demo/gateway) versus Serialbox archives (regionharness), with no visible/holdout split on the Serialbox side and no tolerance path on either side for a real region.
8. The regionharness generator assumes: free-form, cpp'd `.F90`, module-resident `subroutine` with parenthesized args, one `public ::`, REAL(8)/INTEGER only, literal bounds, rank ≤ 2, CMake with `add_sources(`, gfortran, module-scope scratch. It patches the external clone in place, which contradicts the gateway's clean-checkout model (`docs/inventory.md:46-57`).
9. The region spec is anchored by line numbers that go stale after instrumentation.
10. Property-based invariants (symmetry, gradient consistency) have no spec key, no predicate type, and no plugin point; they are hand-written Python per region.
11. Device proof is "the substring `launch ` in stderr", tied to nvfortran runtime messages.

## 4. What is already generic

Would work unchanged for a second Fortran code:

- `equivalent/ledger/*` in full; `equivalent/gateway/app.py`, `main.py`, `table.py`, `regions.py`; the git plumbing in `submit.py` (subject to UTF-8); `config.py`'s loader (not its key list); `equivalent/client.py` and `cli/*`; `strategy/schema.py`.
- `pi-extension/src/` in full.
- `deploy/` network isolation, `up.sh` state/health logic, `down.sh`, `pi.sh`, `isolation_check*.sh`, `gateway/Dockerfile`.
- `demo/docker-compose.yml` topology; the builder/oracle HTTP contracts in shape; `demo/builder/app.py`; `demo/oracle/compare.py` (float32 aside); the oracle's trust properties; the orchestrator's gate sequence and ledger spine; `stages.py`'s workspace machinery.
- `tools/fmutate/fmutate.py` — retargeting is a new JSON file for any code with a `driver <case_dir>` replay binary and float32 outputs; `targets/demo.json` is the best existing template for a per-code config.
- `tools/regionharness/check_sese.py`, `check_captured.py`, `check_footprint.py` (modulo the `src:` string convention and the FCG parser); the spec-to-Fortran generation core of `gen_harness.py` (`Field`, `Region`, `gen_capture_mod`, `gen_driver`, `gen_plain_driver`); the marker-patch mechanism; the `FTG_CAPTURE_*` runtime contract; the five-gate design (SESE, footprint agreement, captured-set-equals-spec, bitwise replay, signaling-NaN poison); the `cases/<region>/` plugin layout (the seam is right, just reached by a literal path).
- `notes/specifying_regions.md` (the region-spec theory) and `notes/ui_questions.md` (the process) name no code.

## 5. Existing seeds of a per-code configuration

Four places already externalize part of what a per-code layer needs:

| file | what it captures | what it lacks |
|---|---|---|
| `tools/fmutate/targets/demo.json` | source dir, file list, driver exe and args, flag sets, corpus layout, input files, output variable→file map, comparator, tolerances | not read by anything but fmutate |
| `notes/regions/<region>.yaml` | anchor file/routine/lines, live_in/live_out/clobbers with extents and types, closure, contract | tolerances unread; not in git; ~40 lines are read by tools, ~250 are prose |
| `equivalent/strategy/files/*.yaml` | compiler, flags, device proof, sanitizers, analyzer command, allow_globs | allow_globs is per-code, misfiled here |
| `deploy/gateway.yaml` regions | spec path, strategy, visible dataset name | no code identity, tree, build, variables, tolerances, timing target, baseline |

## 6. What was resolved

Added after the generalization work. One line per finding: what replaced
it, or that nothing did.

### The structural assumptions of §3

1. **One editable file per region.** The region spec lists `files:` --
   every path the region may edit, including paths that do not exist
   yet. The analyzer returns that list and the allow-list is it plus the
   spec, each path checked against the strategy's globs.
2. **Two rank-1 float32 variables called `h` and `u`.** The code's
   manifest declares each of the region's inputs and outputs by name,
   element type (`f32 f64 i32 i64 l`) and rank (0 to 4). Captures are
   one `.npy` file per variable, which says its own type, shape and
   element order, and the oracle compares whatever variables a case
   declares it holds. No Python file outside a code's own directory
   names a variable.
3. **One compiler command over a flat, hand-ordered file list.** The
   builder runs the tree's own makefile, with `FC` set to a shim that
   logs every compiler invocation before running the strategy's
   compiler. The log is read back: every compile must carry the
   strategy's flags and must compile only the tree's own source, and
   both facts go into the build claim. `make`, `cmake` and `fpm` are in
   the builder image, so a thin makefile can drive either of the other
   two.
4. **A tracked subdirectory of this repository, UTF-8 text only.** The
   UTF-8 half is gone: submissions carry file content as bytes from git
   to disk, so a namelist or a small data file in another encoding
   round-trips. The tracked-subdirectory half remains -- see below.
5. **One oracle image, deployment-global datasets.** Datasets, captures
   and the tolerance policy are per code, under `programs/<code>/`, and
   the oracle bakes one code's whole directory in as a build argument.
   One image per code remains -- see below.
6. **A compiled-in problem size and the tiling trick.** The manifest
   names the timing executable, its arguments, its environment, the
   files it writes and a budget, so the problem size is data. Timing is
   no longer justified by a correctness argument specific to one kernel:
   `program_regression` runs the ported program at that size and
   compares every file it writes against the files the baseline program
   wrote, and `time_port` cannot run without it.
7. **Two capture formats with no tolerance path.** One format: a
   directory per case, one `.npy` per variable (`<name>.npy` in,
   `<name>.out.npy` out), and a `case.json` naming them. One comparator
   file, which the oracle image, the builder image and the gateway all
   use, so there is one definition of "the same answer". The tolerance
   policy is a per-code file with a band per region output variable and
   a band per file the timing run writes.
8. **The region-harness generator's assumptions.** Not addressed, by
   intent: nothing in `tools/regionharness/` was extended. Its
   `check_sese.py` moved into the package as the analyzer the gateway
   runs; the generator is not on the gateway path. What replaced it is
   that the replay driver and the capture program are written during
   onboarding against a stated contract, and checked by
   `harness_replay`, `harness_determinism` and `harness_self_check`
   rather than generated.
9. **Line-numbered anchors that go stale.** Not resolved. A spec still
   names a line range, and the analyzer's verdict is only as good as the
   range it was given -- which the manual says. The analyzer's claim is
   filed against the frozen set rather than the tree, so it survives
   edits inside the region.
10. **Property-based invariants have no home.** A code's manifest may
    name a pytest module of invariants; `regression/property` is a
    predicate type; the builder bakes in the library that module imports
    and runs it against the replay binary; and acceptance requires the
    claim exactly when the code declares a module.
11. **Device proof is the substring `launch `.** The proof now requires
    the offload runtime's own `file`, `function`, `line` and `device`
    fields on each launch line, and the claim records where each launch
    came from. A stronger proof remains open -- see below.

### The duplicated facts of §2

1. **The dependency-ordered module list**, in six places: gone. The
   tree's own makefile is the only list, and the manifest names the
   targets it builds and the executables they leave.
2. **`h`/`u` and the four `.bin` names**, in a dozen places: gone. Names
   come from each case's own `case.json` and types and ranks from the
   manifest.
3. **`src/mod_kernel.f90` as the editable file**: replaced by the spec's
   `files:` list, capped by the strategy's globs.
4. **Compiler flags in three places**: the builder's profile table is
   deleted and the strategy file is the only place they are written. The
   prompt cards that had drifted from it went with the agent-runner.
5. **The region block, retyped in five places**: the deployment's
   configuration is one file per code, and the host's copy is produced
   from it by rewriting only the paths. The region is no longer baked
   into the session image.
6. **The region harness's `input.txt` layout and case-dir stem**: not
   addressed; that layer was not extended.
7. **Absolute `/home/adolgert/...` paths in the region harness**: not
   addressed, for the same reason.

### What remains

- **A baseline has to be a tracked directory in this repository.**
  `seed.py` reads it out of a commit. A code kept in its own upstream
  clone would need a clone-and-pin step, and the ledger key would have
  to include the upstream commit.
- **One oracle image holds one code, and a deployment holds one code.**
  The repository is seeded from one baseline and the oracle bakes one
  code's answers in, so two codes mean two deployments. A multi-code
  oracle can come later.
- **The static footprint check is still missing.** Nothing confirms that
  a spec's declared reads and writes match what the code actually
  touches; that needs static-analysis tooling this repository does not
  run generically. `harness_replay` and `harness_self_check` catch a
  wrong footprint empirically -- a driver that does not set everything
  the region reads shows up as a replay that does not reproduce the
  capture -- but the static check is open.
- **Requirements are scoped to the whole tree.** Any edit invalidates
  every check, which is right for a small kernel and expensive on a
  large code, where a region-scoped subject (a hash of the region's
  dependency cone) would let untouched evidence stand.
- **A stronger device proof.** Counting kernels with `nsys` rather than
  reading the runtime's notification lines is a strategy option nobody
  has written.
