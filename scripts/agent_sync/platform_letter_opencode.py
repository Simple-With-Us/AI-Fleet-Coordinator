"""OpenCode on `platform-letter` (AFC-564, owner request Oct 29, 2026): an Operator session letter, and the title
"XX: Title", on every OpenCode chat, on the Mac (OpenCode.app and its CLI) and on the server.  The library
(`platform_letter`) does the registering, the note, the retries and the title flow; this file is what is OpenCode's
own, and `opencode_letter_plugin/index.js` is the part that runs inside OpenCode.

How it fits together.
- OpenCode has no hook file.  Its extension point is a plugin, listed in the `plugins` array of
  `~/.config/opencode/opencode.json`.  The plugin watches the chat's events (created, prompt, renamed, archived,
  unarchived, deleted) and runs `platform-letter hook --platform opencode` with each as JSON on stdin:
  `{"event": "start" | "prompt" | "rename" | "end", "session_id", "parent_session_id", "title", "prompt", "source",
  "reason"}`.  The spec's output is `json`, so the hook answers with one line, `{"platform_letter": {"label", "title",
  ...}}`, and the plugin writes that `title` to the chat with OpenCode's own session update.  Everything about letters
  (the Operator's registry, the note, the ten minute retry, the touch every half hour, the secret scan of a title)
  stays in the library, exactly as for the platforms that have a hook file.
- A chat another chat started (OpenCode's `task` tool makes one, with a `parentID`) is a sub-thread: the plugin sends
  its parent's session id, the Operator gives it the pair after the parent's (parent CC, child CD), and the parent goes
  first: it is started when the plugin has not seen it, waited for when its own hook is still running, and asked once
  more when that hook did not answer.  A chat that is archived closes its registration (its pair is free again);
  unarchiving it registers it again, and deleting it closes it for good.
- A fork (`session.forked`: OpenCode copies the other chat's title, letter and all, and gives the fork no parent) is a
  main chat of its own, started with the source `fork`: its words are kept and the other chat's letter is not.
- A chat the plugin sees at its creation waits a moment (1.5 seconds) for its first prompt or for a title the client
  gives it, and is then titled `XX: New Chat` and named by its first prompt (`XX: Fix Login Page`); a prompt that comes
  during the wait registers and names it in one step.  A chat it first sees through its very first prompt (OpenCode
  loads a plugin with the first prompt of a fresh server) is registered and named by that prompt in one step.  A chat
  that already has a name keeps its words under the letter.  Only a prompt that names the chat, and then at most one
  every half hour, runs the hook, so a steady-state prompt costs nothing; a chat the Operator has no letter for is
  asked again ten minutes later, and one with a letter and no name (a first prompt of only a link) at its next prompt.
- OpenCode loads the plugin once for each directory it uses, in one process, and tells every copy the events of every
  chat; the copies share one engine on globalThis and drop an event they were told before (the durable sequence number).
- OpenCode names a chat from its first prompt by itself, with a model, about the time the hook answers.  The plugin
  never writes over a name it did not give: it reads the chat's title before it writes, and a name that arrived meanwhile
  is taken in and put under the letter (`AB: Mutex one-sentence explanation`).  A `session.renamed` event is a cue to
  look at the chat's title now, not a title to believe, because the plugin's own titles come back through it, late.

Who takes a letter.  Every OpenCode chat the plugin sees, except the ones that say they take none.  Where the plugin
runs decides what it can see.  `opencode run` and `opencode mini` use the background service (`opencode serve
--service`), one detached process that serves every client; its environment is the environment of whichever client
started it, so AGENT_LAUNCHER, AGENT_LAUNCH_SEAT, `SESSION_LETTER=0` and `OPENCODE_LETTER=0` say nothing about the
client of a chat, and the plugin ignores them there (and runs the hook without them, so that one launcher-run client
cannot switch the letters off for every later chat, nor one plain client switch them on for a launcher's).  A private
server (`serve --stdio`, which the client starts as its own child: ACP, which BotFleet uses, and `--standalone`) has
the client's environment, and there those variables apply as they do for any platform: a launcher's session and the
off switches take no letter.  In the shared service the signals are per chat:
- a title that begins with `[no-letter]` (`opencode run --title "[no-letter] pr-sweep" ...`; the sweep's `opencode`
  entry in `scripts/pr_sweep/platforms.json` carries it, because its workers are headless runs and the plugin cannot
  tell a one-shot from a chat in the app: both create the session and send the prompt within milliseconds);
- session metadata `{"platform_letter": "off"}`, which an API client sets when it creates the session;
- a chat that a chat of either kind started (a sweep worker's sub-agents).
The title is read when the chat is registered, a moment after it is created, so a title given with the chat counts.
The hook also refuses a payload whose title carries the mark (the spec's `kind` answers "headless").  A one-shot that
is not marked and ends before the hook has answered (the server's hook makes two ssh hops, about 3 seconds) is still
finished, because the shared service outlives the client and the hook is started detached.

No double registration.  OpenCode with a Claude model is still OpenCode: the process never sets
CLAUDE_CODE_ENTRYPOINT (the string is in neither binary, v2.1.1 on the server and v2.2.2 on the Mac), and the
Claude hook lives in the `hooks` of `~/.claude/settings.json`, which only Claude Code runs: on a scratch OpenCode with
CLAUDE_CODE_ENTRYPOINT=cli in its environment and marker hooks in that file, no marker fired and the registry held no
`claude` row (opencode_letter_plugin/README.md).  So `claude_letter` never runs for an OpenCode chat, and this
adapter registers under the platform `opencode` and the seat OPENCODE only.

Installing.  `platform-letter install --platform opencode [--location server --remote mac] [--check | --remove]` puts
the plugin directory in the `plugins` array and touches nothing else of opencode.json (its other keys are never read
into the output).  The plugin is not copied: the entry names the directory inside the host's managed checkout (the
Mac's managed runtime, the server's fleet-cli `current` link), so a merge updates the plugin with the checkout, and
a running OpenCode takes up the new file with its next restart.  A task lane's checkout is refused, because its path goes away.
There is no trust step: OpenCode loads a plugin listed in its config with no approval.  Chats that are already open load the
plugin with their next prompt in a restarted OpenCode.

Python 3.11+, standard library only.
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Mapping
from typing import Any, TextIO

from . import platform_letter as PL

PLATFORM = "opencode"
SEAT = "OPENCODE"
SOURCE = "opencode-letter-plugin"
PLUGIN_DIRNAME = "opencode_letter_plugin"
PACKAGE_DIRNAME = "agent_sync"
# A plugin directory to list instead of the checkout's own (a test).
ENV_PLUGIN_DIR = "OPENCODE_LETTER_PLUGIN_DIR"

# The names OpenCode gives a chat before anything names it.  A chat with one of them is unnamed.
DEFAULT_TITLE = re.compile(r"^(?:New session|Child session)(?: - \d{4}-\d{2}-\d{2}T[\d:.]+Z?)?$")


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


#: A chat whose title begins with this takes no letter (a headless run, a sweep worker).  The plugin has the same string
#: as NO_LETTER_MARK, and the sweep's `opencode` entry in scripts/pr_sweep/platforms.json passes it as the title.
NO_LETTER_MARK = "[no-letter]"


def no_letter(title: object) -> bool:
    return PL.one_line(title).lower().startswith(NO_LETTER_MARK)


def opencode_kind(payload: Mapping[str, Any], env: Mapping[str, str]) -> str:
    """A chat the plugin says another chat started is a sub-thread, any other is a chat in its own right, and one that
    carries the no-letter mark in its title is a headless run, which takes none."""
    if no_letter(payload.get("title")):
        return "headless"
    return "subagent" if _text(payload.get("parent_session_id")) else "chat"


def opencode_title(payload: Mapping[str, Any], env: Mapping[str, str] | None = None) -> str:
    """The title OpenCode shows now, "" for a chat nothing has named yet."""
    title = PL.one_line(payload.get("title"))
    return "" if DEFAULT_TITLE.match(title) else title


SPEC = PL.PlatformSpec(
    platform=PLATFORM,
    seat=SEAT,
    source=SOURCE,
    display="OpenCode",
    session_id=PL.from_payload("session_id"),
    parent_id=PL.from_payload("parent_session_id"),
    kind=opencode_kind,
    current_title=opencode_title,
    # An archived or deleted chat is over; the plugin sends no other end.
    close_reasons=("archived", "deleted"),
    output="json",
    title_in_output=True,
)


# --------------------------------------------------------------------------------------------
# Installing the plugin
# --------------------------------------------------------------------------------------------


class PluginDirError(ValueError):
    pass


def config_file(env: Mapping[str, str], home: str) -> str:
    """OpenCode's config file: under XDG_CONFIG_HOME when that is set (OpenCode follows it), else ~/.config."""
    base = _text(env.get("XDG_CONFIG_HOME")) or os.path.join(home, ".config")
    return os.path.join(base, "opencode", "opencode.json")


