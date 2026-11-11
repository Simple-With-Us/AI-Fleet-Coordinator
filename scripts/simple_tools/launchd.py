"""`simple-chat daemon install` and `uninstall`:  the LaunchAgent plist and the sample config.

They write files only.  They never run launchctl:  they print the bootstrap or bootout command
for the person (or orchestrator) doing the install.  Every path is derived from HOME, so tests
run against a temporary home.  The sample config holds no seat:  there is one listener, on the
server, and it holds every seat (owner, Tue, Nov 10).  It names the bots whose credential is
found by checking the known roster's file names one by one (a stat, never a glob and never a
read).

Python 3.11+, standard library only.
"""

from __future__ import annotations

import os
import plistlib
import sys
from collections.abc import Mapping
from typing import TextIO

from . import live as L
from . import zulip as Z

LABEL = "com.jay.operator"
# Fleet seats with a reader on this Mac (the agent seats the partition gives to "mac"), then bots
# that have none.  Since the one-listener cutover (owner, Tue, Nov 10) the server listener holds
# every seat, so the Mac has no reader seat and every bot is in NO_READER.
READER_SEATS: list[str] = []
NO_READER = [
    "CLAUDE",
    "CODEX",
    "AG",
    "CURSOR",
    "GROK",
    "CLUTCH",
    "FX",
    "MM",
    "MC",
    "OPENCODE",
    "ZC",
    "KIMI",
    "AMP",
    "BF-BUILDER",
    "BF-COMPILER",
    "BF-DEPLOYER",
    "BF-DESIGNER",
    "BF-ENGINEER",
    "BF-TRADER",
    "BF-FIXER",
    "BF-HOUSEKEEPER",
    "BF-MONITOR",
    "BF-ORACLE",
    "BF-PLUMBER",
    "BF-PUBLISHER",
    "MA",
    "JET",
    "ECHO",
    "WISE",
    "GROK-WEB",
]
DEFAULT_PYTHON = "/opt/homebrew/bin/python3"


def plist_path(home: str) -> str:
    return os.path.join(home, "Library", "LaunchAgents", LABEL + ".plist")


def program_path(home: str) -> str:
    return os.path.join(home, ".local", "bin", "simple-chat")


def choose_python(explicit: str | None) -> str:
    """An absolute python 3.11+:  under launchd `/usr/bin/env python3` finds the system 3.9."""
    if explicit:
        return explicit
    if os.access(DEFAULT_PYTHON, os.X_OK):
        return DEFAULT_PYTHON
    return sys.executable


def plist_dict(home: str, python: str, program: str, root: str) -> dict:
    logs = L.logs_dir(root)
    return {
        "Label": LABEL,
        "ProgramArguments": [python, program, "daemon", "run"],
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 30,
        # Not "Background":  under a machine-wide CPU storm (load average over 300, Fri, Oct 9) launchd started
        # the listener in the background band and it got 0.02 CPU seconds in six minutes, so it never came up.
        # Standard starts at once under the same load.
        "ProcessType": "Standard",
        "WorkingDirectory": home,
        # SIMPLE_TOOLS_INSTANCE pins this listener to the mac side of the seat partition:  a config
        # that says daemon.instance = "server" is refused here.
        "EnvironmentVariables": {
            "HOME": home,
            "LANG": "en_US.UTF-8",
            "SIMPLE_TOOLS_INSTANCE": "mac",
            "PATH": "%s/.local/bin:/opt/homebrew/bin:/usr/bin:/bin" % home,
        },
        "StandardOutPath": os.path.join(logs, "launchd.out"),
        "StandardErrorPath": os.path.join(logs, "launchd.err"),
    }


