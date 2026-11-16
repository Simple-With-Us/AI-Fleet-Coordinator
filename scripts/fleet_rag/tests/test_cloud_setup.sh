#!/usr/bin/env bash
# Exercise scripts/cloud-setup.sh against a throwaway $HOME and a throwaway fake checkout,
# so no real config on this Mac is touched and nothing ever reaches Infisical: the fetch
# helper is replaced by a stub that writes fixed fake values.
#
#   cd scripts && bash fleet_rag/tests/test_cloud_setup.sh
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS="$(cd "$HERE/../.." && pwd)"
SETUP="$SCRIPTS/cloud-setup.sh"
TMP="$(mktemp -d "${TMPDIR:-/tmp}/fleet-cloud-setup-test.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT

FAKE_HOME="$TMP/home"
FAKE_REPO="$TMP/repo"
FAILS=0

# Fake credential values the stub "fetches".  Distinctive so we can prove no literal
# secret ever lands in ~/.claude.json.
FAKE_TOKEN="STUBTOKEN-recall-aaaaaaaa"
FAKE_CID="STUBCID-access-bbbbbbbb"
FAKE_CSEC="STUBCSEC-access-cccccccc"
FAKE_INFISICAL_TOKEN="st.stub-recall-read-token-0123456789"

pass() { printf 'ok   - %s\n' "$1"; }
fail() { printf 'FAIL - %s\n' "$1"; FAILS=$((FAILS + 1)); }
assert() {  # assert <desc> <command...>
  local desc="$1"; shift
  if "$@"; then pass "$desc"; else fail "$desc"; fi
}
refute() {  # refute <desc> <command...>  -- passes when the command FAILS
  local desc="$1"; shift
  if "$@"; then fail "$desc"; else pass "$desc"; fi
}

# -- fake checkout: the real cloud-setup.sh, a stubbed registry check, a stubbed fetcher
mkdir -p "$FAKE_REPO/scripts" "$FAKE_HOME"
cp "$SETUP" "$FAKE_REPO/scripts/cloud-setup.sh"
printf '#!/usr/bin/env python3\nprint("registry ok (stub)")\n' > "$FAKE_REPO/scripts/check-fleet-registry.py"

# Stub fetcher: same contract as scripts/fleet-infisical-fetch.py (scoped token from the
# environment, 0600 env file, key NAMES on stdout) with no network at all.
cat > "$FAKE_REPO/scripts/fleet-infisical-fetch.py" <<PY
#!/usr/bin/env python3
"""Test stub for fleet-infisical-fetch.py -- never touches Infisical."""
import os, pathlib, shlex, sys

VALUES = {
    "RECALL_API_TOKEN": "$FAKE_TOKEN",
    "CF_ACCESS_CLIENT_ID": "$FAKE_CID",
    "CF_ACCESS_CLIENT_SECRET": "$FAKE_CSEC",
}
args = sys.argv[1:]
if args[:1] == ["--identity"]:
    present = os.environ.get("FLEET_RECALL_INFISICAL_TOKEN", "").startswith("st.")
    print("service-token-binding: " + ("true" if present else "false"))
    sys.exit(0 if present else 3)
assert args[0] == "--env-file", args
path = pathlib.Path(args[1])
wanted = args[2:]
if not os.environ.get("FLEET_RECALL_INFISICAL_TOKEN", "").startswith("st."):
    print("no-service-token")
    sys.exit(3)
print("service-token-binding: true (stub)")
old = os.umask(0o077)
try:
    body = "# stub\n" + "".join(f"{k}={shlex.quote(VALUES[k])}\n" for k in wanted if k in VALUES)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    os.chmod(path, 0o600)
finally:
    os.umask(old)
for k in wanted:
    print("wrote: " + k if k in VALUES else "missing: " + k)
sys.exit(0)
PY

ENV_FILE="$FAKE_HOME/.fleet-recall.env"
CLAUDE_JSON="$FAKE_HOME/.claude.json"

# -- a pre-existing ~/.claude.json whose keys must all survive
cat > "$CLAUDE_JSON" <<'EOF'
{
  "numStartups": 7,
  "mcpServers": {
    "github": {"command": "sh", "args": ["/x/github.sh"]}
  },
  "projects": {"/home/user/AI-Fleet-Coordinator": {"allowedTools": []}}
}
EOF

