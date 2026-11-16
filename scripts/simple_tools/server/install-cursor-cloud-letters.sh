#!/usr/bin/env bash
# Install the one Cursor Cloud letter sweep as a systemd --user unit and timer (owner ruling, Nov 12, 2026:  one
# Operator unit, no seat's own copy).
#
#   bash scripts/simple_tools/server/install-cursor-cloud-letters.sh --dry-run
#   bash scripts/simple_tools/server/install-cursor-cloud-letters.sh
#   bash scripts/simple_tools/server/install-cursor-cloud-letters.sh --start
#   bash scripts/simple_tools/server/install-cursor-cloud-letters.sh --uninstall [--dry-run]
#
# Run it on the server as the jay user.  It refuses to run as root and never uses sudo.  It checks everything first
# and changes nothing until every check passes;  a real run that finds a problem lists them all and stops.  Then it:
#   1. moves the two staged tokens into ~/.config/afc-cursor-cloud-letters/env (directory 0700, file 0600) and
#      removes the staged files.  An env file already installed is kept as it is;
#   2. writes ~/.config/systemd/user/afc-cursor-cloud-letters.service and .timer from the templates beside this
#      script, with its marker line on top.  A unit already there that this script did not write is never
#      overwritten;
#   3. runs `systemctl --user daemon-reload` and enables the timer.  With --start it also starts the timer.
#
# The two tokens are read-only Infisical service tokens, named for their use and for no seat.  A person mints them
# (the service-tokens skill), logs each on the service-tokens tab of the fleet references workbook and stages each
# in its own 0600 file, as NAME=value or the bare token on one line:
#   FLEET_KNOBS_INFISICAL_TOKEN     afc-cursor-cloud-letters-knobs-ro:  AI Fleet Coordinator, prod, /wake-router
#                                   only, read only.  registry_api reads the registry's URL and signing key there,
#                                   as the knobs SIMPLE_TOOLS_OPERATOR_REGISTRY_URL and _KEY, the way the wake
#                                   router does.  Staged in ~/.config/infisical-staging/afc-cursor-cloud-letters-knobs.env
#   CURSOR_LETTERS_INFISICAL_TOKEN  afc-cursor-cloud-letters-ro:  Fleet-OPS, prod, /cursor-cloud-letters only, read
#                                   only.  The sweep reads CURSOR_SYNC_API_KEY with it.  Staged in
#                                   ~/.config/infisical-staging/afc-cursor-cloud-letters-cursor.env
# No token is ever printed, logged or put on a command line:  the checks hand them to a child on standard input.
#
# Every run, the dry run included, checks with the installed runtime's own code (the fleet-cli runtime the unit
# runs, ~/.local/share/fleet-cli/current/scripts) that the registry URL, the registry key and the Cursor key can be
# read with those tokens.  It prints "present" or a reason token for each, never a value.  It also checks that
# lingering is on, so the timer keeps running after logout.
#
# The retired per-seat unit.  While claude-cursor-cloud-letters.timer is still installed, this script says so, and
# --start is refused while that timer is active, so two sweeps never run side by side.  Disable it first with
# `systemctl --user disable --now claude-cursor-cloud-letters.timer` (it fails every run anyway), then use
# --start.  After the shared unit has run clean, a person removes the old unit by hand (README, "Retiring the
# per-seat unit"):  this script never touches it.
#
# Options:
#   --dry-run          check and print the plan, change nothing (exit 3 when a real run would refuse)
#   --start            also start the timer
#   --uninstall        stop and disable the timer, remove exactly the units this script wrote, and move each token
#                      back to its staging file
#   --knobs-staged F   the staged FLEET_KNOBS_INFISICAL_TOKEN file (default above)
#   --cursor-staged F  the staged CURSOR_LETTERS_INFISICAL_TOKEN file (default above)
#
# Offline tests set CURSOR_LETTERS_INSTALL_TEST_ROOT:  home moves under it, its bin/ comes first on PATH, and the
# checks run the test interpreter.  It is ignored when the caller is root.
set -euo pipefail

