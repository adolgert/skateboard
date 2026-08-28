/**
 * Trust role: none. A thin HTTP client for the gateway's four endpoints
 * (the same four `equivalent/client.py` calls on the Python side). Every
 * decision still happens in the gateway; this just shapes the request.
 */

import type { GatewayConfig } from "./config.js";
import type { ActionRow } from "./table.js";

export interface SessionContext {
  sessionManager: { getSessionId(): string };
  model?: { id: string };
}

function authHeaders(config: GatewayConfig): Record<string, string> {
  return { Authorization: `Bearer ${config.token}` };
}

/**
 * The tool-call id is pi's own identifier for the call being executed. It
 * goes on the wire so the gateway's request log and the session file name
 * the same call, and a reader can line the two up one for one instead of
 * guessing from order and whole-second timestamps.
 */
function sessionHeaders(
  config: GatewayConfig,
  ctx: SessionContext,
  toolCallId: string,
): Record<string, string> {
  return {
    ...authHeaders(config),
    "Content-Type": "application/json",
    "X-Session-Id": ctx.sessionManager.getSessionId(),
    "X-Model-Id": ctx.model?.id ?? "unknown",
    "X-Tool-Call-Id": toolCallId,
  };
}

/**
 * The table names a region because the actions a session has are the
 * actions of that region's phase: bringing a code in and porting a
 * region of one are different lists.
 */
export async function fetchTable(config: GatewayConfig): Promise<ActionRow[]> {
  const res = await fetch(`${config.url}/table?region=${encodeURIComponent(config.region)}`, {
    headers: authHeaders(config),
  });
  return (await res.json()) as ActionRow[];
}

export async function fetchStatus(config: GatewayConfig): Promise<unknown> {
  const res = await fetch(`${config.url}/status?region=${encodeURIComponent(config.region)}`, {
    headers: authHeaders(config),
  });
  return res.json();
}

/**
 * A submit names only the region. Which directory the gateway reads for
 * it is part of that region's own configuration, so nothing sent from
 * here can point the gateway at a different path.
 */
export async function postSubmit(
  config: GatewayConfig,
  ctx: SessionContext,
  toolCallId: string,
): Promise<unknown> {
  const res = await fetch(`${config.url}/submit`, {
    method: "POST",
    headers: sessionHeaders(config, ctx, toolCallId),
    body: JSON.stringify({ region: config.region }),
  });
  return res.json();
}

export async function postRun(
  config: GatewayConfig,
  action: string,
  ctx: SessionContext,
  toolCallId: string,
): Promise<unknown> {
  const res = await fetch(`${config.url}/run`, {
    method: "POST",
    headers: sessionHeaders(config, ctx, toolCallId),
    body: JSON.stringify({ action, region: config.region, config: {} }),
  });
  return res.json();
}
