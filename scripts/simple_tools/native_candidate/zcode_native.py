"""Source-only native delivery candidate. No activation or implicit fallback.

The caller owns authenticated registry/session binding and passes it to this module.
No credential loading lives here. The transport owns pinned local host verification.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any


class NativeRefused(Exception):
    pass


def canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def _load(path: Path, dirfd: int) -> dict | None:
    try:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dirfd)
    except FileNotFoundError:
        try:
            os.stat(path.name, dir_fd=dirfd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        raise NativeRefused("journal-unreadable-existing")
    with os.fdopen(fd) as f:
        st = os.fstat(f.fileno())
        if (
            not stat.S_ISREG(st.st_mode)
            or st.st_uid != os.getuid()
            or st.st_mode & 0o077
            or st.st_size > 65536
            or st.st_nlink != 1
        ):
            raise NativeRefused("journal-invalid-custody")
        value = json.load(f)
    if not isinstance(value, dict):
        raise NativeRefused("journal-invalid")
    return value


def _save(path: Path, value: dict, dirfd: int) -> None:
    # Atomic durable state transition. A failed/uncertain transition is never permission
    # to send. The dispatching record is fsynced before the transport can run.
    # Unique names leave crash debris harmless. Never delete a preexisting sibling,
    # and never infer whether dispatch happened from temporary-file presence.
    temp = path.name + "." + uuid.uuid4().hex + ".tmp"
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=dirfd)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(canonical(value))
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path.name, src_dir_fd=dirfd, dst_dir_fd=dirfd)
        os.fsync(dirfd)
    finally:
        try:
            os.unlink(temp, dir_fd=dirfd)
        except FileNotFoundError:
            pass


def outcome(record: Mapping[str, Any], reply: Mapping[str, Any] | None) -> dict:
    """ACK is admission only. No ACK or failed ACK proves a safe retry."""
    key = record["command_id"]
    if not isinstance(reply, Mapping) or reply.get("commandId") != key:
        return {"outcome": "unknown", "code": "native-unreconciled"}
    status = reply.get("status")
    result = reply.get("result")
    if status in ("accepted", "duplicate") and isinstance(result, Mapping):
        if (
            result.get("type") == "inputAccepted"
            and result.get("inputId") == key
            and result.get("delivery") in ("queue", "startNow")
        ):
            return {
                "outcome": "unknown",
                "code": "native-admitted-not-confirmed",
                "admission": result["delivery"],
            }
    # Rejected/stale/failed/noop can follow durable admission or lack a final fact.
    # Leave every uncertain receipt terminal to this dispatch, never retryable.
    return {
        "outcome": "unknown",
        "code": "native-ack-"
        + (
            status
            if status in ("accepted", "duplicate", "rejected", "stale", "failed", "noop")
            else "invalid"
        ),
    }


def compact_ack(reply):
    if not isinstance(reply, Mapping):
        return None
    status = reply.get("status")
    command = reply.get("commandId")
    if (
        status not in ("accepted", "duplicate", "failed", "noop", "rejected", "stale")
        or not isinstance(command, str)
        or len(command) > 256
    ):
        return None
    data = {"commandId": command, "status": status}
    reason = reply.get("reasonCode")
    if isinstance(reason, str) and __import__("re").fullmatch(
        r"(?:fault|guard|proto)\.[A-Za-z0-9_.-]{1,120}", reason
    ):
        data["reasonCode"] = reason
    revision = reply.get("revisionAtDecision")
    if type(revision) is int and 0 <= revision <= 2**53 - 1:
        data["revisionAtDecision"] = revision
    result = reply.get("result")
    if isinstance(result, Mapping):
        data["result"] = {
            k: result[k]
            for k in ("type", "inputId", "delivery", "messageId")
            if k in result and isinstance(result[k], str) and len(result[k]) <= 256
        }
    return data


class NativeDelivery:
    def __init__(
        self,
        folder: Path,
        transport: Callable[[dict], dict],
        verify: Callable[[dict], bool],
    ):
        self.folder, self.transport, self.verify = folder, transport, verify
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        if folder.resolve() != folder.absolute():
            raise NativeRefused("journal-path-symlink")
        if (
            folder.is_symlink()
            or folder.stat().st_uid != os.getuid()
            or folder.stat().st_mode & 0o077
        ):
            raise NativeRefused("journal-not-private")
        self.dirfd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        st = os.fstat(self.dirfd)
        if st.st_uid != os.getuid() or st.st_mode & 0o077:
            os.close(self.dirfd)
            raise NativeRefused("journal-not-private")

    def close(self):
        if getattr(self, "dirfd", None) is not None:
            os.close(self.dirfd)
            self.dirfd = None

    def __del__(self):
        self.close()

    @staticmethod
    def _valid_record(r, command_id):
        required = {
            "version",
            "identity",
            "command_id",
            "binding",
            "intent",
            "phase",
            "payload_sha256",
        }
        if not required <= set(r) or set(r) - required - {"receipt", "last_ack"}:
            return False
        intent = r.get("intent")
        if not isinstance(intent, dict) or set(intent) != {
            "binding",
            "wake_key",
            "text",
            "client_id",
            "requestedDelivery",
        }:
            return False
        return (
            r.get("version") == 1
            and r.get("command_id") == command_id
            and r.get("phase") in ("dispatching", "dispatched")
            and r.get("binding") == intent.get("binding")
            and intent.get("client_id") == "echo-review-alerts"
            and intent.get("requestedDelivery") == "queue"
            and isinstance(intent.get("text"), str)
            and bool(intent["text"])
            and r.get("identity") == digest(intent)
            and r.get("payload_sha256")
            == digest({"text": intent["text"], "requestedDelivery": "queue"})
            and command_id
            == "app-control-echo-review-" + digest({"wake_key": intent.get("wake_key")})
        )

    def deliver(self, binding: dict, wake_key: str, text: str, *, fresh: bool) -> dict:
        # Source candidate supports Linux local standalone-server only. Mac is excluded.
        if (
            not isinstance(binding, dict)
            or not isinstance(wake_key, str)
            or len(wake_key) > 128
            or not isinstance(text, str)
            or len(text.encode()) > 16000
        ):
            return {"outcome": "refused", "code": "native-input-invalid"}
        if (
            set(binding) != {"host", "local", "session_id", "workspace", "generation"}
            or binding.get("host") != "server"
            or binding.get("local") is not True
        ):
            return {"outcome": "refused", "code": "native-local-server-only"}
        if (
            not wake_key
            or not text
            or not isinstance(binding.get("session_id"), str)
            or not binding["session_id"]
            or len(binding["session_id"]) > 512
            or not binding.get("workspace")
            or type(binding.get("generation")) is not int
            or binding["generation"] < 1
            or not isinstance(binding.get("workspace"), str)
            or not os.path.isabs(binding["workspace"])
        ):
            return {"outcome": "refused", "code": "native-binding-invalid"}
        fixed = {
            "binding": binding,
            "wake_key": wake_key,
            "text": text,
            "client_id": "echo-review-alerts",
            "requestedDelivery": "queue",
        }
        identity = digest(fixed)
        # Key scoped to original logical wake, not text or mutable binding. Changing a
        # generation/workspace/payload cannot manufacture a second dispatch slot.
        name = digest({"wake_key": wake_key})
        command_id = "app-control-echo-review-" + name
        path = self.folder / (name + ".json")
        fd = os.open(
            name + ".lock",
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
            0o600,
            dir_fd=self.dirfd,
        )
        with os.fdopen(fd, "w") as lock:
            st = os.fstat(lock.fileno())
            if (
                not stat.S_ISREG(st.st_mode)
                or st.st_uid != os.getuid()
                or st.st_mode & 0o077
                or st.st_nlink != 1
            ):
                return {"outcome": "refused", "code": "native-lock-custody"}
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return {"outcome": "busy", "code": "native-key-locked"}
            try:
                old = _load(path, self.dirfd)
            except (OSError, ValueError, NativeRefused):
                return {"outcome": "refused", "code": "native-journal-corrupt"}
            if old is not None and not self._valid_record(old, command_id):
                return {"outcome": "refused", "code": "native-journal-corrupt"}
            if old is not None and old.get("identity") != identity:
                return {
                    "outcome": "refused",
                    "code": "native-immutable-binding-conflict",
                }
            if not self.verify(binding):
                return {"outcome": "refused", "code": "native-binding-changed"}
            if old is not None:
                # No resubmit even when old state says unknown, failed, or dispatching.
                action = {
                    "action": "query",
                    "binding": binding,
                    "command_id": command_id,
                    "client_id": "echo-review-alerts",
                }
                try:
                    response = self.transport(action)
                except Exception:
                    return {"outcome": "unknown", "code": "native-query-unavailable"}
                reply = response.get("ack") if isinstance(response, dict) else None
                answer = outcome(old, reply)
                old["receipt"] = answer
                old["last_ack"] = compact_ack(reply)
                _save(path, old, self.dirfd)
                return answer
            if not fresh:
                return {"outcome": "refused", "code": "native-legacy-attempt-excluded"}
            record = {
                "version": 1,
                "identity": identity,
                "command_id": command_id,
                "binding": binding,
                "intent": fixed,
                "phase": "dispatching",
                "payload_sha256": digest({"text": text, "requestedDelivery": "queue"}),
            }
            _save(path, record, self.dirfd)
            # From this point even transport startup or verification failure consumes
            # the slot. Conservative underdelivery is preferable to duplicate execution.
            try:
                response = self.transport(
                    {
                        "action": "dispatch",
                        "binding": binding,
                        "command_id": command_id,
                        "client_id": "echo-review-alerts",
                        "text": text,
                    }
                )
                reply = response.get("ack") if isinstance(response, dict) else None
                answer = outcome(record, reply)
            except Exception:
                answer = {"outcome": "unknown", "code": "native-dispatch-uncertain"}
            record["phase"] = "dispatched"
            record["receipt"] = answer
            record["last_ack"] = compact_ack(reply) if "reply" in locals() else None
            _save(path, record, self.dirfd)
            return answer
