#!/usr/bin/env bash
# Install the Operator registry -> Google Sheets export drain on the Coolify host (OPS-1037).
#
#   sudo OPERATOR_SHEETS_WORKBOOK_ID=<id> OPERATOR_SHEETS_TOKEN_FILE=<path> \
#     INFISICAL_FLEET_OPS_PROJECT_ID=<uuid> \
#     bash scripts/agent_sync/server/install-operator-sheets-drain.sh --dry-run   # show the plan
#   (the same without --dry-run)                                                 # install or update
#
# Run it as root from a clean AI-Fleet-Coordinator checkout at origin/main; it refuses any other
# checkout.  Safe to run again: an unchanged file is left alone.  In order, it:
#   1. stops the timer while it works, if the timer is running, and installs
#      operator_sheets_export.mjs, operator_sheets_google.mjs and operator_sheets_drain.mjs into
#      /usr/local/lib/operator-sheets-drain (0644, root);
#   2. reads OPERATOR_GOOGLE_SHEETS_SA from Infisical Fleet-OPS prod /operator-thread-registry
#      with the caller's scoped READ-only service token and writes /etc/operator-sheets/sa.json
#      (0600, root);
#   3. writes /etc/operator-sheets/drain.env (0600, root): the workbook id, sheet ids and the
#      optional knobs, and prints the names of the settings that changed;
#   4. preflight: the registry's pending-outbox and export-attempts reads, inside the container,
#      return JSON (the second fails on an image without the fence or a store before schema v2);
#   5. installs operator-sheets-drain.service and .timer into /etc/systemd/system and reloads
#      systemd.  It never enables the timer; it starts it again only if step 1 stopped it.
#
# Inputs (environment, set by the caller):
#   OPERATOR_SHEETS_WORKBOOK_ID      the SWU Operator workbook id (required; it is not in this repo)
#   OPERATOR_SHEETS_TOKEN_FILE       a root-owned file, mode 0600 or 0400, holding the scoped
#                                    read-only Infisical service token for Fleet-OPS prod
#                                    /operator-thread-registry (required).  The script never mints,
#                                    prints or echoes the token and never puts it on a command line.
#   INFISICAL_FLEET_OPS_PROJECT_ID   the Fleet-OPS project id (or root's ~/.agent-sync/fleet-ops-project-id)
#   OPERATOR_SHEETS_SESSIONS_SHEET_ID  default 20964676
#   OPERATOR_SHEETS_EVENTS_SHEET_ID    default 1449318075
#   OPERATOR_REGISTRY_CONTAINER      default eavesdropper
#   OPERATOR_REGISTRY_DB             default /data/operator/registry.sqlite3
#   OPERATOR_SHEETS_PAGE_LIMIT, OPERATOR_SHEETS_LOCK  optional drain knobs; when unset, a value
#                                    already in drain.env is kept
#   OPERATOR_SHEETS_INSTALL_TEST_ROOT  offline tests only: every destination goes under this
#                                    directory, docker and systemctl must be stubs in its bin/,
#                                    and steps 2 and 3 (Infisical, the config files) are skipped
set -euo pipefail

HERE="$(cd -P -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
AGENT_SYNC="$(cd -P -- "$HERE/.." && pwd)"
SCRIPTS="$(cd -P -- "$AGENT_SYNC/.." && pwd)"
REPO="$(cd -P -- "$SCRIPTS/.." && pwd)"
SERVICE=operator-sheets-drain.service
TIMER=operator-sheets-drain.timer
MODULES=(operator_sheets_export.mjs operator_sheets_google.mjs operator_sheets_drain.mjs)
PREFLIGHT_TIMEOUT=120

DRY=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY=1 ;;
    -h|--help) sed -n '2,39p' "$0"; exit 0 ;;
    *) echo "install-operator-sheets-drain: unknown argument: $arg" >&2; exit 2 ;;
  esac
done

say() { printf 'install-operator-sheets-drain: %s\n' "$*"; }
die() { say "$1" >&2; exit "${2:-1}"; }

