import { describe, expect, it } from "vitest";
import handler from "../src/index";
import { fakeEnv, type FakeEnv } from "./fakes";

function message(to: string, raw = "Subject: hi\r\n\r\nbody", from = "news@esp.example") {
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

async function deliver(f: FakeEnv, to: string) {
  const m = message(to);
  await handler.email(m as unknown as ForwardableEmailMessage, f.env, {} as ExecutionContext);
  return m;
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
