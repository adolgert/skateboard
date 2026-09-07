# Plan: separate the components, then clean the Python

**Started 2026-09-07** on branch `feature/critique`, from a five-agent code
review of the whole repository (gateway; components, analyzers and capture;
ledger, schemas and CLI; builder and oracle services; and a cross-cutting
pass over the import graph). The owner's two concerns, in order, were clear
separation of components and clear, concise Python.

It is executed the same way as the earlier plans: Establish, Decide, Tests
first, Build, Review, one step per conversation pair, code sized to be read
in a sitting. Plan labels (Step 2c, R4) belong in this document, commit
messages, and conversation only. Code, docstrings, tests, and user-facing
docs state the rule in plain words instead.

## The diagnosis the plan acts on

The review's separation findings have a small number of structural causes.
Fixing symptoms one at a time (moving an import, adding a conftest) would
leave the causes in place, so Step 2 acts on the causes and Step 3 re-reviews
to see what is left.

**Coordinates travel where objects should.** A check receives
`(store, repo_dir, ref, region_id, tree_sha, baseline_strategy, builder)` and
re-derives what it needs: materializes the tree to read its manifest,
computes the attempt id, reads prerequisite claims, decides its subject.
Three missing objects account for the three cycles found:

- A `Tree`: payload, tracked files, materialization, in-tree manifest, and
  attempt id are all functions of one value. Because there is none, nearly
  every component imports `equivalent/gateway/submit.py` for git plumbing
  (19 import statements), and `tree_manifest.py` re-materializes the tree on
  every call.
- An `EvidenceContext`: the materials that make a claim current are computed
  in `gateway/evidence.py`, pushed into a `ContextVar` on `LedgerStore`,
  consumed implicitly by `latest`, then overridden for `accepted` in
  `gateway/app.py:481` and `cli/main.py:247`, with a fourth evaluator in
  `cli/session.py:383`. The store answers "what was recorded"; "what is
  valid now" is policy over the store and has no home.
- A builder `Workspace`: the stages in `services/builder/stages.py` share
  state on purpose (a built tree with identified artifacts), but the shared
  thing is a path convention plus `_executable()`. `score_mutant` rebuilds
  without build evidence because there is no single "build into this
  workspace" operation to reuse; the `_JOB_RUNNER` module global is executor
  policy the workspace should hold.

**The gateway is the domain's landlord.** `RegionConfig`, evidence policy,
the action table, and config loading live under `gateway/` because the
gateway was their first client. The CLI, promotion, and the session summary
import the gateway to reach them, contradicting the docstring in
`ledger/acceptance.py:22-24` that says the CLI must work without the gateway
installed. `gateway/evidence.py` imports nothing from the gateway.

**Phase is a branch, not a provider.** Onboarding and porting differ in
five things that vary together: where the manifest comes from (in-tree
versus promoted), which strategies to build with (both versus one), how the
attempt id is formed (`attempt_id_for_strategy` versus `attempt_id_for`),
which predicate names the build (`BUILD_PREDICATE[phase]`), and what shape
the build detail takes (`_build_entries`). Because they are not packaged,
the gateway branches on `cfg.phase` in eight places and a second family of
components grew: `harness_build`/`build_replay`, `harness_replay`/`run_replay`,
`harness_property`/`property_check`, `harness_timing`/`timing.check_baseline`.
Some pairs judge differently and should; the builder call, build verdict,
and timing run underneath are the same job and have already drifted.

**Checks both judge and record.** Half the components take a `LedgerStore`
and write capture sets, program sets, and artifacts, and read prerequisite
claims. That is what forces the `ContextVar`, the nineteen signatures, the
eighteen-branch dispatch ladder in `gateway/app.py:769-1061`, and the
untyped `{"verdict", "detail"}` result whose failure vocabulary has nine
spellings. Making checks pure (context in, result out, no store) is the one
movement most of the rest hangs from.

**Not problems.** The service images copying `equivalent/capture/compare.py`
rather than installing the package is a deliberate trust boundary and stays.
The gateway and ledger being tightly coupled is correct: the gateway is the
ledger's writer. Stages sharing a workspace is correct; only its
implicitness is the problem.

## Decisions already made

From the discussion on 2026-09-07.

- **S1.** Bugs first, then the root-cause refactoring, then a second review
  round, then the remaining separation and clarity findings. Nothing from
  the review's clarity list is addressed in Step 2 unless the refactoring
  passes through it anyway.
- **S2.** `equivalent` stays one package. The layering inside it changes;
  no split by service. Evidence: `deploy/gateway/Dockerfile` installs the
  whole package and both service Dockerfiles refuse to.
- **S3.** The services keep copying `compare.py` in. A shared vocabulary
  package (wire types, dataset names, layout constants) is deferred; the
  cheap fix now is parity tests.
- **S4.** Builder stages are not made independent. The shared workspace is
  made explicit instead.

## Step 1 — Fix the confirmed bugs

Each fix gets a test that fails before and passes after. None changes an
interface.

- **1a. Build timeout crashes the endpoint.** `services/builder/stages.py:412`:
  the `except subprocess.TimeoutExpired` handler binds `rc, out, err` but not
  `audit`; the production branch fifteen lines later reads `audit`, so a
  build exceeding `BUILD_TIMEOUT_S` raises `UnboundLocalError` instead of
  returning a failed build. Initialize `audit = None` before the `try` and
  return the standard build-failure dict from the handler. Test: a runner
  that raises `TimeoutExpired` yields `{"ok": False, "stage": "build", ...}`.
- **1b. A refusal can name a requirement whose status is present.**
  `equivalent/gateway/app.py:676-681` appends `requirement_status(...)`
  unconditionally when a passing build claim carries no executable digests,
  unlike the `missing.extend` below it that filters on `status == "missing"`.
  The session gets `{"refused": true, "missing": [<a present row>]}` with
  nothing to run. Filter the same way and raise an explicit error for "the
  build claim names no executable". Test: a build claim with empty targets
  produces a refusal whose every row is `missing` with a `producing_action`.
- **1c. A transient artifacts call is reported as a lost build.**
  `gateway/app.py:336-348`, `_artifacts_match_build` catches every exception
  around `builder.healthz()` and `builder.artifacts()` and returns `False`,
  sending `/run` into `_restore_build` whose failure text says the cached
  build was lost. Distinguish transport failure (503, as `_runtime_materials`
  does) from a truthful mismatch. Drop the second `healthz()` call. Test: a
  builder whose `artifacts` raises produces a 503, not a rebuild.
