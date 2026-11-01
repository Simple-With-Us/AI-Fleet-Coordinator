#!/usr/bin/env bash
# Exercise scripts/install-fleet-rag.sh against a throwaway $HOME and a throwaway fake
# checkout, so no real config on this Mac is touched.
#
#   cd scripts && bash fleet_rag/tests/test_installer.sh
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS="$(cd "$HERE/../.." && pwd)"
INSTALLER="$SCRIPTS/install-fleet-rag.sh"
TMP="$(mktemp -d "${TMPDIR:-/tmp}/fleet-rag-install-test.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT

FAKE_HOME="$TMP/home"
FAKE_REPO="$TMP/repo"
FAILS=0

pass() { printf 'ok   - %s\n' "$1"; }
fail() { printf 'FAIL - %s\n' "$1"; FAILS=$((FAILS + 1)); }
assert() {  # assert <desc> <command...>
  local desc="$1"; shift
  if "$@"; then pass "$desc"; else fail "$desc"; fi
}

# -- fake checkout: the real installer plus the real fleet_rag package, plus stub entry points
mkdir -p "$FAKE_REPO/scripts/seat-mcp/seat_mcp"
cp "$INSTALLER" "$FAKE_REPO/scripts/install-fleet-rag.sh"
cp -R "$SCRIPTS/fleet_rag" "$FAKE_REPO/scripts/fleet_rag"
cp -R "$SCRIPTS/fleet_lanes" "$FAKE_REPO/scripts/fleet_lanes"
cp -R "$SCRIPTS/fleet_config" "$FAKE_REPO/scripts/fleet_config"
mkdir -p "$FAKE_REPO/scripts/fleet_rag/__pycache__"
touch "$FAKE_REPO/scripts/fleet_rag/__pycache__/junk.pyc"
for f in recall recall-tunnel fleet-recall-mcp.py fleet-rag.py; do
  if [[ -f "$SCRIPTS/$f" ]]; then cp "$SCRIPTS/$f" "$FAKE_REPO/scripts/$f"; else printf '#!/usr/bin/env python3\nprint("stub %s")\n' "$f" > "$FAKE_REPO/scripts/$f"; fi
done
printf '# stub\n' > "$FAKE_REPO/scripts/seat-mcp/seat_mcp/tools.py"
printf '# stub\n' > "$FAKE_REPO/scripts/seat-mcp/seat_mcp/recall_bridge.py"
printf '# stub\n' > "$FAKE_REPO/scripts/seat-mcp/seat_mcp/server.py"
mkdir -p "$FAKE_REPO/scripts/hooks"
cp "$SCRIPTS/hooks/fleet-recall-session-start.sh" "$SCRIPTS/hooks/fleet-recall-stop.py" "$SCRIPTS/hooks/fleet-recall-prompt-gate.py" "$FAKE_REPO/scripts/hooks/"

# -- fake HOME with minimal configs
mkdir -p "$FAKE_HOME/.cursor" "$FAKE_HOME/.codex" "$FAKE_HOME/.grok" "$FAKE_HOME/apps/mac-collab"
cat > "$FAKE_HOME/.claude.json" <<'EOF'
{
  "numStartups": 3,
  "mcpServers": {
    "github": {"command": "sh", "args": ["/x/github.sh"]}
  },
  "projects": {"/Users/jay/Code/x": {"allowedTools": []}}
}
EOF
cat > "$FAKE_HOME/.codex/config.toml" <<'EOF'
model = "gpt-5"

[mcp_servers.x]
command = "npx"
args = [ "-y", "x-mcp" ]
enabled = true
EOF
cat > "$FAKE_HOME/.grok/config.toml" <<'EOF'
[cli]
theme = "dark"

[mcp_servers.seat-mcp]
command = "sh"
args = ["/x/seat-mcp-launch.sh"]
enabled = true

[permission]
allow = ["bash"]
EOF
: > "$FAKE_HOME/.cursor/mcp.json"
mkdir -p "$FAKE_HOME/.claude"
cat > "$FAKE_HOME/.claude/settings.json" <<'EOF'
{
  "permissions": {"allow": ["Bash(ls:*)"]},
  "hooks": {
    "SessionStart": [
      {"matcher": "startup|resume", "hooks": [{"type": "command", "command": "~/.claude/monet-sync/session-hook.sh", "timeout": 10}]},
      {"hooks": [{"type": "command", "command": "'/opt/homebrew/bin/moshi-hook' claude-hook", "async": true}]}
    ],
    "Stop": [
      {"hooks": [{"type": "command", "command": "node /x/shellular-notify.mjs claude-code"}]}
    ],
    "PreToolUse": [
      {"matcher": "Bash", "hooks": [{"type": "command", "command": "python3 ~/.claude/hooks/guard-pretooluse.py", "timeout": 5}]}
    ]
  },
  "theme": "light"
}
EOF
# stale symlink that must be replaced
ln -s "$FAKE_HOME/nowhere/recall" "$FAKE_HOME/apps/mac-collab/recall"
ln -s "$FAKE_HOME/nowhere/recall-tunnel" "$FAKE_HOME/apps/mac-collab/recall-tunnel"

run() { HOME="$FAKE_HOME" bash "$FAKE_REPO/scripts/install-fleet-rag.sh" "$@"; }

