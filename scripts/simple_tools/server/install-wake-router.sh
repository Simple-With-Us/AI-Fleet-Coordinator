#!/usr/bin/env bash
# Install the one wake router and its TLS front as systemd --user units (OPS-1048, AFC-694).
#
#   bash scripts/simple_tools/server/install-wake-router.sh --dry-run --tailnet-addr 100.x.y.z
#   bash scripts/simple_tools/server/install-wake-router.sh --tailnet-addr 100.x.y.z \
#        --tls-cert CERT --tls-key KEY
#   bash scripts/simple_tools/server/install-wake-router.sh --start
#   bash scripts/simple_tools/server/install-wake-router.sh --uninstall
#
# Run it on the server as the jay user.  It refuses to run as root and never uses sudo:  the
# router serves every seat from one process (afc-wake-router.service, loopback 127.0.0.1:8794),
# and its TLS front (afc-wake-tls.service, socat on the tailnet address, port 8795) is the
# router's only door from the tailnet.
#
# What it does.  It checks everything first and changes nothing until every check passes;  a
# real run that finds a problem lists them all and stops.  Then it:
#   1. fetches origin/main in the checkout this script came from (or --repo) and installs a clean
#      `git archive` of origin/main's scripts/ into ~/.local/share/afc-wake-router/releases/<sha>,
#      with ~/.local/share/afc-wake-router/current pointing at it.  Nothing is imported or run from
#      a working checkout;  the knob check below runs the archive's code;
#   2. writes ~/.config/systemd/user/afc-wake-router.service and afc-wake-tls.service from the
#      archive's templates, with the router's ExecStart pointing at the installed release.  A unit
#      already there that this script did not write is never overwritten;
#   3. moves the staged token (~/.config/infisical-staging/afc-wake-router.env, or --staged) into
#      ~/.config/afc-wake-router/env, mode 0600.  That file may hold only
#      FLEET_KNOBS_INFISICAL_TOKEN (the read-only token afc-wake-router-ro, scoped prod:/wake-router),
#      comments and blank lines;  an "export " prefix or any other name is refused;
#   4. copies the tailnet certificate and key (--tls-cert, --tls-key) to
#      ~/.local/state/afc-wake-router/tls/ (key 0600) for the front;
#   5. runs `systemctl --user daemon-reload` and enables both units.
# It checks that lingering is on for the user, that no root unit named *wake-router* is left in
# /etc/systemd/system, and that /wake-router holds WAKE_ROUTER_DELIVERY_ENABLED, WAKE_ROUTER_KEY
# and WAKE_ROUTER_MAC_SSH (which must be mac-wake), by name through fleet_knobs:  presence only, no
# value is ever printed.
#
# Options:
#   --dry-run          check and print the plan, change nothing (any user;  exit 3 when a real
#                      run would refuse, listing every reason)
#   --start            also start both units, or restart a unit that is running stale code.
#                      Refused while delivery is armed (WAKE_ROUTER_DELIVERY_ENABLED true)
#   --uninstall        stop, disable and remove exactly what this script installed, and move the
#                      token back to the staging file
#   --tailnet-addr A   the server's tailnet IPv4 address (100.64.0.0/10);  default: `tailscale ip -4`
#   --tls-cert F, --tls-key F   the tailnet certificate and key the front serves (needed on the
#                      first install;  later runs keep the installed copies)
#   --staged F         the staged token file (default ~/.config/infisical-staging/afc-wake-router.env)
#   --repo DIR         the git checkout to read origin/main from (default: the one holding this file)
#   --bind A, --port N, --front-port N   defaults 127.0.0.1, 8794, 8795.  The router binds
#                      loopback only;  ports are plain decimal, 1025-65535, never 8793
#
# Offline tests set WAKE_ROUTER_TEST_ROOT:  home and /etc move under it and its bin/ comes first
# on PATH.  It is ignored when the caller is root.
set -euo pipefail

for arg in "$@"; do
  if [[ "$arg" == "-h" || "$arg" == "--help" ]]; then
    awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "$0"
    exit 0
  fi
done

HERE="$(cd -P -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="$(command -v python3 || true)"
[[ -n "$PYTHON" ]] || { echo "install-wake-router: python3 is missing" >&2; exit 1; }
export WAKE_ROUTER_INSTALLER_HERE="$HERE"
exec "$PYTHON" -I - "$@" <<'PY'
import argparse
import hashlib
import io
import ipaddress
import json
import os
import pwd
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

