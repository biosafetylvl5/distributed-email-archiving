import { describe, expect, it } from "vitest";
import { handleAdmin, type Authenticator } from "../src/admin/app";
import { createToken, listTokens, revokeToken, tokenUser } from "../src/api/tokens";
import type { Env } from "../src/env";
import handler from "../src/index";
import { fakeEnv } from "./fakes";

const API = "https://mail-api.example.org";
const ADMIN = "https://admin.example.org";
const allow: Authenticator = async () => ({ ok: true, email: "admin@example.org" });

const call = (env: Env, path: string, token?: string, init: RequestInit = {}) =>
  handler.fetch!(
    new Request(API + path, { ...init, headers: token ? { authorization: `Bearer ${token}` } : {} }) as never,
    env,
  );

async function seeded() {
  const f = fakeEnv();
  await f.users.put("ann", JSON.stringify({ active: true }));
  await f.users.put("bob", JSON.stringify({ active: true }));
  await f.tags.put("ann/ramen", JSON.stringify({
    status: "approved", name: "Ramen Weekly", signup_url: "https://ramen.example.com/join",
    note: "admin only", publish: true, sender_domain: "mail.ramen.example.com", capture_limit_mb: 50,
  })); // prettier-ignore
  await f.tags.put("bob/other", JSON.stringify({ status: "approved", name: "Bob's" }));
  const { token, id } = await createToken(f.env, "ann", "Firefox", "admin@example.org");
  return { ...f, token, id };
}

describe("tokens", () => {
  it("stores only a hash, and resolves, lists and revokes", async () => {
    const { env, tokens, token, id } = await seeded();
    expect(token).toMatch(/^dea_[A-Za-z0-9_-]{43}$/);
    expect([...tokens.data.values()].join("")).not.toContain(token.slice(4));
    expect(await tokenUser(env, token)).toMatchObject({ user: "ann", id });
    const listed = await listTokens(env, "ann");
    expect(listed).toHaveLength(1);
    expect(listed[0]).toMatchObject({ id, label: "Firefox", hint: token.slice(0, 8), created_by: "admin@example.org" });
    expect(listed[0].lastUsedAt).not.toBeNull();
    expect(listed[0]).not.toHaveProperty("hash");
    expect(await listTokens(env, "bob")).toEqual([]);
    expect(await revokeToken(env, "bob", id)).toBe(false); // someone else's
    expect(await revokeToken(env, "ann", id)).toBe(true);
    expect(await tokenUser(env, token)).toBeNull();
    expect(tokens.data.size).toBe(0);
  });

  it("marks a token used at most once a day", async () => {
    const { env, tokens, token } = await seeded();
    let writes = 0;
    const put = tokens.put.bind(tokens);
    tokens.put = async (k: string, v: string) => {
      writes++;
      return put(k, v);
    };
    const day = new Date("2026-10-29T10:00:00Z");
    await tokenUser(env, token, day);
    await tokenUser(env, token, new Date("2026-10-29T23:00:00Z"));
    expect(writes).toBe(1);
    await tokenUser(env, token, new Date("2026-10-30T01:00:00Z"));
    expect(writes).toBe(2);
  });
});

