import { describe, expect, it, vi } from "vitest";
import { handleAdmin, type Authenticator } from "../src/admin/app";
import type { Env } from "../src/env";
import { CAPTURE_LIMITS_KEY, listQuarantine, listTags, listUsers, syncCaptureLimits } from "../src/admin/store";
import { handleApi } from "../src/api/app";
import { createToken } from "../src/api/tokens";
import { fakeEnv, type FakeKV } from "./fakes";

// a stored tag, less the time of its last save (checked on its own)
function saved(kv: FakeKV, key: string): unknown {
  const r = kv.json(key) as Record<string, unknown> | null;
  if (!r) return r;
  expect(r.updated_at).toMatch(/^\d{4}-\d\d-\d\dT/);
  const { updated_at: _, ...rest } = r;
  return rest;
}

const ORIGIN = "https://admin.example.org";
const allow: Authenticator = async () => ({ ok: true, email: "admin@example.org" });
const deny: Authenticator = async () => ({ ok: false, reason: "nope" });

const get = (env: Env, path: string, auth = allow) => handleAdmin(new Request(ORIGIN + path), env, auth);

function post(
  env: Env,
  path: string,
  fields: Record<string, string>,
  origin: string | null = ORIGIN,
  extra: Record<string, string> = {},
) {
  const headers: Record<string, string> = { ...extra };
  if (origin) headers.origin = origin;
  return handleAdmin(
    new Request(ORIGIN + path, { method: "POST", body: new URLSearchParams(fields), headers }),
    env,
    allow,
  );
}

const HASH = "a".repeat(64);
const QKEY = `incoming/quarantine/2026-10-25/${HASH}.eml`;
const EML = "From: Spam <x@evil.example>\r\nSubject: <script>alert(1)</script>\r\n\r\nhello";

describe("access control", () => {
  it("refuses unauthenticated requests", async () => {
    const { env } = fakeEnv();
    const res = await get(env, "/", deny);
    expect(res.status).toBe(403);
  });

  it("refuses cross-origin and origin-less POSTs", async () => {
    const { env, users } = fakeEnv();
    expect((await post(env, "/users", { name: "eve" }, "https://evil.example")).status).toBe(403);
    expect((await post(env, "/users", { name: "eve" }, null)).status).toBe(403);
    expect((await post(env, "/users", { name: "eve" }, "null")).status).toBe(403);
    const crossSite = { "sec-fetch-site": "cross-site" };
    expect((await post(env, "/users", { name: "eve" }, "null", crossSite)).status).toBe(403);
    expect(users.data.size).toBe(0);
  });

  it("accepts same-origin POSTs that arrive with Origin: null", async () => {
    const { env, users } = fakeEnv();
    const res = await post(env, "/users", { name: "ann" }, "null", { "sec-fetch-site": "same-origin" });
    expect(res.status).toBe(303);
    expect(users.json("ann")).toEqual({ active: true });
  });

  it("sends security headers", async () => {
    const { env } = fakeEnv();
    const res = await get(env, "/users");
    expect(res.headers.get("content-security-policy")).toContain("default-src 'none'");
    expect(res.headers.get("x-frame-options")).toBe("DENY");
    // "no-referrer" would make browsers send Origin: null on our own form POSTs
    expect(res.headers.get("referrer-policy")).toBe("same-origin");
  });
});

describe("users", () => {
  it("adds a user (lowercased) and refuses invalid or duplicate names", async () => {
    const { env, users } = fakeEnv();
    const res = await post(env, "/users", { name: " John.Smith " });
    expect(res.status).toBe(303);
    expect(res.headers.get("location")).toMatch(/^\/users\/john\.smith\?msg=/);
    expect(users.json("john.smith")).toEqual({ active: true });

    await post(env, "/users", { name: "john_smith" });
    expect(users.data.has("john_smith")).toBe(false);

    await users.put("john.smith", JSON.stringify({ active: false, extra: 1 }));
    const dup = await post(env, "/users", { name: "john.smith" });
    expect(dup.headers.get("location")).toContain("err=");
    expect(users.json("john.smith")).toEqual({ active: false, extra: 1 });
  });

  it("disables and re-enables, keeping unknown fields", async () => {
    const { env, users } = fakeEnv();
    await users.put("ann", JSON.stringify({ active: true, note: "x" }));
    await post(env, "/users/ann/active", { active: "0" });
    expect(users.json("ann")).toEqual({ active: false, note: "x" });
    await post(env, "/users/ann/active", { active: "1" });
    expect(users.json("ann")).toEqual({ active: true, note: "x" });
  });

  it("lists users and escapes stored values", async () => {
    const { env, users, tags } = fakeEnv();
    await users.put("ann", JSON.stringify({ active: true }));
    await tags.put("ann/news", JSON.stringify({ status: "approved", note: '"><script>x</script>' }));
    const list = await (await get(env, "/users")).text();
    expect(list).toContain("ann@mail.example.org");
    const detail = await (await get(env, "/users/ann")).text();
    expect(detail).toContain("ann+news@mail.example.org");
    expect(detail).not.toContain("<script>x");
    expect(detail).toContain("&lt;script&gt;x");
  });

  it("404s unknown users", async () => {
    const { env } = fakeEnv();
    expect((await get(env, "/users/nobody")).status).toBe(404);
  });
});

