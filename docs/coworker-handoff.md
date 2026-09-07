# Applying Skateboard to a new code

This is a pilot handoff for a deterministic Fortran application on an NVIDIA
GPU. The ledger records checks on particular source bytes and reviewed inputs.
`ONBOARDED` means the configured onboarding checks passed; `ACCEPTED` means
the configured porting checks passed. Neither label establishes scientific
validity, test completeness, or support for all Fortran 2008 features.

The deployment owner controls the gateway, checker images, strategies,
original reference and promoted datasets. The coding agent controls its working
copy. Submitted Makefiles, executables and property modules are untrusted and
must run through the disposable builder. Host-only unit tests do not qualify
that deployment boundary.

## 1. Establish the supported boundary

Choose one procedure and list all state it reads and writes, including module
variables, `SAVE` variables, pointer targets, allocatable components, random
number state and effects of called procedures. Record array lower bounds and
aliasing assumptions. Decide which effects the capture driver can restore.
Single entry and exit alone do not establish this state boundary.

Start with serial CPU execution of a deterministic region, ordinary numeric
arrays and explicit scalar arguments. MPI collectives, coarrays, asynchronous
I/O, external callbacks, opaque library state and unrecorded persistent device
state require additional adapters and review. The scanner is conservative
source screening, not a compiler proof of control flow or effects. If it
rejects a construct, inspect its diagnostic and narrow the boundary or improve
the checker; do not bypass the claim.

Preserve the data structures for this first port. Prefer a region large enough
to include several calls or time steps when residency and transfer costs matter.
An isolated one-call replay cannot verify correctness of persistent mappings
across a production call sequence.

## 2. Preserve the original before the agent edits it

Keep an exported original source snapshot, without `.git` or symlinks, outside
the agent working copy:

```text
programs/mycode/
  original-reference.yaml
  original/                 # original sources and reviewed build/output adapter
  baseline/                 # separate working baseline for onboarding
  manifest.yaml
```

The original must build with the baseline strategy through the same build
contract (`FC`, `FFLAGS`, `LDFLAGS`). If it needs a small output adapter, review
that adapter against the original program before onboarding. Document the
upstream revision and the adapter in `provenance`.

Use [the reference contract example](examples/original-reference.yaml). Its
paths are relative to the contract file, and its source snapshot must be
beneath that directory. Add to the gateway configuration:

```yaml
codes:
  mycode:
    manifest: mycode/manifest.yaml
    original_reference: mycode/original-reference.yaml
```

Each reference run names arguments for the original and onboarded timing
program and maps their output files. `bytes` compares exact file bytes;
`array_exact` compares finite numeric NPY arrays with matching shapes and
dtypes; `array_tolerance` uses explicitly reviewed absolute/relative/ULP bands.
Each program must produce identical bytes on two repeated runs even when the
comparison between programs permits tolerance. NaN and infinity fail array
comparison. Include every output that matters to the chosen boundary.

Use several meaningful configurations: a small case easy to inspect, odd
rectangular dimensions, unequal spatial scales, boundary conditions, and later
states reached after repeated calls. Different arguments do not by themselves
establish independent or adequate tests. Record why each case matters.

The gateway hashes both the contract and all original source bytes. Changing
either invalidates dependent claims. Missing references permit exploration,
but cannot produce `ONBOARDED`.

## 3. Qualify the machine, then onboard

Commit the new code's baseline and manifest before starting: deployment seeding
reads source bytes from Git `HEAD`. Use fresh deployment state for a different
code or baseline. The seed step refuses stale files instead of mixing two
applications. Preserve the previous working copy, ledger and state before
setting up another code; restarting an existing gateway does not replace its
baseline repository.

The host needs Docker with support for volume subpaths and the NVIDIA container
runtime. The builder job image needs the configured compiler, build tools,
Compute Sanitizer, Nsight Systems, Python, pytest and Hypothesis. External
libraries must be installed in that reviewed image. Builds have no network.

Follow [installation](pi-install.md) and the deployment qualification described
in [the deployment README](../deploy/README.md). Verify actual GPU execution and
the disposable-job boundary on the target machine before treating its claims
as reviewed evidence. A green Python test suite is insufficient for that step.
The supervisor holds the Docker socket and is part of the trusted computing
base; submitted jobs must never receive that socket.

