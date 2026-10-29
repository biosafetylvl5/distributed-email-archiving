// "Retry now". The dashboard never edits a capture record: a capture that is
// running would save over the edit. It leaves a request under captures/retry/
// instead, which dea-capture applies within about 5 minutes, before it rebuilds
// the summary (processor/src/dea_capture/retry.py). What each did comes back
// in the summary's `retries`.

import type { Env } from "../env";
import { URL_KINDS, type UrlKind } from "./captures";

export const RETRY_PREFIX = "captures/retry/";
export const RETRY_RESULTS_KEY = "dashboard/retries.json";
export const RETRYABLE = ["retrying", "failed"] as const;
export type Retryable = (typeof RETRYABLE)[number];

const SHA_RE = /^[0-9a-f]{64}$/;
const WAITING_MAX = 1000;

export type RetryRequest =
  | { scope: "message"; sha: string } // follow its failed redirects again, then retry its URLs
  | { scope: "urls"; kind: UrlKind; status: Retryable };

// counts only; `sha` names an archived message, as on /captures
export interface RetryResult {
  appliedAt: Date | null;
  scope: string;
  sha?: string;
  kind?: string;
  status?: string;
  rescheduled?: number;
  left?: number;
  relinked?: { tried: number; resolved: number; rateLimited: boolean; outOfTime: boolean };
  error?: string;
}

export interface Retries {
  waiting: number; // requests not applied yet
  recent: RetryResult[]; // newest first
}

export function parseRetryRequest(fields: Record<string, string>): RetryRequest | string {
  if (fields.scope === "message") {
    return SHA_RE.test(fields.sha ?? "") ? { scope: "message", sha: fields.sha } : "Invalid message";
  }
  if (fields.scope === "urls") {
    const kind = fields.kind as UrlKind;
    const status = fields.status as Retryable;
    if (!URL_KINDS.includes(kind)) return `Invalid kind: ${fields.kind}`;
    if (!RETRYABLE.includes(status)) return `Invalid status: ${fields.status}`;
    return { scope: "urls", kind, status };
  }
  return "Invalid retry request";
}

// YYYYMMDDTHHMMSSZ, so the processor applies requests oldest first
function stamp(d: Date): string {
  return d.toISOString().replace(/[-:]/g, "").replace(/\.\d+Z$/, "Z");
}

export async function requestRetry(env: Env, req: RetryRequest, now = new Date()): Promise<string> {
  const id = [...crypto.getRandomValues(new Uint8Array(8))].map((b) => b.toString(16).padStart(2, "0")).join("");
  const key = `${RETRY_PREFIX}${stamp(now)}-${id}.json`;
  await env.ARCHIVE.put(key, JSON.stringify({ ...req, requested_at: now.toISOString() }), {
    httpMetadata: { contentType: "application/json" },
  });
  return key;
}

// one listing: there are only ever a few
export async function waitingRetries(env: Env): Promise<number> {
  const got = await env.ARCHIVE.list({ prefix: RETRY_PREFIX, limit: WAITING_MAX });
  return got.objects.filter((o) => o.key.endsWith(".json")).length;
}

function count(v: unknown): number | undefined {
  return typeof v === "number" && Number.isFinite(v) ? v : undefined;
}

function str(v: unknown): string | undefined {
  return typeof v === "string" ? v : undefined;
}

// the processor's results, newest first; written outside the Worker, so checked
export function parseRetryResults(raw: unknown): RetryResult[] {
  if (!Array.isArray(raw)) return [];
  return raw
    .filter((r): r is Record<string, unknown> => typeof r === "object" && r !== null)
    .map((r) => {
      const at = str(r.applied_at) ? new Date(r.applied_at as string) : null;
      const rl = typeof r.relinked === "object" && r.relinked !== null ? (r.relinked as Record<string, unknown>) : null;
      const sha = str(r.sha);
      return {
        appliedAt: at && !Number.isNaN(at.getTime()) ? at : null,
        scope: str(r.scope) ?? "",
        sha: sha && SHA_RE.test(sha) ? sha : undefined,
        kind: str(r.kind),
        status: str(r.status),
        rescheduled: count(r.rescheduled),
        left: count(r.left),
        relinked: rl
          ? {
              tried: count(rl.tried) ?? 0,
              resolved: count(rl.resolved) ?? 0,
              rateLimited: rl.rate_limited === true,
              outOfTime: rl.out_of_time === true,
            }
          : undefined,
        error: str(r.error),
      };
    });
}

export function parseRetries(raw: unknown): Retries {
  const r = typeof raw === "object" && raw !== null ? (raw as Record<string, unknown>) : {};
  return { waiting: count(r.waiting) ?? 0, recent: parseRetryResults(r.recent) };
}

// without a summary object (a new install)
export async function liveRetries(env: Env): Promise<Retries> {
  const [waiting, obj] = await Promise.all([waitingRetries(env), env.ARCHIVE.get(RETRY_RESULTS_KEY)]);
  let recent: unknown = [];
  try {
    recent = obj ? JSON.parse(await obj.text()) : [];
  } catch {
    recent = [];
  }
  return { waiting, recent: parseRetryResults(recent) };
}