for arg in "$@"; do
  if [[ "$arg" == "-h" || "$arg" == "--help" ]]; then
    awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "$0"
    exit 0
  fi
done

HERE="$(cd -P -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="$(command -v python3 || true)"
[[ -n "$PYTHON" ]] || { echo "install-cursor-cloud-letters: python3 is missing" >&2; exit 1; }
export CURSOR_LETTERS_INSTALLER_HERE="$HERE"
exec "$PYTHON" -I - "$@" <<'PY'
import argparse
import json
import os
import pwd
import re
import secrets
import shutil
import stat
import subprocess
import sys
from pathlib import Path

NAME = "install-cursor-cloud-letters"
SERVICE = "afc-cursor-cloud-letters.service"
TIMER = "afc-cursor-cloud-letters.timer"
UNITS = (SERVICE, TIMER)
MARKER = "# Installed by install-cursor-cloud-letters.sh"
KNOBS_TOKEN = "FLEET_KNOBS_INFISICAL_TOKEN"
CURSOR_TOKEN = "CURSOR_LETTERS_INFISICAL_TOKEN"
TOKENS = (KNOBS_TOKEN, CURSOR_TOKEN)
STAGED = {
    KNOBS_TOKEN: "afc-cursor-cloud-letters-knobs.env",
    CURSOR_TOKEN: "afc-cursor-cloud-letters-cursor.env",
}
TOKEN_LABEL = {
    KNOBS_TOKEN: "afc-cursor-cloud-letters-knobs-ro (AI Fleet Coordinator prod /wake-router)",
    CURSOR_TOKEN: "afc-cursor-cloud-letters-ro (Fleet-OPS prod /cursor-cloud-letters)",
}
CHECKED = ("registry_url", "registry_key", "cursor_key")
RUNTIME = Path(".local") / "share" / "fleet-cli" / "current" / "scripts"
MODULE = "simple_tools.cursor_cloud_letters"
# The retired per-seat registrar.  Named here only to keep two sweeps from running side by side;  never changed.
RETIRED_TIMER = "claude-cursor-cloud-letters.timer"
TEST_ROOT_ENV = "CURSOR_LETTERS_INSTALL_TEST_ROOT"
TOKEN_RE = re.compile(r"st\.[^\s\"'=]{17,8189}")
STOPPED_STATES = frozenset({"inactive", "failed"})
STOPPED_CODES = frozenset({3, 4})


def say(message):
    print("%s: %s" % (NAME, message))


class Plan:
    """Every refusal is collected first, so a dry run lists them all and a real run changes nothing unless there
    are none."""

    def __init__(self, dry):
        self.dry = dry
        self.refusals = []

    def refuse(self, message):
        self.refusals.append(message)
        say(("a real run would refuse: " if self.dry else "refused: ") + message)


def parse_args(argv):
    parser = argparse.ArgumentParser(prog="install-cursor-cloud-letters.sh", add_help=False)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--start", action="store_true")
    parser.add_argument("--uninstall", action="store_true")
    parser.add_argument("--knobs-staged", default="")
    parser.add_argument("--cursor-staged", default="")
    try:
        return parser.parse_args(argv)
    except SystemExit as exc:
        raise SystemExit(2 if exc.code else 0)


def paths_for(uid, environ):
    """(home, test_root, refusal).  The test root is honoured only for a caller that is not root."""
    test_root = "" if uid == 0 else environ.get(TEST_ROOT_ENV, "")
    if test_root:
        root = Path(test_root)
        if not root.is_absolute() or root.is_symlink() or not root.is_dir():
            return None, "", "%s must be an existing absolute directory" % TEST_ROOT_ENV
        return root / "home", test_root, ""
    try:
        return Path(pwd.getpwuid(uid).pw_dir), "", ""
    except KeyError:
        return None, "", "the caller has no passwd entry"


