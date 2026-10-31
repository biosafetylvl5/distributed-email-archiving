#!/usr/bin/env bash
# offline regressions for the operator scripts; npx never invokes Wrangler
set -euo pipefail
root=$(cd "$(dirname "$0")/.." && pwd)
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
cat > "$tmp/npx" <<'MOCK'
#!/usr/bin/env bash
set -euo pipefail
[ "$1 $2 $3" = "wrangler kv key" ] || exit 91
case "$4" in
  list)
    [ "${FAIL_READ:-false}" != true ] || exit 92
    if [ -f "$RECORD" ]; then printf '[{"name":"%s"}]' "$KEY"; else printf '[]'; fi ;;
  get)
    [ "${FAIL_READ:-false}" != true ] || exit 92
    cat "$RECORD" ;;
  put) printf '%s' "${@: -1}" > "$RECORD" ;;
  *) exit 93 ;;
esac
MOCK
chmod +x "$tmp/npx"
export PATH="$tmp:$PATH" RECORD="$tmp/record.json" KEY="ann/news"

check() {
  local script=$1 initial=$2 expected=$3
  shift 3
  if [ "$initial" = absent ]; then rm -f "$RECORD"; else printf '%s' "$initial" > "$RECORD"; fi
  bash "$root/$script" "$@"
  jq -e --argjson expected "$expected" '. == $expected' "$RECORD" > /dev/null
}

record='{"status":"pending","publish":true,"sender_domain":"mail.example.org","capture_limit_mb":5,"note":"keep","extra":{"future":true}}'
for command in approve block publish; do
  case "$command" in
    approve) expected=$(jq '.status = "approved"' <<< "$record") ;;
    block) expected=$(jq '.status = "blocked"' <<< "$record") ;;
    publish) expected=$record ;;
  esac
  check tagctl.sh "$record" "$expected" "$command" ann news
done
check tagctl.sh absent '{"status":"approved","publish":false}' approve ann news
sender='example.org","capture_limit_mb":1,"extra":"'
expected=$(jq --arg sender "$sender" '.status = "approved" | .sender_domain = $sender' <<< "$record")
check tagctl.sh "$record" "$expected" approve ann news "$sender"

export KEY=ann
record='{"active":false,"extra":{"future":true}}'
check userctl.sh "$record" '{"active":true,"extra":{"future":true}}' add ann
check userctl.sh "$record" "$record" disable ann
check userctl.sh absent '{"active":true}' add ann

# a failed lookup or malformed existing record must never lead to a replacement
for script in tagctl.sh userctl.sh; do
  if [ "$script" = tagctl.sh ]; then command=block; KEY=ann/news; else command=disable; KEY=ann; fi
  printf '%s' '{"note":"keep"}' > "$RECORD"
  if FAIL_READ=true bash "$root/$script" "$command" ann news; then
    echo "unexpected success after failed read: $script" >&2; exit 1
  fi
  [ "$(cat "$RECORD")" = '{"note":"keep"}' ]
  printf '%s' '{' > "$RECORD"
  if bash "$root/$script" "$command" ann news 2>/dev/null; then
    echo "unexpected success with malformed record: $script" >&2; exit 1
  fi
  [ "$(cat "$RECORD")" = '{' ]
done
printf 'CLI regressions passed (offline)\n'
