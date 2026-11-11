#!/usr/bin/env -S python3 -I
"""Per-seat stdio launcher for the assigned hosted MCP transport.

install.py copies this file to ~/.local/libexec/<seat>-simple-tools with the
config block below filled in.  At start it decides whether this connection may
run as its seat, checks custody of the seat's bearer file, and execs the
transport (scripts/mcp-servers/assigned-hosted-stdio.py) with only the bearer,
PATH and HOME in its environment.

Seat rules (FLEET-PROTOCOL Identity Rules):
  * A launcher-assigned seat wins.  When AGENT_LAUNCHER is set, AGENT_LAUNCH_SEAT
    must be set and must be this launcher's seat, or the connection is refused.
  * With no AGENT_LAUNCHER, a shell-exported AGENT_SEAT or AGENT_TAG that differs
    from this launcher's seat is ignored:  the bearer file alone decides identity,
    and one stderr line says what was ignored.
  * An AGENT_LAUNCH_SEAT for another seat refuses even with no AGENT_LAUNCHER.
    Only launchers write that variable, so a lone one means a launcher that lost
    AGENT_LAUNCHER, and refusing by name is safer than guessing which seat it meant.

A refusal never exits silently.  The launcher stays up and answers every request
that carries an id, initialize included, with a JSON-RPC error naming the cause,
so the client shows the cause instead of "Connection closed".  No bearer value is
ever printed.
"""

import json
import os
import re
import stat
import sys

# BEGIN LAUNCHER CONFIG (install.py rewrites this block; an unconfigured copy refuses)
SEAT = None
BEARER_FILE = None
PYTHON = None
TRANSPORT = None
HOME = None
# END LAUNCHER CONFIG

REFUSAL_CODE = -32001
MAX_FRAME = 8 * 1024 * 1024
MAX_TOKEN_CHARS = 203
SEAT_RE = re.compile(r"[A-Z][A-Z0-9-]{1,19}")
SAFE_VALUE_RE = re.compile(r"[^A-Za-z0-9_.:-]")


def shown(value):
    """An environment value made safe to put in a message:  short and inert."""
    text = SAFE_VALUE_RE.sub("?", str(value).strip())
    return text[:40] + ("..." if len(text) > 40 else "")


def norm(value):
    return (value or "").strip().upper()


def decide_seat(env, seat):
    """Return (refusal or None, notice or None).

    A refusal is (cause_code, sentence).  A notice is one stderr line.
    """
    launcher = (env.get("AGENT_LAUNCHER") or "").strip()
    launch_seat = norm(env.get("AGENT_LAUNCH_SEAT"))
    if launcher and not launch_seat:
        return (
            "seat_unassigned",
            f"AGENT_LAUNCHER={shown(launcher)} is set but assigns no AGENT_LAUNCH_SEAT, "
            f"so this {seat} connection has no seat",
        ), None
    if launch_seat and launch_seat != seat:
        source = f"launcher {shown(launcher)}" if launcher else "AGENT_LAUNCH_SEAT"
        return (
            "seat_mismatch",
            f"{source} assigned seat {shown(launch_seat)}, but this connection is "
            f"{seat}'s; a launcher-assigned seat wins, so it will not run as {seat}",
        ), None
    ignored = [
        f"{key}={shown(env[key])}"
        for key in ("AGENT_SEAT", "AGENT_TAG")
        if norm(env.get(key)) and norm(env.get(key)) != seat
    ]
    if not ignored:
        return None, None
    why = (
        f"launcher {shown(launcher)} assigned {seat}"
        if launcher
        else "no AGENT_LAUNCHER is set, so the bearer file decides"
    )
    return None, (
        f"{seat.lower()}-simple-tools: ignoring shell {', '.join(ignored)}; {why}; "
        f"this connection runs as {seat}"
    )


