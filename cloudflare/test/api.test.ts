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
    new Request(API + path, {
      ...init,
      headers: { ...(token ? { authorization: `Bearer ${token}` } : {}), ...(init.headers as object) },
    }) as never,
    env,
  );

const send = (env: Env, method: string, path: string, token: string, body: unknown) =>
  call(env, path, token, {
    method,
    body: typeof body === "string" ? body : JSON.stringify(body),
    headers: { "content-type": "application/json" },
  });

interface Sub {
  tag: string;
  status: string;
  name: string | null;
  description: string | null;
  signup_url: string | null;
  user_note: string | null;
  source: string;
  updated_at: string | null;
  addresses: { plus: string };
}
const sub = async (r: Response) => ((await r.json()) as { subscription: Sub }).subscription;

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
    expect(await r.json()).toEqual({
      user: "ann", archive_domain: "mail.example.org", token_id: id,
      limits: { subscriptions: 1, subscriptions_max: 200, added_today: 0, added_per_day: 10, new_status: "approved" },
    }); // prettier-ignore
  });

  it("lists only the user's own subscriptions, without admin-only fields", async () => {
    const { env, token } = await seeded();
    const body = (await (await call(env, "/v1/subscriptions", token)).json()) as { subscriptions: unknown[] };
    expect(body.subscriptions).toEqual([
      {
        tag: "ramen", status: "approved", name: "Ramen Weekly", description: null,
        signup_url: "https://ramen.example.com/join", user_note: null, sender_domain: "mail.ramen.example.com",
        capture_limit_mb: 50, source: "admin", created_at: null, updated_at: null,
        addresses: { plus: "ann+ramen@mail.example.org", underscore: "ann__ramen@mail.example.org" },
      },
    ]); // prettier-ignore
    expect(JSON.stringify(body)).not.toMatch(/admin only|publish|Bob/);
  });

  it("405s other methods and 404s anything else", async () => {
    const { env, token } = await seeded();
    const r = await call(env, "/v1/subscriptions", token, { method: "DELETE" });
    expect(r.status).toBe(405);
    expect(r.headers.get("allow")).toBe("GET, POST");
    expect((await call(env, "/v1/me", token, { method: "POST" })).status).toBe(405);
    expect((await call(env, "/v1/nope", token)).status).toBe(404);
    expect((await call(env, "/v1/subscriptions/nope", token)).status).toBe(404);
    expect((await call(env, "/v1/subscriptions/ramen", token)).status).toBe(200);
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

describe("adding subscriptions", () => {
  it("adds one, approved, with a tag made from its name", async () => {
    const { env, tags, ratelimit, token, id } = await seeded();
    const r = await send(env, "POST", "/v1/subscriptions", token, {
      name: "  Café  Weekly ", signup_url: "https://www.cafe.example.com/signup?ref=x", description: "Coffee\r\nnews",
      user_note: "from the footer",
    }); // prettier-ignore
    expect(r.status).toBe(201);
    const s = await sub(r);
    expect(s).toMatchObject({
      tag: "cafe-weekly", status: "approved", name: "Café Weekly", description: "Coffee\nnews",
      signup_url: "https://www.cafe.example.com/signup?ref=x", user_note: "from the footer", source: "api",
    }); // prettier-ignore
    expect(s.addresses.plus).toBe("ann+cafe-weekly@mail.example.org");
    expect(tags.json("ann/cafe-weekly")).toMatchObject({ source: "api", created_by: `token:${id}` });
    expect([...ratelimit.data.keys()].some((k) => k.startsWith("api:new:ann:"))).toBe(true);
  });

  it("makes a tag from the sign-up site, and numbers clashes", async () => {
    const { env, token } = await seeded();
    const add = async () => sub(await send(env, "POST", "/v1/subscriptions", token, { signup_url: "https://news.ramen.example.co.uk/x" }));
    expect((await add()).tag).toBe("ramen-2"); // "ramen" is taken
    expect((await add()).tag).toBe("ramen-3");
  });

  it("uses a tag it's given, and never overwrites one", async () => {
    const { env, tags, token } = await seeded();
    const r = await send(env, "POST", "/v1/subscriptions", token, { tag: "Ramen", name: "Mine now" });
    expect(r.status).toBe(409);
    expect(((await r.json()) as { subscription: Sub }).subscription.name).toBe("Ramen Weekly");
    expect(tags.json("ann/ramen")).toMatchObject({ name: "Ramen Weekly", note: "admin only" });
    expect((await send(env, "POST", "/v1/subscriptions", token, { tag: "-bad" })).status).toBe(422);
    expect((await sub(await send(env, "POST", "/v1/subscriptions", token, { tag: "Pho" }))).tag).toBe("pho");
  });

  it("refuses admin-only fields, unknown fields and bad values", async () => {
    const { env, tags, token } = await seeded();
    const before = tags.data.size;
    const cases: [unknown, number][] = [
      [{ name: "x", status: "approved" }, 403],
      [{ name: "x", sender_domain: "example.com" }, 403],
      [{ name: "x", capture_limit_mb: 5 }, 403],
      [{ name: "x", note: "hi" }, 403],
      [{ name: "x", publish: true }, 403],
      [{ name: "x", colour: "red" }, 400],
      [{ name: 5 }, 422],
      [{ name: "x", signup_url: "javascript:alert(1)" }, 422],
      [{ description: "no name or link" }, 422],
      [[1], 400],
      ["{nope", 400],
    ];
    for (const [body, status] of cases) {
      expect([JSON.stringify(body), (await send(env, "POST", "/v1/subscriptions", token, body)).status]).toEqual([
        JSON.stringify(body),
        status,
      ]);
    }
    const plain = await call(env, "/v1/subscriptions", token, { method: "POST", body: "{}" });
    expect(plain.status).toBe(415);
    const big = await send(env, "POST", "/v1/subscriptions", token, { name: "x".repeat(20000) });
    expect(big.status).toBe(413);
    expect(tags.data.size).toBe(before);
  });

  it("holds new ones as pending when the setting says so", async () => {
    const { env, settings, token } = await seeded();
    await settings.put("settings", JSON.stringify({ api_new_tag_status: "pending" }));
    expect((await sub(await send(env, "POST", "/v1/subscriptions", token, { name: "Later" }))).status).toBe("pending");
  });

  it("caps additions per day and in total", async () => {
    const { env, settings, token } = await seeded();
    await settings.put("settings", JSON.stringify({ api_tags_per_day: 2 }));
    for (const name of ["a", "b"]) expect((await send(env, "POST", "/v1/subscriptions", token, { name })).status).toBe(201);
    // a refused request doesn't count against the day
    expect((await send(env, "POST", "/v1/subscriptions", token, { name: "x", status: "approved" })).status).toBe(403);
    const r = await send(env, "POST", "/v1/subscriptions", token, { name: "c" });
    expect(r.status).toBe(429);
    expect(Number(r.headers.get("retry-after"))).toBeGreaterThan(0);

    await settings.put("settings", JSON.stringify({ api_tags_max: 3 }));
    expect((await send(env, "POST", "/v1/subscriptions", token, { name: "d" })).status).toBe(429); // ramen, a, b
  });
});

describe("editing subscriptions", () => {
  it("changes only the fields sent; an empty value or null clears one", async () => {
    const { env, tags, token } = await seeded();
    const r = await send(env, "PATCH", "/v1/subscriptions/ramen", token, {
      description: "Noodles", user_note: "mine", signup_url: null,
    }); // prettier-ignore
    expect(r.status).toBe(200);
    const s = await sub(r);
    expect(s).toMatchObject({ name: "Ramen Weekly", description: "Noodles", user_note: "mine", signup_url: null });
    expect(s.updated_at).not.toBeNull();
    // what the user can't see or set is kept
    expect(tags.json("ann/ramen")).toMatchObject({ note: "admin only", publish: true, capture_limit_mb: 50, status: "approved" });
  });

  it("refuses admin-only fields and other users' tags", async () => {
    const { env, tags, token } = await seeded();
    expect((await send(env, "PATCH", "/v1/subscriptions/ramen", token, { status: "blocked" })).status).toBe(403);
    expect((await send(env, "PATCH", "/v1/subscriptions/other", token, { name: "Ann's now" })).status).toBe(404);
    expect(tags.json("bob/other")).toEqual({ status: "approved", name: "Bob's" });
    expect(tags.json("ann/ramen")).toMatchObject({ status: "approved" });
  });

  it("refuses an edit to a version that's since changed", async () => {
    const { env, token } = await seeded();
    // never saved by the dashboard since updated_at was kept: null matches
    const first = await sub(await send(env, "PATCH", "/v1/subscriptions/ramen", token, { if_updated_at: null, name: "A" }));
    const stale = await send(env, "PATCH", "/v1/subscriptions/ramen", token, { if_updated_at: null, name: "B" });
    expect(stale.status).toBe(409);
    expect(((await stale.json()) as { subscription: Sub }).subscription.name).toBe("A");
    const ok = await send(env, "PATCH", "/v1/subscriptions/ramen", token, { if_updated_at: first.updated_at, name: "B" });
    expect(ok.status).toBe(200);
  });

  it("doesn't write or count an edit that changes nothing", async () => {
    const { env, ratelimit, token } = await seeded();
    const r = await send(env, "PATCH", "/v1/subscriptions/ramen", token, { name: "Ramen Weekly" });
    expect((await sub(r)).updated_at).toBeNull();
    expect(ratelimit.data.size).toBe(0);
  });
});

describe("lookup", () => {
  it("finds the user's subscriptions for a page, then for its site", async () => {
    const { env, tags, token } = await seeded();
    await tags.put("ann/ramen-extra", JSON.stringify({ status: "approved", signup_url: "https://ramen.example.com/extra" }));
    await tags.put("bob/ramen", JSON.stringify({ status: "approved", signup_url: "https://ramen.example.com/join" }));
    const r = await call(env, "/v1/subscriptions/lookup?url=" + encodeURIComponent("https://www.ramen.example.com/join/?utm=1#f"), token);
    const body = (await r.json()) as { exact: Sub[]; same_site: Sub[] };
    expect(body.exact.map((s) => s.tag)).toEqual(["ramen"]);
    expect(body.same_site.map((s) => s.tag)).toEqual(["ramen-extra"]);
    expect((await call(env, "/v1/subscriptions/lookup?url=ftp://x", token)).status).toBe(422);
  });

  it("says what other users archive from the page or site, without saying who", async () => {
    const { env, tags, token } = await seeded();
    await tags.put("bob/ramen", JSON.stringify({ status: "approved", name: "Ramen Weekly", signup_url: "https://ramen.example.com/join", note: "secret", user_note: "mine" }));
    await tags.put("cy/ramen-weekly", JSON.stringify({ status: "approved", name: "Ramen Weekly", signup_url: "https://www.ramen.example.com/join/" }));
    await tags.put("cy/broth", JSON.stringify({ status: "blocked", signup_url: "https://ramen.example.com/broth" }));
    await tags.put("cy/elsewhere", JSON.stringify({ status: "approved", name: "Pho", signup_url: "https://pho.example.com/join" }));
    const r = await call(env, "/v1/subscriptions/lookup?url=" + encodeURIComponent("https://ramen.example.com/join"), token);
    const body = (await r.json()) as { others: unknown[] };
    expect(body.others).toEqual([
      { name: "Ramen Weekly", status: "approved", match: "page" },
      { name: "broth", status: "blocked", match: "site" },
    ]);
    expect(JSON.stringify(body)).not.toMatch(/bob|cy|secret|mine/);
  });
});
