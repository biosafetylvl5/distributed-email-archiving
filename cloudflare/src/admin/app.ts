// admin dashboard: server-rendered HTML + plain forms, no client JS
// every request is authenticated (auth.ts); POSTs must come from our own origin

import type { Env } from "../env";
import { isValidTag, isValidUser, type TagRecord } from "../policy";
import {
  DEFAULTS,
  SETTING_KEYS,
  SETTINGS_KEY,
  VAR_NAMES,
  parseCount,
  parseMode,
  resolveSettings,
  sanitizeOverrides,
  type Overrides,
  type SettingKey,
  type Settings,
} from "../settings";
import { authenticate, type AuthResult } from "./auth";
import { CHART_DAYS } from "./stats";
import {
  parseRetryRequest,
  requestRetry,
  RETRYABLE,
  waitingRetries,
  type RetryResult,
} from "./retry";
import { snapshot, STORAGE_AREAS, type Snapshot, type Storage } from "./summary";
import { findMessage, getViewMeta, issueSizes, rawMail, viewFile, type ViewMeta } from "./messages";
import {
  URL_KINDS,
  URL_STATUSES,
  type ArchivedMessage,
  type CaptureState,
  type CaptureSummary,
  type UrlCaptures,
  type UrlKind,
  type UrlStatus,
} from "./captures";
import { html, page, securityHeaders, type Html } from "./html";
import {
  getQuarantined,
  getTag,
  getUser,
  listQuarantine,
  listTags,
  listUsers,
  promote,
  putTag,
  setUserActive,
  today,
  usageForDay,
  type TagEntry,
} from "./store";

export type Authenticator = (request: Request, env: Env) => Promise<AuthResult>;

interface Ctx {
  request: Request;
  env: Env;
  url: URL;
  email: string;
}

const TAG_STATUSES = ["approved", "pending", "blocked"] as const;
const HOSTNAME_RE = /^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$/;

const NAME_MAX = 200;
const DESCRIPTION_MAX = 2000;
const NOTE_MAX = 500;
const TEXT_FIELDS = ["name", "description", "sender_domain", "signup_url", "note"] as const;
type TextField = (typeof TEXT_FIELDS)[number];

// rendered as a link, so only http(s): never javascript:, data:, etc.
// Also checked on display, since KV can be edited outside the dashboard
function isSignupUrl(value: string): boolean {
  if (value.length > 2048) return false;
  try {
    const u = new URL(value);
    return (u.protocol === "https:" || u.protocol === "http:") && u.hostname !== "";
  } catch {
    return false;
  }
}

function redirect(path: string, flash?: { msg?: string; err?: string }): Response {
  const q = new URLSearchParams();
  if (flash?.msg) q.set("msg", flash.msg);
  if (flash?.err) q.set("err", flash.err);
  const qs = q.toString();
  return new Response(null, {
    status: 303,
    headers: securityHeaders({ location: qs ? `${path}${path.includes("?") ? "&" : "?"}${qs}` : path }),
  });
}

function text(status: number, body: string): Response {
  return new Response(body, {
    status,
    headers: securityHeaders({ "content-type": "text/plain; charset=utf-8" }),
  });
}

function show(ctx: Ctx, title: string, body: Html, status?: number): Response {
  return page(ctx.url.pathname, title, body, {
    email: ctx.email,
    msg: ctx.url.searchParams.get("msg"),
    err: ctx.url.searchParams.get("err"),
    status,
  });
}

function field(form: FormData, name: string): string {
  const v = form.get(name);
  return typeof v === "string" ? v.trim() : "";
}

function userPath(user: string): string {
  return `/users/${encodeURIComponent(user)}`;
}

function address(env: Env, user: string, tag?: string): string {
  const domain = (env.ARCHIVE_DOMAIN ?? "").trim().toLowerCase() || "<ARCHIVE_DOMAIN>";
  return tag ? `${user}+${tag}@${domain}` : `${user}@${domain}`;
}

async function settingsFor(env: Env) {
  const overrides = env.SETTINGS
    ? sanitizeOverrides(await env.SETTINGS.get(SETTINGS_KEY, "json"))
    : {};
  return { overrides, ...resolveSettings(env, overrides) };
}

function configWarnings(env: Env): Html {
  const warnings: string[] = [];
  if (!env.SETTINGS) {
    warnings.push("No SETTINGS KV binding: settings are read-only (wrangler.toml values).");
  }
  if (!(env.ARCHIVE_DOMAIN ?? "").trim()) {
    warnings.push("ARCHIVE_DOMAIN is unset: the Worker accepts mail for every domain in the zone.");
  }
  return html`${warnings.map((w) => html`<div class="msg warn">${w}</div>`)}`;
}

// --- pages

const QUIET_DAYS = 30;

function bar(n: number, max: number, cls: string): Html {
  const pct = max > 0 ? Math.floor((n / max) * 100) : 0; // floor: two bars never exceed 100%
  return html`<span class="bar ${cls}" style="width:${pct}%"></span>`;
}

function ago(d: Date | null, now: Date): string {
  if (!d) return "never";
  const days = Math.floor((now.getTime() - d.getTime()) / 86_400_000);
  return days <= 0 ? "today" : days === 1 ? "yesterday" : `${days} days ago`;
}

