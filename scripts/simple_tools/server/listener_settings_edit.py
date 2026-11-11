#!/usr/bin/env python3
"""Change allowlisted settings in the server listener's live config, and nothing else.

The listener (the Eavesdropper container) reads `/data/listener.toml`.  Until now only a hand
edit could change it.  This module is the one writer a tool may use:

    get     the current value of every allowlisted setting (never the whole file)
    plan    what a set of changes would do:  the settings that change, the changed lines and the
            listener's own verdict on the result.  Writes nothing beside the file.
    apply   the same plan, then:  back up, replace atomically, reload, read back, and put the
            old bytes back if the listener refuses the reload.

What it promises:

  * Only a key the allowlist names can change, to a value of the type and range the allowlist
    gives.  The allowlist is `scripts/fleet-ops-listener-settings.json`;  `fleet-ops-control.py`
    reads it and passes it in with the request.
  * Some keys can never be allowlisted, whatever the allowlist says (HARD_DENY below):  a seat's
    `instance`, `creds`, `bot`, bearer and environment-variable names, the `[daemon]` instance and
    owner pins, and any key or sub-key whose name reads as a secret, a token, a path or a URL.
    A routine's `url_env` and `key_env` are variable NAMES;  they may only stay as they are or be
    one of the two names the seat's own code derives, so one seat's key can never be pointed at
    another seat's endpoint.  A string value that looks like a secret, a URL or a path is refused.
  * A table (`budget`, `routine`, `live`) is set whole.  A new value that would drop a sub-key
    the file has (a cap, a pause, a timeout) is refused unless the request names that sub-key in
    `remove`, so nothing is dropped silently and a plan lists what goes (`removes`).
  * The edit is made on the text, so comments and every other line stay byte for byte.  The result
    is then parsed and compared with the original:  the settings that differ must be exactly the
    requested ones, with exactly the requested values, or nothing is written (`edit_unsafe`).
  * The candidate is judged by the daemon's own `check-config` before the live file is touched.
    A candidate it would not take is deleted and the live file is never opened for writing.
  * The write is a temporary file in the same folder, fsync, then one rename.  The old file is
    first copied to `<file>.bak-settings-<UTC stamp>` (mode 600).
  * After the rename the listener is told to reload (SIGHUP) and its status is read back.  A
    refused reload, or a reload that cannot be sent, puts the old bytes back the same atomic way
    and reloads again.  The file on disk and the config the listener runs then agree.
  * One edit at a time:  an exclusive lock on the file's folder, refused at once when held.
  * `apply` is off unless the allowlist says `"applyEnabled": true`.

The request is one JSON document on standard input (`--request-stdin`) and the answer is one JSON
document on standard output.  Exit status:  0 done, planned or unchanged;  1 a step failed and the
old file is back;  2 refused.

Only the standard library is used, and nothing is imported from `simple_tools`, so the file can be
run with `python3 -I` and loaded from its bytes by `fleet-ops-control.py`.  The daemon's check and
reload run in a child process (`python3 -I -c ...` on this checkout's `simple_tools.cli`), which is
the same code `simple-chat daemon check-config` and `simple-chat daemon reload` run.
"""

from __future__ import annotations

import datetime as _dt
import difflib
import fcntl
import hashlib
import json
import math
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
import tomllib
from pathlib import Path

TOOL = "listener-settings"
SCHEMA_VERSION = 1
DEFAULT_LIVE = "/data/listener.toml"
ENV_CONFIG = "SIMPLE_TOOLS_CONFIG"
ENV_STATE_DIR = "SIMPLE_TOOLS_STATE_DIR"
FILE_MAX_BYTES = 1024 * 1024
REQUEST_MAX_BYTES = 64 * 1024
MAX_CHANGES = 32
MAX_REMOVALS = 64
BACKUP_INFIX = ".bak-settings-"
BACKUPS_KEPT = 20
CHECK_TIMEOUT = 60
RELOAD_TIMEOUT = 30
RELOAD_READBACK_SECONDS = 30.0
RELOAD_POLL_SECONDS = 0.5
STATEMENT_MAX_LINES = 400

# `python3 -I` drops the script folder and every PYTHON* variable, so the one import path is the
# argument this module passes:  the `scripts` folder of its own checkout.  SIMPLE_CHAT_REMOTE is
# removed from the child's environment because these two verbs only ever act on this machine.
_CLI_SHIM = (
    "import sys; sys.path.insert(0, sys.argv[1]); from simple_tools import cli; "
    "sys.exit(cli.main(sys.argv[2:]))"
)
_REMOTE_ENV = ("SIMPLE_CHAT_REMOTE", "SIMPLE_CHAT_REMOTE_WRITES")

SEAT_RE = re.compile(r"[A-Z][A-Z0-9-]{0,39}")
NAME_RE = re.compile(r"[a-z][a-z0-9_]{0,47}")
BARE_KEY_RE = re.compile(r"[A-Za-z0-9_-]+")
ENV_NAME_RE = re.compile(r"[A-Z][A-Z0-9_]{0,95}")
SHA256_RE = re.compile(r"[0-9a-f]{64}")
REMOVAL_RE = re.compile(r"[A-Za-z0-9_-]{1,48}(\.[A-Za-z0-9_-]{1,48}){2,3}")
_ASSIGN_RE = re.compile(r"^(\s*)([A-Za-z0-9_-]+)\s*=")
_HEADER_LINE_RE = re.compile(r"^\s*\[")

SPEC_TYPES = ("bool", "int", "number", "enum", "string", "table", "pairs", "seat-env")