def private_file_problem(path, uid):
    """Why a token file may not be read, or "" (a regular file, not a link, owned by the caller, mode 0600 or
    stricter), or "missing"."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return "missing"
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        return "%s must be a regular file, not a link" % path
    if info.st_uid != uid:
        return "%s must be owned by the installing user" % path
    if info.st_mode & 0o077:
        return "%s must have no group or other access (mode 0600)" % path
    return ""


def body_lines(path):
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    return [line.strip() for line in text.splitlines() if line.strip() and not line.strip().startswith("#")]


def read_staged(path, name, uid):
    """(token, problem).  One line:  NAME=value, or the bare token.  The token is never printed."""
    problem = private_file_problem(path, uid)
    if problem:
        return None, problem
    lines = body_lines(path)
    if lines is None:
        return None, "%s cannot be read as text" % path
    if len(lines) != 1:
        return None, "%s must hold exactly one token line" % path
    line = lines[0]
    if line.startswith("export ") or line.startswith("export\t"):
        return None, "%s: an \"export \" prefix is not allowed" % path
    key, sep, value = line.partition("=")
    if sep and key != name:
        return None, "%s must hold %s, not %s" % (path, name, key or "(blank)")
    token = value if sep else line
    if not TOKEN_RE.fullmatch(token):
        return None, "%s does not hold an Infisical service token (st., no spaces or quotes)" % path
    return token, ""


def read_env(path, uid):
    """({name: token}, problem) for the installed env file:  exactly the two names, once each."""
    problem = private_file_problem(path, uid)
    if problem:
        return None, problem
    lines = body_lines(path)
    if lines is None:
        return None, "%s cannot be read as text" % path
    found = {}
    for number, line in enumerate(lines, 1):
        if line.startswith("export ") or line.startswith("export\t"):
            return None, "%s: an \"export \" prefix is not allowed (systemd ignores the line)" % path
        key, sep, value = line.partition("=")
        if not sep:
            return None, "%s: line %d is not NAME=value" % (path, number)
        if key not in TOKENS:
            return None, "%s may hold only %s;  it also names %s" % (path, " and ".join(TOKENS), key or "(blank)")
        if key in found:
            return None, "%s names %s twice" % (path, key)
        if not TOKEN_RE.fullmatch(value):
            return None, "%s: the %s value is not an Infisical service token" % (path, key)
        found[key] = value
    missing = [name for name in TOKENS if name not in found]
    if missing:
        return None, "%s holds no %s line" % (path, " or ".join(missing))
    return found, ""


def ours(path):
    """Whether a unit file is one this script wrote (its marker line is near the top)."""
    try:
        with open(path, encoding="utf-8") as handle:
            head = [handle.readline() for _ in range(3)]
    except (OSError, UnicodeDecodeError):
        return False
    return any(line.startswith(MARKER) for line in head)


class Host:
    """Runs tools with a small, explicit environment:  PATH, HOME, LANG, and what `systemctl --user` needs to reach
    the user manager (XDG_RUNTIME_DIR, and the session bus address when the caller has one)."""

    def __init__(self, path_env, home, runtime_dir, bus="", timeout=120):
        self.path_env = path_env
        self.timeout = timeout
        self.base_env = {"PATH": path_env, "HOME": str(home), "LANG": "C.UTF-8", "XDG_RUNTIME_DIR": runtime_dir}
        if bus:
            self.base_env["DBUS_SESSION_BUS_ADDRESS"] = bus

    def which(self, tool):
        return shutil.which(tool, path=self.path_env)

    def run(self, argv, stdin=None, cwd=None, timeout=None):
        exe = self.which(argv[0]) if "/" not in argv[0] else argv[0]
        if not exe:
            return 127, "", "%s not found" % argv[0]
        try:
            result = subprocess.run(
                [exe] + list(argv[1:]),
                input=stdin,
                capture_output=True,
                text=True,
                env=dict(self.base_env),
                cwd=cwd,
                timeout=timeout or self.timeout,
            )
        except subprocess.TimeoutExpired:
            return 124, "", "timed out"
        except OSError as exc:
            return 126, "", "could not run %s (%s)" % (argv[0], type(exc).__name__)
        return result.returncode, result.stdout, result.stderr

    def systemctl(self, *args):
        return self.run(["systemctl", "--user"] + list(args))


def check_reads(host, interpreter, runtime, tokens, plan):
    """Run the runtime's own `check` with both tokens on standard input.  Its answer is names and reason tokens."""
    stdin = "".join("%s=%s\n" % (name, tokens[name]) for name in TOKENS)
    code, out, _err = host.run([interpreter, "-E", "-s", "-m", MODULE, "check"], stdin=stdin, cwd=str(runtime))
    stdin = ""
    report = None
    for line in reversed((out or "").strip().splitlines()):
        try:
            report = json.loads(line)
        except ValueError:
            continue
        break
    if not isinstance(report, dict) or not all(key in report for key in CHECKED):
        plan.refuse("the installed runtime could not run `%s check` (exit %d);  update the fleet-cli runtime to a "
                    "release that carries the shared unit" % (MODULE, code))
        return
    for key in CHECKED:
        value = str(report.get(key) or "unknown")[:80]
        if value == "present":
            say("%s: present" % key)
        else:
            plan.refuse("%s cannot be read (%s)" % (key, value))


