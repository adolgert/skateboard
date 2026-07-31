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

See `../docs/early_trials.tex` for the full write-up with plots.