describe("API", () => {
  it("needs a valid token of an active user", async () => {
    const { env, users, token } = await seeded();
    for (const t of [undefined, "nope", token.slice(0, -1) + (token.endsWith("A") ? "B" : "A")]) {
      const r = await call(env, "/v1/me", t);
      expect(r.status).toBe(401);
      expect(r.headers.get("www-authenticate")).toMatch(/^Bearer/);
    }
    expect((await call(env, "/v1/me", token)).status).toBe(200);
    await users.put("ann", JSON.stringify({ active: false }));
    expect((await call(env, "/v1/me", token)).status).toBe(401);
  });

  it("tells a user who they are", async () => {
    const { env, token, id } = await seeded();
    const r = await call(env, "/v1/me", token);
    expect(r.headers.get("content-type")).toMatch(/^application\/json/);
    expect(r.headers.get("access-control-allow-origin")).toBeNull();
    expect(await r.json()).toEqual({ user: "ann", archive_domain: "mail.example.org", token_id: id });
  });

  it("lists only the user's own subscriptions, without admin-only fields", async () => {
    const { env, token } = await seeded();
    const body = (await (await call(env, "/v1/subscriptions", token)).json()) as { subscriptions: unknown[] };
    expect(body.subscriptions).toEqual([
      {
        tag: "ramen", status: "approved", name: "Ramen Weekly", description: null,
        signup_url: "https://ramen.example.com/join", sender_domain: "mail.ramen.example.com",
        capture_limit_mb: 50,
        addresses: { plus: "ann+ramen@mail.example.org", underscore: "ann__ramen@mail.example.org" },
      },
    ]); // prettier-ignore
    expect(JSON.stringify(body)).not.toMatch(/admin only|publish|Bob/);
  });

  it("is read-only for now, and 404s anything else", async () => {
    const { env, token } = await seeded();
    expect((await call(env, "/v1/subscriptions", token, { method: "POST" })).status).toBe(405);
    expect((await call(env, "/v1/nope", token)).status).toBe(404);
  });
});

describe("hostnames", () => {
  it("never serves the dashboard on the API's hostname", async () => {
    const { env } = await seeded();
    // no token: the API's 401, not the dashboard (whose Access check would 403)
    const r = await call(env, "/users");
    expect(r.status).toBe(401);
    expect(r.headers.get("content-type")).toMatch(/^application\/json/);
  });

  it("never serves the API on the dashboard's", async () => {
    const { env, token } = await seeded();
    const r = await handler.fetch!(
      new Request(ADMIN + "/v1/me", { headers: { authorization: `Bearer ${token}` } }) as never,
      env,
    );
    expect(r.headers.get("content-type") ?? "").not.toMatch(/json/);
  });

  it("has no API without API_HOSTNAME", async () => {
    const { env, token } = await seeded();
    const r = await call({ ...env, API_HOSTNAME: undefined } as Env, "/v1/me", token);
    expect(r.headers.get("content-type") ?? "").not.toMatch(/json/);
  });
});

describe("dashboard tokens", () => {
  const post = (env: Env, path: string, form: Record<string, string>) =>
    handleAdmin(
      new Request(ADMIN + path, { method: "POST", body: new URLSearchParams(form), headers: { origin: ADMIN } }),
      env,
      allow,
    );

  it("creates a token, shows it once, and revokes it", async () => {
    const { env, users } = fakeEnv();
    await users.put("ann", JSON.stringify({ active: true }));
    const r = await post(env, "/users/ann/tokens", { label: "Chrome <b>" });
    expect(r.status).toBe(200);
    const page = await r.text();
    const token = /dea_[A-Za-z0-9_-]{43}/.exec(page)?.[0];
    expect(token).toBeDefined();
    expect(page).toContain("Chrome &lt;b&gt;");
    expect((await tokenUser(env, token!))?.user).toBe("ann");

    const again = await (await handleAdmin(new Request(ADMIN + "/users/ann"), env, allow)).text();
    expect(again).not.toContain(token!);
    expect(again).toContain(token!.slice(0, 8));

    const [{ id }] = await listTokens(env, "ann");
    const rv = await post(env, "/users/ann/tokens/revoke", { id });
    expect(rv.status).toBe(303);
    expect(await tokenUser(env, token!)).toBeNull();
  });

  it("refuses an unlabelled token, an unknown user, and cross-origin posts", async () => {
    const { env, users, tokens } = fakeEnv();
    await users.put("ann", JSON.stringify({ active: true }));
    expect((await post(env, "/users/ann/tokens", { label: " " })).headers.get("location")).toMatch(/err=/);
    expect((await post(env, "/users/zed/tokens", { label: "x" })).headers.get("location")).toMatch(/err=/);
    const cross = await handleAdmin(
      new Request(ADMIN + "/users/ann/tokens", {
        method: "POST", body: new URLSearchParams({ label: "x" }), headers: { origin: "https://evil.example.net" },
      }),
      env,
      allow,
    ); // prettier-ignore
    expect(cross.status).toBe(403);
    expect(tokens.data.size).toBe(0);
  });
});
