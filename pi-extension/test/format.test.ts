import { describe, expect, it } from "vitest";

import { claimResultText, refusalText, runResultText, submitResultText } from "../src/format.js";

describe("runResultText", () => {
  it("renders a refusal listing every missing claim and its producing action, verbatim", () => {
    const body = {
      refused: true as const,
      action: "build_replay",
      tree: "abc123",
      missing: [{ predicateType: "sese/verified", status: "missing" as const, producing_action: "sese_check" }],
    };
    const text = runResultText("build_replay", body);
    expect(text).toContain("sese/verified");
    expect(text).toContain("sese_check");
    expect(text).toBe(refusalText(body));
  });

  it("renders a pass claim with its verdict and claim id", () => {
    const body = { claim_id: "c-1", verdict: "pass", detail: {} };
    expect(runResultText("sese_check", body)).toBe("sese_check: pass (c-1)");
  });

  it("puts the claim's detail under the verdict line, the keys that explain a fail first", () => {
    // The verdict alone tells a session nothing it can act on. What the
    // check found is in the detail, and the sentence naming the problem
    // must not be buried under the bookkeeping.
    const body = {
      claim_id: "c-0007",
      verdict: "fail",
      detail: { counts: { built: 3 }, problems: ["kernel launched 0 times"] },
    };

    const text = runResultText("run_replay", body);

    expect(text.split("\n")[0]).toBe("run_replay: fail (c-0007)");
    expect(text).toContain("kernel launched 0 times");
    expect(text.indexOf('"problems"')).toBeLessThan(text.indexOf('"counts"'));
  });

  it("caps a long fail detail and says where the rest is", () => {
    const body = {
      claim_id: "c-0007",
      verdict: "fail",
      detail: { problems: ["missing output h"], log: "x".repeat(40000) },
    };

    const text = runResultText("build_replay", body);

    expect(text).toContain("missing output h"); // the explaining key survived the cap
    expect(text.length).toBeLessThan(25000);
    expect(text.trimEnd().endsWith("the whole claim is in the ledger under c-0007.")).toBe(true);
  });

  it("caps a pass detail far shorter -- a pass rarely needs reading", () => {
    const body = { claim_id: "c-1", verdict: "pass", detail: { log: "x".repeat(40000) } };

    const text = runResultText("build_replay", body);

    expect(text.length).toBeLessThan(3500);
    expect(text).toContain("c-1");
  });

  it("renders a verdict-only claim as the verdict line alone", () => {
    // regression/holdout's receipt policy sends no detail at all; there
    // is nothing to print under the verdict.
    const body = { claim_id: "c-8", verdict: "pass" };
    expect(runResultText("regression_holdout", body)).toBe("regression_holdout: pass (c-8)");
  });

  it("renders each claim's detail in a multi-claim answer", () => {
    const body = {
      claims: [
        { predicateType: "sanitize/memcheck", claim_id: "c-2", verdict: "fail", detail: { reason: "invalid read" } },
        { predicateType: "sanitize/racecheck", claim_id: "c-3", verdict: "pass", detail: {} },
      ],
    };

    const text = runResultText("sanitize", body);

    expect(text).toContain("invalid read");
    expect(text.split("\n")[0]).toBe("sanitize/memcheck: fail (c-2)");
    expect(text.trimEnd().endsWith("sanitize/racecheck: pass (c-3)")).toBe(true);
  });

  it("renders a component error plainly", () => {
    const body = { error: "builder not configured" };
    expect(runResultText("build_replay", body)).toBe("error: builder not configured");
  });

  it("renders a multi-claim sanitize response, one line per tool", () => {
    const body = {
      claims: [
        { predicateType: "sanitize/memcheck", claim_id: "c-2", verdict: "pass", detail: {} },
        { predicateType: "sanitize/racecheck", claim_id: "c-3", verdict: "pass", detail: {} },
      ],
    };
    expect(runResultText("sanitize", body)).toBe("sanitize/memcheck: pass (c-2)\nsanitize/racecheck: pass (c-3)");
  });
});

describe("claimResultText", () => {
  it("reads a claim back the way the check's own answer read", () => {
    const body = {
      claim_id: "c-0007",
      predicateType: "gpu/executed",
      verdict: "fail",
      subject: [{ kind: "tree", sha256: "a".repeat(64) }],
      materials: [],
      detail: { reason: "no kernel launches observed" },
    };

    const text = claimResultText(body);

    expect(text.split("\n")[0]).toBe("gpu/executed: fail (c-0007)");
    expect(text).toContain("no kernel launches observed");
  });

  it("reports an unknown id as the gateway's own message", () => {
    expect(claimResultText({ error: "unknown claim: c-9999" })).toBe("error: unknown claim: c-9999");
  });
});

describe("submitResultText", () => {
  it("names ignored files in the receipt, in the gateway's {path, reason} shape", () => {
    const body = {
      tree: "T3",
      frozen: "F1",
      rejected: [{ path: "Makefile", reason: "not_allowed" }],
      committed: true,
    };
    expect(submitResultText(body)).toBe("submitted -> tree T3, frozen F1. ignored: Makefile (not_allowed).");
  });

  it("says nothing about ignored files when none were", () => {
    const body = { tree: "T1", frozen: "F0", rejected: [], not_sent: [], committed: true };
    expect(submitResultText(body)).toBe("submitted -> tree T1, frozen F0.");
  });

  it("warns about allowed files the agent did not send", () => {
    const body = {
      tree: "T2",
      frozen: "F0",
      rejected: [],
      not_sent: ["src/mod_kernel.f90"],
      committed: false,
    };
    expect(submitResultText(body)).toBe(
      "submitted -> tree T2, frozen F0. allowed but not sent: src/mod_kernel.f90.",
    );
  });
});