snapshot() {  # <label>: copy every config for later diffing (distinct names; two are config.toml)
  local d="$TMP/snap-$1"
  mkdir -p "$d"
  cp "$FAKE_HOME/.claude.json" "$d/claude.json"
  cp "$FAKE_HOME/.codex/config.toml" "$d/codex.toml"
  cp "$FAKE_HOME/.grok/config.toml" "$d/grok.toml"
  cp "$FAKE_HOME/.cursor/mcp.json" "$d/cursor.json"
  cp "$FAKE_HOME/.claude/settings.json" "$d/settings.json"
  [[ -f "$FAKE_HOME/.gemini/config/mcp_config.json" ]] && cp "$FAKE_HOME/.gemini/config/mcp_config.json" "$d/gemini.json" || true
}
snap_name() {  # map a $HOME-relative config path to its snapshot file name
  case "$1" in
    .claude.json) echo claude.json ;;
    .codex/config.toml) echo codex.toml ;;
    .grok/config.toml) echo grok.toml ;;
    .cursor/mcp.json) echo cursor.json ;;
  esac
}

json_has() {  # <file> <server> -> 0 if present
  python3 -c 'import json,sys;d=json.load(open(sys.argv[1]));sys.exit(0 if sys.argv[2] in d.get("mcpServers",{}) else 1)' "$1" "$2"
}
hook_count() {  # <file> <event> <needle> -> number of entries whose command mentions needle
  python3 -c 'import json,sys
d=json.load(open(sys.argv[1])); n=0
for e in d.get("hooks",{}).get(sys.argv[2],[]):
    if any(sys.argv[3] in h.get("command","") for h in e.get("hooks",[])): n+=1
print(n)' "$1" "$2" "$3"
}
hook_len() {  # <file> <event> -> length of the array
  python3 -c 'import json,sys;d=json.load(open(sys.argv[1]));print(len(d.get("hooks",{}).get(sys.argv[2],[])))' "$1" "$2"
}
exact_count() {  # <file> <event> <command> -> number of hooks whose command equals it exactly
  python3 -c 'import json,sys
d=json.load(open(sys.argv[1])); n=0
for e in d.get("hooks",{}).get(sys.argv[2],[]):
    n+=sum(1 for h in e.get("hooks",[]) if h.get("command")==sys.argv[3])
print(n)' "$1" "$2" "$3"
}
canon() {  # <file> -> canonical JSON
  python3 -c 'import json,sys;print(json.dumps(json.load(open(sys.argv[1])),sort_keys=True))' "$1"
}
json_get() {  # <file> <dotted path>
  python3 -c 'import json,sys;d=json.load(open(sys.argv[1]))
for k in sys.argv[2].split("."): d=d[k]
print(json.dumps(d,sort_keys=True))' "$1" "$2"
}

echo "== dry-run writes nothing"
snapshot before
run --dry-run > "$TMP/dry.out"
assert "dry-run mentions planned-add" grep -q 'planned-add' "$TMP/dry.out"
assert "dry-run leaves ~/.claude.json untouched" cmp -s "$FAKE_HOME/.claude.json" "$TMP/snap-before/claude.json"
assert "dry-run does not create ~/apps/fleet-rag" test ! -e "$FAKE_HOME/apps/fleet-rag/recall"
assert "dry-run does not create recall-tunnel" test ! -e "$FAKE_HOME/apps/fleet-rag/recall-tunnel"
assert "dry-run does not create gemini config" test ! -e "$FAKE_HOME/.gemini/config/mcp_config.json"
run --dry-run --hooks > "$TMP/dry-hooks.out"
assert "dry-run --hooks plans the settings.json entries" grep -q 'settings.json: planned-add' "$TMP/dry-hooks.out"
assert "dry-run --hooks leaves settings.json untouched" cmp -s "$FAKE_HOME/.claude/settings.json" "$TMP/snap-before/settings.json"
assert "dry-run --hooks copies nothing" test ! -e "$FAKE_HOME/.claude/hooks/fleet-recall-stop.py"

echo "== first install"
run --with-seat-mcp > "$TMP/run1.out"
assert "fleet_rag package installed" test -f "$FAKE_HOME/apps/fleet-rag/fleet_rag/core.py"
assert "tests dir excluded from install" test ! -e "$FAKE_HOME/apps/fleet-rag/fleet_rag/tests"
assert "__pycache__ excluded from install" test ! -e "$FAKE_HOME/apps/fleet-rag/fleet_rag/__pycache__"
assert "recall is executable" test -x "$FAKE_HOME/apps/fleet-rag/recall"
assert "recall-tunnel is executable" test -x "$FAKE_HOME/apps/fleet-rag/recall-tunnel"
assert "mcp server is executable" test -x "$FAKE_HOME/apps/fleet-rag/fleet-recall-mcp.py"
assert "state/cache/logs created" test -d "$FAKE_HOME/apps/fleet-rag/state" -a -d "$FAKE_HOME/apps/fleet-rag/cache" -a -d "$FAKE_HOME/apps/fleet-rag/logs"
assert "stale symlink replaced" test "$(readlink "$FAKE_HOME/apps/mac-collab/recall")" = "$FAKE_HOME/apps/fleet-rag/recall"
assert "stale recall-tunnel symlink replaced" test "$(readlink "$FAKE_HOME/apps/mac-collab/recall-tunnel")" = "$FAKE_HOME/apps/fleet-rag/recall-tunnel"
assert "PATH symlink ~/.local/bin/recall" test "$(readlink "$FAKE_HOME/.local/bin/recall")" = "$FAKE_HOME/apps/fleet-rag/recall"
assert "PATH symlink ~/.local/bin/recall-tunnel" test "$(readlink "$FAKE_HOME/.local/bin/recall-tunnel")" = "$FAKE_HOME/apps/fleet-rag/recall-tunnel"
assert "grok-acp config absent is not created" test ! -e "$FAKE_HOME/apps/grok-acp-runtime/acp-home-config.toml"
assert "seat-mcp server boundary copied" test -f "$FAKE_HOME/apps/seat-mcp/seat_mcp/server.py"
assert "recall presentation helper copied" test -f "$FAKE_HOME/apps/fleet-rag/fleet_rag/recall_presentation.py"
assert "seat-mcp files copied" test -f "$FAKE_HOME/apps/seat-mcp/seat_mcp/recall_bridge.py" -a -f "$FAKE_HOME/apps/seat-mcp/seat_mcp/tools.py"
assert "pm2 restart printed, not run" grep -q 'pm2 restart seat-mcp' "$TMP/run1.out"