WORKBOOK_ID="${OPERATOR_SHEETS_WORKBOOK_ID:-}"
TOKEN_FILE="${OPERATOR_SHEETS_TOKEN_FILE:-}"
SESSIONS_SHEET_ID="${OPERATOR_SHEETS_SESSIONS_SHEET_ID:-20964676}"
EVENTS_SHEET_ID="${OPERATOR_SHEETS_EVENTS_SHEET_ID:-1449318075}"
CONTAINER="${OPERATOR_REGISTRY_CONTAINER:-eavesdropper}"
REGISTRY_DB="${OPERATOR_REGISTRY_DB:-/data/operator/registry.sqlite3}"
PAGE_LIMIT="${OPERATOR_SHEETS_PAGE_LIMIT:-}"
LOCK_PATH="${OPERATOR_SHEETS_LOCK:-}"
TEST_ROOT="${OPERATOR_SHEETS_INSTALL_TEST_ROOT:-}"

# --- where things go: the real host, or a stubbed test root ---------------------------------

if [[ -n "$TEST_ROOT" ]]; then
  [[ "$TEST_ROOT" == /* && -d "$TEST_ROOT" && ! -L "$TEST_ROOT" ]] ||
    die "OPERATOR_SHEETS_INSTALL_TEST_ROOT must be an existing absolute directory" 2
  # Test mode never reaches the real docker or systemd:  both must be stubs in the test root.
  for tool in docker systemctl; do
    [[ -x "$TEST_ROOT/bin/$tool" && ! -L "$TEST_ROOT/bin/$tool" ]] ||
      die "test mode needs a stub $TEST_ROOT/bin/$tool" 2
  done
  PATH="$TEST_ROOT/bin:$PATH"
  PREFIX="$TEST_ROOT"
  OWNER_UID="$(id -u)"
  OWNER_GID="$(id -g)"
  NODE=""
  PYTHON="$(command -v python3 || true)"
else
  PREFIX=""
  OWNER_UID=0
  OWNER_GID=0
  NODE=/usr/bin/node
  PYTHON=/usr/bin/python3
fi
LIB_DIR="$PREFIX/usr/local/lib/operator-sheets-drain"
ETC_DIR="$PREFIX/etc/operator-sheets"
UNIT_DIR="$PREFIX/etc/systemd/system"

# --- inputs: checked before anything is read or changed ------------------------------------

[[ "$WORKBOOK_ID" =~ ^[A-Za-z0-9_-]+$ ]] ||
  die "set OPERATOR_SHEETS_WORKBOOK_ID to the SWU Operator workbook id (letters, digits, _ and -)" 2
[[ "$SESSIONS_SHEET_ID" =~ ^[0-9]{1,10}$ && "$EVENTS_SHEET_ID" =~ ^[0-9]{1,10}$ ]] ||
  die "OPERATOR_SHEETS_SESSIONS_SHEET_ID and OPERATOR_SHEETS_EVENTS_SHEET_ID must be whole numbers" 2
[[ "$SESSIONS_SHEET_ID" != "$EVENTS_SHEET_ID" ]] ||
  die "OPERATOR_SHEETS_SESSIONS_SHEET_ID and OPERATOR_SHEETS_EVENTS_SHEET_ID must differ" 2
[[ "$CONTAINER" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]] ||
  die "OPERATOR_REGISTRY_CONTAINER is not a container name" 2
[[ "$REGISTRY_DB" =~ ^/[A-Za-z0-9._/-]+$ ]] ||
  die "OPERATOR_REGISTRY_DB must be an absolute path" 2
[[ -z "$PAGE_LIMIT" || ( "$PAGE_LIMIT" =~ ^[1-9][0-9]{0,3}$ && "$PAGE_LIMIT" -le 5000 ) ]] ||
  die "OPERATOR_SHEETS_PAGE_LIMIT must be a whole number from 1 to 5000" 2
[[ -z "$LOCK_PATH" || "$LOCK_PATH" =~ ^/[A-Za-z0-9._/-]+$ ]] ||
  die "OPERATOR_SHEETS_LOCK must be an absolute path" 2
[[ -n "$TOKEN_FILE" ]] ||
  die "set OPERATOR_SHEETS_TOKEN_FILE to the file holding the scoped read-only Infisical token" 2
[[ -f "$TOKEN_FILE" && ! -L "$TOKEN_FILE" ]] ||
  die "OPERATOR_SHEETS_TOKEN_FILE is not a regular file (a symlink is refused)" 2
token_mode="$(stat -c '%a' -- "$TOKEN_FILE")"
[[ "$token_mode" == 600 || "$token_mode" == 400 ]] ||
  die "OPERATOR_SHEETS_TOKEN_FILE must be mode 0600 or 0400 (it is $token_mode)" 2

for name in "${MODULES[@]}"; do
  [[ -f "$AGENT_SYNC/$name" ]] || die "missing source $AGENT_SYNC/$name"
done
for name in "$SERVICE" "$TIMER"; do
  [[ -f "$HERE/$name" ]] || die "missing source $HERE/$name"
done
[[ -f "$SCRIPTS/fleet_secrets/core.py" ]] || die "missing source $SCRIPTS/fleet_secrets/core.py"

[[ "$DRY" -eq 1 ]] && say "dry run: nothing will be changed"

# A problem with the host or the checkout:  a real run stops; a dry run reports it and goes on,
# so the whole plan is still shown.
refuse() {
  if [[ "$DRY" -eq 1 ]]; then
    say "a real run would refuse: $1"
  else
    die "$1"
  fi
}

# --- the checkout: clean, at origin/main, and carrying the reviewed drain ------------------

# Run as root on a tree another user owns, git refuses "dubious ownership" unless told this
# one tree is safe; the setting applies to this command only.
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

# The modules must be the reviewed drain (PR 1456):  without it this would install the old
# 10-column drain with no pacing, no run deadline and an OnUnitActiveSec timer.
code_problem() {
  local drain="$AGENT_SYNC/operator_sheets_drain.mjs"
  grep -q '^export function pacedAdapter' "$drain" ||
    { echo "operator_sheets_drain.mjs has no pacedAdapter: this checkout predates the reviewed drain"; return; }
  grep -q '^export const RUN_DEADLINE_MS' "$drain" ||
    { echo "operator_sheets_drain.mjs has no run deadline: this checkout predates the reviewed drain"; return; }
  grep -q '^OnUnitInactiveSec=' "$HERE/$TIMER" ||
    echo "$TIMER still counts from the start of a run (no OnUnitInactiveSec): this checkout predates the reviewed drain"
}

problem="$(checkout_problem)"
[[ -z "$problem" ]] || refuse "$problem"
problem="$(code_problem)"
[[ -z "$problem" ]] || refuse "$problem"

if [[ "$DRY" -eq 0 && -z "$TEST_ROOT" ]]; then
  [[ "$(id -u)" -eq 0 ]] || die "run as root (sudo), or pass --dry-run to see the plan"
fi
if [[ "$DRY" -eq 0 && -z "$TEST_ROOT" ]]; then
  # The service runs /usr/bin/node on this host; a test root never runs the drain.
  [[ -x "$NODE" ]] || die "$NODE is missing (the service runs it)"
  "$NODE" -e 'process.exit(Number(process.versions.node.split(".")[0]) >= 18 ? 0 : 1)' ||
    die "$NODE is older than 18 (the drain needs fetch)"
fi
if [[ "$DRY" -eq 0 ]]; then
  [[ -n "$PYTHON" && -x "$PYTHON" ]] || die "python3 is missing"
  command -v docker >/dev/null || die "docker is missing (the drain reads the registry through it)"
  command -v systemctl >/dev/null || die "systemctl is missing"
  command -v timeout >/dev/null || die "timeout (coreutils) is missing"
fi

# The config directory is checked before anything is installed, so a loose one never leaves a
# half-done install behind.
if [[ -e "$ETC_DIR" || -L "$ETC_DIR" ]]; then
  if [[ -L "$ETC_DIR" || ! -d "$ETC_DIR" ]]; then
    refuse "$ETC_DIR exists and is not a plain directory"
  else
    read -r etc_mode etc_uid < <(stat -c '%a %u' -- "$ETC_DIR")
    if [[ "$etc_uid" != "$OWNER_UID" ]] || (( 8#$etc_mode & 8#077 )); then
      refuse "$ETC_DIR must be root-owned with no group or other access (it is mode $etc_mode, uid $etc_uid)"
    fi
  fi
fi
for dir in "$LIB_DIR" "$UNIT_DIR"; do
  [[ ! -L "$dir" ]] || refuse "$dir is a symlink"
done

# Install one tracked file; returns 0 when it changed (or would change), 1 when unchanged.
# Content, mode and owner all count, so a drifted mode or owner is repaired.  Callers test its
# status, which turns off `set -e` inside it, so every step checks itself.
install_one() {
  local src="$1" dst="$2"
  [[ ! -L "$dst" ]] || die "$dst is a symlink; refusing to write through it"
  if [[ -f "$dst" ]] && cmp -s -- "$src" "$dst" &&
      [[ "$(stat -c '%a %u %g' -- "$dst")" == "644 $OWNER_UID $OWNER_GID" ]]; then
    say "unchanged $dst"
    return 1
  fi
  if [[ "$DRY" -eq 1 ]]; then
    say "would install $dst"
    return 0
  fi
  install -m 0644 -o "$OWNER_UID" -g "$OWNER_GID" -- "$src" "$dst" || die "could not install $dst"
  say "installed $dst"
  return 0
}

# --- 1. stop the timer while files change, then the drain code -----------------------------

# A run the timer started halfway through step 1 would load a mix of old and new modules.  The
# timer is stopped for the install and started again at the end, only if it was running; if
# the install stops early it stays stopped, and the exit message says so.
timer_stopped=0
timer_restarted=0
on_exit() {
  if [[ "$timer_stopped" -eq 1 && "$timer_restarted" -eq 0 ]]; then
    say "$TIMER was stopped for the install and is still stopped; start it with: systemctl start $TIMER" >&2
  fi
}
trap on_exit EXIT
if systemctl is-active --quiet "$TIMER" 2>/dev/null; then
  if [[ "$DRY" -eq 1 ]]; then
    say "would stop $TIMER during the install and start it again at the end"
  else
    systemctl stop "$TIMER" || die "could not stop $TIMER for the install"
    timer_stopped=1
    say "stopped $TIMER for the install, so no run starts on a half-copied set of modules"
  fi
fi

if [[ "$DRY" -eq 1 ]]; then
  [[ -d "$LIB_DIR" ]] || say "would create $LIB_DIR (0755, root)"
else
  install -d -m 0755 -o "$OWNER_UID" -g "$OWNER_GID" -- "$LIB_DIR" || die "could not create $LIB_DIR"
fi
for name in "${MODULES[@]}"; do
  install_one "$AGENT_SYNC/$name" "$LIB_DIR/$name" || true
done

# --- 2 and 3. sa.json from Infisical, and drain.env ----------------------------------------

if [[ "$DRY" -eq 1 ]]; then
  say "would read OPERATOR_GOOGLE_SHEETS_SA from Infisical Fleet-OPS prod /operator-thread-registry"
  say "  with the read-only service token in $TOKEN_FILE (the token is not read in a dry run)"
  say "would write $ETC_DIR/sa.json (0600, root) if its content changed"
  say "would write $ETC_DIR/drain.env (0600, root) if its content changed, with:"
  say "  OPERATOR_SHEETS_SA_FILE=$ETC_DIR/sa.json"
  say "  OPERATOR_SHEETS_WORKBOOK_ID=$WORKBOOK_ID"
  say "  OPERATOR_SHEETS_SESSIONS_SHEET_ID=$SESSIONS_SHEET_ID"
  say "  OPERATOR_SHEETS_EVENTS_SHEET_ID=$EVENTS_SHEET_ID"
  say "  OPERATOR_REGISTRY_CONTAINER=$CONTAINER"
  say "  OPERATOR_REGISTRY_DB=$REGISTRY_DB"
  say "  OPERATOR_SHEETS_PAGE_LIMIT=${PAGE_LIMIT:-<kept from the existing drain.env, if set there>}"
  say "  OPERATOR_SHEETS_LOCK=${LOCK_PATH:-<kept from the existing drain.env, if set there>}"
elif [[ -n "$TEST_ROOT" ]]; then
  say "test mode: skipped the Infisical read, sa.json and drain.env"
else
  # The token is read inside this Python process from the file and travels only in the
  # request header:  never in argv, the environment of a child, or any output.
  "$PYTHON" -I - "$SCRIPTS" "$ETC_DIR" "$TOKEN_FILE" "$WORKBOOK_ID" "$SESSIONS_SHEET_ID" \
    "$EVENTS_SHEET_ID" "$CONTAINER" "$REGISTRY_DB" "$PAGE_LIMIT" "$LOCK_PATH" <<'PY'
import contextlib
import json
import os
import re
import secrets
import stat
import sys

ROOT_UID = 0
SECRET_PATH = "/operator-thread-registry"
SECRET_NAME = "OPERATOR_GOOGLE_SHEETS_SA"
MAX_TOKEN_BYTES = 8192
MAX_ENV_BYTES = 64 * 1024
DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
# The optional drain knobs drain.env may carry, with the values each accepts.
KNOBS = {
    "OPERATOR_SHEETS_PAGE_LIMIT": re.compile(r"[1-9][0-9]{0,3}"),
    "OPERATOR_SHEETS_LOCK": re.compile(r"/[A-Za-z0-9._/-]+"),
}


class InstallError(Exception):
    """A refusal whose message names files and keys, never a value."""


def read_token(path):
    """The scoped service token from a root-owned 0600/0400 regular file."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        raise InstallError(
            "cannot open OPERATOR_SHEETS_TOKEN_FILE (a symlink is refused)"
        ) from None
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise InstallError("OPERATOR_SHEETS_TOKEN_FILE is not a regular file")
        if info.st_uid != ROOT_UID or info.st_mode & 0o077:
            raise InstallError(
                "OPERATOR_SHEETS_TOKEN_FILE must be root-owned with no group or other access"
            )
        data = handle.read(MAX_TOKEN_BYTES + 1)
    if len(data) > MAX_TOKEN_BYTES:
        raise InstallError("OPERATOR_SHEETS_TOKEN_FILE is too large to be a service token")
    try:
        token = data.decode("ascii").strip()
    except UnicodeDecodeError:
        raise InstallError("OPERATOR_SHEETS_TOKEN_FILE does not hold a service token") from None
    if not token.startswith("st.") or any(c.isspace() for c in token):
        raise InstallError("OPERATOR_SHEETS_TOKEN_FILE does not hold an Infisical service token")
    return token


def fetch_service_account(core, token, env):
    """Read the one named secret.  fetch_project first verifies the token's own grant:  Fleet-OPS,
    prod, read only, exactly SECRET_PATH.  A wider or write-capable token is refused."""
    try:
        project = core.infisical_project_id(env, core.home_dir(env))
        # An empty environment pins the default Infisical API and the default read folders,
        # so nothing in the caller's environment can redirect or widen the read.
        found = core.fetch_project(project, SECRET_PATH, {}, token=token, names=[SECRET_NAME])
    except core.SecretsError as exc:
        message = str(getattr(exc, "message", "") or "refused")
        raise InstallError("Infisical read failed: " + message) from None
    if SECRET_NAME not in found:
        raise InstallError(f"{SECRET_NAME} is not in Fleet-OPS prod {SECRET_PATH}")
    return found[SECRET_NAME]


def check_service_account(text):
    """The secret must be a Google service-account key.  Errors name fields only."""
    try:
        account = json.loads(text)
    except ValueError:
        raise InstallError(f"{SECRET_NAME} is not JSON") from None
    if not isinstance(account, dict) or account.get("type") != "service_account":
        raise InstallError(f"{SECRET_NAME} is not a service-account key (type)")
    for field in ("client_email", "private_key"):
        if not isinstance(account.get(field), str) or not account[field]:
            raise InstallError(f"{SECRET_NAME} is missing {field}")
    if "PRIVATE KEY-----" not in account["private_key"]:
        raise InstallError(f"{SECRET_NAME} private_key is not a PEM key")
    return text.encode("utf-8")


def parse_env(data):
    """KEY=VALUE lines of an env file, in order; comments and blank lines are skipped."""
    found = {}
    for line in (data or b"").decode("utf-8", "replace").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            found[key.strip()] = value.strip()
    return found


def resolve_knobs(requested, existing):
    """The knobs to write:  the caller's value when set, else the one already in drain.env.
    Returns (knobs, notes); a note names a key, never its value."""
    knobs, notes = {}, []
    for name, pattern in KNOBS.items():
        value = requested.get(name) or ""
        source = "set by the caller"
        if not value and name in existing:
            value, source = existing[name], "kept from the existing drain.env"
        if not value:
            continue
        if not pattern.fullmatch(value) or (
            name == "OPERATOR_SHEETS_PAGE_LIMIT" and int(value) > 5000
        ):
            raise InstallError(f"{name} ({source}) is not a valid value")
        knobs[name] = value
        if source != "set by the caller":
            notes.append(f"{name} {source}")
    return knobs, notes


def render_env(etc_dir, workbook_id, sessions_id, events_id, container, registry_db, knobs=None):
    lines = [
        "# Written by scripts/agent_sync/server/install-operator-sheets-drain.sh (OPS-1037).",
        "# No secret values here: the service-account key is in sa.json beside this file.",
        f"OPERATOR_SHEETS_SA_FILE={etc_dir}/sa.json",
        f"OPERATOR_SHEETS_WORKBOOK_ID={workbook_id}",
        f"OPERATOR_SHEETS_SESSIONS_SHEET_ID={sessions_id}",
        f"OPERATOR_SHEETS_EVENTS_SHEET_ID={events_id}",
        f"OPERATOR_REGISTRY_CONTAINER={container}",
        f"OPERATOR_REGISTRY_DB={registry_db}",
    ]
    lines += [f"{name}={value}" for name, value in (knobs or {}).items()]
    return ("\n".join(lines) + "\n").encode("utf-8")


def env_changes(old, new):
    """The names of settings added, changed or dropped between two env files."""
    old_map, new_map = parse_env(old), parse_env(new)
    changed = [k for k in new_map if old_map.get(k) != new_map[k]]
    dropped = [k for k in old_map if k not in new_map]
    return changed, dropped


def open_private_directory(path):
    """Create or open the config directory:  root-owned 0700, never through a symlink."""
    parent, name = os.path.split(path.rstrip("/"))
    parent_fd = os.open(parent or "/", DIRECTORY_FLAGS)
    try:
        with contextlib.suppress(FileExistsError):
            os.mkdir(name, 0o700, dir_fd=parent_fd)
        fd = os.open(name, DIRECTORY_FLAGS, dir_fd=parent_fd)
    finally:
        os.close(parent_fd)
    info = os.fstat(fd)
    if info.st_uid != ROOT_UID or info.st_mode & 0o077:
        os.close(fd)
        raise InstallError(f"{path} must be root-owned with no group or other access")
    return fd


def read_private(directory_fd, name):
    """An existing regular file's bytes, or None when it is absent.  Never follows a symlink."""
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
    except FileNotFoundError:
        return None
    except OSError:
        raise InstallError(f"{name} exists and is not a regular file") from None
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise InstallError(f"{name} exists and is not a regular file")
        data = handle.read(MAX_ENV_BYTES + 1)
    if len(data) > MAX_ENV_BYTES:
        raise InstallError(f"{name} is too large")
    return data


def write_private(directory_fd, name, data):
    """Atomically write a 0600 root-owned file.  Returns False, touching nothing, when unchanged."""
    try:
        info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        info = None
    if info is not None:
        if not stat.S_ISREG(info.st_mode):
            raise InstallError(f"{name} exists and is not a regular file")
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
        with os.fdopen(fd, "rb") as handle:
            same = handle.read(len(data) + 1) == data
        if same and info.st_uid == ROOT_UID and stat.S_IMODE(info.st_mode) == 0o600:
            return False
    temporary = ".install-" + secrets.token_hex(16)
    fd = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
        dir_fd=directory_fd,
    )
    try:
        os.fchmod(fd, 0o600)
        os.fchown(fd, ROOT_UID, 0)
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise InstallError(f"{name} could not be fully written")
            view = view[written:]
        os.fsync(fd)
        os.replace(temporary, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        os.fsync(directory_fd)
    finally:
        os.close(fd)
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary, dir_fd=directory_fd)
    return True


def report(path, changed):
    word = "installed" if changed else "unchanged"
    print(f"install-operator-sheets-drain: {word} {path}")


def main(argv):
    (scripts, etc_dir, token_file, workbook_id, sessions_id, events_id, container, registry_db,
     page_limit, lock_path) = argv
    sys.path.insert(0, scripts)
    from fleet_secrets import core  # noqa: E402 - the checkout's own reviewed helper

    env = {key: os.environ[key] for key in ("HOME", core.ENV_PROJECT) if os.environ.get(key)}
    token = read_token(token_file)
    account = check_service_account(fetch_service_account(core, token, env))
    del token
    directory = open_private_directory(etc_dir)
    try:
        report(f"{etc_dir}/sa.json", write_private(directory, "sa.json", account))
        old_env = read_private(directory, "drain.env")
        requested = {"OPERATOR_SHEETS_PAGE_LIMIT": page_limit, "OPERATOR_SHEETS_LOCK": lock_path}
        knobs, notes = resolve_knobs(requested, parse_env(old_env))
        new_env = render_env(
            etc_dir, workbook_id, sessions_id, events_id, container, registry_db, knobs
        )
        for note in notes:
            print(f"install-operator-sheets-drain: drain.env: {note}")
        if old_env is not None:
            changed, dropped = env_changes(old_env, new_env)
            if changed:
                print("install-operator-sheets-drain: drain.env settings changed: " + ", ".join(changed))
            if dropped:
                print(
                    "install-operator-sheets-drain: drain.env settings dropped (not managed here): "
                    + ", ".join(dropped)
                )
        report(f"{etc_dir}/drain.env", write_private(directory, "drain.env", new_env))
    finally:
        os.close(directory)


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except InstallError as exc:
        print(f"install-operator-sheets-drain: {exc}", file=sys.stderr)
        raise SystemExit(1)
    except (OSError, ValueError, ImportError):
        print(
            "install-operator-sheets-drain: writing the drain configuration failed; check "
            "/etc/operator-sheets ownership and the checkout",
            file=sys.stderr,
        )
        raise SystemExit(1)
PY
fi

# --- 4. preflight: the registry reads the drain depends on ---------------------------------

# Prints the number of rows when the read returns a JSON list in time; prints nothing and fails
# otherwise.  The rows themselves carry registry data and are never printed.
registry_list() {
  local out
  out="$(timeout "$PREFLIGHT_TIMEOUT" docker exec -w /app/scripts "$CONTAINER" \
    python3 -m agent_sync.operator_registry "$@" 2>/dev/null)" || return 1
  printf '%s' "$out" | "$PYTHON" -I -c 'import json, sys
try:
    rows = json.load(sys.stdin)
except ValueError:
    raise SystemExit(1)
if not isinstance(rows, list):
    raise SystemExit(1)
print(len(rows))'
}

if [[ "$DRY" -eq 1 ]]; then
  say "would check that 'docker exec $CONTAINER ... operator_registry pending-outbox --db $REGISTRY_DB --limit 1' returns JSON within ${PREFLIGHT_TIMEOUT} s"
  say "would check that '... operator_registry export-attempts --db $REGISTRY_DB --limit 1' returns JSON (the export fence, schema v2)"
else
  rows="$(registry_list pending-outbox --db "$REGISTRY_DB" --limit 1)" ||
    die "preflight failed: pending-outbox in $CONTAINER did not return a JSON list within ${PREFLIGHT_TIMEOUT} s; the units were not installed"
  say "preflight ok: pending-outbox returned JSON ($rows row(s) at --limit 1)"
  attempts="$(registry_list export-attempts --db "$REGISTRY_DB" --limit 1)" ||
    die "preflight failed: export-attempts in $CONTAINER did not return a JSON list: the image lacks the export fence CLI or the store is not yet at schema v2 (redeploy the Eavesdropper app and let it write once); the units were not installed"
  say "preflight ok: export-attempts returned JSON ($attempts row(s) at --limit 1)"
fi

# --- 5. systemd units; the timer is never enabled here ------------------------------------

if [[ "$DRY" -eq 0 ]]; then
  [[ -d "$UNIT_DIR" ]] || install -d -m 0755 -- "$UNIT_DIR" || die "could not create $UNIT_DIR"
fi
units_changed=0
install_one "$HERE/$SERVICE" "$UNIT_DIR/$SERVICE" && units_changed=1
install_one "$HERE/$TIMER" "$UNIT_DIR/$TIMER" && units_changed=1
if [[ "$units_changed" -eq 1 ]]; then
  if [[ "$DRY" -eq 1 ]]; then
    say "would run systemctl daemon-reload"
  else
    systemctl daemon-reload || die "systemctl daemon-reload failed"
    say "reloaded systemd"
  fi
fi
if [[ "$timer_stopped" -eq 1 ]]; then
  systemctl start "$TIMER" || die "could not start $TIMER again"
  timer_restarted=1
  say "started $TIMER again (it was running before the install)"
fi
timer_state="$(systemctl is-enabled "$TIMER" 2>/dev/null || true)"
say "the timer was not enabled by this script (state: ${timer_state:-not installed})"
say "enable it by hand once the first live drain is cleared: systemctl enable --now $TIMER"
say "done"
