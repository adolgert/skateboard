# What a recorded claim establishes

Skateboard separates a submitted program from the tools judging it. The
deployment owner reviews those tools and policies; the coding agent cannot
turn its own terminal output into a ledger claim. The ledger is an audit
record, not a proof of scientific correctness.

## Evidence identity

Schema version 2 records SHA-256 subjects and materials. File sets have an
explicit domain, file count, and length-prefixed paths and contents. A file
named `a` containing `bc` differs from `ab` containing `c`. Duplicate normalized
paths are rejected.

The current context includes strategies, manifest, tolerances, visible dataset,
original-reference contract and source snapshot, and trusted Python checker
implementation. Runtime claims also name the executor identity; porting
comparisons name the oracle identity. Job images resolve to immutable image
IDs. Oracle identity covers comparison implementation and reference data.
Changes invalidate dependent claims; old claims remain readable history.

Claims that depend on a build include its complete set of executable digests.
The gateway checks protected artifact records before reusing a build. A lost
workspace can be rebuilt automatically only if the executable bytes reproduce;
a different build requires new dependent claims. CLI status and promotion use
the same digest requirements when reading the ledger offline.

A run resolves its Git ref to a commit before reading the tree. SESE screening
is repeated for each changed candidate. Later spec edits cannot widen the
reviewed allow-list. Onboarding and porting actions have enforced phase
boundaries.

Offline review requires reviewed backend identity pins in the region
configuration. This verifies claims against the pins, without establishing
that services are running now. Live status checks identities and readiness.
The ledger CLI without deployment configuration is advisory.

## Observations and assumptions

| Check | Observation | What still needs review |
| --- | --- | --- |
| Original comparison | Preserved original and onboarded CPU program agree on declared runs and outputs; each repeats deterministically | Original provenance, adapters, runs and outputs |
| Build | A protected observer saw compiler processes and arguments; executables have recorded digests | Build recipe, dependencies and final executable provenance |
| GPU execution | A protected Nsight report contains GPU kernel activity | Whether the intended scientific work was offloaded, kernel attribution and transfers |
| Sanitizers | Required sanitizers succeeded on selected cases | Tool and case coverage; racecheck does not cover all possible device races |
| Numerical regression | Arrays agree under reviewed bands, with shape/dtype checks and finite floating-point values | Scientific validity, calibration and representativeness |
| Properties | The process completed successfully and a protected observer saw the bound replay execute | Assertions and submitted test counters; an example ceiling is not an observed example count |
| Mutation self-check | The generated campaign was classified, a mutant was caught, and the tolerance-gap criterion passed | Survivors, operator coverage and suitability of the criterion |
| Timing | `harness/times` checks repeatable outputs; `timing/baseline` records repeated durations and retains final outputs; `timing/port` compares every measured repetition to the baseline outputs | Baseline repeatability and validity, contention, workload and meaningful speedup |

Build tracing proves that an allowed compiler ran with observed arguments.
It does **not** prove that the final executable derives exclusively from those
invocations: a submitted Makefile could overwrite the output after a legitimate
compile. Review build recipes during onboarding. A stronger future claim needs
a trusted build plan and independently tracked compiler inputs and outputs.

The SESE scanner handles a conservative source subset. It does not derive a
complete call graph, alias analysis or effect closure. Scientists must inventory
hidden state and review the replay boundary. Pointers, module variables and
device mappings require more than a list of array values.

## Execution boundary

The builder supervises disposable jobs through Docker. Jobs have no service
token or network, a read-only image, and only the source and data mounts needed
by that invocation. Compiler tracing and GPU profiling write to protected
evidence directories; submitted processes use an unprivileged UID. There is
no production fallback to host execution.

The Docker daemon, supervisor, image, kernel and device runtime belong to the
trusted computing base. This is container isolation, not a VM boundary or a
defense against kernel or driver vulnerabilities. Qualify it using
[the deployment instructions](../deploy/README.md).

Private held-out run diagnostics are withheld. Repeated pass/fail queries
still leak information and can support adaptive overfitting; the implementation
has no enforced query budget. Preserve held-out cases for final confirmation
and retain independent scientific validation.

See [the coworker handoff](coworker-handoff.md) for applying these claims to a
new codebase and recording the remaining obligations.
