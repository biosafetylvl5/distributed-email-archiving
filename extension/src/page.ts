// functions run in the page through scripting.executeScript, only when the
// user clicks the extension or its menu item (activeTab). Each must be
// self-contained: they're serialized, so they can't use imports or closures.

export interface PageInfo {
  url: string;
  title: string;
  siteName: string;
}

export function readPage(): PageInfo {
  const meta = (sel: string) => document.querySelector<HTMLMetaElement>(sel)?.content?.trim() ?? "";
  return {
    url: location.href,
    title: document.title.trim(),
    siteName: meta('meta[property="og:site_name"]') || meta('meta[name="application-name"]'),
  };
}

// type the address into an email field; true if one was filled. From the
// context menu (`focused`), the field the user right-clicked, whatever it is;
// from the popup, the focused field only if it's for email (the page may have
// left a search box focused), else the page's first visible email field.
export function fillAddress(address: string, focused: boolean): boolean {
  const fillable = (el: Element | null): el is HTMLInputElement => {
    if (!(el instanceof HTMLInputElement) || el.disabled || el.readOnly) return false;
    return ["email", "text", "search"].includes(el.type);
  };
  const forEmail = (el: HTMLInputElement) =>
    el.type === "email" || /e-?mail/i.test([el.name, el.id, el.autocomplete, el.placeholder].join(" "));
  const visible = (el: HTMLElement) => el.offsetParent !== null || el.getClientRects().length > 0;
  const active = document.activeElement;
  let target: HTMLInputElement | null = fillable(active) && (focused || forEmail(active)) ? active : null;
  if (!target) {
    const candidates = [
      ...document.querySelectorAll<HTMLInputElement>('input[type="email"], input[autocomplete~="email"], input[name*="email" i]'),
    ];
    target = candidates.find((el) => fillable(el) && visible(el)) ?? null;
  }
  if (!target) return false;
  target.focus();
  // through the native setter, so frameworks that track the value notice
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
  if (setter) setter.call(target, address);
  else target.value = address;
  target.dispatchEvent(new Event("input", { bubbles: true }));
  target.dispatchEvent(new Event("change", { bubbles: true }));
  return true;
}
