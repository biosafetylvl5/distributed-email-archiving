// the subscription API, for the browser extension: on its own hostname
// (API_HOSTNAME), outside Cloudflare Access, authenticated by per-user tokens
// (see tokens.ts). JSON in and out; no cookies, and no CORS headers, so web
// pages can't call it from a browser (the extension's background script can).

import { addressesFor, archiveDomain } from "../addresses";
import type { Env } from "../env";
import type { TagRecord, UserRecord } from "../policy";
import { listTags } from "../admin/store";
import { tokenUser } from "./tokens";

const HEADERS = {
  "content-type": "application/json; charset=utf-8",
  "cache-control": "no-store",
  "x-content-type-options": "nosniff",
  "content-security-policy": "default-src 'none'; frame-ancestors 'none'",
  "referrer-policy": "no-referrer",
};

export function json(status: number, body: unknown, extra: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), { status, headers: { ...HEADERS, ...extra } });
}

function error(status: number, code: string, message: string, extra: Record<string, string> = {}): Response {
  return json(status, { error: code, message }, extra);
}

interface Caller {
  user: string;
  tokenId: string;
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
    sender_domain: r.sender_domain ?? null,
    capture_limit_mb: r.capture_limit_mb ?? null,
    addresses: addressesFor(env, user, tag),
  };
}

async function route(request: Request, env: Env, caller: Caller, path: string): Promise<Response> {
  if (request.method !== "GET") return error(405, "method_not_allowed", "GET only", { allow: "GET" });
  if (path === "/v1/me") {
    return json(200, { user: caller.user, archive_domain: archiveDomain(env), token_id: caller.tokenId });
  }
  if (path === "/v1/subscriptions") {
    const tags = await listTags(env, caller.user);
    return json(200, {
      subscriptions: tags
        .filter((t) => t.record && t.user === caller.user)
        .map((t) => publicTag(env, t.user, t.tag, t.record as TagRecord)),
    });
  }
  return error(404, "not_found", "no such endpoint");
}

export async function handleApi(request: Request, env: Env): Promise<Response> {
  const path = new URL(request.url).pathname.replace(/\/+$/, "") || "/";
  const caller = await authenticate(request, env);
  if (!caller) {
    console.log(JSON.stringify({ evt: "api_denied", path }));
    return error(401, "unauthorized", "a valid API token is required", { "www-authenticate": 'Bearer realm="api"' });
  }
  try {
    const res = await route(request, env, caller, path);
    console.log(JSON.stringify({ evt: "api", user: caller.user, token: caller.tokenId, method: request.method, path, status: res.status }));
    return res;
  } catch (err) {
    console.log(JSON.stringify({ evt: "api_error", path, error: String(err) }));
    return error(500, "internal", "internal error");
  }
}
