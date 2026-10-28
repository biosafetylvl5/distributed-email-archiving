// capture progress from dea-capture's issue-status/<sha>.json objects and its
// captures/index/<h>.json records, read from R2 listings of their
// customMetadata (counts, states and the source key only; no bodies, manifests
// or URLs are read)

import type { Env } from "../env";
import type { StoredObject } from "./stats";

const STATUS_PREFIX = "issue-status/";
const INDEX_PREFIX = "captures/index/";
const MAX_PAGES = 25;

export type CaptureState = "done" | "partial" | "pending";

const COUNT_FIELDS = [
  "assets_ok",
  "assets_failed",
  "assets_refused",
  "assets_deferred",
  "pixels",
  "links",
  "links_capture",
  "links_resolve",
  "links_skip",
  "links_resolved",
  "links_unresolved",
  "links_refused",
  "captures_queued",
] as const;
export type CountField = (typeof COUNT_FIELDS)[number];
export type Counts = Record<CountField, number>;

export interface IssueCapture {
  sha: string;
  source: string; // R2 key of the .eml
  state: Exclude<CaptureState, "pending">;
  linksState: CaptureState; // pending until the links are resolved and queued
  completedAt: Date | null;
  counts: Counts;
}

export interface ArchivedMessage {
  key: string;
  day: string;
  user: string;
  tag: string;
  capture: IssueCapture | null; // null: not processed yet
}

export const URL_KINDS = ["page", "direct", "media", "gallery"] as const;
export type UrlKind = (typeof URL_KINDS)[number];
export const URL_STATUSES = ["queued", "retrying", "ok", "failed", "refused", "deferred"] as const;
export type UrlStatus = (typeof URL_STATUSES)[number];

// one record per canonical URL, however many messages link to it
export interface UrlCaptures {
  total: number;
  byKind: Record<UrlKind, Record<UrlStatus, number>>;
  wayback: number; // failed URLs with a Wayback Machine snapshot instead
  lastUpdated: Date | null;
}

export interface CaptureSummary {
  partial: boolean; // hit MAX_PAGES on any listing
  messages: ArchivedMessage[]; // newest first
  states: Record<CaptureState, number>;
  linkStates: Record<CaptureState, number>; // of processed messages
  urls: UrlCaptures;
  totals: Counts;
  lastCompleted: Date | null;
  bySubscription: Map<string, Record<CaptureState, number>>; // keyed "user/tag"
}

const ARCHIVE_KEY_RE = /^incoming\/archive\/(\d{4}-\d{2}-\d{2})\//;

function zeroCounts(): Counts {
  return Object.fromEntries(COUNT_FIELDS.map((f) => [f, 0])) as Counts;
}

// the stage records its source as s3://<bucket>/<key>; only the key matters here
function sourceKey(source: string): string {
  const m = /^s3:\/\/[^/]+\/(.+)$/.exec(source);
  return m ? m[1] : source;
}

function count(v: string | undefined): number {
  const n = Number.parseInt(v ?? "", 10);
  return Number.isFinite(n) && n >= 0 ? n : 0;
}

export function parseStatus(obj: StoredObject): IssueCapture | null {
  const meta = obj.customMetadata ?? {};
  const sha = /^issue-status\/([0-9a-f]{64})\.json$/.exec(obj.key)?.[1];
  if (!sha || !meta.source) return null;
  const counts = zeroCounts();
  for (const f of COUNT_FIELDS) counts[f] = count(meta[f]);
  const when = meta.completed_at ? new Date(meta.completed_at) : null;
  return {
    sha,
    source: sourceKey(meta.source),
    state: meta.state === "done" ? "done" : "partial",
    linksState: meta.links_state === "done" ? "done" : meta.links_state === "partial" ? "partial" : "pending",
    completedAt: when && !Number.isNaN(when.getTime()) ? when : null,
    counts,
  };
}

