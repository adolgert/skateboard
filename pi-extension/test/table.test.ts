import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

import {
  callableRows,
  describeRow,
  runConfig,
  toolParameters,
  type ActionRow,
} from "../src/table.js";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

function loadFixtureTable(): ActionRow[] {
  const raw = readFileSync(path.join(__dirname, "fixtures", "table.json"), "utf8");
  return JSON.parse(raw) as ActionRow[];
}

function fixtureRow(name: string): ActionRow {
  const row = loadFixtureTable().find((r) => r.name === name);
  if (!row) throw new Error(`no ${name} row in the fixture table`);
  return row;
}

describe("table", () => {
  it("excludes accept -- it has no component for POST /run to dispatch to", () => {
    const table = loadFixtureTable();
    const rows = callableRows(table);
    expect(rows.map((r) => r.name)).not.toContain("accept");
    expect(rows).toHaveLength(table.length - 1);
  });

  it("generated tool descriptions for the real table match a golden file", () => {
    const rows = callableRows(loadFixtureTable());
    const rendered = rows.map((row) => `${row.name}: ${describeRow(row)}`).join("\n\n");
    const golden = readFileSync(path.join(__dirname, "fixtures", "tool-descriptions.golden.txt"), "utf8");
    expect(rendered).toBe(golden);
  });

  it("offers a row's settings as optional parameters, worded as the gateway worded them", () => {
    const row = fixtureRow("property_check");

    expect(toolParameters(row)).toEqual({
      type: "object",
      properties: {
        seed: { type: "integer", description: row.config_params!.seed.description },
        max_examples: { type: "integer", description: row.config_params!.max_examples.description },
      },
    });
    // Optional: a call that names neither is the call the gateway
    // already accepted, and gets the defaults.
    expect(runConfig(row, {})).toEqual({});
    expect(runConfig(row, { seed: 7 })).toEqual({ seed: 7 });
  });

  it("gives a row that takes no settings no parameters, and sends no config for it", () => {
    const row = fixtureRow("run_replay");

    expect(toolParameters(row).properties).toEqual({});
    expect(runConfig(row, { repeats: 3 })).toEqual({});
  });

  it("sends only the keys the row declared -- the gateway hashes this config", () => {
    const row = fixtureRow("time_port");

    expect(runConfig(row, { repeats: 3, junk: "x" })).toEqual({ repeats: 3 });
  });

  it("ignores settings a gateway declared without describing -- nothing to type them with", () => {
    const row: ActionRow = {
      name: "harness_self_check",
      emits: ["harness/self_check"],
      requires: [],
      deterministic: true,
      component: "builder:/v1/mutate",
      config_keys: ["limit"],
      config_params: {},
    };

    expect(toolParameters(row).properties).toEqual({});
    expect(runConfig(row, { limit: 5 })).toEqual({});
  });
});
