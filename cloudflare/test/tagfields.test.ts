import { describe, expect, it } from "vitest";
import { isValidTag } from "../src/policy";
import { hostSlug, siteKey, slugify, suggestTag } from "../src/tagfields";

describe("tag suggestions", () => {
  it("slugifies names", () => {
    expect(slugify("  Café & Croissant Weekly! ")).toBe("cafe-and-croissant-weekly");
    expect(slugify("---")).toBe("");
    expect(slugify("x".repeat(80))).toHaveLength(40);
    expect(slugify("a".repeat(39) + " b")).toBe("a".repeat(39));
  });

  it("names a site by the meaningful part of its host", () => {
    expect(hostSlug("www.ramenweekly.example")).toBe("ramenweekly");
    expect(hostSlug("ramen.substack.com")).toBe("ramen");
    expect(hostSlug("news.example.co.uk")).toBe("example");
    expect(hostSlug("newsletter.mail.example.org")).toBe("example");
    expect(hostSlug("localhost")).toBe("localhost");
  });

  it("prefers the name, then the sign-up link, and always gives a valid tag or nothing", () => {
    expect(suggestTag("Ramen Weekly", "https://x.example.com")).toBe("ramen-weekly");
    expect(suggestTag("", "https://www.ramen.example.com/join")).toBe("ramen");
    expect(suggestTag(null, "javascript:alert(1)")).toBe("");
    for (const t of [suggestTag("Ünïcødé ✨", null), suggestTag("", "https://xn--bcher-kva.example/")]) {
      expect(isValidTag(t)).toBe(true);
    }
  });

  it("keys sign-up pages by host and path", () => {
    expect(siteKey("https://WWW.Ramen.example.com/join/?a=1#x")).toEqual({
      host: "ramen.example.com",
      page: "ramen.example.com/join",
    });
    expect(siteKey("mailto:a@example.com")).toBeNull();
  });
});
