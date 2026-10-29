// archived messages, as dea-capture renders them for the dashboard under
// issues/<sha>/view/ (processor/src/dea_capture/view.py): the HTML with every
// embed pointing at the stored copy and every link at where it leads, never
// at a tracker. Served here into a sandboxed frame whose CSP allows nothing
// but this origin, so even a rewrite dea-capture missed loads nothing.

import type { Env } from "../env";
import { securityHeaders } from "./html";

export const SHA_RE = /^[0-9a-f]{64}$/;
const ARCHIVE_KEY_RE = /^incoming\/archive\/\d{4}-\d{2}-\d{2}\/([0-9a-f]{64})\.eml$/;
export const DAY_RE = /^\d{4}-\d{2}-\d{2}$/;
// a message with more objects than this is shown as a lower bound
const ISSUE_LIST_PAGES = 5;
const VIEW_FILE_RE = /^(?:index\.html|(?:assets|css)\/[0-9a-f]{64}(?:\.[a-z0-9]{1,10})?)$/;

// for the message's own content: no scripts, no forms, nothing from elsewhere;
// links open in a new tab, outside the sandbox
export const VIEW_CSP =
  "sandbox allow-same-origin allow-popups allow-popups-to-escape-sandbox; default-src 'none'; " +
  "img-src 'self' data:; style-src 'self' 'unsafe-inline'; font-src 'self' data:; media-src 'self'; " +
  "frame-ancestors 'self'; base-uri 'none'; form-action 'none'";
// raw mail as text: nothing at all
const SOURCE_CSP = "sandbox; default-src 'none'; frame-ancestors 'none'";

export interface MessagePart {
  contentType: string;
  bytes: number;
  filename?: string;
  disposition?: string;
  cid: boolean;
}

export interface ViewMeta {
  renderedAt: Date | null;
  source: string; // incoming/archive/... key, or "" if unknown
  headers: Record<string, string>; // lowercase names
  body: "html" | "text";
  parts: MessagePart[];
  sizes: { eml: number; assets: number; assetFiles: number };
  removed: Record<string, number>;
}

function num(v: unknown): number {
  return typeof v === "number" && Number.isFinite(v) ? v : 0;
}

function str(v: unknown): string {
  return typeof v === "string" ? v : "";
}

export function viewKey(sha: string, file: string): string {
  return `issues/${sha}/view/${file}`;
}

