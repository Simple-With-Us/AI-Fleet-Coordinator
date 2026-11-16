#!/usr/bin/env python3
"""What is installed for one coding platform on this host, beyond what the engine checks.

The fleet-config engine says whether each seat's expected files are right.  This says what is
actually there:  every MCP server, hook, plugin, skill, sub-agent and command a platform has, so
a person can see what the platform will load and judge a drift against something concrete.
Each platform keeps these in its own files and formats, so each has its own list of sources
(``SOURCES``), written from the files the platforms really keep.

One implementation serves both hosts.  The Mac runs it directly, and the server runs it through
the restricted helper (``fleet-ops-control.py``, verb ``server seat-inventory``).  The home
folder is the only difference between them:  a platform keeps its files under the same names on
both.

It reads names and counts only.  A value in a config file (a token, a header, a command line, a
path) is never kept, so nothing here can leak a secret, and a file that is absent, too large or
unreadable says so and never reads as empty.  It never writes.  Stdlib only, and Python 3.9 safe,
matching ``fleet_knobs`` and ``fleet_config``.

    python3 -I scripts/fleet_seat_inventory.py --platform claude [--home HOME]
"""

from __future__ import annotations

import datetime
import json
import os
import re
import sys

SCHEMA_VERSION = 1
TOOL = "fleet-seat-inventory"

# A config file is small, and a huge one is not one.
MAX_FILE_BYTES = 2_000_000
# The most names one section keeps.
MAX_NAMES = 300
MAX_NAME_LENGTH = 100

# A saved copy of another file, which a listing leaves out and counts.  The marker is a whole
# dot-separated word that ends the name or is followed by more text after a `-`, `.` or `_`
# (`settings.json.bak`, `config.toml.bak-fleet-rag-20260901`, `hooks.json.before-lane-guard`),
# so a name that only contains the letters (`template.tmpl`, `foo.bakery`, `origin.md`) is kept.
BACKUP_RE = re.compile(r"\.(?:bak|before|disabled|orig|tmp)(?:$|[-._])")


# MARK: - what each platform keeps
#
# Each source is a tuple whose first item names how to read it.  Paths are relative to the home
# folder.
#
#   ("mcp", file, path)             the keys at `path` in a JSON file, or ("mcp_toml", file) for
#                                   the `[mcp_servers.<name>]` tables of a TOML file
#   ("hook_events", file, path)     {event: [entries]}:  each event with its entry count
#   ("hook_groups", file)           {group: {Event: [entries], enabled: bool}}
#   ("hook_folder", folder)         every *.json in a folder holds {"hooks": {event: [...]}}
#   ("keys", title, file, path)     the keys at `path` are the names (a bool value reads on/off)
#   ("array_names", title, file, path)  an array of objects, named by `name` or `id`
#   ("counts", title, file, ((label, path), ...))  the lengths of arrays
#   ("toml_tables", title, file, prefix)   TOML tables named `prefix.<name>`
#   ("toml_array", title, file, table)     `[[table]]` entries, counted
#   ("folder", title, path, drop_markdown) the entries of a folder
#   ("not_read", title, source, reason)    a place this reader does not read, in words

