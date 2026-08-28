# deploy — running the gateway, the ledger, and an interactive session

Everything in this directory is deployment: container definitions, the
gateway's configuration, and the scripts that start and check a running
stack. The only code here is `seed.py`, which writes the baseline, and
`walkthrough.py`, which drives one region end to end. Both import the
`equivalent` package; neither decides anything.

| file | what it is |
| --- | --- |
| `docker-compose.yml` | the four services and the four networks |
| `gateway.yaml` | the gateway's configuration, in the container's paths |
| `gateway/Dockerfile` | the gateway image: the package plus the analyzer |
| `agent/Dockerfile` | the session image: compilers, a GPU, and the session tool |
| `seed.py` | writes the baseline the gateway's repository starts from |
| `up.sh` | prepare state, seed, build, start, wait for health |
| `pi.sh` | open an interactive session in the agent container |
| `walkthrough.sh`, `walkthrough.py` | drive one region from nothing to accepted |
| `isolation_check.sh` | assert the isolation from inside the agent |
| `isolation_check_gateway.sh` | assert the gateway's half of it, from here |
| `down.sh` | stop; with `--reset`, discard state after asking |

## The four networks

The agent is on `agent_net` and `egress_net`: it can call the gateway and it
can reach the model provider, and that is all. The gateway is on `agent_net`,
`build_net`, and `oracle_net`, all three of which are internal, so it can call
the builder and the oracle but has no route to the internet. The builder is
alone on `build_net` and the oracle alone on `oracle_net`, so the agent shares
no network with either of them — those are missing routes, not blocked ones.
The agent's working copy reaches the gateway as a read-only mount rather than
over the network, so nothing the agent sends chooses which files are read.
Every call also carries a bearer token.

## Getting started

```sh
cp .env.example .env          # then set EQUIVALENT_TOKEN to something of your own
./up.sh                       # state directories, baseline seed, build, start
./walkthrough.sh              # optional: prove the whole path works, end to end
./pi.sh                       # an interactive session
```

`up.sh` is safe to run again. It creates what is missing, seeds the baseline
into `state/seed`, copies that into `state/working` only if the working copy is
empty, and never removes anything. It prints the baseline commit and the
command that reads the ledger.

The first `pi.sh` builds the agent image. That is a long build on a very large
base, which is why `up.sh` does not do it.

## Reading the ledger from this machine

The ledger is plain files under `state/ledger/<baseline commit>/<region id>`,
with the region's colon written as a dash:

```sh
ledger status deploy/state/ledger/<baseline>/ch04-step
```

That reports the tree of the last claim that was filed, because the ledger
alone does not know what the region is sitting on right now — that lives in the
gateway's git repository. To see the same tree the gateway's own status
reports, name the configuration instead:

```sh
ledger status --config deploy/state/gateway.host.yaml --region-id ch04:step
```

`up.sh` writes `state/gateway.host.yaml`: the same deployment as `gateway.yaml`,
with the paths of this machine rather than the container's mount points. Both
are read by the one configuration loader, so there is no second description of
where a region's files live. Edit `gateway.yaml` and re-run `up.sh` rather than
editing the generated copy.

## Logging in, once

The session tool is authenticated by the person, not by this repository: run
`/login` inside the first session and the credentials land in
`state/pi-home/agent/auth.json`, which is a mount, so later sessions are already
logged in. Nothing in these scripts reads or writes that file, and `--reset`
does not touch it.

## Checking the isolation

```sh
./pi.sh /opt/isolation_check.sh    # from inside the agent
./isolation_check_gateway.sh       # the gateway's half, from here
```

The first asserts that the gateway answers, that the builder and the oracle do
not, that the gateway's repository and ledger are not mounted in the agent,
that the model provider is reachable, and that the working copy is writable.
The second asserts that the gateway cannot reach the internet and sees the
working copy read-only.

## Stopping

```sh
./down.sh              # stop the services
./down.sh --reset      # also discard repo, ledger, working copy, and sessions
```

`--reset` names exactly what it would delete and asks before doing it. It keeps
the login and the baseline seed.

## Honest caveats

- **The agent's container is untrusted, and it is not a sandbox.** It has the
  HPC compilers, a GPU, and a route to the internet, and whatever runs in a
  session runs with all three. What keeps it honest is not that it is confined
  but that nothing it does there is evidence: only what the gateway checks,
  against a tree the gateway built itself, becomes a claim.
- **The builder's per-attempt workspace does not survive a restart.** It keeps
  compiled attempts on disk in the container, keyed by region and tree, and
  that disk goes away with the container. After restarting the builder, re-run
  `build_replay` for the tree you are working on; it rebuilds under the same
  key and the later gates find their workspace again.
- **The walkthrough runs in a container, not on this machine.** The gateway is
  on internal networks only, so nothing outside them can reach it — including
  this terminal. `walkthrough.sh` runs the same script on the agent's network,
  with the same token the agent uses.
- **`state/working` is shared between the session and the walkthrough.** They
  edit the same files. Running the walkthrough over a session in progress will
  overwrite the kernel it is working on.
