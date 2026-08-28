import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

import { callableRows, describeRow, type ActionRow } from "../src/table.js";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

function loadFixtureTable(): ActionRow[] {
  const raw = readFileSync(path.join(__dirname, "fixtures", "table.json"), "utf8");
  return JSON.parse(raw) as ActionRow[];
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
});
