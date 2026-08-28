# equivalent — pi extension

A thin `pi` client for interactive porting sessions against the
`equivalent` gateway. Everything it knows, it fetched from the gateway:
on session start it calls `GET /table` and registers one tool per action
row (plus `submit`, `status`, and `claim`), with each tool's description
— including its `Requires:` line — and its parameters generated from the
row. A refusal from the gateway comes back as the tool's result text, so
the model reads it as its next steps. The extension decides nothing; the
gateway remains the reference monitor.

A check's result is the verdict line and then the claim's detail as
pretty-printed JSON — for a `fail`, the keys that explain the failure
first and up to 24000 characters of it; for a `pass`, the same rendering
cut off much sooner. Past either cap the last line names the claim id to
read the rest by. The gateway's own receipt policy decides whether there
is a detail at all, so a verdict-only predicate still renders as the
verdict line alone.

`claim` takes one required `claim_id` and reads that claim back from
`GET /claims/{claim_id}`, rendered the same way a check's result is. It
exists because a verdict a session was handed elsewhere — in a status
row, or earlier in the session — is otherwise unreadable from inside the
session. `status` prints a requirement whose check ran and failed as
`<predicate>  fail  <claim id>  (fix and run <action> again)` rather
than as missing, which is a claim to read rather than a check to run.

A tool's arguments are the settings its row says the action takes, with
the gateway's own wording: `time_baseline` and `time_port` take
`repeats`, `property_check` and `harness_property` take `seed` and
`max_examples`, and `harness_self_check` takes `limit`. All of them are
optional integers, so a call that names none is the ordinary call and
gets the gateway's defaults, and only the keys the row declared are sent
— the gateway hashes the config to spot a repeated request, so nothing
else belongs in it. `claim` takes the id of the claim to read; every
other tool takes no arguments.
`submit` in particular names no path: the gateway reads the working copy
its own configuration gives the region, so the session edits its files
and calls `submit` with nothing.

Every `/submit`, `/run`, and `/claims` call carries three identifying
headers — `X-Session-Id`, `X-Model-Id`, and `X-Tool-Call-Id`, the last
being pi's own id for the tool call being executed — so the gateway's
request log
and the session file can be lined up call by call rather than matched by
order and second-granularity timestamps.

## Configuration

Three environment variables, all required:

| Variable | Meaning |
| --- | --- |
| `EQUIVALENT_GATEWAY_URL` | Base URL of the gateway, e.g. `http://gateway:8000` |
| `EQUIVALENT_GATEWAY_TOKEN` | Bearer token the gateway expects |
| `EQUIVALENT_REGION` | Region id, e.g. `ch04:step` |

If any is missing, the extension reports the problem at session start and
registers nothing.

## Loading the extension

Any of `pi`'s usual mechanisms works; no compile step is needed (`pi`
loads TypeScript directly):

- `pi -e /path/to/pi-extension/src/extension.ts`
- `additionalExtensionPaths` in `~/.pi/agent/settings.json`
- a symlink under `~/.pi/agent/extensions/` or the project's
  `.pi/extensions/`

## Session files

`pi`'s session files must land on the mounted `sessions/` volume so they
can be joined with the gateway's `requests.jsonl` by session id. That is
deployment configuration, not extension code — set one of:

- `pi --session-dir /sessions`
- `PI_CODING_AGENT_SESSION_DIR=/sessions`
- `"sessionDir": "/sessions"` in `settings.json`

The deployment's agent container sets this; `test/sdk.test.ts` proves
the mechanism by running a real (scripted-model) session against a
session directory and a stubbed gateway, then checking that the session
id and tool-call id the gateway received are the ones the session file
records for that call.

## Development

```sh
npm install
npm test        # vitest
npm run typecheck
```

`test/fixtures/table.json` is a copy of the gateway's real
`ACTION_TABLE`; the Python test
`equivalent/tests/gateway/test_app.py::test_get_table_matches_the_pi_extension_fixture`
fails if the two drift, so regenerate the fixture (and the golden tool
descriptions) whenever the table changes.