# run <args...>: with a fake recall service token in the environment
run() {
  HOME="$FAKE_HOME" \
  FLEET_RECALL_INFISICAL_TOKEN="$FAKE_INFISICAL_TOKEN" \
    bash "$FAKE_REPO/scripts/cloud-setup.sh" "$@"
}
# run_bare <args...>: no recall-scoped token in the environment
run_bare() {
  env -u FLEET_RECALL_INFISICAL_TOKEN \
      -u RECALL_API_TOKEN -u CF_ACCESS_CLIENT_ID -u CF_ACCESS_CLIENT_SECRET \
      HOME="$FAKE_HOME" bash "$FAKE_REPO/scripts/cloud-setup.sh" "$@"
}

mode_of() { python3 -c 'import os,sys;print(oct(os.stat(sys.argv[1]).st_mode & 0o777)[2:])' "$1"; }
json_get() {  # <file> <dotted path>
  python3 -c 'import json,sys;d=json.load(open(sys.argv[1]))
for k in sys.argv[2].split("."): d=d[k]
print(json.dumps(d,sort_keys=True))' "$1" "$2"
}
server_count() {  # <file> <name> -> how many mcpServers entries carry that name
  python3 -c 'import json,sys
d=json.load(open(sys.argv[1]))
print(sum(1 for k in d.get("mcpServers",{}) if k==sys.argv[2]))' "$1" "$2"
}

echo "== no scoped token: exits 0, provisions nothing, points at the doc"
if run_bare > "$TMP/none.out" 2>&1; then pass "no-token run exits 0"; else fail "no-token run exits 0"; fi
assert "no-token run says so plainly" grep -q 'no valid recall-scoped Infisical service token' "$TMP/none.out"
assert "no-token run names its binding" grep -q 'FLEET_RECALL_INFISICAL_TOKEN' "$TMP/none.out"
assert "no-token run points at RECALL-ACCESS-CHECK" grep -q 'docs/RECALL-ACCESS-CHECK.md' "$TMP/none.out"
assert "no-token run warns the corpus is unreachable" grep -q 'NOT reachable' "$TMP/none.out"
assert "no-token run writes no env file" test ! -e "$ENV_FILE"
assert "no-token run still registers the MCP entry" test "$(server_count "$CLAUDE_JSON" fleet-recall)" = "1"

# start the credential half from scratch so the first real run is the first write
rm -f "$CLAUDE_JSON".bak-fleet-recall-*
python3 - "$CLAUDE_JSON" <<'PY'
import json, sys
p = sys.argv[1]
d = json.load(open(p))
d["mcpServers"].pop("fleet-recall", None)
json.dump(d, open(p, "w"), indent=2)
PY

echo "== first run with a scoped token"
if run > "$TMP/run1.out" 2>&1; then pass "first run exits 0"; else fail "first run exits 0"; fi
assert "env file created" test -f "$ENV_FILE"
assert "env file is 0600" test "$(mode_of "$ENV_FILE")" = "600"
for k in RECALL_API_TOKEN CF_ACCESS_CLIENT_ID CF_ACCESS_CLIENT_SECRET; do
  assert "env file assigns $k" grep -Eq "^$k=.+$" "$ENV_FILE"
  assert "run output names $k" grep -q "$k" "$TMP/run1.out"
done
assert "run output never prints a credential value" bash -c "! grep -q '$FAKE_TOKEN' '$TMP/run1.out' && ! grep -q '$FAKE_CID' '$TMP/run1.out' && ! grep -q '$FAKE_CSEC' '$TMP/run1.out'"
assert "closing block tells the seat how to load the env file" grep -q "set -a; \. $ENV_FILE; set +a" "$TMP/run1.out"
assert "closing block gives the health self-test" grep -q 'curl -sS https://recall.jays.services/health' "$TMP/run1.out"
assert "closing block gives the search self-test" grep -q 'https://recall.jays.services/recall/search' "$TMP/run1.out"

echo "== MCP registration"
assert "exactly one fleet-recall entry" test "$(server_count "$CLAUDE_JSON" fleet-recall)" = "1"
assert "entry points at the remote recall MCP" test "$(json_get "$CLAUDE_JSON" mcpServers.fleet-recall.url)" = '"https://recall.jays.services/mcp"'
assert "headers are placeholders, matching mcp.example.json" \
  test "$(json_get "$CLAUDE_JSON" mcpServers.fleet-recall.headers)" = '{"Authorization": "Bearer ${RECALL_API_TOKEN}", "CF-Access-Client-Id": "${CF_ACCESS_CLIENT_ID}", "CF-Access-Client-Secret": "${CF_ACCESS_CLIENT_SECRET}"}'
