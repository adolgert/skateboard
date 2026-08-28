# Installing and running the porting framework under pi

This document takes a machine with a GPU from a fresh checkout to an
interactive `pi` session that can port a region and record its evidence.
The companion document, `pi-users-manual.md`, explains what to do inside
that session. This one is about getting there and about the commands
that surround the session: starting the services, checking them, reading
the ledger, and shutting down.

Everything here lives in `deploy/`. Each script is short and safe to read
before running.

## What gets installed

Four containers, on four networks, all defined in `deploy/docker-compose.yml`:

| container | what it holds | can reach |
| --- | --- | --- |
| `agent` | `pi`, the extension, the NVIDIA HPC compilers, the GPU, the working copy | the gateway, and the model provider on the internet |
| `gateway` | the git repository of submitted code, the ledger, the analyzer | the builder and the oracle; no internet |
| `builder` | `nvfortran`, `compute-sanitizer`, the GPU | nothing; it only answers |
| `oracle` | the reference outputs and the tolerance policy, baked in | nothing; it only answers |

The agent shares no network with the builder or the oracle; those are
missing routes, not blocked ones. Everything that persists is a plain
directory under `deploy/state/` on the host, owned by you:

| directory | what it is | who writes it |
| --- | --- | --- |
| `state/working` | the agent's working copy | the agent (read-only to the gateway) |
| `state/repo` | the gateway's git repository: the baseline and every submission | the gateway |
| `state/ledger` | the ledger: claims and the request log, per region | the gateway |
| `state/sessions` | `pi`'s transcripts, one file per session | `pi` |
| `state/seed` | the baseline files the repository starts from | `up.sh` |
| `state/pi-home` | `pi`'s own settings and login, so you log in once | `pi` |

## Prerequisites

- Linux with an NVIDIA GPU and driver. The strategy files compile for
  compute capability 8.9 (`-gpu=cc89`); a different GPU needs that flag
  changed in `equivalent/strategy/files/*.yaml`.
- Docker with the Compose plugin (`docker compose version` prints 2.x)
  and the NVIDIA container toolkit. Check both at once:

      docker run --rm --gpus all ubuntu nvidia-smi

- About 20 GB of disk for images. The agent and builder images are built
  on NVIDIA's HPC SDK image (`nvcr.io/nvidia/nvhpc:25.9-devel-cuda13.0-ubuntu24.04`,
  14 GB), which is pulled on the first build.
- Python 3.12 on the host, for reading the ledger from outside the
  containers. Node is not needed on the host; `pi` runs inside the agent
  container.
- A model for `pi` to run. `pi` authenticates through its own
  `/login` command, so no API key is written anywhere in this repository.

## Install

From the repository root:

    git clone <this repository> && cd skateboard
    python3 -m venv .venv
    .venv/bin/pip install -e .          # the `ledger` command, for the host

Then set up the deployment:

    cd deploy
    cp .env.example .env
    $EDITOR .env                        # set EQUIVALENT_TOKEN to something of your own

`EQUIVALENT_TOKEN` is the one secret. The agent and the `ledger` command
present it to the gateway; the gateway presents it to the builder and the
oracle. `.env` is ignored by git.

## Start the services

    ./up.sh

This creates the `state/` directories, writes the baseline seed (the six
files git tracks under `demo/work`), copies the seed into the working
copy if the working copy is empty, builds the gateway, builder, and
oracle images, starts them, and waits for the gateway to report healthy.
It ends by printing the baseline commit, the ledger directory, and the
commands that come next. It is safe to run again: it creates what is
missing and removes nothing.

`up.sh` deliberately does not build the agent image, which is the slow
one. `pi.sh` builds it the first time it is needed.

## Prove the stack works before involving a model

    ./walkthrough.sh

This drives the region from nothing to acceptance without a model: it
writes the region spec, submits, runs the analyzer, submits a port that
is known to pass, runs every check in order, confirms `accepted`, and
then submits a port that is known not to compile and confirms that the
failure lands as a claim. It runs in a container on the agent's network,
because the gateway is reachable from nowhere else. It takes a few
minutes; the timing checks run the full-size program repeatedly.

Read what it left behind:

    cd ..
    .venv/bin/ledger status --config deploy/state/gateway.host.yaml --region-id ch04:step

`up.sh` writes `deploy/state/gateway.host.yaml`: the same configuration
the gateway reads, with the paths as this machine sees them. That file
is how every host-side `ledger` command finds the ledger, the
repository, and the transcripts.

