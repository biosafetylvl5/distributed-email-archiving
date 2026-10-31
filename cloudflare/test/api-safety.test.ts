import { describe, expect, it } from "vitest";
import { handleApi } from "../src/api/app";
import { createToken } from "../src/api/tokens";
import { fakeEnv } from "./fakes";

const API = "https://mail-api.example.org";
const secretUrl = "https://reader%40example.com:SECRET@example.org/join?token=SECRET&email=reader%40example.com#SECRET";

async function seeded() {
  const f = fakeEnv();
  await f.users.put("ann", JSON.stringify({ active: true }));
  await f.tags.put("ann/news", JSON.stringify({ status: "approved", name: "News", signup_url: secretUrl }));
  const { token } = await createToken(f.env, "ann", "Browser", "admin@example.org");
  const call = (path: string, init: RequestInit = {}) => handleApi(new Request(API + path, {
    ...init,
    headers: { authorization: `Bearer ${token}`, "content-type": "application/json", ...init.headers },
  }), f.env);
  const send = (method: string, path: string, body: unknown) => call(path, { method, body: JSON.stringify(body) });
  return { ...f, call, send };
}

describe("signup URL privacy", () => {
  it("normalizes URLs on create and update before storage and response", async () => {
    const { tags, send } = await seeded();
    for (const [method, path, fields, key] of [
      ["POST", "/v1/subscriptions", { tag: "new", signup_url: secretUrl }, "ann/new"],
      ["PATCH", "/v1/subscriptions/news", { signup_url: secretUrl }, "ann/news"],
    ] as const) {
      const response = await send(method, path, fields);
      expect(response.status).toBe(method === "POST" ? 201 : 200);
      const text = await response.text();
      expect(text).toContain('"signup_url":"https://example.org/join"');
      expect(text).not.toMatch(/SECRET|reader/);
      expect(tags.json(key)).toMatchObject({ signup_url: "https://example.org/join" });
    }
  });

  it("cleans existing records on every API projection, including conflict responses", async () => {
    const { call, send, tags } = await seeded();
    for (const path of [
      "/v1/subscriptions", "/v1/subscriptions/news",
      "/v1/subscriptions/lookup?url=" + encodeURIComponent("https://example.org/join"),
    ]) {
      const response = await call(path);
      expect(response.status).toBe(200);
      expect(await response.text()).not.toMatch(/SECRET|reader/);
    }
    const conflict = await send("PATCH", "/v1/subscriptions/news", { if_updated_at: "stale", name: "New" });
    expect(conflict.status).toBe(409);
    expect(await conflict.text()).not.toMatch(/SECRET|reader/);
    // Projection doesn't silently rewrite KV.
    expect(tags.json("ann/news")).toMatchObject({ signup_url: secretUrl });
  });
});

describe("other subscribers' names", () => {
  it("keeps benign names but never exposes raw tags, mailboxes or URLs", async () => {
    const { tags, call } = await seeded();
    const names = [undefined, "", "Weekly for reader@example.com", "Read https://example.com/SECRET", "www.example.org/SECRET", "example.org/SECRET", "Ramen Weekly"];
    for (const [i, name] of names.entries()) {
      await tags.put(`bob/private-${i}`, JSON.stringify({ status: "approved", name, signup_url: "https://example.org/join" }));
    }
    const response = await call("/v1/subscriptions/lookup?url=" + encodeURIComponent("https://example.org/join"));
    const body = await response.json() as { others: { name: string }[] };
    expect(body.others.map((o) => o.name).sort()).toEqual(["Another subscription", "Ramen Weekly"]);
    expect(JSON.stringify(body)).not.toMatch(/private-|reader|SECRET|bob/);
  });
});