assert "no literal credential value in ~/.claude.json" bash -c "! grep -q '$FAKE_TOKEN' '$CLAUDE_JSON' && ! grep -q '$FAKE_CID' '$CLAUDE_JSON' && ! grep -q '$FAKE_CSEC' '$CLAUDE_JSON'"
assert "no stdio command smuggled in" bash -c "! python3 -c 'import json,sys;sys.exit(0 if \"command\" in json.load(open(sys.argv[1]))[\"mcpServers\"][\"fleet-recall\"] else 1)' '$CLAUDE_JSON'"
assert "existing github server preserved" test "$(json_get "$CLAUDE_JSON" mcpServers.github.command)" = '"sh"'
assert "unrelated top-level key preserved" test "$(json_get "$CLAUDE_JSON" numStartups)" = "7"
assert "projects key preserved" test "$(json_get "$CLAUDE_JSON" 'projects./home/user/AI-Fleet-Coordinator.allowedTools')" = "[]"
assert "backup made before rewriting" bash -c "ls '$CLAUDE_JSON'.bak-fleet-recall-* >/dev/null 2>&1"

echo "== second run is a no-op"
cp "$CLAUDE_JSON" "$TMP/claude-after1.json"
cp "$ENV_FILE" "$TMP/env-after1"
N_BAK="$(ls "$CLAUDE_JSON".bak-fleet-recall-* | wc -l | tr -d ' ')"
if run > "$TMP/run2.out" 2>&1; then pass "second run exits 0"; else fail "second run exits 0"; fi
assert "second run reports the env file unchanged" grep -q "$ENV_FILE: unchanged" "$TMP/run2.out"
assert "second run reports the MCP entry unchanged" grep -q "unchanged: $CLAUDE_JSON" "$TMP/run2.out"
assert "second run leaves ~/.claude.json byte-identical" cmp -s "$CLAUDE_JSON" "$TMP/claude-after1.json"
assert "second run leaves the env file byte-identical" cmp -s "$ENV_FILE" "$TMP/env-after1"
assert "second run makes no new backup" test "$(ls "$CLAUDE_JSON".bak-fleet-recall-* | wc -l | tr -d ' ')" = "$N_BAK"
assert "second run still one fleet-recall entry" test "$(server_count "$CLAUDE_JSON" fleet-recall)" = "1"

echo "== --check reports names and booleans only"
run --check > "$TMP/check.out" 2>&1
assert "check: scoped token true" grep -q 'recall-scoped Infisical service token: true (FLEET_RECALL_INFISICAL_TOKEN)' "$TMP/check.out"
assert "check: env file present and 0600" grep -q "env file $ENV_FILE: present=true mode=600" "$TMP/check.out"
for k in RECALL_API_TOKEN CF_ACCESS_CLIENT_ID CF_ACCESS_CLIENT_SECRET; do
  assert "check: $k resolvable=true" grep -q "$k: resolvable=true" "$TMP/check.out"
done
assert "check: MCP registered=true" grep -q 'mcp fleet-recall .*: registered=true' "$TMP/check.out"
assert "check: no credential value printed" bash -c "! grep -q '$FAKE_TOKEN' '$TMP/check.out' && ! grep -q '$FAKE_CID' '$TMP/check.out' && ! grep -q '$FAKE_CSEC' '$TMP/check.out'"

echo "== --check on a bare sandbox reports false without inventing anything"
BARE_HOME="$TMP/bare-home"; mkdir -p "$BARE_HOME"
env -u FLEET_RECALL_INFISICAL_TOKEN \
    -u RECALL_API_TOKEN -u CF_ACCESS_CLIENT_ID -u CF_ACCESS_CLIENT_SECRET \
    HOME="$BARE_HOME" bash "$FAKE_REPO/scripts/cloud-setup.sh" --check > "$TMP/check-bare.out" 2>&1
assert "bare check: scoped token false" grep -q 'recall-scoped Infisical service token: false' "$TMP/check-bare.out"
assert "bare check: env file absent" grep -q 'present=false' "$TMP/check-bare.out"
assert "bare check: all three unresolvable" test "$(grep -c 'resolvable=false' "$TMP/check-bare.out")" = "3"
assert "bare check: MCP not registered" grep -q 'registered=false' "$TMP/check-bare.out"
assert "bare check wrote nothing" test ! -e "$BARE_HOME/.claude.json" -a ! -e "$BARE_HOME/.fleet-recall.env"

