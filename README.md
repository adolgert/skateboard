# Skateboard

A minimum, firewalled harness that demonstrates an AI agent porting Fortran to
the GPU under professional assurances: an oracle it cannot edit, gates it cannot
weaken, and a ledger that records every decision.

- **`docs/skateboard.tex`** — why this is the smallest artifact that rolls end to
  end (the "skateboard", not a wheel of the eventual car).
- **`docs/architecture.tex`** — the concrete component/service architecture and
  the trust firewall, in plain language with diagrams.
- **`programs/`** — one directory per code being ported: its manifest, the
  baseline sources, the datasets, the reference captures, and the tolerance
  policy. `programs/tsunami` is the ch04 shallow-water kernel.
- **`services/`** — the builder (the only container with the GPU) and the
  oracle (sealed, holding the reference answers).
- **`deploy/`** — the running stack: the agent's session, the gateway that is
  the only thing it can call, and the two services above, on isolated Docker
  networks. See [`deploy/README.md`](deploy/README.md) to run it, and
  [`docs/pi-install.md`](docs/pi-install.md) to set the machine up.

First run: Claude ported the ch04 tsunami kernel to the GPU on the first attempt,
passed every gate including a held-out dataset, at roughly a 12× speedup over the
best CPU build, with zero human interventions.

The `codes/tsunami` example (Curcic, *Modern Fortran*) is an external dependency
and is not vendored here; `programs/tsunami/baseline` holds the specific sources
it ports.
