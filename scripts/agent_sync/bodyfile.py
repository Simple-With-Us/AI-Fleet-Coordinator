"""`--body-file PATH`:  a message body read from a private file, so its words never touch argv.

A BotFleet routine turn cannot pipe stdin (its tool policy rejects shell metacharacters), and a body on
the command line shows up in `ps` and in shell history.  A file the caller owns, that nobody else can
read or change, is the safe channel.  The same rules as the board's `--desc-file`:

  * a regular file, not a symbolic link, a directory, a FIFO or a device;
  * owned by the user running simple-chat;
  * mode exactly 0600 or 0400 (anything wider means someone else could have written or read it);
  * at most 64 KB, and strict UTF-8 with no NUL byte (a byte-order mark is dropped).

The checks run on the open descriptor (`O_NOFOLLOW` refuses a symlink, `O_NONBLOCK` keeps a FIFO from
hanging the open), so the file cannot be swapped between the check and the read.  A refusal says what
is wrong with the path and never repeats a byte of the file.

Python 3.11+, standard library only.
"""
from __future__ import annotations

import errno
import os
import stat

from .zulip import UsageError

__all__ = ["MAX_BYTES", "read_body_file"]

MAX_BYTES = 64 * 1024
_ALLOWED_MODES = (0o600, 0o400)


def _shown(path: str) -> str:
    """The path as one line of text, so a name with a newline in it cannot forge an error line."""
    return "".join(c if c.isprintable() else "?" for c in path)


def read_body_file(path: str) -> str:
    """The text in the private file at `path`.  Raises UsageError (exit 2) for anything else."""
    shown = _shown(path)
    if not path:
        raise UsageError("--body-file needs a path")
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise UsageError("--body-file %s is a symbolic link;  pass the real file" % shown) from None
        raise UsageError("--body-file %s: cannot open it (%s)" % (shown, exc.strerror or type(exc).__name__)) from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise UsageError("--body-file %s is not a regular file" % shown)
        if info.st_uid != os.geteuid():
            raise UsageError("--body-file %s is not owned by you" % shown)
        mode = stat.S_IMODE(info.st_mode)
        if mode not in _ALLOWED_MODES:
            raise UsageError("--body-file %s has mode %04o, not 0600 or 0400, so others may read or change it;  "
                             "run chmod 600 on it first" % (shown, mode))
        if info.st_size > MAX_BYTES:
            raise UsageError("--body-file %s is larger than %d KB" % (shown, MAX_BYTES // 1024))
        data = b""
        while len(data) <= MAX_BYTES:
            chunk = os.read(fd, MAX_BYTES + 1 - len(data))
            if not chunk:
                break
            data += chunk
    except OSError as exc:
        raise UsageError("--body-file %s: cannot read it (%s)" % (shown, exc.strerror or type(exc).__name__)) from None
    finally:
        os.close(fd)
    if len(data) > MAX_BYTES:
        raise UsageError("--body-file %s is larger than %d KB" % (shown, MAX_BYTES // 1024))
    if b"\x00" in data:
        raise UsageError("--body-file %s holds a NUL byte, so it is not text" % shown)
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise UsageError("--body-file %s is not valid UTF-8" % shown) from None
