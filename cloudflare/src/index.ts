// ingest worker:
// no parsing beyond the recipient local-part:
//   1. validate recipient against policy (default-deny)
//   2. enforce coarse daily caps (KV counters, checked before buffering)
//   3. write the raw RFC 5322 message to R2 under a content-addressed key
//   4. forward a permanent backup copy to a verified external mailbox
//
// Failure semantics:
//   - setReject(...)  -> permanent SMTP rejection, sender gives up
//   - throw           -> transient failure, well-behaved senders retry later
//   - R2 put is authoritative; backup-forward failures are logged, never fatal
//
// fetch() serves the admin dashboard (src/admin/) and, on its own hostname, the
// subscription API (src/api/); never on the mail path

import { archiveDomain } from "./addresses";
import { handleAdmin } from "./admin/app";
import { handleApi } from "./api/app";
import type { Env } from "./env";
import {
  decide,
  envelopeFromDomain,
  parseRecipient,
  recipientDomain,
  type TagRecord,
  type UserRecord,
} from "./policy";
import { loadOverrides, resolveSettings } from "./settings";

export type { Env };

async function sha256Hex(buf: ArrayBuffer): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", buf);
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

// Approximate attempted-delivery counters: KV reads/writes are not atomic and
// are eventually consistent. Concurrent bursts can undercount without a bound.
async function dailyCount(kv: KVNamespace, scope: string, day: string) {
  const key = `rl:${scope}:${day}`;
  const current = parseInt((await kv.get(key)) ?? "0", 10) || 0;
  return { key, current };
}

const handler = {
  async email(message: ForwardableEmailMessage, env: Env, _ctx: ExecutionContext) {
    // Scope ingestion to the configured subdomain
    // Cloudflare has no per-subdomain catch-all
    // so apex or other-subdomain mail can arrive here too
    // reject anything not addressed to ARCHIVE_DOMAIN
    const domain = archiveDomain(env);
    if (!domain) throw new Error("ARCHIVE_DOMAIN is required; transient configuration failure");
    if (recipientDomain(message.to) !== domain) {
      message.setReject("no such recipient");
      return;
    }

    const rec = parseRecipient(message.to);
    if (!rec) {
      message.setReject("no such recipient");
      return;
    }

    // KV override > wrangler.toml var > default
    const { settings } = resolveSettings(env, await loadOverrides(env.SETTINGS));

    // Size gate before buffer the body
    if (message.rawSize > settings.max_raw_bytes) {
      message.setReject("message too large");
      return;
    }

    const userRecord = await env.USERS.get<UserRecord>(rec.user, "json");
    const tagRecord = rec.tag
      ? await env.TAGS.get<TagRecord>(`${rec.user}/${rec.tag}`, "json")
      : null;

    const decision = decide({
      rec,
      userRecord,
      tagRecord,
      mode: settings.policy_mode,
      envelopeFromDomain: envelopeFromDomain(message.from),
    });

    if (decision.action === "reject") {
      message.setReject(decision.reason);
      return;
    }

    // Rate limits
    // DEFER (throw) rather than reject
    // senders retry once the daily window resets
    const scope = decision.tag
      ? `tag:${decision.user}/${decision.tag}`
      : `untagged:${decision.user}`;
    const cap = decision.tag ? settings.tag_daily_cap : settings.untagged_daily_cap;
    const day = new Date().toISOString().slice(0, 10);
    const [global, recipient] = await Promise.all([
      dailyCount(env.RATELIMIT, "global", day),
      dailyCount(env.RATELIMIT, scope, day),
    ]);
    if (global.current >= settings.global_daily_cap) {
      throw new Error("over global daily cap; transient, senders retry");
    }
    if (recipient.current >= cap) {
      throw new Error("over recipient daily cap; transient, senders retry");
    }
    await Promise.all([global, recipient].map(({ key, current }) =>
      env.RATELIMIT.put(key, String(current + 1), { expirationTtl: 2 * 24 * 3600 }),
    ));

    // Newsletters are small; hopefully....
    // content-addressed key makes sender retries idempotent
    const raw = await new Response(message.raw).arrayBuffer();
    const id = await sha256Hex(raw);
    const key = `incoming/${decision.disposition}/${day}/${id}.eml`;

    await env.ARCHIVE.put(key, raw, {
      httpMetadata: { contentType: "message/rfc822" },
      customMetadata: {
        user: decision.user,
        tag: decision.tag ?? "",
        envelope_from: message.from,
        rcpt_to: message.to,
        received_at: new Date().toISOString(),
        ingest_source: "live",
      },
    });

    // forward backup copy to an external mailbox
    // the R2 object above is authoritative
    // so a forward failure must never
    // bounce the message
    // Failures are logged but kinda ignored
    try {
      await message.forward(env.BACKUP_MAILBOX);
    } catch (err) {
      console.log(
        JSON.stringify({ evt: "backup_forward_failed", key, error: String(err) }),
      );
    }

    console.log(
      JSON.stringify({
        evt: "ingested",
        key,
        user: decision.user,
        tag: decision.tag,
        disposition: decision.disposition,
        bytes: message.rawSize,
      }),
    );
  },

  async fetch(request: Request, env: Env): Promise<Response> {
    // each hostname gets one handler: the dashboard is never served on the
    // API's hostname (which has no Access app), nor the API on the dashboard's
    const api = (env.API_HOSTNAME ?? "").trim().toLowerCase();
    if (api && new URL(request.url).hostname === api) return handleApi(request, env);
    return handleAdmin(request, env);
  },
} satisfies ExportedHandler<Env>;

export default handler;
