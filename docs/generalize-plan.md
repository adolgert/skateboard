# Plan: make the gateway path work for a second Fortran code

This plan follows `docs/per-code-dependencies.md`, which lists where the
harness depends on the tsunami code. It generalizes the gateway path —
`equivalent/`, `deploy/`, and the two `demo/` services the gateway calls
(builder and oracle) — so that a new Fortran code can be brought in by a
person working with an AI, where every onboarding step has a check the
AI can run to know it succeeded. `tools/regionharness/` is not extended;
only its `check_sese.py` is kept, because the gateway calls it.

It is executed the same way as `notes/pi-ledger-plan.md`: Establish,
Decide, Tests first, Build, Review, one step per conversation pair, code
sized to be read in a sitting. The conventions for code a person will
edit later are unchanged.

## Decisions already made

From the discussion on 2026-08-28.

- **G1.** The second code goes through the gateway path. The demo
  orchestrator and agent-runner are not part of that path and are not
  generalized.
- **G2.** `tools/regionharness/` is older work. Nothing in it is
  extended. `check_sese.py` moves into the package because the gateway
  depends on it.
- **G3.** One oracle image holding one code's captures is acceptable for
  now. A multi-code oracle can come later.
- **G4.** The baseline stays a tracked directory inside this repository,
  read out of a commit by `seed.py`. External clones are not supported
  yet.
- **G5.** These limitations are removed by this plan: one editable file
  per region; two named float32 arrays; no project build system; timing
  that depends on the tiling trick; a capture format with no path for
  tolerances on a real region; no slot for property-based invariants.
- **G6.** A person may ask an AI to adapt a code's build to the
  builder's contract. Every such adaptation has a machine-checked goal so
  the AI can tell when it is done, and the person reviews what passed
  before it becomes trusted.

## The shape of the change

Today the harness has a per-strategy file and a per-region spec. It gets
a third file, the **code manifest**: one hashed YAML per code that names
the source tree, the build contract, the region interface (variables,
types, ranks), the datasets, the timing run, and the tolerance policy.
The manifest is a subject kind like the strategy is, appears in every
claim's `materials`, and is frozen during a porting session.

Onboarding a code is itself a gateway session. The AI has the same
`submit` and `status` tools it has when porting, plus a set of onboarding
actions, each of which files a claim. The `onboarded` row in the action
table lists what a finished onboarding needs, the same way `accept`
lists what a finished port needs. When every claim is present the person
reviews the manifest, driver, and tolerances that passed and promotes
them — that is the only step that is not a gateway action.

```
onboarding session (AI + person)                 porting session (AI)
  submit  -> manifest_check                        submit -> sese_check
          -> harness_build                                -> build_replay
          -> harness_capture                              -> run_replay
          -> harness_replay                               -> sanitize
          -> harness_determinism                          -> regression_visible
          -> harness_self_check                           -> regression_holdout
          -> harness_timing                               -> program_regression
          -> harness_property (optional)                  -> property_check
  status: ONBOARDED                                       -> time_port
  person promotes manifest + datasets                status: ACCEPTED
```

## New decisions this plan proposes

Each is a Decide item in a step below; the recommendation is stated here
so the steps can refer to it by number.

- **D16. Code manifest.** A file `programs/<code>/manifest.yaml`, loaded
  strictly like the strategy file, hashed, subject kind `manifest`.
  Contents: `source` (root, file patterns), `build` (contract, targets,
  executables), `interface` (region entry, inputs, outputs, each with
  name, dtype, rank), `datasets` (visible, holdout, program), `timing`
  (executable, arguments, output files, budget), `tolerances` (path).
- **D17. Build contract.** The builder runs `make -f <makefile> <target>`
  in the tree with `FC`, `FFLAGS`, and `LDFLAGS` set from the strategy,
  where `FC` is a logging shim that records every compiler invocation
  and then executes the strategy's compiler. Required targets: `replay`,
  `timing`, and during onboarding `capture`. The shim log is how the
  builder proves that the strategy's flags reached every compile and
  that only files from the submitted tree were compiled. A code with
  CMake or fpm gets a thin Makefile that drives it; the AI writes that
  Makefile during onboarding and `harness_build` checks it.
