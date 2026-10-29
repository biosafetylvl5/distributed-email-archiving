// the bucket's largest files (dashboard/largest.json, written by dea-capture
// with its hourly storage count) and serving one stored capture or embed.
// Files are untrusted content: only video, audio, raster images and plain text
// are shown in the browser, under a sandbox CSP; anything else is downloaded.

import type { Env } from "../env";
import { securityHeaders } from "./html";

export const LARGEST_KEY = "dashboard/largest.json";

// link captures and stored embeds; not the records next to them (links.json
// and captures.json have the original, per-reader links)
const FILE_KEY_RE =
  /^(?:captures\/[0-9a-f]{2}\/[0-9a-f]{64}\/\d{8}T\d{6}Z\/[A-Za-z0-9._-][A-Za-z0-9._/-]{0,200}|issues\/[0-9a-f]{64}\/assets\/[0-9a-f]{64}(?:\.[a-z0-9]{1,10})?)$/;
const INLINE_RE = /^(?:video\/[a-z0-9.+-]+|audio\/[a-z0-9.+-]+|image\/(?:png|jpeg|gif|webp|avif)|text\/plain)$/;
const FILE_CSP = "sandbox; default-src 'none'; img-src 'self'; media-src 'self'; style-src 'unsafe-inline'; frame-ancestors 'none'";

export interface LargeFile {
  key: string;
  size: number;
  modified: Date | null;
  area: string;
  kind?: string;
  url?: string; // a link capture's URL
  sha?: string; // the message it belongs to
}

export interface Largest {
  computedAt: Date | null;
  files: LargeFile[];
}

export function servable(key: string): boolean {
  return FILE_KEY_RE.test(key) && !key.includes("..");
}

function str(v: unknown): string | undefined {
  return typeof v === "string" && v ? v : undefined;
}

function date(v: unknown): Date | null {
  const d = typeof v === "string" ? new Date(v) : null;
  return d && !Number.isNaN(d.getTime()) ? d : null;
}

// written by dea-capture, so checked
export function parseLargest(raw: string): Largest | null {
  let f: Record<string, unknown>;
  try {
    f = JSON.parse(raw);
  } catch {
    return null;
  }
  if (typeof f !== "object" || f === null || !Array.isArray(f.files)) return null;
  return {
    computedAt: date(f.computed_at),
    files: f.files
      .filter((x): x is Record<string, unknown> => typeof x === "object" && x !== null && typeof x.key === "string")
      .map((x) => {
        const url = str(x.url);
        const sha = str(x.sha);
        return {
          key: x.key as string,
          size: typeof x.size === "number" && x.size >= 0 ? x.size : 0,
          modified: date(x.modified),
          area: str(x.area) ?? "other",
          kind: str(x.kind),
          url: url && /^https?:\/\//.test(url) ? url : undefined,
          sha: sha && /^[0-9a-f]{64}$/.test(sha) ? sha : undefined,
        };
      }),
  };
}

export async function largest(env: Env): Promise<Largest | null> {
  const obj = await env.ARCHIVE.get(LARGEST_KEY);
  return obj ? parseLargest(await obj.text()) : null;
}

// a single "bytes=a-b", "bytes=a-" or "bytes=-n" of a file of `size` bytes,
// as offset and length; "unsatisfiable" past the end; null for the whole file
export function parseRange(h: string | null, size: number): { offset: number; length: number } | "unsatisfiable" | null {
  const m = h ? /^bytes=(\d*)-(\d*)$/.exec(h.trim()) : null;
  if (!m || (!m[1] && !m[2])) return null;
  if (!m[1]) {
    const n = Math.min(Number(m[2]), size);
    return n > 0 ? { offset: size - n, length: n } : "unsatisfiable";
  }
  const offset = Number(m[1]);
  if (offset >= size) return "unsatisfiable";
  const end = m[2] ? Math.min(Number(m[2]), size - 1) : size - 1;
  return end >= offset ? { offset, length: end - offset + 1 } : null;
}

export async function serveFile(env: Env, key: string, request: Request): Promise<Response | null> {
  if (!servable(key)) return null;
  const head = await env.ARCHIVE.head(key);
  if (!head) return null;
  const range = parseRange(request.headers.get("range"), head.size);
  if (range === "unsatisfiable") {
    return new Response(null, { status: 416, headers: securityHeaders({ "content-range": `bytes */${head.size}` }) });
  }
  const obj = await env.ARCHIVE.get(key, range ? { range } : undefined);
  if (!obj) return null;
  const type = obj.httpMetadata?.contentType?.split(";")[0].trim().toLowerCase() ?? "";
  const inline = INLINE_RE.test(type);
  const name = key.split("/").pop() ?? "file";
  const headers = securityHeaders({
    "content-type": inline ? (obj.httpMetadata?.contentType ?? type) : "application/octet-stream",
    "content-disposition": `${inline ? "inline" : "attachment"}; filename="${name}"`,
    "content-security-policy": FILE_CSP,
    "accept-ranges": "bytes",
    "content-length": String(range ? range.length : head.size),
    "cache-control": "private, max-age=86400", // a capture is never rewritten
  });
  if (!range) return new Response(obj.body, { headers });
  headers.set("content-range", `bytes ${range.offset}-${range.offset + range.length - 1}/${head.size}`);
  return new Response(obj.body, { status: 206, headers });
}
