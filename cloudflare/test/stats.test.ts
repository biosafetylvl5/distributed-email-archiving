import { describe, expect, it } from "vitest";
import { handleAdmin, type Authenticator } from "../src/admin/app";
import { summarize, type StoredObject } from "../src/admin/stats";
import { fakeEnv } from "./fakes";

const NOW = new Date("2026-10-26T12:00:00Z");
const allow: Authenticator = async () => ({ ok: true, email: "admin@example.org" });

let n = 0;
function obj(disp: string, day: string, meta: Record<string, string> = {}, size = 100): StoredObject {
  const sha = (n++).toString(16).padStart(64, "0");
  return { key: `incoming/${disp}/${day}/${sha}.eml`, size, uploaded: new Date(`${day}T08:00:00Z`), customMetadata: meta };
}

describe("summarize", () => {
  it("totals, windows, per-tag counts and senders", () => {
    const s = summarize(
      [
        obj("archive", "2026-10-26", { user: "ann", tag: "news", envelope_from: "bounce@Mail.News.Example" }, 1000),
        obj("archive", "2026-10-20", { user: "ann", tag: "news", envelope_from: "b@mail.news.example" }, 500),
        obj("quarantine", "2026-10-01", { user: "ann", tag: "", envelope_from: "x@spam.example" }),
        obj("archive", "2026-01-01", { user: "bob", tag: "old" }),
        { key: "incoming/archive/not-a-day/x.eml", size: 1, uploaded: NOW },
        { key: "elsewhere/2026-10-26/x.eml", size: 1, uploaded: NOW },
      ],
      NOW,
    );
    expect(s.totals).toEqual({ archive: { count: 3, bytes: 1600 }, quarantine: { count: 1, bytes: 100 } });
    expect(s.last7).toEqual({ archive: 2, quarantine: 0 }); // 10-20 .. 10-26
    expect(s.last30).toEqual({ archive: 2, quarantine: 1 }); // from 09-27
    expect(s.oldestQuarantine).toBe("2026-10-01");
    expect(s.days).toHaveLength(14);
    expect(s.days.at(-1)).toEqual({ day: "2026-10-26", archive: 1, quarantine: 0 });
    expect(s.days[0].day).toBe("2026-10-13");
    expect(s.tags.get("ann/news")).toMatchObject({ archive: 2, quarantine: 0, bytes: 1500, last: new Date("2026-10-26T08:00:00Z") });
    expect(s.tags.get("ann/")).toMatchObject({ archive: 0, quarantine: 1 });
    expect(s.senders).toEqual([
      { domain: "mail.news.example", count: 2 },
      { domain: "spam.example", count: 1 },
    ]);
    expect(s.partial).toBe(false);
  });
});

describe("overview page", () => {
  it("shows storage, per-newsletter stats, quiet subscriptions and senders, escaped", async () => {
    const { env, users, tags, archive } = fakeEnv();
    const day = new Date().toISOString().slice(0, 10);
    await users.put("ann", JSON.stringify({ active: true }));
    await tags.put("ann/news", JSON.stringify({ status: "approved", name: "<b>News</b>", sender_domain: "mail.news.example" }));
    await tags.put("ann/dead", JSON.stringify({ status: "approved", name: "Dead Letter" }));
    const meta = { user: "ann", tag: "news", envelope_from: "x@mail.news.example" };
    await archive.put(`incoming/archive/${day}/${"a".repeat(64)}.eml`, "x".repeat(2048), { customMetadata: meta });
    await archive.put(`incoming/quarantine/${day}/${"b".repeat(64)}.eml`, "y", {
      customMetadata: { user: "ann", tag: "mystery", envelope_from: "z@other.example" },
    });

    const body = await (await handleAdmin(new Request("https://admin.example.org/"), env, allow)).text();
    expect(body).toMatch(/<th>Archived<\/th><td>1<\/td><td>2\.0 KiB<\/td><td>1<\/td><td>1<\/td>/);
    expect(body).toContain("&lt;b&gt;News&lt;/b&gt;");
    expect(body).not.toContain("<b>News</b>");
    expect(body).toContain("unknown tag");
    expect(body).toMatch(/1 approved subscription has received nothing in 30 days: <a href="\/users\/ann">Dead Letter<\/a>/);
    expect(body).toMatch(/<code>mail\.news\.example<\/code><\/td><td>1<\/td><td>yes/);
    expect(body).toContain("1 with a sender domain");
  });
});