assert "claude.json: fleet-recall added" json_has "$FAKE_HOME/.claude.json" fleet-recall
assert "claude.json: existing github server preserved" json_has "$FAKE_HOME/.claude.json" github
assert "claude.json: unrelated keys preserved" test "$(json_get "$FAKE_HOME/.claude.json" numStartups)" = "3"
assert "claude.json: projects preserved" test "$(json_get "$FAKE_HOME/.claude.json" 'projects./Users/jay/Code/x.allowedTools')" = "[]"
assert "claude.json: command is python3" test "$(json_get "$FAKE_HOME/.claude.json" mcpServers.fleet-recall.command)" = '"python3"'
assert "claude.json: args point at installed server" test "$(json_get "$FAKE_HOME/.claude.json" mcpServers.fleet-recall.args)" = "[\"$FAKE_HOME/apps/fleet-rag/fleet-recall-mcp.py\"]"
assert "claude.json: backup made" bash -c "ls '$FAKE_HOME'/.claude.json.bak-fleet-rag-* >/dev/null 2>&1"
assert "cursor mcp.json: fleet-recall added to empty file" json_has "$FAKE_HOME/.cursor/mcp.json" fleet-recall
assert "gemini config created with fleet-recall" json_has "$FAKE_HOME/.gemini/config/mcp_config.json" fleet-recall
assert "codex toml: block added once" test "$(grep -c '^\[mcp_servers.fleet-recall\]' "$FAKE_HOME/.codex/config.toml")" = "1"
assert "codex toml: existing block preserved" grep -q '^\[mcp_servers.x\]' "$FAKE_HOME/.codex/config.toml"
assert "codex toml: model line preserved" grep -q '^model = "gpt-5"' "$FAKE_HOME/.codex/config.toml"
assert "grok toml: block added once" test "$(grep -c '^\[mcp_servers.fleet-recall\]' "$FAKE_HOME/.grok/config.toml")" = "1"
assert "grok toml: enabled key used" bash -c "awk '/^\[mcp_servers.fleet-recall\]/{f=1;next} /^\[/{f=0} f' '$FAKE_HOME/.grok/config.toml' | grep -q '^enabled = true'"
assert "grok toml: seat-mcp block preserved" grep -q '^\[mcp_servers.seat-mcp\]' "$FAKE_HOME/.grok/config.toml"
assert "grok toml: trailing [permission] preserved" grep -q '^allow = \["bash"\]' "$FAKE_HOME/.grok/config.toml"
assert "no token-looking strings written" bash -c "! grep -Eiq 'token|secret|bearer' '$FAKE_HOME/.cursor/mcp.json' '$FAKE_HOME/.gemini/config/mcp_config.json'"

assert "without --hooks: settings.json untouched" cmp -s "$FAKE_HOME/.claude/settings.json" "$TMP/snap-before/settings.json"
assert "without --hooks: no hook files" test ! -e "$FAKE_HOME/.claude/hooks/fleet-recall-session-start.sh"
assert "without --hooks: skipped is reported" grep -q 'hooks: skipped' "$TMP/run1.out"

