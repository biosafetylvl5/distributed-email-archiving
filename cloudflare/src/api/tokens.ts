// per-user API tokens, in the TOKENS KV namespace:
//   t:<sha256 of the token>  -> {user, id, last_used_at?}   (looked up per request)
//   u:<user>/<id>            -> {hash, label, hint, created_at, created_by}
// The token itself is never stored: it's shown once, when it's created. It's
// 256 random bits, so a plain SHA-256 of it is as good as a slow hash.

import type { Env } from "../env";

export const TOKEN_PREFIX = "dea_";
const TOKEN_RE = /^dea_[A-Za-z0-9_-]{43}$/;
const ID_RE = /^[0-9a-f]{8}$/;
export const LABEL_MAX = 100;

interface TokenLookup {
  user: string;
  id: string;
  last_used_at?: string;
}

interface TokenInfo {
  hash: string;
  label: string;
  hint: string; // the token's first characters, to tell them apart
  created_at: string;
  created_by: string;
}

export interface TokenEntry extends Omit<TokenInfo, "hash"> {
  id: string;
  lastUsedAt: string | null;
}

function base64url(bytes: Uint8Array): string {
  return btoa(String.fromCharCode(...bytes)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

async function sha256Hex(s: string): Promise<string> {
  const d = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(s));
  return [...new Uint8Array(d)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

function tokensKv(env: Env): KVNamespace {
  if (!env.TOKENS) throw new Error("no TOKENS KV binding");
  return env.TOKENS;
}

export async function createToken(
  env: Env, user: string, label: string, by: string,
): Promise<{ token: string; id: string }> {
  const kv = tokensKv(env);
  const token = TOKEN_PREFIX + base64url(crypto.getRandomValues(new Uint8Array(32)));
  const id = [...crypto.getRandomValues(new Uint8Array(4))].map((b) => b.toString(16).padStart(2, "0")).join("");
  const hash = await sha256Hex(token);
  const info: TokenInfo = {
    hash,
    label: label.slice(0, LABEL_MAX),
    hint: token.slice(0, TOKEN_PREFIX.length + 4),
    created_at: new Date().toISOString(),
    created_by: by,
  };
  await kv.put(`t:${hash}`, JSON.stringify({ user, id } satisfies TokenLookup));
  await kv.put(`u:${user}/${id}`, JSON.stringify(info));
  return { token, id };
}

export async function listTokens(env: Env, user: string): Promise<TokenEntry[]> {
  const kv = tokensKv(env);
  const names: string[] = [];
  let cursor: string | undefined;
  do {
    const page = await kv.list({ prefix: `u:${user}/`, cursor });
    names.push(...page.keys.map((k) => k.name));
    cursor = page.list_complete ? undefined : page.cursor;
  } while (cursor);
  const entries = await Promise.all(
    names.map(async (name) => {
      const info = await kv.get<TokenInfo>(name, "json");
      if (!info) return null;
      const lookup = await kv.get<TokenLookup>(`t:${info.hash}`, "json");
      const { hash: _, ...rest } = info;
      return { ...rest, id: name.slice(name.lastIndexOf("/") + 1), lastUsedAt: lookup?.last_used_at ?? null };
    }),
  );
  return entries.filter((e): e is TokenEntry => e !== null).sort((a, b) => a.created_at.localeCompare(b.created_at));
}

export async function revokeToken(env: Env, user: string, id: string): Promise<boolean> {
  if (!ID_RE.test(id)) return false;
  const kv = tokensKv(env);
  const info = await kv.get<TokenInfo>(`u:${user}/${id}`, "json");
  if (!info) return false;
  await kv.delete(`t:${info.hash}`);
  await kv.delete(`u:${user}/${id}`);
  return true;
}

// the token's user and id, or null; marks it used (at most once a day, to
// stay well inside KV's write limits)
export async function tokenUser(env: Env, token: string, now = new Date()): Promise<TokenLookup | null> {
  if (!env.TOKENS || !TOKEN_RE.test(token)) return null;
  const key = `t:${await sha256Hex(token)}`;
  const found = await env.TOKENS.get<TokenLookup>(key, "json");
  if (!found) return null;
  const today = now.toISOString().slice(0, 10);
  if (found.last_used_at?.slice(0, 10) !== today) {
    await env.TOKENS.put(key, JSON.stringify({ ...found, last_used_at: now.toISOString() }));
  }
  return found;
}
