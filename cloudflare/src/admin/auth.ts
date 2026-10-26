// Cloudflare Access authentication for the admin dashboard
// Access sits in front of the admin hostname and adds a signed JWT to every
// request; we verify it here as well so a missing or misconfigured Access
// application can't expose the dashboard. Fails closed when unconfigured.
// https://developers.cloudflare.com/cloudflare-one/identity/authorization-cookie/validating-json/

import type { Env } from "../env";

export type AuthResult = { ok: true; email: string } | { ok: false; reason: string };

export interface AccessKey extends JsonWebKey {
  kid?: string;
}

export type KeyFetcher = (teamDomain: string, refresh: boolean) => Promise<AccessKey[]>;

const CLOCK_SKEW_S = 60;
const KEY_CACHE_MS = 10 * 60 * 1000;
let keyCache: { teamDomain: string; keys: AccessKey[]; fetchedAt: number } | null = null;

export const fetchAccessKeys: KeyFetcher = async (teamDomain, refresh) => {
  if (
    !refresh &&
    keyCache &&
    keyCache.teamDomain === teamDomain &&
    Date.now() - keyCache.fetchedAt < KEY_CACHE_MS
  ) {
    return keyCache.keys;
  }
  const res = await fetch(`https://${teamDomain}/cdn-cgi/access/certs`);
  if (!res.ok) throw new Error(`access certs: HTTP ${res.status}`);
  const body = (await res.json()) as { keys?: AccessKey[] };
  const keys = body.keys ?? [];
  keyCache = { teamDomain, keys, fetchedAt: Date.now() };
  return keys;
};

export function normalizeTeamDomain(v: string | undefined): string {
  return (v ?? "").trim().toLowerCase().replace(/^https?:\/\//, "").replace(/\/+$/, "");
}

function b64urlBytes(s: string): Uint8Array {
  const b64 = s.replace(/-/g, "+").replace(/_/g, "/");
  const bin = atob(b64 + "=".repeat((4 - (b64.length % 4)) % 4));
  return Uint8Array.from(bin, (c) => c.charCodeAt(0));
}

function b64urlJson(s: string): Record<string, unknown> {
  return JSON.parse(new TextDecoder().decode(b64urlBytes(s)));
}

export async function verifyAccessJwt(
  token: string,
  cfg: { teamDomain: string; aud: string; allowedEmails: string[] },
  fetchKeys: KeyFetcher,
  nowS: number = Date.now() / 1000,
): Promise<AuthResult> {
  const parts = token.split(".");
  if (parts.length !== 3) return { ok: false, reason: "malformed token" };
  let header: Record<string, unknown>;
  let payload: Record<string, unknown>;
  try {
    header = b64urlJson(parts[0]);
    payload = b64urlJson(parts[1]);
  } catch {
    return { ok: false, reason: "malformed token" };
  }
  if (header.alg !== "RS256") return { ok: false, reason: "unexpected alg" };

  let keys = await fetchKeys(cfg.teamDomain, false);
  let jwk = keys.find((k) => k.kid === header.kid);
  if (!jwk) {
    // Access rotates keys; refetch once before giving up
    keys = await fetchKeys(cfg.teamDomain, true);
    jwk = keys.find((k) => k.kid === header.kid);
  }
  if (!jwk) return { ok: false, reason: "unknown signing key" };

  const key = await crypto.subtle.importKey(
    "jwk",
    { kty: jwk.kty, n: jwk.n, e: jwk.e, alg: "RS256", ext: true },
    { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" },
    false,
    ["verify"],
  );
  const valid = await crypto.subtle.verify(
    "RSASSA-PKCS1-v1_5",
    key,
    b64urlBytes(parts[2]),
    new TextEncoder().encode(`${parts[0]}.${parts[1]}`),
  );
  if (!valid) return { ok: false, reason: "bad signature" };

  const auds = Array.isArray(payload.aud) ? payload.aud : [payload.aud];
  if (!auds.includes(cfg.aud)) return { ok: false, reason: "wrong audience" };
  if (payload.iss !== `https://${cfg.teamDomain}`) return { ok: false, reason: "wrong issuer" };
  if (typeof payload.exp !== "number" || payload.exp < nowS - CLOCK_SKEW_S) {
    return { ok: false, reason: "expired" };
  }
  if (typeof payload.nbf === "number" && payload.nbf > nowS + CLOCK_SKEW_S) {
    return { ok: false, reason: "not yet valid" };
  }

  const email = typeof payload.email === "string" ? payload.email.toLowerCase() : "";
  if (!email) return { ok: false, reason: "no email in token" };
  if (cfg.allowedEmails.length > 0 && !cfg.allowedEmails.includes(email)) {
    return { ok: false, reason: "email not allowed" };
  }
  return { ok: true, email };
}

function cookie(request: Request, name: string): string | null {
  for (const part of (request.headers.get("cookie") ?? "").split(";")) {
    const [k, ...v] = part.trim().split("=");
    if (k === name) return v.join("=");
  }
  return null;
}

export async function authenticate(
  request: Request,
  env: Env,
  fetchKeys: KeyFetcher = fetchAccessKeys,
): Promise<AuthResult> {
  const url = new URL(request.url);
  if (
    env.ALLOW_LOCAL_NO_AUTH === "true" &&
    (url.hostname === "localhost" || url.hostname === "127.0.0.1")
  ) {
    return { ok: true, email: "local-dev" };
  }

  const teamDomain = normalizeTeamDomain(env.ACCESS_TEAM_DOMAIN);
  const aud = (env.ACCESS_AUD ?? "").trim();
  if (!teamDomain || !aud) return { ok: false, reason: "Access is not configured" };

  const token =
    request.headers.get("cf-access-jwt-assertion") ?? cookie(request, "CF_Authorization");
  if (!token) return { ok: false, reason: "no Access token" };

  const allowedEmails = (env.ADMIN_EMAILS ?? "")
    .split(",")
    .map((e) => e.trim().toLowerCase())
    .filter(Boolean);
  try {
    return await verifyAccessJwt(token, { teamDomain, aud, allowedEmails }, fetchKeys);
  } catch (err) {
    return { ok: false, reason: `verification error: ${String(err)}` };
  }
}
