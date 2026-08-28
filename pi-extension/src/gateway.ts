/**
 * Trust role: none. A thin HTTP client for the gateway's five endpoints
 * (the same five `equivalent/client.py` calls on the Python side). Every
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
function callerHeaders(
  config: GatewayConfig,
  ctx: SessionContext,
  toolCallId: string,
): Record<string, string> {
  return {
    ...authHeaders(config),
    "X-Session-Id": ctx.sessionManager.getSessionId(),
    "X-Model-Id": ctx.model?.id ?? "unknown",
    "X-Tool-Call-Id": toolCallId,
  };
}

function sessionHeaders(
  config: GatewayConfig,
  ctx: SessionContext,
  toolCallId: string,
): Record<string, string> {
  return { ...callerHeaders(config, ctx, toolCallId), "Content-Type": "application/json" };
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

/**
 * The run config carries the settings the action declared and the model
 * chose to set. It is built by the caller from the row, so a key the
 * gateway never offered cannot reach the request body from here.
 */
export async function postRun(
  config: GatewayConfig,
  action: string,
  ctx: SessionContext,
  toolCallId: string,
  runConfig: Record<string, unknown> = {},
): Promise<unknown> {
  const res = await fetch(`${config.url}/run`, {
    method: "POST",
    headers: sessionHeaders(config, ctx, toolCallId),
    body: JSON.stringify({ action, region: config.region, config: runConfig }),
  });
  return res.json();
}

/**
 * One claim of this region, read back by id. The gateway logs the read
 * like any other call, so it carries the same identifying headers; what
 * it answers with is filtered by the same receipt policy the check's own
 * answer went through. A 404 or any other failure comes back as an error
 * body, so the caller has one shape to render either way.
 */
export async function fetchClaim(
  config: GatewayConfig,
  ctx: SessionContext,
  toolCallId: string,
  claimId: string,
): Promise<unknown> {
  const res = await fetch(
    `${config.url}/claims/${encodeURIComponent(claimId)}?region=${encodeURIComponent(config.region)}`,
    { headers: callerHeaders(config, ctx, toolCallId) },
  );
  const body = (await res.json()) as Record<string, unknown>;
  if (res.ok) return body;
  return {
    error: typeof body.detail === "string" ? body.detail : `claim lookup failed (${res.status})`,
  };
}