def atomic_write(path, text, mode):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / (".%s.%s.tmp" % (path.name, secrets.token_hex(4)))
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(str(tmp), mode)
        os.replace(str(tmp), str(path))
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def locations(args, home):
    staging = home / ".config" / "infisical-staging"
    staged = {
        KNOBS_TOKEN: Path(args.knobs_staged) if args.knobs_staged else staging / STAGED[KNOBS_TOKEN],
        CURSOR_TOKEN: Path(args.cursor_staged) if args.cursor_staged else staging / STAGED[CURSOR_TOKEN],
    }
    return home / ".config" / "systemd" / "user", home / ".config" / "afc-cursor-cloud-letters" / "env", staged


def render_units(here):
    """{unit: text} from the templates beside this script, with the marker on top."""
    head = "%s;  --uninstall removes it.\n" % MARKER
    out = {}
    for unit in UNITS:
        text = (Path(here) / unit).read_text(encoding="utf-8")
        out[unit] = head + text
    return out


def install(args, plan, host, home, uid, here, interpreter):
    user = pwd.getpwuid(uid).pw_name
    unit_dir, env_path, staged = locations(args, home)
    runtime = home / RUNTIME

    if host.which("systemctl") is None:
        plan.refuse("systemctl is not available")
    for unit in UNITS:
        if (unit_dir / unit).exists() and not ours(unit_dir / unit):
            plan.refuse("%s exists and was not written by this script;  move it aside first" % (unit_dir / unit))
    code, out, _err = host.run(["loginctl", "show-user", user, "--property=Linger", "--value"])
    if code != 0 or out.strip() != "yes":
        plan.refuse("lingering is off for %s, so the timer would stop at logout;  the owner runs "
                    "`loginctl enable-linger %s`" % (user, user))
    code, out, _err = host.run([interpreter, "-I", "-c", "import sys; print(sys.version_info >= (3, 11))"])
    if code != 0 or out.strip() != "True":
        plan.refuse("%s must exist and be Python 3.11 or newer (the unit runs it)" % interpreter)
    if not (runtime / "simple_tools" / "cursor_cloud_letters.py").is_file():
        plan.refuse("no fleet-cli runtime at %s;  install the fleet tools first" % runtime)
    units = None
    try:
        units = render_units(here)
    except (OSError, UnicodeDecodeError) as exc:
        plan.refuse("cannot read the unit templates beside this script (%s)" % type(exc).__name__)

    # The tokens:  already installed, or both staged and about to move.  Never both places, never neither.
    tokens = None
    installed, installed_problem = read_env(env_path, uid)
    staged_found = {}
    staged_problems = {}
    for name in TOKENS:
        token, problem = read_staged(staged[name], name, uid)
        if token:
            staged_found[name] = token
        else:
            staged_problems[name] = problem
    any_staged = any(problem != "missing" for problem in staged_problems.values()) or bool(staged_found)
    if installed_problem != "missing":
        if installed_problem:
            plan.refuse(installed_problem)
        elif any_staged:
            plan.refuse("%s is installed and a staged token file exists too;  keep one" % env_path)
        else:
            tokens = installed
    else:
        for name in TOKENS:
            problem = staged_problems.get(name, "")
            if problem == "missing":
                plan.refuse("no %s:  stage %s in %s (0600)" % (name, TOKEN_LABEL[name], staged[name]))
            elif problem:
                plan.refuse(problem)
        if len(staged_found) == len(TOKENS):
            tokens = dict(staged_found)
    staged_found = {}

    if tokens and (runtime / "simple_tools" / "cursor_cloud_letters.py").is_file():
        check_reads(host, interpreter, runtime, tokens, plan)
    else:
        say("reads: not checked (no usable tokens or no runtime)")

    if (unit_dir / RETIRED_TIMER).exists():
        say("the retired per-seat %s is still installed;  disable it before --start, and remove it by hand once "
            "the shared unit runs clean (README, \"Retiring the per-seat unit\")" % RETIRED_TIMER)
    if args.start:
        code, out, _err = host.systemctl("is-active", RETIRED_TIMER)
        if out.strip() == "active":
            plan.refuse("--start is refused while %s is active, so two sweeps never run side by side;  run "
                        "`systemctl --user disable --now %s` first" % (RETIRED_TIMER, RETIRED_TIMER))

    if plan.refusals:
        tokens = None
        say("%d problem(s);  nothing was changed" % len(plan.refusals))
        return 3 if plan.dry else 1
    if plan.dry:
        tokens = None
        say("dry run:  a real run would %s, write %s and %s, reload systemd and enable %s%s" % (
            "keep the installed env file" if installed_problem == "" else "move both staged tokens into %s" % env_path,
            SERVICE, TIMER, TIMER, " and start it" if args.start else ""))
        return 0

    # --- every check passed:  write ------------------------------------------------------------------------------
    if installed_problem == "missing":
        env_path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(str(env_path.parent), 0o700)
        text = ("# Read-only Infisical service tokens for afc-cursor-cloud-letters.service, nothing else.  "
                "Written by install-cursor-cloud-letters.sh.\n"
                "# %s\n%s=%s\n# %s\n%s=%s\n" % (
                    TOKEN_LABEL[KNOBS_TOKEN], KNOBS_TOKEN, tokens[KNOBS_TOKEN],
                    TOKEN_LABEL[CURSOR_TOKEN], CURSOR_TOKEN, tokens[CURSOR_TOKEN]))
        atomic_write(env_path, text, 0o600)
        text = ""
        for name in TOKENS:
            staged[name].unlink()
        say("moved both staged tokens to %s (0600)" % env_path)
    tokens = None

    for unit, text in units.items():
        path = unit_dir / unit
        old = path.read_text(encoding="utf-8") if path.exists() else None
        if old != text:
            atomic_write(path, text, 0o644)
            say("wrote %s" % path)
        else:
            say("unchanged %s" % path)
    if host.systemctl("daemon-reload")[0] != 0 or host.systemctl("enable", TIMER)[0] != 0:
        say("systemctl --user could not reload or enable %s" % TIMER)
        return 1
    say("enabled %s" % TIMER)
    if not args.start:
        # Only --start checks that the retired per-seat timer is not active, so it is the one route offered.
        say("not started;  run again with --start, which first checks that %s is not active" % RETIRED_TIMER)
        return 0
    if host.systemctl("start", TIMER)[0] != 0:
        say("systemctl --user start %s failed" % TIMER)
        return 1
    say("started %s" % TIMER)
    return 0


