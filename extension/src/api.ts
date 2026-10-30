// client for the archive's subscription API (cloudflare/src/api/app.ts)

import type { Config } from "./config";

export interface Subscription {
  tag: string;
  status: "approved" | "pending" | "blocked";
  name: string | null;
  description: string | null;
  signup_url: string | null;
  user_note: string | null;
  sender_domain: string | null;
  capture_limit_mb: number | null;
  source: "api" | "admin";
  created_at: string | null;
  updated_at: string | null;
  addresses: { plus: string; underscore: string };
}

export interface Me {
  user: string;
  archive_domain: string;
  token_id: string;
  limits: {
    subscriptions: number;
    subscriptions_max: number;
    added_today: number;
    added_per_day: number;
    new_status: "approved" | "pending";
  };
}

// another user's subscription matching a page: never whose it is
export interface Other {
  name: string;
  status: Subscription["status"];
  match: "page" | "site";
}

export interface Lookup {
  exact: Subscription[];
  same_site: Subscription[];
  others?: Other[]; // absent from older servers
}

// what users may set (the rest is the admin's)
export interface Editable {
  name?: string | null;
  description?: string | null;
  signup_url?: string | null;
  user_note?: string | null;
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
    readonly body: Record<string, unknown> = {},
  ) {
    super(message);
  }
}

export class Api {
  constructor(private readonly config: Pick<Config, "apiUrl" | "token">) {}

  private async request<T>(method: string, path: string, body?: unknown): Promise<T> {
    if (!this.config.apiUrl || !this.config.token) {
      throw new ApiError(0, "not_configured", "Set the API address and token in the extension's options.");
    }
    let res: Response;
    try {
      res = await fetch(new URL(path, this.config.apiUrl), {
        method,
        headers: {
          authorization: `Bearer ${this.config.token}`,
          ...(body === undefined ? {} : { "content-type": "application/json" }),
        },
        body: body === undefined ? undefined : JSON.stringify(body),
        credentials: "omit",
        cache: "no-store",
      });
    } catch (err) {
      throw new ApiError(0, "network", `Couldn't reach the archive (${String(err)}).`);
    }
    const data = (await res.json().catch(() => ({}))) as Record<string, unknown>;
    if (!res.ok) {
      const message = typeof data.message === "string" ? data.message : `HTTP ${res.status}`;
      throw new ApiError(res.status, String(data.error ?? "http"), message, data);
    }
    return data as T;
  }

  me(): Promise<Me> {
    return this.request("GET", "/v1/me");
  }

  async list(): Promise<Subscription[]> {
    return (await this.request<{ subscriptions: Subscription[] }>("GET", "/v1/subscriptions")).subscriptions;
  }

  lookup(url: string): Promise<Lookup> {
    return this.request("GET", `/v1/subscriptions/lookup?url=${encodeURIComponent(url)}`);
  }

  async create(fields: Editable & { tag?: string }): Promise<Subscription> {
    return (await this.request<{ subscription: Subscription }>("POST", "/v1/subscriptions", fields)).subscription;
  }

  async update(tag: string, fields: Editable, ifUpdatedAt: string | null): Promise<Subscription> {
    const path = `/v1/subscriptions/${encodeURIComponent(tag)}`;
    const body = { ...fields, if_updated_at: ifUpdatedAt };
    return (await this.request<{ subscription: Subscription }>("PATCH", path, body)).subscription;
  }
}