def plugin_dir(env: Mapping[str, str], home: str, source: str = "") -> str:
    """The directory the config entry names: a stable path to this checkout's plugin, never a lane's.  `source` is
    the plugin directory of the checkout the code runs from (a test names another)."""
    forced = _text(env.get(ENV_PLUGIN_DIR))
    if forced:
        if not os.path.isabs(forced) or not os.path.isfile(os.path.join(forced, "index.js")):
            raise PluginDirError(
                "%s must be an absolute directory that holds index.js" % ENV_PLUGIN_DIR
            )
        return forced
    real = source or os.path.join(os.path.dirname(os.path.realpath(__file__)), PLUGIN_DIRNAME)
    if not os.path.isfile(os.path.join(real, "index.js")):
        raise PluginDirError("this checkout has no %s/index.js" % PLUGIN_DIRNAME)
    # The server's runtime is the fleet-cli checkout, reached through its `current` link so a release that moves the
    # link moves the plugin with it.
    current = os.path.join(home, ".local", "share", "fleet-cli", "current")
    root = os.path.realpath(current)
    if os.path.isdir(root) and (real == root or real.startswith(root + os.sep)):
        return os.path.join(current, os.path.relpath(real, root))
    parts = real.split(os.sep)
    if "lanes" in parts:
        after = parts[parts.index("lanes") + 1 : parts.index("lanes") + 2]
        if after != ["_managed"]:
            raise PluginDirError(
                "this is a task lane's checkout, which goes away; run the installer from the host's managed "
                "runtime (~/.local/bin/platform-letter)"
            )
    return real