async function overview(ctx: Ctx): Promise<Response> {
  const { env } = ctx;
  const now = new Date();
  const [users, tags, { settings }, usage, snap] = await Promise.all([
    listUsers(env),
    listTags(env),
    settingsFor(env),
    usageForDay(env, today()),
    snapshot(env, now),
  ]);
  const { stats, captures } = snap;
  const active = users.filter((u) => u.record?.active).length;
  const byStatus = (s: string) => tags.filter((t) => t.record?.status === s).length;
  const withField = (f: "publish" | "sender_domain" | "signup_url") => tags.filter((t) => t.record?.[f]).length;
  const capFor = (scope: string) =>
    scope === "global"
      ? settings.global_daily_cap
      : scope.startsWith("tag:")
        ? settings.tag_daily_cap
        : settings.untagged_daily_cap;

  // every known subscription, plus user/tag pairs only seen in stored mail
  // (bare addresses, or unknown tags under quarantine mode)
  const known = new Map(tags.map((t) => [`${t.user}/${t.tag}`, t]));
  const rows = [...new Set([...known.keys(), ...stats.tags.keys()])].map((id) => {
    const s = stats.tags.get(id);
    const t = known.get(id);
    const [user, ...rest] = id.split("/");
    return { id, user, tag: rest.join("/"), entry: t, s };
  });
  rows.sort((a, b) => (b.s?.last?.getTime() ?? 0) - (a.s?.last?.getTime() ?? 0) || a.id.localeCompare(b.id));
  const quietCutoff = now.getTime() - QUIET_DAYS * 86_400_000;
  const quiet = rows.filter(
    (r) => r.entry?.record?.status === "approved" && (!r.s?.last || r.s.last.getTime() < quietCutoff),
  );
  const boundDomains = new Set(tags.map((t) => t.record?.sender_domain).filter(Boolean));
  const dayMax = Math.max(1, ...stats.days.map((d) => d.archive + d.quarantine));
  const t = stats.totals;
  const partial = stats.partial
    ? html`<p class="msg warn">The archive is large, so only part of it was scanned. Storage numbers are lower bounds.</p>`
    : "";

  return show(
    ctx,
    "Overview",
    html`${configWarnings(env)}
<table><tr><th>Users</th><td>${active} active, ${users.length - active} disabled</td></tr>
<tr><th>Subscriptions</th><td>${byStatus("approved")} approved, ${byStatus("pending")} pending, ${byStatus("blocked")} blocked
<br><span class="muted">${withField("publish")} published · ${withField("sender_domain")} with a sender domain · ${withField("signup_url")} with a sign-up link</span></td></tr>
<tr><th>Policy mode</th><td>${settings.policy_mode}</td></tr>
<tr><th>Receiving domain</th><td><code>${env.ARCHIVE_DOMAIN || "(any)"}</code></td></tr></table>

<h2>Stored mail</h2>
${freshness(snap, now)}
${partial}
<table><tr><th></th><th>Messages</th><th>Size</th><th>Last 7 days</th><th>Last 30 days</th></tr>
<tr><th>Archived</th><td>${t.archive.count}</td><td>${formatBytes(t.archive.bytes)}</td><td>${stats.last7.archive}</td><td>${stats.last30.archive}</td></tr>
<tr><th>Quarantined</th><td><a href="/quarantine">${t.quarantine.count}</a>${
  stats.oldestQuarantine ? html` <span class="muted">(oldest ${stats.oldestQuarantine})</span>` : ""
}</td><td>${formatBytes(t.quarantine.bytes)}</td><td>${stats.last7.quarantine}</td><td>${stats.last30.quarantine}</td></tr>
<tr><th>Total</th><td>${t.archive.count + t.quarantine.count}</td><td>${formatBytes(t.archive.bytes + t.quarantine.bytes)}</td>
<td>${stats.last7.archive + stats.last7.quarantine}</td><td>${stats.last30.archive + stats.last30.quarantine}</td></tr></table>

${captureSection(captures, now)}

${storageSection(snap.storage, now)}

<h2>Last ${CHART_DAYS} days <span class="muted">(UTC)</span></h2>
<table class="chart"><tr><th>Day</th><th>Archived</th><th>Quarantined</th><th></th></tr>
${[...stats.days].reverse().map(
  (d) => html`<tr><td>${d.day}</td><td>${d.archive}</td><td>${d.quarantine}</td>
<td class="bars">${bar(d.archive, dayMax, "a")}${bar(d.quarantine, dayMax, "q")}</td></tr>`,
)}</table>

<h2>Newsletters</h2>
${
  quiet.length
    ? html`<p class="msg warn">${quiet.length} approved subscription${quiet.length === 1 ? " has" : "s have"} received nothing in ${QUIET_DAYS} days: ${quiet.map(
        (r, i) => html`${i ? ", " : ""}<a href="${userPath(r.user)}">${r.entry?.record?.name || r.id}</a>`,
      )}. The signup may have lapsed.</p>`
    : ""
}
${
  rows.length === 0
    ? html`<p class="muted">No subscriptions or stored mail yet.</p>`
    : html`<table><tr><th>Newsletter</th><th>User</th><th>Status</th><th>Archived</th><th>Captured</th><th>Quarantined</th><th>Size</th><th>Last received</th></tr>
${rows.map((r) => {
  const rec = r.entry?.record;
  const label = rec?.name ? html`${rec.name} <code class="muted">${r.tag}</code>` : r.tag ? html`<code>${r.tag}</code>` : html`<span class="muted">(bare address)</span>`;
  const status = r.entry ? (rec?.status ?? "unreadable") : r.tag ? "unknown tag" : "";
  return html`<tr><td>${label}</td><td><a href="${userPath(r.user)}">${r.user}</a></td>
<td class="status-${rec?.status ?? ""}">${status}</td><td>${r.s?.archive ?? 0}</td>
<td>${capturedCell(captures.bySubscription.get(r.id))}</td><td>${r.s?.quarantine ?? 0}</td>
<td>${formatBytes(r.s?.bytes ?? 0)}</td><td title="${r.s?.last?.toISOString() ?? ""}">${ago(r.s?.last ?? null, now)}</td></tr>`;
})}</table>`
}

<h2>Top sender domains <span class="muted">(last 30 days)</span></h2>
${
  stats.senders.length === 0
    ? html`<p class="muted">No mail in the last 30 days.</p>`
    : html`<table><tr><th>Envelope sender domain</th><th>Messages</th><th>Pinned by a subscription</th></tr>
${stats.senders.slice(0, 10).map(
  (s) => html`<tr><td><code>${s.domain}</code></td><td>${s.count}</td><td>${boundDomains.has(s.domain) ? "yes" : html`<span class="muted">no</span>`}</td></tr>`,
)}</table>`
}

<h2>Today's usage <span class="muted">(${today()} UTC)</span></h2>
${
  usage.length === 0
    ? html`<p class="muted">No mail counted today.</p>`
    : html`<table><tr><th>Scope</th><th>Count</th><th>Daily cap</th></tr>
${usage.map((u) => html`<tr><td><code>${u.scope}</code></td><td>${u.count}</td><td>${capFor(u.scope)}</td></tr>`)}</table>
<p class="muted">Counters are approximate (KV is eventually consistent). Deferred retries count too.</p>`
}`,
  );
}

