// the toolbar popup: this site's subscription (or a form to add it), the
// list of all of them, and editing one

import { Api, ApiError, type Editable, type Me, type Subscription } from "./api";
import { hasApiPermission, loadConfig, type Config } from "./config";
import { byId, copyText, el } from "./dom";
import { ext } from "./ext";
import { addressOf, fillInto, pageInfo, signupUrlOf } from "./flow";
import type { PageInfo } from "./page";
import { isWebPage, suggestName, suggestTag } from "./suggest";

let config: Config;
let api: Api;
let tab: chrome.tabs.Tab | undefined;
let page: PageInfo | null = null;
let all: Subscription[] | null = null;
let back: () => void = () => undefined;

const SECTIONS = ["setup", "site", "list", "edit"] as const;

function show(section: (typeof SECTIONS)[number]): void {
  for (const s of SECTIONS) byId(s).hidden = s !== section;
  byId("tab-site").setAttribute("aria-pressed", String(section === "site"));
  byId("tab-list").setAttribute("aria-pressed", String(section === "list"));
}

function status(message: string, error = false): void {
  const p = byId("status");
  p.textContent = message;
  p.className = error ? "error" : "";
  p.hidden = !message;
}

function failed(err: unknown): void {
  if (err instanceof ApiError && err.status === 401) {
    status("The archive refused the token. Check it in Options, or ask the admin for a new one.", true);
  } else {
    status(err instanceof Error ? err.message : String(err), true);
  }
}

// --- a subscription, with its address and what can be done with it

function card(sub: Subscription, opts: { label?: string } = {}): HTMLElement {
  const address = addressOf(sub, config.form);
  const canFill = tab?.id !== undefined && isWebPage(tab.url);
  return el(
    "div",
    { class: "card" },
    opts.label ? el("div", { class: "muted" }, opts.label) : null,
    el("div", { class: "title" }, sub.name || sub.tag, el("span", { class: `badge ${sub.status}` }, sub.status)),
    sub.name ? el("div", { class: "muted" }, sub.tag) : null,
    el("code", {}, address),
    sub.status !== "approved"
      ? el("div", { class: "muted" }, sub.status === "pending" ? "Mail is held until the admin approves it." : "Mail to it is refused.")
      : null,
    el(
      "div",
      { class: "actions" },
      el("button", { type: "button", onclick: () => copyText(address).then(() => status(`Copied ${address}`)) }, "Copy"),
      canFill ? el("button", { type: "button", onclick: () => fill(address) }, "Fill in") : null,
      el("button", { type: "button", onclick: () => edit(sub) }, "Edit"),
      sub.signup_url && isWebPage(sub.signup_url)
        ? el("button", { type: "button", onclick: () => ext.tabs.create({ url: sub.signup_url! }) }, "Sign-up page")
        : null,
    ),
  );
}

async function fill(address: string): Promise<void> {
  if (tab?.id === undefined) return;
  try {
    if (await fillInto(tab.id, undefined, address)) {
      status(`Filled in ${address}`);
      window.close();
    } else {
      await copyText(address);
      status("Couldn't find an email field on this page, so the address is copied instead.");
    }
  } catch (err) {
    failed(err);
  }
}

// --- this site

async function showSite(): Promise<void> {
  show("site");
  back = showSite;
  status("");
  const found = byId("found");
  const form = byId<HTMLFormElement>("create");
  found.replaceChildren();
  form.hidden = true;
  if (!page || !isWebPage(page.url)) {
    found.append(el("p", { class: "muted" }, "Open a newsletter's sign-up page to add it, or see all your subscriptions."));
    return;
  }
  try {
    const [{ exact, same_site }, me] = await Promise.all([api.lookup(page.url), api.me()]);
    for (const sub of exact) found.append(card(sub, { label: "Signed up on this page" }));
    for (const sub of same_site) found.append(card(sub, { label: "Elsewhere on this site" }));
    if (exact.length === 0) showCreate(me, same_site.length > 0);
  } catch (err) {
    failed(err);
  }
}

function showCreate(me: Me, another: boolean): void {
  const p = page!;
  const name = byId<HTMLInputElement>("c-name");
  const tag = byId<HTMLInputElement>("c-tag");
  byId("create").hidden = false;
  byId("create").querySelector("h2")!.textContent = another ? "Add another newsletter from this site" : "Add this newsletter";
  name.value = suggestName(p);
  tag.value = "";
  byId<HTMLInputElement>("c-signup").value = signupUrlOf(p.url);
  const preview = () => {
    const t = tag.value.trim().toLowerCase() || suggestTag(name.value, byId<HTMLInputElement>("c-signup").value);
    tag.placeholder = t;
    byId("c-preview").textContent = t
      ? config.form === "underscore"
        ? `${me.user}__${t}@${me.archive_domain}`
        : `${me.user}+${t}@${me.archive_domain}`
      : "(give it a name or a tag)";
  };
  name.oninput = preview;
  tag.oninput = preview;
  preview();
  const l = me.limits;
  const left = Math.max(0, Math.min(l.added_per_day - l.added_today, l.subscriptions_max - l.subscriptions));
  byId("limits").textContent =
    `You can add ${left} more today.` + (l.new_status === "pending" ? " New ones wait for the admin's approval." : "");
  byId<HTMLButtonElement>("c-submit").disabled = left === 0;
}