# Never allowlisted, whatever the allowlist file says.  A table name maps to the keys in it;
# "seat" stands for every `[seat.X]`.
HARD_DENY = {
    "seat": frozenset(
        {
            "instance",
            "creds",
            "bot",
            "bearer_file",
            "bearer_locator",
            "allow_admin",
            "email_env",
            "key_env",
            "site_env",
        }
    ),
    "daemon": frozenset(
        {
            "instance",
            "owner_user_id",
            "eligible_user_ids",
            "owner_clients",
            "outgoing_webhook_seats",
        }
    ),
}
# A key or sub-key name that reads as a secret, a token, a credential, a path or a URL.
_DENY_NAME_RE = re.compile(
    r"(secret|token|passw|credential|creds|bearer|private|locator|api_?key|url|"
    r"(^|_)key$|(^|_)keys$|_env$|_file$|_path$|_dir$)",
    re.IGNORECASE,
)
# A string value that looks like a secret, a URL or a path.  The live file holds names and
# settings, never values of that kind.
_SECRET_PREFIXES = (
    "sk-",
    "sk_",
    "crsr_",
    "lin_api_",
    "lin_oauth_",
    "ghp_",
    "gho_",
    "ghs_",
    "github_pat_",
    "xoxb-",
    "xoxp-",
    "eyJ",
    "-----BEGIN",
)
_TOKENISH_RE = re.compile(r"[A-Za-z0-9+/=_\-.]{24,}")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")


class Refusal(Exception):
    """The request is not carried out.  `code` is a fixed word a caller can switch on."""

    def __init__(self, code, detail, check=None):
        super().__init__(detail)
        self.code = code
        self.detail = detail
        # The listener's verdict, when that is what refused it.
        self.check = check


class StepFailed(Exception):
    """A step after the write failed and the old file was put back."""

    def __init__(self, code, detail, rolled_back):
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.rolled_back = rolled_back


# MARK: - The allowlist


def _check_spec(spec, where, nested=False):
    if not isinstance(spec, dict) or spec.get("type") not in SPEC_TYPES:
        raise Refusal(
            "allowlist_invalid", "%s needs a type from %s" % (where, ", ".join(SPEC_TYPES))
        )
    kind = spec["type"]
    known = {"type"}
    if kind in ("int", "number"):
        known |= {"min", "max"}
        for bound in ("min", "max"):
            value = spec.get(bound)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise Refusal("allowlist_invalid", "%s needs a numeric %s" % (where, bound))
            if isinstance(value, float) and not math.isfinite(value):
                raise Refusal(
                    "allowlist_invalid", "%s has a %s that is not finite" % (where, bound)
                )
        if spec["min"] > spec["max"]:
            raise Refusal("allowlist_invalid", "%s has min above max" % where)
    elif kind == "enum":
        known |= {"values"}
        values = spec.get("values")
        if (
            not isinstance(values, list)
            or not values
            or not all(isinstance(v, str) and v for v in values)
        ):
            raise Refusal("allowlist_invalid", "%s needs a list of string values" % where)
    elif kind == "string":
        known |= {"pattern", "maxLength"}
        pattern = spec.get("pattern")
        if not isinstance(pattern, str) or not pattern:
            raise Refusal("allowlist_invalid", "%s needs a pattern" % where)
        try:
            re.compile(pattern)
        except re.error:
            raise Refusal("allowlist_invalid", "%s has a pattern that does not compile" % where)
        limit = spec.get("maxLength")
        if type(limit) is not int or not 0 <= limit <= 256:
            raise Refusal("allowlist_invalid", "%s needs a maxLength of 0 to 256" % where)
    elif kind == "pairs":
        known |= {"maxItems", "maxLength"}
        for bound in ("maxItems", "maxLength"):
            if type(spec.get(bound)) is not int or not 1 <= spec[bound] <= 256:
                raise Refusal("allowlist_invalid", "%s needs a %s of 1 to 256" % (where, bound))
    elif kind == "table":
        known |= {"keys"}
        if nested:
            raise Refusal("allowlist_invalid", "%s is a table inside a table" % where)
        keys = spec.get("keys")
        if not isinstance(keys, dict) or not keys:
            raise Refusal("allowlist_invalid", "%s needs its keys" % where)
        for name, sub in keys.items():
            if not isinstance(name, str) or not NAME_RE.fullmatch(name):
                raise Refusal(
                    "allowlist_invalid", "%s has a sub-key name that is not allowed" % where
                )
            _check_spec(sub, "%s.%s" % (where, name), nested=True)
            if sub["type"] != "seat-env" and _DENY_NAME_RE.search(name):
                raise Refusal(
                    "allowlist_invalid",
                    "%s.%s reads as a secret, a path or a URL and can never be allowlisted"
                    % (where, name),
                )
    elif kind == "seat-env":
        if not nested:
            raise Refusal("allowlist_invalid", "%s:  seat-env is for a sub-key only" % where)
    unknown = sorted(set(spec) - known)
    if unknown:
        raise Refusal(
            "allowlist_invalid", "%s has unknown field(s): %s" % (where, ", ".join(unknown))
        )


def validate_allowlist(doc):
    """The allowlist as `{"applyEnabled": bool, "seat": {key: spec}, "tables": {table: {key: spec}}}`,
    or Refusal(`allowlist_invalid`).  A key HARD_DENY names, or one whose name reads as a secret,
    refuses the whole allowlist:  a file that asks for one is wrong, and is not half trusted."""
    if not isinstance(doc, dict) or doc.get("schemaVersion") != SCHEMA_VERSION:
        raise Refusal("allowlist_invalid", "the allowlist needs schemaVersion %d" % SCHEMA_VERSION)
    unknown = sorted(set(doc) - {"schemaVersion", "applyEnabled", "seat", "tables", "notes"})
    if unknown:
        raise Refusal(
            "allowlist_invalid", "the allowlist has unknown field(s): %s" % ", ".join(unknown)
        )
    if not isinstance(doc.get("applyEnabled"), bool):
        raise Refusal("allowlist_invalid", "applyEnabled must be true or false")
    seat = doc.get("seat")
    tables = doc.get("tables")
    if not isinstance(seat, dict) or not isinstance(tables, dict):
        raise Refusal("allowlist_invalid", "the allowlist needs seat and tables")
    if "seat" in tables:
        raise Refusal("allowlist_invalid", "seat keys go under seat, not tables")
    groups = [("seat", seat)]
    for table, keys in tables.items():
        if not isinstance(table, str) or not NAME_RE.fullmatch(table) or not isinstance(keys, dict):
            raise Refusal("allowlist_invalid", "a table in the allowlist is not allowed")
        groups.append((table, keys))
    for table, keys in groups:
        for name, spec in keys.items():
            where = "%s.%s" % ("seat.*" if table == "seat" else table, name)
            if not isinstance(name, str) or not NAME_RE.fullmatch(name):
                raise Refusal("allowlist_invalid", "%s is not a key name that is allowed" % where)
            if name in HARD_DENY.get(table, ()) or _DENY_NAME_RE.search(name):
                raise Refusal("allowlist_invalid", "%s can never be allowlisted" % where)
            _check_spec(spec, where)
    return {"applyEnabled": doc["applyEnabled"], "seat": dict(seat), "tables": dict(tables)}


