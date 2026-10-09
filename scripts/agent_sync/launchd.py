"""`agent-sync daemon install` and `uninstall`:  the LaunchAgent plist and the sample config.

They write files only.  They never run launchctl:  they print the bootstrap or bootout command
for the person (or orchestrator) doing the install.  Every path is derived from HOME, so tests
run against a temporary home.  The sample config names a seat for every credential file found
by checking the known roster's file names one by one (a stat, never a glob and never a read),
and only CLAUDE is enabled.

Python 3.11+, standard library only.
"""
from __future__ import annotations

import os
import plistlib
import sys
from typing import Mapping, TextIO

from . import live as L
from . import zulip as Z

LABEL = "com.jay.agent-sync-listener"
# Fleet seats with a reader on this Mac (they can be enabled), then bots that have none.
READER_SEATS = ["CLAUDE", "CODEX", "AG", "CURSOR", "GROK", "CLUTCH", "FX", "MM", "MC"]  # the Mac seats (partition "mac")
NO_READER = ["BF-BUILDER", "BF-COMPILER", "BF-DEPLOYER", "BF-DESIGNER", "BF-FIXER", "BF-HOUSEKEEPER",
             "BF-MONITOR", "BF-ORACLE", "BF-PLUMBER", "BF-PUBLISHER", "MA", "JET", "ECHO", "INSTINCT", "GROK-WEB"]
DEFAULT_PYTHON = "/opt/homebrew/bin/python3"


def plist_path(home: str) -> str:
    return os.path.join(home, "Library", "LaunchAgents", LABEL + ".plist")


def program_path(home: str) -> str:
    return os.path.join(home, ".local", "bin", "agent-sync")


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
        "ProcessType": "Background",
        "WorkingDirectory": home,
        # AGENT_SYNC_INSTANCE pins this listener to the mac side of the seat partition:  a config
        # that says daemon.instance = "server" is refused here.
        "EnvironmentVariables": {"HOME": home, "LANG": "en_US.UTF-8", "AGENT_SYNC_INSTANCE": "mac",
                                 "PATH": "%s/.local/bin:/opt/homebrew/bin:/usr/bin:/bin" % home},
        "StandardOutPath": os.path.join(logs, "launchd.out"),
        "StandardErrorPath": os.path.join(logs, "launchd.err"),
    }


def sample_config(secrets_dir: str) -> str:
    """The sample listener.toml.  Only CLAUDE is enabled; every other seat whose credential file
    is present is listed commented out."""
    def present(seat: str) -> bool:
        return os.path.isfile(os.path.join(secrets_dir, Z.credential_file_name(seat)))

    lines = [
        "# agent-sync listener config.  Local to this Mac; it holds no secrets.",
        "# Design: docs/protocols/agent-sync-listener.md in AI-Fleet-Coordinator.",
        "# Changes take effect on `agent-sync daemon reload` (SIGHUP) or a restart.",
        "",
        "[daemon]",
        "# The Mac instance holds the Mac seats (owner decision, Thu, Oct 8); the cloud seats (Grok Bots, MA, Jet,",
        "# Instinct, Echo, Grok Web) run on the server instance.  docs/protocols/agent-sync-partition.toml says which instance holds which",
        "# seat and fails closed:  a listener refuses to start with any seat it does not give to this instance.",
        'instance = "mac"',
        "owner_user_id = 0                 # pinned by `agent-sync daemon init`, never derived at runtime",
        "eligible_user_ids = []            # fleet seat bots, pinned by `agent-sync daemon init`",
        "# Owner priority needs the owner's user id AND one of these clients (human Zulip apps).  A post",
        "# made with the owner's key from any other client is treated as not the owner and flagged.",
        "# Manual test 5 confirms the exact client names.",
        'owner_clients = ["website", "ZulipMobile", "ZulipFlutter", "ZulipElectron", "ZulipDesktop"]',
        "stale_after_minutes = 120",
        "coalesce_seconds = 20",
        "coalesce_max_seconds = 90",
        "owner_coalesce_seconds = 5",
        'presence_topics = [["agent-sync", "roll call"], ["agent-sync", "fleet"], ["builds", "gates"], ["alerts", "*"]]',
        "",
        "[platform.claude-code]",
        'seat = "CLAUDE"                   # hooks and the CLI use this when AGENT_SEAT is unset',
        "rewake_verified = false           # true only after manual test 1; until then live delivery is headlines only",
        "",
        "[seat.CLAUDE]",
        'bot = "Claude"                    # reads %s (moderator or member role only)%s'
        % (os.path.join(secrets_dir, Z.credential_file_name("CLAUDE")), "" if present("CLAUDE") else "; NOT FOUND"),
        'wake = "claude"                   # runs only after `agent-sync daemon test-wake --seat CLAUDE --run`, then --pin',
        'model = "sonnet"',
        "budget = { wakes_per_hour = 6, wakes_per_day = 40, per_topic_per_hour = 2, owner_per_day = 20, "
        "owner_per_topic_per_hour = 6, usd_per_day = 2.0, board_per_day = 0 }",
        "live = { per_hour = 6, per_day = 30, per_topic_minutes = 5, owner_per_day = 20, loop_turns = 3 }",
    ]
    listed = [seat for seat in READER_SEATS[1:] if present(seat)]
    if listed:
        lines += ["", "# The other Mac seats (the partition gives them to \"mac\").  Uncomment to capture their",
                  "# @-mentions and DMs into their inboxes (owner, Thu, Oct 8:  get the listeners all active)."]
    for seat in listed:
        stem = Z.credential_file_name(seat)[: -len("-zuliprc")]
        lines += ["", "# [seat.%s]" % seat, '# bot = "%s"' % stem, '# wake = "inbox"']
    others = [s for s in NO_READER if present(s)]
    if others:
        lines += ["", "# Bots with no reader on this Mac (left out on purpose): %s" % ", ".join(others)]
    return "\n".join(lines) + "\n"


def write_private(path: str, text: str, mode: int = 0o600) -> None:
    L.private_dir(os.path.dirname(path))
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.chmod(path, mode)


def install(env: Mapping[str, str], home: str, out: TextIO, *, dry_run: bool = False, python: str | None = None,
            program: str | None = None) -> int:
    root = os.path.join(home, ".agent-sync")  # what the LaunchAgent's daemon uses (the plist sets no state dir)
    secrets_dir = os.path.expanduser(env.get(Z.ENV_SECRETS_DIR) or os.path.join(home, ".secrets", "Zulip"))
    python = choose_python(python)
    program = program or program_path(home)
    target = plist_path(home)
    data = plistlib.dumps(plist_dict(home, python, program, root), sort_keys=False)
    config_path = os.path.join(root, L.CONFIG_NAME)
    say = (lambda text: out.write("would: " + text + "\n")) if dry_run else (lambda text: out.write(text + "\n"))
    if not os.path.exists(program):
        out.write("note: %s does not exist yet; run scripts/agent_sync/install.sh first\n" % program)
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
        say("write the sample config %s (mode 600; only CLAUDE enabled)" % config_path)
        if not dry_run:
            write_private(config_path, sample_config(secrets_dir))
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
    out.write("Before wakes can run:  agent-sync daemon init, then agent-sync daemon test-wake --seat CLAUDE --run, "
              "read the result, then agent-sync daemon test-wake --seat CLAUDE --pin\n")
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
    out.write("State in ~/.agent-sync is kept.\n")
    if dry_run:
        out.write("dry run: nothing was changed\n")
    return 0
