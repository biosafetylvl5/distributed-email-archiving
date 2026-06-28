#!/usr/bin/env bash
# Provision/deactivate users in the USERS KV namespace.
#   ./userctl.sh add john.smith
#   ./userctl.sh disable john.smith
set -euo pipefail
cmd="${1:?usage: userctl.sh add|disable <user>}"; user="${2:?user required}"
case "$cmd" in
  add)     npx wrangler kv key put --binding=USERS --remote "$user" '{"active":true}' ;;
  disable) npx wrangler kv key put --binding=USERS --remote "$user" '{"active":false}' ;;
  *) echo "unknown command: $cmd" >&2; exit 1 ;;
esac