describe("strict KV reads", () => {
  it("leaves a user unchanged when its mutation read fails", async () => {
    const { env, users } = fakeEnv();
    const record = { active: true, note: "keep" };
    await users.put("ann", JSON.stringify(record));
    const read = users.get.bind(users);
    vi.spyOn(users, "get")
      .mockImplementationOnce(read)
      .mockRejectedValueOnce(new Error("KV unavailable"));
    expect((await post(env, "/users/ann/active", { active: "0" })).status).toBe(500);
    expect(users.json("ann")).toEqual(record);
  });

  it.each(["unavailable", "malformed"])("leaves a tag unchanged on a %s read", async (failure) => {
    const { env, users, tags, archive } = fakeEnv();
    await users.put("ann", JSON.stringify({ active: true }));
    const raw = failure === "malformed" ? "{broken" : JSON.stringify({ status: "approved", capture_limit_mb: 10, note: "keep" });
    await tags.put("ann/news", raw);
    if (failure === "unavailable") vi.spyOn(tags, "get").mockRejectedValueOnce(new Error("KV unavailable"));
    expect((await post(env, "/users/ann/tags", { tag: "news", status: "blocked" })).status).toBe(500);
    expect(tags.data.get("ann/news")).toBe(raw);
    expect(archive.objects.has(CAPTURE_LIMITS_KEY)).toBe(false);
  });

  it("refuses API create when the explicit existence read fails", async () => {
    const { env, users, tags } = fakeEnv();
    await users.put("ann", JSON.stringify({ active: true }));
    const blocked = { status: "blocked", note: "keep" };
    await tags.put("ann/news", JSON.stringify(blocked));
    const token = await createToken(env, "ann", "test", "admin@example.org");
    const read = tags.get.bind(tags);
    vi.spyOn(tags, "get")
      .mockImplementationOnce(read) // the initial list succeeds
      .mockRejectedValueOnce(new Error("KV unavailable"));
    const res = await handleApi(new Request("https://mail-api.example.org/v1/subscriptions", {
      method: "POST",
      headers: { authorization: `Bearer ${token.token}`, "content-type": "application/json" },
      body: JSON.stringify({ tag: "news", name: "News" }),
    }), env);
    expect(res.status).toBe(500);
    expect(tags.json("ann/news")).toEqual(blocked);
  });

  it("never publishes a partial limits map after a failed read", async () => {
    const { env, tags, archive } = fakeEnv();
    await tags.put("ann/news", JSON.stringify({ capture_limit_mb: 10 }));
    await tags.put("ann/other", JSON.stringify({ capture_limit_mb: 20 }));
    await syncCaptureLimits(env);
    const before = await (await archive.get(CAPTURE_LIMITS_KEY))!.text();
    const read = tags.get.bind(tags);
    vi.spyOn(tags, "get").mockImplementation((key, opts) => {
      if (key === "ann/other") throw new Error("KV unavailable");
      return read(key, opts);
    });
    await expect(syncCaptureLimits(env)).rejects.toThrow("KV unavailable");
    expect(await (await archive.get(CAPTURE_LIMITS_KEY))!.text()).toBe(before);
  });

  it("reads all KV pages for users and tags", async () => {
    const { env, users, tags } = fakeEnv();
    users.pageSize = tags.pageSize = 1;
    for (const name of ["ann", "bob"]) {
      await users.put(name, JSON.stringify({ active: true }));
      await tags.put(`${name}/news`, JSON.stringify({ capture_limit_mb: 10 }));
    }
    expect((await listUsers(env)).map((u) => u.name)).toEqual(["ann", "bob"]);
    expect((await listTags(env)).map((t) => t.user)).toEqual(["ann", "bob"]);
  });
});