# MARK: - Keys and values


def split_key(key):
    """`seat.GB-FIXER.enabled` -> (("seat", "GB-FIXER"), "enabled");  `daemon.x` -> (("daemon",), "x").
    Anything else is refused:  there is no deeper path, no quoting and no top-level key."""
    if not isinstance(key, str) or len(key) > 160:
        raise Refusal("key_not_allowed", "a setting is named table.key or seat.SEAT.key")
    parts = key.split(".")
    if len(parts) == 3 and parts[0] == "seat":
        if not SEAT_RE.fullmatch(parts[1]) or not NAME_RE.fullmatch(parts[2]):
            raise Refusal("key_not_allowed", "%s is not a seat setting name" % key)
        return ("seat", parts[1]), parts[2]
    if len(parts) == 2 and parts[0] != "seat":
        if not NAME_RE.fullmatch(parts[0]) or not NAME_RE.fullmatch(parts[1]):
            raise Refusal("key_not_allowed", "%s is not a setting name" % key)
        return (parts[0],), parts[1]
    raise Refusal("key_not_allowed", "%s:  a setting is named table.key or seat.SEAT.key" % key)


def env_code(seat):
    """GB-COMPILER -> GB_COMPILER, as the listener's config derives it."""
    return re.sub(r"[^A-Z0-9]", "_", seat.upper())


def seat_env_names(seat, sub):
    """The variable names a routine sub-key may take for this seat:  the listener's default and the
    webhook name Infisical holds for a Grok or BotFleet bot."""
    code = env_code(seat)
    if sub == "url_env":
        return (code + "_ROUTINE_URL", "ZULIP_ALERT_%s_ENDPOINT" % code)
    if sub == "key_env":
        return (code + "_ROUTINE_KEY", "ZULIP_ALERT_%s_KEY" % code)
    return ()


def _secret_like(text):
    if any(text.startswith(prefix) for prefix in _SECRET_PREFIXES):
        return True
    if "://" in text or text.startswith(("/", "~", "\\")) or "/../" in text:
        return True
    if ENV_NAME_RE.fullmatch(text):
        return False
    if not _TOKENISH_RE.fullmatch(text):
        return False
    # A key, not a name:  one unbroken run of 20 or more letters and digits that mixes the two, or
    # a long value that mixes upper case, lower case and digits.  Words joined by `-`, `_` or `.`
    # (`daily-standup-2026-11-11`, `X-Simple-Tools-Signature-256-V2`) are names.
    for run in re.findall(r"[A-Za-z0-9]{20,}", text):
        if re.search(r"[0-9]", run) and re.search(r"[A-Za-z]", run):
            return True
    return len(text) >= 32 and all(re.search(p, text) for p in (r"[a-z]", r"[A-Z]", r"[0-9]"))


def _check_string(text, where):
    if _CONTROL_RE.search(text):
        raise Refusal("value_not_allowed", "%s has a control character" % where)
    if _secret_like(text):
        raise Refusal(
            "secret_like_value",
            "%s looks like a secret, a URL or a path;  the file holds names and settings only"
            % where,
        )


def same(a, b):
    """Equal in value AND in type, all the way down:  `1`, `1.0` and `true` are three values."""
    if type(a) is not type(b):
        return False
    if isinstance(a, dict):
        return a.keys() == b.keys() and all(same(a[k], b[k]) for k in a)
    if isinstance(a, list):
        return len(a) == len(b) and all(same(x, y) for x, y in zip(a, b))
    return a == b


def check_value(spec, value, where, seat=None, current=None, sub=None):
    """Refusal unless `value` is what `spec` allows.  `current` is the value the file has now (for
    a seat-env sub-key, which may always stay as it is)."""
    kind = spec["type"]
    if kind == "bool":
        if not isinstance(value, bool):
            raise Refusal("value_not_allowed", "%s must be true or false" % where)
    elif kind in ("int", "number"):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise Refusal("value_not_allowed", "%s must be a number" % where)
        if kind == "int" and not isinstance(value, int):
            raise Refusal("value_not_allowed", "%s must be a whole number" % where)
        if isinstance(value, float) and not math.isfinite(value):
            raise Refusal("value_not_allowed", "%s must be a finite number" % where)
        if not spec["min"] <= value <= spec["max"]:
            raise Refusal(
                "value_not_allowed",
                "%s must be between %s and %s" % (where, spec["min"], spec["max"]),
            )
    elif kind == "enum":
        if not isinstance(value, str) or value not in spec["values"]:
            raise Refusal(
                "value_not_allowed", "%s must be one of %s" % (where, ", ".join(spec["values"]))
            )
    elif kind == "string":
        if not isinstance(value, str) or len(value) > spec["maxLength"]:
            raise Refusal("value_not_allowed", "%s must be a short string" % where)
        _check_string(value, where)
        if not re.fullmatch(spec["pattern"], value):
            raise Refusal("value_not_allowed", "%s does not have the form it must have" % where)
    elif kind == "pairs":
        if not isinstance(value, list) or len(value) > spec["maxItems"]:
            raise Refusal("value_not_allowed", "%s must be a list of pairs" % where)
        for pair in value:
            if (
                not isinstance(pair, list)
                or len(pair) != 2
                or not all(isinstance(p, str) and 0 < len(p) <= spec["maxLength"] for p in pair)
            ):
                raise Refusal(
                    "value_not_allowed", "%s must be a list of [channel, topic] pairs" % where
                )
            for part in pair:
                _check_string(part, where)
    elif kind == "seat-env":
        if not isinstance(value, str) or not ENV_NAME_RE.fullmatch(value):
            raise Refusal("value_not_allowed", "%s must be an environment variable name" % where)
        allowed = seat_env_names(seat or "", sub or "")
        if value not in allowed and not (isinstance(current, str) and value == current):
            raise Refusal(
                "env_name_not_allowed",
                "%s may stay as it is or be one of %s" % (where, ", ".join(allowed) or "(none)"),
            )
    elif kind == "table":
        if not isinstance(value, dict):
            raise Refusal("value_not_allowed", "%s must be a table" % where)
        now = current if isinstance(current, dict) else {}
        for name in sorted(value, key=str):
            if not isinstance(name, str) or name not in spec["keys"]:
                raise Refusal("key_not_allowed", "%s.%s is not on the allowlist" % (where, name))
            check_value(
                spec["keys"][name],
                value[name],
                "%s.%s" % (where, name),
                seat=seat,
                current=now.get(name),
                sub=name,
            )


