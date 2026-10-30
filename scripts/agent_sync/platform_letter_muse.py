"""`platform-letter` adapter for Muse Code (seat MC, platform `muse`; AFC-564, owner request Oct 29, 2026).

Muse Code takes hooks from a plugin, never from a settings file, so this adapter is a plugin bundle named
`session-letter` whose three hooks (SessionStart, UserPromptSubmit, SessionEnd) run `platform-letter hook --platform
muse`.  The library does the rest (the registry, the letter, the note, the context line);  this file holds what is
Muse's own.

    platform-letter install --platform muse [--location server --remote mac] [--check | --remove]

The bundle.  `install` writes `<home>/.local/share/platform-letter/muse/session-letter/.muse-plugin/plugin.json` (the
manifest shape of the fleet-lane-guard bundle:  capabilities.hooks entries with an id, an event, a command as an argv
array and a timeoutMs).  It does not touch Muse's own state.  The two steps that do are the owner's trust decision,
which the answer names for the person or the orchestrator who runs them:

    muse plugins install <bundle> --scope user       (first time;  later `muse plugins update session-letter`)
    muse plugins approve session-letter              prints one `approve plugin:session-letter:hook:<id>` line a hook

`muse plugins inspect session-letter` shows `status=review_needed` for each hook before the approval and
`status=trusted_enabled` after it, which is what `install --check` reads.  A change to a hook's command (the
--location or --remote flags) is a new definition and needs the approval again.  A running chat does not pick a
plugin up, so only chats started after the approval take part.

The hook command is guarded.  Muse reads exit 2 of a UserPromptSubmit plugin hook as "block this prompt" (a headless
`muse exec` ends Cancelled), and the entry point can exit 2 on its own: the server wrapper does when the current
fleet-cli release has no scripts/platform-letter, which a rollback to an older retained release leaves behind.  Any other
failure only shows "Hook failed".  So the manifest runs the entry as `/bin/sh -c '"$@" || exit 0' sh <entry> hook ...`:
stdout passes through, every failure becomes exit 0, and the ancestor walk still finds Muse through the extra `sh`.

`install --check` exits 0 only when the bundle is current, Muse's own copy of it is the same, and the hooks are
approved.  `muse plugins install` copies the bundle into a cache package (the `cache=` folder `muse plugins inspect`
names), and a bundle changed afterwards stays unapplied there, with every hook still `trusted_enabled`, until `muse
plugins update session-letter`; the check reads that copy.  Off the Mac the hook is useless without `--location server
--remote <the Mac's ssh host>` (it runs with a cleared environment, so the flags are all it has), so `install` refuses and
`--check` fails when neither the command line nor the bundle already installed names them.

Who takes a letter.  The hook runs with a cleared environment (HOME, PATH, the MUSE_* names and nothing of the
caller's AGENT_*), so the facts about the chat come from its parent, the Muse process:  its command line says what
kind of run it is, and its environment says whether a launcher started it.  A chat takes a letter when Muse was
started as `muse` or `muse resume` (or is the desktop app), with no AGENT_LAUNCHER or AGENT_LAUNCH_SEAT, no
SESSION_LETTER=0 or MUSE_LETTER=0, and no MUSE_LETTER_HELPER mark.  Every other Muse command takes none:  `muse exec`
(the Operator's wake runs, BotFleet, anything headless), `muse serve` (a host some program drives, such as the Clutch
bridge or this adapter's own rename helper), `muse plugins hook test` and the rest.  A parent that is not Muse takes
none either, so a fixture piped into the hook by hand never registers a chat.  On the Mac the command line comes from the
kernel (`muse_session_rename.kernel_args`), which keeps each argument whole: `ps` joins them with spaces, so a prompt
that opens with a subcommand word (`muse "login page redirects"`) read as the subcommand `login` and got no letter.

The title.  Muse shows a chat by its session name, which `session/rename` sets and the runtime stores in lower case
("aa: fix login", not "AA: Fix Login").  Only the process that holds the session's lease can rename it, and in Muse
1.4.14 that is the chat itself, so the title writer queues the name and starts a detached helper
(`muse_session_rename`) that applies it as soon as the chat lets go of the session.  Until then the chat keeps the
name Muse gave it, and the Operator already has the letter;  the model is told it too, in the hook's context line.
The live title is not readable from the hook, so a rename by hand during a chat is not noticed (the helper keeps a
custom name's words and adds the letter in front of them).  The helper's host runs the session's own provider and
model, so the rename does not change what `muse resume` starts the chat on.

What a letter does to a name.  The name authority (session-names.db, one per account) keeps one canonical claim for a
name, case-insensitively, and a later claim takes it over from an earlier one without an error.  A letter is held by
one chat at a time, so "aa: ..." names stay distinct while the chats are live, and a letter that comes back to a new
chat takes the name over from the old chat that last carried it.

A chat's end.  SessionEnd always arrives with the reason `other`, so it cannot tell a chat that is over from one the
owner will resume.  The registration therefore stays open (`close_reasons=()`) and the library's 36 hour idle sweep
frees the letter, which a resumed chat gets back if it is still free.

A fork.  A forked chat's first stored record names the chat it came from, and the adapter passes that as the parent,
so the registry gives the fork the pair after its parent's.

Python 3.11+, standard library only.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from typing import Any, TextIO

from . import live as L
from . import muse_session_rename as MR
from . import platform_letter as PL
from . import session_ids as SI

PLATFORM = "muse"
SEAT = "MC"
SOURCE = "muse-letter-hook"
DISPLAY = "Muse Code"
PLUGIN_ID = "session-letter"
#: The bundle's hooks:  id, Muse's event name, and the status line Muse shows while it runs (none for the quick ones).
HOOKS = (
    ("letter-start", "SessionStart", "Getting the Operator session letter"),
    ("letter-prompt", "UserPromptSubmit", ""),
    ("letter-end", "SessionEnd", ""),
)
VERSION = "1.0.0"
#: A first record names the chat a fork came from under one of these payload keys.
PARENT_KEYS = ("source_session_id", "parent_session_id")
FIRST_RECORD_BYTES = 262144
REVIVE_LIMIT = 3
CLI_NAMES = ("muse", "muse-bin")

#: Where the parent process is looked up, and which pid is the hook's parent (a test replaces both).
PROC_ROOT: str | None = None


def _ppid() -> int:
    return os.getppid()


def muse_process() -> MR.Proc | None:
    """The Muse process this hook runs under, or None when its parent is not Muse."""
    return MR.muse_ancestor(_ppid(), PROC_ROOT)


# --------------------------------------------------------------------------------------------
# What the spec asks
# --------------------------------------------------------------------------------------------


def muse_kind(payload: Mapping[str, Any], env: Mapping[str, str]) -> str:
    """ "chat" for an interactive Muse chat, "headless" for everything else (see the module docstring).  The hook's own
    environment is cleared, so the launcher marks and the opt-outs are read from the Muse process's."""
    proc = muse_process()
    if proc is None:
        # Not run by Muse (or by a wrapper deeper than ANCESTOR_DEPTH):  a fixture, never a chat.  The one exception
        # is the end of a chat this host already knows:  Muse may be gone before the hook has read its parent.
        ended = payload.get("hook_event_name") == "SessionEnd" and os.path.exists(
            PL.note_path(env, SEAT, _session_id(payload, env))
        )
        return "chat" if ended else "headless"
    pe = proc.env
    if pe.get(MR.HELPER_MARK) or pe.get("AGENT_LAUNCHER") or pe.get("AGENT_LAUNCH_SEAT"):
        return "headless"
    if pe.get("SESSION_LETTER") == "0" or pe.get("MUSE_LETTER") == "0":
        return "headless"
    if MR.subcommand(proc.argv) not in ("", "resume"):
        return "headless"
    # A sub-agent's session lives inside its parent chat's folder.  The plugin hooks may fire for it too:  it is not a chat.
    return "subagent" if MR.is_subagent(_data_root(env), _session_id(payload, env)) else "chat"