// --- captures (dea-capture's issue-status records)

// the summary is rebuilt every 5 minutes; well past that, dea-capture is likely down
const SUMMARY_STALE_MINUTES = 30;

function freshness(s: Snapshot, now: Date): Html {
  if (!s.generatedAt) return html`<p class="muted">Counted from the bucket just now.</p>`;
  const stale = now.getTime() - s.generatedAt.getTime() > SUMMARY_STALE_MINUTES * 60_000;
  const when = html`<span title="${s.generatedAt.toISOString()}">${agoTime(s.generatedAt, now)}</span>`;
  return stale
    ? html`<p class="msg warn">These numbers were last counted ${when}. dea-capture rebuilds them every 5 minutes, so it may be down.</p>`
    : html`<p class="muted">Counted by dea-capture ${when}; rebuilt every 5 minutes.</p>`;
}

function agoTime(d: Date | null, now: Date): string {
  if (!d) return "never";
  const mins = Math.floor((now.getTime() - d.getTime()) / 60_000);
  if (mins < 60) return mins <= 0 ? "just now" : `${mins} min ago`;
  if (mins < 48 * 60) return `${Math.floor(mins / 60)} h ago`;
  return ago(d, now);
}

function capturedCell(s: Record<CaptureState, number> | undefined): Html {
  if (!s) return html`<span class="muted">0</span>`;
  const extra = [
    s.partial ? html`<a class="status-pending" href="/captures?state=partial">${s.partial} partial</a>` : null,
    s.pending ? html`<span class="muted">${s.pending} pending</span>` : null,
  ].filter((x) => x !== null);
  return html`${s.done + s.partial}${extra.length ? html` <span class="muted">(</span>${extra.map((x, i) => html`${i ? ", " : ""}${x}`)}<span class="muted">)</span>` : ""}`;
}

function captureSection(c: CaptureSummary, now: Date): Html {
  const archived = c.states.done + c.states.partial + c.states.pending;
  const t = c.totals;
  return html`<h2>Capture <span class="muted">(<a href="/captures">by message</a>)</span></h2>
${c.partial ? html`<p class="msg warn">Only part of the archive was scanned; these numbers are lower bounds.</p>` : ""}
<table><tr><th>Messages</th><td>${c.states.done + c.states.partial} of ${archived} processed:
${c.states.done} complete, <a href="/captures?state=partial">${c.states.partial} partial</a>,
<a href="/captures?state=pending">${c.states.pending} pending</a></td></tr>
<tr><th>Embedded files</th><td>${t.assets_ok} stored, ${t.assets_failed} failed, ${t.assets_deferred} deferred, ${t.assets_refused} refused
<br><span class="muted">${t.pixels} of them tracking pixels</span></td></tr>
<tr><th>Links</th><td>${t.links} found: ${t.links_capture} to capture, ${t.links_resolve} redirects to resolve, ${t.links_skip} never fetched
<br>Resolved and queued for ${c.linkStates.done + c.linkStates.partial} of ${c.states.done + c.states.partial} processed messages:
${t.links_resolved} redirects followed, ${t.links_unresolved} couldn't be, ${t.links_refused} refused</td></tr>
<tr><th>Linked URLs</th><td>${urlTable(c.urls)}</td></tr>
<tr><th>Last processed</th><td title="${c.lastCompleted?.toISOString() ?? ""}">${agoTime(c.lastCompleted, now)}
<span class="muted">· new mail is picked up within about 5 minutes</span></td></tr></table>`;
}

function storageSection(st: Storage | null, now: Date): Html {
  if (!st) {
    return html`<h2>Storage</h2><p class="muted">Not counted yet: dea-capture lists the whole bucket about once an hour.</p>`;
  }
  const total = STORAGE_AREAS.reduce(
    (t, [a]) => ({ count: t.count + st.areas[a].count, bytes: t.bytes + st.areas[a].bytes }),
    { count: 0, bytes: 0 },
  );
  const kinds = Object.entries(st.captures).filter(([, k]) => k.count);
  const kindLabel = (k: string) => (k in URL_KIND_LABELS ? URL_KIND_LABELS[k as UrlKind] : k);
  return html`<h2>Storage <span class="muted">(the whole R2 bucket, counted
<span title="${st.computedAt?.toISOString() ?? ""}">${agoTime(st.computedAt, now)}</span>; recounted hourly)</span></h2>
<table><tr><th></th><th>Objects</th><th>Size</th><th>Share</th></tr>
${STORAGE_AREAS.filter(([a]) => st.areas[a].count).map(([a, label]) => {
  const x = st.areas[a];
  const sub =
    a === "captures" && kinds.length
      ? html`<br><span class="muted">${kinds.map(
          ([k, v], i) => html`${i ? " · " : ""}${kindLabel(k)}: ${formatBytes(v.bytes)} for ${v.urls} URLs`,
        )}</span>`
      : "";
  return html`<tr><th>${label}${sub}</th><td>${x.count}</td><td>${formatBytes(x.bytes)}</td>
<td>${total.bytes ? ((100 * x.bytes) / total.bytes).toFixed(1) : "0"}%</td></tr>`;
})}
<tr><th>Total</th><td>${total.count}</td><td>${formatBytes(total.bytes)}</td><td></td></tr></table>`;
}

const URL_KIND_LABELS = { page: "Pages", direct: "Files", media: "Media", gallery: "Social posts" } as const;