echo "== --hooks install"
run --hooks > "$TMP/run-hooks.out"
assert "hook files installed" test -f "$FAKE_HOME/.claude/hooks/fleet-recall-session-start.sh" -a -f "$FAKE_HOME/.claude/hooks/fleet-recall-stop.py"
assert "hook files executable" test -x "$FAKE_HOME/.claude/hooks/fleet-recall-session-start.sh" -a -x "$FAKE_HOME/.claude/hooks/fleet-recall-stop.py"
assert "settings.json: added reported" grep -q 'settings.json: added' "$TMP/run-hooks.out"
assert "settings.json: SessionStart gained exactly one entry" test "$(hook_len "$FAKE_HOME/.claude/settings.json" SessionStart)" = "3"
assert "settings.json: Stop gained exactly one entry" test "$(hook_len "$FAKE_HOME/.claude/settings.json" Stop)" = "2"
assert "settings.json: our SessionStart entry present once" test "$(hook_count "$FAKE_HOME/.claude/settings.json" SessionStart fleet-recall-session-start.sh)" = "1"
assert "settings.json: our Stop entry present once" test "$(hook_count "$FAKE_HOME/.claude/settings.json" Stop fleet-recall-stop.py)" = "1"
assert "settings.json: our Stop entry runs python3 on the installed file" test "$(json_get "$FAKE_HOME/.claude/settings.json" hooks.Stop | python3 -c 'import json,sys;print(json.load(sys.stdin)[-1]["hooks"][0]["command"])')" = "python3 $FAKE_HOME/.claude/hooks/fleet-recall-stop.py"
assert "settings.json: existing monet-sync SessionStart entry preserved" test "$(hook_count "$FAKE_HOME/.claude/settings.json" SessionStart monet-sync)" = "1"
assert "settings.json: existing moshi SessionStart entry preserved" test "$(hook_count "$FAKE_HOME/.claude/settings.json" SessionStart moshi-hook)" = "1"
assert "settings.json: existing Stop entry preserved" test "$(hook_count "$FAKE_HOME/.claude/settings.json" Stop shellular-notify)" = "1"
assert "settings.json: PreToolUse untouched" test "$(json_get "$FAKE_HOME/.claude/settings.json" hooks.PreToolUse)" = "$(json_get "$TMP/snap-before/settings.json" hooks.PreToolUse)"
assert "settings.json: permissions untouched" test "$(json_get "$FAKE_HOME/.claude/settings.json" permissions)" = "$(json_get "$TMP/snap-before/settings.json" permissions)"
assert "settings.json: theme untouched" test "$(json_get "$FAKE_HOME/.claude/settings.json" theme)" = '"light"'
assert "settings.json: backup made" bash -c "ls '$FAKE_HOME'/.claude/settings.json.bak-fleet-rag-* >/dev/null 2>&1"
assert "settings.json: no token-looking strings" bash -c "! grep -Eiq 'token|bearer' '$FAKE_HOME/.claude/settings.json'"

echo "== second install is a no-op"
snapshot after1
run --hooks > "$TMP/run2.out"
for f in .claude.json .codex/config.toml .grok/config.toml .cursor/mcp.json; do
  assert "second run leaves $f byte-identical" cmp -s "$FAKE_HOME/$f" "$TMP/snap-after1/$(snap_name "$f")"
done
assert "second run: gemini byte-identical" cmp -s "$FAKE_HOME/.gemini/config/mcp_config.json" "$TMP/snap-after1/gemini.json"
assert "second run: settings.json byte-identical" cmp -s "$FAKE_HOME/.claude/settings.json" "$TMP/snap-after1/settings.json"
assert "second run reports unchanged for every config" test "$(grep -c ': unchanged$' "$TMP/run2.out")" = "6"
assert "second run makes no new backups" test "$(ls "$FAKE_HOME"/.claude.json.bak-fleet-rag-* | wc -l | tr -d ' ')" = "1"
assert "second run makes no new settings backups" test "$(ls "$FAKE_HOME"/.claude/settings.json.bak-fleet-rag-* | wc -l | tr -d ' ')" = "1"
assert "second run: still one SessionStart entry of ours" test "$(hook_count "$FAKE_HOME/.claude/settings.json" SessionStart fleet-recall-session-start.sh)" = "1"
assert "codex toml still has exactly one block" test "$(grep -c '^\[mcp_servers.fleet-recall\]' "$FAKE_HOME/.codex/config.toml")" = "1"

echo "== uninstall restores"
run --uninstall > "$TMP/run3.out"
if json_has "$FAKE_HOME/.claude.json" fleet-recall; then fail "claude.json: fleet-recall removed"; else pass "claude.json: fleet-recall removed"; fi
assert "claude.json: github still present" json_has "$FAKE_HOME/.claude.json" github
assert "claude.json: content equals pre-install (modulo formatting)" test "$(json_get "$FAKE_HOME/.claude.json" mcpServers)" = "$(json_get "$TMP/snap-before/claude.json" mcpServers)"
assert "codex toml restored byte-for-byte" cmp -s "$FAKE_HOME/.codex/config.toml" "$TMP/snap-before/codex.toml"
assert "grok toml restored byte-for-byte" cmp -s "$FAKE_HOME/.grok/config.toml" "$TMP/snap-before/grok.toml"
assert "grok toml: block removed" bash -c "! grep -q '^\[mcp_servers.fleet-recall\]' '$FAKE_HOME/.grok/config.toml'"
assert "grok toml: [permission] survives removal" grep -q '^allow = \["bash"\]' "$FAKE_HOME/.grok/config.toml"
assert "grok toml: seat-mcp survives removal" grep -q '^\[mcp_servers.seat-mcp\]' "$FAKE_HOME/.grok/config.toml"
if json_has "$FAKE_HOME/.gemini/config/mcp_config.json" fleet-recall; then fail "gemini: removed"; else pass "gemini: removed"; fi
assert "symlink removed" test ! -e "$FAKE_HOME/apps/mac-collab/recall" -a ! -L "$FAKE_HOME/apps/mac-collab/recall"
assert "PATH symlink removed" test ! -e "$FAKE_HOME/.local/bin/recall" -a ! -L "$FAKE_HOME/.local/bin/recall"
assert "recall-tunnel symlink removed" test ! -e "$FAKE_HOME/apps/mac-collab/recall-tunnel" -a ! -L "$FAKE_HOME/apps/mac-collab/recall-tunnel"
assert "recall-tunnel PATH symlink removed" test ! -e "$FAKE_HOME/.local/bin/recall-tunnel" -a ! -L "$FAKE_HOME/.local/bin/recall-tunnel"
assert "installed code removed" test ! -e "$FAKE_HOME/apps/fleet-rag/fleet_rag"
assert "recall-tunnel binary removed" test ! -e "$FAKE_HOME/apps/fleet-rag/recall-tunnel"
assert "state dir kept on uninstall" test -d "$FAKE_HOME/apps/fleet-rag/state"
assert "hook files removed" test ! -e "$FAKE_HOME/.claude/hooks/fleet-recall-session-start.sh" -a ! -e "$FAKE_HOME/.claude/hooks/fleet-recall-stop.py"
assert "settings.json: our entries removed" test "$(hook_count "$FAKE_HOME/.claude/settings.json" SessionStart fleet-recall)" = "0" -a "$(hook_count "$FAKE_HOME/.claude/settings.json" Stop fleet-recall)" = "0"
assert "settings.json: hooks restored to pre-install content" test "$(json_get "$FAKE_HOME/.claude/settings.json" hooks)" = "$(json_get "$TMP/snap-before/settings.json" hooks)"
assert "settings.json: whole file equals pre-install (modulo formatting)" test "$(canon "$FAKE_HOME/.claude/settings.json")" = "$(canon "$TMP/snap-before/settings.json")"
run --uninstall > "$TMP/run3b.out"
assert "second uninstall reports settings absent" grep -q 'settings.json: absent' "$TMP/run3b.out"

