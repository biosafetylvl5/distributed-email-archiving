// the overview's and /captures' numbers, from dashboard/summary.json: dea-capture
// rebuilds it every few minutes from the same listings (`dea-capture summary`),
// because listing thousands of objects here costs more CPU than the Worker has.
// Without that object (a new install), the bucket is listed live instead.

import type { Env } from "../env";
import {
  captureSummary,
  summarizeCaptures,
  URL_KINDS,
  URL_STATUSES,
  type CaptureSummary,
  type ArchivedMessage,
  type IssueCapture,
  type UrlCaptures,
} from "./captures";
import { liveRetries, parseRetries, type Retries } from "./retry";
import { archiveStats, summarize, type ArchiveStats, type StoredObject, type TagStats } from "./stats";

export const SUMMARY_KEY = "dashboard/summary.json";
const VERSION = 1;

export interface Snapshot {
  generatedAt: Date | null; // null: listed live just now
  stats: ArchiveStats;
  captures: CaptureSummary;
  retries: Retries;
  storage: Storage | null; // null: not counted yet (only dea-capture lists the whole bucket)
}

// what the bucket holds, listed by dea-capture about hourly (summary.py's AREAS)
export const STORAGE_AREAS = [
  ["archive", "Archived mail"],
  ["quarantine", "Quarantined mail"],
  ["embeds", "Embedded files"],
  ["views", "Rendered messages"],
  ["link_records", "Link records and status"],
  ["captures", "Link captures"],
  ["capture_index", "Capture index"],
  ["queue", "Capture queue and retry requests"],
  ["dashboard", "Dashboard summary"],
  ["other", "Other"],
] as const;
export type StorageArea = (typeof STORAGE_AREAS)[number][0];

export interface Sized {
  count: number;
  bytes: number;
}

export interface Storage {
  computedAt: Date | null;
  areas: Record<StorageArea, Sized>;
  captures: Record<string, Sized & { urls: number }>; // by kind
  messages: Map<string, number>; // sha -> bytes under issues/<sha>/
}

interface Stored {
  key: string;
  size?: number;
  uploaded?: string;
  customMetadata?: Record<string, string>;
}

interface SummaryFile {
  version: number;
  generated_at: string;
  incoming: Stored[];
  statuses: Stored[];
  aggregates?: unknown;
  retries?: unknown;
  storage?: unknown;
  urls: { byKind: Record<string, Record<string, number>>; wayback: number; lastUpdated: string | null };
}

function date(s: unknown): Date | null {
  const d = typeof s === "string" && s ? new Date(s) : null;
  return d && !Number.isNaN(d.getTime()) ? d : null;
}

function validStored(v: unknown): v is Stored {
  const o = rec(v);
  if (typeof o.key !== "string") return false;
  if (o.size !== undefined && (typeof o.size !== "number" || !Number.isFinite(o.size) || o.size < 0)) return false;
  if (o.uploaded !== undefined && typeof o.uploaded !== "string") return false;
  return o.customMetadata === undefined || (
    isRecord(o.customMetadata) && Object.values(o.customMetadata).every((value) => typeof value === "string")
  );
}

function objects(list: Stored[]): StoredObject[] {
  return list.map((o) => ({
    key: o.key,
    size: o.size ?? 0,
    uploaded: date(o.uploaded) ?? new Date(0),
    customMetadata: o.customMetadata ?? {},
  }));
}

// only known kinds and statuses, like summarizeIndex
export function parseUrls(value: unknown): UrlCaptures {
  const u = rec(value);
  const kinds = rec(u.byKind);
  const byKind = Object.fromEntries(
    URL_KINDS.map((k) => [k, Object.fromEntries(URL_STATUSES.map((s) => [s, n(rec(kinds[k])[s])]))]),
  ) as UrlCaptures["byKind"];
  const total = URL_KINDS.reduce((n, k) => n + URL_STATUSES.reduce((m, s) => m + byKind[k][s], 0), 0);
  return { total, byKind, wayback: n(u.wayback), lastUpdated: date(u.lastUpdated) };
}

function n(v: unknown): number {
  return typeof v === "number" && Number.isFinite(v) && v >= 0 ? v : 0;
}

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

function rec(v: unknown): Record<string, unknown> {
  return isRecord(v) ? v : {};
}

function numericFields(v: unknown, fields?: string[]): boolean {
  return isRecord(v) && (fields ?? Object.keys(v)).every((key) =>
    typeof v[key] === "number" && Number.isFinite(v[key]) && v[key] >= 0);
}

