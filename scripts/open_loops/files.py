"""Small file helpers shared by the commands:  read a private file safely, write one atomically at mode 0600,
and load and check a facts document.

A file the tool reads (the facts, the DM) must be a regular file, not a symbolic link, owned by the user and
not writable by anyone else, opened with O_NOFOLLOW and checked on the open descriptor so it cannot be swapped
between the check and the read.  A file the tool writes is created beside its final name at mode 0600 and moved
into place, in a folder made at mode 0700 if it did not exist.
"""
from __future__ import annotations

import json
import os
import re
import stat
import tempfile

__all__ = ["EXIT_USAGE", "FACTS_ID", "Refusal", "read_private_file", "write_private_file", "load_facts", "MAX_FILE_BYTES"]

EXIT_USAGE = 2
MAX_FILE_BYTES = 1024 * 1024
FACTS_ID = re.compile(r"^ol-(\d{8})-\d{4}-[a-z0-9-]{1,40}$")


class Refusal(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


def read_private_file(path: str, what: str) -> str:
    """Text of a regular file this user owns that nobody else can write.  Raises Refusal(EXIT_USAGE)."""
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(os.path.expanduser(path), flags)
    except OSError as exc:
        raise Refusal(EXIT_USAGE, "%s: cannot open it (%s)" % (what, exc.strerror or type(exc).__name__)) from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise Refusal(EXIT_USAGE, "%s is not a regular file" % what)
        if info.st_uid != os.geteuid():
            raise Refusal(EXIT_USAGE, "%s is not owned by you" % what)
        if stat.S_IMODE(info.st_mode) & 0o022:
            raise Refusal(EXIT_USAGE, "%s can be written by others;  chmod 600 it first" % what)
        if info.st_size > MAX_FILE_BYTES:
            raise Refusal(EXIT_USAGE, "%s is larger than %d KB" % (what, MAX_FILE_BYTES // 1024))
        data = b""
        while True:
            chunk = os.read(fd, 1 << 16)
            if not chunk:
                break
            data += chunk
            if len(data) > MAX_FILE_BYTES:
                raise Refusal(EXIT_USAGE, "%s is larger than %d KB" % (what, MAX_FILE_BYTES // 1024))
    except OSError as exc:
        raise Refusal(EXIT_USAGE, "%s: cannot read it (%s)" % (what, exc.strerror or type(exc).__name__)) from None
    finally:
        os.close(fd)
    if b"\x00" in data:
        raise Refusal(EXIT_USAGE, "%s holds a NUL byte, so it is not text" % what)
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise Refusal(EXIT_USAGE, "%s is not valid UTF-8" % what) from None


def write_private_file(path: str, text: str) -> str:
    """Write `text` (UTF-8) to `path` at mode 0600, atomically.  Returns the path.  Raises Refusal(EXIT_USAGE)."""
    target = os.path.abspath(os.path.expanduser(path))
    if os.path.isdir(target):
        raise Refusal(EXIT_USAGE, "--out %s is a folder" % target)
    folder = os.path.dirname(target)
    try:
        os.makedirs(folder, mode=0o700, exist_ok=True)
        fd, temp = tempfile.mkstemp(prefix=".open-loops-", dir=folder)
    except OSError as exc:
        raise Refusal(EXIT_USAGE, "--out: cannot write in %s (%s)" % (folder, exc.strerror or type(exc).__name__)) from None
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(temp, 0o600)
        os.replace(temp, target)
    except OSError as exc:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise Refusal(EXIT_USAGE, "--out: cannot write %s (%s)" % (target, exc.strerror or type(exc).__name__)) from None
    return target


def load_facts(path: str) -> dict:
    text = read_private_file(path, "the facts file")
    try:
        facts = json.loads(text)
    except ValueError:
        raise Refusal(EXIT_USAGE, "the facts file is not JSON") from None
    if not isinstance(facts, dict) or facts.get("schema") != 1 or not isinstance(facts.get("urls"), list) \
            or not isinstance(facts.get("full_list_url"), str) or not isinstance(facts.get("missing"), list) \
            or not isinstance(facts.get("sources"), dict) or not FACTS_ID.match(str(facts.get("facts_id"))):
        raise Refusal(EXIT_USAGE, "the facts file is not an open-loops facts document (schema 1)")
    return facts
