// minimal in-memory KV / R2 covering the calls the Worker makes

import type { Env } from "../src/env";

export class FakeKV {
  data = new Map<string, string>();
  pageSize = 1000;

  constructor(init: Record<string, unknown> = {}) {
    for (const [k, v] of Object.entries(init)) {
      this.data.set(k, typeof v === "string" ? v : JSON.stringify(v));
    }
  }

  async get(key: string, opts?: string | { type?: string }): Promise<unknown> {
    const v = this.data.get(key);
    if (v === undefined) return null;
    const type = typeof opts === "string" ? opts : opts?.type;
    return type === "json" ? JSON.parse(v) : v;
  }

  async put(key: string, value: string): Promise<void> {
    this.data.set(key, value);
  }

  async delete(key: string): Promise<void> {
    this.data.delete(key);
  }

  async list(opts: { prefix?: string; cursor?: string } = {}) {
    const keys = [...this.data.keys()].filter((k) => k.startsWith(opts.prefix ?? "")).sort();
    const start = opts.cursor ? Number(opts.cursor) : 0;
    const page = keys.slice(start, start + this.pageSize);
    const done = start + this.pageSize >= keys.length;
    return {
      keys: page.map((name) => ({ name })),
      list_complete: done,
      cursor: done ? undefined : String(start + this.pageSize),
    };
  }

  json(key: string): unknown {
    const v = this.data.get(key);
    return v === undefined ? undefined : JSON.parse(v);
  }
}

interface FakeObject {
  key: string;
  bytes: Uint8Array;
  size: number;
  uploaded: Date;
  httpMetadata?: Record<string, string>;
  customMetadata?: Record<string, string>;
}

function body(o: FakeObject, bytes: Uint8Array) {
  return {
    key: o.key,
    size: o.bytes.length,
    uploaded: o.uploaded,
    httpMetadata: o.httpMetadata,
    customMetadata: o.customMetadata,
    body: new Blob([bytes]).stream(),
    arrayBuffer: async () => bytes.slice().buffer,
  };
}

export class FakeR2 {
  objects = new Map<string, FakeObject>();

  async put(
    key: string,
    value: ArrayBuffer | string,
    opts: { httpMetadata?: Record<string, string>; customMetadata?: Record<string, string> } = {},
    uploaded = new Date(),
  ): Promise<void> {
    const bytes = typeof value === "string" ? new TextEncoder().encode(value) : new Uint8Array(value);
    this.objects.set(key, { key, bytes, size: bytes.byteLength, uploaded, ...opts });
  }

  async get(key: string, opts?: { range?: { offset: number; length: number } }) {
    const o = this.objects.get(key);
    if (!o) return null;
    const r = opts?.range;
    return body(o, r ? o.bytes.slice(r.offset, r.offset + r.length) : o.bytes);
  }

  async delete(key: string): Promise<void> {
    this.objects.delete(key);
  }

  async list(opts: { prefix?: string; delimiter?: string; cursor?: string } = {}) {
    const prefix = opts.prefix ?? "";
    const keys = [...this.objects.keys()].filter((k) => k.startsWith(prefix)).sort();
    if (opts.delimiter) {
      const prefixes = new Set<string>();
      const objects = [];
      for (const k of keys) {
        const i = k.indexOf(opts.delimiter, prefix.length);
        if (i >= 0) prefixes.add(k.slice(0, i + 1));
        else objects.push(this.objects.get(k)!);
      }
      return { objects, delimitedPrefixes: [...prefixes], truncated: false };
    }
    return { objects: keys.map((k) => this.objects.get(k)!), delimitedPrefixes: [], truncated: false };
  }
}

export interface FakeEnv {
  env: Env;
  users: FakeKV;
  tags: FakeKV;
  ratelimit: FakeKV;
  settings: FakeKV;
  archive: FakeR2;
}

export function fakeEnv(vars: Partial<Env> = {}, withSettings = true): FakeEnv {
  const users = new FakeKV();
  const tags = new FakeKV();
  const ratelimit = new FakeKV();
  const settings = new FakeKV();
  const archive = new FakeR2();
  const env = {
    USERS: users,
    TAGS: tags,
    RATELIMIT: ratelimit,
    SETTINGS: withSettings ? settings : undefined,
    ARCHIVE: archive,
    BACKUP_MAILBOX: "backup@example.net",
    ARCHIVE_DOMAIN: "mail.example.org",
    ...vars,
  } as unknown as Env;
  return { env, users, tags, ratelimit, settings, archive };
}
