#!/usr/bin/env bash
# Install the fleet-rag tooling on this Mac and register the fleet-recall MCP server
# for every platform.  Idempotent.  Honors $HOME so tests can redirect it.
#
# What it does:
#   1. Copies scripts/fleet_rag/ (no tests, no __pycache__), scripts/recall, scripts/recall-tunnel,
#      scripts/fleet-recall-mcp.py and scripts/fleet-rag.py from this checkout into
#      $FLEET_RAG_HOME (default $HOME/apps/fleet-rag/) and creates state/ cache/ logs/ there.
#   2. Symlinks $HOME/.local/bin/recall -> <install root>/recall and $HOME/.local/bin/recall-tunnel
#      -> <install root>/recall-tunnel, always.  It also links $HOME/apps/mac-collab/{recall,
#      recall-tunnel} when that directory already exists (agent shells prepend ~/apps/mac-collab;
#      board is ~/.local/bin/board) and skips it otherwise.
#   3. Registers a stdio MCP server "fleet-recall"
#        {command: "python3", args: ["<install root>/fleet-recall-mcp.py"]}
#      in ~/.claude.json, ~/.cursor/mcp.json, ~/.gemini/config/mcp_config.json,
#      ~/.codex/config.toml and ~/.grok/config.toml.  Never writes a token anywhere.
#   4. With --with-seat-mcp, also copies seat_mcp/tools.py + recall_bridge.py into
#      $HOME/apps/seat-mcp/seat_mcp/ and prints the pm2 restart command (does not run it).
#   5. With --hooks, copies scripts/hooks/fleet-recall-session-start.sh and
#      fleet-recall-stop.py into $HOME/.claude/hooks/ and appends one matcher entry each to
#      hooks.SessionStart and hooks.Stop in $HOME/.claude/settings.json (backup first; existing
#      entries untouched; idempotent).  --uninstall removes only those two entries and files;
#      ownership is exact command equality, so a wrapper that mentions a hook file is foreign
#      and is never removed.  A config that does not parse as JSON is skipped, never rewritten.
#   6. Prints a summary table of what changed.
#
# Usage:
#   bash scripts/install-fleet-rag.sh                # install / refresh
#   bash scripts/install-fleet-rag.sh --dry-run      # print the plan, write nothing
#   bash scripts/install-fleet-rag.sh --uninstall    # remove only what this script added
#   bash scripts/install-fleet-rag.sh --with-seat-mcp
#   bash scripts/install-fleet-rag.sh --hooks        # also install the Claude Code hooks
#
# Environment:
#   FLEET_RAG_HOME   install root (default $HOME/apps/fleet-rag)
#
# On-demand helper (not a background job).  Canonical doc: docs/RAG-FLEET-INFRA.md.
set -euo pipefail

DRY=0
UNINSTALL=0
WITH_SEAT=0
WITH_HOOKS=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY=1 ;;
    --uninstall) UNINSTALL=1 ;;
    --with-seat-mcp) WITH_SEAT=1 ;;
    --hooks) WITH_HOOKS=1 ;;
    -h|--help)
      sed -n '2,36p' "$0"
      exit 0
      ;;
    *)
      echo "unknown arg: $arg" >&2
      exit 2
      ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SRC="$REPO_ROOT/scripts"
HOME_DIR="${HOME:?HOME must be set}"
# Install root.  $FLEET_RAG_HOME lets somebody who does not lay their machine out like this
# repo's owner put the install anywhere; the default keeps the owner's machine unchanged.
DST="${FLEET_RAG_HOME:-$HOME_DIR/apps/fleet-rag}"
COLLAB_DIR="$HOME_DIR/apps/mac-collab"
LINK="$COLLAB_DIR/recall"
BIN_LINK="$HOME_DIR/.local/bin/recall"
TUNNEL_LINK="$COLLAB_DIR/recall-tunnel"
TUNNEL_BIN_LINK="$HOME_DIR/.local/bin/recall-tunnel"
SEAT_DST="$HOME_DIR/apps/seat-mcp/seat_mcp"
MCP_SERVER="$DST/fleet-recall-mcp.py"
SERVER_NAME="fleet-recall"
MARK="# managed by scripts/install-fleet-rag.sh (fleet-recall)"
TS="$(date +%Y%m%d%H%M%S)"

