import { mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { afterEach, describe, expect, it, vi } from "vitest";

import { createAgentSession, DefaultResourceLoader, getAgentDir, SessionManager } from "@earendil-works/pi-coding-agent";
import { fauxAssistantMessage, fauxProvider, fauxToolCall } from "@earendil-works/pi-ai";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const EXTENSION_PATH = path.join(__dirname, "..", "src", "extension.ts");

const ENV = {
  EQUIVALENT_GATEWAY_URL: "http://gateway.local",
  EQUIVALENT_GATEWAY_TOKEN: "test-token",
  EQUIVALENT_REGION: "ch04:step",
};

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), { status: 200, headers: { "content-type": "application/json" } });
}

/** One parsed line of a session file; only the fields this test reads. */
interface SessionLine {
  type: string;
  id?: string;
  message?: {
    role: string;
    content?: unknown;
  };
}

function readSessionLines(file: string): SessionLine[] {
  return readFileSync(file, "utf8")
    .split("\n")
    .filter((line) => line.trim().length > 0)
    .map((line) => JSON.parse(line) as SessionLine);
}

/**
 * Runs a whole pi session -- a scripted model, the real extension, a stubbed
 * gateway -- to show that the two halves of the record can be lined up: what
 * the gateway sees on the wire and what pi writes into the session file name
 * the same session and the same tool call.
 *
 * Registering the extension's tools takes `bindExtensions`; `createAgentSession`
 * alone never emits the session-start event the extension listens for, and the
 * model's tool call would come back as "not found".
 */
describe("a real pi session: the wire and the session file agree", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    for (const key of Object.keys(ENV)) delete process.env[key];
  });

  it("records the gateway call in the session file under the id it sent as a header", async () => {
    Object.assign(process.env, ENV);

    const table = [
      { name: "sese_check", emits: ["sese/verified"], requires: [], deterministic: true, component: "analyzer:check_sese" },
    ];
    const claim = { claim_id: "c-1", verdict: "pass", detail: { note: "ok" } };
    const requests: { href: string; headers: Record<string, string> }[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string | URL, init?: RequestInit) => {
        const href = String(url);
        requests.push({ href, headers: (init?.headers ?? {}) as Record<string, string> });
        if (href.includes("/table")) return jsonResponse(table);
        if (href.includes("/run")) return jsonResponse(claim);
        throw new Error(`unexpected fetch in sdk test: ${href}`);
      }),
    );

    const faux = fauxProvider();
    faux.setResponses([fauxAssistantMessage([fauxToolCall("sese_check", {})]), fauxAssistantMessage("done")]);

    const sessionDir = mkdtempSync(path.join(tmpdir(), "equivalent-pi-sessions-"));
    const loader = new DefaultResourceLoader({
      cwd: process.cwd(),
      agentDir: getAgentDir(),
      additionalExtensionPaths: [EXTENSION_PATH],
      extensionFactories: [(pi: any) => pi.registerProvider(faux.provider)],
    });
    await loader.reload();

    const { session } = await createAgentSession({
      model: faux.getModel(),
      resourceLoader: loader,
      sessionManager: SessionManager.create(process.cwd(), sessionDir),
    });

    try {
      const extensionErrors: unknown[] = [];
      await session.bindExtensions({ mode: "print", onError: (err) => extensionErrors.push(err) });
      expect(extensionErrors).toEqual([]);
      expect(session.getActiveToolNames()).toContain("sese_check");

      await session.prompt("run the sese check");

      const sessionFile = session.sessionFile;
      expect(sessionFile).toBeTruthy();
      expect(path.dirname(sessionFile as string)).toBe(sessionDir);
      const lines = readSessionLines(sessionFile as string);

      const header = lines[0];
      expect(header.type).toBe("session");
      const sessionId = header.id;

      const toolCalls = lines
        .filter((line) => line.message?.role === "assistant")
        .flatMap((line) => (line.message?.content ?? []) as { type: string; id: string; name: string }[])
        .filter((block) => block.type === "toolCall");
      expect(toolCalls.map((call) => call.name)).toEqual(["sese_check"]);

      const results = lines
        .filter((line) => line.message?.role === "toolResult")
        .map((line) => line.message as unknown as { toolCallId: string; toolName: string; isError: boolean; details: unknown });
      expect(results).toHaveLength(1);
      expect(results[0].isError).toBe(false);
      expect(results[0].details).toEqual(claim);

      const run = requests.find((req) => req.href.includes("/run"));
      expect(run).toBeTruthy();
      expect(run?.headers["X-Session-Id"]).toBe(sessionId);
      expect(run?.headers["X-Model-Id"]).toBe(faux.getModel().id);
      expect(run?.headers["X-Tool-Call-Id"]).toBe(toolCalls[0].id);
      expect(results[0].toolCallId).toBe(run?.headers["X-Tool-Call-Id"]);
    } finally {
      session.dispose();
    }
  }, 20_000);
});