export function summarizeIndex(records: StoredObject[]): UrlCaptures {
  const byKind = Object.fromEntries(
    URL_KINDS.map((k) => [k, Object.fromEntries(URL_STATUSES.map((s) => [s, 0]))]),
  ) as UrlCaptures["byKind"];
  let total = 0;
  let wayback = 0;
  let lastUpdated: Date | null = null;
  for (const r of records) {
    if (!/^captures\/index\/[0-9a-f]{64}\.json$/.test(r.key)) continue;
    const meta = r.customMetadata ?? {};
    const kind = (URL_KINDS as readonly string[]).includes(meta.kind ?? "") ? (meta.kind as UrlKind) : null;
    const st = (URL_STATUSES as readonly string[]).includes(meta.status ?? "") ? (meta.status as UrlStatus) : null;
    if (!kind || !st) continue;
    total++;
    byKind[kind][st]++;
    if (meta.wayback) wayback++;
    const when = meta.updated_at ? new Date(meta.updated_at) : null;
    if (when && !Number.isNaN(when.getTime()) && (!lastUpdated || when > lastUpdated)) lastUpdated = when;
  }
  return { total, byKind, wayback, lastUpdated };
}

export function summarizeCaptures(
  archived: StoredObject[],
  statuses: StoredObject[],
  partial = false,
  index: StoredObject[] = [],
): CaptureSummary {
  const bySource = new Map<string, IssueCapture>();
  for (const s of statuses) {
    const c = parseStatus(s);
    if (c) bySource.set(c.source, c);
  }
  const states: Record<CaptureState, number> = { done: 0, partial: 0, pending: 0 };
  const linkStates: Record<CaptureState, number> = { done: 0, partial: 0, pending: 0 };
  const totals = zeroCounts();
  const bySubscription = new Map<string, Record<CaptureState, number>>();
  let lastCompleted: Date | null = null;
  const messages: ArchivedMessage[] = [];

  for (const obj of archived) {
    const day = ARCHIVE_KEY_RE.exec(obj.key)?.[1];
    if (!day) continue;
    const meta = obj.customMetadata ?? {};
    const capture = bySource.get(obj.key) ?? null;
    const state: CaptureState = capture?.state ?? "pending";
    states[state]++;
    if (capture) {
      linkStates[capture.linksState]++;
      for (const f of COUNT_FIELDS) totals[f] += capture.counts[f];
      if (capture.completedAt && (!lastCompleted || capture.completedAt > lastCompleted)) {
        lastCompleted = capture.completedAt;
      }
    }
    const user = meta.user ?? "";
    const tag = meta.tag ?? "";
    if (user) {
      const id = `${user}/${tag}`;
      const s = bySubscription.get(id) ?? { done: 0, partial: 0, pending: 0 };
      s[state]++;
      bySubscription.set(id, s);
    }
    messages.push({ key: obj.key, day, user, tag, capture });
  }
  messages.sort((a, b) => b.key.localeCompare(a.key));
  return { partial, messages, states, linkStates, urls: summarizeIndex(index), totals, lastCompleted, bySubscription };
}

async function listAll(env: Env, prefix: string): Promise<{ objects: StoredObject[]; partial: boolean }> {
  const objects: StoredObject[] = [];
  let cursor: string | undefined;
  let pages = 0;
  do {
    const page = await env.ARCHIVE.list({ prefix, cursor, include: ["customMetadata"] });
    objects.push(...page.objects);
    cursor = page.truncated ? page.cursor : undefined;
  } while (cursor && ++pages < MAX_PAGES);
  return { objects, partial: Boolean(cursor) };
}

export async function captureSummary(env: Env): Promise<CaptureSummary> {
  const [archived, statuses, index] = await Promise.all([
    listAll(env, "incoming/archive/"),
    listAll(env, STATUS_PREFIX),
    listAll(env, INDEX_PREFIX),
  ]);
  return summarizeCaptures(
    archived.objects,
    statuses.objects,
    archived.partial || statuses.partial || index.partial,
    index.objects,
  );
}