def sample_config(secrets_dir: str | None, env: Mapping[str, str] | None = None) -> str:
    """The sample listener.toml.  It holds no [seat.*] table (the server listener holds every seat,
    owner, Tue, Nov 10);  every bot whose credential is present (its three Infisical /zulip names,
    or its file in the explicit SIMPLE_TOOLS_SECRETS_DIR override) is named in a comment."""
    seats_in_infisical = frozenset() if secrets_dir else Z.infisical_seats(env)

    def present(seat: str) -> bool:
        if secrets_dir:
            return os.path.isfile(os.path.join(secrets_dir, Z.credential_file_name(seat)))
        return seat in seats_in_infisical

    lines = [
        "# simple-chat listener config.  Local to this Mac; it holds no secrets.",
        "# Design: docs/protocols/simple-tools-listener.md in AI-Fleet-Coordinator.",
        "# Changes take effect on `simple-chat daemon reload` (SIGHUP) or a restart.",
        "",
        "[daemon]",
        "# One listener, on the server, holds every seat (owner, Tue, Nov 10:  \"Only should have one and the server's",
        "# listener and router can send a signal to a Mac based wake process if necessary for wake delivery\"), so this",
        "# file holds no [seat.*] table.  The CLI and the plugin hooks still read [daemon] and [platform.*] here.",
        "# docs/protocols/simple-tools-partition.toml says which instance holds which seat and fails closed:  a",
        "# listener refuses to start with any seat it does not give to this instance.",
        'instance = "mac"',
        "# Sender eligibility for migrated BotFleet outgoing bots.  This does not hold a queue:",
        "# the partition gives every BF seat to the server (owner, Fri, Nov 6).",
        'outgoing_webhook_seats = ["BF-COMPILER", "BF-DEPLOYER", "BF-DESIGNER", "BF-DIRECTOR", "BF-ENGINEER", "BF-FIXER", "BF-HOUSEKEEPER", "BF-MONITOR", "BF-ORACLE", "BF-PLUMBER", "BF-PUBLISHER", "BF-TRADER"]',
        "owner_user_id = 0                 # pinned by `simple-chat daemon init`, never derived at runtime",
        "# The Operator service bot's own Zulip user id, pinned by hand:  a production user id is private",
        '# inventory, so none is committed.  Without it a seat cannot be told "the Operator opened this DM"',
        '# from "a seat opened it", and the runtime refuses by name instead of guessing.',
        "operator_user_id = 0",
        "# The Operator bot's own address, pinned by hand for the same reason.  Muster's read-only Operator",
        "# service (`simple-chat muster-service`) needs it, with the two ids, and reads nothing without it.",
        '# operator_email = ""',
        "eligible_user_ids = []            # optional extra pins:  every fleet bot is eligible at runtime (owner 2026-10-09)",
        "# Owner priority needs the owner's user id AND one of these clients (human Zulip apps).  A post",
        "# made with the owner's key from any other client is treated as not the owner and flagged.",
        "# Manual test 5 confirms the exact client names.",
        'owner_clients = ["website", "ZulipMobile", "ZulipFlutter", "ZulipElectron", "ZulipDesktop"]',
        "stale_after_minutes = 120",
        "coalesce_seconds = 20",
        "coalesce_max_seconds = 90",
        "owner_coalesce_seconds = 5",
        'presence_topics = [["work-in-progress", "roll call"], ["work-in-progress", "fleet"], ["builds", "gates"], ["alerts", "*"]]',
        "",
        "[platform.claude-code]",
        'seat = "CLAUDE"                   # the Claude Code platform default:  hooks and local views when AGENT_SEAT is',
        "                                  # unset, never under a launcher (AGENT_LAUNCHER), never for posting",
        "rewake_verified_cli = false       # true only after manual test 1 in a terminal;  until then live delivery is headlines only",
        "rewake_verified_desktop = false   # the same for the Desktop Code tab, which has its own flag (the old rewake_verified sets both)",
        "",
        "# No [seat.*] table:  the partition gives no agent seat to \"mac\", so this listener would refuse one.",
        "# Each seat's section lives in the server listener's config (scripts/simple_tools/server/listener.toml).",
    ]
    others = [s for s in NO_READER if present(s)]
    if others:
        lines += [
            "",
            "# Bots with no reader on this Mac (left out on purpose): %s" % ", ".join(others),
        ]
    return "\n".join(lines) + "\n"