- **D18. Capture format.** One directory per case, one `.npy` file per
  variable, named `<variable>.npy` for inputs and `<variable>.out.npy`
  for outputs, plus `case.json` listing the variables. NPY is
  self-describing (dtype, shape, Fortran order), numpy reads it natively,
  and a Fortran writer is about forty lines. The oracle and the datasets
  loader read the variable list from the files, never from Python.
- **D19. Replay driver.** Written by the AI during onboarding, in the
  tree, to a fixed contract: `replay <case_dir>` reads every declared
  input, calls the region entry once, writes every declared output. It is
  frozen during porting. There is no driver generator.
- **D20. Captures come from the code's own run.** The manifest's
  `capture` target builds a program that runs the code's real setup and
  dumps the region's inputs and outputs at the call site, taking its
  case parameters from the command line. Visible and held-out sets are
  two parameter sets named in the manifest. The AI writes this program;
  `harness_capture` and `harness_replay` check it.
- **D21. Timing at a declared size, checked by program-level
  regression.** The manifest names the timing executable, its arguments,
  and the output files it writes. Onboarding records those outputs from
  the baseline as the `program` dataset. A port passes
  `program/regression` when its timing run's outputs match under the
  tolerance policy. This replaces the tiling trick: correctness at the
  timing size is checked directly.
- **D22. Property predicate.** The manifest may name a pytest module of
  properties over the replay binary. `property_check` runs it and files
  `regression/property`. It is optional per code and required for
  acceptance only when the manifest names one.
- **D23. Multi-file regions.** The region spec lists `files:` (the
  anchor plus any other paths the region may edit, including paths that
  do not exist yet). The analyzer returns `src_files`; the allow-list is
  that list plus the spec. Every path must be within the strategy's
  `allow_globs`.
- **D24. Onboarding trust.** During onboarding the builder executes a
  Makefile the AI wrote. The builder has no route off the host and the
  claims are filed against the manifest hash, so what the person
  promotes is exactly what passed. Porting sessions never run anything
  from the tree except through the promoted manifest.

---

## Step 0 — To-fix list

Small, mechanical, found during the audit. One pull request, before any
design work, so the later steps start from a consistent tree.

1. `.gitignore:43` ignores `notes/`, so region specs and both plans are
   untracked. Decide what under `notes/` should be tracked; move region
   specs to a tracked directory.
2. `deploy/up.sh:70-87` re-types the region block instead of deriving
   `gateway.host.yaml` from `gateway.yaml`. Replace the heredoc with a
   small Python step that rewrites only the `paths:` values.
3. `equivalent/strategy/schema.py:67` — `fnmatch` is case-sensitive, so
   `src/*.f90` rejects `.F90` on submit while `fortran_files_at` accepts
   it. Match case-insensitively on the extension, and fix the comment in
   both strategy files that claims otherwise.
4. `deploy/walkthrough.py:34-40` writes a spec with `entry_symbol:` and
   `step@34-43`; the live working-copy spec uses `entry:` and `@34-66`.
   Check in one reference spec for tsunami and have the walkthrough copy
   it.
5. `deploy/gateway.yaml:15` names `notes/regions/ch04-step.sese.yaml`,
   which exists only inside a session. Covered by item 4.
6. `deploy/agent/Dockerfile:43` bakes `EQUIVALENT_REGION`; compose
   already sets it. Remove the `ENV`.
7. `pyproject.toml:41` puts `tools/regionharness` in `testpaths`, so
   `test_n4pes_properties.py` is collected by plain `pytest`. Remove it;
   keep the `check_sese --json` test with the package (Step 4 moves the
   script).
8. Device proof (`demo/builder/stages.py:114-117`) counts the substring
   `launch ` in stderr. A Fortran program can print that line itself.
   Match the full `NVCOMPILER_ACC_NOTIFY` line format, and record in the
   claim which lines matched. A stronger proof (nsys kernel count) is a
   later strategy option.
9. `equivalent/components/sanitize.py:28-29` sanitizes the first case
   only. Make the case list a strategy field (`sanitize_cases: all | first`).
10. Strategy `required_tools` is never checked. Have the gateway compare
    it against the builder's `/healthz` at startup.
11. `equivalent/gateway/submit.py:70-79,211-223` reads and writes tracked
    files as UTF-8 text and rejects everything else. Carry bytes; a real
    code has namelists and small data files with other encodings.
12. `experiments/README.md:36-39` claims an Opus campaign that
    `docs/run_examples.md:400-404` shows never ran. Correct the README.
