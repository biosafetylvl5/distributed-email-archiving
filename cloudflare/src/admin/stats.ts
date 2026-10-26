// archive statistics for the overview page, from R2 listings only (keys,
// sizes and the customMetadata ingest writes); message bodies are never read

import type { Env } from "../env";
import { envelopeFromDomain } from "../policy";

// a list page is up to 1000 objects; the cap keeps the overview inside the
// Worker's subrequest and CPU limits on a large archive
const MAX_PAGES = 25;
const KEY_RE = /^incoming\/(archive|quarantine)\/(\d{4}-\d{2}-\d{2})\//;

export type Disposition = "archive" | "quarantine";

export interface StoredObject {
  key: string;
  size: number;
  uploaded: Date;
  customMetadata?: Record<string, string>;
}

export interface Totals {
  count: number;
  bytes: number;
}

export interface TagStats {
  user: string;
  tag: string; // "" = bare address
  archive: number;
  quarantine: number;
  bytes: number;
  last: Date | null;
}

export interface ArchiveStats {
  partial: boolean; // hit MAX_PAGES; numbers are a lower bound
  totals: Record<Disposition, Totals>;
  last7: Record<Disposition, number>;
  last30: Record<Disposition, number>;
  days: { day: string; archive: number; quarantine: number }[]; // oldest first
  tags: Map<string, TagStats>; // keyed "user/tag"
  senders: { domain: string; count: number }[]; // last 30 days, most first
  oldestQuarantine: string | null;
}

export const CHART_DAYS = 14;

function dayOffset(now: Date, days: number): string {
  return new Date(now.getTime() - days * 86_400_000).toISOString().slice(0, 10);
}

export function summarize(objects: StoredObject[], now: Date, partial = false): ArchiveStats {
  const zero = () => ({ archive: 0, quarantine: 0 });
  const totals = { archive: { count: 0, bytes: 0 }, quarantine: { count: 0, bytes: 0 } };
  const last7 = zero();
  const last30 = zero();
  const since7 = dayOffset(now, 6);
  const since30 = dayOffset(now, 29);
  const days = new Map<string, Record<Disposition, number>>();
  for (let i = CHART_DAYS - 1; i >= 0; i--) days.set(dayOffset(now, i), zero());
  const tags = new Map<string, TagStats>();
  const senders = new Map<string, number>();
  let oldestQuarantine: string | null = null;

  for (const obj of objects) {
    const m = KEY_RE.exec(obj.key);
    if (!m) continue;
    const disp = m[1] as Disposition;
    const day = m[2];
    totals[disp].count++;
    totals[disp].bytes += obj.size;
    if (day >= since7) last7[disp]++;
    if (day >= since30) last30[disp]++;
    const d = days.get(day);
    if (d) d[disp]++;
    if (disp === "quarantine" && (!oldestQuarantine || day < oldestQuarantine)) oldestQuarantine = day;

    const meta = obj.customMetadata ?? {};
    if (meta.user) {
      const id = `${meta.user}/${meta.tag ?? ""}`;
      let t = tags.get(id);
      if (!t) {
        t = { user: meta.user, tag: meta.tag ?? "", archive: 0, quarantine: 0, bytes: 0, last: null };
        tags.set(id, t);
      }
      t[disp]++;
      t.bytes += obj.size;
      if (!t.last || obj.uploaded > t.last) t.last = obj.uploaded;
    }
    const domain = meta.envelope_from ? envelopeFromDomain(meta.envelope_from) : null;
    if (domain && day >= since30) senders.set(domain, (senders.get(domain) ?? 0) + 1);
  }

  return {
    partial,
    totals,
    last7,
    last30,
    days: [...days].map(([day, c]) => ({ day, ...c })),
    tags,
    senders: [...senders]
      .map(([domain, count]) => ({ domain, count }))
      .sort((a, b) => b.count - a.count || a.domain.localeCompare(b.domain)),
    oldestQuarantine,
  };
}

export async function archiveStats(env: Env, now = new Date()): Promise<ArchiveStats> {
  const objects: StoredObject[] = [];
  let cursor: string | undefined;
  let pages = 0;
  do {
    const page = await env.ARCHIVE.list({ prefix: "incoming/", cursor, include: ["customMetadata"] });
    objects.push(...page.objects);
    cursor = page.truncated ? page.cursor : undefined;
  } while (cursor && ++pages < MAX_PAGES);
  return summarize(objects, now, Boolean(cursor));
}