NAME = "install-wake-router"
ROUTER_UNIT = "afc-wake-router.service"
FRONT_UNIT = "afc-wake-tls.service"
UNITS = (ROUTER_UNIT, FRONT_UNIT)
MARKER = "# Installed by install-wake-router.sh"
TEMPLATE_DIR = "scripts/simple_tools/server"
TEMPLATE_EXEC = "%h/.local/share/fleet-cli/current/scripts/simple-chat"
INSTALLED_EXEC = "%h/.local/share/afc-wake-router/current/scripts/simple-chat"
TEMPLATE_BIND = "--bind 127.0.0.1 --port 8794"
TEMPLATE_ENV = "EnvironmentFile=-%h/.config/afc-wake-router/env"
TOKEN = "FLEET_KNOBS_INFISICAL_TOKEN"
STAGED_NAME = "afc-wake-router.env"
REQUIRED = ("WAKE_ROUTER_DELIVERY_ENABLED", "WAKE_ROUTER_KEY", "WAKE_ROUTER_MAC_SSH")
REPORTED = (
    "WAKE_ROUTER_DELIVERY_SEATS",
    "SIMPLE_TOOLS_OPERATOR_REGISTRY_URL",
    "SIMPLE_TOOLS_OPERATOR_REGISTRY_KEY",
)
MAC_SSH = "mac-wake"
RESERVED_PORTS = frozenset({8793})  # zc-wake-tls.service
LOOPBACK_ALLOWED = frozenset({"127.0.0.1"})
TAILNET = ipaddress.ip_network("100.64.0.0/10")
PORT_RE = re.compile(r"[1-9][0-9]{3,4}")
TEST_ROOT_ENV = "WAKE_ROUTER_TEST_ROOT"

# Runs in a child Python with the archive's scripts/ first on sys.path.  The token comes in on
# stdin and goes only into this process's environment, the way fleet_knobs reads it.  It prints
# names, booleans and reason tokens:  never a value.
KNOB_CHECK = r"""
import json, os, sys
sys.path.insert(0, sys.argv[1])
token = sys.stdin.readline().strip()
os.environ["FLEET_KNOBS_INFISICAL_TOKEN"] = token
del token
from fleet_knobs import knobs
from fleet_knobs.registry import BY_NAME
out = {"names": {}, "armed": None, "mac_ssh": "missing", "folder": "/wake-router"}
for name in json.loads(sys.argv[2]):
    spec = BY_NAME.get(name)
    if spec is None:
        out["names"][name] = {"present": False, "reason": "not-registered"}
        continue
    if spec.folder != "/wake-router":
        out["names"][name] = {"present": False, "reason": "wrong-folder"}
        continue
    try:
        knob = knobs.resolve(name)
    except knobs.SecretUnavailable as exc:
        out["names"][name] = {"present": False, "reason": str(getattr(exc, "reason", "unavailable"))}
        continue
    except Exception as exc:
        out["names"][name] = {"present": False, "reason": "error-" + type(exc).__name__}
        continue
    present = knob.source == "infisical"
    out["names"][name] = {"present": present, "reason": "" if present else str(knob.reason or "unset")}
    if name == "WAKE_ROUTER_DELIVERY_ENABLED" and present:
        out["armed"] = knob.value is True
    if name == "WAKE_ROUTER_MAC_SSH" and present:
        out["mac_ssh"] = "ok" if knob.value == "mac-wake" else "other"
    knob = None
out["fetch"] = knobs.folder_status("/wake-router").get("fetch", "not-attempted")
print(json.dumps(out))
"""


def say(message):
    print("%s: %s" % (NAME, message))


class Plan:
    """Every refusal is collected first, so a dry run lists them all and a real run changes
    nothing unless there are none."""

    def __init__(self, dry):
        self.dry = dry
        self.refusals = []

    def refuse(self, message):
        self.refusals.append(message)
        say(("a real run would refuse: " if self.dry else "refused: ") + message)


