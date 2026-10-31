import { describe, expect, it } from "vitest";
import { handleAdmin, type Authenticator } from "../src/admin/app";
import { fromSummary, SUMMARY_KEY } from "../src/admin/summary";
import { fakeEnv } from "./fakes";
import processorSummary from "./fixtures/processor-summary";

const allow: Authenticator = async () => ({ ok: true, email: "admin@example.org" });
const get = (env: Parameters<typeof handleAdmin>[1], path: string) =>
  handleAdmin(new Request("https://admin.example.org" + path), env, allow);

const sha = (c: string) => c.repeat(64);
const emlKey = (day: string, c: string) => `incoming/archive/${day}/${sha(c)}.eml`;

function file(generatedAt: string) {
  return JSON.stringify({
    version: 1,
    generated_at: generatedAt,
    incoming: [
      { key: emlKey("2026-10-25", "a"), size: 100, uploaded: "2026-10-25T08:00:00+00:00", customMetadata: { user: "ann", tag: "news" } },
      { key: emlKey("2026-10-26", "b"), size: 50, uploaded: "2026-10-26T08:00:00+00:00", customMetadata: { user: "ann", tag: "news" } },
      { key: "incoming/quarantine/2026-10-26/x.eml", size: 7, uploaded: "2026-10-26T09:00:00+00:00", customMetadata: {} },
    ],
    statuses: [
      {
        key: `issue-status/${sha("a")}.json`,
        customMetadata: { source: `s3://bucket/${emlKey("2026-10-25", "a")}`, state: "done", assets_ok: "7" },
      },
    ],
    urls: {
      total: 99, // recomputed, not trusted
      byKind: { page: { ok: 2, queued: 3 }, gallery: { ok: 1 }, nope: { ok: 5 }, media: { weird: 4 } },
      wayback: 1,
      lastUpdated: "2026-10-27T10:00:00+00:00",
    },
  });
}

describe("fromSummary", () => {
  it("reads processor-built aggregates with the same results as the legacy computation", () => {
    const raw = JSON.stringify(processorSummary);
    const doc = JSON.parse(raw);
    const now = new Date(doc.generated_at);
    const precomputed = fromSummary(raw, now);
    const legacy = fromSummary(JSON.stringify({ ...doc, aggregates: undefined }), now);
    expect(precomputed).toEqual(legacy);
    expect(precomputed?.stats.tags.get("ann/news")?.last).toEqual(new Date("2026-10-26T08:00:00Z"));
    expect(precomputed?.captures.lastCompleted).toEqual(new Date("2026-10-25T10:00:00Z"));
    expect(precomputed?.captures.messages[0].capture).toBeNull();
    expect(precomputed?.captures.messages[1].capture?.completedAt).toEqual(new Date("2026-10-25T10:00:00Z"));
    // The legacy rows are not aggregated on requests with precomputed fields.
    doc.incoming = [];
    doc.statuses = [];
    expect(fromSummary(JSON.stringify(doc), now)).toEqual(precomputed);
  });

  it("rebuilds the stats and capture summary from the object", () => {
    const now = new Date("2026-10-27T12:00:00Z");
    const s = fromSummary(file("2026-10-27T11:58:00+00:00"), now);
    expect(s?.generatedAt).toEqual(new Date("2026-10-27T11:58:00Z"));
    expect(s?.stats.totals).toEqual({ archive: { count: 2, bytes: 150 }, quarantine: { count: 1, bytes: 7 } });
    expect(s?.stats.tags.get("ann/news")?.archive).toBe(2);
    expect(s?.captures.states).toEqual({ done: 1, partial: 0, pending: 1 });
    expect(s?.captures.totals.assets_ok).toBe(7);
    expect(s?.captures.urls.total).toBe(6);
    expect(s?.captures.urls.byKind.page).toMatchObject({ ok: 2, queued: 3 });
    expect(s?.captures.urls.byKind.gallery.ok).toBe(1);
    expect(s?.captures.urls.lastUpdated).toEqual(new Date("2026-10-27T10:00:00Z"));
  });

  it("reads trimmed statuses: zero counts left out, the source without its bucket", () => {
    const doc = JSON.parse(file("2026-10-27T11:58:00+00:00"));
    doc.statuses = [
      { key: `issue-status/${sha("a")}.json`, customMetadata: { source: emlKey("2026-10-25", "a"), state: "done", links: "4" } },
    ];
    const s = fromSummary(JSON.stringify(doc), new Date("2026-10-27T12:00:00Z"));
    expect(s?.captures.states).toEqual({ done: 1, partial: 0, pending: 1 });
    expect(s?.captures.totals.links).toBe(4);
    expect(s?.captures.totals.assets_ok).toBe(0);
    expect(s?.captures.messages.find((m) => m.key === emlKey("2026-10-25", "a"))?.capture?.state).toBe("done");
  });

  it.each([null, 7, "bad", {}, { key: 8 }, { key: emlKey("2026-10-25", "a"), customMetadata: null },
    { key: emlKey("2026-10-25", "a"), customMetadata: ["ann"] },
    { key: emlKey("2026-10-25", "a"), customMetadata: { envelope_from: 7 } },
    { key: emlKey("2026-10-25", "a"), size: "large" },
  ])("rejects malformed stored elements: %j", (element) => {
    const now = new Date("2026-10-27T12:00:00Z");
    const doc = JSON.parse(file(now.toISOString()));
    doc.incoming.push(element);
    expect(fromSummary(JSON.stringify(doc), now)).toBeNull();
    doc.incoming.pop();
    doc.statuses.push(element);
    expect(fromSummary(JSON.stringify(doc), now)).toBeNull();
  });

  it("defaults malformed URL counts and dates", () => {
    const now = new Date("2026-10-27T12:00:00Z");
    const doc = JSON.parse(file(now.toISOString()));
    doc.urls = { byKind: { page: { ok: { toString: 7 }, failed: -1 } }, wayback: "bad", lastUpdated: {} };
    const s = fromSummary(JSON.stringify(doc), now);
    expect(s?.captures.urls.total).toBe(0);
    expect(s?.captures.urls.wayback).toBe(0);
    expect(s?.captures.urls.lastUpdated).toBeNull();
  });

  it("ignores malformed aggregate elements and uses the legacy rows", () => {
    const raw = JSON.stringify(processorSummary);
    const now = new Date(processorSummary.generated_at);
    const expected = fromSummary(raw, now);
    const { stats, captures } = processorSummary.aggregates;
    for (const aggregates of [
      { stats: { ...stats, tags: { "ann/news": null } }, captures },
      { stats: { ...stats, days: [...stats.days, null] }, captures },
      { stats: { ...stats, senders: [...stats.senders, 7] }, captures },
      { stats, captures: { ...captures, messages: [...captures.messages, null] } },
      { stats, captures: { ...captures, messages: [{ ...captures.messages[0], capture: 7 }] } },
    ]) {
      expect(fromSummary(JSON.stringify({ ...processorSummary, aggregates }), now)).toEqual(expected);
    }
  });

  it("rejects junk and other versions", () => {
    const now = new Date();
    expect(fromSummary("not json", now)).toBeNull();
    expect(fromSummary(JSON.stringify({ version: 2, incoming: [], statuses: [] }), now)).toBeNull();
    expect(fromSummary(JSON.stringify({ version: 1 }), now)).toBeNull();
  });
});