CLAUDE_JSON="$HOME_DIR/.claude.json"
CURSOR_JSON="$HOME_DIR/.cursor/mcp.json"
GEMINI_JSON="$HOME_DIR/.gemini/config/mcp_config.json"
CODEX_TOML="$HOME_DIR/.codex/config.toml"
GROK_TOML="$HOME_DIR/.grok/config.toml"
ACP_TOML="$HOME_DIR/apps/grok-acp-runtime/acp-home-config.toml"
HOOKS_DIR="$HOME_DIR/.claude/hooks"
SETTINGS_JSON="$HOME_DIR/.claude/settings.json"
HOOK_START="fleet-recall-session-start.sh"
HOOK_STOP="fleet-recall-stop.py"
HOOK_PROMPT_GATE="fleet-recall-prompt-gate.py"

PY="$(command -v python3)"
SUMMARY=()

say() { printf '%s\n' "$*"; }
note() { SUMMARY+=("$1|$2"); }

mode_word() {
  if [[ "$DRY" -eq 1 ]]; then printf 'plan'; else printf 'did'; fi
}

# ---------------------------------------------------------------- step 1: files

install_files() {
  local missing=0
  for f in fleet_rag recall recall-tunnel fleet-recall-mcp.py fleet-rag.py; do
    if [[ ! -e "$SRC/$f" ]]; then
      say "MISSING $SRC/$f (this checkout is incomplete; the install would fail)"
      missing=1
    fi
  done
  if [[ "$DRY" -eq 1 ]]; then
    say "plan: rsync --delete $SRC/fleet_rag/ -> $DST/fleet_rag/ (excluding tests, __pycache__)"
    say "plan: copy recall, recall-tunnel, fleet-recall-mcp.py, fleet-rag.py -> $DST/ (chmod +x recall, recall-tunnel, mcp server)"
    say "plan: mkdir -p $DST/{state,cache,logs}"
    note "$DST" "planned"
    return 0
  fi
  if [[ "$missing" -eq 1 ]]; then
    say "refusing to install from an incomplete checkout" >&2
    exit 1
  fi
  mkdir -p "$DST/state" "$DST/cache" "$DST/logs"
  rsync -a --delete --exclude 'tests' --exclude '__pycache__' --exclude '*.pyc' \
    "$SRC/fleet_rag/" "$DST/fleet_rag/"
  cp -p "$SRC/recall" "$DST/recall"
  cp -p "$SRC/recall-tunnel" "$DST/recall-tunnel"
  cp -p "$SRC/fleet-recall-mcp.py" "$DST/fleet-recall-mcp.py"
  cp -p "$SRC/fleet-rag.py" "$DST/fleet-rag.py"
  chmod +x "$DST/recall" "$DST/recall-tunnel" "$DST/fleet-recall-mcp.py" "$DST/fleet-rag.py"
  say "installed $DST (fleet_rag/, recall, recall-tunnel, fleet-recall-mcp.py, fleet-rag.py, state/ cache/ logs/)"
  note "$DST" "installed"
}

uninstall_files() {
  if [[ "$DRY" -eq 1 ]]; then
    say "plan: remove $DST/fleet_rag/, recall, recall-tunnel, fleet-recall-mcp.py, fleet-rag.py (keep state/ cache/ logs/)"
    note "$DST" "planned-remove"
    return 0
  fi
  if [[ -d "$DST" ]]; then
    rm -rf "$DST/fleet_rag" "$DST/recall" "$DST/recall-tunnel" "$DST/fleet-recall-mcp.py" "$DST/fleet-rag.py"
    say "removed code from $DST (state/ cache/ logs/ kept; delete by hand if unwanted)"
    note "$DST" "removed"
  else
    note "$DST" "absent"
  fi
}

# ---------------------------------------------------------------- step 2: symlink