def _data_root(env: Mapping[str, str]) -> str:
    return MR.data_root(
        str(env.get("HOME") or os.path.expanduser("~")), str(env.get("XDG_DATA_HOME") or "")
    )


def _session_id(payload: Mapping[str, Any], env: Mapping[str, str]) -> str:
    return PL.either(PL.from_payload("session_id"), PL.from_env("MUSE_SESSION_ID"))(payload, env)


def muse_parent(payload: Mapping[str, Any], env: Mapping[str, str]) -> str:
    """The native id of the chat this one was forked from, read from the first record of its stored log, or ""."""
    sid = _session_id(payload, env)
    sdir = MR.find_session_dir(_data_root(env), sid)
    if sdir is None:
        return ""
    try:
        with open(os.path.join(sdir, "session.jsonl"), "rb") as fh:
            first = json.loads(fh.readline(FIRST_RECORD_BYTES))
    except (OSError, ValueError):
        return ""
    body = first.get("payload") if isinstance(first, dict) else None
    if not isinstance(body, dict):
        return ""
    for key in PARENT_KEYS:
        try:
            parent = SI.check(body.get(key), "the parent session id")
        except ValueError:
            continue
        if parent != sid:
            return parent
    return ""


def _note_title(env: Mapping[str, str], sid: str) -> str:
    try:
        with open(PL.note_path(env, SEAT, sid), encoding="utf-8") as fh:
            note = json.load(fh)
    except (OSError, ValueError):
        return ""
    title = note.get("title") if isinstance(note, dict) else ""
    return title if isinstance(title, str) else ""


