# The Skateboard — minimum firewalled harness for AI-assisted Fortran→GPU porting

A working end-to-end demonstration: an AI agent ports a Fortran kernel to the GPU
under real professional controls — an oracle it cannot edit, gates it cannot
weaken, and a ledger that records every decision. See `../docs/architecture.tex`
for the design and `../docs/skateboard.tex` for the rationale.

## What it does

For the ch04 tsunami kernel (`step`, one time step of the 1-D shallow water
equations), the harness:

1. measures honest baselines first — best-CPU and the naive `-stdpar` floor;
2. asks the agent (Claude by default) to port `step` to the GPU;
3. runs it through a fixed gate sequence:
   **build → GPU device-proof → compute-sanitizer → correctness(visible) → timing**;
4. on success, checks a **held-out** dataset the agent never saw;
5. writes one append-only ledger row per attempt (git branch + provenance).

First run result: accepted on attempt 1, **~12× speedup** over the best CPU
configuration, 0 human interventions.

## Architecture (the firewall is the point)

```
 internet (LLM API)          agent-runner shares NO network or volume with
      ▲ egress               the oracle or builder — those are missing routes.
 ┌────┴────────┐  agent_net
 │ agent-runner│◄──────────────┐   UNTRUSTED  (only edits src/mod_kernel.f90)
 └─────────────┘   files→ ◄file │
                                ▼
                    ┌──────────────────────┐
                    │     orchestrator     │  TRUSTED — only initiator,
                    │  staged loop, ledger │  only component on all networks
                    └──┬───────────┬───────┘
        build_net      │           │   oracle_net
       (internal)      ▼           ▼   (internal)
                ┌──────────┐  ┌──────────────┐
                │ builder  │  │   oracle     │  read-only; holds expected
                │ nvfortran│  │ captures +   │  answers + held-out set +
                │ + GPU    │  │ tolerances   │  tolerances; runs no agent code
                └──────────┘  └──────────────┘
```

* **Gate logic is baked into the builder image** (`builder/stages.py`): the
  agent's patch can change Fortran source, never a compiler flag, gate command,
  or `OMP_TARGET_OFFLOAD` setting.
* **The oracle has no write endpoint.** Tolerances change only by rebuilding its
  image. Held-out `/compare` returns pass/fail only — never per-case error — so
  nothing quantitative about the held-out set can leak back to the agent.
* **Bearer token** on every orchestrator→service call, in addition to network
  isolation.

## Prerequisites (this machine already has them)

* Docker + Docker Compose, and **nvidia-container-toolkit** (`nvidia-ctk`) so a
  container can use the GPU. Verify: `docker run --rm --gpus all ubuntu nvidia-smi`.
* The NVIDIA HPC SDK image is pulled on first `docker compose build`
  (`nvcr.io/nvidia/nvhpc:25.9-devel-cuda13.0-ubuntu24.04`, ~14 GB).
* `ANTHROPIC_API_KEY` exported in the shell (forwarded by compose; not stored on
  disk). Only needed for the Claude backend.

## Run it

```bash
cd demo
# 1. (once) generate reference datasets + calibrate tolerances, on the host:
( cd capture && ./generate.sh && python3 calibrate.py )
# 2. build the images (first build pulls the ~14 GB nvhpc base):
docker compose build
# 3. roll the loop:
docker compose up -d agent-runner builder oracle
docker compose up orchestrator          # runs to completion, prints the log
# 4. read the results:
docker run --rm -v demo_ledger:/l alpine cat /l/ledger.csv
```

Re-run cleanly: `docker volume rm demo_repo demo_ledger` then step 3 again.

## Swap the agent (pluggability)

The agent is a config-line change; no other component moves. In `.env`:

```ini
# Claude (default)
AGENT_BACKEND=anthropic
AGENT_MODEL=claude-opus-4-8

# a local model via the already-installed Ollama (OpenAI-compatible endpoint)
AGENT_BACKEND=openai
AGENT_MODEL=qwen2.5:14b
OPENAI_BASE_URL=http://host.docker.internal:11434/v1

# Gemini (OpenAI-compatible endpoint)
AGENT_BACKEND=openai
AGENT_MODEL=gemini-2.0-flash
OPENAI_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai
OPENAI_API_KEY=<key>
```

## Tuning the demo

* **Problem size / speedup** — `work/src/tsunami.f90`, `default_tiles` (each tile
  = 100 points). 20000 → 2M points (~12× here). Bigger tiles → bigger speedup but
  slower CPU baselines. The size-100 capture cases stay valid because every tile
  evolves identically (technique from `codes/tsunami/src/ch04_large`).
* **Retry cap / rungs** — `MAX_ATTEMPTS` in `.env`; rung ladder in
  `orchestrator/orchestrator.py` (`stdpar_managed` then `omp_target`).
* **Tolerances** — regenerate with `capture/calibrate.py` (currently calibrated
  from the CPU's own `-O2` vs `-ffast-math` spread; recalibrate against nvfortran
  for a tighter, honest band).

## Layout

```
demo/
  work/            agent-writable work-tree (the code under port); src/mod_kernel.f90 is the ONLY file the agent may edit
  capture/         capture-replay tooling + generate.sh + calibrate.py (trusted, host-run)
  oracle/          compare API; captures/ (expected + held-out) and tolerances.json baked into the image
  builder/         nvfortran build + gates; stages.py = the complete, auditable set of GPU commands
  agent-runner/    the untrusted patch producer; adapters/ = pluggable backends
  orchestrator/    the deterministic staged loop + ledger + git branches
  docker-compose.yml   three isolated networks (agent_net, build_net[internal], oracle_net[internal])
```

## Honest caveats (state them in any report)

* Same-host containers are a strong boundary against a **misbehaving agent**
  (the threat model here), not against a kernel exploit. Moving the oracle to a
  second machine is a URL change, because every edge is already a service call.
* Timings include per-process CUDA startup — that is the honest end-to-end number.
* `net_jail` (unshare -n on the replay child) is off tonight (needs
  CAP_SYS_ADMIN); build_net is already `internal`, so agent code in the builder
  has no egress regardless. Turn it on as later hardening.