def read_bearer(path, seat, euid=None):
    """Return (token, None) or (None, (cause_code, sentence)).  Never shows the token."""
    euid = os.geteuid() if euid is None else euid
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        return None, ("bearer_missing", f"the {seat} bearer file {path} does not exist")
    except OSError as error:
        return None, (
            "bearer_custody",
            f"the {seat} bearer file {path} could not be opened safely "
            f"({error.__class__.__name__}; a symlink is refused)",
        )
    with os.fdopen(fd, "r", encoding="ascii", errors="replace") as source:
        info = os.fstat(source.fileno())
        mode = stat.S_IMODE(info.st_mode)
        problems = []
        if not stat.S_ISREG(info.st_mode):
            problems.append("it is not a regular file")
        if info.st_uid != euid:
            problems.append(f"it is owned by uid {info.st_uid}, not {euid}")
        if mode != 0o600:
            problems.append(f"its mode is {mode:04o}, not 0600")
        if info.st_nlink != 1:
            problems.append(f"it has {info.st_nlink} hard links, not 1")
        if problems:
            return None, (
                "bearer_custody",
                f"the {seat} bearer file {path} failed the custody check: " + "; ".join(problems),
            )
        token = source.read(MAX_TOKEN_CHARS).strip()
    if not re.fullmatch(rf"sbt_{re.escape(seat.lower())}_[A-Za-z0-9_-]{{43}}", token):
        return None, (
            "bearer_format",
            f"the {seat} bearer file {path} does not hold a {seat} bearer "
            f"(expected the sbt_{seat.lower()}_ form)",
        )
    return token, None


def refusal_reply(request_id, seat, cause):
    code, sentence = cause
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {
            "code": REFUSAL_CODE,
            "message": f"The {seat} simple-tools launcher refused to start: {sentence}.  "
            "No request reached the hosted server.",
            "data": {"cause": code, "seat": seat},
        },
    }


def serve_refusal(seat, cause, stdin, stdout, stderr):
    """Answer every request with the refusal until stdin closes.  Returns 3."""
    print(f"{(seat or 'unconfigured').lower()}-simple-tools: refused: {cause[1]}", file=stderr, flush=True)
    while True:
        raw = stdin.readline(MAX_FRAME + 1)
        if not raw:
            return 3
        while len(raw) > MAX_FRAME and not raw.endswith(b"\n"):
            more = stdin.readline(MAX_FRAME + 1)
            if not more:
                break
            raw = more
        try:
            message = json.loads(raw)
        except ValueError:
            if raw.strip():
                replies = [refusal_reply(None, seat, cause)]
            else:
                replies = []
        else:
            batch = message if isinstance(message, list) else [message]
            replies = [
                refusal_reply(item["id"], seat, cause)
                for item in batch
                if isinstance(item, dict) and "id" in item
            ]
        for reply in replies:
            stdout.write((json.dumps(reply) + "\n").encode())
        stdout.flush()


def plan(env, config, euid=None):
    """Return ("exec", argv, child_env, notice) or ("refuse", seat, cause, notice)."""
    seat = norm(config.get("SEAT"))
    missing = [k for k in ("SEAT", "BEARER_FILE", "PYTHON", "TRANSPORT", "HOME") if not config.get(k)]
    if missing or not SEAT_RE.fullmatch(seat):
        return ("refuse", seat or None, (
            "launcher_unconfigured",
            "this launcher copy has no installed config (" + ", ".join(missing or ["SEAT"]) + "); "
            "install it with scripts/simple_tools/stdio_launcher/install.py",
        ), None)
    refusal, notice = decide_seat(env, seat)
    if refusal:
        return ("refuse", seat, refusal, None)
    for key in ("PYTHON", "TRANSPORT"):
        if not os.path.isfile(config[key]):
            return ("refuse", seat, (
                "transport_missing", f"{key.lower()} {config[key]} does not exist",
            ), notice)
    token, refusal = read_bearer(config["BEARER_FILE"], seat, euid)
    if refusal:
        return ("refuse", seat, refusal, notice)
    child_env = {"PATH": "/usr/bin:/bin", "HOME": config["HOME"], "ASSIGNED_MCP_BEARER": token}
    return ("exec", [config["PYTHON"], "-I", config["TRANSPORT"]], child_env, notice)


def main(env=None, config=None):
    env = os.environ if env is None else env
    config = config or {
        "SEAT": SEAT, "BEARER_FILE": BEARER_FILE, "PYTHON": PYTHON,
        "TRANSPORT": TRANSPORT, "HOME": HOME,
    }
    outcome = plan(env, config)
    if outcome[3]:
        print(outcome[3], file=sys.stderr, flush=True)
    if outcome[0] == "refuse":
        return serve_refusal(outcome[1], outcome[2], sys.stdin.buffer, sys.stdout.buffer, sys.stderr)
    _, argv, child_env, _ = outcome
    try:
        os.execve(argv[0], argv, child_env)
    except OSError as error:
        seat = norm(config["SEAT"])
        return serve_refusal(seat, (
            "transport_exec", f"the transport could not start ({error.__class__.__name__})",
        ), sys.stdin.buffer, sys.stdout.buffer, sys.stderr)
    return 3  # pragma: no cover - execve does not return


if __name__ == "__main__":
    sys.exit(main())