describe("subscriptions", () => {
  it("creates and edits tags, preserving unknown fields", async () => {
    const { env, users, tags } = fakeEnv();
    await users.put("ann", JSON.stringify({ active: true }));

    await post(env, "/users/ann/tags", { tag: "Ramen_Weekly", status: "approved", sender_domain: "Mail.Ramen.Example" });
    expect(saved(tags, "ann/ramen_weekly")).toEqual({
      status: "approved",
      publish: false,
      sender_domain: "mail.ramen.example",
    });

    await tags.put("ann/ramen_weekly", JSON.stringify({ ...(tags.json("ann/ramen_weekly") as object), custom: 1 }));
    const res = await post(env, "/users/ann/tags", {
      tag: "ramen_weekly",
      status: "blocked",
      publish: "1",
      sender_domain: "",
      note: "spammy",
      back: "subscriptions",
    });
    expect(res.headers.get("location")).toMatch(/^\/subscriptions\?/);
    expect(saved(tags, "ann/ramen_weekly")).toEqual({ status: "blocked", publish: true, note: "spammy", custom: 1 });
  });

  it("shows the user's note and marks subscriptions added through the API", async () => {
    const { env, users, tags } = fakeEnv();
    await users.put("ann", JSON.stringify({ active: true }));
    await tags.put("ann/pho", JSON.stringify({
      status: "approved", user_note: "<from the footer>", source: "api", created_at: "2026-10-30T01:02:03Z",
    })); // prettier-ignore
    const page = await (await handleAdmin(new Request("https://admin.example.org/subscriptions"), env, allow)).text();
    expect(page).toContain("ann's note: &lt;from the footer&gt;");
    expect(page).toContain("added by ann through the API on 2026-10-30");
    // a dashboard save keeps them
    await post(env, "/users/ann/tags", { tag: "pho", status: "approved", name: "Pho" });
    expect(tags.json("ann/pho")).toMatchObject({ user_note: "<from the footer>", source: "api", name: "Pho" });
  });

  it("filters to recent API additions, pending or blocked, and keeps the filter across saves", async () => {
    const { env, users, tags } = fakeEnv();
    await users.put("ann", JSON.stringify({ active: true }));
    const day = (n: number) => new Date(Date.now() - n * 86_400_000).toISOString();
    await tags.put("ann/old-api", JSON.stringify({ status: "approved", name: "Old Api", source: "api", created_at: day(8) }));
    await tags.put("ann/new-api", JSON.stringify({ status: "approved", name: "New Api", source: "api", created_at: day(1) }));
    await tags.put("ann/newer-api", JSON.stringify({ status: "pending", name: "Newer Api", source: "api", created_at: day(0) }));
    await tags.put("ann/by-admin", JSON.stringify({ status: "blocked", name: "By Admin" }));
    const page = async (q: string) => (await get(env, `/subscriptions${q}`)).text();

    const api = await page("?show=api");
    expect(api).toContain("<strong>Added through the API, last 7 days</strong>");
    expect(api).not.toContain("Old Api");
    expect(api).not.toContain("By Admin");
    expect(api.indexOf("Newer Api")).toBeLessThan(api.indexOf("New Api"));
    expect(api).toContain('name="show" value="api"');

    expect(await page("?show=pending")).not.toContain("New Api<");
    expect(await page("?show=blocked")).toContain("By Admin");
    const all = await page("?show=nonsense");
    for (const n of ["Old Api", "New Api", "Newer Api", "By Admin"]) expect(all).toContain(n);

    const res = await post(env, "/users/ann/tags", { tag: "new-api", status: "blocked", back: "subscriptions", show: "api" });
    expect(res.headers.get("location")).toMatch(/^\/subscriptions\?show=api&msg=/);
    const res2 = await post(env, "/users/ann/tags", { tag: "new-api", status: "blocked", back: "subscriptions", show: "x" });
    expect(res2.headers.get("location")).toMatch(/^\/subscriptions\?msg=/);

    expect(await (await get(env, "/")).text()).toContain('<a href="/subscriptions?show=api">2 added through the API in the last 7 days</a>');
  });

  it("sets a capture size limit and mirrors every limit to R2 for dea-capture", async () => {
    const { env, users, tags, archive } = fakeEnv();
    await users.put("ann", JSON.stringify({ active: true }));
    await tags.put("ann/other", JSON.stringify({ status: "approved", capture_limit_mb: 50 }));
    await post(env, "/users/ann/tags", { tag: "ramen", status: "approved", capture_limit_mb: "200" });
    expect(saved(tags, "ann/ramen")).toMatchObject({ capture_limit_mb: 200 });
    const mirror = async () => JSON.parse(await (await archive.get("config/capture-limits.json"))!.text());
    expect((await mirror()).limits).toEqual({ "ann/other": 50 * 2 ** 20, "ann/ramen": 200 * 2 ** 20 });
    expect(await (await get(env, "/users/ann")).text()).toContain("captures up to 200 MiB");

    // the short form leaves it alone; blank clears it
    await post(env, "/users/ann/tags", { tag: "ramen", status: "blocked" });
    expect(saved(tags, "ann/ramen")).toMatchObject({ capture_limit_mb: 200 });
    await post(env, "/users/ann/tags", { tag: "ramen", status: "approved", capture_limit_mb: "" });
    expect(saved(tags, "ann/ramen")).not.toHaveProperty("capture_limit_mb");
    expect((await mirror()).limits).toEqual({ "ann/other": 50 * 2 ** 20 });

    for (const bad of ["0", "1.5", "-3", "lots", String(2 * 1024 * 1024)]) {
      const res = await post(env, "/users/ann/tags", { tag: "ramen", status: "approved", capture_limit_mb: bad });
      expect(new URLSearchParams((res.headers.get("location") ?? "").split("?")[1]).get("err"), bad).toContain("Invalid capture size limit");
    }
    expect(saved(tags, "ann/ramen")).not.toHaveProperty("capture_limit_mb");
  });

  it("repairs a failed mirror write on an identical tag save", async () => {
    const { env, users, tags, archive } = fakeEnv();
    await users.put("ann", JSON.stringify({ active: true }));
    await tags.put("ann/news", JSON.stringify({ status: "approved", capture_limit_mb: 10 }));
    await syncCaptureLimits(env);
    vi.spyOn(archive, "put").mockRejectedValueOnce(new Error("R2 unavailable"));
    const form = { tag: "news", status: "approved", capture_limit_mb: "20" };
    expect((await post(env, "/users/ann/tags", form)).status).toBe(500);
    expect(tags.json("ann/news")).toMatchObject({ capture_limit_mb: 20 });
    expect(JSON.parse(await (await archive.get(CAPTURE_LIMITS_KEY))!.text()).limits).toEqual({ "ann/news": 10 * 2 ** 20 });
    expect((await post(env, "/users/ann/tags", form)).status).toBe(303);
    expect(JSON.parse(await (await archive.get(CAPTURE_LIMITS_KEY))!.text()).limits).toEqual({ "ann/news": 20 * 2 ** 20 });
  });

  it("offers an authenticated same-origin action to re-sync out-of-band limits", async () => {
    const { env, tags, archive } = fakeEnv();
    await tags.put("ann/news", JSON.stringify({ capture_limit_mb: 10 }));
    await syncCaptureLimits(env);
    await tags.put("ann/news", JSON.stringify({ status: "blocked" }));
    const path = "/settings/capture-limits/sync";
    expect(await (await get(env, "/settings")).text()).toContain(`action="${path}"`);
    expect((await get(env, path)).status).toBe(404);
    expect((await post(env, path, {}, "https://other.example.org")).status).toBe(403);
    expect((await post(env, path, {}, null)).status).toBe(403);
    const denied = await handleAdmin(new Request(ORIGIN + path, { method: "POST", headers: { origin: ORIGIN } }), env, deny);
    expect(denied.status).toBe(403);
    expect(JSON.parse(await (await archive.get(CAPTURE_LIMITS_KEY))!.text()).limits).toEqual({ "ann/news": 10 * 2 ** 20 });
    const res = await post(env, path, {});
    expect(res.status).toBe(303);
    expect(res.headers.get("location")).toContain("/settings?msg=");
    expect(JSON.parse(await (await archive.get(CAPTURE_LIMITS_KEY))!.text()).limits).toEqual({});
  });

  it("validates tag, status, sender domain and user", async () => {
    const { env, users, tags } = fakeEnv();
    await users.put("ann", JSON.stringify({ active: true }));
    await post(env, "/users/ann/tags", { tag: "_bad", status: "approved" });
    await post(env, "/users/ann/tags", { tag: "ok", status: "yolo" });
    await post(env, "/users/ann/tags", { tag: "ok", status: "approved", sender_domain: "not a domain" });
    await post(env, "/users/ann/tags", { tag: "ok", status: "approved", signup_url: "javascript:alert(1)" });
    await post(env, "/users/ann/tags", { tag: "ok", status: "approved", signup_url: "not a url" });
    await post(env, "/users/bob/tags", { tag: "ok", status: "approved" });
    expect(tags.data.size).toBe(0);
  });

  it("stores, shows and clears a sign-up link", async () => {
    const { env, users, tags } = fakeEnv();
    await users.put("ann", JSON.stringify({ active: true }));
    const url = "https://user:SECRET@ramen.example.org/subscribe?token=SECRET#SECRET";
    await post(env, "/users/ann/tags", { tag: "ramen", status: "approved", signup_url: url });
    expect(saved(tags, "ann/ramen")).toEqual({ status: "approved", publish: false, signup_url: "https://ramen.example.org/subscribe" });

    const body = await (await get(env, "/subscriptions")).text();
    expect(body).toContain('href="https://ramen.example.org/subscribe"');
    expect(body).not.toContain("SECRET");

    await post(env, "/users/ann/tags", { tag: "ramen", status: "approved", signup_url: "" });
    expect(saved(tags, "ann/ramen")).toEqual({ status: "approved", publish: false });
  });

  it("stores and shows name, description and note", async () => {
    const { env, users, tags } = fakeEnv();
    await users.put("ann", JSON.stringify({ active: true }));
    await post(env, "/users/ann/tags", {
      tag: "ramen",
      status: "approved",
      name: "  Ramen \n Weekly  ",
      description: "Noodles.\r\n<b>Every</b> week.",
      note: "signed up at the fair",
    });
    expect(saved(tags, "ann/ramen")).toEqual({
      status: "approved",
      publish: false,
      name: "Ramen Weekly",
      description: "Noodles.\n<b>Every</b> week.",
      note: "signed up at the fair",
    });
    const body = await (await get(env, "/subscriptions")).text();
    expect(body).toContain("<strong>Ramen Weekly</strong>");
    expect(body).toContain('<div class="desc">Noodles.\n&lt;b&gt;Every&lt;/b&gt; week.</div>');
    expect(body).toContain("Note: signed up at the fair");
  });

  it("caps text field lengths", async () => {
    const { env, users, tags } = fakeEnv();
    await users.put("ann", JSON.stringify({ active: true }));
    await post(env, "/users/ann/tags", { tag: "t", status: "approved", name: "n".repeat(500), description: "d".repeat(5000) });
    const rec = tags.json("ann/t") as { name: string; description: string };
    expect(rec.name.length).toBe(200);
    expect(rec.description.length).toBe(2000);
  });

  it("leaves fields the form does not include untouched", async () => {
    const { env, users, tags } = fakeEnv();
    await users.put("ann", JSON.stringify({ active: true }));
    const full = { status: "approved", name: "R", description: "D", note: "N", signup_url: "https://r.example/" };
    await tags.put("ann/ramen", JSON.stringify(full));
    // the user page's short "new tag" form has no description, note or link
    await post(env, "/users/ann/tags", { tag: "ramen", status: "approved", name: "Ramen", sender_domain: "" });
    expect(saved(tags, "ann/ramen")).toEqual({ ...full, name: "Ramen", publish: false });
  });

  it("normalizes legacy sign-up URLs in links and edit forms", async () => {
    const { env, users, tags } = fakeEnv();
    await users.put("ann", JSON.stringify({ active: true }));
    await tags.put("ann/x", JSON.stringify({ status: "approved", signup_url: "https://user:SECRET@example.org/signup?token=SECRET#SECRET" }));
    for (const path of ["/subscriptions", "/users/ann"]) {
      const body = await (await get(env, path)).text();
      expect(body).toContain('href="https://example.org/signup"');
      expect(body).toContain('name="signup_url" value="https://example.org/signup"');
      expect(body).not.toContain("SECRET");
    }
  });

  it("does not link sign-up URLs with unsafe schemes written directly to KV", async () => {
    const { env, tags } = fakeEnv();
    await tags.put("ann/x", JSON.stringify({ status: "approved", signup_url: "javascript:alert(1)" }));
    const body = await (await get(env, "/subscriptions")).text();
    expect(body).not.toContain('href="javascript:');
  });

  it("lists all subscriptions", async () => {
    const { env, tags } = fakeEnv();
    await tags.put("ann/a", JSON.stringify({ status: "approved" }));
    await tags.put("bob/b", JSON.stringify({ status: "pending" }));
    const body = await (await get(env, "/subscriptions")).text();
    expect(body).toContain("ann+a@mail.example.org");
    expect(body).toContain("bob+b@mail.example.org");
  });

  it("skips keys that are not user/tag", async () => {
    const { env, tags } = fakeEnv();
    await tags.put("ann", JSON.stringify({ active: true }));
    await tags.put("ann/a", JSON.stringify({ status: "approved" }));
    const body = await (await get(env, "/subscriptions")).text();
    expect(body).toContain('action="/users/ann/tags"');
    expect(body).not.toContain('action="/users/an/tags"');
  });
});