def spec_for(allowlist, table, name):
    """The allowlist's spec for one setting, or Refusal(`key_not_allowed`)."""
    group = "seat" if table[0] == "seat" else table[0]
    if name in HARD_DENY.get(group, ()) or _DENY_NAME_RE.search(name):
        raise Refusal(
            "key_never_allowed",
            "%s.%s is a pin, a credential name or reads as a secret;  no tool changes it"
            % (".".join(table), name),
        )
    keys = allowlist["seat"] if group == "seat" else allowlist["tables"].get(group)
    if not isinstance(keys, dict) or name not in keys:
        raise Refusal("key_not_allowed", "%s.%s is not on the allowlist" % (".".join(table), name))
    return keys[name]


def render(value):
    """`value` as one line of TOML."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise Refusal("value_not_allowed", "a number must be finite")
        text = repr(value)
        return text if any(c in text for c in ".e") else text + ".0"
    if isinstance(value, str):
        if _CONTROL_RE.search(value):
            raise Refusal("value_not_allowed", "a string has a control character")
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        return "[" + ", ".join(render(item) for item in value) + "]"
    if isinstance(value, dict):
        parts = []
        for name, item in value.items():
            if not isinstance(name, str) or not BARE_KEY_RE.fullmatch(name):
                raise Refusal("value_not_allowed", "a table key must be a bare name")
            parts.append("%s = %s" % (name, render(item)))
        return "{ " + ", ".join(parts) + " }" if parts else "{}"
    raise Refusal("value_not_allowed", "a value of that kind cannot be written")


def parse_assignment(text):
    """`seat.ZC.enabled=false` -> ("seat.ZC.enabled", False).  The value is read as TOML, so a string
    needs its quotes and a table is `{ a = 1 }`."""
    if not isinstance(text, str) or "=" not in text:
        raise Refusal("usage", "--set takes KEY=VALUE")
    key, _, raw = text.partition("=")
    key = key.strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,160}", key):
        raise Refusal("key_not_allowed", "a setting is named table.key or seat.SEAT.key")
    if "\n" in raw or "\r" in raw or len(raw) > 4096:
        raise Refusal("value_not_allowed", "the value of %s must be one short line of TOML" % key)
    try:
        return key, tomllib.loads("v = " + raw)["v"]
    except (tomllib.TOMLDecodeError, KeyError, RecursionError):
        raise Refusal("value_not_allowed", "the value of %s is not TOML" % key) from None


# MARK: - Reading the text


def _table_path(doc):
    """The path a header line names, from its parsed form:  {"seat": {"ZC": {}}} -> ("seat", "ZC").
    None for an array of tables or anything that is not one empty table."""
    path = []
    while isinstance(doc, dict) and len(doc) == 1:
        ((name, doc),) = doc.items()
        path.append(name)
    return tuple(path) if doc == {} and path else None


def scan(text):
    """The statements of a TOML text, in order.  Each is a dict:

        {"kind": "header", "table": path or None, "start": i, "end": j}
        {"kind": "pair", "table": path, "key": bare key or None, "start": i, "end": j}

    `start` and `end` are line indexes (end exclusive).  A value that runs over several lines is
    one statement, found by asking the TOML parser when the statement is complete, so a line inside
    a multi-line array or string is never mistaken for a header or a key.  `key` is None for a
    dotted or quoted key, which this editor does not rewrite."""
    lines = text.splitlines(keepends=True)
    found = []
    table = ()
    index = 0
    while index < len(lines):
        stripped = lines[index].strip()
        if not stripped or stripped.startswith("#"):
            index += 1
            continue
        if _HEADER_LINE_RE.match(lines[index]):
            try:
                path = _table_path(tomllib.loads(lines[index]))
            except (tomllib.TOMLDecodeError, RecursionError):
                raise Refusal(
                    "file_not_toml", "line %d is not a table header" % (index + 1)
                ) from None
            table = path
            found.append({"kind": "header", "table": path, "start": index, "end": index + 1})
            index += 1
            continue
        end = index + 1
        while True:
            try:
                tomllib.loads("".join(lines[index:end]))
                break
            except (tomllib.TOMLDecodeError, RecursionError):
                if end >= len(lines) or end - index >= STATEMENT_MAX_LINES:
                    raise Refusal(
                        "file_not_toml",
                        "the setting that starts on line %d does not end" % (index + 1),
                    ) from None
                end += 1
        match = _ASSIGN_RE.match(lines[index])
        found.append(
            {
                "kind": "pair",
                "table": table,
                "key": match.group(2) if match else None,
                "start": index,
                "end": end,
            }
        )
        index = end
    return lines, found


def _trailing_comment(line):
    """(the gap and comment at the end of a one-line statement, or "").  A `#` starts the comment
    only where cutting the line there leaves the same parsed value, so a `#` inside a string does not."""
    body = line.rstrip("\r\n")
    try:
        whole = tomllib.loads(body)
    except (tomllib.TOMLDecodeError, RecursionError):
        return ""
    position = body.find("#")
    while position != -1:
        try:
            if same(tomllib.loads(body[:position]), whole):
                head = body[:position]
                return body[len(head.rstrip()) :]
        except (tomllib.TOMLDecodeError, RecursionError):
            pass
        position = body.find("#", position + 1)
    return ""


def _line_end(lines):
    for line in lines:
        if line.endswith("\r\n"):
            return "\r\n"
        if line.endswith("\n"):
            return "\n"
    return "\n"


def edit_text(text, changes):
    """The text with each (table, name, value) set.  A key that is there is rewritten where it
    stands (its trailing comment kept);  a missing one is added after the table's last setting;  a
    settings table the file lacks is appended.  A seat the file does not have is refused:  this
    editor changes settings, it does not create seats."""
    for table, name, value in changes:
        lines, found = scan(text)
        newline = _line_end(lines)
        rendered = "%s = %s" % (name, render(value))
        mine = [s for s in found if s["table"] == table]
        pairs = [s for s in mine if s["kind"] == "pair" and s["key"] == name]
        if len(pairs) > 1:
            raise Refusal("edit_unsafe", "%s.%s is set more than once" % (".".join(table), name))
        if pairs:
            statement = pairs[0]
            first = lines[statement["start"]]
            indent = _ASSIGN_RE.match(first).group(1)
            comment = _trailing_comment(first) if statement["end"] - statement["start"] == 1 else ""
            ending = newline if lines[statement["end"] - 1].endswith(("\n", "\r\n")) else ""
            lines[statement["start"] : statement["end"]] = [indent + rendered + comment + ending]
        elif mine:
            last = max(s["end"] for s in mine)
            if not lines[last - 1].endswith(("\n", "\r\n")):
                lines[last - 1] += newline
            lines.insert(last, rendered + newline)
        elif table[0] == "seat":
            raise Refusal("unknown_seat", "the file has no [%s] section" % ".".join(table))
        else:
            if lines and not lines[-1].endswith(("\n", "\r\n")):
                lines[-1] += newline
            if lines and lines[-1].strip():
                lines.append(newline)
            lines.append("[%s]%s" % (".".join(table), newline))
            lines.append(rendered + newline)
        text = "".join(lines)
    return text


def settings_of(doc):
    """Every setting of a parsed config as {(table path, key): value}.  A seat's settings are its
    section's keys;  any other table's are its keys;  a top-level value is under the empty path."""
    found = {}
    for name, value in doc.items():
        if name == "seat" and isinstance(value, dict):
            for seat, section in value.items():
                if isinstance(section, dict):
                    for key, item in section.items():
                        found[(("seat", seat), key)] = item
                else:
                    found[(("seat",), seat)] = section
        elif isinstance(value, dict):
            for key, item in value.items():
                found[((name,), key)] = item
        else:
            found[((), name)] = value
    return found


def _parse(text, what):
    try:
        return tomllib.loads(text)
    except (tomllib.TOMLDecodeError, RecursionError) as exc:
        raise Refusal("file_not_toml", "%s is not valid TOML: %s" % (what, exc)) from None


def _key_text(table, name):
    return ".".join(table + (name,))


def _removal_names(remove):
    """The `remove` list of a request as a set of `table.key.subkey` names, or Refusal."""
    if remove is None:
        return set()
    if (
        not isinstance(remove, list)
        or len(remove) > MAX_REMOVALS
        or not all(isinstance(r, str) and REMOVAL_RE.fullmatch(r) for r in remove)
        or len(set(remove)) != len(remove)
    ):
        raise Refusal("usage", "remove is a short list of table.key.subkey names, each once")
    return set(remove)


def build_plan(text, allowlist, requested, remove=None):
    """(new text, changes) for a `{key: value}` request against the file's text.

    Every key is checked against the allowlist and every value against its spec first.  Then the
    text is edited, parsed, and compared with the original:  the settings that differ must be
    exactly the requested ones that are not already as asked, each with exactly the asked value.
    `changes` lists them in key order as {"key", "set" (was it there), "from", "to", "removes"}.

    A table (`budget`, `routine`, `live`) is set whole, so a new value that leaves out a sub-key
    the file has would drop it:  a cap, a pause (`wakes_per_day = 0`), a timeout.  That is never
    silent.  Each sub-key the new value drops must be named in `remove` as `table.key.subkey`, or
    the request is refused (`would_remove_keys`);  a name in `remove` that the request does not
    drop is refused too, so a plan says exactly what goes."""
    if not isinstance(requested, dict) or not requested:
        raise Refusal("usage", "name at least one setting to change")
    if len(requested) > MAX_CHANGES:
        raise Refusal("usage", "at most %d settings in one request" % MAX_CHANGES)
    removals = _removal_names(remove)
    dropped = {}
    before = _parse(text, "the live file")
    have = settings_of(before)
    wanted = []
    for key in sorted(requested):
        table, name = split_key(key)
        spec = spec_for(allowlist, table, name)
        value = requested[key]
        seat = table[1] if table[0] == "seat" else None
        if seat is not None and not isinstance((before.get("seat") or {}).get(seat), dict):
            raise Refusal("unknown_seat", "the file has no [seat.%s] section" % seat)
        current = have.get((table, name))
        check_value(spec, value, key, seat=seat, current=current)
        if isinstance(value, dict) and isinstance(current, dict):
            gone = sorted("%s.%s" % (key, sub) for sub in set(current) - set(value))
            unnamed = [one for one in gone if one not in removals]
            if unnamed:
                raise Refusal(
                    "would_remove_keys",
                    "%s is set whole, and this value would drop %s;  send each one, or name it in "
                    "remove to drop it" % (key, ", ".join(unnamed)),
                )
            dropped[(table, name)] = gone
        wanted.append((table, name, value))
    stray = sorted(removals - {one for names in dropped.values() for one in names})
    if stray:
        raise Refusal(
            "usage", "remove names %s, which this request does not drop" % ", ".join(stray)
        )
    todo = [w for w in wanted if (w[0], w[1]) not in have or not same(have[(w[0], w[1])], w[2])]
    if not todo:
        return text, []
    new_text = edit_text(text, todo)
    after = settings_of(_parse(new_text, "the edited file"))
    expected = {(table, name): value for table, name, value in todo}
    differing = {
        key
        for key in set(have) | set(after)
        if key not in have or key not in after or not same(have[key], after[key])
    }
    if differing != set(expected) or any(not same(after[k], v) for k, v in expected.items()):
        raise Refusal(
            "edit_unsafe",
            "the edit would change something other than the requested settings;  nothing was written",
        )
    changes = [
        {
            "key": _key_text(table, name),
            "set": (table, name) in have,
            "from": have.get((table, name)),
            "to": value,
            "removes": dropped.get((table, name), []),
        }
        for table, name, value in todo
    ]
    return new_text, changes


def changed_lines(old, new):
    """The changed lines only (a unified diff with no context), so a line that did not change,
    such as an owner pin or a variable name, is never repeated in an answer."""
    lines = difflib.unified_diff(
        old.splitlines(), new.splitlines(), "listener.toml", "listener.toml", n=0, lineterm=""
    )
    return list(lines)


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def plan_hash(base_sha, changes):
    """One hash over what a plan is:  the bytes it starts from and the changes it makes.  An apply
    that names it does exactly what the plan showed, or nothing."""
    canonical = json.dumps(
        {"base": base_sha, "changes": changes},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return sha256(canonical.encode("utf-8"))


def allowlisted_values(doc, allowlist):
    """{key: value} for every allowlisted setting the file sets, and nothing else of the file."""
    found = {}
    for (table, name), value in settings_of(doc).items():
        if not table:
            continue
        group = "seat" if table[0] == "seat" else table[0]
        keys = allowlist["seat"] if group == "seat" else allowlist["tables"].get(group, {})
        if len(table) == (2 if group == "seat" else 1) and name in keys:
            found[_key_text(table, name)] = value
    return dict(sorted(found.items()))


# MARK: - The file


def read_live(path):
    """(bytes, stat) of the live file, opened without following a link, or Refusal."""
    try:
        fd = os.open(
            str(path), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        )
    except OSError:
        raise Refusal("file_unavailable", "the live file cannot be opened") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > FILE_MAX_BYTES:
            raise Refusal("file_unavailable", "the live file is not a regular file of a sane size")
        chunks = []
        total = 0
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            total += len(chunk)
            if total > FILE_MAX_BYTES:
                raise Refusal("file_unavailable", "the live file is too large")
            chunks.append(chunk)
        return b"".join(chunks), info
    finally:
        os.close(fd)


def _fsync_dir(directory):
    fd = os.open(str(directory), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_temp(directory, prefix, data, mode, owner=None):
    """A new file beside the live one holding `data`, flushed to disk.  Its path."""
    fd, tmp = tempfile.mkstemp(prefix=prefix, dir=str(directory))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            os.fchmod(handle.fileno(), mode)
            if owner is not None:
                try:
                    os.fchown(handle.fileno(), owner[0], owner[1])
                except OSError:
                    pass
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return tmp


def _replace(path, data, info):
    """Put `data` in place of the file at `path` with one rename, keeping its mode and owner."""
    tmp = _write_temp(
        path.parent, ".%s.new-" % path.name, data, info.st_mode & 0o777, (info.st_uid, info.st_gid)
    )
    try:
        os.replace(tmp, str(path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    _fsync_dir(path.parent)


def _prune_backups(path):
    prefix = path.name + BACKUP_INFIX
    try:
        names = sorted(n for n in os.listdir(str(path.parent)) if n.startswith(prefix))
    except OSError:
        return
    for name in names[:-BACKUPS_KEPT]:
        try:
            os.unlink(str(path.parent / name))
        except OSError:
            pass


class _FolderLock:
    """An exclusive, non-blocking lock on the folder that holds the live file.  It leaves no file
    behind and is released when the descriptor closes, however the process ends."""

    def __init__(self, directory):
        self.directory = directory
        self.fd = None

    def __enter__(self):
        try:
            self.fd = os.open(str(self.directory), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        except OSError:
            raise Refusal("file_unavailable", "the live file's folder cannot be opened") from None
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(self.fd)
            self.fd = None
            raise Refusal("busy", "another settings edit is running") from None
        return self

    def __exit__(self, *exc):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        return False


# MARK: - The daemon's check and reload


def _scripts_dir():
    """The `scripts` folder of this checkout (this file is scripts/simple_tools/server/x.py)."""
    here = globals().get("__file__")
    return str(Path(here).resolve().parent.parent.parent) if here else None


def _child_env(live_path, environ=None):
    env = {
        k: v
        for k, v in (os.environ if environ is None else environ).items()
        if k not in _REMOTE_ENV
    }
    env[ENV_CONFIG] = str(live_path)
    return env


def _cli(scripts_dir, args, env, timeout):
    """(exit status, stdout) of this checkout's `simple-chat` verb, run without the launcher."""
    if not scripts_dir or not os.path.isdir(os.path.join(scripts_dir, "simple_tools")):
        return 127, ""
    try:
        done = subprocess.run(  # noqa: S603 - an argv list, never a shell
            [sys.executable, "-I", "-c", _CLI_SHIM, scripts_dir] + list(args),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=env,
            cwd="/",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return 124, ""
    except OSError:
        return 127, ""
    return done.returncode, done.stdout.decode("utf-8", "replace")


def daemon_check(candidate, live_path, scripts_dir=None, environ=None):
    """The listener's own verdict on a candidate:  `{"ok", "problems", "warnings"}`.

    This is `simple-chat daemon check-config --file CANDIDATE --json` with the live file as the
    current one.  `ok` is the daemon's `ok`.  `problems` are its parse error, partition refusals
    and the errors the candidate adds.  A check that cannot run is not ok (`check_unavailable`)."""
    status, out = _cli(
        scripts_dir or _scripts_dir(),
        ["daemon", "check-config", "--file", str(candidate), "--json"],
        _child_env(live_path, environ),
        CHECK_TIMEOUT,
    )
    try:
        answer = json.loads(out)
    except ValueError:
        answer = None
    if (
        status not in (0, 1)
        or not isinstance(answer, dict)
        or not isinstance(answer.get("ok"), bool)
    ):
        return {"ok": False, "problems": ["check_unavailable"], "warnings": 0}
    problems = []
    if answer.get("parse_error"):
        problems.append(str(answer["parse_error"]))
    for name in ("refused", "new_errors"):
        problems += [str(p) for p in answer.get(name) or [] if str(p) not in problems]
    ok = answer["ok"] and status == 0
    if not ok and not problems:
        problems.append("check_failed")
    return {"ok": ok, "problems": problems[:20], "warnings": len(answer.get("warnings") or [])}


def _status_path(environ=None):
    env = os.environ if environ is None else environ
    root = env.get(ENV_STATE_DIR) or os.path.join(os.path.expanduser("~"), ".simple-tools")
    return os.path.join(root, "listener", "status.json")


def daemon_reload(live_path, scripts_dir=None, environ=None, clock=time.time, sleep=time.sleep):
    """Tell the listener to re-read its config and read back what it made of it.

    `{"ok", "confirmed", "code"}`.  `ok` False with `reload_not_sent` when the SIGHUP could not be
    sent (no listener running), or `reload_refused` when the listener's status says it refused the
    file.  `confirmed` is True once status.json shows a reload at or after the signal with nothing
    refused;  a status that does not move inside the window is ok but not confirmed."""
    started = clock()
    status, _ = _cli(
        scripts_dir or _scripts_dir(),
        ["daemon", "reload"],
        _child_env(live_path, environ),
        RELOAD_TIMEOUT,
    )
    if status != 0:
        return {"ok": False, "confirmed": False, "code": "reload_not_sent"}
    path = _status_path(environ)
    deadline = started + RELOAD_READBACK_SECONDS
    while True:
        try:
            with open(path, "rb") as handle:
                document = json.loads(handle.read(4 * 1024 * 1024).decode("utf-8"))
        except (OSError, ValueError):
            document = None
        if isinstance(document, dict):
            at = document.get("reload_at")
            if isinstance(at, (int, float)) and not isinstance(at, bool) and at >= started - 1:
                if document.get("reload_refused"):
                    return {"ok": False, "confirmed": True, "code": "reload_refused"}
                return {"ok": True, "confirmed": True, "code": "reloaded"}
        if clock() >= deadline:
            return {"ok": True, "confirmed": False, "code": "reload_unconfirmed"}
        sleep(RELOAD_POLL_SECONDS)


# MARK: - The three verbs


def _is_daemon_file(path, environ=None):
    """True when `path` is the file the listener on this machine reads (SIMPLE_TOOLS_CONFIG)."""
    env = os.environ if environ is None else environ
    named = env.get(ENV_CONFIG)
    if not named:
        return False
    try:
        return os.path.realpath(named) == os.path.realpath(str(path))
    except OSError:
        return False


def _answer(action, path, **fields):
    answer = {"schemaVersion": SCHEMA_VERSION, "tool": TOOL, "action": action, "file": path.name}
    answer.update(fields)
    return answer


def _decode(data):
    try:
        return data.decode("utf-8", "strict")
    except UnicodeDecodeError:
        raise Refusal("file_not_toml", "the live file is not UTF-8") from None


def get(path, allowlist):
    data, _ = read_live(path)
    doc = _parse(_decode(data), "the live file")
    return _answer(
        "get",
        path,
        ok=True,
        result="read",
        baseSHA256=sha256(data),
        applyEnabled=allowlist["applyEnabled"],
        settings=allowlisted_values(doc, allowlist),
    )


def _planned(action, path, data, new_text, changes, check):
    base = sha256(data)
    return _answer(
        action,
        path,
        ok=True,
        result="planned",
        baseSHA256=base,
        newSHA256=sha256(new_text.encode("utf-8")),
        planSHA256=plan_hash(base, changes),
        changes=changes,
        diff=changed_lines(_decode(data), new_text),
        check=check,
    )


def plan(path, allowlist, requested, checker=None, remove=None):
    """What the changes would do.  The candidate the checker reads is written to a private
    temporary folder, never beside the live file, and is gone before this returns."""
    data, _ = read_live(path)
    new_text, changes = build_plan(_decode(data), allowlist, requested, remove)
    if not changes:
        answer = _planned(
            "plan", path, data, new_text, changes, {"ok": True, "problems": [], "warnings": 0}
        )
        answer["result"] = "unchanged"
        return answer
    checker = checker or daemon_check
    with tempfile.TemporaryDirectory(prefix="listener-settings-") as scratch:
        candidate = os.path.join(scratch, "candidate.toml")
        with open(candidate, "w", encoding="utf-8", newline="") as handle:
            handle.write(new_text)
        check = checker(candidate, path)
    return _planned("plan", path, data, new_text, changes, check)


def apply(
    path,
    allowlist,
    requested,
    expect=None,
    checker=None,
    reloader=None,
    reload="auto",
    now=None,
    remove=None,
):
    """Carry the plan out.  See the module text for the order of steps and what each failure leaves.

    `expect` is a plan's `planSHA256`:  when given, the plan built here must hash to it.  `reload`
    is "auto" (reload only when `path` is the listener's own file), "always" or "never"."""
    if not allowlist["applyEnabled"]:
        raise Refusal(
            "apply_disabled",
            "apply is off:  the allowlist's applyEnabled is false until the owner turns it on",
        )
    if expect is not None and (not isinstance(expect, str) or not SHA256_RE.fullmatch(expect)):
        raise Refusal("usage", "the expected plan hash is 64 hex characters")
    checker = checker or daemon_check
    reloader = reloader or daemon_reload
    with _FolderLock(path.parent):
        data, info = read_live(path)
        new_text, changes = build_plan(_decode(data), allowlist, requested, remove)
        answer = _planned(
            "apply", path, data, new_text, changes, {"ok": True, "problems": [], "warnings": 0}
        )
        if expect is not None and answer["planSHA256"] != expect:
            raise Refusal(
                "plan_mismatch",
                "the file or the request is not what the plan was made from;  plan again",
            )
        if not changes:
            answer["result"] = "unchanged"
            return answer
        new_bytes = new_text.encode("utf-8")
        candidate = _write_temp(
            path.parent,
            ".%s.cand-" % path.name,
            new_bytes,
            info.st_mode & 0o777,
            (info.st_uid, info.st_gid),
        )
        backup = None
        try:
            check = checker(candidate, path)
            answer["check"] = check
            if not check.get("ok"):
                raise Refusal(
                    "check_failed",
                    "the listener would not take the result;  nothing was written",
                    check=check,
                )
            stamp = (now or _dt.datetime.now(_dt.timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
            backup = path.with_name("%s%s%s" % (path.name, BACKUP_INFIX, stamp))
            serial = 1
            while os.path.lexists(str(backup)):
                # Two applies in one second:  the earlier backup is never overwritten.
                serial += 1
                backup = path.with_name("%s%s%s-%d" % (path.name, BACKUP_INFIX, stamp, serial))
            backup_tmp = _write_temp(path.parent, ".%s." % backup.name, data, 0o600)
            os.replace(backup_tmp, str(backup))
            _fsync_dir(path.parent)
            again, _ = read_live(path)
            if again != data:
                raise Refusal(
                    "changed_underneath",
                    "the live file changed while this ran;  nothing was written",
                )
            os.replace(candidate, str(path))
            candidate = None
            _fsync_dir(path.parent)
        except Refusal:
            if backup is not None:
                # Refused after the backup was made (the file changed underneath):  the live file was
                # not replaced, so the backup made here is of no use and is removed.
                try:
                    os.unlink(str(backup))
                except OSError:
                    pass
            raise
        finally:
            if candidate is not None:
                try:
                    os.unlink(candidate)
                except OSError:
                    pass
        answer["backup"] = backup.name
        _prune_backups(path)
        wanted = reload == "always" or (reload == "auto" and _is_daemon_file(path))
        if not wanted:
            answer.update(
                result="applied", reload={"ok": True, "confirmed": False, "code": "reload_skipped"}
            )
            return answer
        try:
            outcome = reloader(path)
        except Exception:  # noqa: BLE001 - any failure to reload is the same answer:  roll back
            outcome = {"ok": False, "confirmed": False, "code": "reload_failed"}
        answer["reload"] = outcome
        if outcome.get("ok"):
            answer["result"] = "applied"
            return answer
        # The listener did not take it (or could not be told).  The old bytes go back the same
        # atomic way, and the listener is told again so what it runs is what is on disk.
        _replace(path, data, info)
        try:
            reloader(path)
        except Exception:  # noqa: BLE001, S110 - the file is back;  the second reload is best effort
            pass
        raise StepFailed(
            str(outcome.get("code") or "reload_failed"),
            "the listener did not take the reload;  the old file is back",
            True,
        )


# MARK: - The request


def run_request(request, path=None, checker=None, reloader=None, reload="auto"):
    """(answer, exit status) for one request document:

        {"action": "get" | "plan" | "apply", "allowlist": {...}, "set": {key: value},
         "remove": ["table.key.subkey", ...], "expectPlanSHA256": "..."}

    `path` is the file to act on;  by default the listener's own (SIMPLE_TOOLS_CONFIG, else
    /data/listener.toml).  A request never names the file."""
    live = Path(path or os.environ.get(ENV_CONFIG) or DEFAULT_LIVE)
    action = request.get("action") if isinstance(request, dict) else None
    try:
        if action not in ("get", "plan", "apply"):
            raise Refusal("usage", "the action is get, plan or apply")
        unknown = sorted(
            set(request) - {"action", "allowlist", "set", "remove", "expectPlanSHA256"}
        )
        if unknown:
            raise Refusal("usage", "unknown request field(s): %s" % ", ".join(unknown))
        allowlist = validate_allowlist(request.get("allowlist"))
        if action == "get":
            return get(live, allowlist), 0
        if action == "plan":
            return (
                plan(
                    live,
                    allowlist,
                    request.get("set"),
                    checker=checker,
                    remove=request.get("remove"),
                ),
                0,
            )
        return (
            apply(
                live,
                allowlist,
                request.get("set"),
                expect=request.get("expectPlanSHA256"),
                remove=request.get("remove"),
                checker=checker,
                reloader=reloader,
                reload=reload,
            ),
            0,
        )
    except Refusal as exc:
        answer = _answer(
            action or "", live, ok=False, result="refused", errorCode=exc.code, detail=exc.detail
        )
        if exc.check is not None:
            answer["check"] = exc.check
        return answer, 2
    except StepFailed as exc:
        return (
            _answer(
                action or "",
                live,
                ok=False,
                result="failed",
                errorCode=exc.code,
                detail=exc.detail,
                rolledBack=exc.rolled_back,
            ),
            1,
        )
    except OSError:
        return (
            _answer(
                action or "",
                live,
                ok=False,
                result="failed",
                errorCode="io_error",
                detail="a file step failed;  see the folder for a backup",
                rolledBack=False,
            ),
            1,
        )


def main(argv=None, stdin=None, out=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    out = out or sys.stdout
    if argv != ["--request-stdin"]:
        out.write(
            json.dumps(
                {
                    "ok": False,
                    "result": "refused",
                    "errorCode": "usage",
                    "detail": "run with --request-stdin and one JSON request on standard input",
                }
            )
            + "\n"
        )
        return 2
    raw = (stdin or sys.stdin.buffer).read(REQUEST_MAX_BYTES + 1)
    request = None
    if len(raw) <= REQUEST_MAX_BYTES:
        try:
            request = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError):
            request = None
    answer, code = run_request(request if isinstance(request, dict) else {})
    out.write(json.dumps(answer, sort_keys=True, ensure_ascii=True) + "\n")
    return code


if __name__ == "__main__":
    sys.exit(main())
