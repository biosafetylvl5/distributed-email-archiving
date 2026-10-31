import { describe, expect, it, vi } from "vitest";
import handler from "../src/index";
import { fakeEnv, type FakeEnv } from "./fakes";

function message(to: string, raw = "Subject: hi\r\n\r\nbody", from = "news@example.org") {
  const m = {
    to,
    from,
    rawSize: raw.length,
    raw: new Blob([raw]).stream(),
    rejected: null as string | null,
    forwarded: [] as string[],
    setReject(reason: string) {
      m.rejected = reason;
    },
    async forward(addr: string) {
      m.forwarded.push(addr);
    },
  };
  return m;
}

async function send(f: FakeEnv, m: ReturnType<typeof message>) {
  await handler.email(m as unknown as ForwardableEmailMessage, f.env, {} as ExecutionContext);
  return m;
}

async function deliver(f: FakeEnv, to: string) {
  return send(f, message(to));
}

function unreadMessage() {
  const m = message("ann+news@mail.example.org");
  const read = vi.fn(() => { throw new Error("body must not be read"); });
  Object.defineProperty(m, "raw", { get: read });
  return { m, read };
}

function setup(vars = {}) {
  const f = fakeEnv(vars);
  f.users.data.set("ann", JSON.stringify({ active: true }));
  f.tags.data.set("ann/news", JSON.stringify({ status: "approved" }));
  return f;
}

const stored = (f: FakeEnv) => [...f.archive.objects.keys()];