// `back`: the /captures state to return to, when cells get "Retry now" buttons
function urlTable(u: UrlCaptures, back?: string): Html {
  if (u.total === 0) return html`<span class="muted">none queued yet</span>`;
  const cols = URL_STATUSES.filter((s) => URL_KINDS.some((k) => u.byKind[k][s] > 0));
  const cell = (k: UrlKind, s: UrlStatus) => {
    const n = u.byKind[k][s];
    if (!n) return html`<span class="muted">0</span>`;
    if (back === undefined || !(RETRYABLE as readonly string[]).includes(s)) return html`${n}`;
    return html`${n} ${retryButton(
      { scope: "urls", kind: k, status: s },
      back,
      `Capture these ${n} again now${s === "failed" ? ", one more attempt each" : ""}`,
    )}`;
  };
  return html`${u.total} distinct URLs, each captured once however many messages link to it${u.wayback ? html`; ${u.wayback} have a Wayback Machine snapshot, stored when our own capture failed` : ""}
<table><tr><th></th>${cols.map((s) => html`<th>${s}</th>`)}</tr>
${URL_KINDS.map(
    (k) => html`<tr><th>${URL_KIND_LABELS[k]}</th>${cols.map((s) => html`<td>${cell(k, s)}</td>`)}</tr>`,
  )}</table>
<span class="muted">Each kind is captured one at a time: pages in a browser, media with yt-dlp, social posts with gallery-dl.</span>`;
}

const CAPTURE_STATES = ["all", "partial", "pending", "done"] as const;
const CAPTURE_PAGE_MAX = 200;

function retryButton(fields: Record<string, string>, back: string, title: string): Html {
  return html`<form class="inline" method="post" action="/captures/retry">${Object.entries(fields).map(
    ([k, v]) => html`<input type="hidden" name="${k}" value="${v}">`,
  )}<input type="hidden" name="back" value="${back}"><button title="${title}">Retry now</button></form>`;
}

function retryWhat(r: RetryResult): Html {
  if (r.scope === "message") return html`message <code title="${r.sha ?? ""}">${r.sha?.slice(0, 12) ?? "?"}</code>`;
  const label = URL_KIND_LABELS[r.kind as UrlKind] ?? r.kind ?? "?";
  return html`${label}, ${r.status ?? "?"}`;
}

function retryOutcome(r: RetryResult): string {
  const parts: string[] = [];
  const rl = r.relinked;
  if (rl?.tried) parts.push(`${rl.resolved} of ${rl.tried} redirects followed`);
  if (rl?.rateLimited) parts.push("stopped at a 429");
  if (rl?.outOfTime) parts.push("out of time, continues next run");
  if (r.rescheduled !== undefined) parts.push(`${r.rescheduled} of ${r.rescheduled + (r.left ?? 0)} URLs due now`);
  if (r.error) parts.push(r.error);
  return parts.join("; ") || "nothing to do";
}

function retriesSection(waiting: number, recent: RetryResult[]): Html {
  return html`<p>${waiting ? html`<strong>${waiting} retry request${waiting === 1 ? "" : "s"} waiting.</strong>` : "No retry requests waiting."}
<span class="muted">dea-capture applies them every 5 minutes. Only URLs that failed or wait for a retry are made due,
and not those due within the hour, which may be running; a failed one gets one more attempt.</span></p>
${
  recent.length
    ? html`<table><tr><th>Applied (UTC)</th><th>Retried</th><th>Result</th></tr>
${recent.map(
  (r) => html`<tr><td>${r.appliedAt?.toISOString().slice(0, 16).replace("T", " ") ?? ""}</td><td>${retryWhat(r)}</td>
<td>${retryOutcome(r)}</td></tr>`,
)}</table>`
    : ""
}`;
}

function messagePath(sha: string, day?: string): string {
  return `/messages/${sha}${day ? `?day=${day}` : ""}`;
}

function captureRow(m: ArchivedMessage, back: string, storage: Storage | null): Html {
  const c = m.capture;
  const sub = m.user ? html`<a href="${userPath(m.user)}">${m.user}</a> <code>${m.tag || "(bare)"}</code>` : html`<span class="muted">unknown</span>`;
  const sha = m.key.split("/").pop()?.replace(/\.eml$/, "") ?? "";
  const link = html`<a href="${messagePath(sha, m.day)}" title="View ${sha}"><code>${sha.slice(0, 12)}</code></a>`;
  const stored = storage?.messages.get(sha);
  const size = html`<td title="Embedded files, link records and the rendered view; not the link captures, which messages share">${
    stored === undefined ? "" : formatBytes(stored)
  }</td>`;
  if (!c) {
    return html`<tr><td>${m.day}</td><td>${sub}</td><td>${link}</td>
<td class="muted">pending</td><td></td><td></td>${size}<td></td><td></td></tr>`;
  }
  const n = c.counts;
  const problems = [
    n.assets_failed ? `${n.assets_failed} failed` : "",
    n.assets_deferred ? `${n.assets_deferred} deferred` : "",
    n.assets_refused ? `${n.assets_refused} refused` : "",
  ].filter(Boolean);
  return html`<tr><td>${m.day}</td><td>${sub}</td><td>${link}</td>
<td class="${c.state === "done" ? "status-approved" : "status-pending"}">${c.state === "done" ? "complete" : "partial"}</td>
<td>${n.assets_ok}${problems.length ? html` <span class="status-pending">(${problems.join(", ")})</span>` : ""}</td>
<td>${n.links_capture + n.links_resolve} <span class="muted">/ ${n.links}</span>
${c.linksState === "pending" ? html`<span class="muted">· not queued yet</span>` : html`<span class="muted">· ${n.captures_queued} URLs queued</span>${n.links_unresolved ? html` <span class="status-pending">(${n.links_unresolved} unresolved)</span>` : ""}`}</td>
${size}<td title="${c.completedAt?.toISOString() ?? ""}">${c.completedAt?.toISOString().slice(0, 16).replace("T", " ") ?? ""}</td>
<td>${retryButton({ scope: "message", sha }, back, "Follow this message's failed redirects again, then capture its failed links again now")}</td></tr>`;
}

