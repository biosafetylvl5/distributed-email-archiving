# AGENTS.md

Notes for coding agents working in this repo. User-facing docs are in `README.md`.

## What this is

The Cloudflare edge adapter for a newsletter archive. Mail to a subdomain hits
a zone-wide Email Routing catch-all → ingest Worker, which applies a
default-deny recipient policy, enforces daily caps, writes the raw `.eml` to R2,
and forwards a backup copy. The processor, publishing pass, and site shown in
the README diagram are **not in this repo** — don't add references to them as
if they exist.

## Layout

All code lives in `cloudflare/`:

| Path                     | Purpose                                                        |
| ------------------------ | -------------------------------------------------------------- |
| `src/index.ts`           | Worker `email()` handler: domain scope, size gate, caps, R2 put, backup forward |
| `src/policy.ts`          | Pure policy: `parseRecipient`, `decide`, domain helpers        |
| `test/policy.test.ts`    | vitest tests for `policy.ts`                                   |
| `*.tf`                   | Terraform (Cloudflare provider `~> 4.52`): R2 buckets, KV namespaces, routing rules |
| `*.sh`                   | Admin scripts wrapping `wrangler kv` (users, tags, export) and `mirror.sh` (rclone) |
| `wrangler.toml.example`  | Template for the git-ignored `wrangler.toml`                   |

## Commands

Run from `cloudflare/`:

```sh
npm install
npm test            # vitest
npm run typecheck   # tsc --noEmit
terraform fmt -check && terraform init -backend=false && terraform validate
```

Never run `terraform apply`, `npm run deploy`, or the `*ctl.sh` scripts — they act on live infrastructure.

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