echo "== --hooks on a settings.json with no hooks key, then uninstall restores exactly"
printf '{"theme": "dark"}\n' > "$FAKE_HOME/.claude/settings.json"
run --hooks > "$TMP/run-h2.out"
assert "hooks key created with both events" test "$(hook_len "$FAKE_HOME/.claude/settings.json" SessionStart)" = "1" -a "$(hook_len "$FAKE_HOME/.claude/settings.json" Stop)" = "1"
run --uninstall > "$TMP/run-h3.out"
assert "hooks key removed again" test "$(canon "$FAKE_HOME/.claude/settings.json")" = '{"theme": "dark"}'

echo "== foreign entries that mention a hook file are never ours"
OUR_START="$FAKE_HOME/.claude/hooks/fleet-recall-session-start.sh"
OUR_STOP="python3 $FAKE_HOME/.claude/hooks/fleet-recall-stop.py"
cat > "$FAKE_HOME/.claude/settings.json" <<EOF
{
  "hooks": {
    "SessionStart": [
      {"hooks": [{"type": "command", "command": "bash -lc '$OUR_START'"}]}
    ],
    "Stop": [
      {"hooks": [{"type": "command", "command": "python3 /elsewhere/hooks/fleet-recall-stop.py --quiet"}]}
    ]
  }
}
EOF
snapshot foreign
run --hooks > "$TMP/run-f1.out"
assert "foreign: added and both events reported skipped-foreign" grep -q 'settings.json: added (skipped-foreign SessionStart Stop)' "$TMP/run-f1.out"
assert "foreign: our exact SessionStart entry added once" test "$(exact_count "$FAKE_HOME/.claude/settings.json" SessionStart "$OUR_START")" = "1"
assert "foreign: our exact Stop entry added once" test "$(exact_count "$FAKE_HOME/.claude/settings.json" Stop "$OUR_STOP")" = "1"
assert "foreign: wrapper SessionStart entry kept" test "$(hook_len "$FAKE_HOME/.claude/settings.json" SessionStart)" = "2"
assert "foreign: other-dir Stop entry kept" test "$(hook_len "$FAKE_HOME/.claude/settings.json" Stop)" = "2"
snapshot foreign2
run --hooks > "$TMP/run-f2.out"
assert "foreign: second run unchanged, still reported" grep -q 'settings.json: unchanged (skipped-foreign SessionStart Stop)' "$TMP/run-f2.out"
assert "foreign: second run byte-identical" cmp -s "$FAKE_HOME/.claude/settings.json" "$TMP/snap-foreign2/settings.json"
run --uninstall > "$TMP/run-f3.out"
assert "foreign: removed reported with skipped-foreign" grep -q 'settings.json: removed (skipped-foreign SessionStart Stop)' "$TMP/run-f3.out"
assert "foreign: our exact entries gone" test "$(exact_count "$FAKE_HOME/.claude/settings.json" SessionStart "$OUR_START")" = "0" -a "$(exact_count "$FAKE_HOME/.claude/settings.json" Stop "$OUR_STOP")" = "0"
assert "foreign: wrapper entries survive uninstall" test "$(hook_count "$FAKE_HOME/.claude/settings.json" SessionStart fleet-recall-session-start.sh)" = "1" -a "$(hook_count "$FAKE_HOME/.claude/settings.json" Stop fleet-recall-stop.py)" = "1"
assert "foreign: settings.json equals pre-install (modulo formatting)" test "$(canon "$FAKE_HOME/.claude/settings.json")" = "$(canon "$TMP/snap-foreign/settings.json")"
assert "foreign: file run by the wrapper kept, file only run from elsewhere removed" test -f "$FAKE_HOME/.claude/hooks/fleet-recall-session-start.sh" -a ! -e "$FAKE_HOME/.claude/hooks/fleet-recall-stop.py"
assert "foreign: kept file reported" grep -q 'kept .*fleet-recall-session-start.sh (a foreign settings.json entry runs it)' "$TMP/run-f3.out"
run --uninstall > "$TMP/run-f4.out"
assert "foreign: second uninstall reports absent with skipped-foreign" grep -q 'settings.json: absent (skipped-foreign SessionStart Stop)' "$TMP/run-f4.out"

