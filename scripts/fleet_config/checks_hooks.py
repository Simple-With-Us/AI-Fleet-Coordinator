"""``hooks`` checks:  the hook ids registered for an event in a harness settings file.

The check type is ``HOOK_REGISTERED``.  The target is the platform's hook config file
and ``rule.check.detail`` names the event, which is ``PreToolUse`` in Phase 2.  The
expected hook ids are ``rule.expected.names`` and the expected timeout is
``rule.expected.timeout_seconds``.

The config formats that are read:

  * JSON, in the Claude shape (``hooks.<event>`` is a list of groups, each with a
    ``hooks`` list inside), the Cursor shape (``hooks.<event>`` is a flat list of
    hook objects) and the Antigravity shape (a named group at the top level holds the
    event list);
  * TOML, in the Kimi shape (a ``[[hooks]]`` table per entry, each with its own
    ``event``).

A file whose format cannot be read is never a pass.  A YAML file (the Clutch cordis
patch) has no reader in the standard library, so it comes back as unparseable.

This module also owns ``read_config_document``, the shared reader for a JSON or TOML
config file.  The six-module rule of the design doc leaves no room for a seventh
shared module, and the hooks check is the one that has to read TOML, so the reader
lives here and ``checks_mcp`` and ``checks_permissions`` import it from here.

A hook id is derived in this order:  the entry's own ``name``, else the name of the
group that holds it, else the file name of the script the command runs.  An interpreter
in front of a script does not hide the script's name, so
``python3 ~/.claude/hooks/secret-guard.py`` is the hook ``secret-guard``.  A trailing
``-hook`` is ignored when a name is compared, so a rule may name the guard
``lane-guard`` while the config spells the executable ``lane-guard-hook``.

Stdlib only, and Python 3.9 safe, matching ``fleet_knobs``.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

from .checks import CheckContext, CheckError, build_result, expand_target
from .types import (
    CheckResult,
    CheckState,
    CheckType,
    ObservedState,
    ProofLevel,
    ReasonCode,
    Rule,
)

try:  # Python 3.11 and later
    import tomllib
except ImportError:  # pragma: no cover - the stable copy runs on Python 3.9
    tomllib = None  # type: ignore[assignment]

__all__ = ["CHECKS"]

# The event every rule names when it names none.  Phase 2 has one event per platform
# and PreToolUse is the one the fleet installs everywhere.
DEFAULT_EVENT = "PreToolUse"

_TOML_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")
_TOML_ESCAPES = {
    "\\": "\\",
    '"': '"',
    "b": "\b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
}

# The file extensions that name a script, used to name a hook after the script it runs.
_SCRIPT_SUFFIXES = (".py", ".js", ".mjs", ".cjs", ".sh", ".bash", ".zsh")


class TomlError(ValueError):
    """A TOML file holds something this reader cannot read with certainty."""


# --------------------------------------------------------------------------- the shared reader


def read_config_document(path: str) -> dict[str, Any]:
    """The parsed config document at `path`, by its extension.

    A JSON file is read with the standard parser.  A TOML file is read with
    ``tomllib`` when the runtime has it (Python 3.11 and later) and with the subset
    reader below otherwise, which is the stable copy's case.  The subset reader
    refuses a file it cannot read with certainty, so a caller never guesses.

    Raises OSError, UnicodeDecodeError or ValueError;  the callers turn those into an
    unparseable result rather than a pass.
    """
    with open(path, "rb") as handle:
        raw = handle.read()
    text = raw.decode("utf-8")
    extension = os.path.splitext(path)[1].lower()
    if extension == ".json":
        return _json_document(text)
    if extension == ".toml":
        return _toml_document(text)
    # An unknown extension:  try JSON, then TOML.
    try:
        return _json_document(text)
    except ValueError:
        return _toml_document(text)


def _json_document(text: str) -> dict[str, Any]:
    """A JSON object, or a ValueError when the text is not one."""
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("the config document is not a JSON object")
    return data


def _toml_document(text: str) -> dict[str, Any]:
    """A TOML document, or a ValueError when the text cannot be read."""
    if tomllib is not None:
        try:
            return tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            raise ValueError("the TOML document cannot be read:  %s" % exc) from None
    try:
        return parse_toml(text)
    except TomlError as exc:
        raise ValueError(
            "the TOML document cannot be read by this runtime's subset reader:  %s" % exc
        ) from None


# --------------------------------------------------------------------------- the TOML subset reader


def parse_toml(text: str) -> dict[str, Any]:
    """A TOML subset:  tables, arrays of tables and simple values.

    Everything this reader cannot read with certainty raises TomlError:  multi-line
    strings, inline tables, dates and anything else it does not model.  The caller
    refuses the file instead of guessing at it.
    """
    root: dict[str, Any] = {}
    table: dict[str, Any] = root
    lines = text.split("\n")
    index = 0
    while index < len(lines):
        line = lines[index].strip()
        if not line or line.startswith("#"):
            index += 1
            continue
        if line.startswith("[["):
            if not line.endswith("]]"):
                raise TomlError("cannot read the table header %r" % line)
            table = _open_array_table(root, _key_path(line[2:-2].strip()))
            index += 1
            continue
        if line.startswith("["):
            if not line.endswith("]"):
                raise TomlError("cannot read the table header %r" % line)
            table = _open_table(root, _key_path(line[1:-1].strip()))
            index += 1
            continue
        equals = _find_equals(line)
        if equals < 0:
            raise TomlError("cannot read the line %r" % line)
        key = _key_path(line[:equals].strip())
        value, index = _read_value(lines, index, line[equals + 1 :])
        _assign(table, key, value)
    return root


def _open_table(root: dict[str, Any], path: list[str]) -> dict[str, Any]:
    """The table a header names, created when it is not there yet."""
    node: dict[str, Any] = root
    for part in path:
        child = node.get(part)
        if child is None:
            child = {}
            node[part] = child
        elif isinstance(child, list):
            if not child or not isinstance(child[-1], dict):
                raise TomlError("cannot read the table %r" % part)
            child = child[-1]
        elif not isinstance(child, dict):
            raise TomlError("cannot read the table %r" % part)
        node = child
    return node


def _open_array_table(root: dict[str, Any], path: list[str]) -> dict[str, Any]:
    """A new entry of an array of tables, created when the array is not there yet."""
    if not path:
        raise TomlError("an empty table header")
    parent = _open_table(root, path[:-1])
    name = path[-1]
    child = parent.get(name)
    if child is None:
        child = []
        parent[name] = child
    if not isinstance(child, list):
        raise TomlError("cannot read the table %r" % name)
    entry: dict[str, Any] = {}
    child.append(entry)
    return entry


def _assign(table: dict[str, Any], path: list[str], value: Any) -> None:
    """One key (possibly a dotted path) inside a table."""
    node = table
    for part in path[:-1]:
        child = node.get(part)
        if not isinstance(child, dict):
            child = {}
            node[part] = child
        node = child
    node[path[-1]] = value


def _read_value(lines: list[str], index: int, text: str) -> tuple[Any, int]:
    """(value, index of the next unread line) for the value that starts at `text`.

    `index` is the line the value starts on.  An array may run over several lines, so
    the reader takes more lines when it has to.
    """
    text = text.strip()
    index += 1
    if not text:
        raise TomlError("a key with no value")
    head = text[0]
    if head == '"':
        if text.startswith('"""'):
            raise TomlError("a multi-line string")
        value, _rest = _basic_string(text)
        return value, index
    if head == "'":
        if text.startswith("'''"):
            raise TomlError("a multi-line string")
        value, _rest = _literal_string(text)
        return value, index
    if head == "[":
        buffer = text
        while _bracket_depth(buffer) > 0:
            if index >= len(lines):
                raise TomlError("an array that never closes")
            buffer += " " + lines[index].strip()
            index += 1
        return _array(buffer), index
    if head == "{":
        raise TomlError("an inline table")
    token = text.split("#", 1)[0].strip()
    if token == "true":
        return True, index
    if token == "false":
        return False, index
    try:
        return int(token), index
    except ValueError:
        pass
    try:
        return float(token), index
    except ValueError:
        pass
    raise TomlError("cannot read the value %r" % text)


