# Per-IP rate limit on the subscription API (requests to /v1/*), in front of the
# Worker, so floods and token guessing don't reach it. The Worker's own daily
# caps on adding and editing subscriptions apply as well.
#
# Written for the Free plan, which allows one rate limiting rule matching on the
# path only, counted per IP over 10 seconds, and blocking for 10 seconds. It
# matches /v1/ on every hostname in the zone; on the admin hostname those paths
# are behind Access anyway. A zone has one http_ratelimit ruleset: if the zone
# already has one, import it rather than letting this create a second.

variable "api_rate_limit" {
  type        = number
  default     = 0
  description = "Requests to /v1/* each IP may make in 10 seconds before being blocked for 10 seconds; 0 for no rule."

  validation {
    condition     = var.api_rate_limit >= 0 && floor(var.api_rate_limit) == var.api_rate_limit
    error_message = "api_rate_limit must be a whole number, 0 for none."
  }
}

resource "cloudflare_ruleset" "api_ratelimit" {
  count       = var.api_rate_limit > 0 ? 1 : 0
  zone_id     = var.zone_id
  name        = "Subscription API rate limit"
  description = "Per-IP limit on the subscription API"
  kind        = "zone"
  phase       = "http_ratelimit"

  rules {
    description = "Subscription API: ${var.api_rate_limit} requests per 10 s per IP"
    expression  = "(http.request.uri.path contains \"/v1/\")"
    action      = "block"
    enabled     = true

    ratelimit {
      characteristics     = ["ip.src", "cf.colo.id"]
      period              = 10
      requests_per_period = var.api_rate_limit
      mitigation_timeout  = 10
    }
  }
}
