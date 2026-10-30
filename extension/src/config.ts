// settings, kept in storage.local (not sync: the token stays on this browser)

import { ext } from "./ext";

export type AddressForm = "plus" | "underscore";

export interface Config {
  apiUrl: string; // e.g. https://mail-api.example.org
  token: string;
  form: AddressForm; // which address to copy and fill by default
}

// filled in at build time from API_URL (see build.mjs); may be ""
declare const __DEFAULT_API_URL__: string;

export async function loadConfig(): Promise<Config> {
  const got = (await ext.storage.local.get(["apiUrl", "token", "form"])) as Partial<Config>;
  return {
    apiUrl: got.apiUrl || __DEFAULT_API_URL__,
    token: got.token ?? "",
    form: got.form === "underscore" ? "underscore" : "plus",
  };
}

export async function saveConfig(c: Config): Promise<void> {
  await ext.storage.local.set(c);
}

// the API's origin, for the host permission ("https://host/*"), or null
export function originPattern(apiUrl: string): string | null {
  try {
    const u = new URL(apiUrl);
    if (u.protocol !== "https:" && !(u.protocol === "http:" && u.hostname === "localhost")) return null;
    return `${u.protocol}//${u.hostname}/*`;
  } catch {
    return null;
  }
}

// the extension can reach the API (Firefox treats host permissions as
// optional, so the user may not have granted it at install)
export async function hasApiPermission(apiUrl: string): Promise<boolean> {
  const origin = originPattern(apiUrl);
  return origin !== null && (await ext.permissions.contains({ origins: [origin] }));
}