If you want the first `pi` session to start from a clean region — so you
can watch the model write the spec and be refused for calling a check
too early — reset after the walkthrough:

    cd deploy
    ./down.sh --reset       # lists what it will delete, then asks
    ./up.sh

`--reset` never touches `state/pi-home` or `state/seed`.

## Check the isolation

    ./pi.sh /opt/isolation_check.sh      # from inside the agent container
    ./isolation_check_gateway.sh         # the gateway's half, from the host

The first confirms the agent can reach the gateway and the model
provider, cannot reach the builder or the oracle, has no copy of the
repository or ledger, and can write its working copy. The second
confirms the gateway has no route to the internet and sees the working
copy read-only. Both stop at the first failure and say what was checked.

## Open a session

    ./pi.sh

The first run builds the agent image (several minutes on top of the
14 GB base). After that it drops you into `pi`, running in the agent
container with `/working` as its directory and the extension loaded. The
extension prints one line when it has connected:

    equivalent: registered 10 tools for region ch04:step

Log in once:

    /login

`pi` writes the credentials to `state/pi-home/agent/auth.json`, which is
a mount, so later sessions are already logged in. If the login flow
needs a browser that cannot be reached from inside the container, run
`pi` and `/login` on the host instead and copy the resulting
`~/.pi/agent/auth.json` to `deploy/state/pi-home/agent/auth.json`; it is
the same file.

From here, `pi-users-manual.md` takes over.

Arguments to `pi.sh` are passed to the container's command, which is how
the isolation check above is run.

## Read the ledger while a session is running

The ledger is plain files, and the gateway writes each line completely
before the next, so reading it while the gateway is writing is safe.
From the repository root:

    .venv/bin/ledger status  --config deploy/state/gateway.host.yaml --region-id ch04:step
    .venv/bin/ledger history --config deploy/state/gateway.host.yaml --region-id ch04:step
    .venv/bin/ledger show     deploy/state/ledger/<baseline>/ch04-step c-0007
    .venv/bin/ledger requests deploy/state/ledger/<baseline>/ch04-step
    .venv/bin/ledger session  <session-id> --config deploy/state/gateway.host.yaml --region-id ch04:step

`<baseline>` is the commit id `up.sh` printed; `ls deploy/state/ledger`
shows it. `status` with `--config` reports the tree the gateway is
sitting on right now; given a bare directory instead, it can only report
the tree of the last claim. The session id for `session` is the one
`pi` shows, and also the suffix of the transcript's filename in
`deploy/state/sessions`.

## Stop

    ./down.sh               # stop the containers; state stays
    ./down.sh --reset       # also discard repo, ledger, working copy, sessions (asks first)

Restarting the builder discards the compiled work it keeps between
checks. After a builder restart, the next check on an existing tree may
report that the binary is missing; running `build_replay` again rebuilds
it. The gateway can be restarted freely; the repository and ledger are
on the host.

## Changing the region or the strategy

`deploy/gateway.yaml` names each region: its spec file path, its
strategy, and its visible dataset. `equivalent/strategy/files/` holds
the strategies (`stdpar_managed`, `omp_target`). A new region is a new
entry in `gateway.yaml` and `EQUIVALENT_REGION` in `.env`; a new
strategy is a new YAML file. Restart the gateway after editing
`gateway.yaml`, and re-run `up.sh` so the host copy is regenerated.

## When something is wrong

- **`up.sh` says the gateway did not become healthy.** Run
  `docker compose logs gateway`. The usual cause is a configuration
  error, which the gateway reports by name (a missing field, a strategy
  file that does not exist).
- **The extension reports a configuration error at session start.**
  One of `EQUIVALENT_GATEWAY_URL`, `EQUIVALENT_GATEWAY_TOKEN`, or
  `EQUIVALENT_REGION` is not set in the agent container. They come from
  `docker-compose.yml` and `.env`.
- **A check answers `error: builder not configured`.** The gateway was
  started without `EQUIVALENT_BUILDER_URL` or `EQUIVALENT_ORACLE_URL`;
  the compose file sets both.
- **A check answers an error naming the builder's workspace.** The
  builder was restarted; run `build_replay` again.
- **Files under `state/` are owned by root.** They should not be: the
  gateway runs as your user. If it happens, the containers were started
  without `EQUIVALENT_UID`/`EQUIVALENT_GID` set, which `up.sh` and
  `walkthrough.sh` export. Start them through the scripts.