async function create(e: Event): Promise<void> {
  e.preventDefault();
  const value = (id: string) => byId<HTMLInputElement | HTMLTextAreaElement>(id).value.trim();
  const fields: Editable & { tag?: string } = {
    name: value("c-name") || null,
    description: value("c-description") || null,
    signup_url: value("c-signup") || null,
    user_note: value("c-note") || null,
  };
  if (value("c-tag")) fields.tag = value("c-tag").toLowerCase();
  const button = byId<HTMLButtonElement>("c-submit");
  button.disabled = true;
  try {
    const sub = await api.create(fields);
    all = null;
    byId("create").hidden = true;
    byId("found").replaceChildren(card(sub, { label: "Added. Use this address to sign up:" }));
    status("");
  } catch (err) {
    if (err instanceof ApiError && err.code === "exists" && err.body.subscription) {
      byId("found").replaceChildren(card(err.body.subscription as Subscription, { label: "You already have this tag:" }));
      byId("create").hidden = true;
      status("");
    } else failed(err);
  } finally {
    button.disabled = false;
  }
}

// --- all subscriptions

async function showList(): Promise<void> {
  show("list");
  back = showList;
  status("");
  try {
    all ??= await api.list();
    renderList();
    byId("search").focus();
  } catch (err) {
    failed(err);
  }
}

function renderList(): void {
  const q = byId<HTMLInputElement>("search").value.trim().toLowerCase();
  const items = (all ?? [])
    .filter((s) => !q || [s.tag, s.name, s.description, s.signup_url, s.user_note].some((v) => v?.toLowerCase().includes(q)))
    .sort((a, b) => (a.name || a.tag).localeCompare(b.name || b.tag));
  byId("items").replaceChildren(
    ...(items.length ? items.map((s) => card(s)) : [el("p", { class: "muted" }, q ? "Nothing matches." : "No subscriptions yet.")]),
  );
}

// --- editing

let editing: Subscription | null = null;

const EDIT_FIELDS: [keyof Editable, string][] = [
  ["name", "e-name"],
  ["description", "e-description"],
  ["signup_url", "e-signup"],
  ["user_note", "e-note"],
];

function edit(sub: Subscription): void {
  editing = sub;
  show("edit");
  status("");
  byId("e-title").textContent = `Edit ${sub.tag}`;
  for (const [key, id] of EDIT_FIELDS) byId<HTMLInputElement>(id).value = sub[key] ?? "";
  const admin = [
    `Status: ${sub.status}`,
    sub.sender_domain ? `only mail from ${sub.sender_domain}` : null,
    sub.capture_limit_mb ? `link captures up to ${sub.capture_limit_mb} MiB` : null,
  ].filter(Boolean);
  byId("e-admin").textContent = `${admin.join("; ")}. Only the admin can change these.`;
}

async function save(e: Event): Promise<void> {
  e.preventDefault();
  const sub = editing;
  if (!sub) return;
  // send only what changed, so edits made elsewhere to other fields survive
  const changed: Editable = {};
  for (const [key, id] of EDIT_FIELDS) {
    const v = byId<HTMLInputElement>(id).value.trim();
    if (v !== (sub[key] ?? "")) changed[key] = v;
  }
  if (Object.keys(changed).length === 0) return back();
  try {
    const saved = await api.update(sub.tag, changed, sub.updated_at);
    all = all?.map((s) => (s.tag === saved.tag ? saved : s)) ?? null;
    back();
    status(`Saved ${saved.tag}`);
  } catch (err) {
    if (err instanceof ApiError && err.code === "conflict" && err.body.subscription) {
      edit(err.body.subscription as Subscription);
      status("It was changed somewhere else meanwhile; this is the latest. Make your edit again.", true);
    } else failed(err);
  }
}

// --- start

async function start(): Promise<void> {
  byId("open-options").onclick = () => ext.runtime.openOptionsPage();
  byId("setup-options").onclick = () => ext.runtime.openOptionsPage();
  config = await loadConfig();
  if (!config.apiUrl || !config.token || !(await hasApiPermission(config.apiUrl))) {
    show("setup");
    return;
  }
  api = new Api(config);
  byId("tab-site").onclick = () => void showSite();
  byId("tab-list").onclick = () => void showList();
  byId<HTMLFormElement>("create").onsubmit = (e) => void create(e);
  byId<HTMLFormElement>("edit-form").onsubmit = (e) => void save(e);
  byId("e-cancel").onclick = () => back();
  byId("search").oninput = renderList;
  // popup.html?tab=<id>: about that tab, for testing the popup opened as a page
  const forced = Number(new URLSearchParams(location.search).get("tab"));
  tab = forced ? await ext.tabs.get(forced) : (await ext.tabs.query({ active: true, currentWindow: true }))[0];
  page = tab && isWebPage(tab.url) ? await pageInfo(tab) : null;
  await showSite();
}

start().catch(failed);