async function capturesPage(ctx: Ctx): Promise<Response> {
  const now = new Date();
  const [snap, waiting] = await Promise.all([snapshot(ctx.env, now), waitingRetries(ctx.env)]);
  const c = snap.captures;
  const asked = ctx.url.searchParams.get("state") ?? "all";
  const state = (CAPTURE_STATES as readonly string[]).includes(asked) ? asked : "all";
  const shown = c.messages.filter((m) => state === "all" || (m.capture?.state ?? "pending") === state);
  const total = (st: string) => (st === "all" ? c.messages.length : c.states[st as CaptureState]);
  return show(
    ctx,
    "Captures",
    html`<p class="muted">What dea-capture stored for each archived message, newest first: embedded files and
the link inventory, under <code>issues/&lt;sha&gt;/</code> in R2. Open a message to read it.</p>
${freshness(snap, now)}
${c.partial ? html`<p class="msg warn">Only part of the archive was scanned.</p>` : ""}
<h2>Linked URLs</h2>
${urlTable(c.urls, state)}
${retriesSection(waiting, snap.retries.recent)}
<h2>Messages</h2>
<p>${CAPTURE_STATES.map(
      (st, i) =>
        html`${i ? " · " : ""}${st === state ? html`<strong>${st} (${total(st)})</strong>` : html`<a href="/captures?state=${st}">${st} (${total(st)})</a>`}`,
    )}</p>
${
  shown.length === 0
    ? html`<p class="muted">No messages.</p>`
    : html`<table><tr><th>Received</th><th>Subscription</th><th>Message</th><th>Status</th><th>Embedded files</th>
<th title="links to capture or resolve, of all links">Links</th><th>Stored</th><th>Processed (UTC)</th><th></th></tr>
${shown.slice(0, CAPTURE_PAGE_MAX).map((m) => captureRow(m, state, snap.storage))}</table>
${shown.length > CAPTURE_PAGE_MAX ? html`<p class="muted">Showing the newest ${CAPTURE_PAGE_MAX} of ${shown.length}.</p>` : ""}`
}`,
  );
}

async function usersPage(ctx: Ctx): Promise<Response> {
  const [users, tags] = await Promise.all([listUsers(ctx.env), listTags(ctx.env)]);
  const tagCount = (u: string) => tags.filter((t) => t.user === u).length;
  return show(
    ctx,
    "Users",
    html`<form method="post" action="/users">
<label>New user <input type="text" name="name" placeholder="john.smith" required pattern="[a-z0-9][a-z0-9.\\-]{0,62}"></label>
<button>Add</button> <span class="muted">lowercase letters, digits, dots, hyphens</span></form>
<table><tr><th>User</th><th>Status</th><th>Subscriptions</th><th>Bare address</th></tr>
${users.map(
  (u) => html`<tr><td><a href="${userPath(u.name)}">${u.name}</a></td>
<td>${u.record ? (u.record.active ? "active" : "disabled") : html`<span class="err">unreadable record</span>`}</td>
<td>${tagCount(u.name)}</td><td><code>${address(ctx.env, u.name)}</code></td></tr>`,
)}</table>`,
  );
}

async function addUser(ctx: Ctx): Promise<Response> {
  const name = field(await ctx.request.formData(), "name").toLowerCase();
  if (!isValidUser(name)) return redirect("/users", { err: `Invalid user name: ${name}` });
  if (await getUser(ctx.env, name)) {
    return redirect(userPath(name), { err: `${name} already exists` });
  }
  await setUserActive(ctx.env, name, true);
  return redirect(userPath(name), { msg: `Added ${name}` });
}

function tagForm(t: TagEntry, fromSubscriptions: boolean): Html {
  const r = t.record;
  return html`<form method="post" action="${userPath(t.user)}/tags">
<input type="hidden" name="tag" value="${t.tag}">
${fromSubscriptions ? html`<input type="hidden" name="back" value="subscriptions">` : ""}
<select name="status">${TAG_STATUSES.map(
    (s) => html`<option value="${s}" ${r?.status === s ? html`selected` : ""}>${s}</option>`,
  )}</select>
<label><input type="checkbox" name="publish" value="1" ${r?.publish ? html`checked` : ""}> publish</label>
<details><summary>Edit details</summary><div class="tagform">
<label>Name <input type="text" name="name" value="${r?.name ?? ""}" placeholder="Ramen Weekly" maxlength="${NAME_MAX}"></label>
<label>Description <textarea name="description" rows="3" maxlength="${DESCRIPTION_MAX}">${r?.description ?? ""}</textarea></label>
<label>Sender domain <input type="text" name="sender_domain" value="${r?.sender_domain ?? ""}" placeholder="optional; other senders are quarantined"></label>
<label>Sign-up link <input type="url" name="signup_url" value="${r?.signup_url ?? ""}" placeholder="https://..."></label>
<label>Private note <input type="text" name="note" value="${r?.note ?? ""}" maxlength="${NOTE_MAX}"></label>
</div></details>
<button>Save</button></form>`;
}

function tagSummary(t: TagEntry): Html {
  const r = t.record;
  return html`${r?.name ? html`<strong>${r.name}</strong><br>` : ""}<code>${t.tag}</code>${
    r?.description ? html`<div class="desc">${r.description}</div>` : ""
  }${
    r?.signup_url && isSignupUrl(r.signup_url)
      ? html`<div><a href="${r.signup_url}" rel="noopener noreferrer" target="_blank">sign-up page</a></div>`
      : ""
  }${r?.note ? html`<div class="muted">Note: ${r.note}</div>` : ""}`;
}

function tagTable(ctx: Ctx, tags: TagEntry[], withUser: boolean): Html {
  if (tags.length === 0) return html`<p class="muted">No subscriptions.</p>`;
  return html`<table><tr>${withUser ? html`<th>User</th>` : ""}<th>Newsletter</th><th>Status</th><th>Address</th><th>Edit</th></tr>
${tags.map(
  (t) => html`<tr>${withUser ? html`<td><a href="${userPath(t.user)}">${t.user}</a></td>` : ""}
<td>${tagSummary(t)}</td>
<td class="status-${t.record?.status ?? ""}">${t.record?.status ?? "unreadable"}${t.record?.publish ? " · publish" : ""}${
    t.record?.sender_domain ? html`<br><span class="muted">from ${t.record.sender_domain}</span>` : ""
  }</td>
<td><code>${address(ctx.env, t.user, t.tag)}</code></td><td>${tagForm(t, withUser)}</td></tr>`,
)}</table>`;
}

