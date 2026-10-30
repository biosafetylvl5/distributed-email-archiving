import { afterEach, describe, expect, it, vi } from "vitest";
import { Api, ApiError, type Subscription } from "../src/api";
import { originPattern } from "../src/config";
import { describeOther, existingFor, findOrCreate, signupUrlOf } from "../src/flow";

const sub = (tag: string, extra: Partial<Subscription> = {}) =>
  ({ tag, status: "approved", addresses: { plus: `ann+${tag}@m.example.org`, underscore: `ann__${tag}@m.example.org` }, ...extra }) as Subscription;

function mockFetch(responses: [number, unknown][]) {
  const calls: { url: string; init: RequestInit }[] = [];
  vi.stubGlobal("fetch", async (url: URL, init: RequestInit) => {
    calls.push({ url: String(url), init });
    const [status, body] = responses.shift() ?? [500, {}];
    return new Response(JSON.stringify(body), { status });
  });
  return calls;
}

afterEach(() => vi.unstubAllGlobals());

const api = () => new Api({ apiUrl: "https://mail-api.example.org", token: "dea_x" });

describe("Api", () => {
  it("sends the token and JSON, without cookies", async () => {
    const calls = mockFetch([[200, { subscription: sub("ramen") }]]);
    await api().update("ramen", { name: "R" }, "2026-10-30T00:00:00Z");
    const { url, init } = calls[0];
    expect(url).toBe("https://mail-api.example.org/v1/subscriptions/ramen");
    expect(init.method).toBe("PATCH");
    expect(init.credentials).toBe("omit");
    expect((init.headers as Record<string, string>).authorization).toBe("Bearer dea_x");
    expect(JSON.parse(init.body as string)).toEqual({ name: "R", if_updated_at: "2026-10-30T00:00:00Z" });
  });

  it("turns error bodies into ApiErrors", async () => {
    mockFetch([[409, { error: "conflict", message: "changed", subscription: sub("ramen") }]]);
    const err = await api().update("ramen", {}, null).catch((e) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(err).toMatchObject({ status: 409, code: "conflict", message: "changed" });
    expect(err.body.subscription.tag).toBe("ramen");
  });

  it("needs configuring first", async () => {
    await expect(new Api({ apiUrl: "", token: "" }).me()).rejects.toMatchObject({ code: "not_configured" });
  });
});

describe("finding or adding a site's subscription", () => {
  const page = { url: "https://ramen.example.com/join#top", title: "Join Ramen Weekly", siteName: "" };

  it("uses one made on this page, or the only one on the site", async () => {
    mockFetch([[200, { exact: [sub("a")], same_site: [sub("b")] }]]);
    expect((await existingFor(api(), page.url))?.tag).toBe("a");
    mockFetch([[200, { exact: [], same_site: [sub("b")] }]]);
    expect((await existingFor(api(), page.url))?.tag).toBe("b");
    mockFetch([[200, { exact: [], same_site: [sub("b"), sub("c")] }]]);
    expect(await existingFor(api(), page.url)).toBeNull();
  });

  it("adds one, named from the page, when there's none", async () => {
    const calls = mockFetch([
      [200, { exact: [], same_site: [] }],
      [201, { subscription: sub("ramen-weekly") }],
    ]);
    const { sub: got, created } = (await findOrCreate(api(), page))!;
    expect(created).toBe(true);
    expect(got.tag).toBe("ramen-weekly");
    expect(JSON.parse(calls[1].init.body as string)).toEqual({ name: "Ramen Weekly", signup_url: "https://ramen.example.com/join" });
  });
});

describe("someone else's subscription for the site", () => {
  const page = { url: "https://ramen.example.com/join", title: "Ramen Weekly", siteName: "" };
  const others = [{ name: "Ramen Weekly", status: "approved", match: "page" }];

  it("is asked about before adding, and declining adds nothing", async () => {
    const calls = mockFetch([[200, { exact: [], same_site: [], others }]]);
    const seen: unknown[] = [];
    const got = await findOrCreate(api(), page, async (o) => (seen.push(...o), false));
    expect(got).toBeNull();
    expect(seen).toEqual(others);
    expect(calls).toHaveLength(1);
  });

  it("adds when confirmed, and isn't asked about when the user has their own", async () => {
    mockFetch([[200, { exact: [], same_site: [], others }], [201, { subscription: sub("ramen") }]]);
    expect((await findOrCreate(api(), page, async () => true))?.created).toBe(true);
    mockFetch([[200, { exact: [sub("mine")], same_site: [], others }]]);
    const ask = vi.fn(async () => false);
    expect((await findOrCreate(api(), page, ask))?.sub.tag).toBe("mine");
    expect(ask).not.toHaveBeenCalled();
  });

  it("is described without its owner", () => {
    expect(describeOther({ name: "Ramen Weekly", status: "approved", match: "page" })).toBe("Ramen Weekly (this page)");
    expect(describeOther({ name: "Broth", status: "blocked", match: "site" })).toBe(
      "Broth (elsewhere on this site, blocked by the admin)",
    );
  });
});

describe("helpers", () => {
  it("drops the fragment from sign-up links", () => {
    expect(signupUrlOf("https://x.example.com/a?b=1#c")).toBe("https://x.example.com/a?b=1");
  });

  it("asks for https API origins only", () => {
    expect(originPattern("https://mail-api.example.org/")).toBe("https://mail-api.example.org/*");
    expect(originPattern("http://mail-api.example.org")).toBeNull();
    expect(originPattern("http://localhost:8787")).toBe("http://localhost/*");
    expect(originPattern("nope")).toBeNull();
  });
});
