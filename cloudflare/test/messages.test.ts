import { describe, expect, it } from "vitest";
import { handleAdmin, type Authenticator } from "../src/admin/app";
import { parseViewMeta, VIEW_CSP } from "../src/admin/messages";
import { parseStorage, SUMMARY_KEY } from "../src/admin/summary";
import type { Env } from "../src/env";
import { fakeEnv } from "./fakes";

const allow: Authenticator = async () => ({ ok: true, email: "admin@example.org" });
const get = (env: Env, path: string) => handleAdmin(new Request("https://admin.example.org" + path), env, allow);

const SHA = "a".repeat(64);
const ASSET = "b".repeat(64);
const KEY = `incoming/archive/2026-10-25/${SHA}.eml`;
const EML = "From: Writer <w@example.com>\r\nSubject: <b>Hi</b>\r\n\r\n<script>alert(1)</script>";

const META = {
  view_version: 1,
  rendered_at: "2026-10-29T10:00:00Z",
  source: `s3://bucket/${KEY}`,
  headers: { subject: "October <update>", from: "Writer <w@example.com>", date: "Sun, 25 Oct 2026 08:00:00 +0000" },
  body: "html",
  parts: [
    { content_type: "text/html", bytes: 900 },
    { content_type: "image/png", bytes: 300, cid: true, disposition: "inline" },
  ],
  sizes: { eml: 1200, assets: 2048, asset_files: 1 },
  removed: { pixels: 2, links_removed: 3, links_kept: 5, bogus: "x" },
};

async function seed(withView = true) {
  const f = fakeEnv();
  await f.archive.put(KEY, EML, { customMetadata: { user: "ann", tag: "news" } });
  await f.archive.put(`issue-status/${SHA}.json`, "{}", { customMetadata: { source: `s3://bucket/${KEY}` } });
  await f.archive.put(`issues/${SHA}/assets/${ASSET}.png`, new Uint8Array(2048).buffer, {
    httpMetadata: { contentType: "image/png" },
  });
  await f.archive.put(`issues/${SHA}/links.json`, "{}");
  if (withView) {
    await f.archive.put(`issues/${SHA}/view/index.html`, "<p>hello</p>", {
      httpMetadata: { contentType: "text/html; charset=utf-8" },
    });
    await f.archive.put(`issues/${SHA}/view/meta.json`, JSON.stringify(META));
  }
  return f;
}

describe("parseViewMeta", () => {
  it("reads what dea-capture writes and drops junk", () => {
    const m = parseViewMeta(JSON.stringify(META));
    expect(m?.headers.subject).toBe("October <update>");
    expect(m?.sizes).toEqual({ eml: 1200, assets: 2048, assetFiles: 1 });
    expect(m?.parts[1]).toEqual({ contentType: "image/png", bytes: 300, cid: true, disposition: "inline", filename: undefined });
    expect(m?.removed).toEqual({ pixels: 2, links_removed: 3, links_kept: 5 });
    expect(parseViewMeta("nope")).toBeNull();
    expect(parseViewMeta("null")).toBeNull();
  });
});

