terraform {
  required_version = ">= 1.6"
  required_providers {
    cloudflare = {
      source  = "cloudflare/cloudflare"
      version = "~> 4.52"
    }
  }
}

provider "cloudflare" {
  # auth via CLOUDFLARE_API_TOKEN environment variable.
}

variable "account_id" {
  type        = string
  description = "Cloudflare account ID"

  validation {
    condition     = can(regex("^[0-9a-f]{32}$", var.account_id))
    error_message = "account_id must be 32-character Account ID, not a domain name. Find it in the Cloudflare dashboard."
  }
}

variable "zone_id" {
  type        = string
  description = "Zone ID of the APEX domain, e.g. the Zone ID of example.com."

  validation {
    condition     = can(regex("^[0-9a-f]{32}$", var.zone_id))
    error_message = "zone_id must be the 32-character hex Zone ID, not the domain name!"
  }
}

variable "email_domain" {
  type        = string
  description = "The receiving subdomain, e.g. mail.example.org. Subscribers use user@email_domain. Must be added under the zone via the dashboard."
}

variable "backup_mailbox" {
  type        = string
  description = "External mailbox receiving the PERMANENT backup forward. Cloudflare emails a verification link to it on first run."
}

variable "email_forwards" {
  type        = map(string)
  default     = {}
  description = "Optional map of source address -> destination address to forward instead of ingesting, e.g. {\"me@example.org\" = \"me@personal.example\"}."
}

variable "ingest_worker_name" {
  type    = string
  default = "newsletter-ingest"
}

output "kv_namespace_ids" {
  description = "Paste these into wrangler.toml"
  value = {
    USERS     = cloudflare_workers_kv_namespace.users.id
    TAGS      = cloudflare_workers_kv_namespace.tags.id
    RATELIMIT = cloudflare_workers_kv_namespace.ratelimit.id
    SETTINGS  = cloudflare_workers_kv_namespace.settings.id
  }
}

output "email_domain" {
  description = "Subscribe with addresses at this subdomain, e.g. john.smith+tag@<email_domain>"
  value       = var.email_domain
}

output "manual_subdomain_step" {
  description = "This sets what it can, but some options must be set manually! Please see the README.md for details."
  value       = ""
}