def write_private(path: str, text: str, mode: int = 0o600) -> None:
    L.private_dir(os.path.dirname(path))
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.chmod(path, mode)


def install(
    env: Mapping[str, str],
    home: str,
    out: TextIO,
    *,
    dry_run: bool = False,
    python: str | None = None,
    program: str | None = None,
) -> int:
    root = os.path.join(
        home, ".simple-tools"
    )  # what the LaunchAgent's daemon uses (the plist sets no state dir)
    secrets_dir = Z.override_secrets_dir(env)  # no default:  seats read Infisical /zulip (AFC-359)
    python = choose_python(python)
    program = program or program_path(home)
    target = plist_path(home)
    data = plistlib.dumps(plist_dict(home, python, program, root), sort_keys=False)
    config_path = os.path.join(root, L.CONFIG_NAME)
    say = (
        (lambda text: out.write("would: " + text + "\n"))
        if dry_run
        else (lambda text: out.write(text + "\n"))
    )
    if not os.path.exists(program):
        out.write(
            "note: %s does not exist yet; run scripts/simple_tools/install.sh first\n" % program
        )
    if not os.access(python, os.X_OK):
        out.write("note: %s is not executable; pass --python PATH (3.11 or newer)\n" % python)
    logs = L.logs_dir(root)
    say("create %s (mode 700) with launchd.out and launchd.err (mode 600)" % logs)
    if not dry_run:
        L.private_dir(logs)
        for name in ("launchd.out", "launchd.err"):
            path = os.path.join(logs, name)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            os.close(fd)
            os.chmod(path, 0o600)
        L.private_dir(L.listener_dir(root))
    if os.path.exists(config_path):
        out.write("config already present, left as it is: %s\n" % config_path)
    else:
        say("write the sample config %s (mode 600; no seat:  the server listener holds every seat)" % config_path)
        if not dry_run:
            write_private(config_path, sample_config(secrets_dir, env))
    current = None
    if os.path.exists(target):
        with open(target, "rb") as fh:
            current = fh.read()
    if current == data:
        out.write("LaunchAgent already current: %s\n" % target)
    else:
        say("%s %s" % ("replace" if current is not None else "write", target))
        if not dry_run:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            tmp = target + ".tmp"
            with open(tmp, "wb") as fh:
                fh.write(data)
            os.chmod(tmp, 0o644)
            os.replace(tmp, target)
    uid = os.getuid()
    out.write("\nThis command does not run launchctl.  To load the listener:\n")
    out.write("  launchctl bootstrap gui/%d %s\n" % (uid, target))
    out.write("After an update to the checkout:  launchctl kickstart -k gui/%d/%s\n" % (uid, LABEL))
    out.write(
        "One listener, on the server, holds every seat (owner, Tue, Nov 10);  this Mac config holds none.\n"
    )
    if dry_run:
        out.write("dry run: nothing was changed\n")
    return 0


def uninstall(home: str, out: TextIO, *, dry_run: bool = False) -> int:
    target = plist_path(home)
    uid = os.getuid()
    out.write("This command does not run launchctl.  Unload the listener first:\n")
    out.write("  launchctl bootout gui/%d/%s\n" % (uid, LABEL))
    if os.path.exists(target):
        if dry_run:
            out.write("would: remove %s\n" % target)
        else:
            os.unlink(target)
            out.write("removed %s\n" % target)
    else:
        out.write("no LaunchAgent at %s\n" % target)
    out.write("State in ~/.simple-tools is kept.\n")
    if dry_run:
        out.write("dry run: nothing was changed\n")
    return 0