function validAggregates(stats: Record<string, unknown>, captures: Record<string, unknown>): boolean {
  const disp = ["archive", "quarantine"];
  const states = ["done", "partial", "pending"];
  const totals = rec(stats.totals);
  return disp.every((d) => numericFields(totals[d], ["count", "bytes"]))
    && numericFields(stats.last7, disp) && numericFields(stats.last30, disp)
    && Array.isArray(stats.days) && stats.days.every((d) => typeof rec(d).day === "string" && numericFields(d, disp))
    && Array.isArray(stats.senders) && stats.senders.every((d) => typeof rec(d).domain === "string" && numericFields(d, ["count"]))
    && Object.values(rec(stats.tags)).every((t) => typeof rec(t).user === "string"
      && typeof rec(t).tag === "string" && numericFields(t, [...disp, "bytes"]))
    && numericFields(captures.states, states) && numericFields(captures.linkStates, states)
    && numericFields(captures.totals)
    && Object.values(rec(captures.bySubscription)).every((s) => numericFields(s, states))
    && Array.isArray(captures.messages) && captures.messages.every((value) => {
      const m = rec(value);
      const c = rec(m.capture);
      return [m.key, m.day, m.user, m.tag].every((s) => typeof s === "string")
        && (m.capture === null || (typeof c.sha === "string" && typeof c.source === "string"
          && typeof c.state === "string" && ["done", "partial"].includes(c.state)
          && typeof c.linksState === "string" && states.includes(c.linksState)
          && numericFields(c.counts)));
    });
}

export function parseStorage(v: unknown): Storage | null {
  const s = rec(v);
  if (!s.computed_at) return null;
  const areas = rec(s.areas);
  const sized = (x: unknown): Sized => ({ count: n(rec(x).count), bytes: n(rec(x).bytes) });
  return {
    computedAt: date(typeof s.computed_at === "string" ? s.computed_at : null),
    areas: Object.fromEntries(STORAGE_AREAS.map(([a]) => [a, sized(areas[a])])) as Record<StorageArea, Sized>,
    captures: Object.fromEntries(
      Object.entries(rec(s.captures)).map(([k, x]) => [k, { ...sized(x), urls: n(rec(x).urls) }]),
    ),
    messages: new Map(
      Object.entries(rec(s.messages))
        .filter(([sha]) => /^[0-9a-f]{64}$/.test(sha))
        .map(([sha, b]) => [sha, n(b)]),
    ),
  };
}

// Counts and ordering are computed by the processor; only dates and maps need rehydrating.
function parseAggregates(v: unknown): { stats: ArchiveStats; captures: CaptureSummary } | null {
  const a = rec(v);
  const stats = rec(a.stats);
  const captures = rec(a.captures);
  if (!validAggregates(stats, captures) || !Array.isArray(captures.messages)) return null;
  const parsedStats = {
    ...stats,
    oldestQuarantine: typeof stats.oldestQuarantine === "string" ? stats.oldestQuarantine : null,
    tags: new Map(Object.entries(rec(stats.tags)).map(([id, value]) => {
      const t = value as Omit<TagStats, "last"> & { last: string | null };
      return [id, { ...t, last: date(t.last) }];
    })),
  } as unknown as ArchiveStats;
  const parsedCaptures = {
    ...captures,
    lastCompleted: date(captures.lastCompleted as string | null),
    bySubscription: new Map(Object.entries(rec(captures.bySubscription))),
    messages: captures.messages.map((value) => {
      const m = value as ArchivedMessage;
      const c = m.capture as (Omit<IssueCapture, "completedAt"> & { completedAt: string | null }) | null;
      return { ...m, capture: c ? { ...c, completedAt: date(c.completedAt) } : null };
    }),
  } as unknown as CaptureSummary;
  return { stats: parsedStats, captures: parsedCaptures };
}

export function fromSummary(raw: string, now: Date): Snapshot | null {
  let f: SummaryFile;
  try {
    f = JSON.parse(raw) as SummaryFile;
  } catch {
    return null;
  }
  if (f?.version !== VERSION || !Array.isArray(f.incoming) || !Array.isArray(f.statuses)
      || !f.incoming.every(validStored) || !f.statuses.every(validStored)) return null;
  const precomputed = parseAggregates(f.aggregates);
  const incoming = precomputed ? [] : objects(f.incoming);
  const captures = precomputed?.captures ?? summarizeCaptures(incoming, objects(f.statuses));
  captures.urls = parseUrls(f.urls ?? { byKind: {}, wayback: 0, lastUpdated: null });
  return {
    generatedAt: date(f.generated_at),
    stats: precomputed?.stats ?? summarize(incoming, now),
    captures,
    retries: parseRetries(f.retries),
    storage: parseStorage(f.storage),
  };
}

export async function snapshot(env: Env, now = new Date()): Promise<Snapshot> {
  const obj = await env.ARCHIVE.get(SUMMARY_KEY);
  const got = obj ? fromSummary(await obj.text(), now) : null;
  if (got) return got;
  const [stats, captures, retries] = await Promise.all([archiveStats(env, now), captureSummary(env), liveRetries(env)]);
  return { generatedAt: null, stats, captures, retries, storage: null };
}