def muse_stored_title(payload: Mapping[str, Any], env: Mapping[str, str]) -> str:
    """The words a resumed or forked chat carries:  the title this host's note has for it, else for the chat it was
    forked from.  The hook payload has no title, and the stored name is lower case, so the note is the better source."""
    sid = _session_id(payload, env)
    return _note_title(env, sid) or _note_title(env, muse_parent(payload, env))


# --------------------------------------------------------------------------------------------
# The title writer:  queue the name, start the helper
# --------------------------------------------------------------------------------------------


def spool_dir(env: Mapping[str, str]) -> str:
    return os.path.join(L.state_root(env), SEAT, "renames")


def spool_path(env: Mapping[str, str], sid: str) -> str:
    return os.path.join(spool_dir(env), sid + ".json")


def _cli(proc: MR.Proc | None, home: str, env: Mapping[str, str]) -> str:
    """The Muse command line program the helper runs:  the executable of the chat's own Muse process when it is the
    CLI (the build that holds the session), else the user's launcher."""
    if (
        proc is not None
        and os.path.basename(proc.exe).startswith(CLI_NAMES)
        and os.access(proc.exe, os.X_OK)
    ):
        return proc.exe
    for candidate in (
        os.path.join(home, ".local", "bin", "muse"),
        shutil.which("muse", path=env.get("PATH")) or "",
    ):
        if candidate and os.access(candidate, os.X_OK):
            return candidate
    return ""


def muse_rename(req: PL.TitleRequest) -> bool:
    """Queue `req.title` for the helper.  True when the chat will carry it (it already does, or the helper has it);
    False when it could not be queued, which the library tries again on a later prompt."""
    env, sid = req.env, req.session_id
    home = str(env.get("HOME") or os.path.expanduser("~"))
    path = spool_path(env, sid)
    rec = MR.read_record(path)
    applied = [str(a) for a in rec.get("applied") or []]
    if (
        rec.get("state") == "done"
        and rec.get("name") == req.title
        and applied
        and applied[-1].lower() == req.title.lower()
    ):
        return True  # the stored name is already this title
    proc = muse_process()
    now = time.time()
    running = MR.worker_alive(rec, PROC_ROOT)
    rec.update(
        session_id=sid,
        name=req.title,
        label=req.label,
        applied=applied,
        cwd=str(req.data.get("cwd") or rec.get("cwd") or ""),
        home=home,
        exe=_cli(proc, home, env),
        xdg_data_home=str(env.get("XDG_DATA_HOME") or ""),
        holder_pid=proc.pid if proc else 0,
        queued_at=now,
        state="waiting" if rec.get("state") == "waiting" else "queued",
        attempts=0,
        error="",
        worker_pid=int(rec.get("worker_pid") or 0) if running else 0,
    )
    # The record first, the helper second: a helper that reads the file before it is written finds no job (or the
    # last one's, done) and leaves as `stale`, and the library would never ask for this title again.
    queued = MR.write_record(path, rec)
    pid = int(rec.get("worker_pid") or 0)
    if queued and not running:
        pid = MR.start_worker(path, home, env)
        if pid:
            _claim(path, pid)
    if pid:
        revive(env, home, skip=sid)
    return bool(queued and pid)