- **1d. The mutation pool relies on fork.** `stages.py:1282`, `score_mutant`
  runs in a `ProcessPoolExecutor` worker and reads `_JOB_RUNNER` and
  `_DOCKER_EXECUTOR` from module state, reached only by fork inheritance.
  Pass the runner (or a picklable descriptor) in the job payload like every
  other input. Test: the existing mutate tests pass with
  `multiprocessing.get_context("spawn")` forced. (Python 3.14 changes the
  default start method; `requires-python` is `>=3.12`.)
- **1e. `find_duplicate` docstring is dead code.** `equivalent/ledger/store.py:224`:
  the triple-quoted string follows an assignment, so `__doc__` is `None`.
  Move the string above the assignment. Test: `LedgerStore.find_duplicate.__doc__`
  is not `None`.
- **1f. Unregistered predicate types are accepted on write.**
  `ledger/store.py:122-135`, `record_claim` appends any `predicateType`;
  the failure surfaces later as a `KeyError` in `predicates.get` when the
  claim is read back, after the line is on disk. `Subject.__post_init__`
  already rejects unknown kinds. Call `predicates.get(predicateType)` before
  the append. Test: recording an unknown predicate type raises before
  anything is written.

## Step 2 — Refactor toward the root cause

Ordered so each sub-step is mechanical given the one before, and each
leaves the test suite green. Behavior does not change until 2e, and 2e
changes only where evidence is written, not what is written.

### 2a. A `Tree` value, below both gateway and components

**Establish.** `equivalent/gateway/submit.py` does three jobs: git
repository mutation (`submit`, `_commit_tree_if_changed`, `init_baseline_repo`),
allow-list resolution (`resolve_allow_globs`, `frozen_for_allow_globs`), and
read-only tree access (`tracked_files`, `tree_payload`, `materialize_tree`,
`attempt_id_for`, `attempt_id_for_strategy`, `baseline_tree_sha`,
`current_tree_and_frozen`). Components need only the third.

**Build.** New module `equivalent/tree.py` (name to be confirmed in
Decide) holding a frozen `Tree(repo_dir, ref, sha)` with methods for
payload, tracked files, a materialized scratch copy (context manager,
cached for the object's life), the in-tree manifest and policy bytes (what
`components/tree_manifest.py` does now), and attempt ids. `submit.py`
keeps `submit`, the receipt, allow-list resolution, and branch handling,
and imports `Tree`. Every component replaces its `submit` import with
`Tree`; `tree_manifest.py` is folded in.

**Result.** The components-to-gateway edge drops from 19 imports to
whatever 2c leaves (expected zero). `deploy/onboard_walkthrough.py:33`'s
import of `cli/promote` constants is handled in 2b.

### 2b. Move the domain out from under `gateway/`

**Establish.** Modules under `equivalent/gateway/` with no gateway
dependency: `evidence.py` (imports only ledger, reference, strategy),
`regions.py` (imports manifest), `table.py` (imports ledger.acceptance).
`config.py` is deployment configuration for a region, read by both the
gateway and the CLI.

**Build.** `evidence.py` moves to `equivalent/ledger/evidence.py`.
`table.py` moves next to `acceptance.py` in `ledger/` (its docstring's
stated reason for living there). `regions.py` and `config.py` move to a
top-level `equivalent/region/` (or stay one level up; decide in Decide).
The `programs/<code>/` layout constants (`MANIFEST_NAME`, `CAPTURES_DIR`,
`BASELINE_DIR`, `DATASETS_DIR`) and `in_tree_manifest_text` move from
`cli/promote.py` and `gateway/config.py` into `manifest/schema.py` beside
`IN_TREE_MANIFEST`. `REQUIRED_DATASETS` is imported by `harness_capture.py`
and `regression.py` instead of re-declared. `Variable`, `DTYPES`, `MAX_RANK`
move from `manifest/schema.py` to `capture/`, closing the
`capture -> manifest -> ledger -> capture` cycle.

**Result.** `cli/` imports the gateway only for nothing (check with the
import-graph script from the review, kept under `equivalent/tests/` as a
test that asserts the allowed edges). `gateway/` is HTTP, auth, locks,
precondition enforcement, and backend clients.

### 2c. `CheckContext` and `CheckResult`; checks stop reading and writing the store

**Establish.** Nineteen `check` signatures (table in the appendix). Every
argument is a field of `RegionConfig`, a value the gateway computed (`ref`,
`tree_sha`, `strategy`, materials), or a handle (`builder`, `oracle`,
`store`). Store reads in components: `timing.py:134,138` (`build/replay`,
`program/regression`), `regression.py:28` (`gpu/executed`),
`harness_capture.captured_sets` and its five callers. Store writes:
`harness_capture.py:218`, `harness_determinism.py:123`, `timing.py:225`,
`harness_timing.py:120`, `original_check.py:39`.

**Decide.** The shape of `CheckResult`: `verdict`, `reasons: tuple[str, ...]`,
`detail: dict` for free-form diagnostics, and declared optional fields for
every key the gateway currently reaches into by string
(`policy_sha256`, `program_set`, `reference_sha256`, `capture_set`,
`executable_identity`, `allow_globs`), plus `to_store: tuple[...]` of
arrays the gateway should persist and name as materials. Whether
prerequisite claims are handed to the check as objects or as the values the
check actually uses (a capture set's sha, a build claim's targets).

**Tests first.** A parity test that every `check` in `components/` has the
signature `check(ctx: CheckContext, config: dict) -> CheckResult`. The
existing component tests keep their assertions and change only how they
construct the call.

**Build.** One component at a time, starting with a pair (`build_replay`,
`harness_build`) so the provenance object in 2d takes shape from real
differences. The gateway's `record` step gains the storing that components
used to do; `_capture_set_materials` and the inline `detail.get(...)` reads
at `app.py:819,850,868,901,1002` read `CheckResult` fields. `LedgerStore.activate_context`,
`_required_materials`, and the `ContextVar` are deleted once no component
reads the store; `required_materials` becomes a required parameter of
`latest` and `find_duplicate`.

**Result.** The store has one client. Dispatch in `app.py` becomes a lookup
from `ActionRow` to a callable; the eighteen branches, the fifteen
`"builder not configured"` raises, and the eighteen `log`/`return` pairs
collapse. `ActionRow` gains a `needs: tuple` (`("builder",)`,
`("builder", "oracle")`) so backend availability is checked once from the
table. The unreachable "not implemented yet" fallthrough becomes a
startup-time assertion that every componented row has a handler.

### 2d. Phase as a provider

**Establish.** With the pairs side by side after 2c, list exactly what
differs: manifest source, strategies to build, attempt id scheme, build
predicate name, build-detail shape, missing-target handling (error in
`timing.timing_target`, verdict in `harness_timing`), and the judgment
each check makes on the builder's answer.

