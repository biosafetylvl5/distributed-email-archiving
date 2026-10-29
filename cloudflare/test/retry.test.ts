import { describe, expect, it } from "vitest";
import { handleAdmin, type Authenticator } from "../src/admin/app";
import { parseRetries, parseRetryRequest, RETRY_PREFIX } from "../src/admin/retry";
import { SUMMARY_KEY } from "../src/admin/summary";
import { fakeEnv, type FakeR2 } from "./fakes";

const ORIGIN = "https://admin.example.org";
const allow: Authenticator = async () => ({ ok: true, email: "admin@example.org" });
const SHA = "c".repeat(64);

const get = (env: Parameters<typeof handleAdmin>[1], path: string) =>
  handleAdmin(new Request(ORIGIN + path), env, allow);

const post = (env: Parameters<typeof handleAdmin>[1], fields: Record<string, string>, origin = ORIGIN) =>
  handleAdmin(
    new Request(ORIGIN + "/captures/retry", { method: "POST", body: new URLSearchParams(fields), headers: { origin } }),
    env,
    allow,
  );

function requests(r2: FakeR2) {
  return [...r2.objects.values()].filter((o) => o.key.startsWith(RETRY_PREFIX));
}

describe("retry requests", () => {
  it("validates what it is asked to retry", () => {
    expect(parseRetryRequest({ scope: "message", sha: SHA })).toEqual({ scope: "message", sha: SHA });
    expect(parseRetryRequest({ scope: "urls", kind: "page", status: "failed" })).toEqual({
      scope: "urls",
      kind: "page",
      status: "failed",
    });
    expect(parseRetryRequest({ scope: "urls", kind: "page", status: "ok" })).toMatch(/Invalid status/);
    expect(parseRetryRequest({ scope: "urls", kind: "nope", status: "failed" })).toMatch(/Invalid kind/);
    expect(parseRetryRequest({ scope: "message", sha: "../x" })).toBe("Invalid message");
    expect(parseRetryRequest({ scope: "all" })).toBe("Invalid retry request");
  });

  it("leaves a request for dea-capture and says so", async () => {
    const { env, archive } = fakeEnv();
    const res = await post(env, { scope: "urls", kind: "direct", status: "retrying", back: "partial" });
    expect(res.status).toBe(303);
    const loc = res.headers.get("location") ?? "";
    expect(loc.startsWith("/captures?state=partial&msg=")).toBe(true);
    expect(new URL(loc, ORIGIN).searchParams.get("msg")).toMatch(/^Retry queued for retrying files/);
    const [req] = requests(archive);
    expect(req.key).toMatch(/^captures\/retry\/\d{8}T\d{6}Z-[0-9a-f]{16}\.json$/);
    const body = JSON.parse(new TextDecoder().decode(req.bytes));
    expect(body).toMatchObject({ scope: "urls", kind: "direct", status: "retrying" });
    // who asked goes to the log, not the bucket
    expect(JSON.stringify(body)).not.toContain("admin@");
  });

  it("refuses bad and cross-origin requests without writing", async () => {
    const { env, archive } = fakeEnv();
    const bad = await post(env, { scope: "message", sha: "nope", back: "javascript:x" });
    expect(bad.headers.get("location")).toMatch(/^\/captures\?err=/);
    expect((await post(env, { scope: "message", sha: SHA }, "https://evil.example")).status).toBe(403);
    expect(requests(archive)).toEqual([]);
  });

  it("parses the processor's results defensively", () => {
    const got = parseRetries({
      waiting: 2,
      recent: [
        { applied_at: "2026-10-29T10:00:00+00:00", scope: "message", sha: SHA, rescheduled: 1, left: 3,
          relinked: { tried: 4, resolved: 2, rate_limited: true } },
        { scope: "message", sha: "<b>", error: 5 },
        "junk",
      ],
    });
    expect(got.waiting).toBe(2);
    expect(got.recent[0]).toMatchObject({ sha: SHA, rescheduled: 1, left: 3,
      relinked: { tried: 4, resolved: 2, rateLimited: true, outOfTime: false } });
    expect(got.recent[1]).toMatchObject({ sha: undefined, error: undefined });
    expect(got.recent).toHaveLength(2);
    expect(parseRetries(null)).toEqual({ waiting: 0, recent: [] });
  });

  it("shows buttons, waiting requests and recent results on /captures", async () => {
    const { env, archive } = fakeEnv();
    const emlKey = `incoming/archive/2026-10-25/${SHA}.eml`;
    await archive.put(SUMMARY_KEY, JSON.stringify({
      version: 1,
      generated_at: new Date().toISOString(),
      incoming: [{ key: emlKey, size: 1, uploaded: "2026-10-25T08:00:00+00:00", customMetadata: {} }],
      statuses: [{ key: `issue-status/${SHA}.json`, customMetadata: { source: emlKey, state: "done" } }],
      retries: { waiting: 0, recent: [{ applied_at: "2026-10-29T10:00:00+00:00", scope: "urls", kind: "page",
        status: "failed", rescheduled: 3, left: 1 }] },
      urls: { byKind: { page: { ok: 2, failed: 4 }, direct: { retrying: 1 } }, wayback: 0, lastUpdated: null },
    }));
    await archive.put(`${RETRY_PREFIX}20261029T100500Z-0123456789abcdef.json`, "{}");
    const body = await (await get(env, "/captures")).text();
    expect(body).toContain("1 retry request waiting");
    expect(body).toContain("3 of 4 URLs due now");
    // one per retryable cell, one per processed message
    expect(body.match(/action="\/captures\/retry"/g)).toHaveLength(3);
    expect(body).toContain(`name="sha" value="${SHA}"`);
  });
});
