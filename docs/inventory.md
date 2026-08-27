# Inventory of the current code

Step 0 of `notes/pi-ledger-plan.md`. Written before any code for the
gateway/ledger system exists, against the state of `main` at commit
`ba5a627` (PR #4 merged). Scope follows the plan's Establish list:
services and endpoints, checks and where their command lines live, how
the NVIDIA Fortran strategy is enforced today, what the static analyzer
reads/emits, how captures are generated and stored, the current ledger's
columns, what the orchestrator loop does that an interactive replacement
still needs to do, and how the target codebases are laid out.

## Findings note

**Services today are a batch pipeline, not a gateway.** `demo/orchestrator/orchestrator.py`
is the only component with routes to everything else (agent-runner,
builder, oracle) — it already has the network shape the architecture
document proposes (`agent_net`, `build_net` internal, `oracle_net`
internal), but it plays gateway *and* orchestrator at once: it decides
model × strategy × attempt on its own, calls every check unconditionally
in a fixed order, and writes one ledger row per attempt. There is no
per-request precondition check, no refusal, no session id, and no
human-in-the-loop entry point. `agent-runner` is the piece that goes away
entirely under the new design — `pi` plus the extension takes its place.

**The current ledger is a flat CSV, not claims.** `orchestrator.py` writes
`/ledger/ledger.csv` (a Docker volume, empty on disk, present only at
runtime) with one row per attempt: `ts, region, model_key, rung, attempt,
backend, model, branch, src_sha, oracle_policy_sha, build, device_proof,
kernels_launched, memcheck, racecheck, initcheck, compare_visible,
compare_holdout, cpu_best_s, naive_stdpar_s, port_s, speedup, verdict,
human_intervention, notes`. `experiments/*.csv` are confirmed (by its own
README) to be copies of this same ledger from past campaigns; nothing in
`demo/` reads them back, so per D9/Step 1 they can be left alone as
history with no migration path needed.

**The SESE checker exists but emits nothing machine-readable.**
`tools/regionharness/check_sese.py` reads a region spec
(`notes/regions/*.yaml` — not `tools/regionharness/cases/` as the README
says) and prints PASS/FAIL text plus an exit code. It does not emit a
verdict object, an effects diff, a file list, or an allow-list as data;
`check_footprint.py` (VAL-5) and `check_captured.py` (VAL-2) are separate,
independently-invoked scripts with no shared claim shape connecting the
three. Wrapping this as `sese_check` (Step 6a) means adding structured
output, not just calling the existing tool.

**The replay build does not match D13 as written.** `gen_harness.py`
generates a capture module and driver from the spec, but the actual link
step (`replay/build.sh`, `MODE=tree`) links the generated driver against
the whole pre-built `libcoarseair.a`, not a dependency-ordered file list
compiled from the analyzer's output. More significantly, `gen_harness.py`
patches the anchor source file **in place in the live CoarseAIR
checkout** — the generated `n4pes_capture_mod.F90` and the marker-delimited
edits in `N4_UMN_PES_Class.F90` are sitting as uncommitted changes in
`codes/CoarseAIR` right now. This is the opposite of D3's model (agent
edits a copy it owns; the gateway lays allow-listed files over a clean
checkout it alone can reach).

**"Baseline commit" does not mean what the plan assumes.** `codes/` is
`.gitignore`d in this repo — nothing under it is a subtree or submodule,
each subdirectory (`CoarseAIR`, `tsunami`, etc.) is its own independent
git clone with its own upstream `origin`. `CoarseAIR`'s baseline is
`3758569d` on `origin/master`, and that checkout is currently dirty with
the regionharness instrumentation above. D3/D5/D9 all key off "the
baseline commit" as if it were a clean, addressable commit in a repo the
gateway controls; today the real target codebase is an external clone
that isn't clean and isn't under this repo's git control at all.

**A working mutation self-check already exists, narrower than the plan assumes.**
The architecture document lists "a critic... harness self-check that an
injected mutation fails" as a deferred extension point. `tools/fmutate`
already does exactly this — AOR/ROR/LCR/CRP/SBR/SDL mutation, rebuild,
replay, score against the real oracle and tolerance file — but only
against `demo/`'s hand-written kernel, not against a
`regionharness`-generated harness for a real region like n4pes. Less new
work than the deferred label implies, but it needs generalizing, not
inventing.

**A third check exists with no predicate slot.** `test_n4pes_properties.py`
(Hypothesis) checks permutation invariance, gradient consistency, and
determinism over a sampled envelope around the 50 captured points — a
for-all-inputs claim, distinct from the bitwise capture-replay regression
and not represented by any predicate type in the architecture document's
registry (`sese/verified`, `build/replay`, `gpu/executed`,
`sanitize/*`, `regression/visible`, `regression/holdout`, `timing/*`).

**Captures for a real region have no visible/holdout split.** `demo/`'s
capture pipeline deliberately splits generated cases across the trust
boundary at generation time (visible inputs+outputs to two different
containers, holdout inputs only). The n4pes region's captures
(`ftgdata/ftg_n4pes_test/`, 700 files, Serialbox binary format, 50
invocations) have no such split — all 50 are used together for
capture-replay, and the Hypothesis layer samples its own envelope
separately. Regression/visible vs. regression/holdout as predicate types
assumes a split that doesn't exist yet for a non-demo region.