def entry_path(entry: object) -> str:
    """The plugin path in a `plugins` entry (a string, `{"package": path, "options": {...}}`, or `[path, options]`)."""
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict) and isinstance(entry.get("package"), str):
        return entry["package"]
    if isinstance(entry, list) and entry and isinstance(entry[0], str):
        return entry[0]
    return ""


def entry_options(entry: object) -> dict[str, Any]:
    if isinstance(entry, dict) and isinstance(entry.get("options"), dict):
        return entry["options"]
    if isinstance(entry, list) and len(entry) > 1 and isinstance(entry[1], dict):
        return entry[1]
    return {}


def is_ours(entry: object) -> bool:
    """The entry lists a directory named like this plugin's, whichever host, release or home wrote it: a plugin that
    merely has a similar name elsewhere is not ours (it needs `agent_sync` as its parent)."""
    parts = [p for p in entry_path(entry).replace("\\", "/").split("/") if p not in ("", ".")]
    return len(parts) >= 2 and parts[-1] == PLUGIN_DIRNAME and parts[-2] == PACKAGE_DIRNAME


def make_entry(
    directory: str, where: str, remote: str, kept: Mapping[str, Any] | None = None
) -> Any:
    """A bare path where the plugin needs to be told nothing (the Mac), else the object form with the options.  `kept`
    are the options of the entry being replaced: every key of them stays (the plugin's `log` and `command` are set by
    hand, and a refresh must not drop them), and `where` and `remote` replace only the ones they name."""
    options: dict[str, Any] = dict(kept or {})
    if where:
        options["location"] = where
    if remote:
        options["remote"] = remote
    return {"package": directory, "options": options} if options else directory


class ShapeError(ValueError):
    pass


def merge(doc: dict[str, Any], entry: Any | None) -> tuple[dict[str, Any], str]:
    """(the new document, what was done): the `plugins` array holds our entry exactly once (`entry` None takes it
    out).  Every other key and every other entry stays as it is, in its place."""
    new = json.loads(json.dumps(doc))
    plugins = new.get("plugins")
    if plugins is None:
        plugins = []
    elif not isinstance(plugins, list):
        raise ShapeError("'plugins' in opencode.json is not a list")
    ours = [i for i, e in enumerate(plugins) if is_ours(e)]
    if entry is None:
        if not ours:
            return new, "unchanged"
        kept = [e for i, e in enumerate(plugins) if i not in ours]
        if kept:
            new["plugins"] = kept
        else:
            new.pop("plugins", None)  # the array was only ours
        return new, "removed"
    if len(ours) == 1 and plugins[ours[0]] == entry:
        return new, "unchanged"
    if not ours:
        plugins.append(entry)
        new["plugins"] = plugins
        return new, "added"
    plugins[ours[0]] = entry
    for i in reversed(ours[1:]):
        del plugins[i]
    new["plugins"] = plugins
    return new, "updated"


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("duplicate key %r" % key)
        out[key] = value
    return out


