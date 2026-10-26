import { beforeAll, describe, expect, it } from "vitest";
import { authenticate, verifyAccessJwt, type AccessKey, type KeyFetcher } from "../src/admin/auth";
import { fakeEnv } from "./fakes";

const TEAM = "myteam.cloudflareaccess.com";
const AUD = "aud-123";
const NOW = 1_800_000_000;

let privateKey: CryptoKey;
let publicJwk: AccessKey;
let otherPrivateKey: CryptoKey;

const b64url = (bytes: Uint8Array) =>
  btoa(String.fromCharCode(...bytes)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
const b64urlJson = (o: unknown) => b64url(new TextEncoder().encode(JSON.stringify(o)));

async function sign(payload: Record<string, unknown>, key = privateKey, kid = "k1"): Promise<string> {
  const data = `${b64urlJson({ alg: "RS256", kid, typ: "JWT" })}.${b64urlJson(payload)}`;
  const sig = await crypto.subtle.sign("RSASSA-PKCS1-v1_5", key, new TextEncoder().encode(data));
  return `${data}.${b64url(new Uint8Array(sig))}`;
}

const goodClaims = () => ({
  aud: [AUD],
  iss: `https://${TEAM}`,
  email: "Admin@Example.org",
  exp: NOW + 600,
  nbf: NOW - 10,
  iat: NOW - 10,
});

async function genKey(): Promise<CryptoKeyPair> {
  return (await crypto.subtle.generateKey(
    {
      name: "RSASSA-PKCS1-v1_5",
      modulusLength: 2048,
      publicExponent: new Uint8Array([1, 0, 1]),
      hash: "SHA-256",
    },
    true,
    ["sign", "verify"],
  )) as CryptoKeyPair;
}

beforeAll(async () => {
  const pair = await genKey();
  privateKey = pair.privateKey;
  publicJwk = { ...((await crypto.subtle.exportKey("jwk", pair.publicKey)) as JsonWebKey), kid: "k1" };
  otherPrivateKey = (await genKey()).privateKey;
});

const keys: KeyFetcher = async () => [publicJwk];
const cfg = { teamDomain: TEAM, aud: AUD, allowedEmails: [] as string[] };

describe("verifyAccessJwt", () => {
  it("accepts a valid token and lowercases the email", async () => {
    const r = await verifyAccessJwt(await sign(goodClaims()), cfg, keys, NOW);
    expect(r).toEqual({ ok: true, email: "admin@example.org" });
  });

  it("rejects a token signed by another key", async () => {
    const r = await verifyAccessJwt(await sign(goodClaims(), otherPrivateKey), cfg, keys, NOW);
    expect(r).toMatchObject({ ok: false, reason: "bad signature" });
  });

  it("rejects a tampered payload", async () => {
    const [h, , s] = (await sign(goodClaims())).split(".");
    const forged = `${h}.${b64urlJson({ ...goodClaims(), email: "evil@example.org" })}.${s}`;
    expect(await verifyAccessJwt(forged, cfg, keys, NOW)).toMatchObject({ ok: false });
  });

  it("rejects wrong audience, issuer, expiry and alg", async () => {
    const cases: [Record<string, unknown>, string][] = [
      [{ ...goodClaims(), aud: ["other"] }, "wrong audience"],
      [{ ...goodClaims(), iss: "https://evil.cloudflareaccess.com" }, "wrong issuer"],
      [{ ...goodClaims(), exp: NOW - 3600 }, "expired"],
      [{ ...goodClaims(), nbf: NOW + 3600 }, "not yet valid"],
    ];
    for (const [claims, reason] of cases) {
      expect(await verifyAccessJwt(await sign(claims), cfg, keys, NOW)).toEqual({ ok: false, reason });
    }
    const none = `${b64urlJson({ alg: "none", kid: "k1" })}.${b64urlJson(goodClaims())}.`;
    expect(await verifyAccessJwt(none, cfg, keys, NOW)).toEqual({ ok: false, reason: "unexpected alg" });
  });

  it("refetches keys once for an unknown kid", async () => {
    const calls: boolean[] = [];
    const rotating: KeyFetcher = async (_team, refresh) => {
      calls.push(refresh);
      return refresh ? [publicJwk] : [];
    };
    expect(await verifyAccessJwt(await sign(goodClaims()), cfg, rotating, NOW)).toMatchObject({ ok: true });
    expect(calls).toEqual([false, true]);
  });

  it("enforces the optional email allowlist", async () => {
    const token = await sign(goodClaims());
    expect(
      await verifyAccessJwt(token, { ...cfg, allowedEmails: ["someone@example.org"] }, keys, NOW),
    ).toEqual({ ok: false, reason: "email not allowed" });
    expect(
      await verifyAccessJwt(token, { ...cfg, allowedEmails: ["admin@example.org"] }, keys, NOW),
    ).toMatchObject({ ok: true });
  });
});

describe("authenticate", () => {
  const req = (url: string, headers: Record<string, string> = {}) => new Request(url, { headers });

  it("fails closed when Access is not configured", async () => {
    const { env } = fakeEnv();
    expect(await authenticate(req("https://admin.example.org/"), env, keys)).toEqual({
      ok: false,
      reason: "Access is not configured",
    });
  });

  it("requires a token", async () => {
    const { env } = fakeEnv({ ACCESS_TEAM_DOMAIN: TEAM, ACCESS_AUD: AUD });
    expect(await authenticate(req("https://admin.example.org/"), env, keys)).toMatchObject({ ok: false });
  });

  it("reads the token from the header or the CF_Authorization cookie", async () => {
    const { env } = fakeEnv({ ACCESS_TEAM_DOMAIN: `https://${TEAM}/`, ACCESS_AUD: AUD });
    const claims = { ...goodClaims(), exp: Date.now() / 1000 + 600, nbf: Date.now() / 1000 - 10 };
    const token = await sign(claims);
    const viaHeader = req("https://admin.example.org/", { "cf-access-jwt-assertion": token });
    const viaCookie = req("https://admin.example.org/", { cookie: `a=b; CF_Authorization=${token}` });
    expect(await authenticate(viaHeader, env, keys)).toMatchObject({ ok: true });
    expect(await authenticate(viaCookie, env, keys)).toMatchObject({ ok: true });
  });

  it("only skips auth for localhost when explicitly allowed", async () => {
    const { env } = fakeEnv({ ALLOW_LOCAL_NO_AUTH: "true" });
    expect(await authenticate(req("http://localhost:8787/"), env, keys)).toMatchObject({ ok: true });
    expect(await authenticate(req("https://admin.example.org/"), env, keys)).toMatchObject({ ok: false });
    const { env: off } = fakeEnv();
    expect(await authenticate(req("http://localhost:8787/"), off, keys)).toMatchObject({ ok: false });
  });
});
