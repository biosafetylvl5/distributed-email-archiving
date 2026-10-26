import { describe, expect, it } from "vitest";
import { isValidTag, isValidUser } from "../src/policy";
import {
  DEFAULTS,
  loadOverrides,
  parseCount,
  parseMode,
  resolveSettings,
  sanitizeOverrides,
} from "../src/settings";
import { FakeKV } from "./fakes";

describe("parseMode", () => {
  it("accepts known modes case-insensitively", () => {
    expect(parseMode("strict")).toBe("strict");
    expect(parseMode(" Quarantine ")).toBe("quarantine");
  });
  it("rejects anything else", () => {
    for (const bad of ["", "stict", "off", 1, null, undefined]) expect(parseMode(bad)).toBeUndefined();
  });
});

describe("parseCount", () => {
  it("accepts positive integers as numbers or strings", () => {
    expect(parseCount(50)).toBe(50);
    expect(parseCount(" 26214400 ")).toBe(26214400);
  });
  it("rejects zero, negatives, fractions and junk", () => {
    for (const bad of [0, -1, 1.5, "0", "12abc", "1e3", "", null]) expect(parseCount(bad)).toBeUndefined();
  });
});

describe("resolveSettings", () => {
  it("uses built-in defaults when nothing is set", () => {
    const { settings, source } = resolveSettings({}, {});
    expect(settings).toEqual(DEFAULTS);
    expect(source.policy_mode).toBe("default");
  });

  it("prefers overrides, then wrangler vars, then defaults", () => {
    const { settings, source } = resolveSettings(
      { POLICY_MODE: "quarantine", TAG_DAILY_CAP: "20", GLOBAL_DAILY_CAP: "300" },
      { tag_daily_cap: 5 },
    );
    expect(settings.tag_daily_cap).toBe(5);
    expect(source.tag_daily_cap).toBe("override");
    expect(settings.global_daily_cap).toBe(300);
    expect(source.global_daily_cap).toBe("config");
    expect(settings.policy_mode).toBe("quarantine");
    expect(settings.untagged_daily_cap).toBe(DEFAULTS.untagged_daily_cap);
  });

  it("falls back to strict on a mistyped POLICY_MODE instead of failing open", () => {
    expect(resolveSettings({ POLICY_MODE: "stirct" }, {}).settings.policy_mode).toBe("strict");
  });

  it("ignores invalid override values", () => {
    const { settings } = resolveSettings(
      { POLICY_MODE: "quarantine" },
      { policy_mode: "nope" as never, tag_daily_cap: -3 },
    );
    expect(settings.policy_mode).toBe("quarantine");
    expect(settings.tag_daily_cap).toBe(DEFAULTS.tag_daily_cap);
  });
});

describe("sanitizeOverrides / loadOverrides", () => {
  it("drops unknown and invalid fields", () => {
    expect(sanitizeOverrides({ policy_mode: "strict", tag_daily_cap: "7", junk: 1, max_raw_bytes: 0 })).toEqual({
      policy_mode: "strict",
      tag_daily_cap: 7,
    });
    expect(sanitizeOverrides(null)).toEqual({});
    expect(sanitizeOverrides("strict")).toEqual({});
  });

  it("returns no overrides without a SETTINGS binding", async () => {
    expect(await loadOverrides(undefined)).toEqual({});
  });

  it("reads the settings key from KV", async () => {
    const kv = new FakeKV({ settings: { global_daily_cap: 42 } });
    expect(await loadOverrides(kv as unknown as KVNamespace)).toEqual({ global_daily_cap: 42 });
  });
});

describe("name validators", () => {
  it("match the recipient grammar", () => {
    expect(isValidUser("john.smith")).toBe(true);
    expect(isValidUser("john_smith")).toBe(false); // "_" would collide with the "__" separator
    expect(isValidUser("John")).toBe(false);
    expect(isValidUser(".x")).toBe(false);
    expect(isValidTag("ramen_weekly")).toBe(true);
    expect(isValidTag("_x")).toBe(false);
    expect(isValidTag("a/b")).toBe(false);
  });
});
