// @vitest-environment jsdom
import { beforeEach, describe, expect, it } from "vitest";
import { fillAddress, readPage } from "../src/page";

// jsdom does no layout: treat every element as visible unless hidden
beforeEach(() => {
  Object.defineProperty(HTMLElement.prototype, "offsetParent", {
    configurable: true,
    get(this: HTMLElement) {
      return this.hidden || this.style.display === "none" ? null : document.body;
    },
  });
});

describe("readPage", () => {
  it("reads the title and site name", () => {
    document.head.innerHTML = '<title> Join us </title><meta property="og:site_name" content=" Ramen Weekly ">';
    expect(readPage()).toMatchObject({ title: "Join us", siteName: "Ramen Weekly" });
  });
});

describe("fillAddress", () => {
  it("fills the focused field, telling the page", () => {
    document.body.innerHTML = '<input id="a" type="email"><input id="b" type="text">';
    const b = document.getElementById("b") as HTMLInputElement;
    const seen: string[] = [];
    b.addEventListener("input", () => seen.push("input"));
    b.addEventListener("change", () => seen.push("change"));
    b.focus();
    expect(fillAddress("ann+ramen@mail.example.org", true)).toBe(true);
    expect(b.value).toBe("ann+ramen@mail.example.org");
    expect((document.getElementById("a") as HTMLInputElement).value).toBe("");
    expect(seen).toEqual(["input", "change"]);
  });

  it("else the first visible, editable email field", () => {
    document.body.innerHTML = `
      <input type="email" hidden><input type="email" disabled><input type="email" readonly>
      <input name="EMAIL" id="target"><input type="email">`;
    (document.activeElement as HTMLElement | null)?.blur();
    expect(fillAddress("x@example.org", false)).toBe(true);
    expect((document.getElementById("target") as HTMLInputElement).value).toBe("x@example.org");
  });

  it("from the popup, passes over a focused field that isn't for email", () => {
    document.body.innerHTML = '<input type="search" id="q"><input type="email" id="e">';
    (document.getElementById("q") as HTMLInputElement).focus();
    expect(fillAddress("x@example.org", false)).toBe(true);
    expect((document.getElementById("q") as HTMLInputElement).value).toBe("");
    expect((document.getElementById("e") as HTMLInputElement).value).toBe("x@example.org");
  });

  it("never fills passwords, and says when there's nothing to fill", () => {
    document.body.innerHTML = '<input type="password" id="p">';
    (document.getElementById("p") as HTMLInputElement).focus();
    expect(fillAddress("x@example.org", true)).toBe(false);
    expect((document.getElementById("p") as HTMLInputElement).value).toBe("");
  });
});
