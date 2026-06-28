#!/usr/bin/env bash
# Manage tags in the TAGS KV namespace. A tag must be approved BEFORE
# subscribing with user+tag@domain (POLICY_MODE=strict rejects unknown tags).
#
#   ./tagctl.sh approve john.smith ramen_weekly
#   ./tagctl.sh approve john.smith ramen_weekly mail.ramenweekly.example   # + sender binding
#   ./tagctl.sh publish john.smith ramen_weekly        # opt tag into delayed publication
#   ./tagctl.sh block   john.smith spammy_tag
#   ./tagctl.sh show    john.smith ramen_weekly
set -euo pipefail
cmd="${1:?usage: tagctl.sh approve|publish|block|show <user> <tag> [sender_domain]}"
user="${2:?user required}"; tag="${3:?tag required}"; key="$user/$tag"
case "$cmd" in
  approve)
    if [ -n "${4:-}" ]; then
      npx wrangler kv key put --remote --binding=TAGS "$key" "{\"status\":\"approved\",\"publish\":false,\"sender_domain\":\"$4\"}"
    else
      npx wrangler kv key put --remote --binding=TAGS "$key" '{"status":"approved","publish":false}'
    fi ;;
  publish)
    current=$(npx wrangler kv key get --remote --binding=TAGS "$key")
    updated=$(echo "$current" | python3 -c 'import json,sys; s=sys.stdin.read(); d=json.loads(s[s.index("{"):]); d["publish"]=True; print(json.dumps(d))')
    npx wrangler kv key put --remote --binding=TAGS "$key" "$updated" ;;
  block)
    npx wrangler kv key put --remote --binding=TAGS "$key" '{"status":"blocked"}' ;;
  show)
    npx wrangler kv key get --remote --binding=TAGS "$key" ;;
  *) echo "unknown command: $cmd" >&2; exit 1 ;;
esac
