import { describe, expect, it } from "vitest";
import { handleAdmin, type Authenticator } from "../src/admin/app";
import { LARGEST_KEY, parseLargest, parseRange, servable } from "../src/admin/files";
import type { Env } from "../src/env";
import { fakeEnv } from "./fakes";

const allow: Authenticator = async () => ({ ok: true, email: "admin@example.org" });
const get = (env: Env, path: string, headers: Record<string, string> = {}) =>
  handleAdmin(new Request("https://admin.example.org" + path, { headers }), env, allow);

const H = "e".repeat(64);
const SHA = "a".repeat(64);
const VIDEO = `captures/ee/${H}/20261027T000000Z/media.mp4`;
const WACZ = `captures/ff/${"f".repeat(64)}/20261027T000000Z/page.wacz`;
const ASSET = `issues/${SHA}/assets/${"c".repeat(64)}.svg`;
const file = (key: string) => `/files?key=${encodeURIComponent(key)}`;

const LARGEST = {
  computed_at: "2026-10-29T15:00:00+00:00",
  files: [
    { key: VIDEO, size: 2 * 1024 ** 3, modified: "2026-10-27T00:00:00+00:00", area: "captures", kind: "media",
      url: "https://video.example.com/watch/1" },
    { key: `issues/${SHA}/assets/${"c".repeat(64)}.png`, size: 3_000_000, modified: "2026-10-26T00:00:00+00:00",
      area: "embeds", sha: SHA },
    { key: `incoming/archive/2026-10-25/${SHA}.eml`, size: 900_000, area: "archive", sha: SHA },
    { key: "x", size: 1, area: "other", url: "javascript:alert(1)", sha: "nope" },
    "junk",
  ],
};

async function seed() {
  const f = fakeEnv();
  await f.archive.put(VIDEO, "0123456789", { httpMetadata: { contentType: "video/mp4" } });
  await f.archive.put(WACZ, "PK", { httpMetadata: { contentType: "application/zip" } });
  await f.archive.put(ASSET, "<svg onload=alert(1)>", { httpMetadata: { contentType: "image/svg+xml" } });
  await f.archive.put(`issues/${SHA}/links.json`, "{}", { httpMetadata: { contentType: "application/json" } });
  await f.archive.put(LARGEST_KEY, JSON.stringify(LARGEST));
  return f;
}

describe("largest files", () => {
  it("parses dea-capture's list and drops what doesn't check out", () => {
    const l = parseLargest(JSON.stringify(LARGEST));
    expect(l?.files).toHaveLength(4);
    expect(l?.files[0]).toMatchObject({ kind: "media", url: "https://video.example.com/watch/1" });
    expect(l?.files[3]).toMatchObject({ url: undefined, sha: undefined });
    expect(parseLargest("{}")).toBeNull();
  });

  it("lists them with links on the storage page", async () => {
    const { env } = await seed();
    const body = await (await get(env, "/storage")).text();
    expect(body).toContain("Largest 4 files");
    expect(body).toContain("2.00 GiB");
    expect(body).toContain(`<a href="${file(VIDEO).replace(/&/g, "&amp;")}"`);
    expect(body).toContain('href="https://video.example.com/watch/1" target="_blank" rel="noopener noreferrer"');
    expect(body).toContain(`<a href="/messages/${SHA}">`);
    expect(body).not.toContain("javascript:");
    // the archived .eml is reached through its message, not /files
    expect(body).not.toContain(file(`incoming/archive/2026-10-25/${SHA}.eml`));
  });

  it("says when there's no list yet", async () => {
    const { env } = fakeEnv();
    expect(await (await get(env, "/storage")).text()).toContain("Not listed yet");
  });
});

describe("/files", () => {
  it("only serves captures and embeds", () => {
    expect(servable(VIDEO)).toBe(true);
    expect(servable(ASSET)).toBe(true);
    for (const k of [
      `issues/${SHA}/links.json`,
      `issues/${SHA}/captures.json`,
      `incoming/archive/2026-10-25/${SHA}.eml`,
      `captures/index/${H}.json`,
      `captures/ee/${H}/20261027T000000Z/../../x`,
      LARGEST_KEY,
    ]) {
      expect(servable(k), k).toBe(false);
    }
  });

  it("shows media inline, sandboxed, and downloads the rest", async () => {
    const { env } = await seed();
    const v = await get(env, file(VIDEO));
    expect(v.status).toBe(200);
    expect(v.headers.get("content-type")).toBe("video/mp4");
    expect(v.headers.get("content-disposition")).toBe('inline; filename="media.mp4"');
    expect(v.headers.get("content-security-policy")).toMatch(/^sandbox; default-src 'none'/);
    expect(v.headers.get("x-content-type-options")).toBe("nosniff");
    expect(v.headers.get("accept-ranges")).toBe("bytes");
    for (const k of [WACZ, ASSET]) {
      const r = await get(env, file(k));
      expect(r.headers.get("content-type"), k).toBe("application/octet-stream");
      expect(r.headers.get("content-disposition"), k).toMatch(/^attachment;/);
    }
    expect((await get(env, file(`issues/${SHA}/links.json`))).status).toBe(404);
    expect((await get(env, file(`captures/00/${"0".repeat(64)}/20261027T000000Z/x`))).status).toBe(404);
  });

  it("serves byte ranges, so video can seek", async () => {
    const { env } = await seed();
    const r = await get(env, file(VIDEO), { range: "bytes=2-5" });
    expect(r.status).toBe(206);
    expect(r.headers.get("content-range")).toBe("bytes 2-5/10");
    expect(await r.text()).toBe("2345");
    const tail = await get(env, file(VIDEO), { range: "bytes=-3" });
    expect(await tail.text()).toBe("789");
    const open = await get(env, file(VIDEO), { range: "bytes=8-" });
    expect(open.headers.get("content-range")).toBe("bytes 8-9/10");
    expect((await get(env, file(VIDEO), { range: "bytes=10-" })).status).toBe(416);
    expect(parseRange("bytes=0-99", 10)).toEqual({ offset: 0, length: 10 });
    expect(parseRange("bytes=1-2,4-5", 10)).toBeNull();
  });
});
