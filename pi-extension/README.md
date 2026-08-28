# equivalent — pi extension

A thin `pi` client for interactive porting sessions against the
`equivalent` gateway. Everything it knows, it fetched from the gateway:
on session start it calls `GET /table` and registers one tool per action
row (plus `submit` and `status`), with each tool's description —
including its `Requires:` line — generated from the row. A refusal from
the gateway comes back as the tool's result text, so the model reads it
as its next steps. The extension decides nothing; the gateway remains
the reference monitor.

None of the tools take arguments. `submit` in particular names no path:
the gateway reads the working copy its own configuration gives the
region, so the session edits its files and calls `submit` with nothing.

Every `/submit` and `/run` carries three identifying headers —
`X-Session-Id`, `X-Model-Id`, and `X-Tool-Call-Id`, the last being pi's
own id for the tool call being executed — so the gateway's request log
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