def _array(text: str) -> list[Any]:
    """An array of simple values."""
    inner = text.strip()[1:-1]
    items: list[Any] = []
    for piece in _split_outside(inner, ","):
        piece = piece.strip()
        if not piece:
            continue
        items.append(_scalar(piece))
    return items


def _scalar(text: str) -> Any:
    """One array element:  a string, a number, a boolean or a nested array."""
    head = text[0]
    if head == '"':
        if text.startswith('"""'):
            raise TomlError("a multi-line string")
        value, _rest = _basic_string(text)
        return value
    if head == "'":
        if text.startswith("'''"):
            raise TomlError("a multi-line string")
        value, _rest = _literal_string(text)
        return value
    if head == "[":
        return _array(text)
    if head == "{":
        raise TomlError("an inline table")
    token = text.split("#", 1)[0].strip()
    if token == "true":
        return True
    if token == "false":
        return False
    try:
        return int(token)
    except ValueError:
        pass
    try:
        return float(token)
    except ValueError:
        pass
    raise TomlError("cannot read the value %r" % text)


def _basic_string(text: str) -> tuple[str, str]:
    """(decoded string, text after the closing quote) for a basic TOML string."""
    out: list[str] = []
    i = 1
    while i < len(text):
        char = text[i]
        if char == "\\":
            i += 1
            if i >= len(text):
                raise TomlError("a string that ends in an escape")
            escape = text[i]
            if escape in _TOML_ESCAPES:
                out.append(_TOML_ESCAPES[escape])
                i += 1
                continue
            if escape in ("u", "U"):
                width = 4 if escape == "u" else 8
                digits = text[i + 1 : i + 1 + width]
                if len(digits) != width:
                    raise TomlError("a bad unicode escape")
                try:
                    out.append(chr(int(digits, 16)))
                except ValueError:
                    raise TomlError("a bad unicode escape") from None
                i += 1 + width
                continue
            raise TomlError("an unknown escape %r" % escape)
        if char == '"':
            return "".join(out), text[i + 1 :]
        out.append(char)
        i += 1
    raise TomlError("a string with no closing quote")