install_one_link() {
  local link="$1"
  local target="$2"
  if [[ -L "$link" ]]; then
    local cur
    cur="$(readlink "$link")"
    if [[ "$cur" == "$target" ]]; then
      say "symlink ok: $link -> $target"
      note "$link" "ok"
      return 0
    fi
    if [[ "$DRY" -eq 1 ]]; then
      say "plan: replace symlink $link ($cur) -> $target"
      note "$link" "planned"
      return 0
    fi
    ln -sfn "$target" "$link"
    say "replaced symlink $link -> $target (was $cur)"
    note "$link" "replaced"
    return 0
  fi
  if [[ -e "$link" ]]; then
    say "REFUSING: $link exists and is a real file, not a symlink.  Move it aside first." >&2
    note "$link" "refused"
    [[ "$DRY" -eq 1 ]] || exit 1
    return 0
  fi
  if [[ "$DRY" -eq 1 ]]; then
    say "plan: symlink $link -> $target"
    note "$link" "planned"
    return 0
  fi
  mkdir -p "$(dirname "$link")"
  ln -s "$target" "$link"
  say "symlinked $link -> $target"
  note "$link" "added"
}

uninstall_one_link() {
  local link="$1"
  local target="$2"
  if [[ -L "$link" && "$(readlink "$link")" == "$target" ]]; then
    if [[ "$DRY" -eq 1 ]]; then
      say "plan: remove symlink $link"
      note "$link" "planned-remove"
    else
      rm -f "$link"
      say "removed symlink $link"
      note "$link" "removed"
    fi
  else
    note "$link" "not-ours"
  fi
}

# ~/.local/bin/{recall,recall-tunnel} are always installed; the mac-collab links are a
# convenience for machines that actually have that checkout, so they are only made when the
# directory already exists.
install_link() {
  if [[ -d "$COLLAB_DIR" ]]; then
    install_one_link "$LINK" "$DST/recall"
    install_one_link "$TUNNEL_LINK" "$DST/recall-tunnel"
  else
    say "symlink skipped: $COLLAB_DIR does not exist (no mac-collab checkout here)"
    note "$LINK" "skipped-no-mac-collab"
    note "$TUNNEL_LINK" "skipped-no-mac-collab"
  fi
  install_one_link "$BIN_LINK" "$DST/recall"
  install_one_link "$TUNNEL_BIN_LINK" "$DST/recall-tunnel"
}
uninstall_link() {
  uninstall_one_link "$LINK" "$DST/recall"
  uninstall_one_link "$BIN_LINK" "$DST/recall"
  uninstall_one_link "$TUNNEL_LINK" "$DST/recall-tunnel"
  uninstall_one_link "$TUNNEL_BIN_LINK" "$DST/recall-tunnel"
}

# ---------------------------------------------------------------- step 3: JSON configs

# json_cfg <path> <add|remove> <create-if-missing 0|1>
# Prints one of: added | unchanged | removed | absent | skipped-not-ours | skipped-invalid-json |
# planned-add | planned-remove.  A file that does not parse as JSON is reported and left alone.
json_cfg() {
  local path="$1" action="$2" create="$3"
  "$PY" - "$path" "$action" "$create" "$DRY" "$SERVER_NAME" "$MCP_SERVER" "$TS" <<'PY'
import json, os, sys, tempfile, shutil
path, action, create, dry, name, server, ts = sys.argv[1:8]
dry = dry == "1"
entry = {"command": "python3", "args": [server]}
if not os.path.exists(path):
    if action == "remove":
        print("absent"); sys.exit(0)
    if create != "1":
        print("absent"); sys.exit(0)
    data = {"mcpServers": {}}
    existed = False
else:
    with open(path, encoding="utf-8") as fh:
        raw = fh.read()
    try:
        data = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        # Never rewrite a file we cannot parse; the other configs still get handled.
        print("skipped-invalid-json"); sys.exit(0)
    existed = True
if not isinstance(data, dict):
    print("skipped-not-object"); sys.exit(0)
servers = data.get("mcpServers")
if servers is None:
    servers = {}
if not isinstance(servers, dict):
    print("skipped-bad-mcpServers"); sys.exit(0)
current = servers.get(name)
if action == "add":
    if current == entry:
        print("unchanged"); sys.exit(0)
    if current is not None and current != entry:
        # Someone registered a different fleet-recall by hand; leave it alone.
        print("skipped-foreign"); sys.exit(0)
    if dry:
        print("planned-add"); sys.exit(0)
    servers[name] = entry
    data["mcpServers"] = servers
else:
    if current is None:
        print("absent"); sys.exit(0)
    if current != entry:
        print("skipped-not-ours"); sys.exit(0)
    if dry:
        print("planned-remove"); sys.exit(0)
    del servers[name]
    data["mcpServers"] = servers
os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
if existed:
    shutil.copy2(path, f"{path}.bak-fleet-rag-{ts}")
fd, tmp = tempfile.mkstemp(prefix=".fleet-rag.", dir=os.path.dirname(path) or ".")
with os.fdopen(fd, "w", encoding="utf-8") as fh:
    json.dump(data, fh, indent=2, ensure_ascii=False)
    fh.write("\n")
if existed:
    try:
        os.chmod(tmp, os.stat(path).st_mode & 0o777)
    except OSError:
        pass
else:
    os.chmod(tmp, 0o600)
os.replace(tmp, path)
print("added" if action == "add" else "removed")
PY
}