SOURCES = {
    "claude": [
        ("hook_events", ".claude/settings.json", ("hooks",)),
        (
            "counts",
            "Permissions",
            ".claude/settings.json",
            (
                ("allow rules", ("permissions", "allow")),
                ("deny rules", ("permissions", "deny")),
            ),
        ),
        ("keys", "Plugins", ".claude/plugins/installed_plugins.json", ("plugins",)),
        ("folder", "Skills", ".claude/skills", False),
        ("folder", "Sub-Agents", ".claude/agents", True),
        ("folder", "Commands", ".claude/commands", True),
    ],
    "codex": [
        ("mcp_toml", ".codex/config.toml"),
        ("hook_events", ".codex/hooks.json", ("hooks",)),
        ("toml_tables", "Plugins", ".codex/config.toml", "plugins"),
        ("folder", "Skills", ".codex/skills", False),
    ],
    "cursor": [
        ("mcp", ".cursor/mcp.json", ("mcpServers",)),
        ("hook_events", ".cursor/hooks.json", ("hooks",)),
        (
            "counts",
            "Permissions",
            ".cursor/cli-config.json",
            (
                ("allow rules", ("permissions", "allow")),
                ("deny rules", ("permissions", "deny")),
            ),
        ),
        ("folder", "Skills", ".cursor/skills", False),
        ("folder", "Sub-Agents", ".cursor/agents", True),
        ("folder", "Rules", ".cursor/rules", False),
    ],
    "antigravity": [
        ("mcp", ".gemini/config/mcp_config.json", ("mcpServers",)),
        ("mcp", ".gemini/settings.json", ("mcpServers",)),
        ("hook_groups", ".gemini/config/hooks.json"),
        ("folder", "Plugins", ".gemini/config/plugins", False),
        ("folder", "Skills", ".gemini/config/skills", False),
    ],
    "kimi": [
        ("mcp", ".kimi-code/mcp.json", ("mcpServers",)),
        ("toml_array", "Hooks", ".kimi-code/config.toml", "hooks"),
        ("array_names", "Plugins", ".kimi-code/plugins/installed.json", ("plugins",)),
        ("folder", "Skills", ".kimi-code/skills", False),
    ],
    "grok": [
        ("mcp_toml", ".grok/config.toml"),
        ("hook_folder", ".grok/hooks"),
        ("folder", "Skills", ".grok/skills", False),
    ],
    "fx": [
        ("mcp", ".fx/mcp.json", ("mcp",)),
        ("folder", "Skills", ".fx/skills", False),
    ],
    "zcode": [
        ("mcp", ".zcode/cli/config.json", ("mcpServers",)),
        ("hook_events", ".zcode/cli/config.json", ("hooks", "events")),
        ("keys", "Plugins", ".zcode/cli/config.json", ("plugins", "enabledPlugins")),
        ("folder", "Skills", ".zcode/skills", False),
    ],
    "opencode": [
        ("mcp", ".config/opencode/opencode.json", ("mcp",)),
        ("folder", "Plugins", ".config/opencode/plugins", False),
        ("folder", "Commands", ".config/opencode/commands", True),
    ],
    "muse": [
        ("mcp", ".config/muse/settings.json", ("mcpServers",)),
        ("keys", "Plugin Hooks", ".config/muse/settings.json", ("runtime_capabilities",)),
        ("folder", "Skills", ".config/muse/skills", False),
    ],
    "minimax": [
        ("mcp", ".minimax/mcp.json", ("mcpServers",)),
        ("folder", "Plugins", ".minimax/plugins", False),
        ("folder", "Skills", ".minimax/skills", False),
        ("folder", "Agents", ".minimax/agents", False),
    ],
    "clutch": [
        (
            "not_read",
            "MCP Servers And Hooks",
            "~/.clutch/dsh/cordis.patch.yml",
            "Clutch keeps them in YAML, which this reader does not read yet.",
        ),
        ("folder", "Skills", ".clutch/dsh/skills", False),
    ],
}
# The Muse Code seat runs on the same files as Muse.
SOURCES["muse-code"] = SOURCES["muse"]

PLATFORMS = tuple(sorted(SOURCES))


# MARK: - reading


def clean(name):
    """A name as the answer keeps it:  no control characters, and not longer than a line."""
    kept = "".join(ch for ch in str(name) if ch.isprintable())
    return kept[:MAX_NAME_LENGTH]


def _item(name, detail=None):
    return {"name": clean(name), "detail": detail}


def _section(title, source, items, note=None, empty_note=None):
    """One section, capped, with a note that says why it is empty or cut."""
    if not items:
        return {"title": title, "source": source, "items": [], "note": note or empty_note}
    if len(items) > MAX_NAMES:
        cut = "Only the first %d of %d are listed." % (MAX_NAMES, len(items))
        return {
            "title": title,
            "source": source,
            "items": items[:MAX_NAMES],
            "note": cut if not note else note + "  " + cut,
        }
    return {"title": title, "source": source, "items": items, "note": note}