echo "== a foreign wrapper that runs a file from our hooks dir keeps that file on uninstall"
KEEP_START="fleet-recall-session-start.sh"; KEEP_STOP="fleet-recall-stop.py"
cat > "$FAKE_HOME/.claude/settings.json" <<EOF
{
  "hooks": {
    "SessionStart": [
      {"matcher": "startup", "hooks": [{"type": "command", "command": "bash -lc '$OUR_START --quiet'"}]}
    ]
  }
}
EOF
snapshot keep
run --hooks > "$TMP/run-k1.out"
assert "keep: install reports skipped-foreign SessionStart only" grep -q 'settings.json: added (skipped-foreign SessionStart)$' "$TMP/run-k1.out"
assert "keep: both hook files installed" test -f "$FAKE_HOME/.claude/hooks/$KEEP_START" -a -f "$FAKE_HOME/.claude/hooks/$KEEP_STOP"
run --uninstall --dry-run > "$TMP/run-k2.out"
assert "keep: dry-run plans to keep the wrapped file" grep -q "plan: keep $FAKE_HOME/.claude/hooks/$KEEP_START (a foreign settings.json entry runs it)" "$TMP/run-k2.out"
assert "keep: dry-run plans to remove the other file" grep -q "plan: remove $FAKE_HOME/.claude/hooks/$KEEP_STOP" "$TMP/run-k2.out"
assert "keep: dry-run summary says planned-remove (kept-foreign)" grep -q "\.claude/hooks *planned-remove (kept-foreign $KEEP_START)" "$TMP/run-k2.out"
assert "keep: dry-run removed nothing" test -f "$FAKE_HOME/.claude/hooks/$KEEP_START" -a -f "$FAKE_HOME/.claude/hooks/$KEEP_STOP"
run --uninstall > "$TMP/run-k3.out"
assert "keep: uninstall says kept with the reason" grep -q "kept $FAKE_HOME/.claude/hooks/$KEEP_START (a foreign settings.json entry runs it)" "$TMP/run-k3.out"
assert "keep: wrapped file survives uninstall" test -f "$FAKE_HOME/.claude/hooks/$KEEP_START"
assert "keep: wrapped file is byte-identical to the source" cmp -s "$FAKE_HOME/.claude/hooks/$KEEP_START" "$FAKE_REPO/scripts/hooks/$KEEP_START"
assert "keep: unwrapped stop file removed" test ! -e "$FAKE_HOME/.claude/hooks/$KEEP_STOP"
assert "keep: summary says removed (kept-foreign)" grep -q "\.claude/hooks *removed (kept-foreign $KEEP_START)" "$TMP/run-k3.out"
assert "keep: our exact entries gone" test "$(exact_count "$FAKE_HOME/.claude/settings.json" SessionStart "$OUR_START")" = "0" -a "$(exact_count "$FAKE_HOME/.claude/settings.json" Stop "$OUR_STOP")" = "0"
assert "keep: foreign entry untouched, settings.json equals pre-install" test "$(canon "$FAKE_HOME/.claude/settings.json")" = "$(canon "$TMP/snap-keep/settings.json")"
assert "keep: doctor renders the survivor as installed-but-foreign" env PYTHONPATH="$SCRIPTS" python3 -c '
import pathlib, sys
from fleet_rag import doctor
rows = {r["check"]: r for r in doctor._hook_rows(pathlib.Path(sys.argv[1]))}
ok = (rows["hook:SessionStart"]["status"] == "WARN" and "foreign" in rows["hook:SessionStart"]["detail"]
      and rows["hook:Stop"]["status"] == "WARN" and "not installed" in rows["hook:Stop"]["detail"])
sys.exit(0 if ok else 1)' "$FAKE_HOME"
run --uninstall > "$TMP/run-k4.out"
assert "keep: second uninstall still keeps the file" test -f "$FAKE_HOME/.claude/hooks/$KEEP_START"
assert "keep: second uninstall summary says kept (kept-foreign)" grep -q "\.claude/hooks *kept (kept-foreign $KEEP_START)" "$TMP/run-k4.out"
# Once the wrapper is gone the file is ours to remove again.
printf '{}\n' > "$FAKE_HOME/.claude/settings.json"
run --uninstall > "$TMP/run-k5.out"
assert "keep: without the wrapper the file is removed" test ! -e "$FAKE_HOME/.claude/hooks/$KEEP_START"
assert "keep: summary says removed once the wrapper is gone" grep -q "\.claude/hooks *removed$" "$TMP/run-k5.out"