// the archive key a source names, if it is an archived message's
export function archiveKey(source: string, sha?: string): string | null {
  const key = source.replace(/^s3:\/\/[^/]+\//, "");
  const m = ARCHIVE_KEY_RE.exec(key);
  return m && (!sha || m[1] === sha) ? key : null;
}

// written by dea-capture, so checked
export function parseViewMeta(raw: string): ViewMeta | null {
  let m: Record<string, unknown>;
  try {
    m = JSON.parse(raw);
  } catch {
    return null;
  }
  if (typeof m !== "object" || m === null) return null;
  const at = str(m.rendered_at) ? new Date(str(m.rendered_at)) : null;
  const headers = typeof m.headers === "object" && m.headers ? (m.headers as Record<string, unknown>) : {};
  const sizes = typeof m.sizes === "object" && m.sizes ? (m.sizes as Record<string, unknown>) : {};
  const removed = typeof m.removed === "object" && m.removed ? (m.removed as Record<string, unknown>) : {};
  return {
    renderedAt: at && !Number.isNaN(at.getTime()) ? at : null,
    source: str(m.source),
    headers: Object.fromEntries(Object.entries(headers).filter(([, v]) => typeof v === "string")) as Record<string, string>,
    body: m.body === "text" ? "text" : "html",
    parts: (Array.isArray(m.parts) ? m.parts : [])
      .filter((p): p is Record<string, unknown> => typeof p === "object" && p !== null)
      .map((p) => ({
        contentType: str(p.content_type) || "application/octet-stream",
        bytes: num(p.bytes),
        filename: str(p.filename) || undefined,
        disposition: str(p.disposition) || undefined,
        cid: p.cid === true,
      })),
    sizes: { eml: num(sizes.eml), assets: num(sizes.assets), assetFiles: num(sizes.asset_files) },
    removed: Object.fromEntries(Object.entries(removed).map(([k, v]) => [k, num(v)]).filter(([, v]) => v)),
  };
}

export async function getViewMeta(env: Env, sha: string): Promise<ViewMeta | null> {
  const obj = await env.ARCHIVE.get(viewKey(sha, "meta.json"));
  return obj ? parseViewMeta(await obj.text()) : null;
}

// the message's archive key: from its view, else its status record, else the
// day it arrived (the dashboard links unprocessed messages with ?day=)
export async function findMessage(
  env: Env,
  sha: string,
  meta: ViewMeta | null,
  day?: string | null,
): Promise<string | null> {
  const fromView = meta ? archiveKey(meta.source, sha) : null;
  if (fromView) return fromView;
  const status = await env.ARCHIVE.head(`issue-status/${sha}.json`);
  const fromStatus = status ? archiveKey(status.customMetadata?.source ?? "", sha) : null;
  if (fromStatus) return fromStatus;
  if (!day || !DAY_RE.test(day)) return null;
  const key = `incoming/archive/${day}/${sha}.eml`;
  return (await env.ARCHIVE.head(key)) ? key : null;
}

export interface IssueSizes {
  embeds: { count: number; bytes: number };
  view: { count: number; bytes: number };
  records: { count: number; bytes: number }; // manifest, links, captures.json
  complete: boolean; // false: more objects than were listed
}

// what dea-capture stored for one message under issues/<sha>/
export async function issueSizes(env: Env, sha: string): Promise<IssueSizes> {
  const out: IssueSizes = {
    embeds: { count: 0, bytes: 0 },
    view: { count: 0, bytes: 0 },
    records: { count: 0, bytes: 0 },
    complete: true,
  };
  const prefix = `issues/${sha}/`;
  let cursor: string | undefined;
  for (let i = 0; i < ISSUE_LIST_PAGES; i++) {
    const page = await env.ARCHIVE.list({ prefix, cursor });
    for (const o of page.objects) {
      const rest = o.key.slice(prefix.length);
      const area = rest.startsWith("assets/") ? out.embeds : rest.startsWith("view/") ? out.view : out.records;
      area.count++;
      area.bytes += o.size;
    }
    cursor = page.truncated ? page.cursor : undefined;
    if (!cursor) return out;
  }
  out.complete = false;
  return out;
}

// a file of the rendered view: index.html, assets/<name> (the stored embeds,
// next to the view in issues/<sha>/assets/) or css/<name>
export async function viewFile(env: Env, sha: string, file: string): Promise<Response | null> {
  if (!SHA_RE.test(sha) || !VIEW_FILE_RE.test(file)) return null;
  const key = file.startsWith("assets/") ? `issues/${sha}/${file}` : viewKey(sha, file);
  const obj = await env.ARCHIVE.get(key);
  if (!obj) return null;
  return new Response(obj.body, {
    headers: securityHeaders({
      "content-type": obj.httpMetadata?.contentType ?? "application/octet-stream",
      "content-security-policy": VIEW_CSP,
      "x-frame-options": "SAMEORIGIN",
      "referrer-policy": "no-referrer",
      // the assets are named by their hash; the view is rendered again now and then
      "cache-control": file === "index.html" ? "private, no-cache" : "private, max-age=86400",
    }),
  });
}

// raw mail, archived or quarantined: shown as text, or downloaded
export function rawMail(obj: R2ObjectBody, key: string, download: boolean): Response {
  const name = key.split("/").pop() ?? "message.eml";
  return new Response(obj.body, {
    headers: securityHeaders(
      download
        ? {
            // octet-stream + attachment so the browser never renders untrusted mail on this origin
            "content-type": "application/octet-stream",
            "content-disposition": `attachment; filename="${name}"`,
          }
        : { "content-type": "text/plain; charset=utf-8", "content-security-policy": SOURCE_CSP },
    ),
  });
}