def _literal_string(text: str) -> tuple[str, str]:
    """(string, text after the closing quote) for a literal TOML string."""
    end = text.find("'", 1)
    if end < 0:
        raise TomlError("a string with no closing quote")
    return text[1:end], text[end + 1 :]


def _key_path(text: str) -> list[str]:
    """The parts of a dotted key or a table header."""
    parts: list[str] = []
    for piece in _split_outside(text, "."):
        part = piece.strip()
        if len(part) >= 2 and part.startswith('"') and part.endswith('"'):
            parts.append(part[1:-1])
        elif _TOML_BARE_KEY.match(part):
            parts.append(part)
        else:
            raise TomlError("cannot read the key %r" % part)
    if not parts:
        raise TomlError("an empty key")
    return parts


def _find_equals(line: str) -> int:
    """The index of the ``=`` of a key/value line, or -1."""
    quote = ""
    for index, char in enumerate(line):
        if quote:
            if char == quote:
                quote = ""
            continue
        if char in "\"'":
            quote = char
            continue
        if char == "=":
            return index
    return -1


def _split_outside(text: str, separator: str) -> list[str]:
    """Split on a separator that is not inside a string or a bracket."""
    parts: list[str] = []
    current: list[str] = []
    depth = 0
    quote = ""
    for char in text:
        if quote:
            current.append(char)
            if char == quote:
                quote = ""
            continue
        if char in "\"'":
            quote = char
            current.append(char)
            continue
        if char in "[{":
            depth += 1
        elif char in "]}":
            depth -= 1
        if char == separator and depth == 0:
            parts.append("".join(current))
            current = []
            continue
        current.append(char)
    parts.append("".join(current))
    return parts


def _bracket_depth(text: str) -> int:
    """How many array brackets are still open in the text."""
    depth = 0
    quote = ""
    for char in text:
        if quote:
            if char == quote:
                quote = ""
            continue
        if char in "\"'":
            quote = char
            continue
        if char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
    return depth


# --------------------------------------------------------------------------- the hook shapes


def event_key(name: str) -> str:
    """A comparable key for an event name:  case, underscores and dashes do not matter."""
    return name.lower().replace("_", "").replace("-", "")


