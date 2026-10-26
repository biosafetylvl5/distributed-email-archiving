import { describe, expect, it } from "vitest";
import { decodeEncodedWords, summarizeHeaders } from "../src/admin/mail";

const enc = (s: string) => new TextEncoder().encode(s);

describe("decodeEncodedWords", () => {
  it("decodes B and Q words", () => {
    expect(decodeEncodedWords("=?UTF-8?B?UmFtZW4g8J+NnA==?=")).toBe("Ramen 🍜");
    expect(decodeEncodedWords("=?iso-8859-1?Q?Caf=E9_news?=")).toBe("Café news");
  });
  it("joins adjacent words and keeps surrounding text", () => {
    expect(decodeEncodedWords("Issue =?utf-8?q?n=C2=BA?= =?utf-8?q?42?= out")).toBe("Issue nº42 out");
  });
  it("leaves undecodable words alone", () => {
    expect(decodeEncodedWords("=?x-unknown?q?hi?=")).toBe("=?x-unknown?q?hi?=");
  });
});

describe("summarizeHeaders", () => {
  it("extracts unfolded From, Subject and Date and ignores the body", () => {
    const raw = enc(
      [
        "Received: from x",
        "From: Ramen Weekly <news@mail.ramenweekly.example>",
        "Subject: Issue 42:",
        "  =?utf-8?q?the_broth_issue?=",
        "Date: Mon, 26 Oct 2026 08:00:00 +0000",
        "",
        "Subject: not a header",
      ].join("\r\n"),
    );
    expect(summarizeHeaders(raw)).toEqual({
      from: "Ramen Weekly <news@mail.ramenweekly.example>",
      subject: "Issue 42: the broth issue",
      date: "Mon, 26 Oct 2026 08:00:00 +0000",
    });
  });
  it("returns empty strings for missing headers", () => {
    expect(summarizeHeaders(enc("X-Foo: bar\n\nbody"))).toEqual({ from: "", subject: "", date: "" });
  });
});
