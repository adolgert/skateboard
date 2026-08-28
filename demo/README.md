# The demonstration harness has been replaced

This directory held the first end-to-end demonstration: an orchestrator that
drove an agent-runner through a fixed gate sequence for the ch04 tsunami
kernel. The gateway path replaced it. The agent now initiates, the gateway is
the reference monitor, and the ledger holds every claim.

Where things went:

- the builder and the oracle services are `services/builder` and
  `services/oracle`;
- the tsunami code -- baseline, manifest, datasets, captures, tolerances --
  is `programs/tsunami`;
- how to bring the stack up is `deploy/README.md`, and the machine it runs on
  is set up by `docs/pi-install.md`.

Git history before this commit holds the orchestrator, the agent-runner, and
this directory's old README.
