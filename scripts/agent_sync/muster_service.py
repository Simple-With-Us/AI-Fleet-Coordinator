"""Read-only Operator adapter for Muster's Seats panel.  No ordinary seat, no write, no admin API.

`simple-chat muster-service users [--limit N]` prints the realm's users, deactivated ones included, one
JSON object per line in the shape `simple-chat users --json --inactive` prints.  `simple-chat
muster-service last-post --sender ADDRESS` prints that sender's newest public-channel message, one JSON
object in the shape `simple-chat search --json` prints, or nothing when there is none.

It reads only as the Operator service bot (owner ruling:  Muster never reads as an agent seat).  The
credential is the Operator's own, read from Fleet-OPS /zulip_operator through `service_identity`, and
`users/me` must be the pinned Operator bot owned by the pinned owner before anything else is asked.  The
pins come from the host, never from source:  OPERATOR_ZULIP_USER_ID and OPERATOR_ZULIP_EMAIL from the
environment, else `[daemon] operator_user_id` and `operator_email` in the host's listener.toml, and the
owner from `[daemon] owner_user_id` there.  A missing pin fails closed.

Only three GET endpoints can be reached (users/me, users, messages), through a wrapper that has no other
method.  On any failure it prints one fixed refusal line with a reason code and exits 3:  never provider
text, a credential or an exception message."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from . import live as L
from . import service_identity as SI
from . import zulip as Z
from .zulip_ops import user_row

PROG = "simple-chat muster-service"
#: The most users one `users` call prints.
MAX_USERS = 2000
#: The longest message body passed on.  Zulip caps a message at 10,000 characters.
MAX_CONTENT = 10000
#: The longest sender address accepted.
MAX_ADDRESS = 254
#: Each Zulip request's own timeout, and the budget for all of one invocation's Zulip requests.
REQUEST_TIMEOUT = 10.0
ZULIP_BUDGET = 12.0
#: The only endpoints this adapter can ask for, and only with GET.
ALLOWED_GETS = frozenset({"users/me", "users", "messages"})

#: Every refusal this adapter prints.  The code is the whole explanation:  Muster maps it to a sentence.
REFUSAL_CODES = (
    "launched-or-remote",
    "pins-missing",
    "pins-unreadable",
    "realm-invalid",
    "credential-unreadable",
    "realm-mismatch",
    "identity-mismatch",
    "credential-rejected",
    "zulip-unreachable",
    "zulip-error",
    "malformed-response",
    "bad-request",
    "failed",
)


class Refused(Exception):
    """A refusal with a fixed reason code and nothing else."""

    def __init__(self, code: str) -> None:
        if code not in REFUSAL_CODES:
            code = "failed"
        super().__init__(code)
        self.code = code


class ReadOnlyClient:
    """The only Zulip surface the service has:  `get` on three paths.  No post, patch or delete exists."""

    __slots__ = ("_deadline", "_request")

    def __init__(self, client: Any, deadline: float) -> None:
        self._request = client.request
        self._deadline = deadline

    def get(self, path: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        if path not in ALLOWED_GETS:
            raise Refused("bad-request")
        return self._request("GET", path, params, timeout=REQUEST_TIMEOUT, deadline=self._deadline)


# --------------------------------------------------------------------------------------------
# Pins
# --------------------------------------------------------------------------------------------


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    text = str(value or "").strip()
    if not text.isdigit():
        return None
    number = int(text)
    return number if number > 0 else None


def read_pins(env: Mapping[str, str]) -> tuple[int, str, int]:
    """(operator user id, operator address, owner user id), or Refused.

    The environment wins for the Operator's id and address (the launchd definition and MCP
    registrations carry them);  the host's listener.toml is next, where the owner's id is pinned too.
    A value that is set but malformed is `pins-unreadable`, never skipped for the next source."""
    root = L.state_root(env)
    raw, problem = L.load_config(root, L.config_path(root, env))
    if problem:
        raise Refused("pins-unreadable")
    daemon = raw.get("daemon") if isinstance(raw, dict) else None
    if daemon is None:
        daemon = {}
    if not isinstance(daemon, dict):
        raise Refused("pins-unreadable")

    def pinned_id(env_name: str | None, key: str) -> int | None:
        if env_name is not None:
            text = str(env.get(env_name) or "").strip()
            if text:
                found = _positive_int(text)
                if found is None:
                    raise Refused("pins-unreadable")
                return found
        value = daemon.get(key, 0)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise Refused("pins-unreadable")
        return value or None

    operator_id = pinned_id(Z.ENV_OPERATOR_USER_ID, "operator_user_id")
    owner_id = pinned_id(None, "owner_user_id")
    email = str(env.get("OPERATOR_ZULIP_EMAIL") or "").strip()
    if not email:
        value = daemon.get("operator_email", "")
        if not isinstance(value, str):
            raise Refused("pins-unreadable")
        email = value.strip()
    if email and ("@" not in email or len(email) > MAX_ADDRESS or any(c.isspace() for c in email)):
        raise Refused("pins-unreadable")
    if operator_id is None or owner_id is None or not email:
        raise Refused("pins-missing")
    return operator_id, email.casefold(), owner_id


# --------------------------------------------------------------------------------------------
# The Operator client
# --------------------------------------------------------------------------------------------


def _realm(env: Mapping[str, str]) -> str:
    site = Z.realm_url(env)
    if not site.startswith("https://"):
        raise Refused("realm-invalid")
    return site


def operator_credentials(env: dict[str, str], site: str) -> Z.Credentials:
    """The Operator's own credential from Fleet-OPS /zulip_operator, with the store's realm checked
    against the realm this service reads before any authentication."""
    home = Path(env.get("HOME") or os.path.expanduser("~"))
    try:
        env[SI.PROJECT_ENV] = Z.infisical_project_id(env, home)
        names = ("ZULIP_OPERATOR_EMAIL", "ZULIP_OPERATOR_API_KEY", "ZULIP_OPERATOR_SITE")
        values = Z._default_infisical_fetch(names, env, home, SI.OPERATOR_INFISICAL_PATH)
    except Exception:  # noqa: BLE001 - a reason token at most, and none is printed
        raise Refused("credential-unreadable") from None
    stored = str(values.get("ZULIP_OPERATOR_SITE") or "").strip().rstrip("/")
    if not stored or Z._normalise_site(stored) != site.rstrip("/"):
        raise Refused("realm-mismatch")
    try:
        creds = SI.operator_credentials(
            env, fetch=lambda requested: {n: values.get(n, "") for n in requested}
        )
    except Exception:  # noqa: BLE001 - includes "not the pinned service bot"
        raise Refused("credential-unreadable") from None
    if creds.site.rstrip("/") != site.rstrip("/"):
        raise Refused("realm-mismatch")
    return creds


def connect(
    env: Mapping[str, str], *, client: Any = None, credentials: Any = None
) -> ReadOnlyClient:
    """A verified read-only Operator client, or Refused.  `client` and `credentials` are for tests."""
    env = dict(env)
    if (
        env.get("SIMPLE_CHAT_REMOTE", "").strip() == "1"
        or env.get("AGENT_LAUNCHER")
        or env.get("AGENT_LAUNCH_SEAT")
    ):
        raise Refused("launched-or-remote")
    operator_id, email, owner_id = read_pins(env)
    site = _realm(env)
    env.update(OPERATOR_ZULIP_USER_ID=str(operator_id), OPERATOR_ZULIP_EMAIL=email, ZULIP_SITE=site)
    if credentials is None and client is None:
        credentials = operator_credentials(env, site)
    if credentials is not None and credentials.site.rstrip("/") != site.rstrip("/"):
        raise Refused("realm-mismatch")
    if client is None:
        client = Z.ZulipClient(credentials, site, timeout=REQUEST_TIMEOUT, owner_user_id=owner_id)
    reader = ReadOnlyClient(client, time.monotonic() + ZULIP_BUDGET)
    me = reader.get("users/me")
    if not isinstance(me, dict) or not SI.operator_bot(me, env, owner_id):
        raise Refused("identity-mismatch")
    return reader


# --------------------------------------------------------------------------------------------
# The two reads
# --------------------------------------------------------------------------------------------


def users(reader: ReadOnlyClient, limit: int) -> list[dict[str, Any]]:
    """Every user, deactivated ones included, sorted by display name:  `users --json --inactive`."""
    members = reader.get("users").get("members")
    if not isinstance(members, list) or not all(isinstance(m, dict) for m in members):
        raise Refused("malformed-response")
    rows = [user_row(m) for m in members]
    rows.sort(key=lambda r: str(r.get("full_name") or "").casefold())
    return rows[:limit]


def _channel(message: Mapping[str, Any]) -> str | None:
    recipient = message.get("display_recipient")
    return recipient if isinstance(recipient, str) else None


def last_post(reader: ReadOnlyClient, sender: str) -> list[dict[str, Any]]:
    """The sender's newest message in a public channel, as `search --json` prints it, or nothing.

    One request:  the newest message of the narrow `channels:public sender:ADDRESS`.  Direct messages
    and private channels are outside the narrow, so the panel never learns of either."""
    narrow = [
        {"operator": "channels", "operand": "public"},
        {"operator": "sender", "operand": sender},
    ]
    data = reader.get(
        "messages",
        {
            "narrow": json.dumps(narrow),
            "anchor": "newest",
            "num_before": 1,
            "num_after": 0,
            "include_anchor": "true",
            "apply_markdown": "false",
        },
    )
    messages = data.get("messages")
    if not isinstance(messages, list):
        raise Refused("malformed-response")
    found = [
        m
        for m in messages
        if isinstance(m, dict)
        and isinstance(m.get("id"), int)
        and not isinstance(m.get("id"), bool)
        and m.get("type") == "stream"
        and str(m.get("sender_email") or "").casefold() == sender
    ]
    if not found:
        return []
    newest = max(found, key=lambda m: m["id"])
    content = newest.get("content")
    return [
        {
            "id": newest["id"],
            "sender_id": newest.get("sender_id"),
            "channel": _channel(newest),
            "topic": newest.get("subject") if isinstance(newest.get("subject"), str) else None,
            "dm": False,
            "sender_email": newest.get("sender_email"),
            "sender_full_name": newest.get("sender_full_name"),
            "timestamp": newest.get("timestamp"),
            "content": content[:MAX_CONTENT] if isinstance(content, str) else None,
        }
    ]


def _sender(value: str) -> str:
    text = (value or "").strip()
    if (
        not text
        or len(text) > MAX_ADDRESS
        or text.startswith("-")
        or text.count("@") != 1
        or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in text)
    ):
        raise Refused("bad-request")
    return text.casefold()


def execute(
    args: argparse.Namespace,
    *,
    env: Mapping[str, str] | None = None,
    client: Any = None,
    credentials: Any = None,
) -> list[dict[str, Any]]:
    env = dict(os.environ if env is None else env)
    if args.action == "users":
        limit = args.limit
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_USERS:
            raise Refused("bad-request")
        reader = connect(env, client=client, credentials=credentials)
        return users(reader, limit)
    if args.action == "last-post":
        sender = _sender(args.sender or "")
        reader = connect(env, client=client, credentials=credentials)
        return last_post(reader, sender)
    raise Refused("bad-request")


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # argparse would echo the bad value
        raise Refused("bad-request")


def main(argv: list[str] | None = None, *, env: Mapping[str, str] | None = None) -> int:
    parser = _Parser(prog=PROG, add_help=False)
    parser.add_argument("action", choices=["users", "last-post"])
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--sender", default="")
    try:
        args = parser.parse_args(argv)
        rows = execute(args, env=env)
    except Refused as exc:
        code = exc.code
    except Z.ApiError as exc:
        code = "credential-rejected" if exc.status in (401, 403) else "zulip-error"
    except Z.NetworkError:
        code = "zulip-unreachable"
    except Exception:  # noqa: BLE001 - output must never contain provider text
        code = "failed"
    else:
        for row in rows:
            sys.stdout.write(json.dumps(row, ensure_ascii=False) + "\n")
        return 0
    sys.stderr.write("muster-service: refused (%s)\n" % code)
    return 3


if __name__ == "__main__":
    sys.exit(main())
