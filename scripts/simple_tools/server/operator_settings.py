"""The Operator's canonical nonsecret settings document and autosave transaction.

The existing forced-command editor is the only writer.  Its folder lock and the
canonical revision serialize editor clients; they do not claim provider-level CAS
against an unrelated direct Infisical writer.  A mismatched provider reread is a
conflict, never a successful save or a patched last-known-good value.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import secrets
import stat
import tempfile
import time
import uuid
from pathlib import Path
from urllib.parse import quote

from .. import secretscan
from ..operator_github_state import LedgerError, NonceLedger
from . import eavesdropper_settings_edit as editor

KEY = "OPERATOR_SETTINGS"
FOLDER = "/operator-settings"
READ_BINDING = "OPERATOR_SETTINGS_INFISICAL_READ_TOKEN"
WRITE_BINDING = "OPERATOR_SETTINGS_INFISICAL_WRITE_TOKEN"
PROJECT = "INFISICAL_OPERATOR_SETTINGS_PROJECT_ID"
MAX_DOCUMENT_BYTES = 65536
MAX_DOCUMENT_DEPTH = 16
MAX_DOCUMENT_NODES = 8192
SCHEMA_VERSION = 1
VERIFIED_COPY_NAME = ".operator-settings.verified.json"
MAX_COPY_AGE_SECONDS = 48 * 3600
MAX_TRANSACTION_SECONDS = 150.0
REQUEST_TTL_SECONDS = 24 * 3600
REQUEST_CAPACITY = 1000
REQUEST_NAMESPACE = "operator-settings"
REQUEST_STATE_DIRECTORY = ".operator-settings-state"


class SettingsError(Exception):
    """A fixed reason only; provider response bodies and credentials never escape."""

    def __init__(self, code, *, required_binding=None, field=None):
        super().__init__(code)
        self.code = code
        self.required_binding = (
            required_binding if required_binding in (READ_BINDING, WRITE_BINDING, PROJECT) else None
        )
        self.field = field
        self.request_id = None
        self.outcome = None


class MutationError(SettingsError):
    """A safe mutation classification, without provider body or exception text."""

    def __init__(self, *, status=None, sent=False, retryable=False):
        super().__init__("provider_write_unknown" if sent else "provider_write_refused")
        self.status = status if type(status) is int and 100 <= status <= 599 else None
        self.sent = sent
        self.retryable = retryable


class _Budget:
    """All child I/O shares a bound below the existing 180-second helper."""

    def __init__(self):
        self.deadline = time.monotonic() + MAX_TRANSACTION_SECONDS

    def remaining(self, cap):
        remaining = min(cap, self.deadline - time.monotonic())
        if remaining <= 0:
            raise SettingsError("transaction_timeout")
        return remaining

    def phase(self, store):
        self.remaining(30)
        if hasattr(store, "begin_phase"):
            store.begin_phase(self.deadline)

    def check(self, checker, candidate, path):
        timeout = self.remaining(editor.CHECK_TIMEOUT)
        result = (
            checker(candidate, path)
            if checker is not None
            else editor.daemon_check(candidate, path, timeout=timeout)
        )
        self.remaining(1)
        return result

    def reload(self, reloader, path, wanted):
        timeout = self.remaining(editor.RELOAD_TIMEOUT)
        result = (
            reloader(path)
            if reloader is not None
            else editor.daemon_reload(path, expected_revision=wanted, timeout=timeout)
        )
        self.remaining(1)
        return result


def _bounded(value):
    """Bound both JSON work and internal values before serialization."""
    pending = [(value, 0)]
    count = 0
    while pending:
        item, depth = pending.pop()
        count += 1
        if count > MAX_DOCUMENT_NODES:
            raise SettingsError("document_too_complex")
        if depth > MAX_DOCUMENT_DEPTH:
            raise SettingsError("document_too_deep")
        if isinstance(item, dict):
            if any(not isinstance(key, str) for key in item):
                raise SettingsError("invalid_document")
            pending.extend((child, depth + 1) for pair in item.items() for child in pair)
        elif isinstance(item, list):
            pending.extend((child, depth + 1) for child in item)
        elif item is not None and type(item) not in (str, int, float, bool):
            raise SettingsError("invalid_document")


def _identity(request_id, created_at, *, now=None, expired_ok=False):
    try:
        if not isinstance(request_id, str) or str(uuid.UUID(request_id)) != request_id:
            raise ValueError()
    except (ValueError, AttributeError):
        raise SettingsError("request_id_required") from None
    if type(created_at) is not int or not 0 < created_at < 2**53:
        raise SettingsError("request_time_required")
    age = (time.time() if now is None else now) - created_at / 1000
    if age < -300 or not expired_ok and age > REQUEST_TTL_SECONDS:
        raise SettingsError("request_expired")
    return request_id


def _ledger(path):
    # The retained primitive creates and fsyncs each directory entry itself;
    # pre-creating it here would bypass that durable parent-entry checkpoint.
    return NonceLedger(path.parent / REQUEST_STATE_DIRECTORY, retain_unsettled=True)


class _Intent:
    """One protected Operator request record; never stores settings or secrets.

    Terminal records last 24 hours.  Pending, unknown and replay claims never
    expire or make room for another write; a full ledger refuses new requests.
    The creation-time fence prevents an evicted old request from being rearmed.
    """

    def __init__(
        self,
        path,
        action,
        request_id,
        created_at,
        payload=None,
        *,
        ledger=None,
        now=None,
        reconcile=False,
    ):
        self.now = time.time() if now is None else now
        self.id = _identity(request_id, created_at, now=self.now, expired_ok=reconcile)
        self.created = created_at
        self.ledger = ledger or _ledger(path)
        self.metadata = None
        self.claimed = False
        self.action = action
        self.fingerprint = (
            hashlib.sha256(
                encode(
                    {
                        "action": action,
                        "payload": payload,
                        "requestCreatedAt": created_at,
                    }
                ).encode()
            ).hexdigest()
            if not reconcile
            else None
        )

    def prior(self):
        row = self.ledger.lookup(REQUEST_NAMESPACE, self.id)
        if row is None:
            return None
        outcome, raw = row
        try:
            value = json.loads(raw, object_pairs_hook=_unique_pairs)
            if (
                outcome not in ("pending", "outcome_unknown", "replay", "ok", "refused")
                or not isinstance(value, dict)
                or set(value)
                != {
                    "schemaVersion",
                    "operation",
                    "fingerprint",
                    "createdAt",
                    "baseRevision",
                    "targetRevision",
                    "phase",
                }
                or type(value["schemaVersion"]) is not int
                or value["schemaVersion"] != 1
                or value["operation"] not in ("save", "initialize", "cancelled")
                or type(value["createdAt"]) is not int
                or value["phase"]
                not in ("prepared", "sending", "persisted", "complete", "refused", "cancelled")
                or any(
                    not isinstance(value[key], str) or not editor.SHA256_RE.fullmatch(value[key])
                    for key in ("fingerprint", "baseRevision", "targetRevision")
                )
            ):
                raise ValueError()
        except (ValueError, TypeError, RecursionError, SettingsError):
            raise SettingsError("request_record_invalid") from None
        if value["createdAt"] != self.created or (
            self.fingerprint is not None
            and value["fingerprint"] != self.fingerprint
            and value["operation"] != "cancelled"
        ):
            raise SettingsError("request_id_conflict")
        self.metadata = value
        return outcome

    def cancel_absent(self):
        self.metadata = {
            "schemaVersion": 1,
            "operation": "cancelled",
            "fingerprint": "0" * 64,
            "createdAt": self.created,
            "baseRevision": "0" * 64,
            "targetRevision": "0" * 64,
            "phase": "cancelled",
        }
        result = self.ledger.begin(
            REQUEST_NAMESPACE,
            self.id,
            self.created / 1000 + REQUEST_TTL_SECONDS,
            self.now,
            REQUEST_CAPACITY,
            response=encode(self.metadata),
        )
        if result != "fresh":
            raise SettingsError("request_capacity" if result == "capacity" else "request_conflict")
        self.ledger.settle(REQUEST_NAMESPACE, self.id, "refused", encode(self.metadata))

    def cancel_prepared(self):
        # Every provider mutation is preceded by a committed Sending record
        # under this same folder lock.  Prepared therefore proves that an
        # abandoned request never entered the mutation call.
        self.metadata.update(operation="cancelled", phase="cancelled")
        self.ledger.settle(REQUEST_NAMESPACE, self.id, "refused", encode(self.metadata))

    def begin(self, base, target):
        self.metadata = {
            "schemaVersion": 1,
            "operation": self.action,
            "fingerprint": self.fingerprint,
            "createdAt": self.created,
            "baseRevision": base,
            "targetRevision": target,
            "phase": "prepared",
        }
        result = self.ledger.begin(
            REQUEST_NAMESPACE,
            self.id,
            self.created / 1000 + REQUEST_TTL_SECONDS,
            self.now,
            REQUEST_CAPACITY,
            response=encode(self.metadata),
        )
        if result == "capacity":
            raise SettingsError("request_capacity")
        if result != "fresh":
            # All editor/reconcile calls hold the same folder lock.  An
            # unexpected external ledger writer cannot cause a second write.
            raise SettingsError("request_conflict")
        self.claimed = True

    def mark(self, phase):
        self.metadata["phase"] = phase
        self.ledger.settle(REQUEST_NAMESPACE, self.id, "pending", encode(self.metadata))

    def prepare(self, base, target):
        self.metadata.update(baseRevision=base, targetRevision=target)
        self.mark("prepared")

    def finish(self, answer):
        if not self.claimed:
            return answer
        ok = bool(answer and answer.get("ok"))
        unknown = self.metadata["phase"] in ("sending", "persisted")
        state = "ok" if ok else "outcome_unknown" if unknown else "refused"
        self.metadata["phase"] = (
            "complete" if ok else self.metadata["phase"] if unknown else "refused"
        )
        self.ledger.settle(REQUEST_NAMESPACE, self.id, state, encode(self.metadata))
        if answer is not None:
            answer.update(
                requestID=self.id, outcome="ok" if ok else "unknown" if unknown else "refused"
            )
        return answer


def encode(document):
    _bounded(document)
    try:
        raw = json.dumps(document, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        raise SettingsError("invalid_document") from None
    if len(raw.encode("utf-8")) > MAX_DOCUMENT_BYTES:
        raise SettingsError("document_too_large")
    return raw


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise SettingsError("invalid_document")
        result[key] = value
    return result


def decode(raw):
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_DOCUMENT_BYTES:
        raise SettingsError("invalid_document")
    try:
        document = json.loads(raw, object_pairs_hook=_unique_pairs)
    except (ValueError, TypeError, RecursionError):
        raise SettingsError("invalid_document") from None
    if (
        not isinstance(document, dict)
        or set(document) != {"schemaVersion", "settings"}
        or type(document["schemaVersion"]) is not int
        or document["schemaVersion"] != SCHEMA_VERSION
        or not isinstance(document["settings"], dict)
        or not document["settings"]
    ):
        raise SettingsError("invalid_document")
    encode(document)
    return document


def revision(document):
    return hashlib.sha256(encode(document).encode("utf-8")).hexdigest()


def document(settings):
    return decode(encode({"schemaVersion": SCHEMA_VERSION, "settings": settings}))


def overlay_text(text, allowlist, settings):
    """Validate through the existing schema and preserve unrelated TOML bytes."""
    if not isinstance(settings, dict) or not settings:
        raise SettingsError("invalid_document")
    result = text
    keys = sorted(settings)
    for offset in range(0, len(keys), editor.MAX_CHANGES):
        batch = {key: settings[key] for key in keys[offset : offset + editor.MAX_CHANGES]}
        result, _ = editor.build_plan(
            result, allowlist, batch, remove=_canonical_removals(result, batch)
        )
    return result


def _canonical_removals(text, settings):
    """A verified document owns its allowed whole tables, not unrelated TOML."""
    have = editor.settings_of(editor._parse(text, "runtime file"))
    removed = []
    for key, value in settings.items():
        current = have.get(editor.split_key(key))
        if isinstance(value, dict) and isinstance(current, dict):
            removed.extend("%s.%s" % (key, sub) for sub in set(current) - set(value))
    return sorted(removed)


def _validated_overlay(text, allowlist, current, *, known=()):
    """Provider values must pass the nonsecret schema before any response map."""
    try:
        result = overlay_text(text, allowlist, current["settings"])
    except editor.Refusal:
        raise SettingsError("canonical_document_invalid") from None
    _scan_nonsecret(current["settings"], known)
    return result


def _scan_nonsecret(values, known=()):
    """The schema must validate field names before this fixed-field diagnostic."""
    _bounded(values)
    for field, value in values.items():
        pending = [value]
        while pending:
            item = pending.pop()
            if isinstance(item, dict):
                pending.extend(item.values())
            elif isinstance(item, list):
                pending.extend(item)
            elif isinstance(item, str) and secretscan.scan(item, known):
                raise SettingsError("secret_detected", field=field)


def _manifest():
    path = Path(__file__).resolve().parents[2] / "fleet-ops-eavesdropper-settings.json"
    try:
        return editor.validate_allowlist(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, editor.Refusal):
        raise SettingsError("manifest_unavailable") from None


def _copy_parent(path):
    """Open the protected parent by descriptors; never traverse a symbolic link."""
    parts = Path(os.path.abspath(path.parent)).parts
    flags = (
        os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    )
    descriptor = os.open(parts[0], flags)
    try:
        for index, part in enumerate(parts[1:], 1):
            child = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
            info = os.fstat(descriptor)
            sticky_ancestor = (
                index < len(parts) - 1 and info.st_uid == 0 and info.st_mode & stat.S_ISVTX
            )
            if (
                not stat.S_ISDIR(info.st_mode)
                or info.st_uid not in (0, os.getuid())
                or info.st_mode & 0o022
                and not sticky_ancestor
            ):
                raise SettingsError("verified_copy_unavailable")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _copy_file_safe(info):
    return (
        stat.S_ISREG(info.st_mode)
        and info.st_uid == os.getuid()
        and not info.st_mode & 0o077
        and info.st_nlink == 1
    )


def _verified_copy(path, environ, *, now=None):
    parent = None
    try:
        parent = _copy_parent(path)
        descriptor = os.open(
            VERIFIED_COPY_NAME,
            os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent,
        )
        with os.fdopen(descriptor, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not _copy_file_safe(info):
                raise SettingsError("verified_copy_unavailable")
            raw = handle.read(MAX_DOCUMENT_BYTES + 1025)
        if len(raw) > MAX_DOCUMENT_BYTES + 1024:
            raise SettingsError("verified_copy_unavailable")
        stored = json.loads(raw, object_pairs_hook=_unique_pairs)
        if (
            not isinstance(stored, dict)
            or set(stored)
            != {
                "projectId",
                "environment",
                "folder",
                "key",
                "schemaVersion",
                "verifiedAtEpoch",
                "revision",
                "document",
            }
            or stored["projectId"] != (environ.get(PROJECT) or "").strip()
            or stored["environment"] != "prod"
            or stored["folder"] != FOLDER
            or stored["key"] != KEY
            or type(stored["schemaVersion"]) is not int
            or stored["schemaVersion"] != SCHEMA_VERSION
            or type(stored["verifiedAtEpoch"]) not in (int, float)
            or revision(stored["document"]) != stored["revision"]
        ):
            raise SettingsError("verified_copy_unavailable")
        age = (time.time() if now is None else now) - stored["verifiedAtEpoch"]
        if not math.isfinite(age) or age < -300 or age > MAX_COPY_AGE_SECONDS:
            raise SettingsError("verified_copy_stale")
        return decode(encode(stored["document"]))
    except (OSError, ValueError, KeyError, TypeError, RecursionError):
        raise SettingsError("verified_copy_unavailable") from None
    finally:
        if parent is not None:
            os.close(parent)


def remember_verified(path, value, environ, *, now=None):
    """Cache only an actually verified canonical document, never a write guess."""
    payload = json.dumps(
        {
            "projectId": (environ.get(PROJECT) or "").strip(),
            "environment": "prod",
            "folder": FOLDER,
            "key": KEY,
            "schemaVersion": SCHEMA_VERSION,
            "verifiedAtEpoch": time.time() if now is None else now,
            "revision": revision(value),
            "document": value,
        },
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    parent = _copy_parent(path)
    temporary = ".operator-settings-copy-" + secrets.token_hex(12)
    try:
        try:
            existing = os.stat(VERIFIED_COPY_NAME, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            existing = None
        if existing is not None and not _copy_file_safe(existing):
            raise SettingsError("verified_copy_unavailable")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=parent)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, VERIFIED_COPY_NAME, src_dir_fd=parent, dst_dir_fd=parent)
        os.fsync(parent)
    finally:
        try:
            os.unlink(temporary, dir_fd=parent)
        except FileNotFoundError:
            pass
        os.close(parent)


def load_runtime(raw, path, *, environ=None, store=None):
    """Startup/SIGHUP load only; per-message consumers still read Config memory.

    Candidate check-config files retain their proposed bytes.  A configured live
    listener consumes Infisical, or an independently validated verified copy on a
    failed refresh.  A missing bootstrap does not activate this source.
    """
    env = dict(os.environ if environ is None else environ)
    configured_path = env.get(editor.ENV_CONFIG) or editor.DEFAULT_LIVE
    path = Path(path)
    if not env.get(READ_BINDING) or path.resolve() != Path(configured_path).resolve():
        return raw, None
    allowlist = _manifest()
    text = editor._decode(editor.read_live(path)[0])
    source = "infisical"
    try:
        current = (store or InfisicalStore(env)).read()
        candidate = editor._parse(
            _validated_overlay(text, allowlist, current, known=(env.get(READ_BINDING),)),
            "canonical settings",
        )
        from .. import config

        if config.from_dict(candidate).errors:
            raise SettingsError("canonical_config_invalid")
    except (SettingsError, editor.Refusal):
        current = _verified_copy(path, env)
        candidate = editor._parse(
            _validated_overlay(text, allowlist, current, known=(env.get(READ_BINDING),)),
            "verified settings copy",
        )
        from .. import config

        if config.from_dict(candidate).errors:
            raise SettingsError("verified_copy_invalid")
        source = "verified-copy"
    # The daemon records a last-known-good copy only after accepting this Config,
    # not while check-config or a subsequently refused reload is reading it.
    return candidate, {"source": source, "revision": revision(current), "document": current}


class InfisicalStore:
    """One explicit settings binding on the shared scoped Infisical transport."""

    def __init__(self, environ=None, *, writer=False, timeout=20.0):
        self.env = dict(os.environ if environ is None else environ)
        self.writer = writer
        self.token = (self.env.get(WRITE_BINDING if writer else READ_BINDING) or "").strip()
        self.project = (self.env.get(PROJECT) or "").strip()
        if not self.token:
            raise SettingsError(
                "writer_not_configured" if writer else "reader_not_configured",
                required_binding=WRITE_BINDING if writer else READ_BINDING,
            )
        if not re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", self.project):
            raise SettingsError("project_not_configured", required_binding=PROJECT)
        try:
            import fleet_infisical
        except ImportError:
            raise SettingsError("shared_client_unavailable") from None
        self.client = fleet_infisical
        self.env["INFISICAL_READ_PATHS"] = FOLDER
        self.env["INFISICAL_WRITE_PATHS"] = FOLDER
        self.timeout = max(0.1, min(float(timeout), 30.0))
        self.deadline = time.monotonic() + self.timeout

    def begin_phase(self, transaction_deadline):
        self.deadline = min(transaction_deadline, time.monotonic() + self.timeout)

    def _remaining(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise SettingsError("provider_timeout")
        return remaining

    def read(self, *, missing_ok=False):
        try:
            values = self.client.fetch_project(
                self.project,
                FOLDER,
                self.env,
                token=self.token,
                writer=self.writer,
                names=(KEY,),
                timeout=self._remaining(),
            )
        except (self.client.SecretsError, TimeoutError):
            raise SettingsError("provider_read_failed") from None
        if KEY not in values:
            if missing_ok:
                return None
            raise SettingsError("settings_not_initialized")
        return decode(values[KEY])

    def write(self, value):
        return self._mutate("PATCH", value)

    def create(self, value):
        return self._mutate("POST", value)

    def _mutate(self, method, value):
        if not self.writer:
            raise SettingsError("writer_not_configured", required_binding=WRITE_BINDING)
        raw = encode(value)
        try:
            self.client.verify_service_token(
                self.token,
                self.project,
                FOLDER,
                self.env,
                writer=True,
                deadline=self.deadline,
            )
        except TimeoutError:
            raise MutationError(retryable=True) from None
        except self.client.SecretsError as exc:
            # The shared verifier owns the body-free cause classification.
            # No mutation was entered, including when verification is unavailable.
            raise MutationError(retryable=getattr(exc, "failure_kind", None) != "refused") from None
        body = {
            "workspaceId": self.project,
            "environment": "prod",
            "secretPath": FOLDER,
            "type": "shared",
            "secretValue": raw,
        }
        path = "/v3/secrets/raw/" + quote(KEY, safe="")
        try:
            self._remaining()
        except SettingsError:
            raise MutationError(retryable=True) from None
        try:
            status, _ = self.client._api_json(
                method,
                path,
                self.token,
                body,
                self.env,
                deadline=self.deadline,
            )
        except (self.client.SecretsError, TimeoutError, OSError):
            raise MutationError(sent=True, retryable=True) from None
        if status not in (200, 201):
            # Each method has one purpose: PATCH an existing document or POST
            # one absent document, with no upsert or alternate-grant fallback.
            if status == 429:
                raise MutationError(status=status, retryable=True)
            uncertain = not (type(status) is int and 400 <= status < 500 and status != 408)
            raise MutationError(status=status, sent=uncertain, retryable=uncertain)


def _base_answer(action, current, path, *, known=()):
    return {
        "tool": "eavesdropper-settings",
        "action": action,
        "ok": False,
        "result": "failed",
        "source": "infisical",
        "autosaveEnabled": action in ("get", "save"),
        "persisted": False,
        "readback": False,
        "runtimeApplied": False,
        "settings": copy.deepcopy(current["settings"]),
        "revision": revision(current),
        "baseSHA256": revision(current),
        "runtimeSHA256": editor.sha256(editor.read_live(path)[0]),
        "channelNames": channel_names(known),
        "channelNamesSource": "daemon-Zulip-cache",
    }


def _accepted_revision(wanted):
    try:
        with open(editor._status_path(), "rb") as handle:
            raw = handle.read(4 * 1024 * 1024 + 1)
        if len(raw) > 4 * 1024 * 1024:
            return False
        status = json.loads(raw)
        updated = status.get("updated")
        age = time.time() - updated
        return (
            type(updated) in (int, float)
            and math.isfinite(age)
            and -300 <= age <= 120
            and status.get("instance") == "server"
            and status.get("config_errors") == []
            and status.get("operator_settings_revision") == wanted
        )
    except (OSError, ValueError, TypeError, AttributeError):
        return False


def _repeat(path, allowlist, store, intent, prior, action):
    """A repeat only reads provider/runtime state, never writes, reloads or copies."""
    metadata = intent.metadata
    operation = metadata["operation"]
    known_ok = prior == "ok" or prior == "replay" and metadata["phase"] == "complete"
    answer = {
        "tool": "eavesdropper-settings",
        "action": action,
        "operation": operation,
        "requestID": intent.id,
        "outcome": "ok" if known_ok else "refused" if prior == "refused" else "unknown",
        "ok": False,
        "result": "failed",
        "source": "infisical",
        "autosaveEnabled": operation == "save",
        "persisted": False,
        "readback": False,
        "runtimeApplied": False,
        "errorCode": "prior_result_unavailable" if known_ok else "outcome_unknown",
        "detail": "The previous outcome has no current verified runtime receipt; no write was repeated",
    }
    if operation == "cancelled":
        intent.ledger.settle(REQUEST_NAMESPACE, intent.id, "refused", encode(metadata))
        answer.update(
            outcome="refused",
            cancelled=True,
            errorCode="request_cancelled",
            detail="This absent request was durably cancelled; its ID cannot write",
        )
        return answer
    if prior == "refused":
        answer.update(
            errorCode="request_refused",
            detail="This request was definitively refused; no write was repeated",
        )
        return answer
    if known_ok:
        claimed = intent.ledger.begin(
            REQUEST_NAMESPACE,
            intent.id,
            intent.created / 1000 + REQUEST_TTL_SECONDS,
            min(intent.now, intent.created / 1000 + REQUEST_TTL_SECONDS),
            REQUEST_CAPACITY,
        )
        if claimed not in (
            ("repeat-claimed", "ok", encode(metadata)),
            ("repeat", "replay", encode(metadata)),
        ):
            raise SettingsError("request_record_invalid")
    try:
        current = store.read()
        raw, _ = editor.read_live(path)
        text = _validated_overlay(
            editor._decode(raw), allowlist, current, known=(getattr(store, "token", None),)
        )
        answer.update(_base_answer(action, current, path, known=(getattr(store, "token", None),)))
        answer.update(
            operation=operation,
            requestID=intent.id,
            outcome="ok" if known_ok else "refused" if prior == "refused" else "unknown",
            autosaveEnabled=operation == "save",
        )
        if prior == "refused":
            answer.update(
                errorCode="request_refused",
                detail="This request was definitively refused; no write was repeated",
            )
            return answer
        target = metadata["targetRevision"]
        if revision(current) != target:
            answer.update(
                errorCode="request_superseded" if known_ok else "outcome_unknown",
                detail="Current settings differ from this request; no write was repeated",
            )
            return answer
        answer.update(persisted=True, readback=True)
        if operation == "initialize":
            accepted = revision(
                accepted_snapshot(editor._parse(editor._decode(raw), "runtime file"), allowlist)
            ) == target and _accepted_by_daemon(target)
        else:
            accepted = text.encode("utf-8") == raw and _accepted_revision(target)
        if accepted:
            metadata["phase"] = "complete"
            intent.ledger.settle(REQUEST_NAMESPACE, intent.id, "ok", encode(metadata))
            known_ok = True
            answer.update(
                ok=True,
                result="saved" if operation == "save" else "initialized",
                outcome="ok",
                runtimeApplied=operation == "save",
                detail="The prior request was verified without repeating its write",
            )
            answer.pop("errorCode", None)
        else:
            answer.update(
                errorCode="prior_result_unavailable" if known_ok else "outcome_unknown",
                detail="Canonical persistence is verified; prior runtime acceptance remains unconfirmed",
            )
        return answer
    except (SettingsError, editor.Refusal, OSError):
        # A failed current read never erases a definite earlier success.
        return answer
    finally:
        if known_ok:
            metadata["phase"] = "complete"
            intent.ledger.settle(REQUEST_NAMESPACE, intent.id, "ok", encode(metadata))


def reconcile(path, allowlist, request_id, created_at, *, store=None, ledger=None, now=None):
    budget = _Budget()
    try:
        with editor._FolderLock(path.parent):
            intent = _Intent(
                path, "reconcile", request_id, created_at, ledger=ledger, now=now, reconcile=True
            )
            prior = intent.prior()
            if prior is None:
                intent.cancel_absent()
                prior = "refused"
            elif prior == "pending" and intent.metadata["phase"] == "prepared":
                intent.cancel_prepared()
                prior = "refused"
            if intent.metadata["operation"] != "cancelled":
                store = store or InfisicalStore(writer=True)
                budget.phase(store)
            return _repeat(path, allowlist, store, intent, prior, "reconcile")
    except (LedgerError, OSError):
        raise SettingsError("request_storage_unavailable") from None


def accepted_snapshot(raw, allowlist):
    """The currently accepted nonsecret fields, validated by the shared schema."""
    values = editor.allowlisted_values(raw, allowlist)
    for key, value in values.items():
        table, name = editor.split_key(key)
        editor.check_value(
            editor.spec_for(allowlist, table, name),
            value,
            key,
            seat=table[1] if table[0] == "seat" else None,
            current=value,
        )
    _scan_nonsecret(values)
    return document(values)


def _accepted_by_daemon(wanted, *, now=None):
    try:
        with open(editor._status_path(), "rb") as handle:
            raw = handle.read(4 * 1024 * 1024 + 1)
        if len(raw) > 4 * 1024 * 1024:
            return False
        status = json.loads(raw)
        updated = status.get("updated")
        age = (time.time() if now is None else now) - updated
        return (
            type(updated) in (int, float)
            and math.isfinite(age)
            and -300 <= age <= 120
            and status.get("instance") == "server"
            and status.get("config_errors") == []
            and status.get("operator_settings_snapshot_revision") == wanted
        )
    except (OSError, ValueError, TypeError, AttributeError):
        return False


def _initialize_core(
    path, allowlist, expected, *, store=None, checker=None, accepted=None, intent=None, budget=None
):
    """Explicit one-time migration, never an autosave or an overwrite/upsert."""
    if not isinstance(expected, str) or not editor.SHA256_RE.fullmatch(expected):
        raise editor.Refusal(
            "version_required", "initialize requires the current runtime-file revision"
        )
    budget = budget or _Budget()
    store = store or InfisicalStore(writer=True)
    budget.phase(store)
    accepted = accepted or _accepted_by_daemon
    raw, _ = editor.read_live(path)
    if editor.sha256(raw) != expected:
        raise editor.Refusal("version_conflict", "the runtime file changed before initialization")
    initial = accepted_snapshot(editor._parse(editor._decode(raw), "runtime file"), allowlist)
    known = (getattr(store, "token", None),)
    _scan_nonsecret(initial["settings"], known)
    answer = _base_answer("initialize", initial, path, known=known)
    if not accepted(revision(initial)):
        answer.update(
            errorCode="runtime_not_accepted",
            detail="The active daemon has not accepted this settings snapshot",
        )
        return answer
    intent.prepare(expected, revision(initial))
    with tempfile.TemporaryDirectory(prefix="operator-settings-init-") as scratch:
        candidate = Path(scratch) / "candidate.toml"
        candidate.write_bytes(raw)
        checked = budget.check(checker, candidate, path)
    if not checked.get("ok"):
        answer.update(
            errorCode="check_failed",
            check=checked,
            detail="The daemon checker refused initialization",
        )
        return answer
    if editor.read_live(path)[0] != raw or not accepted(revision(initial)):
        answer.update(
            errorCode="version_conflict",
            detail="The accepted runtime snapshot changed during validation",
        )
        return answer
    budget.phase(store)
    existing = store.read(missing_ok=True)
    if existing is not None:
        answer.update(
            errorCode="already_initialized",
            detail="Canonical settings already exist; no create was attempted",
        )
        return answer
    answer["writeAttempted"] = True
    intent.mark("sending")
    try:
        # POST is create-only.  Never fall back to PATCH after a concurrent
        # initializer or an ambiguous response.  Provider create rejects an
        # existing shared key; the exact readback is a separate receipt.
        store.create(initial)
    except MutationError as exc:
        if not exc.sent:
            intent.mark("refused")
        answer.update(
            errorCode=exc.code,
            writeOutcome="unknown" if exc.sent else "refused",
            requestMayHaveBeenSent=exc.sent,
            retryable=exc.retryable,
            detail="Canonical initialization has no verified create receipt; no create was repeated",
        )
        if exc.status is not None:
            answer["providerStatus"] = exc.status
        return answer
    except SettingsError:
        answer.update(
            errorCode="initialization_conflict",
            detail="Canonical initialization was refused or has no definite create receipt",
        )
        return answer
    try:
        verified = store.read()
    except SettingsError:
        answer.update(
            errorCode="readback_failed",
            detail="Initialization has no verified canonical readback; do not repeat the create",
        )
        return answer
    if revision(verified) != revision(initial):
        answer.update(
            errorCode="readback_conflict",
            detail="Initialization did not read back the accepted snapshot",
        )
        return answer
    answer.update(
        ok=True,
        result="initialized",
        persisted=True,
        readback=True,
        revision=revision(verified),
        baseSHA256=revision(verified),
        settings=copy.deepcopy(verified["settings"]),
        detail="Canonical settings initialized and read back; runtime source is not activated",
    )
    return answer


def initialize(
    path,
    allowlist,
    expected,
    *,
    store=None,
    checker=None,
    accepted=None,
    request_id=None,
    created_at=None,
    ledger=None,
    now=None,
):
    return _execute(
        "initialize",
        path,
        allowlist,
        expected,
        None,
        None,
        store=store,
        checker=checker,
        accepted=accepted,
        request_id=request_id,
        created_at=created_at,
        ledger=ledger,
        now=now,
    )


def save(
    path,
    allowlist,
    requested,
    expected,
    *,
    remove=None,
    store=None,
    checker=None,
    reloader=None,
    request_id=None,
    created_at=None,
    ledger=None,
    now=None,
):
    return _execute(
        "save",
        path,
        allowlist,
        expected,
        requested,
        remove,
        store=store,
        checker=checker,
        reloader=reloader,
        request_id=request_id,
        created_at=created_at,
        ledger=ledger,
        now=now,
    )


def _execute(
    action,
    path,
    allowlist,
    expected,
    requested,
    remove,
    *,
    store=None,
    checker=None,
    reloader=None,
    accepted=None,
    request_id=None,
    created_at=None,
    ledger=None,
    now=None,
):
    budget = _Budget()
    try:
        with editor._FolderLock(path.parent):
            intent = _Intent(
                path,
                action,
                request_id,
                created_at,
                {"set": requested, "remove": sorted(remove or []), "base": expected},
                ledger=ledger,
                now=now,
            )
            prior = intent.prior()
            if prior is not None:
                if intent.metadata["operation"] != "cancelled":
                    store = store or InfisicalStore(writer=True)
                    budget.phase(store)
                return _repeat(path, allowlist, store, intent, prior, action)
            base = (
                expected
                if isinstance(expected, str) and editor.SHA256_RE.fullmatch(expected)
                else "0" * 64
            )
            intent.begin(base, "0" * 64)
            try:
                if intent.ledger.has_unsettled(REQUEST_NAMESPACE, intent.id):
                    return intent.finish(
                        {
                            "tool": "eavesdropper-settings",
                            "action": action,
                            "ok": False,
                            "result": "failed",
                            "source": "infisical",
                            "autosaveEnabled": action == "save",
                            "persisted": False,
                            "readback": False,
                            "runtimeApplied": False,
                            "errorCode": "prior_outcome_unknown",
                            "detail": "A prior settings request remains unsettled; reconcile it before a new edit",
                        }
                    )
                if action == "save":
                    answer = _save_core(
                        path,
                        allowlist,
                        requested,
                        expected,
                        remove=remove,
                        store=store,
                        checker=checker,
                        reloader=reloader,
                        intent=intent,
                        budget=budget,
                    )
                else:
                    answer = _initialize_core(
                        path,
                        allowlist,
                        expected,
                        store=store,
                        checker=checker,
                        accepted=accepted,
                        intent=intent,
                        budget=budget,
                    )
            except BaseException as exc:
                intent.finish(None)
                if isinstance(exc, SettingsError):
                    exc.request_id = intent.id
                    exc.outcome = (
                        "unknown"
                        if intent.metadata["phase"] in ("sending", "persisted")
                        else "refused"
                    )
                raise
            if not intent.claimed:
                answer.update(requestID=intent.id, outcome="refused")
            return intent.finish(answer)
    except (LedgerError, OSError):
        raise SettingsError("request_storage_unavailable") from None


def channel_names(known=()):
    """Actual names already observed by the daemon, with no new credential read."""
    try:
        with open(editor._status_path(), "rb") as handle:
            raw = handle.read(4 * 1024 * 1024 + 1)
        if len(raw) > 4 * 1024 * 1024:
            return []
        value = json.loads(raw).get("channel_names")
        if not isinstance(value, list) or len(value) > 500:
            return []
        return sorted(
            {
                name
                for name in value
                if isinstance(name, str)
                and 0 < len(name) <= 60
                and not secretscan.scan(name, known)
            }
        )
    except (OSError, ValueError, AttributeError):
        return []


def get(path, allowlist, *, store=None):
    store = store or InfisicalStore()
    current = store.read()
    raw, _ = editor.read_live(path)
    _validated_overlay(
        editor._decode(raw), allowlist, current, known=(getattr(store, "token", None),)
    )
    answer = _base_answer("get", current, path, known=(getattr(store, "token", None),))
    answer.update(ok=True, result="read", persisted=True, readback=True, autosaveEnabled=True)
    return answer


def _save_core(
    path,
    allowlist,
    requested,
    expected,
    *,
    remove=None,
    store=None,
    checker=None,
    reloader=None,
    intent=None,
    budget=None,
):
    """Persist canonically, reread exactly, then synchronize the runtime copy.

    A caller retains a failed response's canonical revision/settings when
    persisted/readback are true; it must not blindly replay a timed-out edit.
    """
    if not isinstance(expected, str) or not editor.SHA256_RE.fullmatch(expected):
        raise editor.Refusal("version_required", "save requires the previous canonical revision")
    if not isinstance(requested, dict) or not requested or len(requested) > editor.MAX_CHANGES:
        raise editor.Refusal("usage", "save requires a bounded non-empty settings patch")
    budget = budget or _Budget()
    store = store or InfisicalStore(writer=True)
    budget.phase(store)
    current = store.read()
    raw, _ = editor.read_live(path)
    known = (getattr(store, "token", None),)
    canonical_text = _validated_overlay(editor._decode(raw), allowlist, current, known=known)
    answer = _base_answer("save", current, path, known=known)
    if revision(current) != expected:
        answer.update(
            errorCode="version_conflict",
            detail="Settings changed; read the current values before saving",
        )
        return answer
    new_text, _ = editor.build_plan(canonical_text, allowlist, requested, remove=remove)
    new_settings = copy.deepcopy(current["settings"])
    new_settings.update(copy.deepcopy(requested))
    new_doc = document(new_settings)
    _scan_nonsecret(new_settings, known)
    intent.prepare(expected, revision(new_doc))
    with tempfile.TemporaryDirectory(prefix="operator-settings-check-") as scratch:
        candidate = Path(scratch) / "candidate.toml"
        candidate.write_text(new_text, encoding="utf-8")
        checked = budget.check(checker, candidate, path)
    if not checked.get("ok"):
        answer.update(
            errorCode="check_failed",
            check=checked,
            detail="The daemon refused the proposed settings",
        )
        return answer
    budget.phase(store)
    before_write = store.read()
    try:
        _validated_overlay(canonical_text, allowlist, before_write, known=known)
    except SettingsError:
        answer.update(
            errorCode="readback_invalid",
            detail="Canonical settings failed schema validation; no current values were returned",
        )
        return answer
    if revision(before_write) != expected:
        answer.update(
            errorCode="version_conflict",
            detail="Settings changed while the candidate was checked",
            revision=revision(before_write),
            baseSHA256=revision(before_write),
            settings=copy.deepcopy(before_write["settings"]),
        )
        return answer
    if revision(new_doc) != revision(current):
        answer["writeAttempted"] = True
        intent.mark("sending")
        try:
            store.write(new_doc)
        except MutationError as exc:
            answer.update(
                writeOutcome="unknown" if exc.sent else "refused",
                requestMayHaveBeenSent=exc.sent,
                retryable=exc.retryable,
            )
            if exc.status is not None:
                answer["providerStatus"] = exc.status
            if not exc.sent:
                intent.mark("refused")
                answer.update(errorCode=exc.code, detail="The provider refused the settings write")
                return answer
        except SettingsError:
            # A transport failure may occur after persistence.  Reread once,
            # without replaying the write, before classifying the outcome.
            answer["writeOutcome"] = "unconfirmed"
    try:
        verified = store.read()
    except SettingsError:
        answer.update(
            errorCode="readback_failed", detail="The provider write has no verified readback"
        )
        return answer
    if revision(verified) != revision(new_doc):
        try:
            _validated_overlay(new_text, allowlist, verified, known=known)
        except SettingsError:
            answer.update(
                errorCode="readback_invalid",
                detail="Canonical readback failed schema validation; no current values were returned",
            )
            return answer
        # No cache patch, rollback write, or retry can turn a different
        # authoritative document into proof of this request.
        answer.update(
            errorCode="readback_conflict",
            detail="Canonical settings changed during the save",
            revision=revision(verified),
            baseSHA256=revision(verified),
            settings=copy.deepcopy(verified["settings"]),
        )
        return answer
    answer.update(
        persisted=True,
        readback=True,
        revision=revision(verified),
        baseSHA256=revision(verified),
        settings=copy.deepcopy(verified["settings"]),
    )
    intent.mark("persisted")

    def canonical_reload(live_path):
        return budget.reload(reloader, live_path, revision(verified))

    try:
        # Only this verified Infisical-first route enables the low-level
        # copy writer; the old TOML-only apply gate remains unchanged.
        copy_allowlist = dict(allowlist, applyEnabled=True)
        copied, _ = editor.read_live(path)
        runtime = editor.apply(
            path,
            copy_allowlist,
            verified["settings"],
            checker=lambda candidate, live: budget.check(checker, candidate, live),
            remove=_canonical_removals(editor._decode(copied), verified["settings"]),
            reloader=canonical_reload,
            reload="always",
            _lock_held=True,
            _max_changes=512,
        )
        after, _ = editor.read_live(path)
        wanted = overlay_text(editor._decode(after), allowlist, verified["settings"])
        file_matches = wanted.encode("utf-8") == after
        reload_result = runtime.get("reload") or {}
        if runtime.get("result") == "unchanged":
            reload_result = canonical_reload(path)
        answer.update(runtimeSHA256=editor.sha256(after), reload=reload_result)
        if (
            not file_matches
            or not reload_result.get("ok")
            or not reload_result.get("confirmed")
            or reload_result.get("revision") != revision(verified)
        ):
            raise SettingsError("runtime_pending")
        answer.update(ok=True, result="saved", runtimeApplied=True, detail="Saved and verified")
    except (editor.Refusal, editor.StepFailed, OSError, SettingsError):
        answer.update(
            errorCode="runtime_pending",
            detail="Saved to Infisical; runtime acceptance remains pending",
        )
    return answer
