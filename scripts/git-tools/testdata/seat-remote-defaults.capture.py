# Seat-scoped fleet CLI defaults for a shared host (simple-chat, simple-board, recall).
# No credential values are stored here:  the bearer is read at run time from the declared
# seat's own private file, and an undeclared process gets no bearer and no remote mode.
#
# Run by the managed command wrapper before it execs the tool:
#     runpy.run_path(<this file>, init_globals={"fleet_command": "simple-chat"})
# Failure prints one generic line (never a path, a value or an argument) and exits 3.

import hmac
import os
import re
import stat
import sys

# One pinned location and owner per platform.  Nothing here is read from the environment.
import pwd

BEARER_OWNER_UID = 501 if sys.platform == "darwin" else 1001
try:
    # The home comes from the account database for the pinned uid, never from $HOME.
    BEARER_DIR = os.path.join(pwd.getpwuid(BEARER_OWNER_UID).pw_dir, ".secrets", "fleet-tools")
except KeyError:
    BEARER_DIR = "/nonexistent/.secrets/fleet-tools"
TRUSTED_ANCHOR = "/"
MAX_BEARER_BYTES = 202
SEAT_PATTERN = re.compile(r"[A-Z][A-Z0-9-]{1,30}")
BEARER_PATTERN = re.compile(r"[A-Za-z0-9_-]{20,200}")

COMMANDS = {
    "simple-chat": {"mode": "SIMPLE_CHAT_REMOTE", "native": "SIMPLE_CHAT_NATIVE_ECHO"},
    "simple-board": {"mode": "SIMPLE_BOARD_REMOTE", "native": "SIMPLE_BOARD_NATIVE_ECHO"},
    "recall": {"mode": "SIMPLE_RECALL_REMOTE", "native": None},
}
# Options that carry their own identity or connection:  the caller chose, so this helper stays out.
IDENTITY_OPTIONS = ("--as", "--rc", "--default-seat")
# Options that take the next argument as a value (simple-chat and simple-board common flags).
VALUE_OPTIONS = ("--as", "--rc", "--default-seat", "--session")
HELP_OPTIONS = ("--help", "--version")
# recall verbs the hosted route does not serve:  they keep running locally.
RECALL_LOCAL_VERBS = ("digest", "eval", "doctor", "ingest")


class FleetCliStop(RuntimeError):
    """A refusal with a message that is safe to print."""


def _prefix_of(token, names, minimum=3):
    # argparse accepts any unambiguous prefix of a long option, so an abbreviation counts as the option.
    if not token.startswith("--"):
        return False
    name = token.split("=", 1)[0]
    return len(name) >= minimum and any(n.startswith(name) for n in names)


def _takes_value(token):
    """A value-taking common flag, spelled out or as an unambiguous argparse abbreviation (--sess)."""
    if token in VALUE_OPTIONS:
        return True
    if token.startswith("--") and len(token) >= 4:
        return len([n for n in VALUE_OPTIONS if n.startswith(token)]) == 1
    return False


def _head(argv):
    """The arguments before a bare "--"; after it everything is positional text."""
    return argv[: argv.index("--")] if "--" in argv else list(argv)


def _declared_seat(environ):
    """The seat this process says it is, or None when it says nothing."""
    launcher = environ.get("AGENT_LAUNCHER", "").strip()
    launch_seat = environ.get("AGENT_LAUNCH_SEAT", "").strip().upper()
    seat = environ.get("AGENT_SEAT", "").strip().upper()
    tag = environ.get("AGENT_TAG", "").strip().upper()
    if launcher and not launch_seat:
        raise FleetCliStop("Launcher has no assigned seat")
    if launch_seat and not launcher:
        raise FleetCliStop("Launcher seat has no launcher")
    declared = [value for value in (launch_seat, seat, tag) if value]
    if len(set(declared)) > 1:
        raise FleetCliStop("Conflicting seat declarations")
    if not declared:
        return None
    name = declared[0]
    if SEAT_PATTERN.fullmatch(name) is None:
        raise FleetCliStop("Declared seat name has an invalid format")
    return name


def _explicit_route(command, argv):
    """True when the arguments choose their own identity, serve MCP, or only ask for help.

    Each tool's own grammar decides:
    - simple-chat: --as/--default-seat/--rc/--session are common flags, accepted before or after the command
      word and each taking a value;  the command word is the first argument that is not a flag or such a value.
    - simple-board: no top-level option exists;  argv[0] is the command word.
    - recall: the same rule the tool uses itself (normalize_argv):  any word before "--" that names one of its
      subcommands selects it, and only search, stats and contribute are served remotely."""
    head = _head(argv)
    if any(token == "-h" or token in HELP_OPTIONS or _prefix_of(token, HELP_OPTIONS) for token in head):
        return True
    if command == "recall":
        return any(token in RECALL_LOCAL_VERBS for token in head)
    if command == "simple-board":
        return bool(head) and head[0] == "mcp"
    skip = False
    first_positional = None
    for token in head:
        if skip:
            skip = False
            continue
        name = token.split("=", 1)[0]
        if name in IDENTITY_OPTIONS or _prefix_of(token, IDENTITY_OPTIONS):
            return True
        if "=" not in token and _takes_value(token):
            skip = True
            continue
        if token.startswith("-"):
            continue
        if first_positional is None:
            first_positional = token
    return first_positional == "mcp"