# ---------------------------------------------------------------- step 3: TOML configs

# toml_cfg <path> <add|remove> <extra-line or "">
# Appends a marked [mcp_servers.fleet-recall] block only when none exists; remove deletes
# only the block this script added (identified by the marker line).  Never rewrites the rest.
toml_cfg() {
  local path="$1" action="$2" extra="$3"
  "$PY" - "$path" "$action" "$extra" "$DRY" "$SERVER_NAME" "$MCP_SERVER" "$TS" "$MARK" <<'PY'
import json, os, re, shutil, sys, tomllib
path, action, extra, dry, name, server, ts, mark = sys.argv[1:9]
dry = dry == "1"
header = f"[mcp_servers.{name}]"

def parses(s: str) -> bool:
    try:
        tomllib.loads(s)
        return True
    except tomllib.TOMLDecodeError:
        return False

if not os.path.exists(path):
    print("absent"); sys.exit(0)
with open(path, encoding="utf-8") as fh:
    text = fh.read()
if not parses(text):
    # 2026-09-16: JSON-style \\" inside a TOML basic string made Codex refuse
    # the whole file ("missing comma between array elements").  Do not append.
    print("skipped-invalid-toml"); sys.exit(0)
lines = text.split("\n")
has_header = any(l.strip() == header for l in lines)
if action == "add":
    if has_header:
        print("unchanged"); sys.exit(0)
    if dry:
        print("planned-add"); sys.exit(0)
    block = [mark, header, 'command = "python3"', "args = [" + json.dumps(server) + "]"]
    if extra:
        block.append(extra)
    new = text
    if new and not new.endswith("\n"):
        new += "\n"
    if new and not new.endswith("\n\n"):
        new += "\n"
    new += "\n".join(block) + "\n"
    if not parses(new):
        print("skipped-invalid-toml"); sys.exit(0)
else:
    if not has_header:
        print("absent"); sys.exit(0)
    # Find our marker; the block runs from the marker to the next table header or EOF.
    try:
        start = next(i for i, l in enumerate(lines) if l.strip() == mark and i + 1 < len(lines) and lines[i + 1].strip() == header)
    except StopIteration:
        print("skipped-not-ours"); sys.exit(0)
    end = start + 2
    while end < len(lines) and not re.match(r"^\s*\[", lines[end]):
        end += 1
    # Also drop the single blank separator line we added before the block.
    if start > 0 and lines[start - 1].strip() == "":
        start -= 1
    if dry:
        print("planned-remove"); sys.exit(0)
    new = "\n".join(lines[:start] + lines[end:])
    if not new.endswith("\n"):
        new += "\n"
shutil.copy2(path, f"{path}.bak-fleet-rag-{ts}")
tmp = f"{path}.fleet-rag.tmp"
with open(tmp, "w", encoding="utf-8") as fh:
    fh.write(new)
try:
    os.chmod(tmp, os.stat(path).st_mode & 0o777)
except OSError:
    pass
os.replace(tmp, path)
print("added" if action == "add" else "removed")
PY
}

register_all() {
  local action="$1" r
  r="$(json_cfg "$CLAUDE_JSON" "$action" 0)";  say "$CLAUDE_JSON: $r";  note "$CLAUDE_JSON" "$r"
  r="$(json_cfg "$CURSOR_JSON" "$action" 0)";  say "$CURSOR_JSON: $r";  note "$CURSOR_JSON" "$r"
  r="$(json_cfg "$GEMINI_JSON" "$action" 1)";  say "$GEMINI_JSON: $r";  note "$GEMINI_JSON" "$r"
  r="$(toml_cfg "$CODEX_TOML" "$action" "")";  say "$CODEX_TOML: $r";  note "$CODEX_TOML" "$r"
  # Grok's existing [mcp_servers.X] blocks use command / args / enabled.
  r="$(toml_cfg "$GROK_TOML" "$action" "enabled = true")"; say "$GROK_TOML: $r"; note "$GROK_TOML" "$r"
  # grok-acp stripped home (Conductor / Shellular).  Absent until grok-acp-runtime is installed.
  r="$(toml_cfg "$ACP_TOML" "$action" "enabled = true")"; say "$ACP_TOML: $r"; note "$ACP_TOML" "$r"
}

