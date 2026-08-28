# equivalent — pi extension

A thin `pi` client for interactive porting sessions against the
`equivalent` gateway. Everything it knows, it fetched from the gateway:
on session start it calls `GET /table` and registers one tool per action
row (plus `submit` and `status`), with each tool's description —
including its `Requires:` line — generated from the row. A refusal from
the gateway comes back as the tool's result text, so the model reads it
as its next steps. The extension decides nothing; the gateway remains
the reference monitor.

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
session directory and reading the tool calls back out of the session
file.

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
