"""Private durable reconciliation reports. Never change dispatch state or retry."""

import fcntl
import os
import stat
from pathlib import Path

from .native_custody import read_private
from .zcode_native import NativeDelivery, _load, _save, digest


def alert_id(state, wake_key):
    if not isinstance(wake_key, str) or not __import__("re").fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", wake_key
    ):
        return None
    for area in ("pending", "done"):
        try:
            record = read_private(Path(state) / area / (wake_key + ".json"))
            wake = record.get("wake", {})
            value = wake.get("message_id")
            if (
                wake.get("wake_id") == wake_key
                and wake.get("credential_scope") == "review-alerts"
                and type(value) is int
                and value > 0
            ):
                return value
        except (OSError, ValueError, TypeError):
            pass
    return None


def merge(old, current):
    if old is None:
        return current
    if (
        old.get("binding") != current["binding"]
        or old.get("wake_key") != current["wake_key"]
    ):
        raise ValueError("evidence-binding-conflict")
    previous = old.get("evidence", {})
    now = current["evidence"]
    merged = dict(previous)
    ranks = {
        "admission": {"unverified": 0, "admitted": 1},
        "native_input": {"unverified": 0, "persisted": 1},
        "turn": {"unverified": 0, "started": 1, "completed": 2},
        "owning_alert": {"unverified": 0, "fetched": 1},
    }
    incomplete = False
    conflict = bool(old.get("conflict") or now.get("conflict"))
    for key, values in ranks.items():
        a = previous.get(key, "unverified")
        b = now.get(key, "unverified")
        if a not in values or b not in values:
            raise ValueError("evidence-state-invalid")
        if values[b] < values[a]:
            incomplete = True
        else:
            merged[key] = b
    for key, value in now.items():
        if key in ranks:
            continue
        if (
            key
            in (
                "message_id",
                "sourceCommandId",
                "alert_id",
                "fetch_message_id",
                "fetch_part_id",
            )
            and key in previous
            and previous[key] != value
        ):
            conflict = True
        elif key != "conflict":
            merged[key] = value
    if conflict:
        merged["conflict"] = True
    return {
        **current,
        "evidence": merged,
        "conflict": conflict,
        "read_incomplete": incomplete,
    }


def persist(state, binding, wake_key, evidence):
    folder = Path(state) / "native-evidence"
    journal = NativeDelivery(folder, lambda _: None, lambda _: False)
    try:
        name = digest({"wake_key": wake_key})
        fd = os.open(
            name + ".lock",
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
            0o600,
            dir_fd=journal.dirfd,
        )
        with os.fdopen(fd, "w") as lock:
            st = os.fstat(lock.fileno())
            if (
                not stat.S_ISREG(st.st_mode)
                or st.st_nlink != 1
                or st.st_uid != os.getuid()
                or st.st_mode & 0o077
            ):
                raise ValueError("evidence-lock-custody")
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            path = folder / (name + ".json")
            record = merge(
                _load(path, journal.dirfd),
                {
                    "version": 1,
                    "binding": binding,
                    "wake_key": wake_key,
                    "evidence": evidence,
                },
            )
            _save(path, record, journal.dirfd)
    finally:
        journal.close()