describe("pages read the summary", () => {
  it("falls back to live listings when summary members are malformed", async () => {
    const f = fakeEnv();
    await f.archive.put(SUMMARY_KEY, JSON.stringify({ version: 1, incoming: [null], statuses: [] }));
    await f.archive.put(emlKey("2026-10-27", "c"), "x", { customMetadata: { user: "bob", tag: "" } });
    for (const path of ["/", "/captures", "/storage"]) {
      const response = await get(f.env, path);
      expect(response.status).toBe(200);
      const body = await response.text();
      expect(body).toContain(path === "/storage" ? "Not counted yet" : "Counted from the bucket just now");
    }
  });

  it("uses the summary instead of listing, and says how old it is", async () => {
    const f = fakeEnv();
    const fresh = new Date(Date.now() - 3 * 60_000).toISOString();
    await f.archive.put(SUMMARY_KEY, file(fresh));
    // listed live, this message would be counted; the summary wins
    await f.archive.put(emlKey("2026-10-27", "c"), "x", { customMetadata: { user: "bob", tag: "" } });
    const body = await (await get(f.env, "/")).text();
    expect(body).toMatch(/Counted by dea-capture <span title="[^"]+">3 min ago<\/span>/);
    expect(body).toContain("1 of 2 processed");
    expect(body).not.toContain("bob");
    const caps = await (await get(f.env, "/captures")).text();
    expect(caps).toContain("<strong>all (2)</strong>");
    expect(caps).toContain("Counted by dea-capture");
  });

  it("warns when the summary is stale", async () => {
    const f = fakeEnv();
    await f.archive.put(SUMMARY_KEY, file(new Date(Date.now() - 2 * 3600_000).toISOString()));
    const body = await (await get(f.env, "/")).text();
    expect(body).toContain("last counted");
    expect(body).toContain("may be down");
  });

  it("lists the bucket when there is no summary", async () => {
    const f = fakeEnv();
    await f.archive.put(emlKey("2026-10-27", "c"), "x", { customMetadata: { user: "bob", tag: "" } });
    const body = await (await get(f.env, "/")).text();
    expect(body).toContain("Counted from the bucket just now");
    expect(body).toContain("bob");
  });
});
