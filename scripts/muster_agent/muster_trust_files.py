"""muster_trust_files: the one reader of `/etc/muster` for every Muster verifier.

`server-route-spec.md` version 5, sections 9 and 10.  The helper (`fleet-ops-control.py --from-muster`),
`muster-pub` and `muster-exec` all read the owner key pin, the owner history and the reopen record
through this module, so that the three of them apply the same checks.  It is installed byte-identical
in `/usr/local/lib/fleet-resource-control/` and `/usr/local/lib/muster-agent/` from its one source file,
`scripts/muster_agent/muster_trust_files.py`, and each installer fails when the other copy differs.

`read_trust_files` refuses (TrustFilesUnsafe) when `/etc/muster`, `owner.pub`, `owner-history.jsonl` or,
when it exists, `reopen-codes.jsonl` is missing (the record excepted), a symbolic link, not a regular file
(a directory for `/etc/muster`), not owned by `root_uid`, or writable by group or others; when
`owner.pub` is not one P-256 key; when the history is not canonical, not contiguous from version 1, has
other than exactly one open entry, or its open entry's key differs from `owner.pub`; or when a line of
the reopen record is not canonical JSON of `{"code","createdAt","deviceId","version"}` in its patterns.
Every caller reads afresh for each verification and keeps nothing between them.  It returns the bytes
it read, so that a caller never opens a file a second time.

It reads files and nothing else: no network, no process, no environment variable.  Dependencies: the
standard library and `cryptography`.
"""

from __future__ import annotations

import base64
import datetime
import json
import os
import re
import stat

from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

ETC_DIR = "/etc/muster"
PIN_NAME = "owner.pub"
HISTORY_NAME = "owner-history.jsonl"
REOPEN_NAME = "reopen-codes.jsonl"
PIN_MAX_BYTES = 1024
HISTORY_MAX_BYTES = 64 * 1024
REOPEN_MAX_BYTES = 4 * 1024 * 1024
MAX_VERSION = 2**53 - 1

_B64URL_RE = re.compile(r"[A-Za-z0-9_-]*")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
# DER SubjectPublicKeyInfo of an uncompressed P-256 key: this fixed prefix, then 04 || X || Y.
_P256_SPKI_PREFIX = bytes.fromhex("3059301306072a8648ce3d020106082a8648ce3d030107034200")


class TrustFilesUnsafe(Exception):
    """A file under /etc/muster is missing, unsafe or malformed.  The text names no path."""


class TrustFiles:
    """What one read of /etc/muster found."""

    __slots__ = (
        "history",
        "history_bytes",
        "owner_key",
        "pin_bytes",
        "reopen_bytes",
        "reopen_lines",
    )

    def __init__(self, owner_key, history, pin_bytes, history_bytes, reopen_bytes, reopen_lines):
        self.owner_key = owner_key
        self.history = history
        self.pin_bytes = pin_bytes
        self.history_bytes = history_bytes
        self.reopen_bytes = reopen_bytes
        self.reopen_lines = reopen_lines


def history_key(history, version):
    """The owner key (base64url) that covers `version`, or None."""
    for entry in history:
        if entry["fromVersion"] <= version and (
            entry["toVersion"] is None or version <= entry["toVersion"]
        ):
            return entry["ownerKey"]
    return None


def _load_p256(text):
    """The P-256 public key in a base64url SubjectPublicKeyInfo, or TrustFilesUnsafe."""
    try:
        if type(text) is not str or not _B64URL_RE.fullmatch(text) or len(text) % 4 == 1:
            raise ValueError
        der = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
        if base64.urlsafe_b64encode(der).rstrip(b"=").decode("ascii") != text:
            raise ValueError
        if (
            len(der) != 91
            or not der.startswith(_P256_SPKI_PREFIX)
            or der[len(_P256_SPKI_PREFIX)] != 0x04
        ):
            raise ValueError
        # Named `loaded`, not after a key:  a secret scanner reads such a name as a credential.
        loaded = serialization.load_der_public_key(der)
        if not isinstance(loaded, ec.EllipticCurvePublicKey) or not isinstance(
            loaded.curve, ec.SECP256R1
        ):
            raise ValueError  # noqa: TRY004 - one handler for every fault
    except (ValueError, TypeError, UnsupportedAlgorithm):
        raise TrustFilesUnsafe("a key is not a P-256 key") from None
    return loaded