echo "== FLEET_RECALL_ENV_FILE relocates the env file"
ALT_ENV="$TMP/alt/recall.env"
HOME="$FAKE_HOME" FLEET_RECALL_ENV_FILE="$ALT_ENV" \
  FLEET_RECALL_INFISICAL_TOKEN="$FAKE_INFISICAL_TOKEN" \
  bash "$FAKE_REPO/scripts/cloud-setup.sh" > "$TMP/alt.out" 2>&1
assert "alt: env file written at the override path" test -f "$ALT_ENV"
assert "alt: override file is 0600" test "$(mode_of "$ALT_ENV")" = "600"
assert "alt: closing block names the override path" grep -q "set -a; \. $ALT_ENV; set +a" "$TMP/alt.out"

echo "== a hand-registered fleet-recall entry is left alone"
python3 - "$CLAUDE_JSON" <<'PY'
import json, sys
p = sys.argv[1]
d = json.load(open(p))
d["mcpServers"]["fleet-recall"] = {"url": "https://agents.jays.services/mcp"}
json.dump(d, open(p, "w"), indent=2)
PY
cp "$CLAUDE_JSON" "$TMP/claude-foreign.json"
run > "$TMP/run-foreign.out" 2>&1
assert "foreign: reported skipped-foreign" grep -q "skipped-foreign: $CLAUDE_JSON" "$TMP/run-foreign.out"
assert "foreign: file untouched" cmp -s "$CLAUDE_JSON" "$TMP/claude-foreign.json"

echo "== invalid JSON is reported, never rewritten, and does not fail the sandbox"
printf '{not json' > "$CLAUDE_JSON"
if run > "$TMP/run-invalid.out" 2>&1; then pass "invalid: run exits 0"; else fail "invalid: run exits 0"; fi
assert "invalid: reported skipped-invalid-json" grep -q "skipped-invalid-json: $CLAUDE_JSON" "$TMP/run-invalid.out"
assert "invalid: file byte-identical" test "$(cat "$CLAUDE_JSON")" = '{not json'

echo "== ~/.claude.json is created when absent"
NEW_HOME="$TMP/new-home"; mkdir -p "$NEW_HOME"
HOME="$NEW_HOME" FLEET_RECALL_INFISICAL_TOKEN="$FAKE_INFISICAL_TOKEN" \
  bash "$FAKE_REPO/scripts/cloud-setup.sh" > "$TMP/run-new.out" 2>&1
assert "new: ~/.claude.json created" test -f "$NEW_HOME/.claude.json"
assert "new: created config is 0600" test "$(mode_of "$NEW_HOME/.claude.json")" = "600"
assert "new: exactly one fleet-recall entry" test "$(server_count "$NEW_HOME/.claude.json" fleet-recall)" = "1"
refute "new: no backup made for a file that did not exist" bash -c "ls '$NEW_HOME'/.claude.json.bak-fleet-recall-* >/dev/null 2>&1"

echo "== the real fetch helper needs no network to answer --identity"
if env -u FLEET_RECALL_INFISICAL_TOKEN \
       python3 "$SCRIPTS/fleet-infisical-fetch.py" --identity > "$TMP/ident-none.out" 2>&1; then
  fail "real helper: --identity exits 3 with no token"
else
  test "$?" = "3" && pass "real helper: --identity exits 3 with no token" \
                  || fail "real helper: --identity exits 3 with no token"
fi
assert "real helper: reports token false" grep -q 'service-token-binding: false' "$TMP/ident-none.out"
FLEET_RECALL_INFISICAL_TOKEN="$FAKE_INFISICAL_TOKEN" \
  python3 "$SCRIPTS/fleet-infisical-fetch.py" --identity > "$TMP/ident-auto.out" 2>&1
assert "real helper: accepts the scoped read token" grep -q 'service-token-binding: true' "$TMP/ident-auto.out"

echo
if [[ "$FAILS" -eq 0 ]]; then
  echo "ALL PASS (test_cloud_setup.sh)"
else
  echo "$FAILS FAILURE(S) (test_cloud_setup.sh)"
  exit 1
fi
