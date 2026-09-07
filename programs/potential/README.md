# Charged-cloud GPU demonstration

The [recorded GPU run](../../experiments/potential-gpu-2026-09-07/README.md)
completed every gate for all three implementations and retains the raw evidence.

This repository-owned application computes a softened electrostatic potential:

`phi[i] = sum(q[j] / sqrt((x[i]-x[j])² + (y[i]-y[j])² + (z[i]-z[j])² + 0.125))`.

`original/` is the application before adding capture/replay and NPY output.
`baseline/` preserves that equation and adds those adapters. The original
comparison checks raw `phi.bin` bytes for two distinct configurations, including
an evolved cloud. This is an engineering demonstration, not a scientific
validation of a physical simulation.

The calculation uses single precision on both CPU and GPU. Its CPU baseline is
NVIDIA Fortran `-O2 -stdpar=multicore`, with eight threads for timing. Each timing
run evolves 32,768 charged points through 30 solves, feeding each answer into
the next configuration. Startup, transfers, and output are included. The
current harness additionally measures Docker job setup and teardown. The fixed
performance requirement is median CPU time / median candidate time >= 1.10,
using at least five runs of each. Every timed output must pass the numerical
comparison. This criterion measures this host and workload; it is not a
statistical confidence bound or a hardware portability guarantee.

Visible cases have 129 points and held-out cases 257, exercising partial blocks
and multiple blocks. Each captures two successive states. A property test
checks the equation independently with NumPy and verifies charge linearity.
The frozen replay policy permits maximum absolute or relative error 1e-4, or
16 ULPs. The whole-program absolute band is 5e-4, allowing accumulated rounding
over 30 solves; preliminary CPU/GPU differences were at most 1.08e-4.
The property checks use 1e-5 on their smaller inputs. Review these bands before
adapting the example to scientific use.

Three implementations share the same Fortran interface:

- Standard Fortran `DO CONCURRENT`, compiled with `stdpar_managed`.
- `ports/potential.cu`: CUDA C++ behind an `ISO_C_BINDING` wrapper.
- `ports/potential.ptx`: handwritten PTX, assembled to `potential.cubin` and
  loaded by `ports/potential.cpp` through the CUDA Driver API. Its context and
  module persist across calls; inputs and outputs still transfer each call.

The PTX module is a declared runtime artifact. `when_language: ptx` makes it
mandatory for PTX builds, while allowing the pristine Fortran CPU baseline to
build without it. Its loader resolves the module beside the immutable
executable, not from the writable working directory. Foreign sources are
explicitly listed as `opaque_sources`: the Fortran analyzer does not establish
their control flow or effects.

Run the complete demonstration from the repository root:

```sh
.venv/bin/python deploy/gpu_pilot.py --state deploy/state/potential-gpu
```

The driver requires Docker and an NVIDIA GPU compatible with the supplied
`cc89`/`sm_89` strategies. It builds isolated service images and qualifies actual
profiling and sanitizers, onboards from the original, promotes through the
normal operator command, and runs all porting gates. It first demonstrates
rejection of a CPU-only candidate and a numerically wrong GPU candidate.
It then checks the correct Fortran, CUDA C++, and PTX implementations. The
driver scripts the agent's actions; it does not call a language model.

Use a new state directory for each run. Receipts, status, qualification, logs,
and append-only ledgers remain there after the demonstration containers and
networks are removed. The dedicated work volume is a bind mount beneath that
state directory. Existing deployments are not reused or reconfigured.

Submission overlays the initial baseline and retains omitted files. The driver
therefore keeps the original root-level sources alongside the new `src/` and
`harness/` files; the reviewed Makefile compiles the latter. Promotion requires
the working copy to contain exactly the reviewed tree, including those retained
sources.
