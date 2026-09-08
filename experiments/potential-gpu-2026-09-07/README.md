# Complete GPU workflow: 7 September 2026

The repository-owned charged-cloud application completed onboarding, promotion,
and GPU port acceptance through the public gateway client on an actual NVIDIA
RTX 4000 Ada Generation (20 GB). All three implementations passed the complete
configured workflow. This is a scripted exercise of an agent's gateway actions;
it does not measure a language model's ability to discover a port.

## Results and criterion

The frozen manifest requires **median CPU time / median candidate time >= 1.10**,
with at least five finite, positive samples for each implementation. Each run
computes 30 successive potential solves over 32,768 points, in single precision
on both CPU and GPU. Outputs feed the next solve. The CPU reference uses
`nvfortran -O2 -stdpar=multicore` and `OMP_NUM_THREADS=8` on an AMD Ryzen 9 5900X
(12 cores, 24 logical CPUs). This is a configured eight-thread baseline, not a
search for the fastest possible CPU implementation.

| Implementation | CPU median | Candidate median | Speedup | Result |
| --- | ---: | ---: | ---: | --- |
| NVIDIA Fortran `DO CONCURRENT` | 1.286 s | 0.839 s | **1.53×** | `ACCEPTED` |
| Fortran + CUDA C++ | 1.352 s | 0.905 s | **1.49×** | `ACCEPTED` |
| Fortran + C++ loader + handwritten PTX | 1.396 s | 0.972 s | **1.44×** | `ACCEPTED` |

These are end-to-end isolated-job wall times: Docker creation/start/removal,
application startup, transfers, computation, and output are included. Workspace
copying precedes the timer. Every timed candidate output passed comparison with
the CPU reference. Measurements were taken sequentially on a desktop host with
other services present; GPU exclusivity, a confidence interval, and performance
on other machines are not established. Each variant has its own baseline
measurement. The GPU driver was 580.159.03, Docker 29.1.3, NVIDIA Fortran/C++
25.9-0, and nvcc/ptxas 13.0.48.

## What actually ran

1. Preserved the original application before adding capture/replay and NPY
   adapters. Two original-reference configurations compared raw output bytes,
   including a cloud evolved through three calls.
2. Qualified the production disposable-job boundary, compiler observer, actual
   GPU profiling, and all three sanitizers. Pinned the executor identity.
3. Passed all nine onboarding actions, including deterministic replay, timing,
   independent original comparison, the complete mutation campaign, and the
   property module. Promoted the exact reviewed tree with the operator command.
4. Baked the promoted held-out captures into the oracle and pinned its identity.
   Each port bootstrapped its region specification before submitting source edits.
5. Rejected a CPU-only candidate because its profile contained no CUDA kernel
   table. Rejected a GPU candidate with softening changed from 0.125 to 0.25 at
   visible numerical comparison, after GPU execution and sanitizers passed.
6. Accepted the corrected Fortran, CUDA C++, and PTX candidates after all porting
   actions: source screening, observed builds, GPU replay, memcheck/racecheck/
   initcheck, visible/property/holdout comparisons, whole-program comparison,
   baseline and candidate timing, and the performance gate.
7. Changed only the retained PTX candidate's external `potential.cubin` bytes.
   Acceptance became invalid while executable bytes stayed unchanged. Restoring
   the exact module bytes restored valid status. See
   [the module fault-injection record](ptx/module-tamper.json).

The build records observe nvfortran and nvcc for CUDA C++, and nvfortran, nvc++,
and ptxas for PTX. The latter assembles a handwritten `.ptx` file to an external
`.cubin`; its Driver API loader resolves the module beside the frozen executable.
It links against the SDK's driver stub in GPU-free build jobs and loads the
actual driver in GPU execution jobs. Runtime-module digests join executable
identities in dependent claim materials.

## Limits exposed by this run

The mutation campaign classified all 34 generated mutants: 32 were killed,
with no incomplete results or tolerance gaps. Two mutants changed a loop's
lower bound from 1 to 0 but left the captured outputs unchanged. The tool labels
these `EQUIVALENT`; that label is empirical, not a proof. They remain unresolved
out-of-bounds coverage obligations. The configured adequacy policy permits
these survivors, so `ONBOARDED` must not be read as complete fault detection.

Visible captures contain two successive states with 129 points; held-out
captures use 257 points. Properties independently check the equation with NumPy
and reverse-charge linearity on small inputs. Replay tolerance is absolute or
relative 1e-4, or 16 ULPs; whole-program absolute tolerance is 5e-4. Preliminary
CPU/GPU rounding measurements informed these bands before acceptance. The
application is an engineering fixture, not an externally validated physical
solver; other sizes, extreme values, precision requirements, and scientific
outputs need review.

Foreign sources are explicitly opaque to the Fortran scanner. Compiler traces
and artifact identities do not reconstruct includes, complete link provenance,
or prove that an arbitrary loader used only declared bytes. A trusted build/load
plan would be needed for those stronger claims. The exercised code uses a
Fortran wrapper and application-specific adapters; arbitrary directories still
require that onboarding work.

## Reproduce and inspect

Repository validation passed: 1,037 Python regression tests plus all five real
Docker/GPU qualification tests (1,042 total), 40 extension tests, and TypeScript
checking. The regression run reported 14 multiprocessing `fork` deprecation
warnings. Commands:

```sh
.venv/bin/pytest -q --ignore=services/tests/test_qualification.py -o faulthandler_timeout=60
.venv/bin/pytest -q services/tests/test_qualification.py
npm --prefix pi-extension test
npm --prefix pi-extension run typecheck
```

From the repository root, with Docker and a compatible NVIDIA GPU:

```sh
.venv/bin/python deploy/gpu_pilot.py --state deploy/state/potential-gpu-reproduction
```

Use an unused state directory. The script builds isolated service images and
removes its own containers and networks afterward, retaining its work volume
and state. The full recorded state for this run is
`deploy/state/potential-gpu-final/` (ignored by Git).

This archive contains [qualification](qualification.json),
[compiler versions](compiler-versions.json), [all client receipts](receipts.json),
[performance samples and image identities](completed.json), per-phase status and
complete ledgers with their retained output artifacts. Configuration files name
the original container mount paths and are reference records, not ready-to-run
host configurations. `source-trees.json.gz` maps each evidence tree hash to its
UTF-8 source files, including failed candidates; [the index](source-index.json)
maps per-phase Git commits to those hashes. Every exported tree was rehashed
against its key. [Source provenance](source-provenance.json) hashes image-build
inputs from the working tree based on commit `50b0b51`; the run includes the
changes implemented for this task. `SHA256SUMS` covers the archive files.

See [the application](../../programs/potential/README.md) and
[the mixed-language contract](../../docs/mixed-language.md) for implementation
and adaptation details.
