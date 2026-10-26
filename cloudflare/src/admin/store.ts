// KV / R2 access for the admin dashboard
// record shapes match what the ingest Worker and *ctl.sh scripts read and write;
// edits are read-modify-write so unknown fields survive

import type { Env } from "../env";
import type { TagRecord, UserRecord } from "../policy";
import { summarizeHeaders, type HeaderSummary } from "./mail";

export interface UserEntry {
  name: string;
  record: UserRecord | null;
}

export interface TagEntry {
  user: string;
  tag: string;
  record: TagRecord | null;
}

export async function listAllKeys(kv: KVNamespace, prefix?: string): Promise<string[]> {
  const names: string[] = [];
  let cursor: string | undefined;
  do {
    const page = await kv.list({ prefix, cursor });
    names.push(...page.keys.map((k) => k.name));
    cursor = page.list_complete ? undefined : page.cursor;
  } while (cursor);
  return names;
}

async function getJson<T>(kv: KVNamespace, key: string): Promise<T | null> {
  try {
    return await kv.get<T>(key, "json");
  } catch {
    return null; // unparseable value: show it as broken rather than failing the page
  }
}

export async function listUsers(env: Env): Promise<UserEntry[]> {
  const names = (await listAllKeys(env.USERS)).sort();
  return Promise.all(
    names.map(async (name) => ({ name, record: await getJson<UserRecord>(env.USERS, name) })),
  );
}

export async function getUser(env: Env, name: string): Promise<UserRecord | null> {
  return getJson<UserRecord>(env.USERS, name);
}

export async function setUserActive(env: Env, name: string, active: boolean): Promise<void> {
  const current = (await getUser(env, name)) ?? {};
  await env.USERS.put(name, JSON.stringify({ ...current, active }));
}

export async function listTags(env: Env, user?: string): Promise<TagEntry[]> {
  const keys = (await listAllKeys(env.TAGS, user ? `${user}/` : undefined)).sort();
  return Promise.all(
    keys.map(async (key) => {
      const slash = key.indexOf("/");
      return {
        user: key.slice(0, slash),
        tag: key.slice(slash + 1),
        record: await getJson<TagRecord>(env.TAGS, key),
      };
    }),
  );
}

export async function getTag(env: Env, user: string, tag: string): Promise<TagRecord | null> {
  return getJson<TagRecord>(env.TAGS, `${user}/${tag}`);
}

export async function putTag(env: Env, user: string, tag: string, record: TagRecord): Promise<void> {
  await env.TAGS.put(`${user}/${tag}`, JSON.stringify(record));
}

// --- rate-limit counters (keys written by underDailyCap in index.ts)

export interface Usage {
  scope: string;
  count: number;
}

export function today(): string {
  return new Date().toISOString().slice(0, 10);
}

export async function usageForDay(env: Env, day: string): Promise<Usage[]> {
  const suffix = `:${day}`;
  const keys = (await listAllKeys(env.RATELIMIT, "rl:")).filter((k) => k.endsWith(suffix));
  const usage = await Promise.all(
    keys.map(async (key) => ({
      scope: key.slice("rl:".length, -suffix.length),
      count: parseInt((await env.RATELIMIT.get(key)) ?? "0", 10) || 0,
    })),
  );
  return usage.sort((a, b) => b.count - a.count || a.scope.localeCompare(b.scope));
}

// --- quarantine (R2 incoming/quarantine/<day>/<sha256>.eml)

export const QUARANTINE_PREFIX = "incoming/quarantine/";
export const QUARANTINE_KEY_RE = /^incoming\/quarantine\/(\d{4}-\d{2}-\d{2})\/([0-9a-f]{64})\.eml$/;
const HEADER_BYTES = 16 * 1024;

export interface QuarantineItem extends HeaderSummary {
  key: string;
  size: number;
  uploaded: Date;
  meta: Record<string, string>;
}

async function listDays(bucket: R2Bucket): Promise<string[]> {
  const days: string[] = [];
  let cursor: string | undefined;
  do {
    const page = await bucket.list({ prefix: QUARANTINE_PREFIX, delimiter: "/", cursor });
    days.push(...page.delimitedPrefixes);
    cursor = page.truncated ? page.cursor : undefined;
  } while (cursor);
  return days.sort().reverse();
}

export async function listQuarantine(env: Env, limit = 50): Promise<QuarantineItem[]> {
  const objects: R2Object[] = [];
  for (const day of await listDays(env.ARCHIVE)) {
    let cursor: string | undefined;
    const dayObjects: R2Object[] = [];
    do {
      const page = await env.ARCHIVE.list({ prefix: day, cursor, include: ["customMetadata"] });
      dayObjects.push(...page.objects);
      cursor = page.truncated ? page.cursor : undefined;
    } while (cursor);
    dayObjects.sort((a, b) => b.uploaded.getTime() - a.uploaded.getTime());
    objects.push(...dayObjects);
    if (objects.length >= limit) break;
  }

  return Promise.all(
    objects.slice(0, limit).map(async (obj) => {
      let summary: HeaderSummary = { from: "", subject: "", date: "" };
      const head = await env.ARCHIVE.get(obj.key, { range: { offset: 0, length: HEADER_BYTES } });
      if (head) summary = summarizeHeaders(new Uint8Array(await head.arrayBuffer()));
      return {
        key: obj.key,
        size: obj.size,
        uploaded: obj.uploaded,
        meta: obj.customMetadata ?? {},
        ...summary,
      };
    }),
  );
}

export async function getQuarantined(env: Env, key: string): Promise<R2ObjectBody | null> {
  if (!QUARANTINE_KEY_RE.test(key)) return null;
  return env.ARCHIVE.get(key);
}

// move a quarantined message to incoming/archive/ (same day + content hash)
export async function promote(env: Env, key: string, by: string): Promise<string | null> {
  const m = QUARANTINE_KEY_RE.exec(key);
  if (!m) return null;
  const obj = await env.ARCHIVE.get(key);
  if (!obj) return null;
  const dest = `incoming/archive/${m[1]}/${m[2]}.eml`;
  await env.ARCHIVE.put(dest, await obj.arrayBuffer(), {
    httpMetadata: obj.httpMetadata,
    customMetadata: {
      ...obj.customMetadata,
      promoted_at: new Date().toISOString(),
      promoted_by: by,
    },
  });
  await env.ARCHIVE.delete(key);
  return dest;
}