# ---------------------------------------------------------------- step 4: seat-mcp

install_seat_mcp() {
  local files=(tools.py recall_bridge.py)
  for f in "${files[@]}"; do
    if [[ ! -f "$SRC/seat-mcp/seat_mcp/$f" ]]; then
      say "MISSING $SRC/seat-mcp/seat_mcp/$f" >&2
      [[ "$DRY" -eq 1 ]] || exit 1
    fi
  done
  if [[ "$DRY" -eq 1 ]]; then
    say "plan: copy seat_mcp/{tools.py,recall_bridge.py} -> $SEAT_DST/"
    note "$SEAT_DST" "planned"
  else
    mkdir -p "$SEAT_DST"
    for f in "${files[@]}"; do
      cp -p "$SRC/seat-mcp/seat_mcp/$f" "$SEAT_DST/$f"
    done
    say "installed $SEAT_DST/{tools.py,recall_bridge.py}"
    note "$SEAT_DST" "installed"
  fi
  say "next: pm2 restart seat-mcp   # not run by this script"
}

# ---------------------------------------------------------------- step 5: Claude Code hooks

# settings_hooks <add|remove>
# Appends exactly one matcher entry to hooks.SessionStart and hooks.Stop, or removes only those.
# An entry is ours only when a hook command equals exactly what this script writes
# ("<hooks_dir>/fleet-recall-session-start.sh" / "python3 <hooks_dir>/fleet-recall-stop.py"); an
# entry that merely mentions the file name (a wrapper, another hooks dir) is foreign: it is left
# alone on --uninstall, reported as "skipped-foreign <event>", and does not stop ours from being
# added.  Every other key and entry is kept byte-for-byte in content (the file is re-serialized
# with indent 2).  A settings.json that does not parse is reported and never rewritten.
# Prints: added | unchanged | removed | absent | skipped-not-object | skipped-invalid-json |
# planned-add | planned-remove, optionally followed by " (skipped-foreign <event> ...)".
settings_hooks() {
  local action="$1"
  "$PY" - "$SETTINGS_JSON" "$action" "$DRY" "$HOOKS_DIR" "$HOOK_START" "$HOOK_STOP" "$HOOK_PROMPT_GATE" "$TS" <<'PY'
import json, os, shutil, sys, tempfile
path, action, dry, hooks_dir, start, stop, prompt_gate, ts = sys.argv[1:9]
dry = dry == "1"
ours = {
    "SessionStart": {"matcher": "startup|resume",
                     "hooks": [{"type": "command", "command": f"{hooks_dir}/{start}",
                                "timeout": 5, "statusMessage": "Fleet recall corpus"}]},
    "Stop": {"hooks": [{"type": "command", "command": f"python3 {hooks_dir}/{stop}", "timeout": 10}]},
    "UserPromptSubmit": {"hooks": [{"type": "command", "command": f"python3 {hooks_dir}/{prompt_gate}", "timeout": 3}]},
}
names = {"SessionStart": start, "Stop": stop, "UserPromptSubmit": prompt_gate}
# The one command string this script writes per event.  Ownership is exact equality on it: a
# wrapper that merely mentions the hook file ("bash -lc '.../fleet-recall-stop.py'", a different
# hooks dir, an extra flag) is somebody else's entry and is never added to, removed, or counted.
exact = {event: entry["hooks"][0]["command"] for event, entry in ours.items()}

def commands(entry):
    if not isinstance(entry, dict):
        return []
    return [str(h.get("command", "")) for h in entry.get("hooks") or [] if isinstance(h, dict)]

def is_ours(entry, event):
    return exact[event] in commands(entry)

def is_foreign(entry, event):
    return not is_ours(entry, event) and any(names[event] in c for c in commands(entry))

def strip_ours(entry, event):
    """The entry without our hook; None when our hook was the only one in it."""
    kept = [h for h in entry.get("hooks") or []
            if not (isinstance(h, dict) and h.get("command") == exact[event])]
    if not kept:
        return None
    return {**entry, "hooks": kept}

if not os.path.exists(path):
    if action == "remove":
        print("absent"); sys.exit(0)
    data, existed = {}, False
else:
    with open(path, encoding="utf-8") as fh:
        raw = fh.read()
    try:
        data = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        # Never rewrite a file we cannot parse; the other configs still get handled.
        print("skipped-invalid-json"); sys.exit(0)
    existed = True
if not isinstance(data, dict):
    print("skipped-not-object"); sys.exit(0)
hooks = data.get("hooks")
if hooks is None:
    hooks = {}
if not isinstance(hooks, dict):
    print("skipped-bad-hooks"); sys.exit(0)
changed = False
foreign = []

def done(word):
    # e.g. "added (skipped-foreign Stop)": ours went in, a look-alike entry was left alone.
    print(word + (" (skipped-foreign " + " ".join(foreign) + ")" if foreign else "")); sys.exit(0)

for event, entry in ours.items():
    arr = hooks.get(event)
    if arr is None:
        arr = []
    if not isinstance(arr, list):
        print("skipped-bad-hooks"); sys.exit(0)
    if any(is_foreign(e, event) for e in arr):
        foreign.append(event)
    have = [e for e in arr if is_ours(e, event)]
    if action == "add":
        if not have:
            arr = arr + [entry]; changed = True
        hooks[event] = arr
    elif have:
        arr = [e for e in (strip_ours(e, event) if is_ours(e, event) else e for e in arr) if e is not None]
        changed = True
        if arr:
            hooks[event] = arr
        else:
            hooks.pop(event, None)      # we were the only entry: restore the pre-install shape
if not changed:
    done("unchanged" if action == "add" else "absent")
if dry:
    done("planned-add" if action == "add" else "planned-remove")
if hooks:
    data["hooks"] = hooks
else:
    data.pop("hooks", None)
os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
if existed:
    shutil.copy2(path, f"{path}.bak-fleet-rag-{ts}")
fd, tmp = tempfile.mkstemp(prefix=".fleet-rag.", dir=os.path.dirname(path) or ".")
with os.fdopen(fd, "w", encoding="utf-8") as fh:
    json.dump(data, fh, indent=2, ensure_ascii=False)
    fh.write("\n")
if existed:
    try:
        os.chmod(tmp, os.stat(path).st_mode & 0o777)
    except OSError:
        pass
else:
    os.chmod(tmp, 0o600)
os.replace(tmp, path)
done("added" if action == "add" else "removed")
PY
}

