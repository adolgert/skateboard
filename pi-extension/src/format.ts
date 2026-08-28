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
  detail?: unknown; // absent when the predicate's receipt policy is verdict-only
}

export interface MultiClaimBody {
  claims: { predicateType: string; claim_id: string; verdict: string; detail?: unknown }[];
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

/**
 * The keys of a detail that say why a check came out the way it did,
 * shown before the rest. A failing check's detail is often mostly
 * bookkeeping -- counts, digests, per-case tables -- and the sentence
 * that names the problem can be anywhere in it. Putting these first
 * means the reason survives the cap below.
 */
const EXPLAINING_KEYS = ["reason", "problems", "gap", "failed_strategies", "log_tail"];

/**
 * How much of a claim's detail the tool result carries. A fail is what a
 * session has to read and act on, so it gets nearly all of it; a pass
 * rarely needs reading, so it gets enough to see what was checked. Past
 * either cap, the claim id is what to read the rest by.
 */
const FAIL_DETAIL_LIMIT = 24000;
const PASS_DETAIL_LIMIT = 3000;

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function explainingFirst(detail: Record<string, unknown>): Record<string, unknown> {
  const ordered: Record<string, unknown> = {};
  for (const key of EXPLAINING_KEYS) {
    if (Object.prototype.hasOwnProperty.call(detail, key)) ordered[key] = detail[key];
  }
  for (const key of Object.keys(detail)) {
    if (!Object.prototype.hasOwnProperty.call(ordered, key)) ordered[key] = detail[key];
  }
  return ordered;
}

/**
 * The claim's detail as the model reads it: pretty-printed JSON, the
 * keys that explain a failure first, cut off at the verdict's cap with a
 * line saying where the whole thing is. A claim the gateway sent no
 * detail for -- a verdict-only predicate, or a check that recorded
 * nothing -- renders as nothing at all, so the verdict line stands alone.
 */
export function detailText(claim: ClaimBody): string {
  const detail = claim.detail;
  if (detail === undefined || detail === null) return "";
  if (isPlainObject(detail) && Object.keys(detail).length === 0) return "";
  const pretty = JSON.stringify(isPlainObject(detail) ? explainingFirst(detail) : detail, null, 2);
  if (pretty === undefined) return "";
  const limit = claim.verdict === "pass" ? PASS_DETAIL_LIMIT : FAIL_DETAIL_LIMIT;
  if (pretty.length <= limit) return pretty;
  return (
    pretty.slice(0, limit) +
    `\n... ${pretty.length - limit} more characters of detail; the whole claim is in the ` +
    `ledger under ${claim.claim_id}.`
  );
}

/** One claim as the model reads it: the verdict line, then the detail. */
export function claimText(label: string, claim: ClaimBody): string {
  const verdict = `${label}: ${claim.verdict} (${claim.claim_id})`;
  const detail = detailText(claim);
  return detail === "" ? verdict : `${verdict}\n${detail}`;
}

export function runResultText(actionName: string, body: RunBody): string {
  if (isError(body)) return `error: ${body.error}`;
  if (isRefusal(body)) return refusalText(body);
  if (isMultiClaim(body)) {
    return body.claims.map((c) => claimText(c.predicateType, c)).join("\n");
  }
  return claimText(actionName, body as ClaimBody);
}

/**
 * A claim read back by id. The gateway answers with the same receipt a
 * check's own result carries, plus what the claim is about, so it reads
 * the same way: the verdict line, then whatever detail the receipt
 * policy allows.
 */
export interface ClaimReadBody extends ClaimBody {
  predicateType: string;
  subject?: { kind: string; sha256: string }[];
  materials?: { kind: string; sha256: string }[];
}

export function claimResultText(body: ClaimReadBody | ErrorBody): string {
  if (isError(body)) return `error: ${body.error}`;
  const claim = body as ClaimReadBody;
  return claimText(claim.predicateType ?? claim.claim_id, claim);
}

export interface SubmitBody {
  tree: string;
  frozen: string;
  rejected: { path: string; reason: string }[] | string[];
  not_sent?: string[];
  committed: boolean;
}

function rejectedName(entry: { path: string; reason: string } | string): string {
  return typeof entry === "string" ? entry : `${entry.path} (${entry.reason})`;
}

export function submitResultText(body: SubmitBody): string {
  const rejected =
    body.rejected.length > 0 ? ` ignored: ${body.rejected.map(rejectedName).join(", ")}.` : "";
  const notSent =
    body.not_sent && body.not_sent.length > 0
      ? ` allowed but not sent: ${body.not_sent.join(", ")}.`
      : "";
  return `submitted -> tree ${body.tree}, frozen ${body.frozen}.${rejected}${notSent}`;
}