13. `demo/agent-runner/app.py:84` prompt card disagrees with
    `stages.py:21` on `mem:managed`. The agent-runner is superseded by
    `pi`; decide in Step 1 whether `demo/orchestrator` and
    `demo/agent-runner` are deleted or moved to an `archive/` directory
    with `experiments/`.

**Tests.** Each item that changes code gets a test in the module's
existing test file. Items 1, 5, 12, 13 are review only.

**Shown that week.** `pytest` passes without a CoarseAIR checkout;
`up.sh` produces a host config that matches `gateway.yaml`.

---

## Step 1 — Repository layout for codes, and the code manifest

**Goal.** One place per code, and one file that says what the code is.

**Establish.** Confirm what `deploy/seed.py`, `deploy/docker-compose.yml`
(`datasets` mount, builder and oracle build contexts), `demo/oracle/
Dockerfile`, and `equivalent/gateway/config.py` currently read from where.
Confirm which of `demo/`'s files the gateway path actually uses: builder
(`app.py`, `stages.py`, `Dockerfile`), oracle (`app.py`, `compare.py`,
`Dockerfile`, `captures/`, `tolerances.json`), and the two capture
harness files the builder image bakes.

**Build.**

- A directory `programs/<code>/` holding `manifest.yaml`, `baseline/`
  (the tracked source tree, G4), `tolerances.json`, `datasets/visible/`,
  `captures/{visible,holdout,program}/`, and optionally `properties/`.
  Tsunami moves from `demo/work` to `programs/tsunami/baseline`.
- `equivalent/manifest/schema.py`: a strict loader like
  `strategy/schema.py`, returning a frozen dataclass with `sha256`, and
  `as_subject()` with a new subject kind `manifest`.
- `gateway.yaml` gains a `codes:` section (`<code>: {manifest: path}`)
  and each region names its `code`. `RegionConfig` carries the manifest.
- `seed.py` takes the baseline directory from the manifest of the code
  named on the command line, not from a constant.
- The builder and oracle move out of `demo/` to `services/builder` and
  `services/oracle` (or stay and `demo/` is trimmed — Decide). Their
  images are built with `--build-arg CODE=<code>` so the oracle bakes
  `programs/<code>/captures` and `tolerances.json`.
- Every claim's `materials` gains the manifest subject.

**Decide.** The directory name (`programs/` is proposed; `subjects/`
collides with ledger subjects, `targets/` with fmutate). Whether
`demo/orchestrator`, `demo/agent-runner`, `demo/capture`, and
`docs/examples` are deleted, archived, or left. Whether the manifest's
`source.patterns` replaces `fortran_files_at`'s hard-coded extension
test (proposed: yes; the default pattern list includes `.f90 .F90 .f08
.f03 .f .F .for .inc`).

**Tests.**

- A manifest missing a required field, naming a path not in the tree, or
  with an unknown key fails to load with the field named.
- Loading the tsunami manifest yields the same baseline file list
  `test_seed.py` asserts today.
- A gateway config whose region names a code with no manifest fails at
  startup.
- A claim filed through `/run` carries the manifest subject in
  `materials`, and `ledger status` shows it.

**Shown that week.** The tsunami port still reaches `ACCEPTED` through
the walkthrough, now with `programs/tsunami/manifest.yaml` in every
claim's materials.

---

## Step 2 — Capture format and a variable-agnostic oracle

**Goal.** Remove `h`, `u`, float32, and rank-1 from every Python file.

**Establish.** Read `demo/oracle/app.py`, `compare.py`, `equivalent/
gateway/datasets.py`, `demo/builder/stages.py` (`run`, `sanitize`), and
the wire shapes in `equivalent/gateway/backend_client.py`. List every
place a variable name or dtype is written down (the audit found fifteen).

**Build.**

- `equivalent/capture/npy.py`: read and write NPY, including
  Fortran-order and every dtype the manifest may declare (`f32 f64 i32
  i64 l`). `case.json` lists inputs and outputs by name.
- A Fortran module `harness/npy_io.f90` (baked into the builder image)
  that writes and reads NPY for rank 1–4 real32/real64/int32/logical,
  generated once from a template rather than hand-written per rank.
- Wire shape for `/v1/run`, `/v1/sanitize`, `/v1/compare`: a case is
  `{name: {variable: {dtype, shape, order, b64}}}`. The oracle compares
  whatever variables the expected case holds and fails if any is
  missing from the submission — never silently drops one.