def _open_dir(path, dir_fd=None):
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    return os.open(path, flags, dir_fd=dir_fd)


def _dir_trusted(info, leaf):
    if not stat.S_ISDIR(info.st_mode):
        return False
    if leaf:
        return info.st_uid == BEARER_OWNER_UID and stat.S_IMODE(info.st_mode) == 0o700
    return info.st_uid in (0, BEARER_OWNER_UID) and (info.st_mode & 0o022) == 0


def _read_bearer(seat):
    """The seat's own bearer, from a regular 0600 file the pinned owner holds alone.

    The directory chain is walked by file descriptor from the trusted anchor with no symlink followed, so a
    swapped component cannot redirect the read.  Every refusal says the same thing."""
    fail = FleetCliStop("The approved bearer could not be loaded")
    anchor = os.path.normpath(TRUSTED_ANCHOR)
    target = os.path.normpath(BEARER_DIR)
    if not (target == anchor or target.startswith(anchor.rstrip("/") + "/")):
        raise fail
    parts = [p for p in target[len(anchor):].split("/") if p]
    if not parts:
        raise fail
    fds = []
    try:
        current = _open_dir(anchor)
        fds.append(current)
        if not _dir_trusted(os.fstat(current), leaf=False):
            raise fail
        for index, part in enumerate(parts):
            current = _open_dir(part, dir_fd=current)
            fds.append(current)
            if not _dir_trusted(os.fstat(current), leaf=index == len(parts) - 1):
                raise fail
        name = seat.lower()
        if re.fullmatch(r"[a-z][a-z0-9-]{1,30}", name) is None:
            raise fail
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=current)
        fds.append(fd)
        before = os.fstat(fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_uid != BEARER_OWNER_UID
            or before.st_nlink != 1
            or not 1 <= before.st_size <= MAX_BEARER_BYTES
        ):
            raise fail
        data = b""
        while len(data) <= MAX_BEARER_BYTES:
            chunk = os.read(fd, MAX_BEARER_BYTES + 1 - len(data))
            if not chunk:
                break
            data += chunk
        after = os.fstat(fd)
        stable = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_nlink) == (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_nlink)
        if not stable or len(data) > MAX_BEARER_BYTES or len(data) != before.st_size:
            raise fail
        try:
            bearer = data.decode("ascii").strip()
        except UnicodeDecodeError:
            raise fail from None
    except FleetCliStop:
        raise
    except OSError:
        raise fail from None
    finally:
        for descriptor in fds:
            try:
                os.close(descriptor)
            except OSError:
                pass
    if BEARER_PATTERN.fullmatch(bearer) is None:
        raise fail
    return bearer


def plan(command, argv, environ):
    """The environment changes this call needs, as a dict (empty: leave everything alone).  Pure apart from
    reading the seat's own file when, and only when, every explicit route has been ruled out first."""
    spec = COMMANDS.get(command)
    if spec is None:
        raise FleetCliStop("Unsupported fleet command")
    mode = spec["mode"]
    if _explicit_route(command, argv):
        return {}
    if spec["native"] and environ.get(spec["native"]) == "1":
        return {}
    if mode in environ and environ[mode].strip() != "1":
        return {}
    seat = _declared_seat(environ)
    if seat is None:
        return {}
    own = _read_bearer(seat)
    inherited = environ.get("SIMPLE_TOOLS_BEARER", "").strip()
    if inherited and not hmac.compare_digest(inherited.encode("ascii", "replace"), own.encode("ascii")):
        raise FleetCliStop("The inherited bearer is not this seat's own")
    return {"SIMPLE_TOOLS_BEARER": own, mode: "1"}


def configure():
    changes = plan(globals().get("fleet_command"), sys.argv[1:], os.environ)
    os.environ.update(changes)


if globals().get("fleet_command") in COMMANDS:
    try:
        configure()
    except FleetCliStop as exc:
        sys.stderr.write("fleet-cli: " + str(exc) + "\n")
        raise SystemExit(3)
    except (OSError, UnicodeError):
        sys.stderr.write("fleet-cli: The approved bearer could not be loaded\n")
        raise SystemExit(3)