def _load(home, relative):
    """The bytes of a file, or ("absent"|"failed", why)."""
    path = os.path.join(home, relative)
    try:
        info = os.stat(path)
    except OSError:
        return "absent", None
    if os.path.isdir(path):
        return "failed", "It is a folder, not a file."
    if info.st_size > MAX_FILE_BYTES:
        return "failed", "The file is larger than this reader reads, so nothing was read."
    try:
        with open(path, "rb") as handle:
            return "data", handle.read()
    except OSError:
        return "failed", "The file is there and could not be read."


def _json_section(home, title, relative, build):
    shown = "~/" + relative
    state, payload = _load(home, relative)
    if state == "absent":
        return _section(title, shown, [], note="The file is not on this host.")
    if state == "failed":
        return _section(title, shown, [], note=payload)
    try:
        root = json.loads(payload.decode("utf-8", errors="replace"))
    except ValueError:
        return _section(title, shown, [], note="The file is not valid JSON, so nothing was read.")
    return _section(title, shown, build(root), empty_note="The file lists none.")


def _value_at(root, path):
    current = root
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _entries(count):
    return "1 entry" if count == 1 else "%d entries" % count


def _mcp_transport(entry):
    """`remote` or `local command`, from which keys an entry has and never from their values."""
    if not isinstance(entry, dict):
        return None
    if any(key in entry for key in ("url", "serverUrl", "httpUrl", "sse_url")):
        return "remote"
    if "command" in entry:
        return "local command"
    return None


def is_backup(name):
    return BACKUP_RE.search(name) is not None


def _strip_toml_comment(line):
    """The line without a trailing ``# comment``.  A ``#`` inside quotes is part of a name."""
    quote = None
    for position, char in enumerate(line):
        if quote:
            if char == quote:
                quote = None
        elif char in ('"', "'"):
            quote = char
        elif char == "#":
            return line[:position]
    return line


def toml_headers(text, arrays=False):
    """The table headers of a TOML text, each split into its dotted parts.

    ``[a.b."c.d"]`` is ``["a", "b", "c.d"]``.  Single-bracket headers by default, and the
    ``[[double]]`` ones when ``arrays`` is set.  Only headers are looked at:  no ``key = value``
    line is read."""
    headers = []
    for raw in text.splitlines():
        line = _strip_toml_comment(raw).strip()
        is_array = line.startswith("[[") and line.endswith("]]")
        is_table = (not is_array) and line.startswith("[") and line.endswith("]")
        if not ((arrays and is_array) or (not arrays and is_table)):
            continue
        inner = line[2:-2] if is_array else line[1:-1]
        headers.append(_split_dotted(inner))
    return headers


def _split_dotted(text):
    parts, current, quote = [], [], None
    for ch in text:
        if quote:
            if ch == quote:
                quote = None
            else:
                current.append(ch)
        elif ch in ('"', "'"):
            quote = ch
        elif ch == ".":
            parts.append("".join(current).strip())
            current = []
        else:
            current.append(ch)
    parts.append("".join(current).strip())
    return parts


def _toml_section(home, title, relative, build, arrays=False):
    shown = "~/" + relative
    state, payload = _load(home, relative)
    if state == "absent":
        return _section(title, shown, [], note="The file is not on this host.")
    if state == "failed":
        return _section(title, shown, [], note=payload)
    headers = toml_headers(payload.decode("utf-8", errors="replace"), arrays=arrays)
    return _section(title, shown, build(headers), empty_note="The file lists none.")


