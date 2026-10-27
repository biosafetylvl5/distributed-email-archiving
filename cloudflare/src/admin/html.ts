// tiny HTML templating: every interpolated value is escaped unless it is
// already an Html fragment, so KV contents and mail headers can't inject markup

export class Html {
  constructor(readonly value: string) {}
  toString(): string {
    return this.value;
  }
}

export function esc(s: string): string {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function render(v: unknown): string {
  if (v instanceof Html) return v.value;
  if (Array.isArray(v)) return v.map(render).join("");
  if (v === null || v === undefined || v === false) return "";
  return esc(String(v));
}

export function html(strings: TemplateStringsArray, ...values: unknown[]): Html {
  let out = strings[0];
  values.forEach((v, i) => {
    out += render(v) + strings[i + 1];
  });
  return new Html(out);
}

const SECURITY_HEADERS = {
  "content-security-policy":
    "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
  "x-content-type-options": "nosniff",
  "x-frame-options": "DENY",
  // not "no-referrer": that makes browsers send `Origin: null` on our own form
  // POSTs, which the same-origin check in app.ts would refuse
  "referrer-policy": "same-origin",
  "cache-control": "no-store",
};

export function securityHeaders(extra: Record<string, string> = {}): Headers {
  return new Headers({ ...SECURITY_HEADERS, ...extra });
}

const NAV = [
  ["/", "Overview"],
  ["/users", "Users"],
  ["/subscriptions", "Subscriptions"],
  ["/settings", "Settings"],
  ["/quarantine", "Quarantine"],
  ["/captures", "Captures"],
];

const CSS = `
body{font:15px/1.45 system-ui,sans-serif;margin:0;color:#222;background:#fafafa}
header{background:#2b2d42;color:#fff;padding:.6rem 1.2rem;display:flex;gap:1.2rem;align-items:center;flex-wrap:wrap}
header a{color:#fff;text-decoration:none}header a.on{text-decoration:underline}
header .who{margin-left:auto;opacity:.8;font-size:.9em}
main{padding:1rem 1.2rem;max-width:72rem}
table{border-collapse:collapse;width:100%;background:#fff;margin:.5rem 0 1.2rem}
th,td{border:1px solid #ddd;padding:.35rem .5rem;text-align:left;vertical-align:top}
th{background:#f0f0f0}
.msg{padding:.5rem .8rem;border-radius:4px;margin:.5rem 0}
.ok{background:#e3f6e8;border:1px solid #9ad0a8}.err{background:#fde8e8;border:1px solid #e2a0a0}
.warn{background:#fff6dd;border:1px solid #e5cf8a}
.muted{color:#777}code{font-size:.9em}
input[type=text],input[type=number],input[type=url],textarea{padding:.2rem .3rem;font:inherit}
.tagform{display:grid;gap:.3rem;margin-top:.3rem;min-width:22rem}
.tagform label{font-size:.9em;color:#555}
.tagform input[type=text],.tagform input[type=url],.tagform textarea{display:block;width:100%;box-sizing:border-box;color:#222}
.desc{white-space:pre-line}
.chart td.bars{width:50%}.bar{display:inline-block;height:.8em;vertical-align:middle}
.bar.a{background:#3d8f5a}.bar.q{background:#d9a520}
form.inline{display:inline}
.status-approved{color:#17703a}.status-blocked{color:#b02a2a}.status-pending{color:#8a6d00}
`;

export function page(
  path: string,
  title: string,
  body: Html,
  opts: { email?: string; msg?: string | null; err?: string | null; status?: number } = {},
): Response {
  const nav = NAV.map(
    ([href, label]) =>
      html`<a href="${href}" class="${(href === "/" ? path === "/" : path.startsWith(href)) ? "on" : ""}">${label}</a>`,
  );
  const doc = html`<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>${title} · newsletter admin</title><style>${new Html(CSS)}</style></head>
<body><header><strong>newsletter admin</strong>${nav}<span class="who">${opts.email ?? ""}</span></header>
<main><h1>${title}</h1>
${opts.msg ? html`<div class="msg ok">${opts.msg}</div>` : ""}
${opts.err ? html`<div class="msg err">${opts.err}</div>` : ""}
${body}</main></body></html>`;
  return new Response(doc.value, {
    status: opts.status ?? 200,
    headers: securityHeaders({ "content-type": "text/html; charset=utf-8" }),
  });
}
