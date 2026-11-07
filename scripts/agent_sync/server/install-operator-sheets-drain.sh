#!/usr/bin/env bash
# Install the Operator registry -> Google Sheets export drain on the Coolify host (OPS-1037).
#
#   sudo OPERATOR_SHEETS_WORKBOOK_ID=<id> OPERATOR_SHEETS_TOKEN_FILE=<path> \
#     INFISICAL_FLEET_OPS_PROJECT_ID=<uuid> \
#     bash scripts/agent_sync/server/install-operator-sheets-drain.sh --dry-run   # show the plan
#   (the same without --dry-run)                                                 # install or update
#
# Run it as root from an AI-Fleet-Coordinator checkout at origin/main.  Safe to run again:
# an unchanged file is left alone.  In order, it:
#   1. installs operator_sheets_export.mjs, operator_sheets_google.mjs and
#      operator_sheets_drain.mjs into /usr/local/lib/operator-sheets-drain (0644, root);
#   2. reads OPERATOR_GOOGLE_SHEETS_SA from Infisical Fleet-OPS prod /operator-thread-registry
#      with the caller's scoped READ-only service token and writes /etc/operator-sheets/sa.json
#      (0600, root);
#   3. writes /etc/operator-sheets/drain.env (0600, root): the workbook id and sheet ids;
#   4. preflight: the registry's pending-outbox read, inside the container, returns JSON;
#   5. installs operator-sheets-drain.service and .timer into /etc/systemd/system and reloads
#      systemd.  It never enables or starts the timer:  enable it by hand when the first live
#      drain is cleared.
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
set -euo pipefail

HERE="$(cd -P -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
AGENT_SYNC="$(cd -P -- "$HERE/.." && pwd)"
SCRIPTS="$(cd -P -- "$AGENT_SYNC/.." && pwd)"
LIB_DIR=/usr/local/lib/operator-sheets-drain
ETC_DIR=/etc/operator-sheets
UNIT_DIR=/etc/systemd/system
SERVICE=operator-sheets-drain.service
TIMER=operator-sheets-drain.timer
MODULES=(operator_sheets_export.mjs operator_sheets_google.mjs operator_sheets_drain.mjs)

DRY=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY=1 ;;
    -h|--help) sed -n '2,32p' "$0"; exit 0 ;;
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

if [[ "$DRY" -eq 0 ]]; then
  [[ "$(id -u)" -eq 0 ]] || die "run as root (sudo), or pass --dry-run to see the plan"
  [[ -x /usr/bin/node ]] || die "/usr/bin/node is missing (the service runs it)"
  /usr/bin/node -e 'process.exit(Number(process.versions.node.split(".")[0]) >= 18 ? 0 : 1)' ||
    die "/usr/bin/node is older than 18 (the drain needs fetch)"
  command -v docker >/dev/null || die "docker is missing (the drain reads the registry through it)"
  command -v systemctl >/dev/null || die "systemctl is missing"
fi

[[ "$DRY" -eq 1 ]] && say "dry run: nothing will be changed"

# Install one tracked file; returns 0 when it changed (or would change), 1 when unchanged.
# Callers test its status, which turns off `set -e` inside it, so every step checks itself.
install_one() {
  local src="$1" dst="$2"
  [[ ! -L "$dst" ]] || die "$dst is a symlink; refusing to write through it"
  if [[ -f "$dst" ]] && cmp -s -- "$src" "$dst"; then
    say "unchanged $dst"
    return 1
  fi
  if [[ "$DRY" -eq 1 ]]; then
    say "would install $dst"
    return 0
  fi
  install -m 0644 -o root -g root -- "$src" "$dst" || die "could not install $dst"
  say "installed $dst"
  return 0
}

# --- 1. the drain code ---------------------------------------------------------------------

if [[ "$DRY" -eq 1 ]]; then
  [[ -d "$LIB_DIR" ]] || say "would create $LIB_DIR (0755, root)"
else
  install -d -m 0755 -o root -g root -- "$LIB_DIR"
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
else
  # The token is read inside this Python process from the file and travels only in the
  # request header:  never in argv, the environment of a child, or any output.
  /usr/bin/python3 -I - "$SCRIPTS" "$ETC_DIR" "$TOKEN_FILE" "$WORKBOOK_ID" \
    "$SESSIONS_SHEET_ID" "$EVENTS_SHEET_ID" "$CONTAINER" "$REGISTRY_DB" <<'PY'
