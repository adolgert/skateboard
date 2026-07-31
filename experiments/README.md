# Experiments

Results from running the skateboard harness across configurations. Each CSV is a
copy of the harness ledger (`demo/ledger` volume) for one campaign.

## model-matrix-2026-07-30.csv

Same region (`ch04:step`), same rung (`stdpar_managed`), up to 3 attempts each,
four agent backends. Baselines: best-CPU **10.9 s**, naive-`stdpar` **8.0 s**
(2,000,000-point run).

| Model | Backend | Result | Attempts | Speedup vs best-CPU |
|---|---|---|---|---|
| Claude Haiku 4.5 | anthropic | accepted | 1 | **0.13×** (correct but slow) |
| Claude Sonnet 5 | anthropic | accepted | 2 | **8.9×** |
| Gemini 2.5 Flash | openai-compat | not accepted | 3 (all build-fail) | — |
| qwen2.5:14b (local) | openai-compat / Ollama | not accepted | 3 (all build-fail) | — |

Three findings, each a load-bearing point of the design:

1. **The harness is model-agnostic and fails safely.** All four backends ran the
   identical pipeline. The two that could not produce compilable GPU code
   (Gemini Flash, qwen2.5) failed at the **build gate** on every attempt and were
   never accepted — the oracle never even ran on their output. Weak generation is
   caught, not trusted (TRACTOR's central point).

2. **The bounded feedback loop works.** Sonnet's attempt 1 failed to build — it
   put a `block` construct inside a `do concurrent`, which nvfortran's
   `-stdpar=gpu` offload cannot compile. The structured compiler-error report fed
   back, and attempt 2 removed the block and was accepted at 8.9×. Orchestration,
   not raw generation, is what turned a failure into a success.

3. **Correct is not the same as effective — and the performance measurement
   caught it.** Haiku produced a *correct* GPU port (device-execution proof,
   sanitizer, visible AND held-out comparison all pass) that runs **8× slower than
   the CPU** (86.9 s vs 10.9 s). The cause: its kernel keeps extra whole-array
   copies (`h_old = h`, `u = u_new`) that execute on the host, so 2M-element
   managed-memory arrays migrate host⇄device every step. Sonnet/Opus updated in
   place and kept the data resident on the device.

The speedup spectrum (including Haiku's 0.13×) is kept deliberately: the harness
records every port, effective or not, rather than gating acceptance on speed.

## qwen-20-2026-07-30.csv

qwen2.5:14b (local Ollama), 20 attempts on the same region, with the repair loop
strengthened so each attempt sees its own previous failing code plus the
compiler/comparator diagnostic. Progression: attempts 1–3 fail to build;
attempts 4–20 all compile **and** launch GPU kernels (build + device proof pass)
but fail the correctness oracle. Zero accepted — the model crossed the build
barrier but never the correctness barrier, and the oracle rejected all 17
compiling-but-wrong ports with no false accepts. Generation ~10–18 s/attempt.
This is the sharpest demonstration that build success ≠ correctness and that the
oracle is the real gate.

## qwen-30-temp08-hint-2026-07-31.csv

qwen2.5:14b (local Ollama), 30 attempts, same region and rung, with **two changes
from the 20-attempt run**: sampling temperature raised from 0.2 to 0.8
(`AGENT_TEMPERATURE`), and one line added to the agent system prompt —
*"Check for off-by-one errors in indexing."* Baselines: best-CPU **10.11 s**,
naive-`stdpar` **6.95 s**.

**Result: 2 of 30 attempts compiled; 0 accepted.** Both compiling attempts (2 and
16) passed the device-execution proof and all three sanitizers, and both failed
the correctness oracle. The other 28 never got past the build gate.

| | 20 attempts @ T=0.2 | 30 attempts @ T=0.8 + hint |
|---|---|---|
| Build pass | 17/20 (85%) | **2/30 (7%)** |
| Device proof pass | 17 | 2 |
| Correctness pass | 0 | 0 |
| Accepted | 0 | 0 |

Three findings:

1. **The hint did not work.** Attempts 2 and 16 both still contain an off-by-one
   in the stencil. Both write `mod(i+1,n)+1` for the forward neighbour, which
   evaluates to `i+2`, and `mod(i-1,n)+1` for the backward neighbour, which
   evaluates to `i` — the correct form is `mod(i,n)+1`. Instructing the model to
   check for off-by-one errors did not prevent it from writing one.

2. **Higher temperature broke the repair loop.** At T=0.2 the model failed to
   build on attempts 1–3, absorbed the compiler error, and never repeated it:
   every attempt from 4 on compiled. At T=0.8 it re-emitted the *same* illegal
   construct — subscripting a function result, `diff_centered(u)(i)` — on 28 of
   30 attempts, receiving the identical nvfortran error each round. Attempts 11
   and 12 differ only in whether a sign is written `+ -g *` or `- g *`. The
   feedback loop stopped converging.

3. **The numerics degraded.** Attempt 16 replaced the centered first difference
   with `0.5*(u(i+1) + u(i-1)) - u(i)` — a *sum*, algebraically a second
   derivative — and its continuity update contains no difference operator at all.
   Its `h` loop is also a plain `do i = 1, n`, so it runs on the host, yet
   `device_proof` still passed because the other loops launched kernels.

**Caveat:** the two changes were made together, so temperature and prompt effects
are confounded. Isolating them requires T=0.8 with the original prompt.

Extracted source: `../docs/examples/qwen30t-attempt{02,11,16}-mod_kernel.f90`
(the two compiling attempts and one representative build failure).

## codestral-30-2026-07-31.csv

`codestral:22b-v0.1-q4_0` (local Ollama, 12.6 GB Q4_0), 30 attempts, same region
and rung, temperature 0.8, **stock prompt** (the off-by-one hint was removed
again). Baselines: best-CPU **10.37 s**, naive-`stdpar` **7.26 s**.

**Result: 15 of 30 compiled, 13 reached the oracle, 0 accepted.** No attempt ever
passed the visible comparison. No sanitizer ever fired.

| | qwen 20 @ T=0.2 | qwen 30 @ T=0.8 | codestral 30 @ T=0.8 |
|---|---|---|---|
| Build pass | 17/20 (85%) | 2/30 (7%) | **15/30 (50%)** |
| Device proof pass | 17 | 2 | 13 |
| Device proof **fail** | 0 | 0 | **2** (attempts 2, 20) |
| Correctness pass | 0 | 0 | 0 |
| Accepted | 0 | 0 | 0 |

Codestral is the first local model to make the **oracle** the binding constraint
rather than the compiler. It cleared the build gate on attempt 1 — something
qwen2.5 never did in 50 attempts across two campaigns.

Three findings:

1. **It reaches for directive-based GPU models, and only one of them works
   here.** Nearly every attempt uses `!$acc` (OpenACC) or `!$omp target`
   (OpenMP). Measured on this compiler with the harness flags
   (`-O2 -stdpar=gpu -gpu=cc89,mem:managed`): `do concurrent` offloads, `!$acc`
   offloads, and **`!$omp target` does not** — OpenMP offload needs `-mp=gpu`,
   which is the harness's other rung. Whole-array syntax does not offload either.
   By attempts 6–7 the model had begun stacking an `!$omp` directive on top of a
   `do concurrent`; the `do concurrent` is what offloads and the directive is
   inert decoration. It arrived at working offload by accretion, not by
   understanding which mechanism did the work.

2. **The device-execution gate caught the case the compiler could not.**
   Attempts 2 and 20 used `!$omp target` around plain `do` loops. nvfortran
   accepted both files without a warning — an unrecognised `!$omp` line is a legal
   comment — and produced a binary in which every loop ran on the host.
   `kernels_launched` hit 0 and the gate fired: the first two device-proof
   failures in the project. Attempt 1 made the adjacent guess, `!$acc`, which this
   profile *does* honour; its loops genuinely ran on the device (10 kernels) and it
   advanced to the oracle, failing there on physics. Same class of error, opposite
   outcome, decided by which directive dialect the model happened to pick.
   Worked example with the compiler's `-Minfo=accel` reports:
   `../docs/run_examples.md`, Example 3.

3. **A stable physics error survives the repair loop.** From attempt 1 onward it
   writes `h(i) - (du_dx(i)*(hmean + h(i)))/dx*dt` — differentiating `u` alone and
   multiplying by `(hmean+h)`, where the reference differentiates the flux
   `u*(hmean+h)`. The product rule is dropped. From attempt 5 it additionally
   dropped the `/ dx`. Attempt 1 also never assigns the updated velocity back to
   `u` at all, so the momentum result is discarded.

The contrast with qwen is the useful one: qwen repeated one *illegal construct*
and mostly never compiled; codestral compiles readily and produces a stream of
distinct, plausible, wrong numerics. That is the harder case, and the one only a
correctness oracle catches.

Extracted source: `../docs/examples/codestral-attempt{01,02,21}-mod_kernel.f90`.

## codestral-omp-2026-07-31.csv

`codestral:22b-v0.1-q4_0`, temperature 0.8, same region — but on the
**`omp_target` rung** (`-O2 -mp=gpu -gpu=cc89`, OpenMP target offload, no managed
memory, run under `OMP_TARGET_OFFLOAD=MANDATORY`) instead of `stdpar_managed`.
Up to 30 attempts allowed. Baselines: best-CPU **10.33 s**, naive-`stdpar` 7.36 s.

**Result: ACCEPTED on attempt 5.** The first acceptance by a local model in this
project, and the first use of the `omp_target` rung.

| attempt | build | device | kernels | memcheck | racecheck | initcheck | visible | holdout | verdict |
|---|---|---|---|---|---|---|---|---|---|
| 1 | fail | — | — | — | — | — | — | — | fail |
| 2 | pass | pass | 10 | pass | pass | **fail** | fail | — | fail |
| 3 | pass | pass | 20 | pass | pass | pass | fail | — | fail |
| 4 | fail | — | — | — | — | — | — | — | fail |
| 5 | pass | pass | 25 | pass | pass | pass | **pass** | **pass** | **ACCEPTED** |

Recorded speedup **0.818×** (12.63 s vs 10.33 s best-CPU) — correct but slower
than the CPU. See the flag caveat below: that number is an artifact of the rung's
compile flags, not of the model's code.

### A harness bug this campaign had to fix first

The `omp_target` rung had never been run. Its device-execution proof was
**structurally incapable of passing**: `_notify_env` set `LIBOMPTARGET_INFO=1`,
an LLVM/Clang offload variable that nvfortran ignores completely (zero bytes of
stderr, even at `-1`). NVIDIA's runtime uses `NVCOMPILER_ACC_NOTIFY`, which
covers OpenMP target regions as well as OpenACC/stdpar. A hand-written, verified
correct OpenMP port scored `kernels_launched = 0` before the fix. Every attempt
on this rung would have failed the device proof regardless of quality. Fixed in
`demo/builder/stages.py`; both notify modes now use the NVIDIA notifier and the
same `launch ` regex.

### Findings

1. **The model is markedly better at OpenMP than at `do concurrent`.** Same
   model, same temperature, same region, same day: 0 of 30 accepted on
   `stdpar_managed`, accepted on attempt 5 of `omp_target`. On the stdpar rung it
   repeatedly wrote `!$omp target` directives that the profile ignored; given a
   rung where those directives are the intended mechanism, it succeeded quickly.

2. **The accepted port is correct for the right reasons.** It materialises the
   flux `flux(i) = u(i)*(hmean + h(i))` in its own loop before differencing it —
   the same structure that made Sonnet's port correct and whose absence caused
   qwen's off-by-one. It folds `0.5` and `/dx` into `/(2.0*dx)`, orders the u-then-h
   updates correctly, and wraps everything in one
   `!$omp target data map(tofrom: h, u) map(alloc: ...)` region.

3. **The rung's flags, not the model, cost the speedup.** `omp_target` compiles
   without `mem:managed`, so the `target data` region inside `step` copies both
   2M-element arrays host⇄device on every timestep. Recompiling the *identical*
   accepted kernel with `mem:managed` added:

   | flags | wall clock |
   |---|---|
   | `-mp=gpu -gpu=cc89` (as run) | 13.2 s |
   | `-mp=gpu -gpu=cc89,mem:managed` | **0.87 s** |

   A 15× difference from one flag. The accepted port would be **~11.9× faster than
   best-CPU** on a managed-memory OMP rung — faster than Sonnet's 8.9× on stdpar.
   The recorded 0.818× measures the rung, not the port. Adding `mem:managed` to
   the `omp_target` profile would make the two rungs comparable.

Accepted source: `../docs/examples/codestral-omp-attempt05-ACCEPTED-mod_kernel.f90`.

See `../docs/early_trials.tex` for the full write-up with plots, and
`../docs/run_examples.md` for annotated code from the earlier campaigns.
