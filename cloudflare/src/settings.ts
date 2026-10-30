// runtime settings, resolved per message:
//   1. override in the SETTINGS KV namespace (key "settings", set from the admin dashboard)
//   2. [vars] in wrangler.toml
//   3. built-in default
// invalid values at any layer are ignored, so a typo falls back to the
// next layer instead of failing open

import type { PolicyMode } from "./policy";

export interface Settings {
  policy_mode: PolicyMode;
  max_raw_bytes: number;
  global_daily_cap: number;
  tag_daily_cap: number;
  untagged_daily_cap: number;
  // subscription API (src/api/): the status of subscriptions users add, and
  // how many each user may add per day and keep in total
  api_new_tag_status: NewTagStatus;
  api_tags_per_day: number;
  api_tags_max: number;
}

export type NewTagStatus = "approved" | "pending";

export type SettingKey = keyof Settings;
export type SettingSource = "override" | "config" | "default";
export type Overrides = Partial<Settings>;

export interface SettingsVars {
  POLICY_MODE?: string;
  MAX_RAW_BYTES?: string;
  GLOBAL_DAILY_CAP?: string;
  TAG_DAILY_CAP?: string;
  UNTAGGED_DAILY_CAP?: string;
  API_NEW_TAG_STATUS?: string;
  API_TAGS_PER_DAY?: string;
  API_TAGS_MAX?: string;
}

export const SETTINGS_KEY = "settings";

export const DEFAULTS: Settings = {
  policy_mode: "strict",
  max_raw_bytes: 25 * 1024 * 1024,
  global_daily_cap: 500,
  tag_daily_cap: 50,
  untagged_daily_cap: 10,
  api_new_tag_status: "approved",
  api_tags_per_day: 10,
  api_tags_max: 200,
};

export const SETTING_KEYS = Object.keys(DEFAULTS) as SettingKey[];

export const VAR_NAMES: Record<SettingKey, keyof SettingsVars> = {
  policy_mode: "POLICY_MODE",
  max_raw_bytes: "MAX_RAW_BYTES",
  global_daily_cap: "GLOBAL_DAILY_CAP",
  tag_daily_cap: "TAG_DAILY_CAP",
  untagged_daily_cap: "UNTAGGED_DAILY_CAP",
  api_new_tag_status: "API_NEW_TAG_STATUS",
  api_tags_per_day: "API_TAGS_PER_DAY",
  api_tags_max: "API_TAGS_MAX",
};

export function parseMode(v: unknown): PolicyMode | undefined {
  const s = typeof v === "string" ? v.trim().toLowerCase() : "";
  return s === "strict" || s === "quarantine" ? s : undefined;
}

// "blocked" isn't offered: a subscription that can never receive mail is no use
export function parseNewTagStatus(v: unknown): NewTagStatus | undefined {
  const s = typeof v === "string" ? v.trim().toLowerCase() : "";
  return s === "approved" || s === "pending" ? s : undefined;
}

// positive integers only
export function parseCount(v: unknown): number | undefined {
  let n = NaN;
  if (typeof v === "number") n = v;
  else if (typeof v === "string" && /^\s*\d+\s*$/.test(v)) n = Number(v);
  return Number.isSafeInteger(n) && n > 0 ? n : undefined;
}

export function parseSetting(key: SettingKey, v: unknown): Settings[SettingKey] | undefined {
  if (key === "policy_mode") return parseMode(v);
  if (key === "api_new_tag_status") return parseNewTagStatus(v);
  return parseCount(v);
}

// keep only valid fields of whatever is stored in KV
export function sanitizeOverrides(raw: unknown): Overrides {
  const out: Record<string, unknown> = {};
  if (!raw || typeof raw !== "object") return out;
  for (const key of SETTING_KEYS) {
    const v = parseSetting(key, (raw as Record<string, unknown>)[key]);
    if (v !== undefined) out[key] = v;
  }
  return out as Overrides;
}

export function resolveSettings(
  vars: SettingsVars,
  overrides: Overrides,
): { settings: Settings; source: Record<SettingKey, SettingSource> } {
  const settings: Record<string, unknown> = {};
  const source = {} as Record<SettingKey, SettingSource>;
  for (const key of SETTING_KEYS) {
    const fromOverride = parseSetting(key, overrides[key]);
    const fromVar = parseSetting(key, vars[VAR_NAMES[key]]);
    if (fromOverride !== undefined) {
      settings[key] = fromOverride;
      source[key] = "override";
    } else if (fromVar !== undefined) {
      settings[key] = fromVar;
      source[key] = "config";
    } else {
      settings[key] = DEFAULTS[key];
      source[key] = "default";
    }
  }
  return { settings: settings as unknown as Settings, source };
}

// the SETTINGS binding is optional so older wrangler.toml files keep working
// KV edge-caches reads for 60s, so dashboard changes take about a minute
export async function loadOverrides(kv: KVNamespace | undefined): Promise<Overrides> {
  if (!kv) return {};
  return sanitizeOverrides(await kv.get(SETTINGS_KEY, { type: "json", cacheTtl: 60 }));
}
