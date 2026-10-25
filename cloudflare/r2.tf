# Storage

# raw incoming .eml files
# incoming/ (raw, unprocessed)
# incoming/quarantine/ and incoming/failed/ for triage
resource "cloudflare_r2_bucket" "archive" {
  account_id = var.account_id
  name       = "newsletter-archive"
  location   = "WEUR"
}

# for published archives
resource "cloudflare_r2_bucket" "public" {
  account_id = var.account_id
  name       = "newsletter-archive-public"
  location   = "WEUR"
}

# you must manually cap the length that files stay in the quarantine
# eg. set incoming/quarantine to be 30 days
# if you wish!

# KV namespaces backing worker decisions
resource "cloudflare_workers_kv_namespace" "users" {
  account_id = var.account_id
  title      = "newsletter-users"
}

resource "cloudflare_workers_kv_namespace" "tags" {
  account_id = var.account_id
  title      = "newsletter-tags"
}

resource "cloudflare_workers_kv_namespace" "ratelimit" {
  account_id = var.account_id
  title      = "newsletter-ratelimit"
}