def hook_key(name: str) -> str:
    """A comparable key for a hook id:  a trailing ``-hook`` does not matter."""
    if name.endswith("-hook"):
        return name[: -len("-hook")]
    return name


def hook_entries(document: dict[str, Any], event: str) -> list[tuple[str, int | None]]:
    """Every hook entry registered for `event`, as (id, timeout in seconds).

    Raises ValueError when the document holds a shape this reader cannot read.
    """
    wanted = event_key(event)
    entries: list[tuple[str, int | None]] = []
    hooks = document.get("hooks")
    if isinstance(hooks, dict):
        for key, value in hooks.items():
            if not isinstance(key, str) or event_key(key) != wanted:
                continue
            if not isinstance(value, list):
                raise ValueError("the hook list for %r is not a list" % key)
            for item in value:
                entries.extend(_group_entries(item))
        return entries
    if isinstance(hooks, list):
        for item in hooks:
            if not isinstance(item, dict):
                raise ValueError("a hook entry is not a table")
            name = item.get("event")
            if isinstance(name, str) and event_key(name) == wanted:
                entries.append((_hook_id(item, None), _hook_timeout(item)))
        return entries
    # The named-group shape:  Antigravity holds one group per hook name at the top level.
    for group_name, group in document.items():
        if not isinstance(group, dict):
            continue
        for key, value in group.items():
            if not isinstance(key, str) or event_key(key) != wanted:
                continue
            if not isinstance(value, list):
                raise ValueError("the hook list for %r is not a list" % key)
            for item in value:
                entries.extend(_group_entries(item, group_name))
    return entries


def _group_entries(item: Any, group_name: str | None = None) -> list[tuple[str, int | None]]:
    """The entries of one hook group or one flat hook object."""
    if not isinstance(item, dict):
        raise ValueError("a hook entry is not an object")
    inner = item.get("hooks")
    if isinstance(inner, list):
        out: list[tuple[str, int | None]] = []
        for hook in inner:
            if not isinstance(hook, dict):
                raise ValueError("a hook entry is not an object")
            out.append((_hook_id(hook, group_name), _hook_timeout(hook)))
        return out
    return [(_hook_id(item, group_name), _hook_timeout(item))]


def _hook_id(hook: dict[str, Any], group_name: str | None) -> str:
    """The id of one hook entry:  its own name, else its group name, else its command."""
    name = hook.get("name")
    if isinstance(name, str) and name.strip():
        return name.strip()
    if group_name:
        return group_name
    command = hook.get("command")
    if isinstance(command, str) and command.strip():
        return command_id(command)
    return ""


def command_id(command: str) -> str:
    """The id a command names, which is the file name of the script it runs.

    The first token that names a script wins, so an interpreter in front of a script
    does not hide the script's name:  ``python3 ~/.claude/hooks/secret-guard.py`` is the
    hook ``secret-guard``.  A command that names no script is the file name of its own
    first token, which is how the fleet's own hook commands read.
    """
    tokens = command.split()
    if not tokens:
        return ""
    for token in tokens:
        base = os.path.basename(token.strip("\"'"))
        stem, extension = os.path.splitext(base)
        if extension in _SCRIPT_SUFFIXES:
            return stem
    base = os.path.basename(tokens[0].strip("\"'"))
    stem, extension = os.path.splitext(base)
    if extension in _SCRIPT_SUFFIXES:
        return stem
    return base


def _hook_timeout(hook: dict[str, Any]) -> int | None:
    """The timeout of a hook entry in seconds, or None when it has none."""
    value = hook.get("timeout")
    seconds = _as_int(value)
    if seconds is not None:
        return seconds
    millis = _as_int(hook.get("timeoutMs"))
    if millis is not None:
        return millis // 1000
    return None