describe("email()", () => {
  it("archives approved tags and forwards a backup", async () => {
    const f = setup();
    const m = await deliver(f, "ann+news@mail.example.org");
    expect(m.rejected).toBeNull();
    expect(stored(f)).toHaveLength(1);
    expect(stored(f)[0]).toMatch(/^incoming\/archive\/\d{4}-\d{2}-\d{2}\/[0-9a-f]{64}\.eml$/);
    expect(m.forwarded).toEqual(["backup@example.net"]);
  });

  it("stores the exact bytes, digest and metadata before forwarding", async () => {
    const f = setup();
    const m = message("ann+news@mail.example.org");
    const put = vi.spyOn(f.archive, "put");
    const forward = vi.spyOn(m, "forward").mockImplementation(async () => {
      expect(stored(f)).toHaveLength(1);
    });
    await send(f, m);
    const key = stored(f)[0];
    expect(key).toMatch(/\/6059e5b0d341c20e5980acdd38fff46d347a20adf375af4e3db52661e30c3821\.eml$/);
    const object = f.archive.objects.get(key)!;
    expect(object.bytes).toEqual(new TextEncoder().encode("Subject: hi\r\n\r\nbody"));
    expect(object.httpMetadata).toEqual({ contentType: "message/rfc822" });
    expect(object.customMetadata).toEqual({
      user: "ann", tag: "news", envelope_from: m.from, rcpt_to: m.to,
      received_at: expect.stringMatching(/^\d{4}-\d{2}-\d{2}T/), ingest_source: "live",
    });
    expect(put.mock.invocationCallOrder[0]).toBeLessThan(forward.mock.invocationCallOrder[0]);
  });

  it("defers on authoritative storage failure without forwarding or rejecting", async () => {
    const f = setup();
    const m = message("ann+news@mail.example.org");
    vi.spyOn(f.archive, "put").mockRejectedValue(new Error("R2 unavailable"));
    await expect(send(f, m)).rejects.toThrow("R2 unavailable");
    expect(m.forwarded).toEqual([]);
    expect(m.rejected).toBeNull();
    expect(stored(f)).toEqual([]);
  });

  it("keeps stored bytes and succeeds when backup forwarding fails", async () => {
    const f = setup();
    const m = message("ann+news@mail.example.org");
    vi.spyOn(m, "forward").mockRejectedValue(new Error("backup unavailable"));
    await expect(send(f, m)).resolves.toBe(m);
    expect(f.archive.objects.get(stored(f)[0])?.bytes)
      .toEqual(new TextEncoder().encode("Subject: hi\r\n\r\nbody"));
    expect(m.rejected).toBeNull();
  });

  it.each([undefined, "", "   "])("defers before reading when ARCHIVE_DOMAIN is %s", async (domain) => {
    const f = setup({ ARCHIVE_DOMAIN: domain });
    const { m, read } = unreadMessage();
    await expect(send(f, m)).rejects.toThrow(/ARCHIVE_DOMAIN/);
    expect(read).not.toHaveBeenCalled();
    expect(m.rejected).toBeNull();
    expect(stored(f)).toEqual([]);
    expect(f.ratelimit.data.size).toBe(0);
  });

  it("normalizes the configured archive domain", async () => {
    const f = setup({ ARCHIVE_DOMAIN: " MAIL.EXAMPLE.ORG " });
    expect((await deliver(f, "ann__news@mail.example.org")).rejected).toBeNull();
  });

  it.each(["policy", "size", "domain"])("does not read the body on a %s refusal", async (gate) => {
    const f = setup();
    const { m, read } = unreadMessage();
    if (gate === "policy") f.tags.data.clear();
    if (gate === "size") m.rawSize = 100 * 1024 * 1024;
    if (gate === "domain") m.to = "ann+news@other.example.org";
    await send(f, m);
    expect(m.rejected).not.toBeNull();
    expect(read).not.toHaveBeenCalled();
    expect(m.forwarded).toEqual([]);
    expect(stored(f)).toEqual([]);
  });

  it.each(["global", "tag:ann/news", "untagged:ann"])("checks all caps before charging: %s", async (scope) => {
    const f = setup({ GLOBAL_DAILY_CAP: "1", TAG_DAILY_CAP: "1", UNTAGGED_DAILY_CAP: "1" });
    const day = new Date().toISOString().slice(0, 10);
    f.ratelimit.data.set(`rl:${scope}:${day}`, "1");
    const before = [...f.ratelimit.data];
    const { m, read } = unreadMessage();
    if (scope === "untagged:ann") m.to = "ann@mail.example.org";
    await expect(send(f, m)).rejects.toThrow(/daily cap/);
    expect([...f.ratelimit.data]).toEqual(before);
    expect(read).not.toHaveBeenCalled();
    expect(m.rejected).toBeNull();
    expect(m.forwarded).toEqual([]);
  });

  it("does not exhaust the global cap with attempts at an exhausted tag", async () => {
    const f = setup({ GLOBAL_DAILY_CAP: "2", TAG_DAILY_CAP: "1" });
    await deliver(f, "ann+news@mail.example.org");
    for (let i = 0; i < 2; i++) {
      await expect(deliver(f, "ann+news@mail.example.org")).rejects.toThrow(/recipient daily cap/);
    }
    f.tags.data.set("ann/other", JSON.stringify({ status: "approved" }));
    expect((await deliver(f, "ann+other@mail.example.org")).rejected).toBeNull();
  });

  it("falls back to config when SETTINGS contains malformed JSON", async () => {
    const f = setup({ POLICY_MODE: "quarantine" });
    f.settings.data.set("settings", "{");
    expect((await deliver(f, "ann+unknown@mail.example.org")).rejected).toBeNull();
    expect(stored(f)[0]).toMatch(/^incoming\/quarantine\//);
  });

  it("quarantines and backs up bound mail with an empty sender", async () => {
    const f = setup();
    f.tags.data.set("ann/news", JSON.stringify({ status: "approved", sender_domain: "example.org" }));
    const m = message("ann+news@mail.example.org", undefined, "");
    await send(f, m);
    expect(stored(f)[0]).toMatch(/^incoming\/quarantine\//);
    expect(m.forwarded).toEqual([f.env.BACKUP_MAILBOX]);
  });

  it("rejects other domains", async () => {
    const f = setup();
    expect((await deliver(f, "ann+news@example.org")).rejected).toBe("no such recipient");
    expect(stored(f)).toHaveLength(0);
  });

  it("applies a KV policy_mode override over wrangler.toml", async () => {
    const f = setup({ POLICY_MODE: "strict" });
    expect((await deliver(f, "ann+unknown@mail.example.org")).rejected).toBe("no such recipient");
    f.settings.data.set("settings", JSON.stringify({ policy_mode: "quarantine" }));
    expect((await deliver(f, "ann+unknown@mail.example.org")).rejected).toBeNull();
    expect(stored(f)[0]).toMatch(/^incoming\/quarantine\//);
  });

  it("treats a mistyped POLICY_MODE as strict", async () => {
    const f = setup({ POLICY_MODE: "Quarantined" });
    expect((await deliver(f, "ann+unknown@mail.example.org")).rejected).toBe("no such recipient");
  });

  it("applies cap and size overrides", async () => {
    const f = setup();
    f.settings.data.set("settings", JSON.stringify({ tag_daily_cap: 1 }));
    await deliver(f, "ann+news@mail.example.org");
    await expect(deliver(f, "ann+news@mail.example.org")).rejects.toThrow(/daily cap/);

    const g = setup();
    g.settings.data.set("settings", JSON.stringify({ max_raw_bytes: 5 }));
    expect((await deliver(g, "ann+news@mail.example.org")).rejected).toBe("message too large");
  });
});