def confirmed_stopped(code, out):
    return code in STOPPED_CODES and out.strip() in STOPPED_STATES and "\n" not in out.strip()


def uninstall(args, plan, host, home, uid):
    unit_dir, env_path, staged = locations(args, home)
    remove_units = []
    for unit in UNITS:
        path = unit_dir / unit
        if path.exists():
            if ours(path):
                remove_units.append(unit)
            else:
                plan.refuse("%s was not written by this script;  it is left alone" % path)
    installed = None
    if env_path.exists():
        installed, problem = read_env(env_path, uid)
        if problem:
            plan.refuse("%s;  move it aside by hand" % problem)
        for name in TOKENS:
            if staged[name].exists():
                plan.refuse("%s exists;  the token cannot move back without overwriting it" % staged[name])
    if plan.refusals:
        installed = None
        return 3 if plan.dry else 1
    actions = ["stop, disable and remove %s" % unit for unit in remove_units]
    if installed:
        actions.append("move both tokens back to %s and %s" % (staged[KNOBS_TOKEN], staged[CURSOR_TOKEN]))
    if not actions:
        say("nothing installed")
        return 0
    if plan.dry:
        installed = None
        for action in actions:
            say("dry run:  would " + action)
        return 0
    # Stop first and prove both units are down before anything is removed:  a failure here leaves every file and
    # the tokens in place.
    if TIMER in remove_units and host.systemctl("disable", "--now", TIMER)[0] != 0:
        say("systemctl --user disable --now %s failed;  nothing was removed" % TIMER)
        return 1
    if remove_units and host.systemctl("stop", SERVICE)[0] != 0:
        say("systemctl --user stop %s failed;  nothing was removed" % SERVICE)
        return 1
    for unit in remove_units:
        code, out, _err = host.systemctl("is-active", unit)
        if not confirmed_stopped(code, out):
            say("%s is not confirmed stopped (is-active answered %r, exit %d);  nothing was removed"
                % (unit, out.strip()[:40], code))
            return 1
    for unit in remove_units:
        (unit_dir / unit).unlink()
        say("removed %s" % unit)
    if remove_units and host.systemctl("daemon-reload")[0] != 0:
        say("systemctl --user daemon-reload failed after the unit files were removed;  the tokens are kept:  run "
            "daemon-reload, then --uninstall again")
        return 1
    if installed:
        for name in TOKENS:
            atomic_write(staged[name], "%s=%s\n" % (name, installed[name]), 0o600)
        installed = None
        env_path.unlink()
        say("moved both tokens back to their staging files")
        try:
            env_path.parent.rmdir()
        except OSError:
            pass
    say("the sweep's state (~/.simple-tools/CURSOR/cloud-letters.json) is kept")
    return 0


