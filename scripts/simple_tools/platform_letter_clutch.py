"""Clutch (DeepSeek Harness, `dsh`; seat CLUTCH, Mac only) on `platform-letter`: an Operator session letter on
every interactive Harness session.  The library (`platform_letter`) does the registering, the note, the
retries, the registry transport and the install; this file is what is the Harness's own.

What the Harness gives a hook.  DeepSeek Harness runs command hooks through its first-party Claude Code hook
bridge (`@deepseek-ai/dsh-hooks-claude-code`), which runs the supported command-hook subset of a Claude Code
hook config — a `hooks.json`, or a settings file's `hooks` key — on the harness's own interception points,
with Claude Code shaped payloads on stdin.  Of the points the bridge maps, two are this adapter's:
`SessionStart` (the harness's `agent/session-start`, an emit point: a hook's JSON `additionalContext` is
injected into the new session, and it cannot block) and `UserPromptSubmit` (an accepted prompt, which can
also add context).  `SessionEnd` is one of the events the bridge does not support, so no end event is hooked:
a registration is never closed by a hook, and the library's 36 hour idle sweep frees the letter of a session
nobody came back to, as on the platforms whose end event says nothing final.  The bridge consumes a hook's
`additionalContext` only: plain stdout context, `sessionTitle` and the rest of a Claude Code answer are not
supported, so this adapter, like Antigravity's, registers the session and tells it its letter, and does not
touch the title (`title_writer` and `title_in_output` are off; the spec's output is the library's `hook`
style, whose `additionalContext` is the field the bridge reads).

The bridge's config is process-level: the file is parsed once when the Harness process loads, a relative
`configPath` resolves against the launch directory, and there is no per-session hook config discovery yet.
So the hooks file this adapter installs is the seat's own, `~/.dsh/hooks-letter.json`, and the step that
points the bridge at it — a bare insert in the profile's `cordis.patch.yml` naming
`@deepseek-ai/dsh-hooks-claude-code` with that `configPath` — is a mount the installer reports and never
writes, the way the Codex adapter reports its trust step: it is the profile owner's edit, in a file other
plugins also live in.  Until the mount exists the installed hooks are inert and no session takes a letter;
every install answer carries the mount in its `trust` field rather than reporting a bare finished install.  Only
shell-form `type: "command"` hooks run through the bridge, which is the only form this adapter installs.

Who takes a letter.  A session of the seat's own Harness profile, from a payload that carries a session id.
A `dsh` run a launcher started never does (the library checks AGENT_LAUNCHER and AGENT_LAUNCH_SEAT — the
Harness seats BotFleet drives are launchers' sessions, not this seat's chats), and a headless run (a
one-shot or exec invocation, which the payload's entrypoint or mode names) takes none.  A sub-agent session
takes a letter only as a sub-thread, when the payload names the session that started it.

Installing.  `platform-letter install --platform clutch [--check | --remove]` adds this adapter's entries to
`~/.dsh/hooks-letter.json`, in the Claude Code shape (`hooks` at the top, one matcher-less group per event)
the bridge parses.  A profile
that already loads the Claude letter hooks from `~/.claude/settings.json` through the same bridge must not
also load this file: the two would register one session twice, once per platform, which is the split the
namespaces exist to prevent — the seat's profile loads one hooks file for letters, this one.

Python 3.11+, standard library only.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

from . import platform_letter as PL

PLATFORM = "clutch"
SEAT = "CLUTCH"
SOURCE = "clutch-letter-hook"
DISPLAY = "Clutch"

ENV_OFF = "CLUTCH_LETTER"
HOOK_FILE = os.path.join(".dsh", "hooks-letter.json")
HOOK_TIMEOUTS = {"SessionStart": 20, "UserPromptSubmit": 15}
EVENTS = {"SessionStart": "start", "UserPromptSubmit": "prompt"}
#: The bridge has no SessionEnd, so no end closes a registration; the 36 hour sweep frees idle letters.
CLOSE_REASONS: tuple[str, ...] = ()

#: How the bridge is pointed at this adapter's hooks file: a bare insert in the profile's cordis patch,
#: reported by every install answer and never written by the installer.
MOUNT = (
    "mount the Claude Code hook bridge in the seat profile's cordis.patch.yml as a bare insert: "
    "id letter-hooks-claude-code, package "
    "@deepseek-ai/dsh-hooks-claude-code, with configPath ~/.dsh/hooks-letter.json; until then the "
    "installed hooks are inert"
)


def clutch_kind(payload: Mapping[str, Any], env: Mapping[str, str]) -> str:
    """ "chat", "subagent" or "headless" for a Harness hook payload, in the Claude Code dialect the bridge
    speaks: an exec or one-shot entrypoint is headless, and a payload that names the session that started
    it (a sub-agent's own fields) is a sub-agent run."""
    entrypoint = str(payload.get("entrypoint") or payload.get("mode") or "").strip().lower()
    if entrypoint in ("exec", "headless", "print", "-p", "sdk"):
        return "headless"
    if payload.get("parent_session_id") or payload.get("agent_id"):
        return "subagent"
    return "chat"


SPEC = PL.PlatformSpec(
    platform=PLATFORM,
    seat=SEAT,
    source=SOURCE,
    display=DISPLAY,
    session_id=PL.from_payload("session_id"),
    parent_id=PL.from_payload("parent_session_id"),
    kind=clutch_kind,
    events=EVENTS,
    close_reasons=CLOSE_REASONS,
    locations=("mac",),
    env_off=ENV_OFF,
)

#: The shared JSON installer writes the entries; its answer carries MOUNT as its `trust` step, the part
#: of the install a person (or the orchestrator) performs, exactly as Codex's re-trust step is carried.
INSTALL = PL.JsonHookInstaller(
    path=HOOK_FILE,
    events=HOOK_TIMEOUTS,
    trust=MOUNT,
)