async function userPage(ctx: Ctx, user: string): Promise<Response> {
  const record = await getUser(ctx.env, user);
  if (!record) return show(ctx, "Not found", html`<p>No user ${user}.</p>`, 404);
  const tags = await listTags(ctx.env, user);
  return show(
    ctx,
    `User ${user}`,
    html`<p>Status: <strong>${record.active ? "active" : "disabled"}</strong>
<form class="inline" method="post" action="${userPath(user)}/active">
<input type="hidden" name="active" value="${record.active ? "0" : "1"}">
<button>${record.active ? "Disable" : "Enable"}</button></form></p>
<p>Bare address (always quarantined): <code>${address(ctx.env, user)}</code></p>
<h2>Subscriptions</h2>
<form method="post" action="${userPath(user)}/tags">
<label>New tag <input type="text" name="tag" placeholder="ramen_weekly" required></label>
<input type="hidden" name="status" value="approved">
<input type="text" name="name" placeholder="name (optional)" maxlength="${NAME_MAX}">
<input type="text" name="sender_domain" placeholder="sender domain (optional)">
<input type="url" name="signup_url" placeholder="sign-up link (optional)">
<button>Approve</button></form>
${tagTable(ctx, tags, false)}`,
  );
}

async function setActive(ctx: Ctx, user: string): Promise<Response> {
  if (!(await getUser(ctx.env, user))) return redirect("/users", { err: `No user ${user}` });
  const active = field(await ctx.request.formData(), "active") === "1";
  await setUserActive(ctx.env, user, active);
  return redirect(userPath(user), { msg: `${user} ${active ? "enabled" : "disabled"}` });
}

async function saveTag(ctx: Ctx, user: string): Promise<Response> {
  if (!(await getUser(ctx.env, user))) return redirect("/users", { err: `No user ${user}` });
  const form = await ctx.request.formData();
  const back = field(form, "back") === "subscriptions" ? "/subscriptions" : userPath(user);
  const tag = field(form, "tag").toLowerCase();
  if (!isValidTag(tag)) return redirect(back, { err: `Invalid tag: ${tag}` });
  const status = field(form, "status");
  if (!(TAG_STATUSES as readonly string[]).includes(status)) {
    return redirect(back, { err: `Invalid status: ${status}` });
  }
  const senderDomain = field(form, "sender_domain").toLowerCase();
  if (senderDomain && !HOSTNAME_RE.test(senderDomain)) {
    return redirect(back, { err: `Invalid sender domain: ${senderDomain}` });
  }
  const signupUrl = field(form, "signup_url");
  if (signupUrl && !isSignupUrl(signupUrl)) {
    return redirect(back, { err: `Invalid sign-up link (must be http or https): ${signupUrl}` });
  }
  const text: Record<TextField, string> = {
    name: field(form, "name").replace(/\s+/g, " ").slice(0, NAME_MAX),
    description: field(form, "description").replace(/\r\n?/g, "\n").slice(0, DESCRIPTION_MAX),
    sender_domain: senderDomain,
    signup_url: signupUrl,
    note: field(form, "note").slice(0, NOTE_MAX),
  };

  const current = (await getTag(ctx.env, user, tag)) ?? ({} as Partial<TagRecord>);
  const next: TagRecord = {
    ...current,
    status: status as TagRecord["status"],
    publish: form.get("publish") === "1",
  };
  // only touch fields the form actually has, so the short "new tag" form
  // can't wipe a description or note; an empty value clears the field
  for (const key of TEXT_FIELDS) {
    if (!form.has(key)) continue;
    if (text[key]) next[key] = text[key];
    else delete next[key];
  }
  await putTag(ctx.env, user, tag, next);
  return redirect(back, { msg: `Saved ${user}/${tag} (${status})` });
}

async function subscriptionsPage(ctx: Ctx): Promise<Response> {
  const tags = await listTags(ctx.env);
  return show(ctx, "Subscriptions", tagTable(ctx, tags, true));
}

const SETTING_LABELS: Record<SettingKey, string> = {
  policy_mode: "Policy mode (unknown tags)",
  max_raw_bytes: "Max message size (bytes)",
  global_daily_cap: "Global daily cap",
  tag_daily_cap: "Per-tag daily cap",
  untagged_daily_cap: "Bare-address daily cap",
};

async function settingsPage(ctx: Ctx): Promise<Response> {
  const { env } = ctx;
  const { overrides, settings, source } = await settingsFor(env);
  const editable = Boolean(env.SETTINGS);
  const input = (key: SettingKey): Html => {
    const v = overrides[key];
    if (key === "policy_mode") {
      return html`<select name="policy_mode" ${editable ? "" : html`disabled`}>
<option value="">(no override)</option>
${(["strict", "quarantine"] as const).map(
  (m) => html`<option value="${m}" ${v === m ? html`selected` : ""}>${m}</option>`,
)}</select>`;
    }
    return html`<input type="number" min="1" step="1" name="${key}" value="${v ?? ""}" placeholder="(no override)" ${editable ? "" : html`disabled`}>`;
  };
  return show(
    ctx,
    "Settings",
    html`${configWarnings(env)}
<form method="post" action="/settings"><table>
<tr><th>Setting</th><th>Effective</th><th>Source</th><th>wrangler.toml</th><th>Default</th><th>Override</th></tr>
${SETTING_KEYS.map(
  (key) => html`<tr><td>${SETTING_LABELS[key]}</td><td><strong>${settings[key]}</strong></td>
<td>${source[key]}</td><td><code>${env[VAR_NAMES[key]] ?? ""}</code></td><td>${DEFAULTS[key]}</td><td>${input(key)}</td></tr>`,
)}</table>
${editable ? html`<button>Save overrides</button>` : ""}</form>
<p class="muted"><strong>strict</strong> rejects mail to unknown tags; <strong>quarantine</strong> stores it in
<code>incoming/quarantine/</code>. Leave a field blank to fall back to wrangler.toml, then the default.
Changes reach the mail Worker within about a minute.</p>`,
  );
}

async function saveSettings(ctx: Ctx): Promise<Response> {
  if (!ctx.env.SETTINGS) return redirect("/settings", { err: "No SETTINGS KV binding" });
  const form = await ctx.request.formData();
  const next: Overrides = {};
  for (const key of SETTING_KEYS) {
    const raw = field(form, key);
    if (!raw) continue;
    const v = key === "policy_mode" ? parseMode(raw) : parseCount(raw);
    if (v === undefined) {
      return redirect("/settings", { err: `Invalid value for ${SETTING_LABELS[key]}: ${raw}` });
    }
    (next as Record<string, Settings[SettingKey]>)[key] = v;
  }
  await ctx.env.SETTINGS.put(SETTINGS_KEY, JSON.stringify(next));
  return redirect("/settings", { msg: "Settings saved" });
}