install_hooks() {
  local f r
  for f in "$HOOK_START" "$HOOK_STOP" "$HOOK_PROMPT_GATE"; do
    if [[ ! -f "$SRC/hooks/$f" ]]; then
      say "MISSING $SRC/hooks/$f" >&2
      [[ "$DRY" -eq 1 ]] || exit 1
    fi
  done
  if [[ "$DRY" -eq 1 ]]; then
    say "plan: copy hooks/{$HOOK_START,$HOOK_STOP,$HOOK_PROMPT_GATE} -> $HOOKS_DIR/ (chmod +x)"
    note "$HOOKS_DIR" "planned"
  else
    mkdir -p "$HOOKS_DIR"
    for f in "$HOOK_START" "$HOOK_STOP" "$HOOK_PROMPT_GATE"; do
      cp -p "$SRC/hooks/$f" "$HOOKS_DIR/$f"
      chmod +x "$HOOKS_DIR/$f"
    done
    say "installed $HOOKS_DIR/{$HOOK_START,$HOOK_STOP,$HOOK_PROMPT_GATE}"
    note "$HOOKS_DIR" "installed"
  fi
  r="$(settings_hooks add)"; say "$SETTINGS_JSON: $r"; note "$SETTINGS_JSON" "$r"
}

# foreign_hook_files: the hook file names (one per line) that some settings.json entry other
# than ours still runs from $HOOKS_DIR.  --uninstall only removes our exact settings.json
# entries, so a wrapper like "bash -lc '$HOOKS_DIR/fleet-recall-stop.py'" survives; deleting
# the file under it would leave that entry failing on every session.  Unparseable or oddly
# shaped settings.json yields nothing (the files are removed as before).
foreign_hook_files() {
  "$PY" - "$SETTINGS_JSON" "$HOOKS_DIR" "$HOOK_START" "$HOOK_STOP" "$HOOK_PROMPT_GATE" <<'PY'
import json, sys
path, hooks_dir, start, stop, prompt_gate = sys.argv[1:6]
exact = {
    start: f"{hooks_dir}/{start}",
    stop: f"python3 {hooks_dir}/{stop}",
    prompt_gate: f"python3 {hooks_dir}/{prompt_gate}",
}
try:
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
except (OSError, ValueError):
    sys.exit(0)
hooks = data.get("hooks") if isinstance(data, dict) else None
if not isinstance(hooks, dict):
    sys.exit(0)
commands = []
for arr in hooks.values():
    for entry in arr if isinstance(arr, list) else []:
        if isinstance(entry, dict):
            for h in entry.get("hooks") or []:
                if isinstance(h, dict):
                    commands.append(str(h.get("command", "")))
for fname, ours in exact.items():
    if any(f"{hooks_dir}/{fname}" in c and c != ours for c in commands):
        print(fname)
PY
}

