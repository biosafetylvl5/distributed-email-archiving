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
  type UrlCaptures,
} from "./captures";
import { liveRetries, parseRetries, type Retries } from "./retry";
import { archiveStats, summarize, type ArchiveStats, type StoredObject } from "./stats";

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
  retries?: unknown;
  storage?: unknown;
  urls: { byKind: Record<string, Record<string, number>>; wayback: number; lastUpdated: string | null };
}

function date(s: string | null | undefined): Date | null {
  const d = s ? new Date(s) : null;
  return d && !Number.isNaN(d.getTime()) ? d : null;
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
export function parseUrls(u: SummaryFile["urls"]): UrlCaptures {
  const byKind = Object.fromEntries(
    URL_KINDS.map((k) => [k, Object.fromEntries(URL_STATUSES.map((s) => [s, Number(u.byKind?.[k]?.[s]) || 0]))]),
  ) as UrlCaptures["byKind"];
  const total = URL_KINDS.reduce((n, k) => n + URL_STATUSES.reduce((m, s) => m + byKind[k][s], 0), 0);
  return { total, byKind, wayback: Number(u.wayback) || 0, lastUpdated: date(u.lastUpdated) };
}

function n(v: unknown): number {
  return typeof v === "number" && Number.isFinite(v) && v >= 0 ? v : 0;
}

function rec(v: unknown): Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v) ? (v as Record<string, unknown>) : {};
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

export function fromSummary(raw: string, now: Date): Snapshot | null {
  let f: SummaryFile;
  try {
    f = JSON.parse(raw) as SummaryFile;
  } catch {
    return null;
  }
  if (f?.version !== VERSION || !Array.isArray(f.incoming) || !Array.isArray(f.statuses)) return null;
  const incoming = objects(f.incoming);
  const captures = summarizeCaptures(incoming, objects(f.statuses));
  captures.urls = parseUrls(f.urls ?? { byKind: {}, wayback: 0, lastUpdated: null });
  return {
    generatedAt: date(f.generated_at),
    stats: summarize(incoming, now),
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
