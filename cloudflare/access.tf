# Cloudflare Access in front of the admin dashboard (optional)
# the Worker also verifies the Access JWT itself and refuses all requests
# until ACCESS_TEAM_DOMAIN and ACCESS_AUD are set in wrangler.toml
# requires a Zero Trust organization (team domain), created once in the dashboard

variable "admin_hostname" {
  type        = string
  default     = ""
  description = "Hostname for the admin dashboard, e.g. admin.example.org. Empty skips creating the Access application."
}

variable "admin_emails" {
  type        = list(string)
  default     = []
  description = "Email addresses allowed to sign in to the admin dashboard."
}

resource "cloudflare_zero_trust_access_policy" "admin" {
  count = var.admin_hostname == "" ? 0 : 1

  account_id = var.account_id
  name       = "newsletter admin: allowed emails"
  decision   = "allow"

  include {
    email = var.admin_emails
  }
}

resource "cloudflare_zero_trust_access_application" "admin" {
  count = var.admin_hostname == "" ? 0 : 1

  account_id       = var.account_id
  name             = "newsletter admin"
  domain           = var.admin_hostname
  type             = "self_hosted"
  session_duration = "24h"
  policies         = [cloudflare_zero_trust_access_policy.admin[0].id]

  lifecycle {
    precondition {
      condition     = length(var.admin_emails) > 0
      error_message = "admin_emails must list at least one address when admin_hostname is set."
    }
  }
}

output "access_aud" {
  description = "Paste into ACCESS_AUD in wrangler.toml"
  value       = try(cloudflare_zero_trust_access_application.admin[0].aud, "")
}