describe("settings", () => {
  it("shows effective values and their source", async () => {
    const { env, settings } = fakeEnv({ POLICY_MODE: "quarantine", TAG_DAILY_CAP: "20" });
    await settings.put("settings", JSON.stringify({ tag_daily_cap: 5 }));
    const body = await (await get(env, "/settings")).text();
    expect(body).toMatch(/Per-tag daily cap<\/td><td><strong>5<\/strong><\/td>\s*<td>override/);
    expect(body).toMatch(/Policy mode \(unknown tags\)<\/td><td><strong>quarantine<\/strong><\/td>\s*<td>config/);
  });

  it("saves valid overrides, drops blanks, rejects invalid input", async () => {
    const { env, settings } = fakeEnv();
    await post(env, "/settings", { policy_mode: "quarantine", global_daily_cap: "100", tag_daily_cap: "" });
    expect(settings.json("settings")).toEqual({ policy_mode: "quarantine", global_daily_cap: 100 });

    const res = await post(env, "/settings", { global_daily_cap: "-5" });
    expect(res.headers.get("location")).toContain("err=");
    expect(settings.json("settings")).toEqual({ policy_mode: "quarantine", global_daily_cap: 100 });
  });

  it("is read-only without a SETTINGS binding", async () => {
    const { env } = fakeEnv({}, false);
    const body = await (await get(env, "/settings")).text();
    expect(body).toContain("No SETTINGS KV binding");
    const res = await post(env, "/settings", { global_daily_cap: "100" });
    expect(res.headers.get("location")).toContain("err=");
  });
});

