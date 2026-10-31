import { describe, expect, it } from "vitest";
import { addressesFor } from "../src/addresses";
import { fakeEnv } from "./fakes";
import {
  decide,
  envelopeFromDomain,
  isValidTag,
  isValidUser,
  parseRecipient,
  recipientDomain,
  type TagRecord,
  type UserRecord,
} from "../src/policy";

const activeUser: UserRecord = { active: true };
const approved: TagRecord = { status: "approved" };

describe("parseRecipient", () => {
  it("parses plus-tagged addresses", () => {
    expect(parseRecipient("john.smith+ramen_weekly@example.org")).toEqual({
      user: "john.smith",
      tag: "ramen_weekly",
      separator: "+",
    });
  });

  it("parses double-underscore fallback addresses (forms that reject '+')", () => {
    expect(parseRecipient("john.smith__ramen_weekly@example.org")).toEqual({
      user: "john.smith",
      tag: "ramen_weekly",
      separator: "__",
    });
  });

  it("parses bare addresses", () => {
    expect(parseRecipient("john.smith@example.org")).toEqual({
      user: "john.smith",
      tag: null,
      separator: null,
    });
  });

  it("normalizes case", () => {
    expect(parseRecipient("John.Smith+News@example.org")?.user).toBe("john.smith");
  });

  it("rejects garbage local parts at the grammar level", () => {
    for (const bad of [
      "@example.org",
      "john smith@example.org",
      "john.smith+@example.org",
      "john.smith+_leading@example.org",
      ".dotfirst@example.org",
      "john.smith+tag+extra@example.org",
      "no-at-sign",
    ]) {
      expect(parseRecipient(bad), bad).toBeNull();
    }
  });
});

describe("subscription address grammar", () => {
  it.each(["a..b", "a.", ".a", "a b", "a@b", "é"])("rejects invalid name %s", (name) => {
    expect(isValidUser(name)).toBe(false);
    expect(isValidTag(name)).toBe(false);
    expect(parseRecipient(`${name}@example.org`)).toBeNull();
    expect(parseRecipient(`ann+${name}@example.org`)).toBeNull();
    expect(() => addressesFor(fakeEnv().env, "ann", name)).toThrow(/invalid/);
  });

  it("rejects trailing line breaks in provisioned names", () => {
    expect(isValidUser("ann\n")).toBe(false);
    expect(isValidTag("news\n")).toBe(false);
    expect(() => addressesFor(fakeEnv().env, "ann", "news\n")).toThrow(/invalid/);
  });

  it("reserves enough local-part bytes for both separators", () => {
    const env = fakeEnv({ ARCHIVE_DOMAIN: "example.org" }).env;
    const user = "u".repeat(30);
    const tag = "t".repeat(32);
    expect(isValidTag(tag, user)).toBe(true);
    const addresses = addressesFor(env, user, tag);
    expect(addresses.underscore.split("@")[0]).toHaveLength(64);
    for (const address of Object.values(addresses)) {
      expect(parseRecipient(address)).toMatchObject({ user, tag });
    }
    expect(isValidTag(`${tag}t`, user)).toBe(false);
    for (const separator of ["+", "__"]) {
      expect(parseRecipient(`${user}${separator}${tag}t@example.org`)).toBeNull();
    }
    expect(() => addressesFor(env, user, `${tag}t`)).toThrow(/invalid/);
    expect(isValidUser("u".repeat(63))).toBe(true);
    expect(isValidTag("t", "u".repeat(63))).toBe(false);
    expect(isValidTag("t".repeat(64))).toBe(false);
  });
});

describe("decide", () => {
  const base = { mode: "strict" as const, envelopeFromDomain: "esp.example" };

  it("rejects unknown users outright", () => {
    const rec = parseRecipient("stranger+x@example.org")!;
    const d = decide({ ...base, rec, userRecord: null, tagRecord: null });
    expect(d.action).toBe("reject");
  });

  it("strict mode rejects unknown tags", () => {
    const rec = parseRecipient("john.smith+unknown@example.org")!;
    const d = decide({ ...base, rec, userRecord: activeUser, tagRecord: null });
    expect(d.action).toBe("reject");
  });

  it("quarantine mode captures unknown tags into quarantine", () => {
    const rec = parseRecipient("john.smith+unknown@example.org")!;
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
    const rec = parseRecipient("john.smith+ramen_weekly@example.org")!;
    const d = decide({ ...base, rec, userRecord: activeUser, tagRecord: approved });
    expect(d).toMatchObject({ action: "accept", disposition: "archive", tag: "ramen_weekly" });
  });

  it("sender binding quarantines mismatched envelope domains", () => {
    const rec = parseRecipient("john.smith+ramen_weekly@example.org")!;
    const d = decide({
      ...base,
      rec,
      userRecord: activeUser,
      tagRecord: { status: "approved", sender_domain: "mail.ramenweekly.example" },
      envelopeFromDomain: "evil.example",
    });
    expect(d).toMatchObject({ action: "accept", disposition: "quarantine" });
  });

  it.each([null, "other.example.org"])("quarantines a bound tag with sender %s", (sender) => {
    expect(decide({
      ...base,
      rec: parseRecipient("ann+news@example.org")!,
      userRecord: activeUser,
      tagRecord: { status: "approved", sender_domain: "mail.example.org" },
      envelopeFromDomain: sender,
    })).toMatchObject({ action: "accept", disposition: "quarantine" });
  });

  it("normalizes stored sender bindings before comparison", () => {
    expect(decide({
      ...base,
      rec: parseRecipient("ann+news@example.org")!,
      userRecord: activeUser,
      tagRecord: { status: "approved", sender_domain: " MAIL.EXAMPLE.ORG " },
      envelopeFromDomain: envelopeFromDomain("news@mail.example.org"),
    })).toMatchObject({ action: "accept", disposition: "archive" });
  });

  it("bare addresses quarantine (capture double-opt-in mail, capped)", () => {
    const rec = parseRecipient("john.smith@example.org")!;
    const d = decide({ ...base, rec, userRecord: activeUser, tagRecord: null });
    expect(d).toMatchObject({ action: "accept", disposition: "quarantine", tag: null });
  });

  it("blocked tags reject", () => {
    const rec = parseRecipient("john.smith+spammy@example.org")!;
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