describe("pre-write subscription rechecks", () => {
  it.each([true, false])("rechecks existence just before explicit=%s creation", async (explicit) => {
    const { tags, ratelimit, send } = await seeded();
    const get = tags.get.bind(tags);
    let reads = 0;
    tags.get = async (key, opts) => {
      if (key === "ann/new" && ++reads === 2) {
        await tags.put(key, JSON.stringify({ status: "blocked", name: "Keep", note: "admin" }));
      }
      return get(key, opts);
    };
    const response = await send("POST", "/v1/subscriptions", explicit ? { tag: "new" } : { name: "New" });
    expect(response.status).toBe(409);
    expect(tags.json("ann/new")).toEqual({ status: "blocked", name: "Keep", note: "admin" });
    expect(ratelimit.data.size).toBe(0);
  });

  it("rechecks the edit version after the quota read and before writing", async () => {
    const { tags, ratelimit, send } = await seeded();
    const get = ratelimit.get.bind(ratelimit);
    let changed = false;
    ratelimit.get = async (key, opts) => {
      if (!changed && key.startsWith("api:edit:")) {
        changed = true;
        await tags.put("ann/news", JSON.stringify({ status: "blocked", name: "Keep", updated_at: "2026-10-31T00:00:00Z" }));
      }
      return get(key, opts);
    };
    const response = await send("PATCH", "/v1/subscriptions/news", { if_updated_at: null, name: "Overwrite" });
    expect(response.status).toBe(409);
    expect(tags.json("ann/news")).toMatchObject({ status: "blocked", name: "Keep" });
    expect(ratelimit.data.size).toBe(0);
  });

  it("preserves fresh admin fields even without a changed timestamp", async () => {
    const { tags, ratelimit, send } = await seeded();
    const get = ratelimit.get.bind(ratelimit);
    let changed = false;
    ratelimit.get = async (key, opts) => {
      if (!changed && key.startsWith("api:edit:")) {
        changed = true;
        await tags.put("ann/news", JSON.stringify({ status: "blocked", name: "News", note: "Keep", extra: true }));
      }
      return get(key, opts);
    };
    const response = await send("PATCH", "/v1/subscriptions/news", { if_updated_at: null, name: "Edited" });
    expect(response.status).toBe(200);
    expect(tags.json("ann/news")).toMatchObject({ status: "blocked", name: "Edited", note: "Keep", extra: true });
  });
});

describe("bounded request bodies", () => {
  it("counts UTF-8 bytes rather than string characters", async () => {
    const { send, tags } = await seeded();
    const response = await send("POST", "/v1/subscriptions", { name: "😀".repeat(5000) });
    expect(response.status).toBe(413);
    expect(tags.data.size).toBe(1);
  });

  it("rejects an excessive Content-Length without reading the body", async () => {
    const { call } = await seeded();
    let read = false;
    const body = new ReadableStream<Uint8Array>({ pull() { read = true; throw new Error("must not read"); } }, { highWaterMark: 0 });
    const response = await call("/v1/subscriptions", {
      method: "POST", body, headers: { "content-length": "16385" }, duplex: "half",
    } as RequestInit);
    expect(response.status).toBe(413);
    expect(read).toBe(false);
  });

  it.each([undefined, "1"])("cancels overflowing streams with Content-Length %s", async (length) => {
    const { call } = await seeded();
    let pulls = 0;
    let cancelled = false;
    const body = new ReadableStream<Uint8Array>({
      pull(controller) {
        pulls++;
        if (pulls <= 3) controller.enqueue(new Uint8Array(8192).fill(32));
        else controller.close();
      },
      cancel() { cancelled = true; },
    }, { highWaterMark: 0 });
    const headers = length ? { "content-length": length } : {};
    const response = await call("/v1/subscriptions", { method: "POST", body, headers, duplex: "half" } as RequestInit);
    expect(response.status).toBe(413);
    expect(cancelled).toBe(true);
    expect(pulls).toBe(3);
  });

  it("accepts exactly 16 KiB of UTF-8 JSON", async () => {
    const { call } = await seeded();
    const body = JSON.stringify({ tag: "new" }).padEnd(16384, " ");
    const response = await call("/v1/subscriptions", { method: "POST", body });
    expect(response.status).toBe(201);
  });
});