def _claim(path: str, pid: int, stale: int = 0) -> None:
    """Name the helper in the record, unless it has already done so (it writes its own pid first thing, and may be
    finished by now).  `stale` is the pid of a helper that is gone, which the record may still name."""
    rec = MR.read_record(path)
    if rec.get("state") in ("queued", "waiting") and int(rec.get("worker_pid") or 0) in (0, stale):
        rec["worker_pid"] = pid
        MR.write_record(path, rec)


def revive(env: Mapping[str, str], home: str, *, skip: str = "") -> int:
    """Start a helper for each queued job whose helper is gone (a reboot, a killed process), at most REVIVE_LIMIT.
    Returns how many were started."""
    started = 0
    try:
        names = sorted(os.listdir(spool_dir(env)))
    except OSError:
        return 0
    for name in names:
        if started >= REVIVE_LIMIT:
            break
        if not name.endswith(".json") or name[:-5] == skip:
            continue
        path = os.path.join(spool_dir(env), name)
        rec = MR.read_record(path)
        if rec.get("state") not in ("queued", "waiting") or MR.worker_alive(rec, PROC_ROOT):
            continue
        if time.time() - float(rec.get("queued_at") or 0) > MR.WAIT_MAX:
            continue
        pid = MR.start_worker(path, str(rec.get("home") or home), env)
        if pid:
            _claim(path, pid, stale=int(rec.get("worker_pid") or 0))
            started += 1
    return started


SPEC = PL.PlatformSpec(
    platform=PLATFORM,
    seat=SEAT,
    source=SOURCE,
    display=DISPLAY,
    session_id=_session_id,
    parent_id=muse_parent,
    kind=muse_kind,
    stored_title=muse_stored_title,
    close_reasons=(),
    title_writer=muse_rename,
)


# --------------------------------------------------------------------------------------------
# The bundle (`platform-letter install --platform muse`)
# --------------------------------------------------------------------------------------------


def bundle_dir(home: str) -> str:
    return os.path.join(home, ".local", "share", "platform-letter", PLATFORM, PLUGIN_ID)


def manifest_path(home: str) -> str:
    return os.path.join(bundle_dir(home), ".muse-plugin", "plugin.json")


#: The hook runs under `sh`: Muse reads exit 2 of a UserPromptSubmit hook as "block this prompt", and the entry point can
#: exit 2 by itself (see the module docstring).  `sh` keeps stdout and turns every failure into exit 0.
GUARD = ["/bin/sh", "-c", '"$@" || exit 0', "sh"]


def hook_argv(home: str, where: str = "", remote: str = "") -> list[str]:
    """The hook command as the argv array Muse takes: the library's own command line, split, under GUARD."""
    return [*GUARD, *shlex.split(PL.command_for(home, PLATFORM, where, remote))]


def render_manifest(argv: list[str]) -> str:
    """The plugin manifest, in the shape of the fleet-lane-guard bundle.  Muse rejects a `matcher` on a plugin hook,
    and runs it with a cleared environment, so the command is absolute."""
    hooks = []
    for hook_id, event, status in HOOKS:
        entry: dict[str, Any] = {
            "id": hook_id,
            "event": event,
            "command": list(argv),
            "timeoutMs": PL.HOOK_TIMEOUTS[event] * 1000,
        }
        if status:
            entry["statusMessage"] = status
        hooks.append(entry)
    doc = {
        "schemaVersion": 1,
        "name": PLUGIN_ID,
        "displayName": "Operator Session Letter",
        "version": VERSION,
        "description": "Gives each interactive Muse Code chat its Operator session letter (a pair, AA to ZZ) and "
        'queues the session name "letter: Title".',
        "compat": {"source": "native", "manifestDir": ".muse-plugin"},
        "capabilities": {
            "skills": [],
            "commands": [],
            "hooks": hooks,
            "mcpServers": [],
            "reminders": [],
        },
    }
    return json.dumps(doc, indent=2) + "\n"


def _installed_flags(path: str) -> tuple[str, str]:
    """(--location, --remote) of the command already in the bundle, so a plain re-install keeps them."""
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
        words = [str(w) for w in doc["capabilities"]["hooks"][0]["command"]]
    except (OSError, ValueError, KeyError, IndexError, TypeError):
        return "", ""
    return PL._flag(words, "--location"), PL._flag(words, "--remote")


