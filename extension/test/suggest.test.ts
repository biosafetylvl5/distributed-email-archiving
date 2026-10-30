import { describe, expect, it } from "vitest";
import { isValidTag } from "../../cloudflare/src/policy";
import { isWebPage, suggestName, suggestTag } from "../src/suggest";

const page = (title: string, siteName = "", url = "https://www.ramen.example.com/join") => ({ url, title, siteName });

describe("suggestName", () => {
  it("prefers the site's own name", () => {
    expect(suggestName(page("Sign up | Whatever", "Ramen Weekly"))).toBe("Ramen Weekly");
  });

  it("drops actions and platforms from the title", () => {
    expect(suggestName(page("Subscribe to Ramen Weekly | Substack", "Substack"))).toBe("Ramen Weekly");
    expect(suggestName(page("Newsletter - Ramen Weekly"))).toBe("Ramen Weekly");
    expect(suggestName(page("Sign up for the Noodle Digest — Home"))).toBe("Noodle Digest");
  });

  it("falls back to the site's host", () => {
    expect(suggestName(page("Subscribe"))).toBe("Ramen");
    expect(suggestName(page("", "", "not a url"))).toBe("");
  });

  it("gives tags the API accepts", () => {
    for (const p of [page("Café ✨ Weekly"), page("Subscribe"), page("Ünïcødé")]) {
      expect(isValidTag(suggestTag(suggestName(p), p.url))).toBe(true);
    }
  });
});

describe("isWebPage", () => {
  it("is http(s) only", () => {
    expect(isWebPage("https://example.com/")).toBe(true);
    expect(isWebPage("about:addons")).toBe(false);
    expect(isWebPage("chrome://extensions")).toBe(false);
    expect(isWebPage(undefined)).toBe(false);
  });
});