- `compare.py` handles float64 (ULP in int64), integers (exact), and
  logicals (exact). Tolerances are keyed by variable name and validated
  against the manifest's output list at oracle startup.
- `datasets.py` reads the variable list from `case.json`.
- A converter that rewrites tsunami's existing `.bin` captures to NPY,
  so the reference datasets are not regenerated with a different
  compiler.

**Decide.** Whether NPY or raw stream plus JSON sidecar (proposed: NPY).
Whether the oracle also holds the `program` dataset from D21 (proposed:
yes, same comparator, per-file instead of per-variable).

**Tests.**

- Round trip of every dtype and rank through the Fortran writer and the
  numpy reader, with a non-trivial shape, preserves values and order.
- The oracle fails a submission that omits a declared output, and names
  it.
- A float64 case with a one-ULP difference passes under `ulp: 1` and
  fails under `ulp: 0`.
- The converted tsunami captures compare equal to the originals.

**Shown that week.** The tsunami port reaches `ACCEPTED` with NPY
captures and no variable name in any Python file.

---

## Step 3 — The build contract

**Goal.** The builder builds any code through one contract, and proves
the strategy's flags were used.

**Establish.** Read `stages.py` `build`, `run`, `time_run`; the builder
image; the strategy schema's `languages` and `link_flags`. Try the
contract by hand on tsunami and on the candidate second code (Step 8)
before designing the shim.

**Build.**

- `harness/fc-shim`: a script installed as the `FC` the Makefile sees.
  It appends one JSON line per invocation (argv, cwd, input files,
  output file) to a log in the workspace and executes the strategy's
  compiler with the same arguments.
- `stages.build` runs `make -f <manifest.build.makefile> <targets>` with
  `FC`, `FFLAGS`, `LDFLAGS` from the strategy, in a workspace holding the
  submitted tree with its directory structure intact (no basename
  flattening). After the build it reads the shim log and returns: the
  list of compiled files, whether every compile carried the strategy's
  flags, whether any compiled file was outside the tree, and the
  `-Minfo` excerpt.
- `build_replay` files `build/replay` as today with that detail; a
  compile that lacked the flags or reached outside the tree is a `fail`,
  not an error.
- `stages.run` and `stages.sanitize` invoke the manifest's replay
  executable as `replay <case_dir>` on NPY cases. `stages.time_run`
  invokes the timing executable with the manifest's arguments and
  environment and collects the declared output files.
- `PROFILES`, `REPLAY_PAYLOAD`, `TSUNAMI_PAYLOAD`, `HARNESS`'s baked
  driver, and the `MALLOC_*` tuning are deleted. The `cpu_best` baseline
  becomes a strategy file (`cpu_reference.yaml`) like any other.
- The builder image adds `cmake` and `fpm` so a thin Makefile can drive
  either.
- tsunami gets a `Makefile` with `replay`, `timing`, and `capture`
  targets; its `gen_reference.f90` becomes the `capture` target.

**Decide.** Whether the Makefile is frozen during porting (proposed:
frozen by default; a region spec may list it under `files:` and the
shim check is what makes that safe). Whether a build may run a
configure step (`cmake -S . -B build`) as part of the `replay` target
(proposed: yes, inside the tree, recorded in the log). Whether the
timing run's problem size is an argument or an environment variable
(proposed: argument list in the manifest).

**Tests.**

- A Makefile that hard-codes `FFLAGS` produces a `build/replay` fail
  whose detail names the compile line that lacked the strategy flags.
- A Makefile that compiles a file outside the tree produces a fail
  naming the file.
- A tree with a source file in a subdirectory builds; two files with
  the same basename in different directories build.
- Time, run, and sanitize each invoke the executable named in the
  manifest, not a fixed name.
- The tsunami Makefile builds all three targets under `stdpar_managed`,
  `omp_target`, and `cpu_reference`.

**Shown that week.** `build/replay` detail lists every compile line and
its flags; `ledger status` shows them.

---

## Step 4 — Multi-file regions and the analyzer's new home

**Goal.** A region may span several files and may create a file; the
analyzer lives in the package.

**Establish.** Read `check_sese.py`, `components/sese_check.py`, the
region spec fields the analyzer reads, and how `app.py` recomputes the
frozen subject after a pass.

**Build.**

