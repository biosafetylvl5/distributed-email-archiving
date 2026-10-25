# distributed-email-archiving

Archives email newsletters as raw `.eml` plus a browsable HTML copy with remote dependencies.

**Status:** this repo currently contains only the Cloudflare edge adapter
(Email Routing + ingest Worker + R2/KV via Terraform) and its admin scripts.
Everything after `S3 incoming/` below is the goal architecture and not yet published.

Goal architecture:

```
sender ──► EDGE ADAPTER ──────────────────► S3 incoming/   (any S3-compatible
            a) Cloudflare Email Routing+Worker      │            store: R2, MinIO,
            b) self-hosted smtpd (MX → your box)    ▼            NAS, Garage, AWS)
                                                processor ── every 15 min
                                                    │        (container, anywhere)
                                                    ▼
                                              S3 archive/ ──► mirror.sh ──► onsite NAS
                                                    │
                            publish pass (sanitized, post-embargo) ──► public S3
                                                    │
                                          collection indexes ──► 11ty static site
```

## Address scheme

Mail is received at a dedicated **subdomain** (e.g. `mail.example.org`)
and labeled with users and tags:

| Address                                     | Behavior                                                          |
| ------------------------------------------- | ----------------------------------------------------------------- |
| `john.smith+ramen_weekly@mail.example.org`  | archived if the tag is approved, quarantined if it is pending     |
| `john.smith__ramen_weekly@mail.example.org` | same as above (fallback for signup forms that reject `+`)         |
| `john.smith@mail.example.org`               | quarantined                                                       |
| unknown tag                                 | `strict` mode: rejected; `quarantine` mode: quarantined           |
| blocked tag                                 | rejected                                                          |
| unregistered or disabled user               | rejected and never stored                                         |
| any other domain in the zone                | rejected (unless listed in `email_forwards`)                      |

If a tag has a `sender_domain`, mail from any other envelope sender domain is quarantined.
`POLICY_MODE` is set in `wrangler.toml`.

Accepted mail is written to the `newsletter-archive` R2 bucket under
`incoming/archive/` or `incoming/quarantine/`, and a copy is forwarded to the backup mailbox.

## Initial Setup

1. **Manual Cloudflare Setup**:
    1. Enable Email Routing for the apex domain (eg. `example.org`)
    2. Add your subdomain (eg. `mail.example.org`)
    3. Enable subaddressing for the subdomain
    4. Get your `zone_id` and your `account_id`
    5. Generate an API token

2. **Terraform Cloudflare Setup** from the CLI:
    1. Set `CLOUDFLARE_API_TOKEN` to your API token
    2. Run the following:
       ```sh
       cd cloudflare
       terraform init
       terraform apply -var account_id=<your-account-id> \
         -var zone_id=<your-zone-id-for-apex-domain> \
         -var email_domain=<your email domain, eg. mail.example.org> \
         -var backup_mailbox=<a backup email to send emails to for secondary storage, eg. newsletter-backup@your-provider.example>
       ```
       Optionally forward other addresses in the zone to a person instead of the Worker with
       `-var 'email_forwards={"me@example.org"="me@personal.example"}'`.
    3. Click the verification link for the backup mailbox (and any forward destinations) and re-run
    4. Get your KV namespace ids for wrangler by running `terraform output`

3. **Deploy Cloudflare Worker:**
    1. Create your local config (it is git-ignored):
       ```sh
       cd cloudflare
       cp wrangler.toml.example wrangler.toml
       ```
    2. Paste the KV namespace ids from `terraform output` into `wrangler.toml`
       and fill out `BACKUP_MAILBOX` and `ARCHIVE_DOMAIN`
    3. Deploy the worker:
       ```sh
       npm install && npm test && npm run deploy
       ```

4. **Add a user** (run from `cloudflare/`, since wrangler reads `wrangler.toml` from the current directory):
   ```sh
   ./userctl.sh add john.smith
   ./tagctl.sh approve john.smith ramen_weekly mail.ramenweekly.example
   ```

## Admin scripts

All in `cloudflare/`, run from that directory:

| Script           | Purpose                                                         |
| ---------------- | --------------------------------------------------------------- |
| `userctl.sh`     | add / disable users                                             |
| `tagctl.sh`      | approve / publish / block / show tags                           |
| `export-tags.sh` | dump the TAGS namespace as JSON (`./export-tags.sh > tags.json`) |
| `mirror.sh`      | rclone sync + verify between two S3 remotes (e.g. R2 → NAS)      |

## License

[Apache License 2.0](LICENSE)