describe("overview", () => {
  it("shows today's usage against caps", async () => {
    const { env, users, ratelimit } = fakeEnv();
    const day = new Date().toISOString().slice(0, 10);
    await users.put("ann", JSON.stringify({ active: true }));
    await ratelimit.put(`rl:global:${day}`, "7");
    await ratelimit.put(`rl:tag:ann/news:${day}`, "3");
    await ratelimit.put("rl:global:2000-01-01", "99");
    const body = await (await get(env, "/")).text();
    expect(body).toContain("1 active, 0 disabled");
    expect(body).toMatch(/<code>global<\/code><\/td><td>7<\/td><td>500/);
    expect(body).toMatch(/<code>tag:ann\/news<\/code><\/td><td>3<\/td><td>50/);
    expect(body).not.toContain(">99<");
  });
});

describe("R2 listing contracts", () => {
  it("paginates objects and projects metadata only when requested", async () => {
    const { archive } = fakeEnv();
    archive.pageSize = 1;
    for (const key of ["a", "b"]) await archive.put(key, "mail", {
      customMetadata: { user: "ann" }, httpMetadata: { contentType: "text/plain" },
    });
    const first = await archive.list();
    expect(first.truncated).toBe(true);
    expect(first.objects.map((o) => o.key)).toEqual(["a"]);
    expect(first.objects[0].customMetadata).toBeUndefined();
    expect(first.objects[0].httpMetadata).toBeUndefined();
    const second = await archive.list({ cursor: first.cursor, include: ["customMetadata", "httpMetadata"] });
    expect(second.truncated).toBe(false);
    expect(second.objects.map((o) => o.key)).toEqual(["b"]);
    expect(second.objects[0].customMetadata).toEqual({ user: "ann" });
    expect(second.objects[0].httpMetadata).toEqual({ contentType: "text/plain" });
  });

  it("reads quarantine day and object pages with custom metadata", async () => {
    const { env, archive } = fakeEnv();
    archive.pageSize = 1;
    const keys = [
      `incoming/quarantine/2026-10-26/${"a".repeat(64)}.eml`,
      `incoming/quarantine/2026-10-26/${"b".repeat(64)}.eml`,
      `incoming/quarantine/2026-10-25/${"c".repeat(64)}.eml`,
    ];
    for (const [i, key] of keys.entries()) {
      await archive.put(key, `Subject: message ${i}\r\n\r\n`, { customMetadata: { user: "ann", tag: String(i) } }, new Date(`2026-10-2${i === 2 ? 5 : 6}T0${i}:00:00Z`));
    }
    const items = await listQuarantine(env);
    expect(items.map((o) => o.key)).toEqual([keys[1], keys[0], keys[2]]);
    expect(items.map((o) => o.meta.tag)).toEqual(["1", "0", "2"]);
  });
});