import contextlib
import json
import os
import secrets
import stat
import sys

ROOT_UID = 0
SECRET_PATH = "/operator-thread-registry"
SECRET_NAME = "OPERATOR_GOOGLE_SHEETS_SA"
MAX_TOKEN_BYTES = 8192
DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


class InstallError(Exception):
    """A refusal whose message names files and keys, never a value."""


def read_token(path):
    """The scoped service token from a root-owned 0600/0400 regular file."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        raise InstallError("cannot open OPERATOR_SHEETS_TOKEN_FILE (a symlink is refused)") from None
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise InstallError("OPERATOR_SHEETS_TOKEN_FILE is not a regular file")
        if info.st_uid != ROOT_UID or info.st_mode & 0o077:
            raise InstallError("OPERATOR_SHEETS_TOKEN_FILE must be root-owned with no group or other access")
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
        raise InstallError("Infisical read failed: " + str(getattr(exc, "message", "") or "refused")) from None
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


def render_env(etc_dir, workbook_id, sessions_id, events_id, container, registry_db):
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
    return ("\n".join(lines) + "\n").encode("utf-8")


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
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600,
                 dir_fd=directory_fd)
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


def main(argv):
    scripts, etc_dir, token_file, workbook_id, sessions_id, events_id, container, registry_db = argv
    sys.path.insert(0, scripts)
    from fleet_secrets import core  # noqa: E402 - the checkout's own reviewed helper

    env = {key: os.environ[key] for key in ("HOME", core.ENV_PROJECT) if os.environ.get(key)}
    token = read_token(token_file)
    account = check_service_account(fetch_service_account(core, token, env))
    del token
    directory = open_private_directory(etc_dir)
    try:
        for name, data in (
            ("sa.json", account),
            ("drain.env", render_env(etc_dir, workbook_id, sessions_id, events_id, container, registry_db)),
        ):
            changed = write_private(directory, name, data)
            print(f"install-operator-sheets-drain: {'installed' if changed else 'unchanged'} {etc_dir}/{name}")
    finally:
        os.close(directory)


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except InstallError as exc:
        print(f"install-operator-sheets-drain: {exc}", file=sys.stderr)
        raise SystemExit(1)
    except (OSError, ValueError, ImportError):
        print("install-operator-sheets-drain: writing the drain configuration failed; check "
              "/etc/operator-sheets ownership and the checkout", file=sys.stderr)
        raise SystemExit(1)
PY
fi

# --- 4. preflight: the registry read the drain depends on ----------------------------------

if [[ "$DRY" -eq 1 ]]; then
  say "would check that 'docker exec $CONTAINER ... operator_registry pending-outbox --db $REGISTRY_DB --limit 1' returns JSON"
else
  # Only the row count is printed:  pending rows carry registry data.
  rows="$(docker exec -w /app/scripts "$CONTAINER" python3 -m agent_sync.operator_registry \
      pending-outbox --db "$REGISTRY_DB" --limit 1 |
    /usr/bin/python3 -I -c 'import json, sys
rows = json.load(sys.stdin)
if not isinstance(rows, list):
    raise SystemExit(1)
print(len(rows))')" || die "preflight failed: the pending-outbox read did not return a JSON list; the units were not installed"
  say "preflight ok: pending-outbox returned JSON ($rows row(s) at --limit 1)"
fi

# --- 5. systemd units, timer left disabled -------------------------------------------------

units_changed=0
install_one "$HERE/$SERVICE" "$UNIT_DIR/$SERVICE" && units_changed=1
install_one "$HERE/$TIMER" "$UNIT_DIR/$TIMER" && units_changed=1
if [[ "$units_changed" -eq 1 ]]; then
  if [[ "$DRY" -eq 1 ]]; then
    say "would run systemctl daemon-reload"
  else
    systemctl daemon-reload
    say "reloaded systemd"
  fi
fi
timer_state="$(systemctl is-enabled "$TIMER" 2>/dev/null || true)"
say "the timer was not enabled or started by this script (state: ${timer_state:-not installed})"
say "enable it by hand once the first live drain is cleared: systemctl enable --now $TIMER"
say "done"
