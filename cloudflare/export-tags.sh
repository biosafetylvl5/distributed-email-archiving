#!/usr/bin/env bash
# Dump the TAGS KV namespace to a JSON file
#   ./export-tags.sh > tags.json 
set -euo pipefail
python3 - "$(npx wrangler kv key list --binding=TAGS)" << 'PY'
import json, subprocess, sys
raw = sys.argv[1]
keys = [k["name"] for k in json.loads(raw[raw.index("["):])]
out = {}
for key in keys:
    val = subprocess.run(
        ["wrangler", "kv", "key", "get", "--binding=TAGS", key],
        capture_output=True, text=True, check=True,
    ).stdout
    out[key] = json.loads(val[val.index("{"):])  # tolerate wrangler banner lines
print(json.dumps(out, indent=2))
PY
