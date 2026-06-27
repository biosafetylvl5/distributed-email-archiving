# newsletter-archive

Archives email newsletters as raw `.eml` plus a browsable HTML copy with remote dependencies.

Goal architecture:

```
sender ──► EDGE ADAPTER ──────────────────► S3 incoming/   (any S3-compatible
            a) Cloudflare Email Routing+Worker      │            store: R2, MinIO,
            b) self-hosted smtpd (MX → your box)    ▼            NAS, Garage, AWS)
                                                processor ── every 15 min
                                                    │        (container, anywhere)
                                                    ▼
                                              S3 archive/ ──► scripts/mirror.sh ──► onsite NAS
                                                    │
                            publish pass (sanitized, post-embargo) ──► public S3
                                                    │
                                          collection indexes ──► 11ty static site
```

## Address scheme

Mail is received at a dedicated **subdomain** (e.g. `mail.example.org`) 
and labeled with users and tags:

| Address                                | Behavior                                              |
| -------------------------------------- | ----------------------------------------------------- |
| `john.smith+ramen_weekly@mail.example.org`  | archived, if the tag is approved otherwise quarantined                |
| `john.smith__ramen_weekly@mail.example.org` | same as above  |
| `john.smith@mail.example.org`               | quarantined|
| something without a registered user | rejected and never stored              |


## Initial Setup

1. **Manual Cloudflare Setup**:
    a. Enable Email Routing for the apex domain (eg. `example.org`)
    b. Add your subdomain  (eg. `mail.example.org`)
    c. Enable subaddressing for the subdomain
    d. Get your `zone_id` and your `account_id`
    e. Generate an API token

2. **Terraform Cloudflare Setup** from the CLI:
   a. Set CLOUDFLARE_API_TOKEN to your API token
   a. run the following:
   ```sh
   cd cloudflare_setup
   terraform init
   terraform apply -var account_id=<your-account-id> \
     -var zone_id=<your-zone-id-for-apex-domain> \
     -var email_domain=<your email domain, eg. mail.example.org> \
     -var backup_mailbox=<a backup email to send emails to for secondary storage, eg. newsletter-backup@your-provider.example>
   ```
   b. Click the verification link for the backup mailbox and re-run
   c. Get your keys for wrangler by running `terraform output`
3. **Deploy Cloudflare Worker:** 
   a. paste the KV namespace ids from `terraform output` into
   `worker/wrangler.toml` and fill out the other missing fields
   b. run the following to deploy the worker:
   ```sh
   cd worker && npm install && npm test && npm run deploy
   ```
4. **Add a user**
   ```sh
   scripts/userctl.sh add john.smith
   scripts/tagctl.sh approve john.smith ramen_weekly mail.ramenweekly.example
   ```