def _folder_section(home, title, relative, drop_markdown):
    shown = "~/" + relative
    path = os.path.join(home, relative)
    if not os.path.exists(path):
        return _section(title, shown, [], note="The folder is not on this host.")
    try:
        names = os.listdir(path)
    except OSError:
        return _section(title, shown, [], note="It is there and could not be read as a folder.")
    visible = [n for n in names if not n.startswith(".") and n != "README.md"]
    backups = len([n for n in visible if is_backup(n)])
    kept = sorted(n for n in visible if not is_backup(n))
    items = []
    for name in kept:
        shown_name = name[:-3] if drop_markdown and name.endswith(".md") else name
        items.append(_item(shown_name))
    note = None
    if backups:
        note = (
            "1 backup copy is" if backups == 1 else "%d backup copies are" % backups
        ) + " not listed."
    return _section(title, shown, items, note=note, empty_note="The folder is empty.")


def _hook_folder_section(home, relative):
    shown = "~/" + relative
    path = os.path.join(home, relative)
    if not os.path.isdir(path):
        return _section("Hooks", shown, [], note="The folder is not on this host.")
    try:
        names = sorted(os.listdir(path))
    except OSError:
        return _section("Hooks", shown, [], note="It is there and could not be read as a folder.")
    items, problems = [], []
    for name in names:
        if not name.endswith(".json") or is_backup(name):
            continue
        state, payload = _load(home, relative + "/" + name)
        if state != "data":
            problems.append("%s:  %s" % (name, payload or "could not be read"))
            continue
        try:
            root = json.loads(payload.decode("utf-8", errors="replace"))
        except ValueError:
            problems.append("%s:  not valid JSON" % name)
            continue
        events = _value_at(root, ("hooks",))
        if isinstance(events, dict):
            for event in sorted(events):
                count = len(events[event]) if isinstance(events[event], list) else None
                items.append(
                    _item(
                        "%s (%s)" % (clean(event), clean(name)),
                        _entries(count) if count is not None else None,
                    )
                )
    note = "  ".join(problems) if problems else None
    return _section("Hooks", shown, items, note=note, empty_note="No hook file registers any.")


def _read_source(home, source):
    kind = source[0]
    if kind == "mcp":
        _, relative, path = source

        def build(root):
            servers = _value_at(root, path)
            servers = servers if isinstance(servers, dict) else {}
            return [_item(name, _mcp_transport(servers[name])) for name in sorted(servers)]

        return _json_section(home, "MCP Servers", relative, build)
    if kind == "mcp_toml":
        _, relative = source

        def build_toml(headers):
            names = sorted({h[1] for h in headers if len(h) >= 2 and h[0] == "mcp_servers"})
            return [_item(name) for name in names]

        return _toml_section(home, "MCP Servers", relative, build_toml)
    if kind == "hook_events":
        _, relative, path = source

        def build_events(root):
            events = _value_at(root, path)
            events = events if isinstance(events, dict) else {}
            return [
                _item(
                    event, _entries(len(events[event])) if isinstance(events[event], list) else None
                )
                for event in sorted(events)
            ]

        return _json_section(home, "Hooks", relative, build_events)
    if kind == "hook_groups":
        _, relative = source

        def build_groups(root):
            groups = root if isinstance(root, dict) else {}
            items = []
            for group in sorted(groups):
                entry = groups[group] if isinstance(groups[group], dict) else {}
                parts = sorted(
                    clean(key) for key, value in entry.items() if isinstance(value, list)
                )
                if isinstance(entry.get("enabled"), bool):
                    parts.append("on" if entry["enabled"] else "off")
                items.append(_item(group, ", ".join(parts) if parts else None))
            return items

        return _json_section(home, "Hook Groups", relative, build_groups)
    if kind == "hook_folder":
        return _hook_folder_section(home, source[1])
    if kind == "keys":
        _, title, relative, path = source

        def build_keys(root):
            keys = _value_at(root, path)
            keys = keys if isinstance(keys, dict) else {}
            return [
                _item(key, ("on" if keys[key] else "off") if isinstance(keys[key], bool) else None)
                for key in sorted(keys)
            ]

        return _json_section(home, title, relative, build_keys)
    if kind == "array_names":
        _, title, relative, path = source

        def build_array(root):
            array = _value_at(root, path)
            array = array if isinstance(array, list) else []
            items = []
            for index, element in enumerate(array):
                fields = element if isinstance(element, dict) else {}
                name = (
                    fields.get("name") if isinstance(fields.get("name"), str) else fields.get("id")
                )
                items.append(_item(name if isinstance(name, str) else "entry %d" % (index + 1)))
            return items

        return _json_section(home, title, relative, build_array)
    if kind == "counts":
        _, title, relative, lines = source

        def build_counts(root):
            items = []
            for label, path in lines:
                value = _value_at(root, path)
                if isinstance(value, list):
                    items.append(_item(label, str(len(value))))
            return items

        return _json_section(home, title, relative, build_counts)
    if kind == "toml_tables":
        _, title, relative, prefix = source

        def build_tables(headers):
            names = sorted({h[1] for h in headers if len(h) >= 2 and h[0] == prefix})
            return [_item(name) for name in names]

        return _toml_section(home, title, relative, build_tables)
    if kind == "toml_array":
        _, title, relative, table = source

        def build_array_count(headers):
            count = len([h for h in headers if h == [table]])
            return [_item("registrations", str(count))] if count else []

        return _toml_section(home, title, relative, build_array_count, arrays=True)
    if kind == "folder":
        _, title, relative, drop_markdown = source
        return _folder_section(home, title, relative, drop_markdown)
    if kind == "not_read":
        _, title, shown, reason = source
        return _section(title, shown, [], note=reason)
    raise ValueError("unknown source kind: %s" % kind)