- Move `tools/regionharness/check_sese.py` to
  `equivalent/analyzers/check_sese.py`; drop the `COPY tools/regionharness`
  and the editable install from the gateway image. Strategy
  `analyzer_command` becomes `python -m equivalent.analyzers.check_sese`.
- The spec gains `files:` — every path the region may edit. Paths need
  not exist. The anchor's file must be listed. Closure entries carry a
  `file` so callees in other files are scanned.
- The analyzer returns `src_files`. `sese_check` builds the allow-list
  from it plus the spec and checks each against `allow_globs`.
- `walkthrough.py` and the manual describe `files:`.

**Decide.** Whether a file the spec lists but the strategy does not
allow is a `fail` (proposed) or a refusal. Whether creating a new file
requires a Makefile wildcard rule or a Makefile edit (Step 3's decision
covers this).

**Tests.**

- A spec listing two files produces an allow-list of three paths.
- A spec listing a path outside `allow_globs` fails and names it.
- A submission that edits a listed-but-not-yet-existing file commits it.
- A callee in a second file with a `goto` fails the SESE check.

**Shown that week.** A tsunami region spec that lists `mod_diff.f90` too,
and a port that inlines the stencil into a new file.

---

## Step 5 — Onboarding actions and the `onboarded` row

**Goal.** The verifiable goals of G6: every onboarding step is a gateway
action that files a claim, and `status` says what is missing.

*Settled during execution (2026-08-28), after Steps 0–4 were built.* The
step is done in three parts, and one of its checks moves after Step 7.
The decisions:

- **A manifest has a minimal form.** `version`, `name`, and `source` are
  enough to seed a baseline and start an onboarding region; the rest
  (`build`, `interface`, `datasets`, `timing`, `tolerances`,
  `properties`) is what onboarding produces. A porting region refuses to
  start on a manifest that lacks them. Every path in the manifest other
  than `source.root` is relative to the *source tree root*, so the same
  file reads the same inside the tree and beside it.
- **During onboarding the manifest lives in the tree** at
  `harness/manifest.yaml`, written by the AI, with `source.root: .`.
  `promote` copies it out to `programs/<code>/manifest.yaml` with
  `source.root: baseline`, and the promoted tree (minus that file)
  becomes the new baseline. Tolerances and properties live under
  `harness/` in the tree too.
- **Tolerances are the AI's proposal, not the harness's calibration.**
  The harness checks that every floating-point output has a band and,
  in `harness_self_check`, that no mutant survives inside the band. How
  the AI arrives at the numbers (running two flag sets locally, say) is
  its business and the person's review. No calibration profiles in the
  manifest.
- **Timing outputs are `.npy` files.** A code whose program writes text
  gets a timing driver that writes arrays; program-level regression then
  uses the same comparator as the region's outputs.
- **The onboarding region's allow-list is the strategy's `allow_globs`**
  (`onboarding.yaml` allows the whole tree); no SESE claim is involved.
  `phase: onboarding | porting` is a region config field; `/table` and
  `status` serve the rows for the region's phase; `spec_path` is not
  required for an onboarding region.
- **The oracle image copies the whole `programs/<code>/` directory** and
  starts without captures (answering `/v1/compare` with an error naming
  the missing dataset) so a deployment can be brought up for onboarding
  before any capture exists.
- **`harness_self_check` waits for Step 7's runner** and is built with
  `harness_property` as part 5b, after Steps 6 and 7. Mutation and
  comparison run inside the builder (the comparator is baked in beside
  the shim); the builder returns per-mutant verdicts, not outputs.

**5a-i — phase, rows, minimal manifest, the first two checks.**
`phase` in region config; `onboarding.yaml`; `/table?region=` and the
extension passing its region; the `onboarded` acceptance row beside
`accept`, chosen by phase in the gateway and the CLI; predicate types
`manifest/valid`, `harness/builds`, `harness/captured`, `harness/replays`,
`harness/deterministic`, `harness/times`; components `manifest_check`
(gateway-side: loads `harness/manifest.yaml` from the materialized tree,
checks every named path exists, every float output has a band in the
tree's tolerance file) and `harness_build` (both the baseline strategy
and the region's port strategy build every declared target with the
flags proven).

