// steps shared by the popup and the context menu

import { Api, type Subscription } from "./api";
import type { AddressForm } from "./config";
import { ext } from "./ext";
import { fillAddress, readPage, type PageInfo } from "./page";
import { suggestName } from "./suggest";

export function addressOf(sub: Subscription, form: AddressForm): string {
  return form === "underscore" ? sub.addresses.underscore : sub.addresses.plus;
}

export async function pageInfo(tab: chrome.tabs.Tab): Promise<PageInfo> {
  const fallback = { url: tab.url ?? "", title: tab.title ?? "", siteName: "" };
  if (tab.id === undefined) return fallback;
  try {
    const [res] = await ext.scripting.executeScript({ target: { tabId: tab.id }, func: readPage });
    return (res?.result as PageInfo | undefined) ?? fallback;
  } catch {
    return fallback; // e.g. the browser's own pages, or the web store
  }
}

// into the field right-clicked in that frame, or (no frame) the page's email field
export async function fillInto(tabId: number, frameId: number | undefined, address: string): Promise<boolean> {
  const [res] = await ext.scripting.executeScript({
    target: frameId === undefined ? { tabId } : { tabId, frameIds: [frameId] },
    func: fillAddress,
    args: [address, frameId !== undefined],
  });
  return res?.result === true;
}

// the page without its fragment: what's kept as the sign-up link
export function signupUrlOf(url: string): string {
  const u = new URL(url);
  u.hash = "";
  return u.toString();
}

// the user's subscription for this page: one signed up on this very page,
// or the only one on this site; null when it's ambiguous or there's none
export async function existingFor(api: Api, url: string): Promise<Subscription | null> {
  const { exact, same_site } = await api.lookup(url);
  if (exact.length) return exact[0];
  return same_site.length === 1 ? same_site[0] : null;
}

export async function findOrCreate(api: Api, page: PageInfo): Promise<{ sub: Subscription; created: boolean }> {
  const found = await existingFor(api, page.url);
  if (found) return { sub: found, created: false };
  const sub = await api.create({ name: suggestName(page) || null, signup_url: signupUrlOf(page.url) });
  return { sub, created: true };
}