def read_platform(platform, home):
    """The inventory for one platform under ``home``, or None for a platform not known."""
    sources = SOURCES.get(platform)
    if sources is None:
        return None
    return [_read_source(home, source) for source in sources]


def host_label():
    return "mac" if sys.platform == "darwin" else "server"


def inventory(platform, home=None, now=None, host=None):
    """The whole answer for one platform:  the JSON document both hosts return.

    ``host`` names the host the answer is for;  it defaults to the one this process runs on."""
    home = home or os.path.expanduser("~")
    sections = read_platform(platform, home)
    # The `UTC` alias on the datetime module is Python 3.11, and the Mac's system Python is 3.9,
    # so this stays the older spelling that both read.
    moment = now or datetime.datetime.now(datetime.timezone.utc)  # noqa: UP017
    stamp = moment.strftime("%Y-%m-%dT%H:%M:%SZ")
    host = host or host_label()
    if sections is None:
        return {
            "schemaVersion": SCHEMA_VERSION,
            "tool": TOOL,
            "ok": False,
            "host": host,
            "platform": platform,
            "generatedAt": stamp,
            "sections": [],
            "detail": "This reader has no sources for that platform.",
        }
    return {
        "schemaVersion": SCHEMA_VERSION,
        "tool": TOOL,
        "ok": True,
        "host": host,
        "platform": platform,
        "generatedAt": stamp,
        "sections": sections,
        "detail": "",
    }


PLATFORM_RE = re.compile(r"[a-z][a-z0-9-]{0,24}")


def main(argv=None, out=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    out = out or sys.stdout
    platform, home = None, None
    index = 0
    while index < len(argv):
        word = argv[index]
        if word == "--platform" and index + 1 < len(argv):
            platform = argv[index + 1]
            index += 2
        elif word == "--home" and index + 1 < len(argv):
            home = argv[index + 1]
            index += 2
        elif word == "--json":
            index += 1
        else:
            out.write(
                json.dumps({"ok": False, "detail": "usage: --platform NAME [--home HOME] [--json]"})
                + "\n"
            )
            return 2
    if not platform or not PLATFORM_RE.fullmatch(platform):
        out.write(json.dumps({"ok": False, "detail": "name one platform with --platform"}) + "\n")
        return 2
    answer = inventory(platform, home)
    out.write(json.dumps(answer, sort_keys=True) + "\n")
    return 0 if answer["ok"] else 2


if __name__ == "__main__":
    sys.exit(main())