**Build.** A `Provenance` (name in Decide) on `CheckContext` answering
manifest, strategies, attempt id for a strategy, build predicate, and tree
subject for its phase. One `build` layer (`build_replay.build_verdict`
already is that), one `time_program`, one `replay` call; the
phase-specific judgment stays in the component that makes it.
`harness_timing` and `timing.check_baseline` become one implementation
with one stored-set detail key. `BUILD_PREDICATE`, `_build_entries`, and
the `cfg.phase == "porting"` checks in `app.py` are replaced by calls on the
provider. The `timing`/`program_regression` module cycle is broken by
moving `timing_target`, `tolerance_policy`, and `compare_outputs` to a
shared lower module; `check_port` either stops re-comparing outputs or the
`timing/port` predicate description says that it does.

### 2e. `EvidenceContext` and one acceptance rule

**Build.** `compute_status` in `ledger/status.py` takes the context
(materials, phase, tree, build cohort, `context_verified: bool`) and sets
`accepted` and `note` itself. `gateway/app.py:476-485` and
`cli/main.py:245-249` supply only the boolean each can compute.
`cli/session.py:383`'s `_accepted_after` calls a helper in `status.py`
that applies `claim_matches_context`, which moves from the store to
`ledger/evidence.py`. `_open_region` in `cli/main.py:117` returns a
dataclass instead of an 8-tuple, since it is being touched.

### 2f. An explicit builder workspace

**Establish.** `services/builder/stages.py` stage table (appendix). The
path rules (`_workspace`, `_tree_dir`, `_in_tree`), artifact identity
(`_write_artifacts`, `artifact_identities`, `_identity_matches`,
`_executable`), and ownership (`_prepare_job_files`, `_freeze_tree`,
`_writable_copy`) are the workspace; the `_JOB_RUNNER is None` checks in
seven functions are executor policy.

**Build.** A `Workspace(work_root, attempt_id, executor)` class owning
those helpers and one `execute(cmd, *, mode)` replacing `_run`,
`_run_audited`, `_run_profiled`. `build()` creates it; `run`, `capture`,
`sanitize`, `properties`, `time_run`, and `mutate` take it. `score_mutant`
calls the workspace's build operation so a mutant is built and verified the
way the tree was. The seven `_JOB_RUNNER` conditionals become one injected
policy object with a real and a no-op implementation. One helper maps
`TimeoutExpired`/`IsolationUnavailable` onto a stage's failure dict so every
stage catches the same set. `contract.py` is renamed `compile_log.py`; a
new `contract.py` holds request and response dataclasses that `app.py`
validates and every stage returns, so a stage cannot omit a key on an
error path. `BuilderClient` returns those response types; `FakeBuilder`'s
33 knobs become their fields.

**Tests.** `equivalent/tests/services/` moves to `services/tests/` with
`testpaths = ["equivalent", "services"]`. A parity test asserts each
`backend_client` request body validates against the builder's request
model and that `FakeBuilder`'s method signatures match `BuilderClient`'s.

## Step 3 — Second review round

Re-run the same five reviews with the same prompts (recorded in the
appendix), plus the import-graph test from 2b as a hard check. Compare
against the appendix: which findings dissolved, which remain, which are
new. Produce a revised ranked list. Expected to remain: the clarity
findings in the appendix that Step 2 does not pass through (preflight,
the hyphenated script names, ledger query caching, schema loader
consistency, test fixture duplication).

## Step 4 — Address what remains

From the Step 3 list, in its ranked order. Candidates already known,
grouped by the module they touch, are in the appendix under
"Clarity findings". The strategy and reference loaders (appendix R-L4,
R-L5) are the most consequential of these because they gate what gets
compiled and are not touched by Step 2.

## Progress and as-built deviations

Recorded as each step lands. Line anchors in the appendix are as of
`c9fb867` and are not updated.

- **Step 1** — commit `065fe4d`. 1a returns the failure dict from the
  timeout handler rather than initializing `audit` (no path reads it).
  1b raises 503 for a passing build claim naming no executable; the
  refusal path filters on `status == "missing"`. 1c threads the executor
  identity that `_runtime_materials` already established into
  `_artifacts_match_build`, so `GET /status` now returns 503 (not
  advisory) when the artifacts call fails. 1d passes the job runner in
  each worker's payload and adds `MUTATE_START_METHOD` for tests to
  force spawn; the worker still installs the runner into module state
  because `_prepare_job_files` reads it (2f removes that seam).
- **2a** — commit `1e000e2`. Module is `equivalent/tree.py`.
  `Tree(repo_dir, ref)` with `files` (dict path→bytes, read once),
  `sha`, `payload()`, `write_to()`, `materialized()` (one scratch
  directory per object, removed by `weakref.finalize`), `manifest()`,
  `manifest_and_policy()`. The attempt-id functions moved as module
  functions, unchanged, for 2d to re-home. `tree_manifest.py` is gone;
  the ComponentError wrapping is `errors.after_the_manifest_check_passed`.
  `tests/test_imports.py` prints the package adjacency.
- **2b** — commit `241fd3f`. `gateway/evidence.py` split rather than
  moved whole: the claim-context functions went to `ledger/evidence.py`,
  and `evidence_materials_for` to `region/evidence.py`, because
  `strategy/` and `reference/` import `ledger.subjects` and a whole move
  would have made two new cycles. New package `equivalent/region/`:
  `config.py` (RegionConfig, region_slug), `deployment.py` (the file
  gateway and CLI both read), `current.py` (current tree, allow-list,
  working copy), `evidence.py`. Layout names went to a new
  `manifest/layout.py` rather than into `schema.py`. `Variable`,
  `DTYPES`, `MAX_RANK` are in `capture/variables.py`. `promote.py` is
  top-level. `deploy/seed.py` keeps its own `MANIFEST_NAME` because
  `up.sh` runs it before the package is installed; a parity test covers
  it and the oracle. Allowed edges are asserted in `tests/test_imports.py`.

- **2f (builder side)** — commit `33378d2`, run in parallel with 2c
  because it touches only `services/`. `Workspace(work_root, attempt_id,
  policy)` in `services/builder/workspace.py`; every stage takes one
  (not only the six named), and `build()` alone creates it on disk. One
  `execute()` with plain/audited/profiled modes; every executor failure
  maps onto `ExecutionFailed`. Executor policy is `DisposableJobs`
  (production) or `InProcessJobs` (tests, set once in
  `services/tests/conftest.py`); the mutation worker receives the
  workspace, which carries the policy. `score_mutant` builds through the
  same `_make` as the tree. Old `contract.py` is `compile_log.py`; the
  new `contract.py` holds pydantic request and response models with a
  failure constructor carrying the whole shape (keys formerly omitted on
  some paths are now present as null/0/{}). Tests moved to
  `services/tests/`; `pythonpath = ["."]` added. Left for the client
  half: `BuilderClient` returning the response types, `FakeBuilder`
  knobs as fields, the request-body parity test, deleting
  `kernel_launches`/`LAUNCH_LINE` (kept alive by
  `tests/components/test_run_replay.py`).
