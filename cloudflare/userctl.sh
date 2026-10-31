#!/usr/bin/env bash
# Provision/deactivate users in the USERS KV namespace.
#   ./userctl.sh add john.smith
#   ./userctl.sh disable john.smith
set -euo pipefail
cmd="${1:?usage: userctl.sh add|disable <user>}"; user="${2:?user required}"
case "$cmd" in
  add) active=true ;;
  disable) active=false ;;
  *) echo "unknown command: $cmd" >&2; exit 1 ;;
esac
# distinguish a new key from a failed read; never overwrite after a read failure
keys=$(npx wrangler kv key list --remote --binding=USERS --prefix "$user")
exists=$(printf '%s' "$keys" | jq -r --arg key "$user" 'any(.[]; .name == $key)')
current='{}'
if [ "$exists" = true ]; then
  current=$(npx wrangler kv key get --remote --binding=USERS "$user")
fi
updated=$(printf '%s' "$current" | jq -e --argjson active "$active" '
  if type != "object" then error("expected a user record") else . end |
  .active = $active')
npx wrangler kv key put --binding=USERS --remote "$user" "$updated"