def _as_int(value: Any) -> int | None:
    """A value as an integer, or None when it is not one."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and float(value).is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().lstrip("+-").isdigit():
        return int(value.strip())
    return None


# --------------------------------------------------------------------------- the check


def check_hook_registered(rule: Rule, platform: str, ctx: CheckContext) -> CheckResult:
    """Check that the expected hooks are registered for an event in a settings file."""
    target = rule.check.target
    try:
        path = expand_target(target, ctx.home)
    except CheckError as exc:
        return build_result(
            rule,
            platform,
            CheckState.MISSING,
            str(exc),
            ReasonCode.MISSING,
            ObservedState(present=False),
            ctx,
        )

    if not os.path.exists(path):
        return build_result(
            rule,
            platform,
            CheckState.MISSING,
            "The hook config file %s is missing." % target,
            ReasonCode.MISSING,
            ObservedState(present=False),
            ctx,
        )

    if not os.path.isfile(path):
        return build_result(
            rule,
            platform,
            CheckState.UNKNOWN,
            "The hook config target %s is not a regular file." % target,
            ReasonCode.ERROR,
            ObservedState(present=None),
            ctx,
        )

    try:
        document = read_config_document(path)
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        return build_result(
            rule,
            platform,
            CheckState.UNKNOWN,
            "The hook config file %s cannot be read:  %s" % (target, exc),
            ReasonCode.UNPARSEABLE,
            ObservedState(present=None),
            ctx,
        )

    event = rule.check.detail or DEFAULT_EVENT
    try:
        entries = hook_entries(document, event)
    except ValueError as exc:
        return build_result(
            rule,
            platform,
            CheckState.UNKNOWN,
            "The hook config file %s does not hold a readable %s list:  %s" % (target, event, exc),
            ReasonCode.UNPARSEABLE,
            ObservedState(present=None),
            ctx,
        )

    expected_names = tuple(rule.expected.names)
    found_keys = {hook_key(ident) for ident, _timeout in entries if ident}
    matched = [name for name in expected_names if hook_key(name) in found_keys]
    missing = [name for name in expected_names if hook_key(name) not in found_keys]

    if not entries:
        if rule.expected.present is False:
            return build_result(
                rule,
                platform,
                CheckState.PASS,
                "No %s hook is registered in %s, as the rule expects." % (event, target),
                ReasonCode.OK,
                ObservedState(present=False),
                ctx,
                proof_level_achieved=ProofLevel.L0,
            )
        return build_result(
            rule,
            platform,
            CheckState.MISSING,
            "No %s hook is registered in %s." % (event, target),
            ReasonCode.MISSING,
            ObservedState(present=False),
            ctx,
        )

    if rule.expected.present is False:
        return build_result(
            rule,
            platform,
            CheckState.DRIFT,
            "A %s hook is registered in %s, which the rule says must be absent." % (event, target),
            ReasonCode.DRIFT,
            ObservedState(present=True, names=tuple(sorted(matched))),
            ctx,
        )

    timeout = _timeout_of(entries, expected_names)
    observed = ObservedState(
        present=True,
        names=tuple(sorted(matched)),
        timeout_seconds=timeout,
    )

    if missing:
        return build_result(
            rule,
            platform,
            CheckState.DRIFT,
            "The %s hook %s is not registered in %s."
            % (event, ", ".join(repr(name) for name in missing), target),
            ReasonCode.DRIFT,
            observed,
            ctx,
        )

    if rule.expected.timeout_seconds is not None and timeout != rule.expected.timeout_seconds:
        return build_result(
            rule,
            platform,
            CheckState.DRIFT,
            "The %s hook in %s has a timeout of %s seconds, not the expected %s."
            % (
                event,
                target,
                "no" if timeout is None else str(timeout),
                str(rule.expected.timeout_seconds),
            ),
            ReasonCode.DRIFT,
            observed,
            ctx,
        )

    if matched:
        reason = "The %s hook %s is registered in %s." % (
            event,
            ", ".join(repr(name) for name in matched),
            target,
        )
    else:
        reason = "A %s hook is registered in %s." % (event, target)
    return build_result(
        rule,
        platform,
        CheckState.PASS,
        reason,
        ReasonCode.OK,
        observed,
        ctx,
        proof_level_achieved=ProofLevel.L0,
    )


def _timeout_of(
    entries: list[tuple[str, int | None]], expected_names: tuple[str, ...]
) -> int | None:
    """The timeout of the first entry that matches an expected name, else of the first entry."""
    wanted = {hook_key(name) for name in expected_names}
    for ident, timeout in entries:
        if ident and hook_key(ident) in wanted:
            return timeout
    for _ident, timeout in entries:
        return timeout
    return None


CHECKS = {
    CheckType.HOOK_REGISTERED: check_hook_registered,
}