def find_muse(home: str, env: Mapping[str, str]) -> str:
    for candidate in (
        shutil.which("muse", path=env.get("PATH")) or "",
        os.path.join(home, ".local", "bin", "muse"),
    ):
        if candidate and os.access(candidate, os.X_OK):
            return candidate
    return ""


def muse_status(
    home: str, env: Mapping[str, str], runner: Callable[..., Any] | None = None
) -> dict[str, Any]:
    """What Muse says about the plugin (`muse plugins inspect`, which changes nothing):  installed, and the review
    status of each hook and the folder of the copy of the bundle Muse runs (`cache`).  Run from the home folder, which
    Muse trusts, with Muse's updater off."""
    run = runner or subprocess.run
    exe = find_muse(home, env)
    if not exe:
        return {"error": "no muse on this host"}
    run_env = {
        "HOME": home,
        "PATH": str(env.get("PATH") or "/usr/local/bin:/usr/bin:/bin"),
        "LANG": "C.UTF-8",
        "MUSE_NO_AUTO_UPDATE": "1",
        "MUSE_LOGIN": "0",
    }
    try:
        proc = run(
            [exe, "plugins", "inspect", PLUGIN_ID],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=home,
            env=run_env,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"error": "muse did not answer (%s)" % type(exc).__name__}
    if proc.returncode != 0:
        return {"installed": False, "approved": False}
    caps: dict[str, str] = {}
    cache = ""
    for line in str(proc.stdout or "").splitlines():
        cells = line.split("\t")
        if len(cells) >= 3 and cells[0] == "runtime-capability" and cells[2].startswith("status="):
            caps[cells[1].rsplit(":", 1)[-1]] = cells[2][len("status=") :]
        elif len(cells) >= 3 and cells[0] == PLUGIN_ID:
            found = re.search(r"(?:^| )cache=(.+)$", cells[2])
            cache = found.group(1).strip() if found else cache
    wanted = [h[0] for h in HOOKS]
    approved = all(caps.get(h) == "trusted_enabled" for h in wanted)
    return {"installed": True, "approved": approved, "hooks": caps, "cache": cache}


def _on_mac() -> bool:
    return sys.platform == "darwin"


def location_problem(where: str, remote: str) -> str:
    """Why a hook installed with these flags could never register on this host, or "".  The hook runs with a cleared
    environment, so the flags are all it has: off the Mac it must be told to reach the registry over ssh."""
    if not where and not _on_mac():
        return (
            "this host is not the Mac: pass --location server --remote <the ssh host of the Mac> "
            "(or install over a bundle that has them), else the hook never registers a chat"
        )
    if where == "server" and not remote:
        return "--location server needs --remote <the ssh host of the Mac>, else the hook never registers a chat"
    return ""


