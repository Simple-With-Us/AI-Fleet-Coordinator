"""One outcome per needs-owner board comment, so a record is written once.

The board CLI derives a comment's own id from the seat and the session, so the
same record written by the filing seat and again by CLAUDE's fallback notifier
is two comments on one issue, and a retry after a lost response can post the
record a second time.  This store holds one outcome per (item, record text)
pair, so the second writer returns the first one's result instead of posting
again.

The file lives beside the fallback notifier's own state, in a directory of its own
and not inside the notifier's, because the filing seat writes this store too and a
dry run must leave the notifier's state untouched.  It holds digests and outcome
words only:  never a question, an answer, a comment's words or a credential.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Mapping

from . import ledger

FILE = "comment-keys.json"
SCHEMA = 1
MAX_KEYS = 500
KEEP_SECONDS = 14 * 24 * 3600

OK = "ok"  # the comment is on the issue
UNKNOWN = "unknown"  # the request may have landed, so nothing more is written on this key


def key_for(identifier: str, text: str) -> str:
    """The store key for one record:  a digest of the item and the words."""
    raw = "%s\x00%s" % (identifier, text)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _path(environ: Mapping[str, str] | None = None) -> str:
    env = environ if environ is not None else os.environ
    return os.path.join(ledger.comment_keys_dir(env), FILE)


def _load(environ: Mapping[str, str] | None = None) -> dict:
    data = ledger.read_json(_path(environ))
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        return {}
    keys = data.get("keys")
    return keys if isinstance(keys, dict) else {}


def _save(keys: dict, environ: Mapping[str, str] | None = None) -> None:
    path = _path(environ)
    ledger.private_dir(os.path.dirname(path))
    ledger.write_private(path, json.dumps({"schema": SCHEMA, "keys": keys}, sort_keys=True))


def get(key: str, environ: Mapping[str, str] | None = None) -> str | None:
    """The outcome held for `key`:  `ok`, `unknown`, or None when nothing is held."""
    row = _load(environ).get(key)
    if not isinstance(row, dict):
        return None
    state = row.get("state")
    return state if state in (OK, UNKNOWN) else None


def put(key: str, state: str, environ: Mapping[str, str] | None = None) -> None:
    """Hold one outcome for `key`, keeping the file small and the old rows out."""
    keys = _load(environ)
    now = time.time()
    keys[key] = {"state": state, "at": now}
    fresh = {
        k: row
        for k, row in keys.items()
        if isinstance(row, dict) and (now - float(row.get("at") or 0)) < KEEP_SECONDS
    }
    if len(fresh) > MAX_KEYS:
        ordered = sorted(fresh.items(), key=lambda pair: float(pair[1].get("at") or 0))
        fresh = dict(ordered[-MAX_KEYS:])
    _save(fresh, environ)


def release(key: str, environ: Mapping[str, str] | None = None) -> None:
    """Let `key` go, so a later run may write the record again."""
    keys = _load(environ)
    if key in keys:
        del keys[key]
        _save(keys, environ)
