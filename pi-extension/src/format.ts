/**
 * Trust role: none. This renders the gateway's own response into the
 * text a tool call returns to the model; it adds no claims and checks
 * nothing.
 */

export interface MissingItem {
  predicateType: string;
  status: "missing";
  producing_action?: string;
}

export interface RefusalBody {
  refused: true;
  action: string;
  tree: string | null;
  missing: MissingItem[];
}

export interface ClaimBody {
  claim_id: string;
  verdict: string;
  detail: unknown;
}

export interface MultiClaimBody {
  claims: { predicateType: string; claim_id: string; verdict: string; detail: unknown }[];
}

export interface ErrorBody {
  error: string;
}

export type RunBody = RefusalBody | ClaimBody | MultiClaimBody | ErrorBody;

function isRefusal(body: RunBody): body is RefusalBody {
  return (body as RefusalBody).refused === true;
}

function isError(body: RunBody): body is ErrorBody {
  return typeof (body as ErrorBody).error === "string";
}

function isMultiClaim(body: RunBody): body is MultiClaimBody {
  return Array.isArray((body as MultiClaimBody).claims);
}

export function refusalText(body: RefusalBody): string {
  const lines = body.missing.map(
    (m) => `  - ${m.predicateType} is missing; run ${m.producing_action ?? "?"} to produce it.`,
  );
  return [`refused: '${body.action}' requires:`, ...lines].join("\n");
}

export function runResultText(actionName: string, body: RunBody): string {
  if (isError(body)) return `error: ${body.error}`;
  if (isRefusal(body)) return refusalText(body);
  if (isMultiClaim(body)) {
    return body.claims.map((c) => `${c.predicateType}: ${c.verdict} (${c.claim_id})`).join("\n");
  }
  const claim = body as ClaimBody;
  return `${actionName}: ${claim.verdict} (${claim.claim_id})`;
}

export interface SubmitBody {
  tree: string;
  frozen: string;
  rejected: string[];
  committed: boolean;
}

export function submitResultText(body: SubmitBody): string {
  const rejected = body.rejected.length > 0 ? ` ignored: ${body.rejected.join(", ")}.` : "";
  return `submitted -> tree ${body.tree}, frozen ${body.frozen}.${rejected}`;
}
