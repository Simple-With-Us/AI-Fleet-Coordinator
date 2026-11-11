#!/usr/bin/env bash
# Install Claude's wake router unit on the server (OPS-1048, AFC-694).
#
#   bash scripts/simple_tools/server/install-claude-wake-router.sh --dry-run   # show the plan, change nothing
#   sudo bash scripts/simple_tools/server/install-claude-wake-router.sh        # install or update the unit
#   sudo bash scripts/simple_tools/server/install-claude-wake-router.sh --start  # ... and enable and start it
#
# Run it from a clean AI-Fleet-Coordinator checkout at origin/main;  a real run refuses any other.
# Safe to run again:  an unchanged unit is left alone.  It never writes a secret, never mints a
# token, never changes an Infisical value and never starts the router unless --start is passed.
#
# What it checks first, before it changes anything (a real run stops at the first problem;  a dry
# run names every one and still exits 0, so the whole plan shows):
#   1. the checkout is clean and at origin/main, and carries the reviewed unit (port 8794, KillMode
#      mixed);
#   2. the unit's user has the installed runtime its ExecStart runs
#      (~/.local/share/fleet-cli/current/scripts/simple-chat, with the wake-router command);
#   3. the env file the unit reads (/etc/claude-wake-router.env) is a root-owned regular file with
#      no group or other access, and names FLEET_KNOBS_INFISICAL_TOKEN with a value.  Only names
#      are read from it, plus three non-secret settings:  CLAUDE_WAKE_BIND (never a wildcard
#      address), CLAUDE_WAKE_PORT (never 8793, which is zc-wake-tls.service's) and
#      CLAUDE_WAKE_KEY_FILE.  The token value is never printed;
#   4. the routine key file is a regular file the unit's user owns, mode 0600 or 0400, at least
#      16 characters, the rule the router itself applies (wake_router.read_key);
#   5. the delivery knobs.  WAKE_ROUTER_DELIVERY_ENABLED and WAKE_ROUTER_DELIVERY_SEATS must both be
#      set in Infisical (AI Fleet Coordinator prod, read fresh with the env file's token), so the
#      router never starts on defaults nobody chose.  "false" is a valid setting:  the router then
#      picks and receipts every wake and delivers nothing.  The script prints whether delivery
#      would be armed for CLAUDE (enabled true and CLAUDE in the seats) or held.
# Then:
#   6. installs claude-wake-router.service as /etc/systemd/system/afc-claude-wake-router.service
#      (0644, root) and reloads systemd when it changed;
#   7. with --start only:  systemctl enable --now, or a restart when the unit changed and it was
#      already running.  Without --start it prints the command and leaves the unit as it was.
#
# Inputs (environment, all optional):
#   CLAUDE_WAKE_INSTALL_TEST_ROOT  offline tests only:  every destination goes under this directory,
#                                  systemctl and knob-read must be stubs in its bin/, ownership is
#                                  checked against the caller instead of root and the unit's user,
#                                  and the checkout check (step 1) is skipped.
set -euo pipefail

HERE="$(cd -P -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SIMPLE_TOOLS="$(cd -P -- "$HERE/.." && pwd)"
SCRIPTS="$(cd -P -- "$SIMPLE_TOOLS/.." && pwd)"
REPO="$(cd -P -- "$SCRIPTS/.." && pwd)"
SOURCE_UNIT="$HERE/claude-wake-router.service"
UNIT_NAME=afc-claude-wake-router.service
TOKEN_NAME=FLEET_KNOBS_INFISICAL_TOKEN
ZC_TLS_PORT=8793

DRY=0
START=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY=1 ;;
    --start) START=1 ;;
    -h|--help) sed -n '2,45p' "$0"; exit 0 ;;
    *) echo "install-claude-wake-router: unknown argument: $arg" >&2; exit 2 ;;
  esac
done

say() { printf 'install-claude-wake-router: %s\n' "$*"; }
die() { say "$1" >&2; exit "${2:-1}"; }

# A problem with the host or the checkout:  a real run stops;  a dry run reports it and goes on.
problems=0
refuse() {
  problems=$((problems + 1))
  if [[ "$DRY" -eq 1 ]]; then
    say "a real run would refuse: $1"
  else
    die "$1"
  fi
}

# Mode, owner uid and group gid of a path, on Linux (GNU stat) or macOS (BSD stat).
meta() { stat -c '%a %u %g' -- "$1" 2>/dev/null || stat -f '%Lp %u %g' -- "$1"; }