- **2c** — commit `0682566`. `CheckContext` and `CheckResult` in
  `components/context.py`. Prerequisite claims are handed as objects
  (`ctx.claims` keyed by predicate type, exactly the row's `requires`
  plus the phase's build predicate for dependent actions). Stored arrays
  are read through a read-only `SetReader`. Writes go through
  `CheckResult.stores` of packed sets and artifacts (`ledger/packed.py`,
  `pack_capture_set`/`pack_program_set`, `LedgerStore.keep`). `reasons`
  is a tuple on the result and appears in the `/run` body only when
  non-empty; detail keys are unchanged so the claim format on disk is
  unchanged. Rows gained `requires` their components were reading
  without declaring; `baseline_tree` is a new subject kind in the
  gateway (still a `tree` Subject on disk). `gateway/dispatch.py` holds
  the handler table with an import-time parity check. The backend
  availability check runs just before dispatch so a refusal can still
  name what to run on a gateway without a builder. `gateway/datasets.py`
  moved to `components/datasets.py`. `components/names.py` holds the
  shared role and key names.

- **2d and 2e** — commit `71556c7`, built in parallel. `Provenance`
  lives in `components/context.py` (a separate module would cycle with
  `CheckResult`), with class-level `build_predicate`, `build_entries`,
  `oracle_judges` (the gateway asks with only a phase, via
  `provenance_for`) and instance-level `manifest()`, `strategies()`,
  `attempt_id()`, `build_target()`. The attempt-id functions stayed in
  `tree.py`: `original_check` derives one for a reference tree that is
  not the context's. Shared layers: `build_verdict` (already), new
  `components/program_outputs.py` (time-and-validate, plus the tolerance
  policy and output comparison that broke the timing/program_regression
  cycle), new `components/backend.py` (replay and capture calls). The
  replay judgments stayed separate on purpose: bitwise reproduction
  versus declared-shape-plus-kernel-count are different questions.
  `program_set` is the one detail key. `check_port` keeps re-comparing;
  the `timing/port` predicate description now says so. Acceptance:
  `compute_status(..., context_verified=<required>)` sets `accepted` and
  `note`; the CLI passes whether the deployment pins executor and oracle
  identities (restored after the agent first made it always False, so
  the CLI and promotion stand on the same ground); the gateway passes
  its artifact recheck. `claim_matches_context` is in
  `ledger/evidence.py`; `status.accepted_by` serves the session summary.
  `OpenRegion` replaces the 8-tuple.

- **2f (client side)** — commit `5e5e05c`. `backend_client.py` parses
  every builder answer into a frozen dataclass of the fields the gateway
  reads, validated in one place; an ill-formed answer is a
  `ComponentError` at the parse point rather than a fail claim blaming
  the port (a deliberate trust change; tests state both halves).
  Components take the typed answers without naming their type, because
  `components` may not import `gateway`. `FakeBuilder(**answers)` takes
  a response object, a callable, or an exception per endpoint; the
  twenty-one knobs are gone. `tests/test_builder_parity.py` validates
  every request body against the builder's request model, every field
  the gateway reads against the builder's response model and default,
  and the fake's methods against the client's. `kernel_launches` and
  the stderr grammar are deleted; two strategy files still carry a
  stale comment about `NVCOMPILER_ACC_NOTIFY` counting launches, left
  because they are hashed configuration.

Step 2 is complete at `5e5e05c`: 846 tests, up from 745 at `c9fb867`.

## Step 3 as run — the second review round

Five Opus agents re-ran the review at commit `25c8ed7` with the prompts
in the appendix. Anchors below are as of that commit. The full reports
are summarized here; the appendix ids say what each first-round finding
became.

**Overall.** All five agree the separation is now real: zero violating
import edges and zero module cycles across 210 files (recomputed by AST
walk, agreeing with `tests/test_imports.py`); every check is
`check(ctx, config)`; the store has one client; the builder wire has
two typed spellings with a parity test. What remains is of three kinds:
a rule stated in one place and honored differently in another; a thin
layer of unfinished adoption of the new objects; and the clarity items
Step 2 was not meant to pass through.

**Reconciled against the appendix.** Dissolved: G2, G3, G5, G6, G9,
G10, G11, G15; C1, C4, C5, C7, C9, C13, C15; L2, L3, L7, L8, L14; S1,
S2, S4, S5, S6, S7, S8, S9, S14; X1, X2, X4, X6, X7, X9, X10, X11, X12.
Mostly dissolved with a residue named below: G1, G8, G14; C2, C3, C6,
C8, C14; L1, L6; S3; X3, X5, X8. Remain (expected): G7, G12; C10, C11,
C12; L4, L5, L9, L10, L11, L12, L15; S10, S11, S12, S13, S15; X13, X14.
Marked "dissolves" but did not: S13 (payload types still bare dicts;
`mutate` ten positionals; no test of `services/builder/app.py`).

**Revised ranked list.** Grouped by the area an agent can own; within a
group, most consequential first.

Rules stated once and honored differently:
1. [separation] `region/current.py:52 matches_any` (case-sensitive
   `fnmatch`) gates submit and the frozen hash while
   `strategy/schema.py:67-83 Strategy.allows` case-folds; a file the
   strategy allows is rejected at submit. Three glob semantics in all
   (`manifest/schema.py:394-419` adds `./` and `**/`). One
   `glob_matches`, the manifest's rule.
2. [separation] the tolerance policy is hashed two ways: plain sha256 in
   components and the oracle, `hash_files` in `region/evidence.py:53-54`;
   every program/regression and self_check claim carries two `policy`
   subjects for one file. One `policy_subject`, plain sha256.
3. [separation] `gateway/app.py:658-669 _restore_build` dispatches a
   check with `claims={}` before the backend check at `:794`; a gateway
   with no builder answers with an `AttributeError` string.
4. [separation] `ledger/status.py:118-124` `context_verified` is defined
   as "executables confirmed in place"; the CLI and promotion pass True
   on the deployment's reviewed pins. State both grounds.
5. [separation] `services/builder/stages.py:368-369` and
   `workspace.py:189` raise `ValueError` out of five stages as 500s,
   contradicting `contract.py:1-9`.
6. [separation] `ledger/evidence.py:49-68 binary_materials` scrapes
   detail keys spelled as literals in six components;
   `FOUNDATION_PREDICATES` (`:21-24`) is hand-kept against the table;
   the verdict word has no declaration (64 `"pass"`, 15 `"fail"`);
   `"baseline_tree"` is spelled in four places with no registry
   (`app.py:83-92 SUBJECT_KIND_OF` versus `CheckResult.subject_kind`;
   `status.py:134` reads it as frozen). One vocabulary module in the
   ledger; a subject-kind registry beside the table.

Unfinished adoption:
7. [separation] `program_regression.py:91-105` calls `builder.time`
   around `time_program`; `timing.py:122-123 check_port` declares no
   materials; `original_check.py:79-96` re-spells `build_verdict` and
   keeps artifacts on the failure path (`:148`); `components/backend.py`
   covers two of nine backend calls while its docstring claims all;
   `sanitize.py:51`/`regression.py:59` read `ctx.manifest`; ad-hoc
   workspace names at `timing.py:147`, `original_check.py:72-74`.
8. [separation] the components' wire types live in
   `gateway/backend_client.py:70-296`; `backend_client.py:37` imports
   `components.errors`; twelve component tests import the gateway. The
   oracle wire got none of the typing (`backend_client.py:398-424`,
   `regression.py:39-77` by string, no oracle contract, no parity).
9. [separation] `promote.py:30` imports `components.harness_capture`;
   `sets_named_by` raises `ComponentError` past `cli/main.py:264`.
10. [separation] builder policies: `stages.py:1106 time_run` runs in a
    different directory per policy and `test_builder_stages.py:359-373`
    pins the test-only branch; `Workspace.policy` read at five stage
    sites; `audited` means two things (`workspace.py:308`,
    `test_builder_executor.py:17-25`); `_observed_compiler_log`
    (`:191-217`) and replay-count evidence (`:730-746`) untested;
    `reset()` leaves `artifact_file`; `contract.ENDPOINTS` untested.
11. [clarity] `tree.py:182-185 materialized()` manages nothing;
    `packed.py:28-30` two unread fields pinned by a test;
    `store.py:229 exists_pass` dead; the `required_materials_by_predicate`
    derivation at three call sites; `status.py:46,100,157` default
    materials the store forbids; `capture_sets.py:147 program_arrays`
    decodes the wire inside the ledger.

Gateway clarity:
12. `_post_run` still 233 lines with five closures (`app.py:600-832`);
    `RunRequest` lacks `extra="forbid"` (`:155-158`); `"harness/original"`
    hardcoded in the recording rule (`:764-775`); two identity blocks
    (`:245-291`) and a hex regex in four places; `_artifacts_match_build`
    untyped (`:311-350`); `materials_for` restates `evidence.py:79-96`;
    nested helpers and six `getattr`s; dead test constants;
    `test_onboarding_dispatch.py:21` importing a component test;
    `test_submit.py` mostly testing `tree.py` and `region/current.py`;
    `Handler.check: object`; `record` inside a comprehension after
    `keep`.

Untouched by Step 2, in the appendix's order: R-L4, R-L5 (loaders);
R-S11 (preflight, whose fallback now breaks `docs/pi-install.md:131`);
R-S12; R-S10 (harness library; tsunami's `_same_bits` is weaker than
`bitwise_equal`); R-S15 plus the untested SQLite kernel query; R-L9,
R-L10, R-L11, R-L12, R-L15; R-C10 (the five long checks; `original_check`
grew to 98 lines), R-C11 (three tolerance parses), R-C12; capture-format
constants in four places and the oracle's raw manifest keys outside the
parity net; R-X13; R-X14 (and the pi extension drops `note`).

Layout recommendations from the cross-cutting review, adopted:
`Provenance` to `components/phase.py`; the attempt-id functions out of
`tree.py` to the components (they name a builder directory); `tree.py`,
`promote.py`, `region/` stay where they are. The import test's
relative-import blind spot and its three looser-than-needed rows are
fixed with it.

## Appendix — The review findings, verbatim anchors

Findings marked **[dissolves in 2x]** are expected to disappear as a
consequence of that sub-step; the rest are for Step 4 unless Step 3 says
otherwise. Line numbers are as of commit `c9fb867` on `feature/critique`.

### Gateway (`equivalent/gateway/`)

- R-G1 [separation] `app.py:581` `_post_run` is 481 lines; `create_app`
  is 869 (195–1063). Validation 603–640, evidence 642–665, preconditions
  675–705, duplicates 707–738, recording 740–767, dispatch 769–1055.
  **[dissolves in 2c]**
- R-G2 [separation] `app.py:769-1061` 19 copy-pasted dispatch branches;
  `raise ComponentError("builder not configured")` 15 times; `log`/`return`
  pairs 18 times; "which backend" re-declared outside `table.py:94`.
  Fallthrough at 1060 unreachable. **[dissolves in 2c]**
- R-G3 [clarity] `app.py:571-588` `/run` authenticates and resolves the
  region twice; `_post_run` takes `authorization` only to repeat it.
  **[dissolves in 2c]**
- R-G4 [clarity] `app.py:676-681` refusal can name a present requirement.
  **Step 1b.**
- R-G5 [separation] `app.py:603-640` config value validation in the route;
  spec lives in `table.py`. Type check only for `"integer"`. Move to
  `table.check_config(row, config)`.
- R-G6 [clarity] `app.py:725` duplicate detection hardcodes
  `("build_replay", "harness_build")`; derivable from
  `PRODUCERS[BUILD_PREDICATE[cfg.phase]]`. **[dissolves in 2d]**
- R-G7 [clarity] `app.py:264-310` `_runtime_materials` has two
  near-identical identity-validation blocks; SHA-256 regex also at
  `config.py:231` and `ledger/subjects.py:41`. Extract
  `_verified_identity(fetch, what, pin)`; one shared predicate.
- R-G8 [clarity] `app.py:324,391,405` `_build_claim`, `_current`,
  `_visible_cases` nested in the factory though they close over nothing;
  `_runtime_materials`, `_artifacts_match_build`, `_restore_build` close
  over only `builder`/`oracle`. Make module-level. **[partly 2c]**
- R-G9 [separation] `app.py:476-485` acceptance decided twice.
  **[dissolves in 2e]**
- R-G10 [clarity] `evidence.py:35` unreachable `else ()`.
- R-G11 [clarity] `app.py:59` `ONBOARDING` imported unused; `"porting"`
  literal at 256, 289, 477 versus `PORTING` at 130, 370. **[2d]**
- R-G12 [clarity] `app.py:180-183` `RunRequest` lacks the
  `extra="forbid"` that `SubmitRequest` has.
- R-G13 [clarity] `app.py:336-348` transient error reported as lost build;
  `healthz()` called twice. **Step 1c.**
- R-G14 [separation] tests: six hand-built `RegionConfig` literals
  (`test_app.py:32`, `test_run.py:40`, `test_sese_check_dispatch.py:92`,
  `test_golden_path_dispatch.py:64`, `test_onboarding_dispatch.py:39`,
  `tests/test_client.py:26`); no `conftest.py`; `test_evidence.py:12` and
  `test_onboarding_dispatch.py:21` import private helpers from other test
  modules. Add `tests/gateway/conftest.py`; move `reference`/`ReferenceBuilder`
  to `fakes.py`.
- R-G15 [clarity] `app.py:113-120` comment says four harness checks (five
  call sites); `app.py:744` comment says `strategy` is loaded below (it is
  loaded above at 642).

### Components, analyzers, capture (`equivalent/components/`, `analyzers/`, `capture/`)

Signatures as found:

| component | inputs | output |
|---|---|---|
| `manifest_check.check` | `repo_dir, ref` | `{verdict, detail}` |
| `sese_check.check` | `repo_dir, ref, spec_path, strategy` | `{verdict, detail, allow_globs}` |
| `build_replay.check` | `repo_dir, ref, region_id, tree_sha, strategy, manifest, builder` | `{verdict, detail}` |
| `run_replay.check` | `region_id, tree_sha, strategy, manifest, visible_cases, builder` | `{verdict, detail}` |
| `sanitize.check` | same as run_replay | `{tool: {verdict, detail}}` |
| `property_check.check` | `region_id, tree_sha, manifest, visible_cases, builder, *seed, max_examples` | `{verdict, detail}` |
| `regression.check_visible` | `store, tree, oracle` | `{verdict, detail}` |
| `regression.check_holdout` | `region_id, tree_sha, strategy, manifest, oracle, builder` | `{verdict, detail}` |
| `program_regression.check` | `store, baseline_tree, region_id, tree_sha, manifest, builder` | `{verdict, detail}` |
| `timing.check_port` | `store, tree, region_id, tree_sha, manifest, builder, repeats` | `{verdict, detail}` |
| `timing.check_baseline` | `store, repo_dir, region_id, baseline_tree_sha, manifest, baseline_strategy, builder, repeats` | `{verdict, detail}` |
| `harness_build.check` | `repo_dir, ref, region_id, tree_sha, strategy, baseline_strategy, builder` | `{verdict, detail}` |
| `harness_capture.check` | `store, repo_dir, ref, region_id, tree_sha, baseline_strategy, builder` | writes sets |
| `harness_replay.check` | `store, tree, repo_dir, ref, region_id, tree_sha, baseline_strategy, builder` | `{verdict, detail}` |
| `harness_determinism.check` | same 8 | writes sets |
| `harness_property.check` | same 8 `, *seed, max_examples` | `{verdict, detail}` |
| `harness_self_check.check` | same 8 `, *limit` | `{verdict, detail}` |
| `harness_timing.check` | `store, repo_dir, ref, region_id, tree_sha, baseline_strategy, builder` | writes set |
| `original_check.check` | same 8 `, original_reference_path` | writes artifacts |

- R-C1 [separation] `timing.py:149` function-local import of
  `program_regression` breaks a module cycle with
  `program_regression.py:49`; `check_port` re-runs the regression
  comparison undocumented in `predicates.py`. **[dissolves in 2d]**
- R-C2 [separation] `harness_timing.py:58` vs `timing.py:162`: two
  implementations of time-and-store; detail keys diverged (`datasets`
  versus `program_set`, `app.py:113` versus `app.py:901`); validation
  rigor differs (`timing.py:102-105` versus `harness_timing.py:107`).
  **[dissolves in 2d]**
- R-C3 [clarity] `manifest_check.py:66` and `harness_timing.py:42` both
  define `_fail` with incompatible signatures; nine failure spellings
  across modules. **[dissolves in 2c]**
- R-C4 [clarity] `sese_check.py:94` top-level `allow_globs` return is dead;
  consumer is `submit.py:164-174` reading `detail["allow_globs"]`; only
  `test_sese_check.py:89` keeps it alive. **[2c]**
- R-C5 [clarity] `harness_capture.py:51` `captured_sets`' `predicate_type`
  never passed by any of five callers. **[2c]**
- R-C6 [separation] components hardcode predicate names (`timing.py:134,138`,
  `regression.py:28`) and an action name (`program_regression.py:60-63`
  `BASELINE_ACTION`, used in error text at 99). Import from
  `ledger/predicates.py`; let the gateway phrase "run X first".
  **[dissolves in 2c]**
- R-C7 [separation] `timing.py:50` vs `harness_timing.py:70`: same
  missing-target condition is an error in one, a verdict in the other, with
  copy-pasted text; same for `harness_capture.py:155` versus
  `harness_determinism.py:96`. **[2d]**
- R-C8 [clarity] duplicated constants: `TIMING_ROLE` (`timing.py:43`,
  `harness_timing.py:36`), `REPLAY_ROLE` (`harness_replay.py:40`,
  `harness_self_check.py:48`), `VISIBLE` (`harness_capture.py:45`,
  `harness_property.py:35`, `harness_self_check.py:51`),
  `VARIABLE_BANDS`/`FILE_BANDS` (`manifest_check.py:47-48`,
  `program_regression.py:56`, `harness_self_check.py:55`),
  `PROGRAM_SET_KEY` (`timing.py:46`, `program_regression.py:61`). One
  `components/names.py`. **[partly 2b, 2d]**
- R-C9 [clarity] three builder-response validators repeat the integer
  guard (`property_check.py:108,145`, `harness_self_check.py:200,203`,
  `sanitize.py:92`). **[dissolves in 2f typed responses]**
- R-C10 [clarity] long checks interleaving prepare/validate/assemble:
  `harness_self_check.py:148` (108 lines), `property_check.py:58` (111),
  `harness_determinism.py:83` (92), `harness_capture.py:138` (91),
  `original_check.py:43` (75, four levels of nesting, `report["pass"]` set
  after append at 95/115).
- R-C11 [separation] `program_regression.py:70` third way to read the
  tolerance policy alongside `tree_manifest.manifest_and_policy:47` and
  `harness_self_check.bands_of:93`. **[2a folds tree_manifest into Tree]**
- R-C12 [separation] `sese_check.py:51-95` subprocess and verdict logic in
  one function; tests write real Fortran for every case. Split
  `_analyze` from `verdict_for`.
- R-C13 [separation] components write to the store (`harness_capture.py:218`,
  `harness_determinism.py:123`, `timing.py:225`, `harness_timing.py:120`,
  `original_check.py:39`); `harness_determinism` stores the second set
  before comparing (123 versus 126). **[dissolves in 2c]**
- R-C14 [clarity] `tests/components/` has no `conftest.py`;
  `STRATEGY_DIR` in nine files; `_baseline_strategy` in four;
  `_captured` in four (three identical).
- R-C15 [clarity] `tests/fakes.py:136` `fixture_case` duplicates
  `encode_case(fixture_arrays(offset))`; `FakeBuilder.timing_outputs`
  never uses `run`.

### Ledger, schemas, CLI (`equivalent/ledger/`, `manifest/`, `strategy/`, `reference/`, `cli/`, `client.py`)

- R-L1 [separation] `status.py:120` `accepted` decided in three places
  (`app.py:476-485`, `cli/main.py:245-249`) with different
  `context_verified` definitions. **[dissolves in 2e]**
- R-L2 [separation] `cli/session.py:383` `_accepted_after` is a fourth
  evaluator ignoring `claim_matches_context`. **[dissolves in 2e]**
- R-L3 [separation] `cli/promote.py:280` promotion gate and file writing
  under `cli/`; imports `components.harness_capture` and four gateway
  modules. Move to `equivalent/promote.py`. **[2b]**
- R-L4 [separation] `strategy/schema.py:93` loader accepts unknown keys,
  string `allow_globs` (becomes one-character globs), non-int `version`;
  fails with `KeyError`/`AttributeError`/`TypeError` on malformed files.
  Run through `manifest.schema._check_keys` (shared); validate list
  fields; every failure a `ValueError` naming file and field.
- R-L5 [separation/clarity] `reference/schema.py:55` one 81-line function;
  `_keys` (line 18) has no location argument so five call sites raise the
  identical message; `runs`/`outputs` are raw dicts; nine-argument
  positional constructor at 134. Split per section; thread `where`;
  dataclasses for `Run`/`Output`.
- R-L6 [separation] `store.py:34` `ContextVar` and `claim_matches_context`
  (170) make the store carry evidence policy; `test_store.py:118-126`
  pins the implicit behavior. **[dissolves in 2c/2e]**
- R-L7 [clarity] `cli/main.py:117` `_open_region` returns an unnamed
  8-tuple, unpacked at 236 and 259. **[2e]**
- R-L8 [clarity] `store.py:221` dead docstring. **Step 1e.**
- R-L9 [clarity] `manifest/schema.py:406` `_normalized` duplicates
  `ledger/subjects.py:52-59`; `_matches` (413-431) and `Strategy.allows`
  (`strategy/schema.py:67-83`) disagree on `./` and `**/`. One
  `_normalize_path`, one `glob_matches`; decide `**/` everywhere or nowhere.
- R-L10 [separation] `client.py:1` docstring's reason for existing is
  false (CLI does not use it; pi extension is TypeScript); `TIMEOUT` at
  line 15 copies `backend_client.py:24`. Importers are the two `deploy/`
  walkthroughs only. No logic overlap with `backend_client.py`.
- R-L11 [clarity] element types in comments (`records.py:56`,
  `cli/session.py:113-115`, `manifest/schema.py` `Build.targets`,
  `strategy/schema.py` `languages`) despite `from __future__ import annotations`.
- R-L12 [clarity] `store.py:115` every query re-parses `claims.jsonl`; a
  19-row `compute_status` parses up to 38 times. Cache keyed by
  size+mtime, invalidated on append.
- R-L13 [clarity] `tests/fakes.py:136` duplicate (same as R-C15).
- R-L14 [clarity] `cli/main.py:258` `history` runs full region setup and
  discards it; `return 1` at 287 unreachable.
- R-L15 [clarity] `manifest/schema.py:370` loop variable shadows `path`.

### Services (`services/builder/`, `services/oracle/`, `deploy/`, `programs/*/harness/`)

Stages in `services/builder/stages.py`:

| stage | lines | depends on |
|---|---|---|
| `build` | 368–499 | `contract.compile_records`, executor, writes workspace and `.artifacts`, freezes tree |
| `run` | 606–680 | workspace via `_executable`; `_run_profiled` nsys evidence |
| `capture` | 720–788 | `_executable`; `case.json` convention |
| `sanitize` | 791–844 | `_executable`; `compute-sanitizer` in job image |
| `properties` | 916–1032 | `_executable`; `/opt/harness/harness_properties.py`; pytest |
| `mutate` | 1172–1268 (+`score_mutant` 1064–1132, `_score_all` 1272–1307) | tree on disk, `build_env`, `mutate.py`, own make invocation |
| `time_run` | 1310–1434 | `_executable`; writable copy of frozen tree |

- R-S1 [clarity] `stages.py:412` build timeout raises `UnboundLocalError`.
  **Step 1a.**
- R-S2 [separation] `stages.py:646,752,823` each stage catches a different
  subset of executor exceptions; `sanitize`'s `FileNotFoundError` branch
  is unreachable in production. **[dissolves in 2f]**
- R-S3 [separation] `stages.py:42` `_JOB_RUNNER` seam at seven sites
  makes the tested path differ from deployed (no ownership change, no
  freeze, no audit evidence, different `FC`). **[dissolves in 2f]**
- R-S4 [separation] `stages.py:1082` `score_mutant` re-implements the make
  invocation with no shim log, compile records, or artifact identity.
  **[dissolves in 2f]**
- R-S5 [separation] `contract.py:1` is a compiler-log reader; the actual
  request shape is in `app.py:41-146` and `backend_client.py:44-150`
  docstrings; response shape nowhere (`build()` has six return shapes).
  **[dissolves in 2f]**
- R-S6 [clarity] `stages.py:936,947,989` same eight-key zero-count literal
  three times in `properties()`.
- R-S7 [clarity] `stages.py:148-179` `_run`, `_run_audited`, `_run_profiled`
  three copies of one dispatch. **[dissolves in 2f]**
- R-S8 [clarity] `stages.py:530` `kernel_launches()` and `LAUNCH_LINE`
  dead in production; kept alive by `test_run_replay.py:10` whose comment
  is stale. Delete.
- R-S9 [clarity] `stages.py:243` identity verification written twice
  (inline and `_identity_matches` at 343). **[dissolves in 2f]**
- R-S10 [separation] `programs/kh2d/baseline/harness/properties.py:8` and
  tsunami's `states()` (69), `test_one_step_is_the_same_step_twice` (93),
  `_same_bits` (86, duplicating `mutate.py:284 bitwise_equal`) re-write the
  corpus draw and determinism property per code. Add `case_strategy()`,
  `same_bits()`, and a determinism property to
  `services/builder/harness/harness_properties.py`.
- R-S11 [clarity] `preflight.py:98` `qualify()` is 280 lines of eight
  identical try/except blocks (116, 145, 163, 197, 213, 242, 257, 270);
  fallback answer at 359 omits `gpu_checks`.
- R-S12 [separation] `services/oracle/app.py:208` calls
  `cmp._tolerance_problem`, a private name across the image boundary.
  Promote to `compare.tolerance_problem`.
- R-S13 [separation] `app.py:69,116,126` `cases`, `replay_target`,
  `bands`, `env` are bare `dict`; `stages.mutate` takes ten positionals.
  **[dissolves in 2f]**
- R-S14 [separation] `stages.py:1282` pool relies on fork. **Step 1d.**
- R-S15 [clarity] `audit-run.py`, `profile-run.py` cannot be imported by
  name; `test_builder_executor.py:11-15` uses `importlib` machinery;
  `profile-run.py`'s SQLite kernel-count query has no unit test. Rename to
  `audit_run.py`/`profile_run.py`; update `executor.py:219,222` and
  `Dockerfile:44`.

### Cross-cutting (import graph as of `c9fb867`)

```
deploy                -> equivalent/cli, equivalent/client, equivalent/manifest
equivalent/analyzers  -> (nothing)
equivalent/client     -> (nothing)
equivalent/capture    -> equivalent/manifest
equivalent/manifest   -> equivalent/ledger
equivalent/reference  -> equivalent/ledger
equivalent/strategy   -> equivalent/ledger
equivalent/ledger     -> equivalent/capture
equivalent/components -> equivalent/gateway, ledger, capture, manifest, strategy, reference
equivalent/gateway    -> equivalent/components, ledger, capture, manifest, strategy, reference
equivalent/cli        -> equivalent/gateway, ledger, components, manifest, strategy
services/builder      -> equivalent/capture   (ImportError fallback only)
services/oracle       -> equivalent/capture   (ImportError fallback only)
```

Violating edges: components→gateway (19 statements, 15 modules);
gateway→components (`app.py:35,54`); cli→gateway internals
(`cli/main.py:38-40`, `cli/promote.py:32-35`, `cli/session.py:25`);
deploy→`cli.promote` constants (`deploy/onboard_walkthrough.py:33`);
package cycle capture→manifest→ledger→capture (`capture/npy.py:29`,
`manifest/schema.py:37`, `ledger/capture_sets.py:29`). Module cycle:
`components.timing` ↔ `components.program_regression`.

- R-X1 **[2a]** components import `gateway.submit` for pure functions.
- R-X2 **[2c]** nineteen signatures, no shared context.
- R-X3 **[2c]** `"verdict"` literal 66 times in components; gateway reaches
  into detail by key at `app.py:819,850,868-869,901,1002`.
- R-X4 **[2b]** capture/manifest/ledger cycle.
- R-X5 **[2f]** builder wire contract in three copies, no parity test
  (contrast `test_builder_contract.py:226`).
- R-X6 **[2f]** `FakeBuilder` 33 knobs vs `FakeOracle` 3; 19 of 55 test
  files import it.
- R-X7 **[2b]** `programs/<code>/` layout declared in `cli/promote.py:52,56`,
  `gateway/config.py:87`, `services/oracle/app.py:70-71`.
- R-X8 **[2b + parity test]** `("visible", "holdout")` declared at
  `manifest/schema.py:73`, `services/oracle/app.py:63`,
  `harness_capture.py:45`, `regression.py:61`; the oracle re-reads the
  manifest as raw YAML (`app.py:129-160`) with no drift test.
- R-X9 **[2b]** `cli/main.py:38-40` contradicts `acceptance.py:22-24`;
  `gateway/evidence.py` has no gateway dependency.
- R-X10 **[2b]** `deploy/onboard_walkthrough.py:33` imports CLI privates.
- R-X11 **[2f]** services' tests under `equivalent/tests/services/`.
- R-X12 **Step 1f** `record_claim` does not validate `predicateType`.
- R-X13 [clarity] `client.py:15` and `backend_client.py:24` identical
  `TIMEOUT`.
- R-X14 [clarity] `pi-extension/src/status.ts:28-31` re-declares
  `FINISHED_WORD`; have `GET /status` return the word.

### Leave alone

`table.py` as declarative policy with `test_run.py:235` pinning it to the
predicate registry. `backend_client.py` decision-free. `main.py` the only
environment reader. `config.py`'s one `_check_keys` for all sections.
`ledger/status.py:requirement_status` rendered once for `/status` and
`/run` refusals. Gateway tests driving HTTP and importing only
`create_app` and `config_hash`. `contract.py`'s compile-log reader as a
pure function pinned to `manifest.schema._matches`. `executor.py` failing
closed with credential scrubbing and minimal mounts. The oracle's
held-out asymmetry in one place and length-framed identity hashing. The
"not ready" 409 design in `_what_is_missing`. `time_run` collecting
outputs per repetition. `mutate.py` as pure functions. `deploy/hostconfig.py`
and `deploy/seed.py` refusing rather than guessing. Append-only enforced
by flock with the torn-line reader. `hash_files` length-delimiting.
`Claim.from_dict` rejecting unknown fields. `acceptance.py` as data with
reasons beside it. `predicates.py` separating reuse from receipt policy.
`analyzers/check_sese.py` as a true leaf. `compare.py` as one comparator
copied into both images with the reason written down. Test names as
sentences throughout.

### The review prompts, for Step 3

Five Opus agents, each told to read every file in scope, verify each
finding against the code before reporting, prefer fewer grounded findings,
skip style trivia, and report an overall assessment, a ranked list of at
most ~15 findings with `path:line` anchors tagged [separation] or
[clarity], and a factual "done well" list. Scopes: (1) `gateway/` and its
tests plus `fakes.py`; (2) `components/`, `analyzers/`, `capture/` and
tests, with a signature table; (3) `ledger/`, the three schema modules,
`cli/`, `client.py` and tests; (4) `services/`, `deploy/*.py`,
`programs/*/harness`, `programs/*/baseline/harness`, and
`tests/services/`, with a stage table; (5) cross-cutting: import
adjacency by AST walk, violating edges and cycles, shared vocabulary
declared more than once, contract duplication, what `fakes.py`'s width
says about real interfaces, and whether the package layout should change.