function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 ** 2) return `${(n / 1024).toFixed(1)} KiB`;
  if (n < 1024 ** 3) return `${(n / 1024 ** 2).toFixed(1)} MiB`;
  return `${(n / 1024 ** 3).toFixed(2)} GiB`;
}

// --- one archived message, as dea-capture rendered it

// the message page frames its own rendered view; nothing else
const MESSAGE_PAGE_CSP =
  "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-src 'self'; frame-ancestors 'none'; base-uri 'none'";
const HEADER_LABELS = [
  ["subject", "Subject"],
  ["from", "From"],
  ["to", "To"],
  ["date", "Date"],
  ["list-id", "List-Id"],
] as const;
const REMOVED_LABELS: Record<string, string> = {
  pixels: "tracking pixels",
  unstored: "embeds not stored",
  links_removed: "links removed (trackers, unsubscribe and other per-reader links)",
  links_kept: "links kept, pointing where they lead",
};

function partsTable(meta: ViewMeta): Html {
  if (!meta.parts.length) return html`<p class="muted">None.</p>`;
  return html`<table><tr><th>Part</th><th>Type</th><th>Size</th><th></th></tr>
${meta.parts.map(
  (p, i) => html`<tr><td>${i + 1}</td><td><code>${p.contentType}</code></td><td>${formatBytes(p.bytes)}</td>
<td class="muted">${[p.disposition, p.filename, p.cid ? "embedded (cid)" : ""].filter(Boolean).join(" · ")}</td></tr>`,
)}</table>`;
}

async function messagePage(ctx: Ctx, sha: string): Promise<Response> {
  const { env } = ctx;
  const meta = await getViewMeta(env, sha);
  const [key, sizes] = await Promise.all([findMessage(env, sha, meta, ctx.url.searchParams.get("day")), issueSizes(env, sha)]);
  const eml = key ? await env.ARCHIVE.head(key) : null;
  if (!eml && !meta) return text(404, "not found");
  const m = key ? /^incoming\/archive\/(\d{4}-\d{2}-\d{2})\//.exec(key) : null;
  const user = eml?.customMetadata?.user ?? "";
  const tag = eml?.customMetadata?.tag ?? "";
  const emlBytes = eml?.size ?? meta?.sizes.eml ?? 0;
  const total = emlBytes + sizes.embeds.bytes + sizes.view.bytes + sizes.records.bytes;
  const subject = meta?.headers.subject || "(no subject)";
  const body = html`<p>${m ? html`Received ${m[1]}` : ""}${user ? html` for <a href="${userPath(user)}">${user}</a> <code>${tag || "(bare)"}</code>` : ""}
· <code title="${sha}">${sha.slice(0, 12)}</code>
${key ? html` · <a href="/messages/${sha}/source">view source</a> · <a href="/messages/${sha}/raw">download .eml</a>` : ""}
· <a href="/captures">all messages</a></p>
${
  meta
    ? html`<dl class="hdrs">${HEADER_LABELS.filter(([h]) => meta.headers[h]).map(
        ([h, label]) => html`<dt>${label}</dt><dd>${meta.headers[h]}</dd>`,
      )}</dl>
<p class="muted">As stored: every embedded file is our copy and every link goes where it leads, never through a
tracker; nothing loads from elsewhere and scripts don't run. Rendered ${meta.renderedAt?.toISOString().slice(0, 16).replace("T", " ") ?? "?"} UTC.
${Object.entries(meta.removed)
  .filter(([k]) => k in REMOVED_LABELS)
  .map(([k, v]) => `${v} ${REMOVED_LABELS[k]}`)
  .join("; ")}</p>
<iframe class="mail" src="/messages/${sha}/view/index.html" title="${subject}"
 sandbox="allow-same-origin allow-popups allow-popups-to-escape-sandbox" referrerpolicy="no-referrer"></iframe>`
    : html`<p class="msg warn">dea-capture hasn't rendered this message yet; it does so when it processes it, within
about 5 minutes of arrival. The source is below.</p>`
}

<h2>Size</h2>
<table><tr><th></th><th>Files</th><th>Size</th></tr>
<tr><th>The message (.eml)</th><td>1</td><td>${formatBytes(emlBytes)}</td></tr>
<tr><th>Embedded files</th><td>${sizes.embeds.count}</td><td>${formatBytes(sizes.embeds.bytes)}</td></tr>
<tr><th>Rendered view</th><td>${sizes.view.count}</td><td>${formatBytes(sizes.view.bytes)}</td></tr>
<tr><th>Link records and manifest</th><td>${sizes.records.count}</td><td>${formatBytes(sizes.records.bytes)}</td></tr>
<tr><th>Total</th><td>${1 + sizes.embeds.count + sizes.view.count + sizes.records.count}${sizes.complete ? "" : "+"}</td><td>${formatBytes(total)}</td></tr></table>
<p class="muted">Not counted: the captures of its links, which every message linking to the same URL shares
(see <a href="/">Storage</a> on the overview).</p>
${meta ? html`<h2>MIME parts</h2>${partsTable(meta)}` : ""}`;
  return page("/captures", subject, body, {
    email: ctx.email,
    msg: ctx.url.searchParams.get("msg"),
    err: ctx.url.searchParams.get("err"),
    headers: { "content-security-policy": MESSAGE_PAGE_CSP },
  });
}

// the archived .eml: as text (view source) or downloaded
async function messageRaw(ctx: Ctx, sha: string, download: boolean): Promise<Response> {
  const key = await findMessage(ctx.env, sha, await getViewMeta(ctx.env, sha), ctx.url.searchParams.get("day"));
  const obj = key ? await ctx.env.ARCHIVE.get(key) : null;
  return obj && key ? rawMail(obj, key, download) : text(404, "not found");
}

