// default-deny — anything that doesn't match is rejected
// otherwise:
//
//   user@domain            -> bare address, accepted to QUARANTINE (capped)
//   user+tag@domain        -> tagged (RFC 5233 subaddressing)
//   user__tag@domain       -> tagged (fallback for signup forms that reject "+")
//
// `user` must exist in the USERS KV namespace
// in "strict" mode `tag` must exist in the TAGS KV namespace too

export type Disposition = "archive" | "quarantine";
export type PolicyMode = "strict" | "quarantine";

export interface ParsedRecipient {
  user: string;
  tag: string | null;
  separator: "+" | "__" | null;
}

export interface UserRecord {
  active: boolean;
}

export interface TagRecord {
  status: "approved" | "pending" | "blocked";
  // Optional envelope-sender binding, e.g. "mail.ramenweekly.example".
  // When set, mail whose MAIL FROM domain differs is quarantined, which blunts
  // both spoofing and subscription-bombing against known tags.
  sender_domain?: string;
  // Eligible for the delayed publication pipeline (processor `publish` pass).
  publish?: boolean;
  // Most each link capture from this newsletter may take, in MiB (dea-capture
  // defers bigger ones); mirrored to R2 for it by the dashboard. None: the
  // capture stages' own caps.
  capture_limit_mb?: number;
  // Admin metadata below; ingest ignores it.
  name?: string; // display name, e.g. "Ramen Weekly"
  description?: string;
  signup_url?: string; // where the newsletter's sign-up form lives (http/https only)
  note?: string; // private admin note
}

// Reasons carry no SMTP status codes — the platform (Cloudflare) or adapter
// supplies those. KEEP IN PARITY with processor/src/processor/policy.py.
export type Decision =
  | { action: "reject"; reason: string } // permanent SMTP reject (setReject)
  | { action: "defer"; reason: string } // transient failure (throw) -> sender retries
  | { action: "accept"; disposition: Disposition; user: string; tag: string | null };

// user: starts alnum, then alnum/dot/hyphen (no underscore, so "__" is an
// unambiguous separator). tag: starts alnum, then alnum/dot/underscore/hyphen.
const USER_PART = "[a-z0-9][a-z0-9.-]{0,62}";
const TAG_PART = "[a-z0-9][a-z0-9._-]{0,62}";
const LOCAL_RE = new RegExp(`^(${USER_PART})(?:(\\+|__)(${TAG_PART}))?$`);
const USER_RE = new RegExp(`^${USER_PART}$`);
const TAG_RE = new RegExp(`^${TAG_PART}$`);

// for admin input: names that parseRecipient could ever produce
export function isValidUser(name: string): boolean {
  return USER_RE.test(name);
}

export function isValidTag(name: string): boolean {
  return TAG_RE.test(name);
}

export function parseRecipient(address: string): ParsedRecipient | null {
  const at = address.lastIndexOf("@");
  if (at <= 0) return null;
  const local = address.slice(0, at).trim().toLowerCase();
  const m = LOCAL_RE.exec(local);
  if (!m) return null;
  return {
    user: m[1],
    tag: m[3] ?? null,
    separator: (m[2] as "+" | "__" | undefined) ?? null,
  };
}

export function envelopeFromDomain(envelopeFrom: string): string | null {
  const at = envelopeFrom.lastIndexOf("@");
  if (at <= 0) return null;
  return envelopeFrom.slice(at + 1).trim().toLowerCase() || null;
}

// Recipient domain (the part after @), lowercased. The ingest Worker checks
// this against ARCHIVE_DOMAIN: Cloudflare's catch-all is ZONE-WIDE (there is no
// per-subdomain catch-all), so when mail is received at a subdomain the Worker
// must scope ingestion to that subdomain itself. KEEP IN PARITY with
// processor/src/processor/policy.py (recipient_domain) and the smtpd adapter's
// own domain check.
export function recipientDomain(address: string): string | null {
  const at = address.lastIndexOf("@");
  if (at <= 0) return null;
  return address.slice(at + 1).trim().toLowerCase() || null;
}

export function decide(args: {
  rec: ParsedRecipient;
  userRecord: UserRecord | null;
  tagRecord: TagRecord | null;
  mode: PolicyMode;
  envelopeFromDomain: string | null;
}): Decision {
  const { rec, userRecord, tagRecord, mode } = args;

  if (!userRecord || !userRecord.active) {
    // Unknown/disabled user -> permanent reject
    return { action: "reject", reason: "no such recipient" };
  }

  if (rec.tag === null) {
    // bare address 
    // some double-opt-in flows mail the base address
    // so capture it 
    // but only into quarantine
    return { action: "accept", disposition: "quarantine", user: rec.user, tag: null };
  }

  if (tagRecord) {
    if (tagRecord.status === "blocked") {
      return { action: "reject", reason: "recipient unavailable" };
    }
    if (
      tagRecord.sender_domain &&
      args.envelopeFromDomain &&
      tagRecord.sender_domain !== args.envelopeFromDomain
    ) {
      return { action: "accept", disposition: "quarantine", user: rec.user, tag: rec.tag };
    }
    if (tagRecord.status === "approved") {
      return { action: "accept", disposition: "archive", user: rec.user, tag: rec.tag };
    }
    // "pending" tags are captured but not archived until promoted.
    return { action: "accept", disposition: "quarantine", user: rec.user, tag: rec.tag };
  }

  // Unknown tag.
  if (mode === "strict") {
    return { action: "reject", reason: "no such recipient" };
  }
  return { action: "accept", disposition: "quarantine", user: rec.user, tag: rec.tag };
}
