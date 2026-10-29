import { describe, expect, it } from "vitest";
import { handleAdmin, type Authenticator } from "../src/admin/app";
import type { Env } from "../src/env";
import { fakeEnv } from "./fakes";

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

describe("subscriptions", () => {
  it("creates and edits tags, preserving unknown fields", async () => {
    const { env, users, tags } = fakeEnv();
    await users.put("ann", JSON.stringify({ active: true }));

    await post(env, "/users/ann/tags", { tag: "Ramen_Weekly", status: "approved", sender_domain: "Mail.Ramen.Example" });
    expect(tags.json("ann/ramen_weekly")).toEqual({
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
    expect(tags.json("ann/ramen_weekly")).toEqual({ status: "blocked", publish: true, note: "spammy", custom: 1 });
  });

  it("sets a capture size limit and mirrors every limit to R2 for dea-capture", async () => {
    const { env, users, tags, archive } = fakeEnv();
    await users.put("ann", JSON.stringify({ active: true }));
    await tags.put("ann/other", JSON.stringify({ status: "approved", capture_limit_mb: 50 }));
    await post(env, "/users/ann/tags", { tag: "ramen", status: "approved", capture_limit_mb: "200" });
    expect(tags.json("ann/ramen")).toMatchObject({ capture_limit_mb: 200 });
    const mirror = async () => JSON.parse(await (await archive.get("config/capture-limits.json"))!.text());
    expect((await mirror()).limits).toEqual({ "ann/other": 50 * 2 ** 20, "ann/ramen": 200 * 2 ** 20 });
    expect(await (await get(env, "/users/ann")).text()).toContain("captures up to 200 MiB");

    // the short form leaves it alone; blank clears it
    await post(env, "/users/ann/tags", { tag: "ramen", status: "blocked" });
    expect(tags.json("ann/ramen")).toMatchObject({ capture_limit_mb: 200 });
    await post(env, "/users/ann/tags", { tag: "ramen", status: "approved", capture_limit_mb: "" });
    expect(tags.json("ann/ramen")).not.toHaveProperty("capture_limit_mb");
    expect((await mirror()).limits).toEqual({ "ann/other": 50 * 2 ** 20 });

    for (const bad of ["0", "1.5", "-3", "lots", String(2 * 1024 * 1024)]) {
      const res = await post(env, "/users/ann/tags", { tag: "ramen", status: "approved", capture_limit_mb: bad });
      expect(new URLSearchParams((res.headers.get("location") ?? "").split("?")[1]).get("err"), bad).toContain("Invalid capture size limit");
    }
    expect(tags.json("ann/ramen")).not.toHaveProperty("capture_limit_mb");
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
    const url = "https://ramen.example/subscribe?ref=a&b=\"x\"";
    await post(env, "/users/ann/tags", { tag: "ramen", status: "approved", signup_url: url });
    expect(tags.json("ann/ramen")).toEqual({ status: "approved", publish: false, signup_url: url });

    const body = await (await get(env, "/subscriptions")).text();
    expect(body).toContain('href="https://ramen.example/subscribe?ref=a&amp;b=&quot;x&quot;"');

    await post(env, "/users/ann/tags", { tag: "ramen", status: "approved", signup_url: "" });
    expect(tags.json("ann/ramen")).toEqual({ status: "approved", publish: false });
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
    expect(tags.json("ann/ramen")).toEqual({
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
    expect(tags.json("ann/ramen")).toEqual({ ...full, name: "Ramen", publish: false });
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
