import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

import { renderStatus, type StatusBody } from "../src/status.js";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

/**
 * The gateway's own word for each finished phase, read out of its
 * source. The extension keeps a copy of this map as its fallback for a
 * gateway that does not send the word, and a copy that drifts would
 * print the wrong word for a finished region.
 */
function gatewayFinishedWords(): Record<string, string> {
  const source = readFileSync(
    path.join(__dirname, "..", "..", "equivalent", "ledger", "acceptance.py"),
    "utf8",
  );
  const phases: Record<string, string> = {};
  for (const [, name, value] of source.matchAll(/^([A-Z_]+) = "([^"]+)"$/gm)) {
    phases[name] = value;
  }
  const entry = source.match(/^FINISHED_WORD = \{(.+)\}$/m);
  if (!entry) throw new Error("no FINISHED_WORD map in the gateway's acceptance.py");
  const words: Record<string, string> = {};
  for (const [, name, word] of entry[1].matchAll(/([A-Z_]+): "([^"]+)"/g)) {
    const phase = phases[name];
    if (!phase) throw new Error(`no phase name for ${name} in the gateway's acceptance.py`);
    words[phase] = word;
  }
  return words;
}

function body(fields: Partial<StatusBody> = {}): StatusBody {
  return {
    tree: "T4",
    frozen: "F1",
    phase: "porting",
    accepted: false,
    rows: [],
    ...fields,
  };
}

describe("renderStatus", () => {
  it("prints each present claim's verdict and claim id, and ACCEPTED when accepted", () => {
    const body: StatusBody = {
      tree: "T4",
      frozen: "F1",
      phase: "porting",
      accepted: true,
      rows: [{ predicateType: "sese/verified", status: "present", verdict: "pass", claim_id: "c-31" }],
    };
    const text = renderStatus(body);
    expect(text).toContain("sese/verified");
    expect(text).toContain("pass");
    expect(text).toContain("c-31");
    expect(text.trim().endsWith("ACCEPTED")).toBe(true);
  });

  it("names the producing action for a missing claim and prints not accepted", () => {
    const body: StatusBody = {
      tree: "T4",
      frozen: "F1",
      phase: "porting",
      accepted: false,
      rows: [{ predicateType: "gpu/executed", status: "missing", producing_action: "run_replay" }],
    };
    const text = renderStatus(body);
    expect(text).toContain("run_replay");
    expect(text.trim().endsWith("not accepted")).toBe(true);
  });

  it("shows a requirement whose check ran and failed as a fail with its claim id", () => {
    // The gateway reports this row as "missing" -- it is an unmet
    // requirement -- but it carries the failing claim. Printing it as
    // "missing" would say the check never ran, and hide the id that has
    // the reason in it.
    const body: StatusBody = {
      tree: "T4",
      frozen: "F1",
      phase: "onboarding",
      accepted: false,
      rows: [{
        predicateType: "harness/self_check",
        status: "missing",
        verdict: "fail",
        claim_id: "c-0007",
        producing_action: "harness_self_check",
      }],
    };

    const line = renderStatus(body).split("\n")[1];

    expect(line).toBe("  harness/self_check  fail  c-0007  (fix and run harness_self_check again)");
    expect(line).not.toContain("missing");
  });

  it("uses the word the gateway sent for a finished region", () => {
    const text = renderStatus(body({ phase: "onboarding", accepted: true, finished_word: "ONBOARDED" }));

    expect(text.trim().endsWith("ONBOARDED")).toBe(true);
    expect(text).not.toContain("ACCEPTED");
  });

  it("falls back to its own word for a gateway that sends none", () => {
    expect(renderStatus(body({ phase: "onboarding", accepted: true })).trim().endsWith("ONBOARDED"))
      .toBe(true);
    expect(renderStatus(body({ phase: "porting", accepted: false })).trim().endsWith("not accepted"))
      .toBe(true);
  });

  it("keeps that fallback in step with the word the gateway would send", () => {
    const words = gatewayFinishedWords();
    expect(Object.keys(words).length).toBeGreaterThan(0);

    for (const [phase, word] of Object.entries(words)) {
      expect(renderStatus(body({ phase, accepted: true })).trim().endsWith(word)).toBe(true);
    }
  });

  it("prints the reason acceptance is withheld above the rows, and nothing when there is none", () => {
    const note = "Advisory only: nobody could confirm the executables.";
    const withNote = renderStatus(body({
      note,
      rows: [{ predicateType: "gpu/executed", status: "missing", producing_action: "run_replay" }],
    }));

    expect(withNote.split("\n")[0]).toBe(note);
    expect(renderStatus(body())).not.toContain("Advisory");
  });
});