def parse_args(argv):
    parser = argparse.ArgumentParser(prog="install-wake-router.sh", add_help=False)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--start", action="store_true")
    parser.add_argument("--uninstall", action="store_true")
    parser.add_argument("--tailnet-addr", default="")
    parser.add_argument("--tls-cert", default="")
    parser.add_argument("--tls-key", default="")
    parser.add_argument("--staged", default="")
    parser.add_argument("--repo", default="")
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", default="8794")
    parser.add_argument("--front-port", default="8795")
    try:
        return parser.parse_args(argv)
    except SystemExit as exc:
        raise SystemExit(2 if exc.code else 0)


def check_port(text, label):
    """A plain decimal port above 1024:  no sign, no leading zero, no hex, octal or spaces."""
    if not isinstance(text, str) or not PORT_RE.fullmatch(text):
        return None, "%s must be a plain decimal port (no leading zero), got %r" % (label, text)
    port = int(text, 10)
    if not 1025 <= port <= 65535:
        return None, "%s must be between 1025 and 65535" % label
    if port in RESERVED_PORTS:
        return None, "%s %d belongs to zc-wake-tls.service" % (label, port)
    return port, ""


def check_bind(text):
    """The router's bind address must be on the loopback allowlist, written canonically."""
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        return "--bind %r is not an IP address" % text
    if str(address) != text or text not in LOOPBACK_ALLOWED:
        return "--bind must be one of %s (the router is loopback only;  the front serves the tailnet)" % (
            ", ".join(sorted(LOOPBACK_ALLOWED)))
    return ""


def check_tailnet(text):
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        return "the tailnet address %r is not an IPv4 address" % text
    if address.version != 4 or str(address) != text or address not in TAILNET:
        return "the tailnet address must be a canonical IPv4 address in %s" % TAILNET
    return ""


def paths_for(uid, environ):
    """(home, etc, test_root, refusal).  The test root is honoured only for a caller that is not
    root;  root is refused before any path is used."""
    test_root = "" if uid == 0 else environ.get(TEST_ROOT_ENV, "")
    if test_root:
        root = Path(test_root)
        if not root.is_absolute() or root.is_symlink() or not root.is_dir():
            return None, None, "", "%s must be an existing absolute directory" % TEST_ROOT_ENV
        return root / "home", root / "etc", test_root, ""
    try:
        home = Path(pwd.getpwuid(uid).pw_dir)
    except KeyError:
        return None, None, "", "the caller has no passwd entry"
    return home, Path("/etc"), "", ""