async function quarantinePage(ctx: Ctx): Promise<Response> {
  const items = await listQuarantine(ctx.env);
  return show(
    ctx,
    "Quarantine",
    html`<p class="muted">The ${items.length === 50 ? "50 most recent" : items.length} quarantined messages.
Promote moves a message to <code>incoming/archive/</code>. View source shows the raw mail as text; download saves the .eml, to open in a mail client, not a browser.</p>
${
  items.length === 0
    ? html`<p class="muted">Quarantine is empty.</p>`
    : html`<table><tr><th>Received</th><th>To</th><th>From / Subject</th><th>Size</th><th></th></tr>
${items.map(
  (i) => html`<tr><td>${i.uploaded.toISOString().slice(0, 16).replace("T", " ")}</td>
<td><code>${i.meta.rcpt_to ?? ""}</code></td>
<td>${i.from || i.meta.envelope_from || ""}<br><strong>${i.subject || "(no subject)"}</strong>${
    i.meta.envelope_from ? html`<br><span class="muted">envelope: ${i.meta.envelope_from}</span>` : ""
  }</td>
<td>${formatBytes(i.size)}</td>
<td><a href="/quarantine/raw?key=${encodeURIComponent(i.key)}&amp;view=source">view source</a> ·
<a href="/quarantine/raw?key=${encodeURIComponent(i.key)}">download</a>
<form class="inline" method="post" action="/quarantine/promote"><input type="hidden" name="key" value="${i.key}"><button>Promote</button></form></td></tr>`,
)}</table>`
}`,
  );
}

async function quarantineRaw(ctx: Ctx): Promise<Response> {
  const key = ctx.url.searchParams.get("key") ?? "";
  const obj = await getQuarantined(ctx.env, key);
  if (!obj) return text(404, "not found");
  return rawMail(obj, key, ctx.url.searchParams.get("view") !== "source");
}

async function promoteMessage(ctx: Ctx): Promise<Response> {
  const key = field(await ctx.request.formData(), "key");
  const dest = await promote(ctx.env, key, ctx.email);
  if (!dest) return redirect("/quarantine", { err: "Message not found" });
  console.log(JSON.stringify({ evt: "promoted", from: key, to: dest, by: ctx.email }));
  return redirect("/quarantine", { msg: `Promoted to ${dest}` });
}

async function retryCaptures(ctx: Ctx): Promise<Response> {
  const form = await ctx.request.formData();
  const fields = Object.fromEntries(["scope", "sha", "kind", "status", "back"].map((k) => [k, field(form, k)]));
  const back = (CAPTURE_STATES as readonly string[]).includes(fields.back) ? `/captures?state=${fields.back}` : "/captures";
  const req = parseRetryRequest(fields);
  if (typeof req === "string") return redirect(back, { err: req });
  await requestRetry(ctx.env, req);
  console.log(JSON.stringify({ evt: "retry_requested", ...req, by: ctx.email }));
  const what =
    req.scope === "message" ? `message ${req.sha.slice(0, 12)}` : `${req.status} ${URL_KIND_LABELS[req.kind].toLowerCase()}`;
  return redirect(back, { msg: `Retry queued for ${what}; dea-capture picks it up within about 5 minutes` });
}

// --- routing

// CSRF guard for POSTs. Sec-Fetch-Site is set by the browser and can't be
// forged by a page; it covers browsers/extensions that send `Origin: null`
function sameOrigin(request: Request, url: URL): boolean {
  return (
    request.headers.get("origin") === url.origin ||
    request.headers.get("sec-fetch-site") === "same-origin"
  );
}

async function route(ctx: Ctx): Promise<Response> {
  const { request, url } = ctx;
  const path = url.pathname.replace(/\/+$/, "") || "/";
  const method = request.method;
  const userMatch = /^\/users\/([a-z0-9.-]+)(\/active|\/tags)?$/.exec(path);
  const msgMatch = /^\/messages\/([0-9a-f]{64})(?:\/(source|raw)|\/view\/(.+))?$/.exec(path);

  if (method === "GET" || method === "HEAD") {
    if (path === "/") return overview(ctx);
    if (path === "/users") return usersPage(ctx);
    if (path === "/subscriptions") return subscriptionsPage(ctx);
    if (path === "/settings") return settingsPage(ctx);
    if (path === "/quarantine") return quarantinePage(ctx);
    if (path === "/captures") return capturesPage(ctx);
    if (path === "/quarantine/raw") return quarantineRaw(ctx);
    if (msgMatch) {
      const [, sha, raw, file] = msgMatch;
      if (file !== undefined) return (await viewFile(ctx.env, sha, file)) ?? text(404, "not found");
      if (raw) return messageRaw(ctx, sha, raw === "raw");
      return messagePage(ctx, sha);
    }
    if (userMatch && !userMatch[2]) {
      const user = userMatch[1];
      if (!isValidUser(user)) return text(404, "not found");
      return userPage(ctx, user);
    }
    return text(404, "not found");
  }

  if (method === "POST") {
    if (!sameOrigin(request, url)) return text(403, "cross-origin request refused");
    if (path === "/users") return addUser(ctx);
    if (path === "/settings") return saveSettings(ctx);
    if (path === "/quarantine/promote") return promoteMessage(ctx);
    if (path === "/captures/retry") return retryCaptures(ctx);
    if (userMatch && userMatch[2]) {
      const user = userMatch[1];
      if (!isValidUser(user)) return text(404, "not found");
      return userMatch[2] === "/active" ? setActive(ctx, user) : saveTag(ctx, user);
    }
    return text(404, "not found");
  }

  return text(405, "method not allowed");
}

export async function handleAdmin(
  request: Request,
  env: Env,
  auth: Authenticator = authenticate,
): Promise<Response> {
  const result = await auth(request, env);
  if (!result.ok) {
    console.log(JSON.stringify({ evt: "admin_denied", reason: result.reason }));
    return text(403, "forbidden");
  }
  const ctx: Ctx = { request, env, url: new URL(request.url), email: result.email };
  try {
    return await route(ctx);
  } catch (err) {
    console.log(JSON.stringify({ evt: "admin_error", path: ctx.url.pathname, error: String(err) }));
    return text(500, "internal error");
  }
}
