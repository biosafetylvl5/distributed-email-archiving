// the subscription API, for the browser extension: on its own hostname
// (API_HOSTNAME), outside Cloudflare Access, authenticated by per-user tokens
// (see tokens.ts). JSON in and out; no cookies, and no CORS headers, so web
// pages can't call it from a browser (the extension's background script can).
//
// Users see and edit only their own subscriptions, and only the fields in
// EDITABLE; status, sender domain, capture limit, publish and the admin's note
// stay with the admin.

import { addressesFor, archiveDomain } from "../addresses";
import { getTag, listTags, putTag } from "../admin/store";
import type { Env } from "../env";
import { isValidTag, type TagRecord, type UserRecord } from "../policy";
import { loadOverrides, resolveSettings, type Settings } from "../settings";
import { cleanDescription, cleanName, cleanNote, isSignupUrl, siteKey, suggestTag } from "../tagfields";
import { tokenUser } from "./tokens";

const HEADERS = {
  "content-type": "application/json; charset=utf-8",
  "cache-control": "no-store",
  "x-content-type-options": "nosniff",
  "content-security-policy": "default-src 'none'; frame-ancestors 'none'",
  "referrer-policy": "no-referrer",
};

const BODY_MAX = 16 * 1024;
export const EDITS_PER_DAY = 200; // per user; edits cost a KV write each
const DERIVED_TRIES = 50; // ramen, ramen-2 … ramen-50

const EDITABLE = ["name", "description", "signup_url", "user_note"] as const;
type Editable = (typeof EDITABLE)[number];
const ADMIN_ONLY = new Set([
  "status", "sender_domain", "capture_limit_mb", "publish", "note",
  "source", "created_at", "created_by", "updated_at",
]); // prettier-ignore
const CLEAN: Record<Editable, (v: string) => string> = {
  name: cleanName,
  description: cleanDescription,
  signup_url: (v) => v.trim(),
  user_note: cleanNote,
};

export function json(status: number, body: unknown, extra: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), { status, headers: { ...HEADERS, ...extra } });
}

function error(
  status: number, code: string, message: string,
  more: Record<string, unknown> = {}, headers: Record<string, string> = {},
): Response {
  return json(status, { error: code, message, ...more }, headers);
} // prettier-ignore

interface Caller {
  user: string;
  tokenId: string;
}

interface Ctx {
  request: Request;
  env: Env;
  caller: Caller;
  url: URL;
}

async function authenticate(request: Request, env: Env): Promise<Caller | null> {
  const m = /^Bearer (\S+)$/.exec(request.headers.get("authorization") ?? "");
  const found = m ? await tokenUser(env, m[1]) : null;
  if (!found) return null;
  const user = await env.USERS.get<UserRecord>(found.user, "json");
  return user?.active ? { user: found.user, tokenId: found.id } : null;
}

// what a user sees of their own subscription: not the admin's note or flags
export function publicTag(env: Env, user: string, tag: string, r: TagRecord) {
  return {
    tag,
    status: r.status,
    name: r.name ?? null,
    description: r.description ?? null,
    signup_url: r.signup_url ?? null,
    user_note: r.user_note ?? null,
    sender_domain: r.sender_domain ?? null,
    capture_limit_mb: r.capture_limit_mb ?? null,
    source: r.source ?? "admin",
    created_at: r.created_at ?? null,
    updated_at: r.updated_at ?? null,
    addresses: addressesFor(env, user, tag),
  };
}

async function settings(env: Env): Promise<Settings> {
  return resolveSettings(env, await loadOverrides(env.SETTINGS)).settings;
}

// --- per-user daily counters, in RATELIMIT under "api:" (the dashboard's
// usage table lists only "rl:" keys). Read before acting and bumped after, so
// refused requests don't count; KV is eventually consistent, so a burst can
// overshoot slightly, as with the mail caps.

function counterKey(kind: string, user: string, now: Date): string {
  return `api:${kind}:${user}:${now.toISOString().slice(0, 10)}`;
}

