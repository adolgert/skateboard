/**
 * Trust role: none. Renders GET /status for a person; the gateway's
 * `accepted` field is the actual decision.
 */

export interface StatusRow {
  predicateType: string;
  status: "present" | "missing";
  verdict?: string;
  claim_id?: string;
  producing_action?: string;
}

export interface StatusBody {
  tree: string | null;
  frozen: string | null;
  phase: string;
  rows: StatusRow[];
  accepted: boolean;
  // Why acceptance is being withheld for a reason no row shows: nobody
  // could confirm the executables the claims were reached against. The
  // gateway sends the sentence; a gateway that predates it sends none.
  note?: string;
  // What this phase calls a region that has met every requirement. The
  // gateway sends it so this file does not have to know which word goes
  // with which phase; a gateway that predates it sends none.
  finished_word?: string;
}

/**
 * The word for a region that has met every requirement of its phase.
 * They differ because they mean different things: an onboarded code is
 * ready for a person to review and promote, an accepted port is ready to
 * merge. This is only the fallback for a gateway old enough not to send
 * the word with the status; it must stay in step with the gateway's own
 * map, and a test checks that it does.
 */
const FINISHED_WORD: Record<string, string> = {
  onboarding: "ONBOARDED",
  porting: "ACCEPTED",
};

function finishedWord(body: StatusBody): string {
  return body.finished_word ?? FINISHED_WORD[body.phase] ?? "ACCEPTED";
}

export function renderStatus(body: StatusBody): string {
  const lines: string[] = [];
  // First, as the ledger CLI prints it: a reader never sees the rows
  // without the sentence saying how far to trust them.
  if (body.note) lines.push(body.note);
  lines.push(`tree ${body.tree ?? "(none)"}  frozen ${body.frozen ?? "(none)"}`);
  for (const row of body.rows) {
    if (row.status === "present") {
      lines.push(`  ${row.predicateType}  ${row.verdict}  ${row.claim_id}`);
    } else if (row.verdict) {
      // The check ran and did not pass. That is still an unmet
      // requirement, but "missing" would say it never ran; the claim id
      // is there to be read, so the row shows it.
      lines.push(
        `  ${row.predicateType}  ${row.verdict}  ${row.claim_id}` +
          `  (fix and run ${row.producing_action} again)`,
      );
    } else {
      lines.push(`  ${row.predicateType}  missing  (run ${row.producing_action})`);
    }
  }
  const word = finishedWord(body);
  lines.push(body.accepted ? word : `not ${word.toLowerCase()}`);
  return lines.join("\n");
}
