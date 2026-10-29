# distributed-email-archiving

Archives email newsletters as raw `.eml` plus a browsable HTML copy with remote dependencies.

**Status:** this repo currently contains only the Cloudflare edge adapter
(Email Routing + ingest Worker + R2/KV via Terraform), its admin scripts, and an admin dashboard,
plus the first piece of the processor: [`processor/`](processor/README.md) (`dea-capture`), which
works out which linked and embedded content to archive without acting as the subscriber.
It doesn't fetch anything yet. Everything else after `S3 incoming/` below is the goal
architecture and not yet published.

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
`POLICY_MODE` is set in `wrangler.toml` and can be overridden from the admin dashboard.

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

4. **Add a user** from the [admin dashboard](#admin-dashboard), or with the scripts
   (run from `cloudflare/`, since wrangler reads `wrangler.toml` from the current directory):
   ```sh
   ./userctl.sh add john.smith
   ./tagctl.sh approve john.smith ramen_weekly mail.ramenweekly.example
   ```

## Admin dashboard

The ingest Worker also serves a small dashboard (`src/admin/`) for:

- **Users:** list, add, disable or enable
- **Subscriptions:** approve, mark pending, block or publish tags; set a name, description, sender domain, sign-up link and private note
- **Settings:** override `POLICY_MODE`, the size limit and the daily caps without redeploying
  (stored in the `SETTINGS` KV namespace; blank falls back to `wrangler.toml`)
- **Quarantine:** the 50 most recent quarantined messages, with view source (as plain text),
  download and promote-to-archive
- **Captures:** per message, what `dea-capture` stored (embedded files by status, links by
  decision, how many URLs it queued), filterable to partial or pending messages; "Retry now"
  for every failed or retrying URL of a kind, or for one message's links (its failed redirects
  are followed again first). The Worker leaves a request under `captures/retry/` that the
  processor applies within 5 minutes; the page shows what the latest requests did
- **Messages:** each archived message as the processor rendered it, with its headers, MIME
  parts and what it takes up in storage, plus view source and download. Embeds are the
  stored copies and links point where they lead, never through a tracker. The view is served
  into a sandboxed frame whose CSP allows no scripts and nothing from another origin
- **Storage:** what the bucket holds by area and by kind of link capture, and its 100
  largest files, each linked to open it (video, audio, images and text in the browser,
  anything else as a download), to the page it captured, or to its message. Counted
  hourly by the processor
- **Overview:** totals, capture progress (including every linked URL by kind and status),
  storage by area and today's usage against the daily caps

The overview and captures pages read `dashboard/summary.json`, which the
[processor](processor/README.md) rebuilds every 5 minutes, and say how old it is. Listing
thousands of objects in the Worker would exceed the free plan's CPU limit. Until that object
exists, the bucket is listed live.

It is protected by [Cloudflare Access](https://developers.cloudflare.com/cloudflare-one/applications/),
and the Worker verifies the Access token itself. Until `ACCESS_TEAM_DOMAIN` and `ACCESS_AUD` are set,
it refuses every request.

Setup:

1. In the Cloudflare dashboard, open **Zero Trust** once to create your team domain
   (e.g. `myteam.cloudflareaccess.com`) and enable a login method (the one-time email PIN works).
2. Create the Access application with Terraform (in addition to the variables above):
   ```sh
   terraform apply ... -var admin_hostname=admin.example.org \
     -var 'admin_emails=["you@example.org"]'
   terraform output access_aud
   ```
3. In `wrangler.toml`:
   - add the `SETTINGS` namespace id from `terraform output kv_namespace_ids`
   - uncomment `routes` and set it to the same hostname
   - set `ACCESS_TEAM_DOMAIN`, `ACCESS_AUD`, and optionally `ADMIN_EMAILS`
4. `npm run deploy`, then open `https://admin.example.org`.

For local development, `npm run dev:admin` serves the dashboard at `http://localhost:8787`
against local simulated KV/R2, with auth skipped for localhost only.

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
