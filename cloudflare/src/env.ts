import type { SettingsVars } from "./settings";

export interface Env extends SettingsVars {
  ARCHIVE: R2Bucket;
  USERS: KVNamespace;
  TAGS: KVNamespace;
  RATELIMIT: KVNamespace;
  // optional: runtime setting overrides written by the admin dashboard
  SETTINGS?: KVNamespace;
  BACKUP_MAILBOX: string;
  ARCHIVE_DOMAIN: string;

  // admin dashboard (Cloudflare Access)
  ACCESS_TEAM_DOMAIN?: string; // e.g. myteam.cloudflareaccess.com
  ACCESS_AUD?: string; // Access application audience tag
  ADMIN_EMAILS?: string; // optional comma-separated allowlist
  ALLOW_LOCAL_NO_AUTH?: string; // "true" skips auth for localhost only (wrangler dev)

  // subscription API for the browser extension (src/api/): served only on
  // this hostname, which has no Access app; off when unset
  API_HOSTNAME?: string; // e.g. mail-api.example.org
  TOKENS?: KVNamespace; // per-user API tokens
}