def _open_directory(path, root_uid):
    try:
        fd = os.open(str(path), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        raise TrustFilesUnsafe("the directory cannot be opened") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != root_uid or info.st_mode & 0o022:
            raise TrustFilesUnsafe("the directory is not root-owned and protected")
    except BaseException:
        os.close(fd)
        raise
    return fd


def _read_file(dir_fd, name, root_uid, cap, optional=False):
    """The bytes of one file, None when `optional` and absent, or TrustFilesUnsafe."""
    try:
        fd = os.open(
            name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=dir_fd
        )
    except FileNotFoundError:
        if optional:
            return None
        raise TrustFilesUnsafe("a file is missing") from None
    except OSError:
        raise TrustFilesUnsafe("a file cannot be opened") from None
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != root_uid
            or info.st_mode & 0o022
            or info.st_size > cap
        ):
            raise TrustFilesUnsafe("a file is not a protected regular file of the allowed size")
        chunks = []
        offset = 0
        while offset < info.st_size:
            chunk = os.pread(fd, min(65536, info.st_size - offset), offset)
            if not chunk:
                break
            chunks.append(chunk)
            offset += len(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def _history_line(line):
    """The entry of one canonical history line, or TrustFilesUnsafe."""

    def strict_pairs(pairs):
        found = {}
        for key, value in pairs:
            if key in found:
                raise ValueError
            found[key] = value
        return found

    def refuse(_text):
        raise ValueError

    try:
        text = line.decode("ascii")
        entry = json.loads(
            text, object_pairs_hook=strict_pairs, parse_constant=refuse, parse_float=refuse
        )
        if type(entry) is not dict or set(entry) != {
            "fromVersion",
            "ownerKey",
            "toSHA256",
            "toVersion",
        }:
            raise ValueError
        start, end = entry["fromVersion"], entry["toVersion"]
        digest = entry["toSHA256"]
        if type(start) is not int or not 1 <= start <= MAX_VERSION:
            raise ValueError
        if end is None:
            # The open entry: no end version, so no hash of an end version.
            if digest is not None:
                raise ValueError
        elif (
            type(end) is not int
            or not start <= end <= MAX_VERSION
            or not (type(digest) is str and _SHA256_RE.fullmatch(digest))
        ):
            raise ValueError
        _load_p256(entry["ownerKey"])
        digest_text = "null" if digest is None else f'"{digest}"'
        end_text = "null" if end is None else str(end)
        expected = (
            f'{{"fromVersion":{start},"ownerKey":"{entry["ownerKey"]}",'
            f'"toSHA256":{digest_text},"toVersion":{end_text}}}'
        )
        if expected != text:
            raise ValueError
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise TrustFilesUnsafe("a history line is not canonical") from None
    return entry


_CODE_RE = re.compile(r"[0-9a-f]{32}")
_TIME_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
_DEVICE_RE = re.compile(r"d-[0-9a-f]{24}")


def _reopen_line(line):
    """The entry of one reopen record line (spec section 9), or TrustFilesUnsafe.

    The canonical JSON of `{"code","createdAt","deviceId","version"}`: a code of 32 lowercase hex, a
    time, a device id and a version from 1.  Anything else, in any detail, is not a line."""

    def strict_pairs(pairs):
        found = {}
        for key, value in pairs:
            if key in found:
                raise ValueError
            found[key] = value
        return found

    def refuse(_text):
        raise ValueError

    try:
        text = line.decode("ascii")
        entry = json.loads(
            text, object_pairs_hook=strict_pairs, parse_constant=refuse, parse_float=refuse
        )
        if type(entry) is not dict or set(entry) != {"code", "createdAt", "deviceId", "version"}:
            raise ValueError
        code, created, device, version = (
            entry["code"],
            entry["createdAt"],
            entry["deviceId"],
            entry["version"],
        )
        if not (
            type(code) is str
            and _CODE_RE.fullmatch(code)
            and type(created) is str
            and _TIME_RE.fullmatch(created)
            and type(device) is str
            and _DEVICE_RE.fullmatch(device)
            and type(version) is int
            and 1 <= version <= MAX_VERSION
        ):
            raise ValueError
        datetime.datetime(
            int(created[0:4]),
            int(created[5:7]),
            int(created[8:10]),
            int(created[11:13]),
            int(created[14:16]),
            int(created[17:19]),
        )
        expected = (
            f'{{"code":"{code}","createdAt":"{created}","deviceId":"{device}","version":{version}}}'
        )
        if expected != text:
            raise ValueError
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise TrustFilesUnsafe("a reopen record line is not in its format") from None
    return entry


def _reopen_lines(raw):
    """Every line of the reopen record (none for an empty file), or TrustFilesUnsafe."""
    if not raw:
        return []
    if not raw.endswith(b"\n"):
        raise TrustFilesUnsafe("the reopen record does not end with a line feed")
    return [_reopen_line(line) for line in raw.split(b"\n")[:-1]]


def read_trust_files(etc_dir=ETC_DIR, root_uid=0):
    """One read of `/etc/muster`: a TrustFiles, or TrustFilesUnsafe."""
    dir_fd = _open_directory(etc_dir, root_uid)
    try:
        pin_bytes = _read_file(dir_fd, PIN_NAME, root_uid, PIN_MAX_BYTES)
        history_bytes = _read_file(dir_fd, HISTORY_NAME, root_uid, HISTORY_MAX_BYTES)
        reopen_bytes = _read_file(dir_fd, REOPEN_NAME, root_uid, REOPEN_MAX_BYTES, optional=True)
    finally:
        os.close(dir_fd)
    if not pin_bytes.endswith(b"\n") or pin_bytes.count(b"\n") != 1:
        raise TrustFilesUnsafe("owner.pub is not one line")
    try:
        owner_key = pin_bytes[:-1].decode("ascii")
    except UnicodeDecodeError:
        raise TrustFilesUnsafe("owner.pub is not ASCII") from None
    _load_p256(owner_key)
    if not history_bytes.endswith(b"\n"):
        raise TrustFilesUnsafe("the history does not end with a line feed")
    history = [_history_line(line) for line in history_bytes.split(b"\n")[:-1]]
    if not history:
        raise TrustFilesUnsafe("the history is empty")
    previous_end = 0
    for index, entry in enumerate(history):
        if entry["fromVersion"] != previous_end + 1:
            raise TrustFilesUnsafe("the history is not contiguous from version 1")
        if entry["toVersion"] is None:
            if index != len(history) - 1:
                raise TrustFilesUnsafe("the history has an open entry that is not the last")
        else:
            previous_end = entry["toVersion"]
    if history[-1]["toVersion"] is not None or history[-1]["ownerKey"] != owner_key:
        raise TrustFilesUnsafe("the open history entry is not the pinned key")
    reopen_lines = [] if reopen_bytes is None else _reopen_lines(reopen_bytes)
    return TrustFiles(owner_key, history, pin_bytes, history_bytes, reopen_bytes, reopen_lines)
