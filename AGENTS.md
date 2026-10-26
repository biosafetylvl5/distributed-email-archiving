# AGENTS.md

Notes for coding agents working in this repo. User-facing docs are in `README.md`.

## What this is

The Cloudflare edge adapter for a newsletter archive. Mail to a subdomain hits
a zone-wide Email Routing catch-all → ingest Worker, which applies a
default-deny recipient policy, enforces daily caps, writes the raw `.eml` to R2,
and forwards a backup copy. The same Worker's `fetch()` serves an admin
dashboard behind Cloudflare Access. The processor, publishing pass, and site shown in
the README diagram are **not in this repo** — don't add references to them as
if they exist.

## Layout

All code lives in `cloudflare/`:

| Path                     | Purpose                                                        |
| ------------------------ | -------------------------------------------------------------- |
| `src/index.ts`           | Worker `email()` handler: domain scope, size gate, caps, R2 put, backup forward; `fetch()` → admin |
| `src/policy.ts`          | Pure policy: `parseRecipient`, `decide`, name validators, domain helpers |
| `src/settings.ts`        | Settings resolution: SETTINGS KV override > `wrangler.toml` var > default |
| `src/env.ts`             | `Env` bindings type shared by ingest and admin                 |
| `src/admin/`             | Dashboard: `auth.ts` (Access JWT), `app.ts` (routes/pages), `store.ts` (KV/R2), `html.ts`, `mail.ts` |
| `test/`                  | vitest; `fakes.ts` has in-memory KV/R2 used by the other tests |
| `*.tf`                   | Terraform (Cloudflare provider `~> 4.52`): R2 buckets, KV namespaces, routing rules, Access app |
| `*.sh`                   | Admin scripts wrapping `wrangler kv` (users, tags, export) and `mirror.sh` (rclone) |
| `wrangler.toml.example`  | Template for the git-ignored `wrangler.toml`                   |

## Commands

Run from `cloudflare/`:

```sh
npm install
npm test            # vitest
npm run typecheck   # tsc --noEmit
npm run dev:admin   # local dashboard on simulated KV/R2, auth skipped for localhost
terraform fmt -check && terraform init -backend=false && terraform validate
```

Never run `terraform apply`, `npm run deploy`, `wrangler dev --remote`, or the `*ctl.sh` scripts — they act on live infrastructure.
Run wrangler under Node, not Bun: `wrangler dev` under Bun answers every request with a 503.

## Invariants

- **Default-deny.** Anything `decide()` doesn't explicitly accept is rejected.
  New address forms or policy branches need tests in `test/policy.test.ts`.
- **Failure semantics** (see the header of `src/index.ts`):
  `setReject` = permanent rejection; `throw` = transient, sender retries.
  Rate limits defer (throw), they never reject.
- **R2 is authoritative.** Backup-forward failures are logged, never fatal.
- The Worker does no MIME parsing; it only reads the recipient local part and
  envelope sender. Keep heavy work out of the mail delivery path.
- User names may not contain `_`, so `__` is an unambiguous tag separator.
  Addresses are lowercased before matching.
- **Admin auth fails closed.** Every dashboard request goes through
  `authenticate()`; POSTs also require a same-origin `Origin` header. Don't add
  routes that bypass `handleAdmin`, and don't widen `ALLOW_LOCAL_NO_AUTH` beyond localhost.
- **Escape all output.** Build HTML with the `html` template in `src/admin/html.ts`,
  which escapes interpolations; mail headers and KV values are untrusted.
  Never render stored mail inline; quarantine downloads are `attachment` + octet-stream.
- KV records (`USERS`, `TAGS`) are shared with the `*ctl.sh` scripts. Edits are
  read-modify-write and must preserve unknown fields.
- Invalid settings fall back to the next layer, ending at strict defaults; never fail open.
- `policy.ts` comments say "KEEP IN PARITY" with a Python port that lives
  outside this repo. Mention policy behavior changes in your summary so the
  port can be updated.

## Don't commit

`wrangler.toml` (real KV ids, domain, mailbox), `.wrangler/`, `node_modules/`,
`.terraform/`, `*.tfstate*`, `*.tfvars`, `tags.json`. Do commit
`.terraform.lock.hcl`. Use placeholders like `example.org` in docs and examples,
never real domains, mailboxes, or account ids.

## Style

Match the surrounding code: short lowercase comments, TypeScript `strict`,
2-space indent, `terraform fmt` output. Keep the README setup steps in sync
with any change to Terraform variables, outputs, or `wrangler.toml` fields.

## License

Apache-2.0. Contributions are under the same license.
