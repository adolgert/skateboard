/**
 * Trust role: none. Delete this and no claim changes -- the gateway
 * remains the only thing that decides anything. This just registers one
 * tool per gateway action, plus `submit` and `status`, and forwards
 * calls with the session id and model id attached so the gateway's
 * request log can be joined with the session files.
 */

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

import { type GatewayConfig, readConfig } from "./config.js";
import { fetchStatus, fetchTable, postRun, postSubmit } from "./gateway.js";
import { runResultText, submitResultText, type RunBody, type SubmitBody } from "./format.js";
import { renderStatus, type StatusBody } from "./status.js";
import { callableRows, describeRow } from "./table.js";

export default function equivalentExtension(pi: ExtensionAPI) {
  let config: GatewayConfig | undefined;

  pi.on("session_start", async (_event, ctx) => {
    try {
      config = readConfig();
    } catch (err) {
      ctx.ui.notify((err as Error).message, "error");
      return;
    }
    const cfg = config;

    const table = await fetchTable(cfg);
    const rows = callableRows(table);

    for (const row of rows) {
      pi.registerTool({
        name: row.name,
        label: row.name,
        description: describeRow(row),
        parameters: Type.Object({}),
        async execute(_toolCallId, _params, _signal, _onUpdate, execCtx) {
          const body = (await postRun(cfg, row.name, execCtx)) as RunBody;
          return {
            content: [{ type: "text", text: runResultText(row.name, body) }],
            details: body as unknown as Record<string, unknown>,
          };
        },
      });
    }

    pi.registerTool({
      name: "submit",
      label: "Submit",
      description:
        "Send the region's working copy to the gateway. Takes no arguments: the gateway " +
        "reads the working copy this session edits. Only files on the region's " +
        "allow-list are kept; anything else is ignored and named in the receipt. " +
        "Returns the resulting tree and frozen hashes.",
      parameters: Type.Object({}),
      async execute(_toolCallId, _params, _signal, _onUpdate, execCtx) {
        const body = (await postSubmit(cfg, execCtx)) as SubmitBody;
        return {
          content: [{ type: "text", text: submitResultText(body) }],
          details: body as unknown as Record<string, unknown>,
        };
      },
    });

    pi.registerTool({
      name: "status",
      label: "Status",
      description: "Report the region's current tree, which claims are present, and which are missing and why.",
      parameters: Type.Object({}),
      async execute() {
        const body = (await fetchStatus(cfg)) as StatusBody;
        return {
          content: [{ type: "text", text: renderStatus(body) }],
          details: body as unknown as Record<string, unknown>,
        };
      },
    });

    ctx.ui.notify(`equivalent: registered ${rows.length + 2} tools for region ${cfg.region}`, "info");
  });

  pi.registerCommand("status", {
    description: "Print the region's status from the gateway",
    handler: async (_args, ctx) => {
      if (!config) {
        ctx.ui.notify("equivalent: not configured (session_start failed)", "error");
        return;
      }
      const body = (await fetchStatus(config)) as StatusBody;
      ctx.ui.notify(renderStatus(body), "info");
    },
  });
}
