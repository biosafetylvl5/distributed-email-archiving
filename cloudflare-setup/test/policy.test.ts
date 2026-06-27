import { describe, expect, it } from "vitest";
import {
  decide,
  envelopeFromDomain,
  parseRecipient,
  recipientDomain,
  type TagRecord,
  type UserRecord,
} from "../src/policy";

const activeUser: UserRecord = { active: true };
const approved: TagRecord = { status: "approved" };

describe("parseRecipient", () => {
  it("parses plus-tagged addresses", () => {
    expect(parseRecipient("john.smith+ramen_weekly@customdomain.com")).toEqual({
      user: "john.smith",
      tag: "ramen_weekly",
      separator: "+",
    });
  });

  it("parses double-underscore fallback addresses (forms that reject '+')", () => {
    expect(parseRecipient("john.smith__ramen_weekly@customdomain.com")).toEqual({
      user: "john.smith",
      tag: "ramen_weekly",
      separator: "__",
    });
  });

  it("parses bare addresses", () => {
    expect(parseRecipient("john.smith@customdomain.com")).toEqual({
      user: "john.smith",
      tag: null,
      separator: null,
    });
  });

  it("normalizes case", () => {
    expect(parseRecipient("John.Smith+News@customdomain.com")?.user).toBe("john.smith");
  });

  it("rejects garbage local parts at the grammar level", () => {
    for (const bad of [
      "@customdomain.com",
      "john smith@customdomain.com",
      "john.smith+@customdomain.com",
      "john.smith+_leading@customdomain.com",
      ".dotfirst@customdomain.com",
      "john.smith+tag+extra@customdomain.com",
      "no-at-sign",
    ]) {
      expect(parseRecipient(bad), bad).toBeNull();
    }
  });
});

describe("decide", () => {
  const base = { mode: "strict" as const, envelopeFromDomain: "esp.example" };

  it("rejects unknown users outright", () => {
    const rec = parseRecipient("stranger+x@d.com")!;
    const d = decide({ ...base, rec, userRecord: null, tagRecord: null });
    expect(d.action).toBe("reject");
  });

  it("strict mode rejects unknown tags", () => {
    const rec = parseRecipient("john.smith+unknown@d.com")!;
    const d = decide({ ...base, rec, userRecord: activeUser, tagRecord: null });
    expect(d.action).toBe("reject");
  });

  it("quarantine mode captures unknown tags into quarantine", () => {
    const rec = parseRecipient("john.smith+unknown@d.com")!;
    const d = decide({
      ...base,
      mode: "quarantine",
      rec,
      userRecord: activeUser,
      tagRecord: null,
    });
    expect(d).toMatchObject({ action: "accept", disposition: "quarantine" });
  });

  it("approved tags archive", () => {
    const rec = parseRecipient("john.smith+ramen_weekly@d.com")!;
    const d = decide({ ...base, rec, userRecord: activeUser, tagRecord: approved });
    expect(d).toMatchObject({ action: "accept", disposition: "archive", tag: "ramen_weekly" });
  });

  it("sender binding quarantines mismatched envelope domains", () => {
    const rec = parseRecipient("john.smith+ramen_weekly@d.com")!;
    const d = decide({
      ...base,
      rec,
      userRecord: activeUser,
      tagRecord: { status: "approved", sender_domain: "mail.ramenweekly.example" },
      envelopeFromDomain: "evil.example",
    });
    expect(d).toMatchObject({ action: "accept", disposition: "quarantine" });
  });

  it("bare addresses quarantine (capture double-opt-in mail, capped)", () => {
    const rec = parseRecipient("john.smith@d.com")!;
    const d = decide({ ...base, rec, userRecord: activeUser, tagRecord: null });
    expect(d).toMatchObject({ action: "accept", disposition: "quarantine", tag: null });
  });

  it("blocked tags reject", () => {
    const rec = parseRecipient("john.smith+spammy@d.com")!;
    const d = decide({
      ...base,
      rec,
      userRecord: activeUser,
      tagRecord: { status: "blocked" },
    });
    expect(d.action).toBe("reject");
  });
});

describe("recipientDomain", () => {
  it("extracts and lowercases the domain", () => {
    expect(recipientDomain("John.Smith+tag@Mail.Example.ORG")).toBe("mail.example.org");
    expect(recipientDomain("bare@mail.example.org")).toBe("mail.example.org");
  });
  it("returns null without a domain", () => {
    expect(recipientDomain("no-at-sign")).toBeNull();
    expect(recipientDomain("@leading")).toBeNull();
  });
});

describe("envelopeFromDomain", () => {
  it("extracts and lowercases", () => {
    expect(envelopeFromDomain("Bounce@Mail.Example")).toBe("mail.example");
    expect(envelopeFromDomain("nodomain")).toBeNull();
  });
});