async function counted(env: Env, kind: string, user: string, now: Date): Promise<number> {
  return parseInt((await env.RATELIMIT.get(counterKey(kind, user, now))) ?? "0", 10) || 0;
}

async function bump(env: Env, kind: string, user: string, now: Date): Promise<void> {
  const n = await counted(env, kind, user, now);
  await env.RATELIMIT.put(counterKey(kind, user, now), String(n + 1), { expirationTtl: 2 * 24 * 3600 });
}

function untilMidnight(now: Date): string {
  const next = Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate() + 1);
  return String(Math.ceil((next - now.getTime()) / 1000));
}

// --- request bodies

type Body = Record<string, unknown>;

async function readBody(request: Request): Promise<Body | Response> {
  if (!/^application\/json\b/i.test(request.headers.get("content-type") ?? "")) {
    return error(415, "unsupported_media_type", "send application/json");
  }
  const text = await request.text();
  if (text.length > BODY_MAX) return error(413, "too_large", `bodies are limited to ${BODY_MAX} bytes`);
  let body: unknown;
  try {
    body = JSON.parse(text);
  } catch {
    return error(400, "bad_json", "the body isn't valid JSON");
  }
  if (!body || typeof body !== "object" || Array.isArray(body)) {
    return error(400, "bad_json", "the body must be a JSON object");
  }
  return body as Body;
}

// the editable fields present in body, cleaned ("" clears one), or an error
function editableFields(body: Body, extra: readonly string[]): Partial<Record<Editable, string>> | Response {
  for (const key of Object.keys(body)) {
    if (ADMIN_ONLY.has(key)) return error(403, "admin_only", `only an admin can set ${key}`, { field: key });
    if (!(EDITABLE as readonly string[]).includes(key) && !extra.includes(key)) {
      return error(400, "unknown_field", `unknown field ${key}`, { field: key });
    }
  }
  const out: Partial<Record<Editable, string>> = {};
  for (const key of EDITABLE) {
    if (!(key in body)) continue;
    const v = body[key];
    if (v !== null && typeof v !== "string") {
      return error(422, "invalid", `${key} must be a string or null`, { field: key });
    }
    out[key] = CLEAN[key](v ?? "");
  }
  if (out.signup_url && !isSignupUrl(out.signup_url)) {
    return error(422, "invalid", "signup_url must be an http or https URL", { field: "signup_url" });
  }
  return out;
}

function apply(record: TagRecord, fields: Partial<Record<Editable, string>>): boolean {
  let changed = false;
  for (const [key, v] of Object.entries(fields) as [Editable, string][]) {
    if ((record[key] ?? "") === v) continue;
    if (v) record[key] = v;
    else delete record[key];
    changed = true;
  }
  return changed;
}

// --- endpoints

async function me(ctx: Ctx): Promise<Response> {
  const { env, caller } = ctx;
  const now = new Date();
  const [s, tags, today] = await Promise.all([
    settings(env),
    listTags(env, caller.user),
    counted(env, "new", caller.user, now),
  ]);
  return json(200, {
    user: caller.user,
    archive_domain: archiveDomain(env),
    token_id: caller.tokenId,
    limits: {
      subscriptions: tags.length,
      subscriptions_max: s.api_tags_max,
      added_today: today,
      added_per_day: s.api_tags_per_day,
      new_status: s.api_new_tag_status,
    },
  });
}

async function list(ctx: Ctx): Promise<Response> {
  const { env, caller } = ctx;
  const tags = await listTags(env, caller.user);
  return json(200, {
    subscriptions: tags
      .filter((t) => t.record && t.user === caller.user)
      .map((t) => publicTag(env, t.user, t.tag, t.record as TagRecord)),
  });
}

// most of other users' matches a lookup returns
const OTHERS_MAX = 10;

