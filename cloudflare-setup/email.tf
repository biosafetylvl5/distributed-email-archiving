# permanent backup destination
# forwards are best-effort in the Worker, so mail still archives if it fails
resource "cloudflare_email_routing_address" "backup" {
  account_id = var.account_id
  email      = var.backup_mailbox
}

# Zone-wide catch-all -> ingest worker
# catches every unmatched address for the
# zone, including the subdomain
resource "cloudflare_email_routing_catch_all" "all" {
  zone_id = var.zone_id
  name    = "catch-all to ingest worker (default-deny policy + domain scope live in the Worker)"
  enabled = true

  matcher {
    type = "all"
  }

  action {
    type  = "worker"
    value = [var.ingest_worker_name]
  }
}

resource "cloudflare_email_routing_rule" "forward" {
  for_each = var.email_forwards

  zone_id = var.zone_id
  name    = "forward ${each.key} -> ${each.value}"
  enabled = true

  matcher {
    type  = "literal"
    field = "to"
    value = each.key
  }

  action {
    type  = "forward"
    value = [each.value]
  }

  # The destination must be registered before a rule can forward to it.
  depends_on = [cloudflare_email_routing_address.forward_dest]
}