def read_token_file(path, uid, allow_bare):
    """(token, problem).  A regular file, not a link, owned by the caller, no group or other
    access, holding exactly one FLEET_KNOBS_INFISICAL_TOKEN line plus comments and blank lines.
    The token is never printed;  only the problem is."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None, "missing"
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        return None, "%s must be a regular file, not a link" % path
    if info.st_uid != uid:
        return None, "%s must be owned by the installing user" % path
    if info.st_mode & 0o077:
        return None, "%s must have no group or other access (mode 0600)" % path
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None, "%s cannot be read as text" % path
    token = None
    lines = [line.strip() for line in text.splitlines()]
    body = [line for line in lines if line and not line.startswith("#")]
    for number, line in enumerate(body, 1):
        if line.startswith("export ") or line.startswith("export\t"):
            return None, "%s: an \"export \" prefix is not allowed (systemd ignores the line)" % path
        name, sep, value = line.partition("=")
        if not sep:
            if allow_bare and len(body) == 1 and re.fullmatch(r"[^\s=\"']{16,}", line):
                return line, ""
            return None, "%s: line %d is not NAME=value" % (path, number)
        if name != TOKEN:
            return None, "%s may hold only %s;  it also names %s" % (path, TOKEN, name or "(blank)")
        if token is not None:
            return None, "%s names %s twice" % (path, TOKEN)
        if not re.fullmatch(r"[^\s\"']{16,}", value):
            return None, "%s: the %s value is empty, short, quoted or has spaces" % (path, TOKEN)
        token = value
    if token is None:
        return None, "%s holds no %s line" % (path, TOKEN)
    return token, ""


def ours(path):
    """Whether a unit file is one this script wrote (its marker line is near the top)."""
    try:
        with open(path, encoding="utf-8") as handle:
            head = [handle.readline() for _ in range(3)]
    except (OSError, UnicodeDecodeError):
        return False
    return any(line.startswith(MARKER) for line in head)


class Host:
    def __init__(self, path_env, home):
        self.path_env = path_env
        self.home = str(home)

    def which(self, tool):
        return shutil.which(tool, path=self.path_env)

    def run(self, argv, check=False, stdin=None, env=None, binary=False):
        exe = self.which(argv[0]) if "/" not in argv[0] else argv[0]
        if not exe:
            return 127, "" if not binary else b"", "%s not found" % argv[0]
        result = subprocess.run(
            [exe] + list(argv[1:]),
            input=stdin,
            capture_output=True,
            text=not binary,
            env=env if env is not None else {"PATH": self.path_env, "HOME": self.home, "LANG": "C.UTF-8"},
            timeout=300,
        )
        if check and result.returncode != 0:
            raise SystemExit("%s: %s failed (%d)" % (NAME, " ".join(argv[:3]), result.returncode))
        return result.returncode, result.stdout, result.stderr

    def systemctl(self, *args):
        return self.run(["systemctl", "--user"] + list(args))


def safe_extract(blob, dest):
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:") as archive:
        for member in archive.getmembers():
            name = member.name
            if name.startswith("/") or ".." in Path(name).parts:
                raise SystemExit("%s: the archive holds an unsafe path" % NAME)
            if not (member.isfile() or member.isdir() or member.issym()):
                raise SystemExit("%s: the archive holds an unexpected entry type" % NAME)
            if member.issym():
                target = (Path(name).parent / member.linkname)
                if member.linkname.startswith("/") or ".." in Path(os.path.normpath(target)).parts:
                    raise SystemExit("%s: the archive holds a link that leaves it" % NAME)
        if hasattr(tarfile, "data_filter"):
            archive.extractall(dest, filter="data")
        else:
            archive.extractall(dest)


def fetch_release(host, repo, plan):
    """(sha, tar bytes) of origin/main's scripts/, or (None, None) after a refusal.  Git runs as
    the caller, with the checkout's hooks and fsmonitor turned off;  only committed objects are
    read, never the working tree."""
    git = ["git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null", "-C", str(repo)]
    code, _out, _err = host.run(git + ["rev-parse", "--git-dir"])
    if code != 0:
        plan.refuse("%s is not a git checkout (pass --repo)" % repo)
        return None, None
    code, _out, _err = host.run(git + ["fetch", "--quiet", "origin", "main"])
    if code != 0:
        plan.refuse("could not fetch origin main in %s" % repo)
        return None, None
    code, out, _err = host.run(git + ["rev-parse", "--verify", "refs/remotes/origin/main^{commit}"])
    sha = out.strip()
    if code != 0 or not re.fullmatch(r"[0-9a-f]{40}", sha):
        plan.refuse("origin/main does not resolve to a commit in %s" % repo)
        return None, None
    code, blob, _err = host.run(git + ["archive", "--format=tar", sha, "scripts"], binary=True)
    if code != 0 or not blob:
        plan.refuse("git archive of origin/main failed")
        return None, None
    return sha, blob


def render_units(scripts, sha, args, tailnet, socat):
    """{unit name: text}, or raise ValueError naming what the templates lack."""
    server = scripts / "simple_tools" / "server"
    router = (server / ROUTER_UNIT).read_text(encoding="utf-8")
    front = (server / FRONT_UNIT).read_text(encoding="utf-8")
    for needle in (TEMPLATE_EXEC, TEMPLATE_BIND, TEMPLATE_ENV):
        if router.count(needle) != 1:
            raise ValueError("the router template does not hold %r exactly once" % needle)
    router = router.replace(TEMPLATE_EXEC, INSTALLED_EXEC).replace(
        TEMPLATE_BIND, "--bind %s --port %s" % (args.bind, args.port))
    for placeholder in ("@SOCAT@", "@FRONT_PORT@", "@TAILNET_ADDR@", "@ROUTER_ADDR@", "@ROUTER_PORT@"):
        if front.count(placeholder) != 1:
            raise ValueError("the front template does not hold %s exactly once" % placeholder)
    front = (front.replace("@SOCAT@", socat).replace("@FRONT_PORT@", args.front_port)
             .replace("@TAILNET_ADDR@", tailnet).replace("@ROUTER_ADDR@", args.bind)
             .replace("@ROUTER_PORT@", args.port))
    # Only the router's text names the release, so a new release restarts the router and leaves
    # the front, whose text does not change, running.
    head = "%s;  --uninstall removes it.\n" % MARKER
    return {ROUTER_UNIT: head + "# Release:  origin/main %s\n" % sha + router, FRONT_UNIT: head + front}


def check_knobs(host, python, scripts, token, plan, start):
    names = list(REQUIRED + REPORTED)
    code, out, _err = host.run(
        [python, "-I", "-c", KNOB_CHECK, str(scripts), json.dumps(names)],
        stdin=token + "\n",
    )
    try:
        report = json.loads(out.strip().splitlines()[-1]) if code == 0 and out.strip() else None
    except ValueError:
        report = None
    if not isinstance(report, dict):
        plan.refuse("the knob check could not run (exit %d);  is fleet_knobs in origin/main?" % code)
        return None
    if report.get("fetch") != "ok":
        plan.refuse("fleet_knobs could not read /wake-router with the staged token (%s)" % report.get("fetch"))
    found = report.get("names", {})
    for name in REQUIRED:
        row = found.get(name, {})
        if row.get("present"):
            say("knob %s: present" % name)
        else:
            plan.refuse("knob %s is not set in /wake-router (%s)" % (name, row.get("reason") or "unknown"))
    for name in REPORTED:
        row = found.get(name, {})
        say("knob %s: %s" % (name, "present" if row.get("present") else
                             "not set (%s);  the router reports it in /health" % (row.get("reason") or "unknown")))
    if report.get("mac_ssh") == "other":
        plan.refuse("WAKE_ROUTER_MAC_SSH must be %s" % MAC_SSH)
    armed = report.get("armed")
    say("delivery: %s" % {True: "armed", False: "off", None: "unknown"}[armed])
    if start and armed is not False:
        plan.refuse("--start is refused while delivery is %s;  set WAKE_ROUTER_DELIVERY_ENABLED to false "
                    "first, and arm it only after the dry run and the test canary" %
                    ("armed" if armed else "not confirmed off"))
    return report


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


def install(args, plan, host, home, etc, uid, here):
    user = pwd.getpwuid(uid).pw_name
    unit_dir = home / ".config" / "systemd" / "user"
    env_path = home / ".config" / "afc-wake-router" / "env"
    staged = Path(args.staged) if args.staged else home / ".config" / "infisical-staging" / STAGED_NAME
    base = home / ".local" / "share" / "afc-wake-router"
    tls_dir = home / ".local" / "state" / "afc-wake-router" / "tls"

    problem = check_bind(args.bind)
    if problem:
        plan.refuse(problem)
    router_port, problem = check_port(args.port, "--port")
    if problem:
        plan.refuse(problem)
    front_port, problem = check_port(args.front_port, "--front-port")
    if problem:
        plan.refuse(problem)
    if router_port is not None and router_port == front_port:
        plan.refuse("--port and --front-port must differ")

    for unit in sorted((etc / "systemd" / "system").glob("*wake-router*")):
        # The command is printed for the owner;  this script never runs it.
        plan.refuse("a root unit %s is still installed;  the owner removes it first:  sudo systemctl disable --now %s && sudo rm %s && sudo systemctl daemon-reload" % (unit, unit.name, unit))  # noqa: E501
    for unit in UNITS:
        if (unit_dir / unit).exists() and not ours(unit_dir / unit):
            plan.refuse("%s exists and was not written by this script;  move it aside first" % (unit_dir / unit))

    code, out, _err = host.run(["loginctl", "show-user", user, "--property=Linger", "--value"])
    if code != 0 or out.strip() != "yes":
        plan.refuse("lingering is off for %s, so the units would stop at logout;  the owner runs "
                    "`loginctl enable-linger %s`" % (user, user))

    tailnet = args.tailnet_addr
    if not tailnet:
        code, out, _err = host.run(["tailscale", "ip", "-4"])
        tailnet = out.strip().splitlines()[0].strip() if code == 0 and out.strip() else ""
    problem = check_tailnet(tailnet) if tailnet else "no tailnet address (pass --tailnet-addr)"
    if problem:
        plan.refuse(problem)
    socat = host.which("socat") or ""
    if not socat:
        plan.refuse("socat is not installed")

    # The token:  already installed, or staged and about to move.  Never both, never neither.
    token, installed_problem = read_token_file(env_path, uid, allow_bare=False)
    staged_token, staged_problem = read_token_file(staged, uid, allow_bare=True)
    if installed_problem != "missing" and staged_problem != "missing":
        plan.refuse("both %s and %s exist;  keep one" % (env_path, staged))
        token = None
    elif installed_problem == "missing" and staged_problem == "missing":
        plan.refuse("no token:  stage afc-wake-router-ro in %s (0600)" % staged)
    elif installed_problem == "missing":
        if staged_problem:
            plan.refuse(staged_problem)
        token = staged_token
    elif installed_problem:
        plan.refuse(installed_problem)
        token = None
    staged_token = None

    cert = Path(args.tls_cert) if args.tls_cert else None
    key = Path(args.tls_key) if args.tls_key else None
    if bool(cert) != bool(key):
        plan.refuse("pass --tls-cert and --tls-key together")
    elif cert:
        for path, private in ((cert, False), (key, True)):
            try:
                info = os.stat(path)
            except OSError:
                plan.refuse("%s cannot be read" % path)
                continue
            if not stat.S_ISREG(info.st_mode):
                plan.refuse("%s must be a regular file" % path)
            elif private and (info.st_uid != uid or info.st_mode & 0o077):
                plan.refuse("%s must be owned by the installing user with no group or other access" % path)
    elif not ((tls_dir / "cert.pem").is_file() and (tls_dir / "key.pem").is_file()):
        plan.refuse("no TLS certificate installed yet:  pass --tls-cert and --tls-key")

    repo = Path(args.repo) if args.repo else Path(here).parents[2]
    sha, blob = fetch_release(host, repo, plan)
    workdir = Path(tempfile.mkdtemp(prefix="wake-router-release-"))
    try:
        units = None
        scripts = workdir / "scripts"
        if blob:
            safe_extract(blob, workdir)
            if not (scripts / "simple-chat").is_file():
                plan.refuse("origin/main has no scripts/simple-chat")
            try:
                units = render_units(scripts, sha, args, tailnet or "0.0.0.0", socat or "/usr/bin/socat")
            except (OSError, ValueError) as exc:
                plan.refuse("cannot render the units from origin/main:  %s" % exc)
            if token:
                check_knobs(host, sys.executable, scripts, token, plan, args.start)
            else:
                say("knobs: not checked (no usable token)")
        say("release: origin/main %s" % (sha or "unknown"))

        if plan.refusals:
            say("%d problem(s);  nothing was changed" % len(plan.refusals))
            return 3 if plan.dry else 1
        if plan.dry:
            say("dry run:  a real run would install release %s, write %s and %s, %s, enable both units%s"
                % (sha[:12], ROUTER_UNIT, FRONT_UNIT,
                   "keep the installed token" if installed_problem == "" else "move the staged token",
                   " and start them" if args.start else ""))
            return 0

        # --- every check passed:  write ---------------------------------------------------------
        releases = base / "releases"
        releases.mkdir(parents=True, exist_ok=True)
        os.chmod(str(base), 0o700)
        target = releases / sha
        if target.exists():
            shutil.rmtree(target)
        fresh = releases / (".new-" + secrets.token_hex(4))
        fresh.mkdir()
        shutil.move(str(scripts), str(fresh / "scripts"))
        os.replace(str(fresh), str(target))
        current = base / "current"
        before = os.readlink(current) if current.is_symlink() else ""
        link = base / (".current-" + secrets.token_hex(4))
        os.symlink(os.path.join("releases", sha), str(link))
        os.replace(str(link), str(current))
        release_changed = before != os.path.join("releases", sha)
        for old in releases.iterdir():
            if old.name != sha:
                shutil.rmtree(old) if old.is_dir() and not old.is_symlink() else old.unlink()
        say("installed release %s" % sha[:12])

        if installed_problem == "missing":
            env_path.parent.mkdir(parents=True, exist_ok=True)
            os.chmod(str(env_path.parent), 0o700)
            atomic_write(env_path, "# afc-wake-router-ro:  read-only, scoped prod:/wake-router.  "
                         "Written by install-wake-router.sh.\n%s=%s\n" % (TOKEN, token), 0o600)
            staged.unlink()
            say("moved the staged token to %s (0600)" % env_path)
        token = None

        if cert:
            tls_dir.mkdir(parents=True, exist_ok=True)
            os.chmod(str(tls_dir), 0o700)
            atomic_write(tls_dir / "cert.pem", cert.read_text(encoding="utf-8"), 0o644)
            atomic_write(tls_dir / "key.pem", key.read_text(encoding="utf-8"), 0o600)
            say("installed the TLS certificate and key under %s" % tls_dir)

        changed = {}
        for unit, text in units.items():
            path = unit_dir / unit
            old = path.read_text(encoding="utf-8") if path.exists() else None
            changed[unit] = old != text
            if changed[unit]:
                atomic_write(path, text, 0o644)
                say("wrote %s" % path)
        host_ok = host.systemctl("daemon-reload")[0] == 0
        if not host_ok or host.systemctl("enable", *UNITS)[0] != 0:
            say("systemctl --user could not reload or enable the units")
            return 1
        say("enabled %s" % " and ".join(UNITS))

        if not args.start:
            say("not started;  run again with --start, or `systemctl --user start %s`" % " ".join(UNITS))
            return 0
        for unit in UNITS:
            active = host.systemctl("is-active", unit)[1].strip() == "active"
            stale = host.systemctl("show", "--property=NeedDaemonReload", "--value", unit)[1].strip() == "yes"
            stale = stale or changed[unit] or (unit == ROUTER_UNIT and release_changed) or (
                unit == FRONT_UNIT and bool(cert))
            verb = "restart" if active and stale else ("start" if not active else None)
            if verb is None:
                say("%s is running the installed code" % unit)
                continue
            if host.systemctl(verb, unit)[0] != 0:
                say("systemctl --user %s %s failed" % (verb, unit))
                return 1
            say("%s %s" % ("restarted" if verb == "restart" else "started", unit))
        return 0
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def uninstall(args, plan, host, home, uid):
    unit_dir = home / ".config" / "systemd" / "user"
    env_path = home / ".config" / "afc-wake-router" / "env"
    staged = Path(args.staged) if args.staged else home / ".config" / "infisical-staging" / STAGED_NAME
    base = home / ".local" / "share" / "afc-wake-router"
    tls_dir = home / ".local" / "state" / "afc-wake-router" / "tls"
    remove_units = []
    for unit in UNITS:
        path = unit_dir / unit
        if path.exists():
            if ours(path):
                remove_units.append(unit)
            else:
                plan.refuse("%s was not written by this script;  it is left alone" % path)
    if env_path.exists() and staged.exists():
        plan.refuse("both %s and %s exist;  the token cannot move back without overwriting one" % (env_path, staged))
    if plan.refusals:
        return 3 if plan.dry else 1
    actions = ["stop, disable and remove %s" % unit for unit in remove_units]
    if env_path.exists():
        actions.append("move %s back to %s" % (env_path, staged))
    for path in (base, tls_dir):
        if path.exists():
            actions.append("remove %s" % path)
    if not actions:
        say("nothing installed")
        return 0
    if plan.dry:
        for action in actions:
            say("dry run:  would " + action)
        return 0
    for unit in remove_units:
        host.systemctl("disable", "--now", unit)
        (unit_dir / unit).unlink()
        say("removed %s" % unit)
    if remove_units:
        host.systemctl("daemon-reload")
    if env_path.exists():
        staged.parent.mkdir(parents=True, exist_ok=True)
        os.replace(str(env_path), str(staged))
        os.chmod(str(staged), 0o600)
        say("moved the token back to %s" % staged)
    for path in (base, tls_dir):
        if path.exists():
            shutil.rmtree(path)
            say("removed %s" % path)
    for path in (env_path.parent, tls_dir.parent):
        try:
            path.rmdir()
        except OSError:
            pass
    say("the router's own state (~/.local/state/wake-router) is kept")
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
    home, etc, test_root, problem = paths_for(uid, environ)
    if problem:
        plan.refuse(problem)
        return 3 if plan.dry else 2
    path_env = environ.get("PATH", "/usr/bin:/bin")
    if test_root:
        path_env = os.path.join(test_root, "bin") + os.pathsep + path_env
    host = Host(path_env, home)
    if args.uninstall:
        return uninstall(args, plan, host, home, uid)
    if host.which("systemctl") is None:
        plan.refuse("systemctl is not available")
    return install(args, plan, host, home, etc, uid, environ.get("WAKE_ROUTER_INSTALLER_HERE", "."))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:], dict(os.environ), os.getuid()))
PY