// the user's subscriptions whose sign-up link is this page, or on this site;
// and, as `others`, other users' (so a newsletter isn't archived twice), with
// only the name, status and how it matched: never whose they are
async function lookup(ctx: Ctx): Promise<Response> {
  const { env, caller, url } = ctx;
  const key = siteKey(url.searchParams.get("url") ?? "");
  if (!key) return error(422, "invalid", "url must be an http or https URL", { field: "url" });
  type Match = "page" | "site";
  const exact: ReturnType<typeof publicTag>[] = [];
  const sameSite: ReturnType<typeof publicTag>[] = [];
  const others = new Map<string, { name: string; status: string; match: Match }>();
  for (const t of await listTags(env)) {
    if (!t.record?.signup_url) continue;
    const k = siteKey(t.record.signup_url);
    const match: Match | null = k?.page === key.page ? "page" : k?.host === key.host ? "site" : null;
    if (!match) continue;
    if (t.user === caller.user) {
      (match === "page" ? exact : sameSite).push(publicTag(env, t.user, t.tag, t.record));
    } else {
      const other = { name: t.record.name || t.tag, status: t.record.status, match };
      others.set(JSON.stringify(other), other);
    }
  }
  const rank = (o: { match: string; status: string }) => (o.match === "page" ? 0 : 2) + (o.status === "approved" ? 0 : 1);
  const othersList = [...others.values()].sort((a, b) => rank(a) - rank(b) || a.name.localeCompare(b.name));
  return json(200, { exact, same_site: sameSite, others: othersList.slice(0, OTHERS_MAX) });
}

async function create(ctx: Ctx): Promise<Response> {
  const { env, caller, request } = ctx;
  const body = await readBody(request);
  if (body instanceof Response) return body;
  const fields = editableFields(body, ["tag"]);
  if (fields instanceof Response) return fields;

  const now = new Date();
  const [s, tags, today] = await Promise.all([
    settings(env),
    listTags(env, caller.user),
    counted(env, "new", caller.user, now),
  ]);
  if (tags.length >= s.api_tags_max) {
    return error(429, "too_many_subscriptions", `you have the most subscriptions allowed (${s.api_tags_max})`);
  }
  if (today >= s.api_tags_per_day) {
    return error(429, "daily_limit", `you can add ${s.api_tags_per_day} subscriptions a day; try again tomorrow (UTC)`, {}, {
      "retry-after": untilMidnight(now),
    });
  }

  let tag: string;
  if (body.tag !== undefined && body.tag !== null && body.tag !== "") {
    tag = typeof body.tag === "string" ? body.tag.trim().toLowerCase() : "";
    if (!isValidTag(tag)) {
      return error(422, "invalid", "tag must be 1-63 of a-z, 0-9, '.', '_' or '-', starting with a letter or digit", { field: "tag" });
    } // prettier-ignore
    const existing = await getTag(env, caller.user, tag);
    if (existing) {
      return error(409, "exists", `you already have ${tag}`, { subscription: publicTag(env, caller.user, tag, existing) });
    }
  } else {
    const base = suggestTag(fields.name, fields.signup_url);
    if (!base || !isValidTag(base)) {
      return error(422, "invalid", "give a tag, or a name or sign-up link to make one from", { field: "tag" });
    }
    const taken = new Set(tags.map((t) => t.tag));
    const free = [base, ...Array.from({ length: DERIVED_TRIES - 1 }, (_, i) => `${base}-${i + 2}`)].find(
      (t) => !taken.has(t) && isValidTag(t),
    );
    // the list can lag a recent write: confirm the one we picked
    if (!free || (await getTag(env, caller.user, free))) {
      return error(409, "exists", `couldn't find a free tag like ${base}; give one`, { field: "tag" });
    }
    tag = free;
  }

  const record: TagRecord = {
    status: s.api_new_tag_status,
    source: "api",
    created_at: now.toISOString(),
    created_by: `token:${caller.tokenId}`,
    updated_at: now.toISOString(),
  };
  apply(record, fields);
  await putTag(env, caller.user, tag, record);
  await bump(env, "new", caller.user, now);
  console.log(JSON.stringify({ evt: "api_tag_created", user: caller.user, tag, token: caller.tokenId, status: record.status }));
  return json(201, { subscription: publicTag(env, caller.user, tag, record) });
}

