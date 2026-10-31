#!/usr/bin/env bash
# Manage tags in the TAGS KV namespace. A tag must be approved BEFORE
# subscribing with user+tag@domain (POLICY_MODE=strict rejects unknown tags).
#
#   ./tagctl.sh approve john.smith ramen_weekly
#   ./tagctl.sh approve john.smith ramen_weekly mail.example.org   # + sender binding
#   ./tagctl.sh publish john.smith ramen_weekly        # opt tag into delayed publication
#   ./tagctl.sh block   john.smith spammy_tag
#   ./tagctl.sh show    john.smith ramen_weekly
set -euo pipefail
cmd="${1:?usage: tagctl.sh approve|publish|block|show <user> <tag> [sender_domain]}"
user="${2:?user required}"; tag="${3:?tag required}"; key="$user/$tag"
case "$cmd" in
  approve|publish|block) ;;
  show) npx wrangler kv key get --remote --binding=TAGS "$key"; exit ;;
  *) echo "unknown command: $cmd" >&2; exit 1 ;;
esac
# distinguish a new key from a failed read; never overwrite after a read failure
keys=$(npx wrangler kv key list --remote --binding=TAGS --prefix "$key")
exists=$(printf '%s' "$keys" | jq -r --arg key "$key" 'any(.[]; .name == $key)')
current='{}'
if [ "$exists" = true ]; then
  current=$(npx wrangler kv key get --remote --binding=TAGS "$key")
fi
updated=$(printf '%s' "$current" | jq -e --arg cmd "$cmd" --arg sender "${4:-}" '
  if type != "object" then error("expected a tag record") else . end |
  if $cmd == "approve" then
    .status = "approved" | .publish //= false |
    if $sender != "" then .sender_domain = $sender else . end
  elif $cmd == "block" then .status = "blocked"
  else .publish = true end')
npx wrangler kv key put --remote --binding=TAGS "$key" "$updated"
