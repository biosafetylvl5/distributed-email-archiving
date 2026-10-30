// a name for a newsletter from the page it's signed up on

import { hostSlug, suggestTag } from "../../cloudflare/src/tagfields";
import type { PageInfo } from "./page";

export { suggestTag };

// title parts that name the action or the platform, not the newsletter
const GENERIC =
  /^(home|sign ?up|signup|subscribe|join|newsletters?|email list|mailing list|log ?in|welcome|substack|beehiiv|buttondown|ghost|mailchimp|convertkit|kit|medium)$/i;
const LEADING = /^(subscribe to|sign up for|sign up to|join|get)\s+(the\s+)?/i;
const SEPARATOR = /\s+[|\-–—·:•]\s+/;

export function suggestName(page: Pick<PageInfo, "url" | "title" | "siteName">): string {
  if (page.siteName && !GENERIC.test(page.siteName)) return page.siteName.slice(0, 200);
  const parts = page.title
    .split(SEPARATOR)
    .map((p) => p.replace(LEADING, "").trim())
    .filter((p) => p && !GENERIC.test(p));
  if (parts.length) return parts[0].slice(0, 200);
  try {
    const slug = hostSlug(new URL(page.url).hostname);
    return slug ? slug.charAt(0).toUpperCase() + slug.slice(1) : "";
  } catch {
    return "";
  }
}

export function isWebPage(url: string | undefined): url is string {
  return !!url && /^https?:\/\//.test(url);
}