After qualification, record the reviewed SHA-256 value reported by the
builder `/healthz` response in the onboarding region configuration:

```yaml
executor_identity: <builder executor_identity, 64 lowercase hex digits>
```

After promotion, rebuild the oracle with the promoted captures and policy.
Before porting, record its `/healthz` `oracle_identity` value in the porting
region's `oracle_identity` field, alongside the executor pin. An oracle without
reference captures is intentionally unready and has no identity to pin.

The gateway refuses a backend that disagrees with these pins. Offline status
requires an executor pin and, for porting, an oracle pin; promotion requires
the executor pin. Rebuilding a checker image or changing the GPU environment
may change its identity. Review and update the pin, then re-run checks. The
offline command verifies evidence against those configured identities; it
does not contact the services to establish that they are running now.

Follow [onboarding](onboarding.md). After `harness_build` and `harness_timing`,
run `harness_original`. The independent original comparison must pass before
promotion. Review the outputs named by the contract, not just the final label.
The full mutation campaign must finish; a limited campaign cannot establish
the adequacy claim. A surviving mutant is an unresolved test obligation, not
a proof that the mutant is semantically equivalent. Property checks require
at least one passing test and a protected observation of the bound replay
executable running; an empty or skipped module cannot pass. The observer does
not establish how many Hypothesis examples were tested.

## 4. Review and promote

Use the configuration-aware reviewer command:

```sh
ledger status --config deploy/state/gateway.host.yaml --region-id mycode:onboard
ledger show <region-ledger-directory> <claim-id>
ledger promote --config deploy/state/gateway.host.yaml --region-id mycode:onboard --programs /tmp/reviewed-programs
```

The last command writes a reviewable promotion to a separate directory. Inspect
it before deploying it. Preserve the original snapshot and the region ledger
alongside the promotion. The ledger holds the original comparison's output
artifacts and reference identity; those artifacts are not copied into the
promoted baseline.

A bare `ledger status <directory>` is a historical view. It cannot verify the
current deployment context and cannot report current acceptance. Old schema
claims remain readable but cannot satisfy the new evidence policy. Re-run
checks after upgrading the harness or changing reviewed inputs; do not edit old
claims to give them new materials.

Choose a porting region using the promoted baseline, then follow
[the porting manual](pi-users-manual.md). Keep the oracle's held-out data private
and use it as final confirmation. Repeated pass/fail queries still permit
adaptive learning; this implementation does not enforce a statistical holdout
query budget.

Review the fixed timing workload and CPU thread count during onboarding. A
port's median speedup is recorded by `performance_check` from at least five
baseline and five port samples, for comparing ports later; acceptance does
not depend on it unless the manifest declares `timing.performance.min_median_speedup`.
The timer includes Docker job overhead; see the [evidence contract](evidence-contract.md).
For foreign implementations, review the [mixed-language contract](mixed-language.md),
including explicit opaque-source paths and runtime-artifact declarations.

## 5. Record what the pilot established

Keep a short qualification record with the code revision, reviewer, compiler
and GPU/driver versions, strategy, exact region, capture-state inventory,
case rationales, tolerance calibration and unresolved mutants. Record human
time spent creating adapters as well as speedup.

Before trusting the first port, deliberately introduce a few known faults:
wrong array bounds, a changed module variable, a reduction error, a missing
boundary update and stale device data across two calls. Record which check
rejects each fault and any misses. Check that an unavailable sanitizer,
modified executable and changed policy cannot yield acceptance. Run the
unchanged reference as a control to measure false rejections.

Compute Sanitizer's racecheck covers particular device hazards, including
shared-memory races; it is not a proof of freedom from every GPU race. A
recorded GPU kernel establishes activity during the measured process, not
that the intended scientific work was all offloaded. Review the compiler
report, kernel profile, data movement and whole-program timing together.

The repository's CPU integration fixture exercises two odd rectangular grids
of a third Fortran code and catches a deliberately wrong grid spacing. That
is a regression experiment for the onboarding contract, not evidence that an
unseen application or a target GPU deployment has been qualified.

The [recorded GPU pilot](../experiments/potential-gpu-2026-09-07/README.md)
demonstrates the complete gateway workflow on a fresh charged-cloud application
with Fortran, CUDA C++, and PTX implementations. It retains its measurements,
source snapshots, qualification, rejected attempts, and unresolved mutation
survivors for review.
