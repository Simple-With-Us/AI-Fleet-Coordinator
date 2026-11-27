"""Bounded exact-target cap1 callback. No install, config change or approval wait."""

import fcntl
import json
import math
import os
import re
import stat
import threading
import time
from pathlib import Path

from .native_custody import fresh, read_private
from .zcode_native import NativeDelivery, _load, _save, canonical, digest


class Qualification:
    def __init__(
        self,
        state,
        target,
        workspace,
        epoch,
        expires,
        native,
        fallback,
        render,
        unknown,
        custody,
        clock=time.time,
        qualification_generation=None,
        source_boundary=None,
    ):
        if (
            not isinstance(epoch, (int, float))
            or isinstance(epoch, bool)
            or not math.isfinite(epoch)
            or (
                expires is not None
                and (
                    not isinstance(expires, (int, float))
                    or isinstance(expires, bool)
                    or not math.isfinite(expires)
                    or not 0 < expires - epoch <= 3600
                )
            )
            or not Path(workspace).is_absolute()
        ):
            raise ValueError("qualification-window")
        if qualification_generation is not None and (
            not isinstance(qualification_generation, str)
            or not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", qualification_generation)
        ):
            raise ValueError("qualification-generation")
        self.state, self.target, self.workspace = Path(state), dict(target), workspace
        if (
            set(target) != {"session_id", "host", "letter", "generation"}
            or target["host"] != "server"
            or not isinstance(target["session_id"], str)
            or not re.fullmatch(r"sess_[A-Za-z0-9._-]{1,123}", target["session_id"])
            or not isinstance(target["letter"], str)
            or not re.fullmatch(r"[A-Z]{2}", target["letter"])
            or type(target["generation"]) is not int
            or target["generation"] < 1
        ):
            raise ValueError("qualification-target")
        from .native_event_eligibility import valid_boundary

        if qualification_generation is not None and (
            expires is None
            or not valid_boundary(source_boundary)
            or source_boundary["epoch"] != epoch
        ):
            raise ValueError("qualification-prospective-boundary")
        if qualification_generation is None and source_boundary is not None:
            raise ValueError("qualification-source-boundary")
        self.epoch, self.expires, self.clock = epoch, expires, clock
        self.qualification_generation = qualification_generation
        self.source_boundary = json.loads(canonical(source_boundary))
        self.reservation_attempted = None
        self.mutex = threading.Lock()
        self.native, self.fallback, self.render = native, fallback, render
        self.unknown, self.custody = unknown, custody
        folder = self.state / "native-qualification"
        if qualification_generation is not None:
            folder = self.state / "native-qualification-generations" / qualification_generation
        self.store = NativeDelivery(folder, lambda _: None, lambda _: False)
        if qualification_generation is not None:
            # A generation's boundary survives restart even before cap-one is used.
            # A different epoch or expiry never silently reuses the same namespace.
            contract = {
                "version": 1,
                "qualification_generation": qualification_generation,
                "target": self.target,
                "workspace": self.workspace,
                "epoch": self.epoch,
                "expires": self.expires,
                "source_boundary": self.source_boundary,
            }
            try:
                fd = os.open(
                    "generation.json",
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=self.store.dirfd,
                )
            except FileExistsError:
                try:
                    if _load(Path("generation.json"), self.store.dirfd) != contract:
                        raise ValueError("qualification-generation-contract")
                except Exception:
                    self.store.close()
                    raise
            else:
                try:
                    with os.fdopen(fd, "w") as f:
                        f.write(canonical(contract))
                        f.flush()
                        os.fsync(f.fileno())
                    os.fsync(self.store.dirfd)
                except Exception:
                    self.store.close()
                    raise

    def window_open(self, now):
        # Null expiry is event-driven cap1, not a rolling/rearmed clock window.
        return (
            type(now) in (int, float)
            and math.isfinite(now)
            and self.epoch <= now
            and (self.expires is None or now < self.expires)
        )

    def close(self):
        self.store.close()

    def __call__(self, target, head, riders, turn_id, *, fallback=None):
        fallback = fallback or self.fallback
        # No global lock or per-session lock is held by this wrapper during IO.
        if any(getattr(target, k, None) != v for k, v in self.target.items()):
            return fallback(target, head, riders, turn_id)
        try:
            old = _load(Path("slot.claim"), self.store.dirfd)
        except Exception:
            return self.unknown("native-qualification-claim-unreadable")
        if old is not None:
            required = {
                "version",
                "target",
                "workspace",
                "wake_id",
                "message_id",
                "notification_turn_id",
                "text",
                "text_sha256",
                "epoch",
                "expires",
                "reserved_at",
            }
            if (
                set(old) != required
                or old.get("version") != 1
                or old.get("target") != self.target
                or old.get("workspace") != self.workspace
                or old.get("epoch") != self.epoch
                or old.get("expires") != self.expires
                or not isinstance(old.get("wake_id"), str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", old["wake_id"])
                or type(old.get("message_id")) is not int
                or old["message_id"] <= 0
                or not isinstance(old.get("notification_turn_id"), str)
                or not re.fullmatch(r"t-[0-9a-f]{16}", old["notification_turn_id"])
                or not isinstance(old.get("text"), str)
                or not 0 < len(old["text"].encode()) <= 16000
                or old.get("text_sha256") != digest(old["text"])
                or type(old.get("reserved_at")) not in (int, float)
                or not self.window_open(old["reserved_at"])
            ):
                return self.unknown("native-qualification-claim-invalid")
            if old.get("wake_id") == head.wake_id:
                return self.unknown("native-qualification-slot-consumed")
            return fallback(target, head, riders, turn_id)
        # Namespace isolation never creates a second command identity for a wake.
        # An existing journal, even unreadable/symlinked, excludes reservation.
        prior = self.state / "native-commands" / (digest({"wake_key": head.wake_id}) + ".json")
        if prior.exists() or prior.is_symlink():
            return self.unknown("native-qualification-prior-command")
        if self.qualification_generation is not None:
            from .native_event_eligibility import persisted_event

            if not persisted_event(
                self.state, self.qualification_generation, self.source_boundary, head
            ):
                return fallback(target, head, riders, turn_id)
        now = self.clock()
        if not self.window_open(now):
            return fallback(target, head, riders, turn_id)
        if riders or not fresh(self.state, head.wake_id, target, self.epoch):
            return fallback(target, head, riders, turn_id)
        try:
            record = read_private(self.state / "pending" / (head.wake_id + ".json"))
            wake = record["wake"]
            if (
                type(head.message_id) is not int
                or head.message_id <= 0
                or wake.get("message_id") != head.message_id
                or not self.custody(target, self.workspace)
            ):
                return self.unknown("native-qualification-binding-refused")
            if not isinstance(turn_id, str) or not re.fullmatch(r"t-[0-9a-f]{16}", turn_id):
                return self.unknown("native-qualification-turn-refused")
            text = self.render(head, turn_id)
            if not isinstance(text, str) or not 0 < len(text.encode()) <= 16000:
                return self.unknown("native-qualification-text-refused")
            preview = {
                "version": 1,
                "target": self.target,
                "workspace": self.workspace,
                "wake_id": head.wake_id,
                "message_id": head.message_id,
                "notification_turn_id": turn_id,
                "text": text,
                "text_sha256": digest(text),
                "epoch": self.epoch,
                "expires": self.expires,
                "reserved_at": now,
            }
            if not self.mutex.acquire(blocking=False):
                return self.unknown("native-qualification-slot-consumed")
            try:
                previous = self.reservation_attempted
                if previous is None:
                    self.reservation_attempted = head.wake_id
            finally:
                self.mutex.release()
            if previous is not None:
                if previous != head.wake_id:
                    return fallback(target, head, riders, turn_id)
                return self.unknown("native-qualification-slot-consumed")
            fd = os.open(
                "slot.lock",
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
                0o600,
                dir_fd=self.store.dirfd,
            )
            with os.fdopen(fd, "w") as lock:
                st = os.fstat(lock.fileno())
                if (
                    not stat.S_ISREG(st.st_mode)
                    or st.st_uid != os.getuid()
                    or st.st_nlink != 1
                    or st.st_mode & 0o077
                ):
                    return self.unknown("native-qualification-lock-refused")
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                # Exclusive claim is never removed, even when preview save fails.
                # A restart sees its presence and cannot reserve another event.
                claim = os.open(
                    "slot.claim",
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=self.store.dirfd,
                )
                with os.fdopen(claim, "w") as f:
                    f.write(canonical(preview))
                    f.flush()
                    os.fsync(f.fileno())
                os.fsync(self.store.dirfd)
                if _load(Path("slot.json"), self.store.dirfd) is not None:
                    return self.unknown("native-qualification-slot-consumed")
                _save(Path("slot.json"), preview, self.store.dirfd)
            # Existing reservation on restart never resumes dispatch. Only the original
            # process can reach this path, and only while authoritative custody holds.
            if (
                (
                    self.qualification_generation is not None
                    and not persisted_event(
                        self.state,
                        self.qualification_generation,
                        self.source_boundary,
                        head,
                    )
                )
                or not self.window_open(self.clock())
                or not fresh(self.state, head.wake_id, target, self.epoch)
                or not self.custody(target, self.workspace)
                or not self.window_open(self.clock())
            ):
                return self.unknown("native-qualification-reserved-not-dispatched")
            return self.native(target, head, text, turn_id)
        except Exception:
            # Even a failed save can have committed. Never fall back after reservation
            # attempt, never consume a second event after an uncertain result.
            return self.unknown("native-qualification-uncertain")
