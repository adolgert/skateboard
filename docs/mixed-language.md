# Mixed-language builds and runtime artifacts

Strategies can declare `fortran`, `c`, `cxx`, `cuda`, and `ptx` toolchains. Each
entry supplies its compiler and required flags. The builder gives Make these
variables:

| Language | Compiler | Flags |
| --- | --- | --- |
| Fortran | `FC` | `FFLAGS` |
| C | `CC` | `CFLAGS` |
| C++ | `CXX` | `CXXFLAGS` |
| CUDA C++ | `NVCC` | `NVCCFLAGS` |
| PTX assembly | `PTXAS` | `PTXASFLAGS` |

`LDFLAGS` remains the strategy's linking configuration. A frozen Makefile can
select the appropriate rules from those variables. Each declared language must
actually compile attributable source with its required flags; a compiler
version query cannot satisfy the build gate. The protected observer tracks
compiler processes and working directories. Internal compiler tools are
distinguished from separately invoked toolchains (for example, nvcc's own
ptxas invocation versus an explicitly declared PTX assembly step). Response
files hiding arguments are refused. Include processing and final link
provenance are not exhaustively reconstructed.

The legacy Fortran request remains supported. New mixed requests use a
`toolchains` mapping. The gateway records the complete configuration, observed
language invocations and compiled sources. Unknown or ambiguous configurations
fail rather than silently selecting Fortran. See the supplied
[`fortran_cuda`](../equivalent/strategy/files/fortran_cuda.yaml) and
[`fortran_ptx`](../equivalent/strategy/files/fortran_ptx.yaml) strategies.

Build jobs have no GPU driver mount. The PTX example links its Driver API loader
against the CUDA SDK's `libcuda` stub, using a path for the reviewed builder
image. Execution jobs load the actual mounted driver library. No runtime search
path points at the stub. Adapt that link directory when changing SDK images.

The replay/capture interface remains application-specific. A Fortran wrapper
using `ISO_C_BINDING` can call a C ABI exported by a CUDA C++ implementation or
CUDA Driver API loader. Array layout, scalar passing and ownership must agree
across that boundary. The current Fortran SESE scanner cannot analyze foreign
code. List it explicitly in the region specification:

```yaml
files: [src/potential.f90, src/potential.cpp, src/potential.ptx]
opaque_sources: [src/potential.cpp, src/potential.ptx]
anchor:
  file: src/potential.f90
  entry_symbol: potential
  pst_node: potential@13-17
```

The strategy must also permit those paths. The resulting analyzer claim notes
that the foreign code's control flow and effects were not checked, and whether
each file was present. Bootstrap first submits this spec against the original
Fortran procedure; opaque paths may name files to be introduced after that
spec passes. Then submit the implementation and its updated Fortran anchor,
and rerun the analyzer and build gates. The build still requires attributable
compiles for every declared language. Mutation
onboarding remains Fortran-oriented: onboard the Fortran baseline before
porting its region to another language.

Port replay and held-out regression explicitly request protected GPU profiling,
independently of the strategy's optional runtime notification setting. Thus a
mixed strategy may use `notify: null` while the builder still collects
`nsys/CUPTI_ACTIVITY_KIND_KERNEL` evidence. Older callers that omit `profile`
retain the legacy behavior in which `acc` or `omp` notification enables profiling.

Declare dynamically loaded modules and shared libraries beside each target:

```yaml
replay:
  target: replay
  executable: replay
  runtime_artifacts:
    - path: potential.cubin
      kind: gpu_module
      when_language: ptx
```

Kinds are `gpu_module` and `shared_library`. Paths must be unique, canonical
relative file paths inside the submitted tree. Optional `when_language`
requires the companion only for strategies containing that language; this
allows the same manifest to describe a CPU baseline without GPU modules.

The builder freezes each declared file and records its digest and size. The
gateway checks that the successful build response includes the requested
companions. They join the executable's evidence materials and are reverified
before reuse. Missing or changed companions invalidate the build even if the
executable bytes are unchanged. Retained legacy executable-only build records
remain readable under their original policy; they cannot supply the new
policy's missing evidence.

Resolve runtime companions beside the immutable executable (`$ORIGIN` for a
shared library, or `/proc/self/exe` for the example loader). Timing runs have a
writable working copy, from which declared companion copies are removed.
Identity checks establish retention of declared bytes; they do not prove that
an arbitrary submitted loader actually used those bytes, or that a submitted
Makefile produced them exclusively through the observed toolchains. That
stronger provenance guarantee still requires a trusted build/load plan.

[The charged-cloud example](../programs/potential/README.md) exercises Fortran,
CUDA C++, and an external assembled PTX module through the gateway.
