#!/usr/bin/env bash
# Vendor-agnostic S3 -> S3 mirror with verification. Both ends are plain S3
# remotes in rclone.conf — primary store, NAS, anything speaking the API.
#
#   ./mirror.sh primary:newsletter-archive nas:newsletter-archive
#
# Example ~/.config/rclone/rclone.conf:
#
#   [primary]                          [nas]
#   type = s3                          type = s3
#   provider = Cloudflare              provider = Minio
#   endpoint = https://<acct>.r2...    endpoint = https://nas.local:9000
#   access_key_id = ...                access_key_id = ...
#   secret_access_key = ...            secret_access_key = ...
#                                      force_path_style = true
#
# Run nightly (cron/systemd timer). `check` is the part that matters: a sync
# that silently diverges is worse than no sync.
set -euo pipefail
SRC="${1:?usage: mirror.sh <src-remote:bucket> <dst-remote:bucket>}"
DST="${2:?usage: mirror.sh <src-remote:bucket> <dst-remote:bucket>}"
rclone sync "$SRC" "$DST" --fast-list --transfers 8
rclone check "$SRC" "$DST" --one-way --fast-list
