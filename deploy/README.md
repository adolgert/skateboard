# deploy — running the gateway, the ledger, and an interactive session

Everything in this directory is deployment: container definitions, the
gateway's configuration, and the scripts that start and check a running
stack. The only code here is `seed.py`, which writes the baseline, and
the two walkthroughs, which drive one region end to end — `walkthrough.py`
a port, `onboard_walkthrough.py` a code being brought in. None of them
decides anything.

What is deployed comes from two directories above this one: `programs/`,
one directory per code, holding that code's manifest, baseline sources
(including its own makefile, replay driver, and capture program),
datasets, captures, and tolerance policy; and `services/`, holding the
builder and the oracle. The builder knows no code: it builds a submitted
tree by running that tree's own makefile, with the compiler and flags
the strategy names, and reports back what the compiler was actually
asked to do. Both image builds take the repository root as
their context, and the oracle takes the code as a build argument
(`EQUIVALENT_CODE`) because it bakes that code's answers in: the whole
of `programs/<code>/`, so that it has the manifest, the captures, and
the tolerance policy inside the source tree wherever the manifest says
they are. A code that has not been brought in yet has only some of
those, and the oracle starts anyway, reporting that it is not ready and
answering every comparison with the name of what is missing.

| file | what it is |
| --- | --- |
| `docker-compose.yml` | the four services and the four networks |
| `gateway.<code>.yaml` | the gateway's configuration for a deployment built around that code, in the container's paths; `EQUIVALENT_CODE` picks it |
| `gateway/Dockerfile` | the gateway image: the package, which holds the analyzer |
| `agent/Dockerfile` | the session image: compilers, a GPU, and the session tool |
| `seed.py` | writes the baseline the gateway's repository starts from, reading which tree from the code's manifest |
| `up.sh` | prepare state, seed, build, start, wait for health |
| `pi.sh` | open an interactive session in the agent container |
| `walkthrough.sh`, `walkthrough.py` | drive one region from nothing to accepted; the region is `EQUIVALENT_REGION` |
| `onboard_walkthrough.sh`, `onboard_walkthrough.py` | bring a code in from its bare baseline to onboarded; the region is `--region`, defaulting to `tsunami:onboard` |
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
./onboard_walkthrough.sh      # optional: the same for bringing a code in
./pi.sh                       # an interactive session
```

Both walkthroughs write into `state/working`, so run them one after the
other rather than at once.

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

The same file names the region an onboarding session is promoted from,
once its eight checks have passed and you have read them:

```sh
ledger promote --config deploy/state/gateway.host.yaml --region-id tsunami:onboard
```

That writes `programs/<code>/` — the manifest, the baseline, the visible
dataset, and the captures — and prints the steps that stay yours: the
commit, a `phase: porting` region in `gateway.<code>.yaml`, and `down.sh` /
`up.sh`, because the oracle bakes the captures into its image. It
refuses rather than writing over what is already there; `--replace`
empties the destinations first and `--programs` writes somewhere else
entirely, which is how to compare a promotion against what is checked
in.

`up.sh` writes `state/gateway.host.yaml`: the same deployment as `gateway.yaml`,
with the paths of this machine rather than the container's mount points. Both
are read by the one configuration loader, so there is no second description of
where a region's files live. Edit `gateway.<code>.yaml` and re-run `up.sh` rather than
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
- **The builder runs a makefile that came in with the submission.** That is
  what lets any code be built without teaching the builder about it, and it
  is why the compiler it hands that makefile is a shim that writes down every
  invocation. The `build/replay` claim carries that log, and a build that
  compiled without the strategy's flags, or compiled a file that is not the
  tree's own source, is a failed claim rather than a passed one. The builder
  has no route off the host either way.
- **The walkthrough runs in a container, not on this machine.** The gateway is
  on internal networks only, so nothing outside them can reach it — including
  this terminal. `walkthrough.sh` runs the same script on the agent's network,
  with the same token the agent uses.
- **`state/working` is shared between the session and the walkthrough.** They
  edit the same files. Running the walkthrough over a session in progress will
  overwrite the kernel it is working on.