# --- where things go:  the real host, or a stubbed test root ---------------------------------

TEST_ROOT="${CLAUDE_WAKE_INSTALL_TEST_ROOT:-}"
if [[ -n "$TEST_ROOT" ]]; then
  [[ "$TEST_ROOT" == /* && -d "$TEST_ROOT" && ! -L "$TEST_ROOT" ]] ||
    die "CLAUDE_WAKE_INSTALL_TEST_ROOT must be an existing absolute directory" 2
  for tool in systemctl knob-read; do
    [[ -x "$TEST_ROOT/bin/$tool" && ! -L "$TEST_ROOT/bin/$tool" ]] ||
      die "test mode needs a stub $TEST_ROOT/bin/$tool" 2
  done
  PATH="$TEST_ROOT/bin:$PATH"
  PREFIX="$TEST_ROOT"
  ROOT_UID="$(id -u)"
  ROOT_GID="$(id -g)"
else
  PREFIX=""
  ROOT_UID=0
  ROOT_GID=0
fi
PYTHON="$(command -v python3 || true)"
[[ -n "$PYTHON" ]] || die "python3 is missing"
UNIT_DIR="$PREFIX/etc/systemd/system"
UNIT_DST="$UNIT_DIR/$UNIT_NAME"

[[ "$DRY" -eq 1 ]] && say "dry run: nothing will be changed"
[[ "$DRY" -eq 1 && "$START" -eq 1 ]] && say "dry run: --start is shown, not done"

# --- 1. the checkout and the reviewed unit ---------------------------------------------------

[[ -f "$SOURCE_UNIT" ]] || die "missing source $SOURCE_UNIT"

repo_git() { git -c safe.directory="$REPO" -C "$REPO" "$@"; }
checkout_problem() {
  repo_git rev-parse --git-dir >/dev/null 2>&1 || { echo "$REPO is not a git checkout"; return; }
  if [[ -n "$(repo_git status --porcelain --untracked-files=no 2>/dev/null)" ]]; then
    echo "the checkout $REPO has uncommitted changes; install only reviewed code"
    return
  fi
  local head main
  head="$(repo_git rev-parse HEAD)"
  main="$(repo_git rev-parse --verify -q refs/remotes/origin/main || true)"
  [[ -n "$main" ]] || { echo "the checkout has no origin/main to compare with; git fetch first"; return; }
  [[ "$head" == "$main" ]] ||
    echo "the checkout is at ${head:0:9}, not origin/main (${main:0:9}); git fetch, then check out origin/main"
}
if [[ -z "$TEST_ROOT" ]]; then
  problem="$(checkout_problem)"
  [[ -z "$problem" ]] || refuse "$problem"
fi
grep -q 'CLAUDE_WAKE_PORT:-8794' "$SOURCE_UNIT" ||
  refuse "claude-wake-router.service does not default to port 8794: this checkout predates #1783"
grep -q '^KillMode=mixed' "$SOURCE_UNIT" ||
  refuse "claude-wake-router.service has no KillMode=mixed: a stop would end running deliveries"

# The unit names its own user, home and env file;  this script reads them from it, so the two
# can never disagree.
unit_value() { sed -n "s/^$1=//p" "$SOURCE_UNIT" | head -n 1; }
RUN_USER="$(unit_value User)"
RUN_HOME="$(sed -n 's/^Environment=HOME=//p' "$SOURCE_UNIT" | head -n 1)"
ENV_FILE_NAME="$(unit_value EnvironmentFile)"
ENV_FILE_NAME="${ENV_FILE_NAME#-}"
[[ "$RUN_USER" =~ ^[a-z_][a-z0-9_-]{0,31}$ ]] || die "the unit's User= is missing or malformed"
[[ "$RUN_HOME" == /* ]] || die "the unit's Environment=HOME= is missing or not absolute"
[[ "$ENV_FILE_NAME" == /* ]] || die "the unit's EnvironmentFile= is missing or not absolute"
HOME_DIR="$PREFIX$RUN_HOME"
ENV_FILE="$PREFIX$ENV_FILE_NAME"
if [[ -n "$TEST_ROOT" ]]; then
  RUN_UID="$(id -u)"
else
  RUN_UID="$(id -u "$RUN_USER" 2>/dev/null || true)"
  [[ -n "$RUN_UID" ]] || refuse "the unit's user $RUN_USER does not exist on this host"
fi
say "unit user $RUN_USER, home $RUN_HOME, env file $ENV_FILE_NAME"

# --- 2. the installed runtime the unit runs --------------------------------------------------

SIMPLE_CHAT="$HOME_DIR/.local/share/fleet-cli/current/scripts/simple-chat"
if [[ ! -f "$SIMPLE_CHAT" ]]; then
  refuse "$SIMPLE_CHAT is missing: install the fleet-cli runtime for $RUN_USER first"
elif ! grep -q 'wake-router' "$SIMPLE_CHAT"; then
  refuse "$SIMPLE_CHAT has no wake-router command: the installed runtime predates the router"
else
  say "runtime ok: $SIMPLE_CHAT has the wake-router command"
fi

# --- 3. the env file:  names only, plus three non-secret settings ----------------------------

BIND="127.0.0.1"
PORT="8794"
KEY_FILE_NAME="$RUN_HOME/.local/state/claude-wake/routine-key"
env_ok=0
if [[ -L "$ENV_FILE" || ( -e "$ENV_FILE" && ! -f "$ENV_FILE" ) ]]; then
  refuse "$ENV_FILE_NAME is not a regular file (a symlink is refused)"
elif [[ ! -e "$ENV_FILE" ]]; then
  refuse "$ENV_FILE_NAME is missing: create it (root, mode 0600) with $TOKEN_NAME=<the read-only knob token>"
else
  read -r env_mode env_uid _ < <(meta "$ENV_FILE")
  if [[ "$env_uid" != "$ROOT_UID" ]] || (( 8#$env_mode & 8#077 )); then
    refuse "$ENV_FILE_NAME must be root-owned with no group or other access (it is mode $env_mode, uid $env_uid)"
  else
    # Prints has_token=0|1 and the three non-secret settings when present.  Never a token value.
    env_facts="$("$PYTHON" -I - "$ENV_FILE" "$TOKEN_NAME" <<'PY'
import sys

path, token_name = sys.argv[1], sys.argv[2]
SHOWN = ("CLAUDE_WAKE_BIND", "CLAUDE_WAKE_PORT", "CLAUDE_WAKE_KEY_FILE")
found = {}
with open(path, "rb") as handle:
    data = handle.read(64 * 1024)
for line in data.decode("utf-8", "replace").splitlines():
    line = line.strip()
    if line.startswith("export "):
        line = line[len("export "):].strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    key, _, value = line.partition("=")
    found[key.strip()] = value.strip().strip("'\"")
print("has_token=%d" % (1 if found.get(token_name) else 0))
for name in SHOWN:
    value = found.get(name, "")
    if value and all(c.isalnum() or c in "._:/-[]" for c in value):
        print("%s=%s" % (name, value))
    elif value:
        print("%s=<unreadable>" % name)
PY
)" || die "could not read $ENV_FILE_NAME"
    has_token=0
    while IFS='=' read -r name value; do
      case "$name" in
        has_token) has_token="$value" ;;
        CLAUDE_WAKE_BIND) BIND="$value" ;;
        CLAUDE_WAKE_PORT) PORT="$value" ;;
        CLAUDE_WAKE_KEY_FILE) KEY_FILE_NAME="$value" ;;
      esac
    done <<<"$env_facts"
    if [[ "$has_token" != 1 ]]; then
      refuse "$ENV_FILE_NAME has no $TOKEN_NAME value: the router could not read its knobs"
    else
      env_ok=1
      say "env file ok: $ENV_FILE_NAME is root-only and names $TOKEN_NAME (value not shown)"
    fi
  fi
fi
case "$BIND" in
  0.0.0.0|::|"[::]"|"*"|""|"<unreadable>")
    refuse "CLAUDE_WAKE_BIND must be loopback or the tailnet address, never a wildcard (it is '${BIND}')" ;;
esac
if [[ ! "$PORT" =~ ^[0-9]{1,5}$ ]] || (( PORT < 1 || PORT > 65535 )); then
  refuse "CLAUDE_WAKE_PORT must be a port number (it is '$PORT')"
elif [[ "$PORT" == "$ZC_TLS_PORT" ]]; then
  refuse "CLAUDE_WAKE_PORT is $ZC_TLS_PORT, which zc-wake-tls.service holds on the server"
fi
say "the router would listen on $BIND:$PORT"

# --- 4. the routine key ----------------------------------------------------------------------

[[ "$KEY_FILE_NAME" == /* ]] || refuse "CLAUDE_WAKE_KEY_FILE must be an absolute path"
KEY_FILE="$PREFIX$KEY_FILE_NAME"
if [[ -L "$KEY_FILE" || ( -e "$KEY_FILE" && ! -f "$KEY_FILE" ) ]]; then
  refuse "the routine key $KEY_FILE_NAME is not a regular file (a symlink is refused)"
elif [[ ! -e "$KEY_FILE" ]]; then
  refuse "the routine key $KEY_FILE_NAME is missing: write CLAUDE_ROUTINE_KEY there, owned by $RUN_USER, mode 0600"
else
  read -r key_mode key_uid _ < <(meta "$KEY_FILE")
  key_len="$("$PYTHON" -I -c 'import sys
with open(sys.argv[1], "rb") as h:
    print(len(h.read(4096).decode("utf-8", "replace").strip()))' "$KEY_FILE" 2>/dev/null || echo 0)"
  if [[ "$key_uid" != "${RUN_UID:-x}" ]]; then
    refuse "the routine key must be owned by $RUN_USER (it is uid $key_uid)"
  elif [[ "$key_mode" != 600 && "$key_mode" != 400 ]]; then
    refuse "the routine key must be mode 0600 or 0400 (it is $key_mode): the router answers 503 otherwise"
  elif (( key_len < 16 )); then
    refuse "the routine key is shorter than 16 characters: the router answers 503 to every wake"
  else
    say "routine key ok: $KEY_FILE_NAME (mode $key_mode, value not shown)"
  fi
fi

# --- 5. the delivery knobs:  both must be set, and read fresh --------------------------------

ARMED=""
read_knobs() {
  if [[ -n "$TEST_ROOT" ]]; then
    knob-read
    return
  fi
  local scratch
  scratch="$(mktemp -d)"
  # The token is read from the env file inside this process and set only in its own environment,
  # so it never reaches an argv or any output;  HOME is a scratch folder, so the last-known-good
  # copy fleet_knobs keeps is not written into root's home, and it is removed afterwards.
  HOME="$scratch" "$PYTHON" -I - "$SCRIPTS" "$ENV_FILE" "$TOKEN_NAME" <<'PY' || { rm -rf -- "$scratch"; return 1; }
import json
import os
import sys

scripts, env_file, token_name = sys.argv[1:4]
with open(env_file, "rb") as handle:
    for line in handle.read(64 * 1024).decode("utf-8", "replace").splitlines():
        line = line.strip().removeprefix("export ").strip()
        key, sep, value = line.partition("=")
        if sep and key.strip() == token_name:
            os.environ[token_name] = value.strip().strip("'\"")
sys.path.insert(0, scripts)
from fleet_knobs import knobs, values  # noqa: E402 - the checkout's own reviewed helper
from fleet_knobs.registry import BY_NAME  # noqa: E402

out = {}
for name in ("WAKE_ROUTER_DELIVERY_ENABLED", "WAKE_ROUTER_DELIVERY_SEATS", "WAKE_ROUTER_MAC_SSH"):
    knob = knobs.resolve(name)
    value = None if knob.value is None else values.render(BY_NAME[name], knob.value)
    out[name] = {"source": knob.source, "stale": bool(knob.stale), "value": value}
print(json.dumps(out, sort_keys=True))
PY
  rm -rf -- "$scratch"
}

if [[ "$env_ok" -ne 1 ]]; then
  if [[ "$DRY" -eq 1 ]]; then
    say "would read WAKE_ROUTER_DELIVERY_ENABLED and WAKE_ROUTER_DELIVERY_SEATS from Infisical with the env file's token"
  fi
else
  knob_json="$(read_knobs 2>/dev/null)" || knob_json=""
  verdict="$(printf '%s' "$knob_json" | "$PYTHON" -I -c 'import json, sys
try:
    k = json.load(sys.stdin)
    e, s = k["WAKE_ROUTER_DELIVERY_ENABLED"], k["WAKE_ROUTER_DELIVERY_SEATS"]
except (ValueError, KeyError, TypeError):
    print("unreadable"); raise SystemExit
for name, row in (("WAKE_ROUTER_DELIVERY_ENABLED", e), ("WAKE_ROUTER_DELIVERY_SEATS", s)):
    if row.get("stale"):
        print("stale " + name); raise SystemExit
    if row.get("source") != "infisical":
        print("unset " + name); raise SystemExit
seats = [p.strip().upper() for p in str(s.get("value") or "").replace(";", ",").split(",") if p.strip()]
on = str(e.get("value")).strip().lower() == "true"
mac = k.get("WAKE_ROUTER_MAC_SSH") or {}
print("%s enabled=%s seats=%s mac_ssh=%s" % (
    "armed" if on and "CLAUDE" in seats else "held",
    "true" if on else "false", ",".join(seats) or "-", mac.get("value") or "-"))')"
  case "$verdict" in
    armed*|held*)
      ARMED="${verdict%% *}"
      say "knobs ok: ${verdict#* }"
      if [[ "$ARMED" == armed ]]; then
        say "delivery for CLAUDE would be ARMED: a started router delivers wakes"
      else
        say "delivery for CLAUDE would be held: a started router picks and receipts, and delivers nothing"
      fi ;;
    unset*)
      refuse "${verdict#unset } is not set in Infisical: set both delivery knobs (false is fine) before installing" ;;
    stale*)
      refuse "Infisical could not be read for ${verdict#stale }: the knob values are not confirmed" ;;
    *)
      refuse "the delivery knobs could not be read (check $TOKEN_NAME and the infisical CLI)" ;;
  esac
fi

# --- 6. the unit -----------------------------------------------------------------------------

if [[ "$DRY" -eq 0 && -z "$TEST_ROOT" && "$(id -u)" -ne 0 ]]; then
  die "run as root (sudo), or pass --dry-run to see the plan"
fi
command -v systemctl >/dev/null || refuse "systemctl is missing"
[[ ! -L "$UNIT_DIR" ]] || refuse "$UNIT_DIR is a symlink"
[[ ! -L "$UNIT_DST" ]] || refuse "$UNIT_DST is a symlink; refusing to write through it"

changed=0
if [[ -f "$UNIT_DST" ]] && cmp -s -- "$SOURCE_UNIT" "$UNIT_DST" &&
    [[ "$(meta "$UNIT_DST")" == "644 $ROOT_UID $ROOT_GID" ]]; then
  say "unchanged $UNIT_DST"
elif [[ "$DRY" -eq 1 ]]; then
  changed=1
  say "would install $UNIT_DST (0644, root) from claude-wake-router.service"
  say "would run systemctl daemon-reload"
else
  changed=1
  [[ -d "$UNIT_DIR" ]] || install -d -m 0755 -- "$UNIT_DIR" || die "could not create $UNIT_DIR"
  install -m 0644 -o "$ROOT_UID" -g "$ROOT_GID" -- "$SOURCE_UNIT" "$UNIT_DST" ||
    die "could not install $UNIT_DST"
  say "installed $UNIT_DST"
  systemctl daemon-reload || die "systemctl daemon-reload failed"
  say "reloaded systemd"
fi

# --- 7. start, only when asked ---------------------------------------------------------------

active=0
systemctl is-active --quiet "$UNIT_NAME" 2>/dev/null && active=1
if [[ "$START" -eq 1 ]]; then
  if [[ "$DRY" -eq 1 ]]; then
    if [[ "$active" -eq 1 && "$changed" -eq 1 ]]; then
      say "would run systemctl restart $UNIT_NAME (it is running an older unit)"
    elif [[ "$active" -eq 1 ]]; then
      say "would leave $UNIT_NAME running (unchanged)"
    else
      say "would run systemctl enable --now $UNIT_NAME"
    fi
  elif [[ "$active" -eq 1 && "$changed" -eq 1 ]]; then
    systemctl restart "$UNIT_NAME" || die "systemctl restart $UNIT_NAME failed"
    say "restarted $UNIT_NAME on the new unit"
  elif [[ "$active" -eq 1 ]]; then
    say "$UNIT_NAME is already running the installed unit"
  else
    systemctl enable --now "$UNIT_NAME" || die "systemctl enable --now $UNIT_NAME failed"
    say "enabled and started $UNIT_NAME"
  fi
else
  state="$(systemctl is-enabled "$UNIT_NAME" 2>/dev/null || true)"
  say "the router was not started by this script (enabled: ${state:-not installed}, running: $([[ $active -eq 1 ]] && echo yes || echo no))"
  if [[ "$active" -eq 1 && "$changed" -eq 1 ]]; then
    say "it runs an older unit; to pick this one up: systemctl restart $UNIT_NAME"
  else
    say "to start it: run this script again with --start (or systemctl enable --now $UNIT_NAME)"
  fi
fi

if [[ "$DRY" -eq 1 ]]; then
  say "dry run done: $problems problem(s) a real run would refuse"
else
  say "done"
fi