def main(argv, environ, uid):
    args = parse_args(argv)
    plan = Plan(args.dry_run)
    if args.dry_run:
        say("dry run:  nothing will be changed")
    if uid == 0:
        plan.refuse("this runs as the jay user, never root (no sudo);  run it again as jay")
        return 3 if plan.dry else 1
    if args.uninstall and args.start:
        plan.refuse("--uninstall and --start cannot be combined")
        return 3 if plan.dry else 2
    home, test_root, problem = paths_for(uid, environ)
    if problem:
        plan.refuse(problem)
        return 3 if plan.dry else 2
    path_env = environ.get("PATH", "/usr/bin:/bin")
    if test_root:
        path_env = os.path.join(test_root, "bin") + os.pathsep + path_env
    runtime_dir = environ.get("XDG_RUNTIME_DIR") or "/run/user/%d" % uid
    host = Host(path_env, home, runtime_dir, environ.get("DBUS_SESSION_BUS_ADDRESS", ""))
    if args.uninstall:
        return uninstall(args, plan, host, home, uid)
    # The unit runs /usr/bin/python3;  offline tests use the interpreter running them.
    interpreter = sys.executable if test_root else "/usr/bin/python3"
    return install(args, plan, host, home, uid, environ.get("CURSOR_LETTERS_INSTALLER_HERE", "."), interpreter)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:], dict(os.environ), os.getuid()))
PY
