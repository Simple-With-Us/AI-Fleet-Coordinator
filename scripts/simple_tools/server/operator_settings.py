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
from pathlib import Path
from urllib.parse import quote

from . import eavesdropper_settings_edit as editor

KEY = "OPERATOR_SETTINGS"
FOLDER = "/operator-settings"
READ_BINDING = "OPERATOR_SETTINGS_INFISICAL_READ_TOKEN"
WRITE_BINDING = "OPERATOR_SETTINGS_INFISICAL_WRITE_TOKEN"
PROJECT = "INFISICAL_OPERATOR_SETTINGS_PROJECT_ID"
MAX_DOCUMENT_BYTES = 65536
SCHEMA_VERSION = 1
VERIFIED_COPY_NAME = ".operator-settings.verified.json"
MAX_COPY_AGE_SECONDS = 48 * 3600


class SettingsError(Exception):
    """A fixed reason only; provider response bodies and credentials never escape."""

    def __init__(self, code, *, required_binding=None):
        super().__init__(code)
        self.code = code
        self.required_binding = (
            required_binding if required_binding in (READ_BINDING, WRITE_BINDING, PROJECT) else None
        )


def encode(document):
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


def _validated_overlay(text, allowlist, current):
    """Provider values must pass the nonsecret schema before any response map."""
    try:
        return overlay_text(text, allowlist, current["settings"])
    except editor.Refusal:
        raise SettingsError("canonical_document_invalid") from None


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
            overlay_text(text, allowlist, current["settings"]), "canonical settings"
        )
        from .. import config

        if config.from_dict(candidate).errors:
            raise SettingsError("canonical_config_invalid")
    except (SettingsError, editor.Refusal):
        current = _verified_copy(path, env)
        candidate = editor._parse(
            overlay_text(text, allowlist, current["settings"]), "verified settings copy"
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
        self.deadline = time.monotonic() + max(0.1, min(float(timeout), 30.0))

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
            body = {
                "workspaceId": self.project,
                "environment": "prod",
                "secretPath": FOLDER,
                "type": "shared",
                "secretValue": raw,
            }
            path = "/v3/secrets/raw/" + quote(KEY, safe="")
            self._remaining()
            status, _ = self.client._api_json(
                method,
                path,
                self.token,
                body,
                self.env,
                deadline=self.deadline,
            )
        except (self.client.SecretsError, TimeoutError):
            raise SettingsError("provider_write_failed") from None
        if status not in (200, 201):
            # Each method has one purpose: PATCH an existing document or POST
            # one absent document, with no upsert or alternate-grant fallback.
            raise SettingsError("provider_write_failed")


def _base_answer(action, current, path):
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
        "channelNames": channel_names(),
        "channelNamesSource": "daemon-Zulip-cache",
    }


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


def initialize(path, allowlist, expected, *, store=None, checker=None, accepted=None):
    """Explicit one-time migration, never an autosave or an overwrite/upsert."""
    if not isinstance(expected, str) or not editor.SHA256_RE.fullmatch(expected):
        raise editor.Refusal(
            "version_required", "initialize requires the current runtime-file revision"
        )
    store = store or InfisicalStore(writer=True)
    checker = checker or editor.daemon_check
    accepted = accepted or _accepted_by_daemon
    with editor._FolderLock(path.parent):
        raw, _ = editor.read_live(path)
        if editor.sha256(raw) != expected:
            raise editor.Refusal(
                "version_conflict", "the runtime file changed before initialization"
            )
        initial = accepted_snapshot(editor._parse(editor._decode(raw), "runtime file"), allowlist)
        answer = _base_answer("initialize", initial, path)
        if not accepted(revision(initial)):
            answer.update(
                errorCode="runtime_not_accepted",
                detail="The active daemon has not accepted this settings snapshot",
            )
            return answer
        with tempfile.TemporaryDirectory(prefix="operator-settings-init-") as scratch:
            candidate = Path(scratch) / "candidate.toml"
            candidate.write_bytes(raw)
            checked = checker(candidate, path)
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
        existing = store.read(missing_ok=True)
        if existing is not None:
            answer.update(
                errorCode="already_initialized",
                detail="Canonical settings already exist; no create was attempted",
            )
            return answer
        answer["writeAttempted"] = True
        try:
            # POST is create-only.  Never fall back to PATCH after a concurrent
            # initializer or an ambiguous response.  Provider create rejects an
            # existing shared key; the exact readback is a separate receipt.
            store.create(initial)
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


def channel_names():
    """Actual names already observed by the daemon, with no new credential read."""
    try:
        with open(editor._status_path(), "rb") as handle:
            raw = handle.read(4 * 1024 * 1024 + 1)
        if len(raw) > 4 * 1024 * 1024:
            return []
        value = json.loads(raw).get("channel_names")
        if not isinstance(value, list) or len(value) > 500:
            return []
        return sorted({name for name in value if isinstance(name, str) and 0 < len(name) <= 60})
    except (OSError, ValueError, AttributeError):
        return []


def get(path, allowlist, *, store=None):
    store = store or InfisicalStore()
    current = store.read()
    raw, _ = editor.read_live(path)
    _validated_overlay(editor._decode(raw), allowlist, current)
    answer = _base_answer("get", current, path)
    answer.update(ok=True, result="read", persisted=True, readback=True, autosaveEnabled=True)
    return answer


def save(
    path, allowlist, requested, expected, *, remove=None, store=None, checker=None, reloader=None
):
    """Persist canonically, reread exactly, then synchronize the runtime copy.

    A caller retains a failed response's canonical revision/settings when
    persisted/readback are true; it must not blindly replay a timed-out edit.
    """
    if not isinstance(expected, str) or not editor.SHA256_RE.fullmatch(expected):
        raise editor.Refusal("version_required", "save requires the previous canonical revision")
    if not isinstance(requested, dict) or not requested or len(requested) > editor.MAX_CHANGES:
        raise editor.Refusal("usage", "save requires a bounded non-empty settings patch")
    store = store or InfisicalStore(writer=True)
    checker = checker or editor.daemon_check
    with editor._FolderLock(path.parent):
        current = store.read()
        raw, _ = editor.read_live(path)
        canonical_text = _validated_overlay(editor._decode(raw), allowlist, current)
        answer = _base_answer("save", current, path)
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
        with tempfile.TemporaryDirectory(prefix="operator-settings-check-") as scratch:
            candidate = Path(scratch) / "candidate.toml"
            candidate.write_text(new_text, encoding="utf-8")
            checked = checker(candidate, path)
        if not checked.get("ok"):
            answer.update(
                errorCode="check_failed",
                check=checked,
                detail="The daemon refused the proposed settings",
            )
            return answer
        before_write = store.read()
        try:
            _validated_overlay(canonical_text, allowlist, before_write)
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
            try:
                store.write(new_doc)
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
                _validated_overlay(new_text, allowlist, verified)
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

        def canonical_reload(live_path):
            if reloader is not None:
                return reloader(live_path)
            return editor.daemon_reload(live_path, expected_revision=revision(verified))

        try:
            # Only this verified Infisical-first route enables the low-level
            # copy writer; the old TOML-only apply gate remains unchanged.
            copy_allowlist = dict(allowlist, applyEnabled=True)
            copied, _ = editor.read_live(path)
            runtime = editor.apply(
                path,
                copy_allowlist,
                verified["settings"],
                checker=checker,
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
