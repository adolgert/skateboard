import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import equivalentExtension from "../src/extension.js";

const ENV = {
  EQUIVALENT_GATEWAY_URL: "http://gateway.local",
  EQUIVALENT_GATEWAY_TOKEN: "test-token",
  EQUIVALENT_REGION: "ch04:step",
};

function fakePi() {
  const tools = new Map<string, any>();
  const commands = new Map<string, any>();
  const handlers = new Map<string, any>();
  return {
    tools,
    commands,
    handlers,
    on(event: string, handler: any) {
      handlers.set(event, handler);
    },
    registerTool(def: any) {
      tools.set(def.name, def);
    },
    registerCommand(name: string, def: any) {
      commands.set(name, def);
    },
  };
}

function fakeCtx() {
  return {
    ui: { notify: vi.fn() },
    sessionManager: { getSessionId: () => "sess-1" },
    model: { id: "claude-sonnet-5" },
  };
}

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), { status: 200, headers: { "content-type": "application/json" } });
}

describe("equivalentExtension", () => {
  beforeEach(() => {
    Object.assign(process.env, ENV);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    for (const key of Object.keys(ENV)) delete process.env[key];
  });

  it("registers one tool per callable row plus submit and status, skipping accept", async () => {
    const table = [
      { name: "sese_check", emits: ["sese/verified"], requires: [], deterministic: true, component: "analyzer:check_sese" },
      { name: "accept", emits: [], requires: [], deterministic: true, component: null },
    ];
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse(table)));

    const pi = fakePi();
    equivalentExtension(pi as any);
    await pi.handlers.get("session_start")({}, fakeCtx());

    expect([...pi.tools.keys()].sort()).toEqual(["sese_check", "status", "submit"]);
  });

  it("a refusal from /run becomes the tool's result text, missing claims verbatim", async () => {
    const table = [
      {
        name: "build_replay",
        emits: ["build/replay"],
        requires: [["sese/verified", "frozen"]],
        deterministic: true,
        component: "builder:/v1/build",
      },
    ];
    const refusal = {
      refused: true,
      action: "build_replay",
      tree: "T1",
      missing: [{ predicateType: "sese/verified", status: "missing", producing_action: "sese_check" }],
    };
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(table))
      .mockResolvedValueOnce(jsonResponse(refusal));
    vi.stubGlobal("fetch", fetchMock);

    const pi = fakePi();
    equivalentExtension(pi as any);
    await pi.handlers.get("session_start")({}, fakeCtx());

    const result = await pi.tools.get("build_replay").execute("call-1", {}, undefined, undefined, fakeCtx());
    expect(result.content[0].text).toContain("sese/verified");
    expect(result.content[0].text).toContain("sese_check");
  });

  it("sends the session id and model id as headers on every /run and /submit call", async () => {
    const table = [
      { name: "sese_check", emits: ["sese/verified"], requires: [], deterministic: true, component: "analyzer:check_sese" },
    ];
    const claim = { claim_id: "c-1", verdict: "pass", detail: {} };
    const receipt = { tree: "T1", frozen: "F0", rejected: [], committed: true };
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(table))
      .mockResolvedValueOnce(jsonResponse(claim))
      .mockResolvedValueOnce(jsonResponse(receipt));
    vi.stubGlobal("fetch", fetchMock);

    const pi = fakePi();
    equivalentExtension(pi as any);
    const ctx = fakeCtx();
    await pi.handlers.get("session_start")({}, ctx);

    await pi.tools.get("sese_check").execute("call-1", {}, undefined, undefined, ctx);
    await pi.tools.get("submit").execute("call-2", { working_copy_dir: "/work" }, undefined, undefined, ctx);

    const runHeaders = fetchMock.mock.calls[1][1].headers;
    const submitHeaders = fetchMock.mock.calls[2][1].headers;
    expect(runHeaders["X-Session-Id"]).toBe("sess-1");
    expect(runHeaders["X-Model-Id"]).toBe("claude-sonnet-5");
    expect(submitHeaders["X-Session-Id"]).toBe("sess-1");
    expect(submitHeaders["X-Model-Id"]).toBe("claude-sonnet-5");
  });
});