**Strategy enforcement is hardcoded, not a file.** `demo/builder/stages.py`
hardcodes four compiler profiles (`stdpar_managed`, `omp_target`,
`cpu_best`, `cpu_naive_stdpar`); `agent-runner`'s `STRATEGY_CARDS` are
free-text prompt guidance, not machine-enforced. The only file-restriction
mechanism today is a single hardcoded path constant (`EDITABLE =
"src/mod_kernel.f90"`), not a glob-based allow-list. Step 2's strategy
file and Step 4's allow-list are both genuinely new; nothing today
generalizes past one file.

**Things that already match the plan closely.** The oracle's receipt
shape already matches the intended receipt policy exactly: `/v1/compare`
returns per-case detail for `visible` and pass/fail only for `holdout`
(`demo/oracle/app.py`), with no code change implied by D-whatever governs
receipt policy. `docs/examples/` already holds exactly the known-good/
known-bad `mod_kernel.f90` snapshots the plan's Step 6 test pattern
expects. The docker-compose network topology (three networks, two of them
internal-only) is close enough to the proposed deployment view that Step
8 is mostly "add a gateway container and a sessions volume," not a
redesign.

## Table

| existing thing | plan step that uses/replaces it | what is missing |
|---|---|---|
| `demo/orchestrator/orchestrator.py` (batch loop, fused orchestrator+gateway) | Step 5 (gateway) replaces its role | Interactive request/response (D1), precondition table, refusals, session ids |
| `demo/agent-runner` (single-file `EDITABLE` constant, free-text `STRATEGY_CARDS`) | Superseded by `pi` + extension (Step 7) | Nothing carries forward except the idea of a restricted edit surface |
| `demo/builder` (`stages.py`: 4 hardcoded profiles, build/run/sanitize/time) | Step 6b/6c/6d dispatch targets | Re-keying to accept gateway-constructed trees instead of orchestrator's git repo |
| `demo/oracle` (`/v1/compare`, `/v1/policy`, visible-detail/holdout-blind) | Step 6e dispatch target | Already matches receipt-policy intent; effectively reusable as-is |
| `/ledger/ledger.csv` + `experiments/*.csv` copies | Step 1 replaces with `claims.jsonl`/`requests.jsonl` | No migration needed (D9: leave as history); new shape is per-region JSONL, not per-attempt CSV row |
| `tools/regionharness/check_sese.py` | Step 6a wraps this | Structured verdict/detail output; emitted file list and allow-list as artifacts (today: stdout + exit code only) |
| `check_footprint.py` (VAL-5), `check_captured.py` (VAL-2) | Not named in the plan; need predicate types or folding into `sese/verified` detail | A shared claim shape connecting all three SESE-family gates |
| `gen_harness.py` + `replay/build.sh` (`MODE=tree`, links `libcoarseair.a`) | Step 6b `build_replay` | Reconciling with D13 (dependency-ordered file list); today it links the whole library, not a generated file list |
| `gen_harness.py` patching the live CoarseAIR checkout in place | Conflicts with D3 (clean checkout the agent cannot reach) | A submit/allow-list path that doesn't require in-place patching of an external, uncommitted working tree |
| `codes/CoarseAIR` (external, untracked, dirty clone at `3758569d`) | D3/D5/D9's "baseline commit" | A definition of "baseline" that works when the target isn't in this repo and isn't clean |
| `notes/regions/n4_umn_pes.yaml` spec format | Step 1/6a spec input | Reusable as-is; already has more sections (`footprint`, `contract`, `obstructions`, `internal_flow`, `validation`) than the architecture doc names |
| `ftgdata/ftg_n4pes_test/` (700 files, Serialbox, 50 captures, no visible/holdout split) | Step 1 `capture_set` subject/hashing | A hashing function for this format; a decision on whether/how a real region gets a visible/holdout split |
| `test_n4pes_properties.py` (Hypothesis property layer) | Not represented in the predicate registry | A new predicate type, e.g. `regression/property` |
| `tools/fmutate` (mutation self-check, `demo/`-only target) | Listed as deferred (§9) but partially exists | Generalizing the target to a regionharness-generated harness (e.g. n4pes), not building from scratch |
| `docs/coverage-testing.md` (§12: fmutate not yet in CI) | Background/context for Step 6 | Not itself a gap; confirms fmutate's current scope |
| docker-compose networks (`agent_net`, `build_net` internal, `oracle_net` internal) | Step 8 containers | A discrete gateway container and a `sessions/` volume; topology otherwise close |
| `docs/examples/` (known-good/known-bad `mod_kernel.f90` snapshots) | Step 6 test fixtures | Nothing — matches the plan's expectation directly |
| Strategy enforcement (`stages.py` profile dict + `STRATEGY_CARDS` prompt text) | Step 2 (strategy file) replaces | The file itself: no hash, no `allow_globs`, no per-strategy analyzer command exists today |
