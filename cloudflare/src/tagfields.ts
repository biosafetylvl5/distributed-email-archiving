// subscription (tag) field rules shared by the dashboard and the API, so both
// accept and clean the same values. Pure: the extension bundles suggestTag.

export const TAG_STATUSES = ["approved", "pending", "blocked"] as const;
export const HOSTNAME_RE = /^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$/;

export const NAME_MAX = 200;
export const DESCRIPTION_MAX = 2000;
export const NOTE_MAX = 500;

// rendered as a link, so only http(s): never javascript:, data:, etc.
// Also checked on display, since KV can be edited outside the dashboard
export function isSignupUrl(value: string): boolean {
  if (value.length > 2048) return false;
  try {
    const u = new URL(value);
    return (u.protocol === "https:" || u.protocol === "http:") && u.hostname !== "";
  } catch {
    return false;
  }
}

export const cleanName = (v: string): string => v.trim().replace(/\s+/g, " ").slice(0, NAME_MAX);
export const cleanDescription = (v: string): string =>
  v.trim().replace(/\r\n?/g, "\n").slice(0, DESCRIPTION_MAX);
export const cleanNote = (v: string): string => v.trim().slice(0, NOTE_MAX);

// --- tag suggestions

const TAG_MAX = 40; // isValidTag allows 63; leave room for "-2" etc. and keep addresses short

// host labels that say nothing about which newsletter it is
const GENERIC_LABELS = new Set([
  "www", "m", "mail", "email", "news", "newsletter", "newsletters", "subscribe", "join",
  "signup", "go", "get", "lists", "list", "info", "app", "pages", "page", "lp",
]); // prettier-ignore
// second-level labels of two-part public suffixes (example.co.uk)
const SECOND_LEVEL = new Set(["co", "com", "org", "net", "ac", "gov", "edu", "or", "ne"]);

export function slugify(s: string): string {
  return s
    .normalize("NFKD")
    .replace(/[\u0300-\u036f]/g, "")
    .toLowerCase()
    .replace(/&/g, " and ")
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, TAG_MAX)
    .replace(/-+$/, "");
}

// the part of a hostname that names the site: "www.ramenweekly.example.com"
// -> "ramenweekly", "ramen.substack.com" -> "ramen", "news.example.co.uk" -> "example"
export function hostSlug(hostname: string): string {
  const labels = hostname.toLowerCase().replace(/\.$/, "").split(".").filter(Boolean);
  if (labels.length > 1) labels.pop(); // TLD
  if (labels.length > 1 && SECOND_LEVEL.has(labels[labels.length - 1])) labels.pop();
  while (labels.length > 1 && GENERIC_LABELS.has(labels[0])) labels.shift();
  return slugify(labels[0] ?? "");
}

// a tag for a new subscription: from its name, else its sign-up site
export function suggestTag(name?: string | null, signupUrl?: string | null): string {
  const fromName = slugify(name ?? "");
  if (fromName) return fromName;
  if (signupUrl && isSignupUrl(signupUrl)) return hostSlug(new URL(signupUrl).hostname);
  return "";
}

// for matching sign-up pages: host without "www.", path without a trailing
// slash; query and fragment dropped
export function siteKey(url: string): { host: string; page: string } | null {
  if (!isSignupUrl(url)) return null;
  const u = new URL(url);
  const host = u.hostname.toLowerCase().replace(/^www\./, "");
  return { host, page: host + (u.pathname.replace(/\/+$/, "") || "") };
}