uninstall_hooks() {
  local f r kept=()
  r="$(settings_hooks remove)"; say "$SETTINGS_JSON: $r"; note "$SETTINGS_JSON" "$r"
  local foreign
  foreign="$(foreign_hook_files)"
  local any=0 removed=0
  for f in "$HOOK_START" "$HOOK_STOP" "$HOOK_PROMPT_GATE"; do
    if [[ -f "$HOOKS_DIR/$f" ]]; then
      any=1
      if grep -qxF "$f" <<<"$foreign"; then
        kept+=("$f")
        if [[ "$DRY" -eq 1 ]]; then
          say "plan: keep $HOOKS_DIR/$f (a foreign settings.json entry runs it)"
        else
          say "kept $HOOKS_DIR/$f (a foreign settings.json entry runs it)"
        fi
      elif [[ "$DRY" -eq 1 ]]; then
        say "plan: remove $HOOKS_DIR/$f"
      else
        rm -f "$HOOKS_DIR/$f"
        removed=1
        say "removed $HOOKS_DIR/$f"
      fi
    fi
  done
  local word
  if [[ "$any" -eq 0 ]]; then word="absent"
  elif [[ "$DRY" -eq 1 ]]; then word="planned-remove"
  elif [[ "$removed" -eq 0 ]]; then word="kept"
  else word="removed"; fi
  if [[ "${#kept[@]}" -gt 0 ]]; then word="$word (kept-foreign ${kept[*]})"; fi
  note "$HOOKS_DIR" "$word"
}

# ---------------------------------------------------------------- main

if [[ "$DRY" -eq 1 ]]; then say "== fleet-rag installer: DRY RUN (nothing written) =="; fi
if [[ "$UNINSTALL" -eq 1 ]]; then
  say "== fleet-rag installer: uninstall =="
  register_all remove
  uninstall_hooks
  uninstall_link
  uninstall_files
else
  say "== fleet-rag installer: install =="
  install_files
  install_link
  register_all add
  if [[ "$WITH_SEAT" -eq 1 ]]; then
    install_seat_mcp
  else
    say "seat-mcp: skipped (pass --with-seat-mcp to refresh $SEAT_DST)"
    note "$SEAT_DST" "skipped"
  fi
  if [[ "$WITH_HOOKS" -eq 1 ]]; then
    install_hooks
  else
    say "hooks: skipped (pass --hooks to install the Claude Code SessionStart/Stop hooks)"
    note "$HOOKS_DIR" "skipped"
  fi
fi

say ""
say "== summary ($(mode_word)) =="
printf '%-60s %s\n' "target" "result"
printf '%-60s %s\n' "------" "------"
for row in "${SUMMARY[@]}"; do
  printf '%-60s %s\n' "${row%%|*}" "${row#*|}"
done
if [[ "$UNINSTALL" -eq 0 && "$DRY" -eq 0 ]]; then
  say ""
  say "next: restart Claude Code / Cursor / Gemini / Codex / Grok sessions so they pick up '$SERVER_NAME'."
fi