echo "== our hook sharing an entry with someone else's: only our hook is stripped"
cat > "$FAKE_HOME/.claude/settings.json" <<EOF
{
  "hooks": {
    "Stop": [
      {"hooks": [{"type": "command", "command": "$OUR_STOP", "timeout": 10},
                 {"type": "command", "command": "node /x/other.mjs"}]}
    ]
  }
}
EOF
run --hooks > "$TMP/run-s1.out"
assert "shared: Stop already ours, SessionStart added" grep -q 'settings.json: added$' "$TMP/run-s1.out"
assert "shared: Stop entry not duplicated" test "$(hook_len "$FAKE_HOME/.claude/settings.json" Stop)" = "1"
run --uninstall > "$TMP/run-s2.out"
assert "shared: removed reported" grep -q 'settings.json: removed$' "$TMP/run-s2.out"
assert "shared: our hook gone, the other hook kept in its entry" test "$(json_get "$FAKE_HOME/.claude/settings.json" hooks.Stop)" = '[{"hooks": [{"command": "node /x/other.mjs", "type": "command"}]}]'
assert "shared: SessionStart key removed again" bash -c "! python3 -c 'import json,sys;sys.exit(0 if \"SessionStart\" in json.load(open(sys.argv[1]))[\"hooks\"] else 1)' '$FAKE_HOME/.claude/settings.json'"

echo "== invalid JSON is reported, never rewritten, and does not abort the run"
printf '{not json' > "$FAKE_HOME/.claude.json"
printf '{"hooks": [broken' > "$FAKE_HOME/.claude/settings.json"
N_BAK_CLAUDE="$(ls "$FAKE_HOME"/.claude.json.bak-fleet-rag-* | wc -l | tr -d ' ')"
N_BAK_SET="$(ls "$FAKE_HOME"/.claude/settings.json.bak-fleet-rag-* | wc -l | tr -d ' ')"
if run --hooks > "$TMP/run-i1.out" 2>&1; then pass "invalid: install exits 0"; else fail "invalid: install exits 0"; fi
assert "invalid: ~/.claude.json reported skipped-invalid-json" grep -q '\.claude\.json: skipped-invalid-json' "$TMP/run-i1.out"
assert "invalid: settings.json reported skipped-invalid-json" grep -q 'settings\.json: skipped-invalid-json' "$TMP/run-i1.out"
assert "invalid: ~/.claude.json byte-identical" test "$(cat "$FAKE_HOME/.claude.json")" = '{not json'
assert "invalid: settings.json byte-identical" test "$(cat "$FAKE_HOME/.claude/settings.json")" = '{"hooks": [broken'
assert "invalid: no backup of the unparsed files" test "$(ls "$FAKE_HOME"/.claude.json.bak-fleet-rag-* | wc -l | tr -d ' ')" = "$N_BAK_CLAUDE" -a "$(ls "$FAKE_HOME"/.claude/settings.json.bak-fleet-rag-* | wc -l | tr -d ' ')" = "$N_BAK_SET"
assert "invalid: the other configs were still handled" json_has "$FAKE_HOME/.cursor/mcp.json" fleet-recall
assert "invalid: gemini still handled" json_has "$FAKE_HOME/.gemini/config/mcp_config.json" fleet-recall
assert "invalid: codex toml still handled" grep -q '^\[mcp_servers.fleet-recall\]' "$FAKE_HOME/.codex/config.toml"
assert "invalid: hook files still installed" test -f "$FAKE_HOME/.claude/hooks/fleet-recall-session-start.sh" -a -f "$FAKE_HOME/.claude/hooks/fleet-recall-stop.py"
if run --uninstall > "$TMP/run-i2.out" 2>&1; then pass "invalid: uninstall exits 0"; else fail "invalid: uninstall exits 0"; fi
assert "invalid: uninstall reports ~/.claude.json skipped-invalid-json" grep -q '\.claude\.json: skipped-invalid-json' "$TMP/run-i2.out"
assert "invalid: uninstall reports settings.json skipped-invalid-json" grep -q 'settings\.json: skipped-invalid-json' "$TMP/run-i2.out"
assert "invalid: uninstall leaves the unparsed files alone" test "$(cat "$FAKE_HOME/.claude.json")" = '{not json' -a "$(cat "$FAKE_HOME/.claude/settings.json")" = '{"hooks": [broken'
if json_has "$FAKE_HOME/.cursor/mcp.json" fleet-recall; then fail "invalid: cursor removed on uninstall"; else pass "invalid: cursor removed on uninstall"; fi
assert "invalid: hook files removed on uninstall" test ! -e "$FAKE_HOME/.claude/hooks/fleet-recall-stop.py"
cp "$TMP/snap-before/claude.json" "$FAKE_HOME/.claude.json"
cp "$TMP/snap-before/settings.json" "$FAKE_HOME/.claude/settings.json"

echo "== portable install: FLEET_RAG_HOME, and no mac-collab checkout"
ALT_HOME="$TMP/alt-home"
ALT_ROOT="$TMP/alt-root/fleet-rag"
mkdir -p "$ALT_HOME"
alt_run() { HOME="$ALT_HOME" FLEET_RAG_HOME="$ALT_ROOT" bash "$FAKE_REPO/scripts/install-fleet-rag.sh" "$@"; }

if alt_run --dry-run > "$TMP/alt-dry.out" 2>&1; then pass "alt: dry-run exits 0"; else fail "alt: dry-run exits 0"; fi
assert "alt: dry-run names the custom root" grep -q "$ALT_ROOT" "$TMP/alt-dry.out"
assert "alt: dry-run writes nothing" test ! -e "$ALT_ROOT"