describe("message page", () => {
  it("frames the rendered view, escapes the headers and shows sizes", async () => {
    const { env } = await seed();
    const res = await get(env, `/messages/${SHA}`);
    expect(res.status).toBe(200);
    const body = await res.text();
    expect(body).toContain("October &lt;update&gt;");
    expect(body).not.toContain("<update>");
    expect(body).toContain(`<iframe class="mail" src="/messages/${SHA}/view/index.html"`);
    expect(body).toContain('sandbox="allow-same-origin allow-popups allow-popups-to-escape-sandbox"');
    expect(body).toContain("2 tracking pixels");
    expect(body).toContain("2.0 KiB"); // the embedded file
    expect(body).toContain(`/messages/${SHA}/raw`);
    const csp = res.headers.get("content-security-policy") ?? "";
    expect(csp).toContain("frame-src 'self'");
    expect(csp).toContain("frame-ancestors 'none'");
    expect(res.headers.get("x-frame-options")).toBe("DENY");
  });

  it("serves the view sandboxed, framable only by us", async () => {
    const { env } = await seed();
    const res = await get(env, `/messages/${SHA}/view/index.html`);
    expect(res.status).toBe(200);
    expect(await res.text()).toBe("<p>hello</p>");
    expect(res.headers.get("content-security-policy")).toBe(VIEW_CSP);
    expect(VIEW_CSP).toMatch(/^sandbox /);
    expect(VIEW_CSP).not.toContain("allow-scripts");
    expect(VIEW_CSP).toContain("default-src 'none'");
    expect(res.headers.get("x-frame-options")).toBe("SAMEORIGIN");
    expect(res.headers.get("x-content-type-options")).toBe("nosniff");

    const img = await get(env, `/messages/${SHA}/view/assets/${ASSET}.png`);
    expect(img.status).toBe(200);
    expect(img.headers.get("content-type")).toBe("image/png");
    expect(img.headers.get("content-security-policy")).toBe(VIEW_CSP);
  });

  it("serves nothing outside the view", async () => {
    const { env } = await seed();
    for (const p of [
      `/messages/${SHA}/view/meta.json`,
      `/messages/${SHA}/view/links.json`,
      `/messages/${SHA}/view/../links.json`,
      `/messages/${SHA}/view/assets/../../links.json`,
      `/messages/${SHA}/view/assets/x.png`,
      `/messages/${"z".repeat(64)}`,
      `/messages/${"c".repeat(64)}`,
    ]) {
      expect((await get(env, p)).status, p).toBe(404);
    }
  });

  it("shows the source as inert text, or downloads it", async () => {
    const { env } = await seed();
    const src = await get(env, `/messages/${SHA}/source`);
    expect(src.headers.get("content-type")).toBe("text/plain; charset=utf-8");
    expect(src.headers.get("content-security-policy")).toMatch(/^sandbox; default-src 'none'/);
    expect(await src.text()).toBe(EML);
    const dl = await get(env, `/messages/${SHA}/raw`);
    expect(dl.headers.get("content-type")).toBe("application/octet-stream");
    expect(dl.headers.get("content-disposition")).toBe(`attachment; filename="${SHA}.eml"`);
  });

  it("offers the source of a message not rendered yet", async () => {
    const { env, archive } = await seed(false);
    archive.objects.delete(`issue-status/${SHA}.json`);
    expect((await get(env, `/messages/${SHA}`)).status).toBe(404); // nothing says where it is
    const res = await get(env, `/messages/${SHA}?day=2026-10-25`);
    const body = await res.text();
    expect(body).toContain("hasn't rendered this message yet");
    expect(body).toContain(`/messages/${SHA}/source`);
    expect(body).not.toContain("<iframe");
    expect((await get(env, `/messages/${SHA}/source?day=2026-10-25`)).status).toBe(200);
    expect((await get(env, `/messages/${SHA}/source?day=../x`)).status).toBe(404);
  });

  it("views a quarantined message's source as text", async () => {
    const { env, archive } = fakeEnv();
    const q = `incoming/quarantine/2026-10-25/${SHA}.eml`;
    await archive.put(q, EML);
    const res = await get(env, `/quarantine/raw?key=${encodeURIComponent(q)}&view=source`);
    expect(res.headers.get("content-type")).toBe("text/plain; charset=utf-8");
    expect(res.headers.get("content-security-policy")).toMatch(/^sandbox;/);
    const dl = await get(env, `/quarantine/raw?key=${encodeURIComponent(q)}`);
    expect(dl.headers.get("content-type")).toBe("application/octet-stream");
  });
});

const STORAGE = {
  computed_at: "2026-10-29T09:00:00+00:00",
  areas: {
    archive: { count: 264, bytes: 27_100_000 },
    captures: { count: 14_470, bytes: 84_850_000_000 },
    embeds: { count: 5486, bytes: 470_000_000 },
    nonsense: { count: 1, bytes: 1 },
  },
  captures: { page: { count: 9000, bytes: 80_000_000_000, urls: 2400 }, media: { count: 100, bytes: 4e9, urls: 78 } },
  messages: { [SHA]: 123_456, "not-a-sha": 5 },
};

describe("storage", () => {
  it("parses dea-capture's counts", () => {
    const st = parseStorage(STORAGE);
    expect(st?.areas.captures).toEqual({ count: 14_470, bytes: 84_850_000_000 });
    expect(st?.areas.quarantine).toEqual({ count: 0, bytes: 0 });
    expect(st?.captures.page.urls).toBe(2400);
    expect([...(st?.messages.keys() ?? [])]).toEqual([SHA]);
    expect(parseStorage(undefined)).toBeNull();
    expect(parseStorage({ areas: {} })).toBeNull();
  });

  it("shows on the overview and per message on /captures", async () => {
    const { env, archive } = await seed();
    await archive.put(
      SUMMARY_KEY,
      JSON.stringify({
        version: 1,
        generated_at: new Date().toISOString(),
        incoming: [{ key: KEY, size: 1200, uploaded: "2026-10-25T08:00:00Z", customMetadata: { user: "ann", tag: "news" } }],
        statuses: [{ key: `issue-status/${SHA}.json`, customMetadata: { source: KEY, state: "done" } }],
        urls: { byKind: {}, wayback: 0, lastUpdated: null },
        storage: STORAGE,
      }),
    );
    const overview = await (await get(env, "/")).text();
    expect(overview).toContain("<h2>Storage");
    expect(overview).toContain("79.02 GiB"); // link captures
    expect(overview).toContain("Pages: 74.51 GiB for 2400 URLs");
    const captures = await (await get(env, "/captures")).text();
    expect(captures).toContain(`<a href="/messages/${SHA}?day=2026-10-25"`);
    expect(captures).toContain("120.6 KiB");
  });

  it("says when it hasn't been counted", async () => {
    const { env } = fakeEnv();
    expect(await (await get(env, "/")).text()).toContain("Not counted yet");
  });
});
