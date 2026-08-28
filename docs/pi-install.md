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
- A model for `pi` to run, by any of the three routes under "Choosing a
  model" below: `pi`'s own `/login`, an API key passed through Docker, or
  a provider you define yourself.

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
files git tracks under the source tree the code's manifest names, which
for tsunami is `programs/tsunami/baseline`), copies the seed into the working
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

An argument to `pi.sh` that begins with a dash is added to the session's
own command line (`./pi.sh --model ollama/devstral-small-2:24b`).
Anything else replaces the command, which is how the isolation check
above is run.

## Choosing a model

The session container is the only one with a route to the internet, so
this is the only place a model credential belongs. The gateway has no
internet route and never sees one. `deploy/state/pi-home` is mounted as
`pi`'s configuration directory inside the container, with the same layout
as `~/.pi` on the host, and git ignores it. Three routes, which combine:

**An API key, passed through Docker.** The agent service in
`docker-compose.yml` forwards a list of provider variables —
`GEMINI_API_KEY`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` and others — to
that container and to no other. Each is empty unless you set it, in
either of two places:

    export GEMINI_API_KEY=...           # in this terminal; never on disk
    echo 'GEMINI_API_KEY=...' >> .env   # or in deploy/.env, which git ignores

A value in the terminal wins over one in `.env`. If your shell profile
already exports the key, it is already forwarded and there is nothing to
do. The variable names are `pi`'s own, listed in the environment section
of `pi --help`; to use a provider whose name is not in the compose file,
add a line for it there.

**`/login`, inside the session.** Type it at the `pi` prompt and follow
the flow. `pi` writes the result to `state/pi-home/agent/auth.json`, so
later sessions are already logged in. If the login needs a browser the
container cannot open, run `pi` and `/login` on the host and copy your
`~/.pi/agent/auth.json` to `deploy/state/pi-home/agent/auth.json`; it is
the same file. Copying it means one credential is refreshed from two
places, which for a rotating token can log one of them out — a
container-only login avoids that.

**A provider you define, in `models.json`.** For a local server or an
endpoint `pi` does not know, put a `models.json` in
`deploy/state/pi-home/agent/`. It is read exactly as `~/.pi/agent/models.json`
is on the host, so your own file can be copied in, with one change: a
`baseUrl` naming `localhost` means the container, not this machine. The
compose file gives the session the name `host.docker.internal` for this
machine, so for a server on the host — ollama, say — rewrite the URL as
you copy:

    sed 's#//localhost:#//host.docker.internal:#' \
        ~/.pi/agent/models.json > state/pi-home/agent/models.json

The server has to be listening on more than the loopback address for
that to reach it: ollama needs `OLLAMA_HOST=0.0.0.0`. Naming this
machine is a widening — the session can reach services here that are not
exposed to the internet — and the `extra_hosts` line in the compose file
is where it is granted and can be taken back. It does not affect the
session's isolation from the builder, the oracle, or the ledger, which
`isolation_check.sh` still asserts.

Confirm what the session can actually use:

    ./pi.sh --list-models

Without a stated preference `pi` starts on Google's default model. To
change that for every session, write `state/pi-home/agent/settings.json`
with the provider and model you want:

    {"defaultProvider": "ollama", "defaultModel": "devstral-small-2:24b"}

Within a session, Ctrl+P cycles models.

From here, `pi-users-manual.md` takes over.

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

## Changing the code, the region, or the strategy

`programs/` holds one directory per code: its `manifest.yaml`, the
baseline sources, the datasets, the reference captures, the tolerance
policy, and the region specs. `deploy/gateway.yaml` names each code in
its `codes:` section and each region in its `regions:` section, where a
region gives its code, its spec file path, its strategy, and its visible
dataset. `equivalent/strategy/files/` holds the strategies
(`stdpar_managed`, `omp_target`).

A new region is a new entry in `gateway.yaml` and `EQUIVALENT_REGION` in
`.env`; a new strategy is a new YAML file; a new code is a new directory
under `programs/`, a new `codes:` entry, and `EQUIVALENT_CODE` in
`.env`. Changing the code also means rebuilding the oracle image, which
bakes in that code's captures. Restart the gateway after editing
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