if alt_run > "$TMP/alt1.out" 2>&1; then pass "alt: install exits 0"; else fail "alt: install exits 0"; fi
assert "alt: package installed under FLEET_RAG_HOME" test -f "$ALT_ROOT/fleet_rag/core.py"
assert "alt: recall executable under FLEET_RAG_HOME" test -x "$ALT_ROOT/recall"
assert "alt: recall-tunnel executable under FLEET_RAG_HOME" test -x "$ALT_ROOT/recall-tunnel"
assert "alt: state/cache/logs under FLEET_RAG_HOME" test -d "$ALT_ROOT/state" -a -d "$ALT_ROOT/cache" -a -d "$ALT_ROOT/logs"
assert "alt: nothing written to the default root" test ! -e "$ALT_HOME/apps/fleet-rag"
assert "alt: PATH symlink still installed" test "$(readlink "$ALT_HOME/.local/bin/recall")" = "$ALT_ROOT/recall"
assert "alt: recall-tunnel PATH symlink still installed" test "$(readlink "$ALT_HOME/.local/bin/recall-tunnel")" = "$ALT_ROOT/recall-tunnel"
assert "alt: mac-collab symlink skipped" grep -q 'symlink skipped' "$TMP/alt1.out"
assert "alt: mac-collab dir not created" test ! -e "$ALT_HOME/apps/mac-collab"
assert "alt: mcp args point at the custom root" test "$(json_get "$ALT_HOME/.gemini/config/mcp_config.json" mcpServers.fleet-recall.args)" = "[\"$ALT_ROOT/fleet-recall-mcp.py\"]"

if alt_run > "$TMP/alt2.out" 2>&1; then pass "alt: second install is idempotent" ; else fail "alt: second install is idempotent"; fi
assert "alt: second run reports the symlink ok" grep -q "symlink ok: $ALT_HOME/.local/bin/recall" "$TMP/alt2.out"

mkdir -p "$ALT_HOME/apps/mac-collab"
if alt_run > "$TMP/alt3.out" 2>&1; then pass "alt: install with mac-collab present exits 0"; else fail "alt: install with mac-collab present exits 0"; fi
assert "alt: mac-collab symlink made once the dir exists" test "$(readlink "$ALT_HOME/apps/mac-collab/recall")" = "$ALT_ROOT/recall"
assert "alt: mac-collab recall-tunnel symlink made once the dir exists" test "$(readlink "$ALT_HOME/apps/mac-collab/recall-tunnel")" = "$ALT_ROOT/recall-tunnel"

if alt_run --uninstall > "$TMP/alt4.out" 2>&1; then pass "alt: uninstall exits 0"; else fail "alt: uninstall exits 0"; fi
assert "alt: code removed from the custom root" test ! -e "$ALT_ROOT/fleet_rag"
assert "alt: state kept in the custom root" test -d "$ALT_ROOT/state"
assert "alt: PATH symlink removed" test ! -L "$ALT_HOME/.local/bin/recall"
assert "alt: mac-collab symlink removed" test ! -L "$ALT_HOME/apps/mac-collab/recall"
assert "alt: recall-tunnel PATH symlink removed" test ! -L "$ALT_HOME/.local/bin/recall-tunnel"
assert "alt: mac-collab recall-tunnel symlink removed" test ! -L "$ALT_HOME/apps/mac-collab/recall-tunnel"

echo "== invalid TOML is reported, never rewritten, and does not abort the run"
cp "$FAKE_HOME/.codex/config.toml" "$TMP/codex-before-bad.toml"
cat > "$FAKE_HOME/.codex/config.toml" <<'EOF'
model = "gpt-5"

[mcp_servers.github]
command = "/bin/sh"
args = [ "-c", "npx -y mcp-remote https://api.githubcopilot.com/mcp/ --header \\"Authorization: Bearer $GITHUB_MCP_TOKEN\\"" ]
EOF
cp "$FAKE_HOME/.codex/config.toml" "$TMP/codex-bad.toml"
if run > "$TMP/run-toml.out" 2>&1; then pass "invalid-toml: install exits 0"; else fail "invalid-toml: install exits 0"; fi
assert "invalid-toml: reported skipped-invalid-toml" grep -q 'config.toml: skipped-invalid-toml' "$TMP/run-toml.out"
assert "invalid-toml: byte-identical" cmp -s "$FAKE_HOME/.codex/config.toml" "$TMP/codex-bad.toml"
assert "invalid-toml: cursor still handled" json_has "$FAKE_HOME/.cursor/mcp.json" fleet-recall
cp "$TMP/codex-before-bad.toml" "$FAKE_HOME/.codex/config.toml"

echo "== refuses to clobber a real file at the symlink path"
rm -rf "$FAKE_HOME/apps/mac-collab/recall"; printf 'real\n' > "$FAKE_HOME/apps/mac-collab/recall"
if run > "$TMP/run4.out" 2>&1; then fail "real file at symlink path refused"; else pass "real file at symlink path refused"; fi
assert "real file untouched" test "$(cat "$FAKE_HOME/apps/mac-collab/recall")" = "real"

echo
if [[ "$FAILS" -eq 0 ]]; then
  echo "ALL PASS (test_installer.sh)"
else
  echo "$FAILS FAILURE(S) (test_installer.sh)"
  exit 1
fi