def _indent(text: str) -> str | int:
    match = re.search(r'^([ \t]+)"', text, re.MULTILINE)
    if not match:
        return 2
    return "\t" if match.group(1).startswith("\t") else len(match.group(1))


def install(options: PL.InstallOptions, env: Mapping[str, str], stdout: TextIO) -> int:
    """`install --platform opencode [--location server|mac] [--remote DEST] [--check | --remove]`.

    It is idempotent: a current entry is left alone, an outdated one is replaced where it stands, a missing one is
    appended, and running it again without --location or --remote keeps the ones installed, and every option set by
    hand (`log`, `command`, `shared`, `settle`) stays too, so a refresh and `--check` agree.  The file it rewrites is
    backed up first (`<file>.bak-letter-<UTC stamp>`, 0600), keeps its mode and its indent, and is replaced whole or
    not at all.  A file that is not a JSON object, has a duplicate key, or whose `plugins` is not a list is refused
    untouched.  Only the entry is described in the answer: no other key of opencode.json is ever printed."""
    home = _text(env.get("HOME")) or os.path.expanduser("~")
    path = config_file(env, home)
    if options.remote and not PL.DEST_RE.fullmatch(options.remote):
        return PL._emit(stdout, {"error": "--remote must name an ssh destination"}, 2)
    if options.location and options.location not in PL.LOCATIONS:
        return PL._emit(stdout, {"error": "--location must be mac or server"}, 2)
    if not os.path.isdir(os.path.dirname(path)):
        missing = os.path.dirname(path)
        return PL._emit(
            stdout, {"error": "OpenCode has no config directory here (%s)" % missing}, 2
        )
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        doc = json.loads(text, object_pairs_hook=_no_duplicates)
        existed = True
    except FileNotFoundError:
        text, doc, existed = "", {}, False
    except ValueError as exc:
        return PL._emit(stdout, {"error": "%s is not valid JSON (%s)" % (path, str(exc)[:80])}, 2)
    if not isinstance(doc, dict):
        return PL._emit(stdout, {"error": "%s is not a JSON object" % path}, 2)
    plugins = doc.get("plugins")
    if plugins is not None and not isinstance(plugins, list):
        return PL._emit(stdout, {"error": "'plugins' in %s is not a list" % path}, 2)
    have = [e for e in plugins or [] if is_ours(e)]
    installed = next(iter(have), None)
    inherited = entry_options(installed)
    where = options.location or _text(inherited.get("location"))
    remote = options.remote or _text(inherited.get("remote"))
    try:
        directory = plugin_dir(env, home)
    except PluginDirError as exc:
        if options.remove:
            directory = entry_path(installed) if installed is not None else ""
        else:
            return PL._emit(stdout, {"error": str(exc)}, 2)
    want = make_entry(directory, where, remote, inherited) if directory else None
    hook = os.path.join(home, ".local", "bin", PL.ENTRY)
    if options.check:
        answer = {
            "platform": PLATFORM,
            "path": path,
            "installed": {"plugins": bool(have)},
            "current": {"plugins": len(have) == 1 and have[0] == want},
            "hook_present": os.path.exists(hook),
        }
        ok = answer["installed"]["plugins"] and answer["current"]["plugins"]
        return PL._emit(stdout, answer, 0 if ok else 1)
    try:
        new, action = merge(doc, None if options.remove else want)
    except ShapeError as exc:
        return PL._emit(stdout, {"error": str(exc)}, 2)
    if action == "unchanged":
        return PL._emit(stdout, {"platform": PLATFORM, "changed": [], "path": path}, 0)
    backup = ""
    if existed:
        mode = os.stat(path).st_mode & 0o777
        backup = PL._backup(path)
    else:
        mode = 0o600
    body = (json.dumps(new, indent=_indent(text), ensure_ascii=False) + "\n").encode("utf-8")
    tmp = path + ".letter.tmp"
    PL._write_private(tmp, body, mode)
    os.replace(tmp, path)
    answer: dict[str, Any] = {
        "platform": PLATFORM,
        "changed": ["plugins"],
        "action": action,
        "path": path,
        "backup": backup,
    }
    if not options.remove:
        answer["plugin"] = directory
        notes = []
        if not os.path.exists(hook):
            notes.append("%s does not exist yet; the plugin takes no letter until it does" % hook)
        if sys.platform != "darwin" and not (where and remote):
            notes.append(
                "this host is not the Mac: install with --location server --remote <ssh destination>"
            )
        if notes:
            answer["notes"] = notes
    return PL._emit(stdout, answer, 0)