async function show(ctx: Ctx, tag: string): Promise<Response> {
  const record = await getTag(ctx.env, ctx.caller.user, tag);
  if (!record) return error(404, "not_found", `no subscription ${tag}`);
  return json(200, { subscription: publicTag(ctx.env, ctx.caller.user, tag, record) });
}

async function update(ctx: Ctx, tag: string): Promise<Response> {
  const { env, caller, request } = ctx;
  const body = await readBody(request);
  if (body instanceof Response) return body;
  const fields = editableFields(body, ["if_updated_at"]);
  if (fields instanceof Response) return fields;
  const record = await getTag(env, caller.user, tag);
  if (!record) return error(404, "not_found", `no subscription ${tag}`);

  // the caller says which version it edited: refuse if it changed since
  if ("if_updated_at" in body && (body.if_updated_at ?? null) !== (record.updated_at ?? null)) {
    return error(409, "conflict", "it changed since you loaded it; reload and try again", {
      subscription: publicTag(env, caller.user, tag, record),
    });
  }
  const now = new Date();
  if (!apply(record, fields)) return json(200, { subscription: publicTag(env, caller.user, tag, record) });
  if ((await counted(env, "edit", caller.user, now)) >= EDITS_PER_DAY) {
    return error(429, "daily_limit", `you can make ${EDITS_PER_DAY} edits a day; try again tomorrow (UTC)`, {}, {
      "retry-after": untilMidnight(now),
    });
  }
  record.updated_at = now.toISOString();
  await putTag(env, caller.user, tag, record);
  await bump(env, "edit", caller.user, now);
  return json(200, { subscription: publicTag(env, caller.user, tag, record) });
}

function methods(method: string, allowed: Record<string, () => Promise<Response>>): Promise<Response> | Response {
  const run = allowed[method === "HEAD" ? "GET" : method];
  if (run) return run();
  const allow = Object.keys(allowed).join(", ");
  return error(405, "method_not_allowed", `use ${allow}`, {}, { allow });
}

async function route(ctx: Ctx, path: string): Promise<Response> {
  const method = ctx.request.method;
  if (path === "/v1/me") return methods(method, { GET: () => me(ctx) });
  if (path === "/v1/subscriptions") return methods(method, { GET: () => list(ctx), POST: () => create(ctx) });
  if (path === "/v1/subscriptions/lookup") return methods(method, { GET: () => lookup(ctx) });
  const m = /^\/v1\/subscriptions\/([^/]+)$/.exec(path);
  if (m) {
    const tag = decodeURIComponent(m[1]).toLowerCase();
    if (!isValidTag(tag)) return error(404, "not_found", `no subscription ${tag}`);
    return methods(method, { GET: () => show(ctx, tag), PATCH: () => update(ctx, tag) });
  }
  return error(404, "not_found", "no such endpoint");
}

export async function handleApi(request: Request, env: Env): Promise<Response> {
  const url = new URL(request.url);
  const path = url.pathname.replace(/\/+$/, "") || "/";
  const caller = await authenticate(request, env);
  if (!caller) {
    console.log(JSON.stringify({ evt: "api_denied", path }));
    return error(401, "unauthorized", "a valid API token is required", {}, { "www-authenticate": 'Bearer realm="api"' });
  }
  try {
    const res = await route({ request, env, caller, url }, path);
    console.log(JSON.stringify({ evt: "api", user: caller.user, token: caller.tokenId, method: request.method, path, status: res.status }));
    return res;
  } catch (err) {
    console.log(JSON.stringify({ evt: "api_error", path, error: String(err) }));
    return error(500, "internal", "internal error");
  }
}