describe("quarantine", () => {
  it("lists newest first with escaped header summaries", async () => {
    const { env, archive } = fakeEnv();
    await archive.put(QKEY, EML, { customMetadata: { rcpt_to: "ann@mail.example.org" } }, new Date("2026-10-25T10:00:00Z"));
    const newer = `incoming/quarantine/2026-10-26/${"b".repeat(64)}.eml`;
    await archive.put(newer, "Subject: newer\r\n\r\n", {}, new Date("2026-10-26T10:00:00Z"));
    await archive.put(`incoming/archive/2026-10-26/${"c".repeat(64)}.eml`, "Subject: archived\r\n\r\n");

    const body = await (await get(env, "/quarantine")).text();
    expect(body.indexOf("newer")).toBeLessThan(body.indexOf("alert(1)"));
    expect(body).not.toContain("archived");
    expect(body).not.toContain("<script>alert");
    expect(body).toContain("&lt;script&gt;alert(1)&lt;/script&gt;");
  });

  it("downloads raw messages as attachments only for quarantine keys", async () => {
    const { env, archive } = fakeEnv();
    await archive.put(QKEY, EML);
    await archive.put("incoming/archive/x.eml", "secret");
    const res = await get(env, `/quarantine/raw?key=${encodeURIComponent(QKEY)}`);
    expect(res.status).toBe(200);
    expect(res.headers.get("content-type")).toBe("application/octet-stream");
    expect(res.headers.get("content-disposition")).toContain("attachment");
    expect(await res.text()).toBe(EML);
    expect((await get(env, `/quarantine/raw?key=${encodeURIComponent("incoming/archive/x.eml")}`)).status).toBe(404);
  });

  it("promotes a message to incoming/archive/ with provenance", async () => {
    const { env, archive } = fakeEnv();
    await archive.put(QKEY, EML, {
      httpMetadata: { contentType: "message/rfc822" },
      customMetadata: { user: "ann", tag: "" },
    });
    const res = await post(env, "/quarantine/promote", { key: QKEY });
    expect(res.headers.get("location")).toContain("msg=");
    expect(archive.objects.has(QKEY)).toBe(false);
    const moved = archive.objects.get(`incoming/archive/2026-10-25/${HASH}.eml`)!;
    expect(new TextDecoder().decode(moved.bytes)).toBe(EML);
    expect(moved.httpMetadata).toEqual({ contentType: "message/rfc822" });
    expect(moved.customMetadata).toMatchObject({ user: "ann", promoted_by: "admin@example.org" });
  });

  it("refuses to promote keys outside quarantine", async () => {
    const { env, archive } = fakeEnv();
    await archive.put("incoming/failed/x.eml", "x");
    const res = await post(env, "/quarantine/promote", { key: "incoming/failed/x.eml" });
    expect(res.headers.get("location")).toContain("err=");
    expect(archive.objects.has("incoming/failed/x.eml")).toBe(true);
  });
});