**5a-ii — captures, replay, determinism, timing.** A builder endpoint
that runs the `capture` executable with a dataset's arguments and
returns the dataset it wrote; `harness_capture` validates every case
against the interface, requires the visible and held-out inputs to
differ, and stores both sets as ledger artifacts under a `capture_set`
subject; `harness_replay` runs the baseline-strategy replay on the
captured inputs and requires bitwise agreement with the captured
outputs; `harness_determinism` repeats capture and replay and requires
bitwise agreement with the stored artifacts; `harness_timing` runs the
timing target twice within budget and stores its declared outputs as the
`program` capture set, requiring the two runs to agree bitwise.

**5a-iii — `promote`, and tsunami onboarded from a bare baseline.** The
CLI command, refusing unless the current tree is `ONBOARDED` and the
working copy matches it; then a `pi` session that onboards tsunami from
`programs/tsunami/baseline` with the manifest reduced to its minimal
form, reaching `ONBOARDED` with the person doing nothing but reading
`status`; `promote` reproduces the checked-in tsunami manifest and
datasets.

**5b — after Step 7.** `harness_self_check` (mutation in the builder,
tolerance-blind gap zero, survivors reported) and `harness_property`.

**Tests.** As listed for each component in the original text below, plus:
a minimal manifest loads and a porting region refuses it; `/table` for
an onboarding region lists the onboarding rows only; `promote` refuses
when the working copy differs from the passing tree.

**Shown that week.** A `pi` session onboards tsunami from a bare
`programs/tsunami/baseline` to `ONBOARDED`.

---

## Step 6 — Program-level regression and timing without the tiling trick

*Settled during execution (2026-08-28).* The program dataset is not
checked in and not baked into the oracle. A promoted tsunami program
output is 8 MB per promotion, and every code at a real timing size is
the same order, so the reference for program-level regression is the
deployment's own `time_baseline` run: that claim stores the baseline
program's declared outputs as a capture set in the ledger, and
`program_regression` compares the port's run against it with the code's
tolerance policy. The comparator moves to `equivalent/capture/compare.py`
and the oracle image copies that one file, so there is one comparator.
`harness_timing` still stores the onboarding run's program set in the
onboarding ledger as the evidence the timing target works; `promote` no
longer writes `captures/program`.

*Found during execution.* A whole-program run drifts far more than a
single region call: two CPU compilations of the unported tsunami disagree
by 3.6e-06 absolute after 5000 float32 steps at the timing size, where
the single-call spread was exactly zero. So the tolerance file gains a
`files:` section, one band per file the timing run writes, calibrated
the same way as the region bands, and `program_regression` reads that
section rather than `variables:`.


**Goal.** D21: correctness at the timing size is checked, and the timing
problem size is data, not source.

**Establish.** Read `components/timing.py`, `stages.time_run`, the
`time_baseline` row, and how the acceptance list is consumed by both the
gateway and the CLI.

**Build.**

- `program_regression` component and `program/regression` predicate:
  runs the timing executable under the port strategy, sends its declared
  output files to the oracle's `program` dataset comparison.
- `time_port` requires `program/regression` on the tree; `accept`
  requires both.
- `time_baseline` builds under `cpu_reference` from the manifest and
  records the same output files, which is what `harness_timing` already
  stored — so it reuses that artifact rather than rerunning when the
  manifest hash matches.
- Timing detail records the arguments and environment used.

**Decide.** Whether a timing run whose outputs fail regression still
records a `timing/port` claim (proposed: no; the row's `requires`
prevents it). Whether the tolerance policy for program outputs is the
same file as the region policy (proposed: same file, a `files:` section
beside `variables:`).

**Tests.**

- A port that is fast and wrong at the timing size fails
  `program/regression` and cannot file `timing/port`.
- `time_baseline` on a manifest whose `harness/times` artifact exists
  does not rebuild.
- Tsunami's `default_tiles` moves from `tsunami.f90` to the manifest's
  timing arguments, and the timing claim's `runs_s` matches the earlier
  campaign within noise.

**Shown that week.** `ledger status` showing `program/regression` beside
`timing/port` for the tsunami port.

---

## Step 7 — Property-based invariants as a predicate

**Goal.** D22: a per-code property module has a slot in the table.

**Establish.** Read `tools/regionharness/test_n4pes_properties.py` for
what generalizes (the seed and scratch-directory handling, the
determinism property) and what does not (everything about a potential
energy surface). Read `tools/fmutate/fmutate.py`'s `_replay_case` for
the driver-invocation helper the properties should share.

