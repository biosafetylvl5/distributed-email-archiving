import { describe, expect, it } from "vitest";
import { handleAdmin, type Authenticator } from "../src/admin/app";
import { parseStatus, summarizeCaptures } from "../src/admin/captures";
import type { StoredObject } from "../src/admin/stats";
import { fakeEnv } from "./fakes";

const allow: Authenticator = async () => ({ ok: true, email: "admin@example.org" });
const get = (env: Parameters<typeof handleAdmin>[1], path: string) =>
  handleAdmin(new Request("https://admin.example.org" + path), env, allow);

const sha = (c: string) => c.repeat(64);
const emlKey = (day: string, c: string) => `incoming/archive/${day}/${sha(c)}.eml`;

function status(c: string, day: string, meta: Record<string, string>): StoredObject {
  return {
    key: `issue-status/${sha(c)}.json`,
    size: 10,
    uploaded: new Date(),
    customMetadata: { source: `s3://bucket/${emlKey(day, c)}`, ...meta },
  };
}

function eml(day: string, c: string, meta: Record<string, string> = { user: "ann", tag: "news" }): StoredObject {
  return { key: emlKey(day, c), size: 100, uploaded: new Date(`${day}T08:00:00Z`), customMetadata: meta };
}

describe("parseStatus", () => {
  it("reads counts, tolerates junk and ignores other keys", () => {
    const c = parseStatus(status("a", "2026-10-26", { state: "done", assets_ok: "12", links: "x", pixels: "-3" }));
    expect(c).toMatchObject({ sha: sha("a"), source: emlKey("2026-10-26", "a"), state: "done" });
    expect(c?.counts.assets_ok).toBe(12);
    expect(c?.counts.links).toBe(0);
    expect(c?.counts.pixels).toBe(0);
    expect(parseStatus({ key: "issue-status/nope.json", size: 1, uploaded: new Date(), customMetadata: { source: "x" } })).toBeNull();
    expect(parseStatus({ key: `issue-status/${sha("b")}.json`, size: 1, uploaded: new Date() })).toBeNull();
  });
});

describe("summarizeCaptures", () => {
  it("joins status records to archived messages", () => {
    const s = summarizeCaptures(
      [
        eml("2026-10-24", "a"),
        eml("2026-10-25", "b"),
        eml("2026-10-26", "c", { user: "bob", tag: "" }),
        { key: "incoming/quarantine/2026-10-26/x.eml", size: 1, uploaded: new Date() },
      ],
      [
        status("a", "2026-10-24", { state: "done", assets_ok: "10", links: "5", links_capture: "3", links_skip: "2", completed_at: "2026-10-24T09:00:00Z" }),
        status("b", "2026-10-25", { state: "partial", assets_ok: "4", assets_failed: "1", completed_at: "2026-10-25T09:00:00Z" }),
        status("d", "2026-10-26", { state: "done" }), // its message isn't listed (quarantined or gone)
      ],
    );
    expect(s.states).toEqual({ done: 1, partial: 1, pending: 1 });
    expect(s.totals).toMatchObject({ assets_ok: 14, assets_failed: 1, links: 5, links_capture: 3, links_skip: 2 });
    expect(s.lastCompleted).toEqual(new Date("2026-10-25T09:00:00Z"));
    expect(s.bySubscription.get("ann/news")).toEqual({ done: 1, partial: 1, pending: 0 });
    expect(s.bySubscription.get("bob/")).toEqual({ done: 0, partial: 0, pending: 1 });
    expect(s.messages.map((m) => m.day)).toEqual(["2026-10-26", "2026-10-25", "2026-10-24"]);
  });
});

describe("captures page and overview", () => {
  async function seeded() {
    const f = fakeEnv();
    await f.archive.put(emlKey("2026-10-25", "a"), "x", { customMetadata: { user: "ann", tag: "news" } });
    await f.archive.put(emlKey("2026-10-26", "b"), "x", { customMetadata: { user: "ann", tag: "news" } });
    await f.archive.put(`issue-status/${sha("a")}.json`, "{}", {
      customMetadata: {
        source: `s3://bucket/${emlKey("2026-10-25", "a")}`, state: "partial", assets_ok: "7", assets_failed: "2",
        links: "9", links_capture: "4", links_resolve: "3", links_skip: "2", completed_at: "2026-10-25T09:00:00Z",
      },
    });
    return f;
  }

  it("lists messages with their status and filters by state", async () => {
    const { env } = await seeded();
    const all = await (await get(env, "/captures")).text();
    expect(all).toContain("<strong>all (2)</strong>");
    expect(all).toContain(sha("a").slice(0, 12));
    expect(all).toContain("(2 failed)");
    expect(all).toMatch(/>pending</);
    const partial = await (await get(env, "/captures?state=partial")).text();
    expect(partial).toContain(sha("a").slice(0, 12));
    expect(partial).not.toContain(sha("b").slice(0, 12));
    const junk = await (await get(env, "/captures?state=%3Cscript%3E")).text();
    expect(junk).toContain("<strong>all (2)</strong>");
    expect(junk).not.toContain("<script>");
  });

  it("shows capture totals on the overview", async () => {
    const { env } = await seeded();
    const body = await (await get(env, "/")).text();
    expect(body).toContain("1 of 2 processed");
    expect(body).toContain("7 stored, 2 failed");
    expect(body).toContain("9 found: 4 to capture, 3 redirects to resolve, 2 never fetched");
    expect(body).toMatch(/1 <span class="muted">\(<\/span><a class="status-pending" href="\/captures\?state=partial">1 partial<\/a>, <span class="muted">1 pending/);
  });
});