def cached_manifest(status: Mapping[str, Any], home: str, env: Mapping[str, str]) -> str | None:
    """The manifest of the copy of the bundle Muse runs (the cache package `muse plugins inspect` names), or None when
    Muse did not name one or it cannot be read."""
    cache = str(status.get("cache") or "")
    if not cache:
        return None
    if not os.path.isabs(cache):
        cache = os.path.join(_data_root_of(home, env), cache)
    try:
        with open(os.path.join(cache, ".muse-plugin", "plugin.json"), encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return None


def _data_root_of(home: str, env: Mapping[str, str]) -> str:
    return MR.data_root(home, str(env.get("XDG_DATA_HOME") or ""))


def _synced(status: Mapping[str, Any], home: str, env: Mapping[str, str], body: str) -> bool | None:
    """Whether Muse's own copy of the bundle is `body`: None when there is no copy to look at."""
    cached = cached_manifest(status, home, env)
    return None if cached is None else cached == body


def pending_steps(home: str, status: Mapping[str, Any], synced: bool | None) -> list[str]:
    """The `muse plugins` steps a current bundle still needs, from what Muse says: install it, or update the copy Muse
    runs, then approve.  Empty when Muse is all done, and when there is no Muse to ask."""
    if status.get("error"):
        return []
    steps = []
    if not status.get("installed"):
        steps.append("muse plugins install %s --scope user" % shlex.quote(bundle_dir(home)))
    elif synced is not True:
        steps.append("muse plugins update %s" % PLUGIN_ID)
    if steps or not status.get("approved"):
        steps.append("muse plugins approve %s" % PLUGIN_ID)
    return steps


def install(opts: PL.InstallOptions, env: Mapping[str, str], stdout: TextIO) -> int:
    """Write (or check, or remove) the bundle.  Idempotent:  a bundle that is current is left alone, and one that
    changes is backed up first (next to the bundle, 0600, so the backup never ships inside the plugin)."""
    home = str(env.get("HOME") or os.path.expanduser("~"))
    path = manifest_path(home)
    if opts.remote and not PL.DEST_RE.fullmatch(opts.remote):
        return PL._emit(stdout, {"error": "--remote must name an ssh destination"}, 2)
    if opts.location and opts.location not in PL.LOCATIONS:
        return PL._emit(stdout, {"error": "--location must be mac or server"}, 2)
    kept_where, kept_remote = _installed_flags(path)
    where, remote = opts.location or kept_where, opts.remote or kept_remote
    argv = hook_argv(home, where, remote)
    body = render_manifest(argv)
    try:
        with open(path, encoding="utf-8") as fh:
            have = fh.read()
    except OSError:
        have = None
    problem = "" if opts.remove else location_problem(where, remote)
    if opts.check:
        status = muse_status(home, env)
        synced = _synced(status, home, env, body)
        answer: dict[str, Any] = {
            "platform": PLATFORM,
            "path": path,
            "installed": {"bundle": have is not None, "muse": bool(status.get("installed"))},
            "current": have == body,
            "cached": synced,
            "muse": status,
        }
        if problem:
            answer["problem"] = problem
        steps = pending_steps(home, status, synced) if have == body else []
        if steps:
            answer["steps"] = steps
        ok = have == body and status.get("approved") and synced and not problem
        return PL._emit(stdout, answer, 0 if ok else 1)
    if opts.remove:
        if have is None:
            return PL._emit(stdout, {"platform": PLATFORM, "changed": [], "path": path}, 0)
        backup = _backup(path, have)
        shutil.rmtree(bundle_dir(home))
        steps = ["muse plugins remove %s" % PLUGIN_ID]
        answer = {
            "platform": PLATFORM,
            "changed": ["bundle"],
            "path": path,
            "backup": backup,
            "trust": steps[0],
            "steps": steps,
        }
        return PL._emit(stdout, answer, 0)
    if problem:
        return PL._emit(stdout, {"error": problem}, 2)
    if have == body:
        status = muse_status(home, env)
        answer = {"platform": PLATFORM, "changed": [], "path": path}
        steps = pending_steps(home, status, _synced(status, home, env, body))
        if steps:
            answer.update(
                trust=" then ".join(steps), steps=steps
            )  # a step the last install named is still to be run
        return PL._emit(stdout, answer, 0)
    backup = _backup(path, have) if have is not None else ""
    os.makedirs(os.path.dirname(path), mode=0o755, exist_ok=True)
    tmp = path + ".letter.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(body)
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)
    known = muse_status(home, env).get("installed")
    steps = [
        "muse plugins update %s" % PLUGIN_ID
        if known
        else "muse plugins install %s --scope user" % shlex.quote(bundle_dir(home)),
        "muse plugins approve %s" % PLUGIN_ID,
    ]
    answer = {
        "platform": PLATFORM,
        "changed": ["bundle"],
        "path": path,
        "backup": backup,
        "command": argv,
        "trust": " then ".join(steps),
        "steps": steps,
    }
    return PL._emit(stdout, answer, 0)


def _backup(path: str, body: str) -> str:
    """A dated copy of the manifest beside the bundle folder (`session-letter.bak-letter-<stamp>.json`, 0600)."""
    from datetime import UTC, datetime

    parent = os.path.dirname(os.path.dirname(os.path.dirname(path)))
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    for n in range(1000):
        name = os.path.join(
            parent, "%s.bak-letter-%s%s.json" % (PLUGIN_ID, stamp, "-%d" % n if n else "")
        )
        try:
            PL._write_private(name, body.encode("utf-8"), 0o600, exclusive=True)
        except FileExistsError:
            continue
        return name
    raise OSError("no free backup name for %s" % path)