**Build.**

- `equivalent/properties/runner.py`: invokes `pytest` on the manifest's
  properties module with the replay executable, the visible cases
  directory, and a seed passed by environment, inside the builder;
  returns pass/fail with the failing example minimized by Hypothesis.
- `property_check` component; `regression/property` predicate; the
  `accept` row requires it when the manifest declares properties.
- A small library the properties module imports: `run_replay(inputs) ->
  outputs` over NPY cases, and `corpus()` yielding the visible cases.
- A properties module for tsunami: mass conservation of `h` per step,
  translation invariance under periodic shift, and determinism.

**Decide.** Whether properties run in the builder (they execute the
replay binary, so yes) or the gateway. How many examples by default.

**Tests.**

- A property that fails yields a claim whose detail holds the minimized
  example.
- A manifest with no properties module makes `accept` not require
  `regression/property`, and `status` says so.
- The tsunami mass-conservation property fails on a mutant that drops
  a flux term.

**Shown that week.** A port that passes capture-replay but breaks mass
conservation is refused acceptance.

---

## Step 8 — The second code, end to end

**Goal.** Onboard a code that is not tsunami through Step 5's session,
then port a region of it to acceptance.

**Establish.** Pick the code (Decide). Read its survey entry in
`notes/code-survey/`. Build it by hand under `cpu_reference` and under
`stdpar_managed` to learn what the Makefile must do. Write the findings
note as the first draft of `docs/onboarding.md`'s "before you start"
section.

**Decide.** Which code. The survey's recommendation is CoarseAIR as the
primary companion, but it is CMake, LAPACK, 63k lines, and its
recommended region sits behind polymorphic dispatch — every hard thing
at once. Proposed: first `fusion-physics-suite`'s `hydro/kh2d.f90`
(compiles as-is, deterministic, 2-D real arrays, serial reductions that
force real tolerances, 512² runs long enough to time), then CoarseAIR's
O₃ UMN PES path as Step 8b once the contract has survived one real
code. Whether the whole code or only the files the port needs are
vendored under `programs/<code>/baseline` (G4).

**Build.** Nothing in the harness, by intent. The AI writes, in an
onboarding session: the Makefile, the replay driver, the capture
program, the manifest, the tolerances, and optionally properties. Every
harness change this step turns out to need is filed as a new step
rather than made in passing.

**Tests.** The onboarding session reaches `ONBOARDED` without the person
editing a file. A porting session reaches `ACCEPTED`. The request log
shows every refusal the AI received and what it did next — that record
is the first eval of the onboarding process.

**Shown that week.** `ledger status` for a region of a code that is not
tsunami.

---

## Step 9 — Documentation

**Goal.** A person who has never seen this repository can onboard a code
by reading one document.

**Build.**

- `docs/onboarding.md`: what to put in `programs/<code>/`, the build
  contract, the replay contract, the capture contract, the manifest
  fields, the onboarding session step by step with the claim each step
  files, what `promote` does, and the rubric from
  `notes/code-survey/tsunami.md:394-463` as a "before you start" checklist.
- `docs/pi-users-manual.md` and `docs/pi-install.md` rewritten so no
  step is stated in terms of tsunami; tsunami stays as the worked
  example. The "Changing the region or the strategy" section becomes
  "Changing the code, the region, or the strategy."
- `docs/per-code-dependencies.md` gets a closing note saying which
  findings were resolved by which step and which remain (G3, G4).
- `README.md` describes the gateway path, not the demo orchestrator.

**Tests.** The onboarding document is followed by someone other than its
author, on a third code, and the places they got stuck are the review.

---

## Open questions carried forward

- External baselines (G4). When a code is not vendored, `seed.py` needs a
  clone-and-pin step and the ledger key needs to include the upstream
  commit. Not planned here.
- A multi-code oracle (G3). One image per code is fine until two codes
  are ported in the same deployment.
- Region-scoped subjects (the dependency-cone hash from the architecture
  document). On a 63k-line code, `tree`-scoped requirements mean any
  edit re-runs every check. Becomes pressing at Step 8b.
- A stronger device proof (nsys kernel count) as a strategy option.
- The footprint check (spec-declared reads and writes versus what the
  code touches) still needs an analyzer this repository does not run
  generically. `harness_self_check` and `harness_replay` catch a wrong
  footprint empirically; the static check remains open.
