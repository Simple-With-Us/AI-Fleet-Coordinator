"""Canonical autosave fences, actual readback and partial runtime outcomes."""

from __future__ import annotations

import copy
import io
import json
import os
import secrets
import stat
import sys
import tempfile
import threading
import time
import types
import unittest
import uuid
from pathlib import Path
from unittest import mock
from concurrent.futures import ThreadPoolExecutor

from simple_tools.server import eavesdropper_settings_edit as editor
from simple_tools.server import operator_settings as settings
from simple_tools.tests.test_server import ServerHarness


class Store:
    def __init__(self, document):
        self.document = copy.deepcopy(document)
        self.writes = []
        self.after_write = None
        self.write_error = False
        self.read_error_after_write = False

    def read(self, *, missing_ok=False):
        if self.writes and self.read_error_after_write:
            raise settings.SettingsError("provider_read_failed")
        if self.document is None and not missing_ok:
            raise settings.SettingsError("settings_not_initialized")
        return copy.deepcopy(self.document)

    def write(self, document):
        self.writes.append(copy.deepcopy(document))
        self.document = copy.deepcopy(self.after_write or document)
        if self.write_error:
            raise settings.SettingsError("provider_write_failed")

    def create(self, document):
        if self.document is not None:
            raise settings.SettingsError("provider_write_failed")
        self.write(document)


class AutosaveTests(unittest.TestCase):
    def identity(self):
        return {"requestID": str(uuid.uuid4()), "requestCreatedAt": int(time.time() * 1000)}

    def identity_args(self):
        value = self.identity()
        return {"request_id": value["requestID"], "created_at": value["requestCreatedAt"]}

    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.path = Path(self.scratch.name) / "listener.toml"
        self.original = (
            '# unrelated comment survives\n[daemon]\ninstance = "server"\n'
            'owner_user_id = 12\ncoalesce_seconds = 1.0\n'
            'presence_topics = [["Work-In-Progress", "A Topic"]]\n'
            '[seat.GROK]\ninstance = "server"\ncreds = "env"\nwake = "inbox"\n'
        )
        self.path.write_text(self.original)
        self.status_path = self.path.with_name("status.json")
        status = mock.patch.object(editor, "_status_path", return_value=str(self.status_path))
        status.start()
        self.addCleanup(status.stop)
        source = Path(__file__).resolve().parents[2] / "fleet-ops-eavesdropper-settings.json"
        self.allowlist = editor.validate_allowlist(json.loads(source.read_text()))
        self.document = settings.document({
            "daemon.coalesce_seconds": 1.0,
            "daemon.presence_topics": [["Work-In-Progress", "A Topic"]],
            "seat.GROK.wake": "inbox",
        })
        self.store = Store(self.document)

    def canonical_wire(self, request=None, *, reloader=None, reader_enabled=True):
        """Real helper/editor JSON envelopes; only external effects are faked."""
        from simple_tools.tests.test_eavesdropper_settings_edit import load_control
        control = load_control()
        home = self.path.parent / "helper-home"
        home.mkdir(mode=0o700, exist_ok=True)

        def reader(_argv, *, data, **_kwargs):
            proposed = json.loads(data)
            answer, code = editor.run_request(
                proposed, path=self.path, checker=lambda *_: {"ok": True},
                reloader=reloader or (lambda _: {
                    "ok": True, "confirmed": True, "code": "reloaded", "revision": settings.revision(self.store.document),
                }),
            )
            return code, json.dumps(answer).encode(), b"", False

        if request is not None and request.get("action") in ("save", "initialize", "reconcile"):
            request = dict(self.identity(), **request)
        stdin = b"" if request is None else json.dumps(request).encode()
        ctx = control.Context(
            platform="linux", uid=os.getuid(), home=home, environ={"PATH": "/usr/bin:/bin"},
            reader=reader, stdin=lambda: stdin,
        )
        out = io.StringIO()
        env = {settings.READ_BINDING: "st." + secrets.token_urlsafe(32)} if reader_enabled else {}
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(settings, "InfisicalStore", return_value=self.store):
            code = control.main(
                ["server", "eavesdropper-settings", "server", "--plan" if request is None else "--yes", "--json"],
                ctx=ctx, out=out,
            )
        return json.loads(out.getvalue()), code

    def test_actual_wire_receipt_supports_two_consecutive_autosaves(self):
        baseline, code = self.canonical_wire()
        self.assertEqual(code, 0)
        baseline = baseline["eavesdropperSettings"]
        self.assertIs(baseline["autosaveEnabled"], True)
        for value in (2.0, 3.0):
            envelope, code = self.canonical_wire({
                "action": "save", "set": {"daemon.coalesce_seconds": value},
                "expectBaseSHA256": baseline["revision"],
            })
            self.assertEqual(code, 0)
            baseline = envelope["eavesdropperSettings"]
            self.assertIs(baseline["autosaveEnabled"], True)
            self.assertTrue(all(baseline[key] for key in ("persisted", "readback", "runtimeApplied")))
            self.assertEqual(baseline["settings"]["daemon.coalesce_seconds"], value)
        self.assertEqual(len(self.store.writes), 2)
        self.assertIn("coalesce_seconds = 3.0", self.path.read_text())

    def test_actual_partial_receipt_preserves_availability_without_claiming_saved(self):
        envelope, code = self.canonical_wire({
            "action": "save", "set": {"daemon.coalesce_seconds": 2.0},
            "expectBaseSHA256": settings.revision(self.document),
        }, reloader=lambda _: {"ok": True, "confirmed": False, "code": "reload_unconfirmed"})
        answer = envelope["eavesdropperSettings"]
        self.assertEqual(code, 1)
        self.assertIs(answer["autosaveEnabled"], True)
        self.assertTrue(answer["persisted"] and answer["readback"])
        self.assertFalse(answer["ok"] or answer["runtimeApplied"])
        self.assertEqual(answer["errorCode"], "runtime_pending")

    def test_actual_runtime_copy_get_keeps_autosave_disabled(self):
        envelope, code = self.canonical_wire(reader_enabled=False)
        answer = envelope["eavesdropperSettings"]
        self.assertEqual(code, 0)
        self.assertEqual(answer["source"], "runtime-copy")
        self.assertIs(answer["autosaveEnabled"], False)
        self.assertFalse(answer["persisted"] or answer["readback"] or answer["runtimeApplied"])

    def test_actual_initialization_receipt_does_not_enable_autosave(self):
        self.store = Store(None)
        snapshot = settings.accepted_snapshot(editor._parse(self.original, "fixture"), self.allowlist)
        self.status_path.write_text(json.dumps({
            "updated": time.time(), "instance": "server", "config_errors": [],
            "operator_settings_snapshot_revision": settings.revision(snapshot),
        }))
        envelope, code = self.canonical_wire({
            "action": "initialize", "expectBaseSHA256": editor.sha256(self.path.read_bytes()),
        }, reader_enabled=False)
        answer = envelope["eavesdropperSettings"]
        self.assertEqual(code, 0)
        self.assertEqual(answer["result"], "initialized")
        self.assertIs(answer["autosaveEnabled"], False)
        self.assertTrue(answer["persisted"] and answer["readback"])
        self.assertFalse(answer["runtimeApplied"])

    def save(self, patch=None, **kwargs):
        identity = self.identity_args()
        identity.update({key: kwargs.pop(key) for key in ("request_id", "created_at") if key in kwargs})
        return settings.save(
            self.path, self.allowlist, patch or {"daemon.coalesce_seconds": 2.0},
            settings.revision(self.document), store=self.store,
            checker=kwargs.pop("checker", lambda *_: {"ok": True}),
            reloader=kwargs.pop("reloader", lambda *_: {
                "ok": True, "confirmed": True, "code": "reloaded",
                "revision": settings.revision(self.store.document),
            }),
            **identity,
            **kwargs,
        )

    def test_success_requires_provider_reread_and_runtime_confirmation(self):
        answer = self.save()
        self.assertEqual((answer["ok"], answer["result"]), (True, "saved"))
        self.assertTrue(all(answer[key] for key in ("persisted", "readback", "runtimeApplied")))
        self.assertEqual(answer["revision"], settings.revision(self.store.document))
        self.assertEqual(answer["settings"]["daemon.coalesce_seconds"], 2.0)
        self.assertIn('# unrelated comment survives', self.path.read_text())
        self.assertIn('owner_user_id = 12', self.path.read_text())
        self.assertEqual(len(self.store.writes), 1)
        self.assertFalse(self.allowlist["applyEnabled"])

    def test_stale_revision_does_not_write_or_change_runtime(self):
        self.store.document["settings"]["daemon.coalesce_seconds"] = 3.0
        answer = self.save()
        self.assertEqual(answer["errorCode"], "version_conflict")
        self.assertEqual(self.store.writes, [])
        self.assertEqual(self.path.read_text(), self.original)

    def test_change_during_a_late_check_does_not_overwrite_provider(self):
        def check(*_):
            self.store.document["settings"]["daemon.coalesce_seconds"] = 3.0
            return {"ok": True}
        answer = self.save(checker=check)
        self.assertEqual(answer["errorCode"], "version_conflict")
        self.assertEqual(self.store.writes, [])

    def test_provider_readback_conflict_is_not_patched_into_saved(self):
        self.store.after_write = copy.deepcopy(self.document)
        self.store.after_write["settings"]["daemon.coalesce_seconds"] = 3.0
        answer = self.save()
        self.assertFalse(answer["ok"])
        self.assertFalse(answer["readback"])
        self.assertEqual(answer["errorCode"], "readback_conflict")
        self.assertEqual(self.path.read_text(), self.original)
        self.assertEqual(len(self.store.writes), 1)

    def invalid_provider_document(self):
        private = "gh" + "p_" + secrets.token_hex(20)
        value = copy.deepcopy(self.document)
        value["settings"]["daemon.presence_topics"] = [["Alerts", private]]
        return value, private

    def test_stale_revision_cannot_reflect_unvalidated_provider_values(self):
        invalid, private = self.invalid_provider_document()
        request = {
            "action": "save", "allowlist": dict(self.allowlist, schemaVersion=1),
            "set": {"daemon.coalesce_seconds": 2.0},
            "expectBaseSHA256": settings.revision(self.document),
            **self.identity(),
        }
        for current in (invalid, settings.document({private: 1})):
            with self.subTest(secret_is_key=private in current["settings"]):
                request.update(self.identity())
                self.store.document = current
                with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(settings, "InfisicalStore", return_value=self.store):
                    answer, code = editor.run_request(request, path=self.path)
                self.assertEqual(code, 1)
                self.assertEqual(answer["errorCode"], "canonical_document_invalid")
                self.assertNotIn(private, json.dumps(answer))
                self.assertNotIn("settings", answer)
                self.assertEqual(self.store.writes, [])
                self.assertEqual(self.path.read_text(), self.original)

    def test_late_invalid_document_returns_no_unvalidated_map_or_write(self):
        invalid, private = self.invalid_provider_document()
        def check(*_):
            self.store.document = invalid
            return {"ok": True}
        answer = self.save(checker=check)
        self.assertEqual(answer["errorCode"], "readback_invalid")
        self.assertNotIn(private, json.dumps(answer))
        self.assertEqual(answer["settings"], self.document["settings"])
        self.assertFalse(answer["persisted"] or answer["readback"] or answer["ok"])
        self.assertEqual(self.store.writes, [])
        self.assertEqual(self.path.read_text(), self.original)

    def test_invalid_write_readback_never_reaches_response_or_runtime(self):
        invalid, private = self.invalid_provider_document()
        self.store.after_write = invalid
        answer = self.save()
        self.assertEqual(answer["errorCode"], "readback_invalid")
        self.assertNotIn(private, json.dumps(answer))
        self.assertTrue(answer["writeAttempted"])
        self.assertFalse(answer["persisted"] or answer["readback"] or answer["ok"])
        self.assertEqual(len(self.store.writes), 1)
        self.assertEqual(self.path.read_text(), self.original)

    def test_missing_readback_never_reports_success_or_replays(self):
        self.store.read_error_after_write = True
        answer = self.save()
        self.assertEqual(answer["errorCode"], "readback_failed")
        self.assertFalse(answer["ok"])
        self.assertEqual(len(self.store.writes), 1)
        self.assertEqual(self.path.read_text(), self.original)

    def test_lost_write_response_can_only_succeed_from_exact_readback(self):
        self.store.write_error = True
        answer = self.save()
        self.assertTrue(answer["ok"])
        self.assertTrue(answer["readback"])
        self.assertEqual(len(self.store.writes), 1)

    def test_runtime_copy_failure_keeps_truthful_canonical_receipt(self):
        with mock.patch.object(editor, "apply", side_effect=OSError("fixture fault")):
            answer = self.save()
        self.assertFalse(answer["ok"])
        self.assertTrue(answer["persisted"])
        self.assertTrue(answer["readback"])
        self.assertFalse(answer["runtimeApplied"])
        self.assertIs(answer["autosaveEnabled"], True)
        self.assertEqual(answer["errorCode"], "runtime_pending")
        self.assertEqual(answer["revision"], settings.revision(self.store.document))
        self.assertEqual(self.path.read_text(), self.original)

    def test_unconfirmed_reload_is_not_saved(self):
        answer = self.save(reloader=lambda *_: {"ok": True, "confirmed": False, "code": "reload_unconfirmed"})
        self.assertFalse(answer["ok"])
        self.assertTrue(answer["persisted"])
        self.assertFalse(answer["runtimeApplied"])

    def test_a_different_runtime_revision_is_not_saved(self):
        answer = self.save(reloader=lambda *_: {
            "ok": True, "confirmed": True, "code": "reloaded", "revision": "0" * 64,
        })
        self.assertEqual(answer["errorCode"], "runtime_pending")
        self.assertFalse(answer["runtimeApplied"])

    def test_presence_pairs_preserve_real_channel_and_topic_case(self):
        pairs = [["Builds", "Release Notes"], ["Work-In-Progress", "A Topic"]]
        answer = self.save({"daemon.presence_topics": pairs})
        self.assertTrue(answer["ok"])
        self.assertEqual(answer["settings"]["daemon.presence_topics"], pairs)
        self.assertIn('"Release Notes"', self.path.read_text())

    def test_channel_choices_use_daemon_metadata_without_inventing_case(self):
        status = self.path.with_name("status.json")
        status.write_text(json.dumps({"channel_names": ["Work-In-Progress", "Builds", "Alerts"]}))
        with mock.patch.object(editor, "_status_path", return_value=str(status)):
            answer = settings.get(self.path, self.allowlist, store=self.store)
        self.assertEqual(answer["channelNames"], ["Alerts", "Builds", "Work-In-Progress"])
        self.assertEqual(answer["settings"], self.document["settings"])
        self.assertEqual(answer["revision"], settings.revision(self.document))

    def test_identity_pins_cannot_enter_canonical_document(self):
        with self.assertRaises(editor.Refusal):
            self.save({"daemon.owner_user_id": 44})
        self.assertEqual(self.store.writes, [])

    def test_routine_reference_names_cannot_borrow_another_seat_key(self):
        with self.assertRaises(editor.Refusal):
            self.save({"seat.GROK.routine": {
                "url_env": "GROK_ROUTINE_URL", "key_env": "CLAUDE_ROUTINE_KEY",
                "method": "POST", "auth": "bearer",
            }})
        self.assertEqual(self.store.writes, [])

    def test_manifest_registers_one_fixed_canonical_home(self):
        source = Path(__file__).resolve().parents[2] / "fleet-ops-eavesdropper-settings.json"
        self.assertEqual(json.loads(source.read_text())["canonicalHome"], editor.CANONICAL_HOME)
        self.assertEqual(editor.CANONICAL_HOME["key"], settings.KEY)
        self.assertEqual(editor.CANONICAL_HOME["folder"], settings.FOLDER)

    def test_canonical_wire_cannot_widen_the_packaged_schema(self):
        from simple_tools import config
        widened = copy.deepcopy(self.allowlist)
        widened["tables"]["daemon"]["coalesce_seconds"]["max"] = 5000
        request = {
            "action": "save", "allowlist": dict(widened, schemaVersion=1),
            **self.identity(),
            "set": {"daemon.coalesce_seconds": 4000.0},
            "expectBaseSHA256": settings.revision(self.document),
        }
        def checker(candidate, _):
            parsed = editor._parse(Path(candidate).read_text(), "fixture")
            return {"ok": not config.from_dict(parsed).errors}
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(settings, "InfisicalStore", return_value=self.store):
            answer, code = editor.run_request(
                request, path=self.path, checker=checker,
                reloader=lambda _: {"ok": True, "confirmed": True, "revision": settings.revision(self.store.document)},
            )
        self.assertEqual(code, 2)
        self.assertEqual(answer["errorCode"], "value_not_allowed")
        self.assertEqual(self.store.writes, [])
        self.assertEqual(self.path.read_text(), self.original)

    def runtime_env(self):
        return {
            settings.READ_BINDING: "st." + secrets.token_urlsafe(32),
            settings.PROJECT: "00000000-0000-4000-8000-000000000042",
            editor.ENV_CONFIG: str(self.path),
        }

    def test_runtime_load_uses_canonical_values_without_overwriting_structure(self):
        self.store.document["settings"]["daemon.coalesce_seconds"] = 7.0
        original = editor._parse(self.original, "fixture")
        raw, source = settings.load_runtime(original, self.path, environ=self.runtime_env(), store=self.store)
        self.assertEqual(source["source"], "infisical")
        self.assertEqual(raw["daemon"]["coalesce_seconds"], 7.0)
        self.assertEqual(raw["daemon"]["owner_user_id"], 12)
        self.assertEqual(self.path.read_text(), self.original)
        self.assertFalse(self.path.with_name(settings.VERIFIED_COPY_NAME).exists())

    def test_only_verified_runtime_reads_seed_a_last_known_good_copy(self):
        env = self.runtime_env()
        original = editor._parse(self.original, "fixture")
        settings.load_runtime(original, self.path, environ=env, store=self.store)
        settings.remember_verified(self.path, self.store.document, env)
        failing = mock.Mock()
        failing.read.side_effect = settings.SettingsError("provider_read_failed")
        raw, source = settings.load_runtime(original, self.path, environ=env, store=failing)
        self.assertEqual(source["source"], "verified-copy")
        self.assertEqual(raw["daemon"]["coalesce_seconds"], 1.0)
        cache = self.path.with_name(settings.VERIFIED_COPY_NAME)
        payload = json.loads(cache.read_text())
        payload["document"]["settings"]["daemon.coalesce_seconds"] = 99.0
        cache.write_text(json.dumps(payload))
        with self.assertRaises(settings.SettingsError):
            settings.load_runtime(original, self.path, environ=env, store=failing)

    def test_candidate_check_is_not_overlaid_with_old_canonical_settings(self):
        candidate = self.path.with_name("candidate.toml")
        candidate.write_text(self.original.replace("1.0", "7.0"))
        supplied = editor._parse(candidate.read_text(), "fixture")
        result, source = settings.load_runtime(supplied, candidate, environ=self.runtime_env(), store=self.store)
        self.assertEqual(result["daemon"]["coalesce_seconds"], 7.0)
        self.assertIsNone(source)

    def test_invalid_typed_fallback_is_a_refused_configuration_not_a_crash(self):
        from simple_tools import config
        env = self.runtime_env()
        invalid = settings.document({"daemon.coalesce_seconds": "invalid fixture"})
        settings.remember_verified(self.path, invalid, env)
        failing = mock.Mock()
        failing.read.side_effect = settings.SettingsError("provider_read_failed")
        with mock.patch.object(settings, "InfisicalStore", return_value=failing):
            result = config.load(str(self.path.parent), str(self.path), environ=env)
        self.assertTrue(result.operator_settings_error)
        self.assertIn("canonical Operator settings unavailable", result.errors[0])

    def test_verified_copy_from_another_project_is_not_reused(self):
        env = self.runtime_env()
        settings.remember_verified(self.path, self.document, env)
        env[settings.PROJECT] = "00000000-0000-4000-8000-000000000043"
        failing = mock.Mock()
        failing.read.side_effect = settings.SettingsError("provider_read_failed")
        with self.assertRaises(settings.SettingsError):
            settings.load_runtime(editor._parse(self.original, "fixture"), self.path, environ=env, store=failing)

    def test_wrong_home_schema_and_expired_copies_are_refused(self):
        env = self.runtime_env()
        for field, value in (("environment", "staging"), ("folder", "/operator"), ("key", "OTHER_SETTINGS"), ("schemaVersion", 2)):
            with self.subTest(field=field):
                settings.remember_verified(self.path, self.document, env, now=1000)
                cache = self.path.with_name(settings.VERIFIED_COPY_NAME)
                content = json.loads(cache.read_text())
                content[field] = value
                cache.write_text(json.dumps(content))
                with self.assertRaises(settings.SettingsError):
                    settings._verified_copy(self.path, env, now=1001)
        settings.remember_verified(self.path, self.document, env, now=1000)
        with self.assertRaises(settings.SettingsError) as caught:
            settings._verified_copy(self.path, env, now=1001 + settings.MAX_COPY_AGE_SECONDS)
        self.assertEqual(caught.exception.code, "verified_copy_stale")

    def test_linked_copy_or_symlinked_parent_cannot_supply_fallback(self):
        env = self.runtime_env()
        settings.remember_verified(self.path, self.document, env)
        cache = self.path.with_name(settings.VERIFIED_COPY_NAME)
        linked = self.path.with_name("linked.json")
        os.link(cache, linked)
        with self.assertRaises(settings.SettingsError):
            settings._verified_copy(self.path, env)
        linked.unlink()
        real_parent = self.path.parent
        link_parent = real_parent.parent / (real_parent.name + "-link")
        link_parent.symlink_to(real_parent, target_is_directory=True)
        self.addCleanup(link_parent.unlink)
        with self.assertRaises((settings.SettingsError, OSError)):
            settings._verified_copy(link_parent / self.path.name, env)

    def test_unsafe_parent_is_not_a_verified_copy_store(self):
        self.path.parent.chmod(0o777)
        try:
            with self.assertRaises(settings.SettingsError):
                settings.remember_verified(self.path, self.document, self.runtime_env())
        finally:
            self.path.parent.chmod(0o700)

    def test_initialize_validates_accepted_runtime_and_never_updates_existing(self):
        absent = Store(None)
        answer = settings.initialize(
            self.path, self.allowlist, editor.sha256(self.path.read_bytes()), store=absent,
            checker=lambda *_: {"ok": True}, accepted=lambda _: True, **self.identity_args(),
        )
        self.assertTrue(answer["ok"])
        self.assertEqual(answer["result"], "initialized")
        self.assertTrue(answer["persisted"] and answer["readback"])
        self.assertFalse(answer["runtimeApplied"])
        self.assertEqual(len(absent.writes), 1)
        again = settings.initialize(
            self.path, self.allowlist, editor.sha256(self.path.read_bytes()), store=absent,
            checker=lambda *_: {"ok": True}, accepted=lambda _: True, **self.identity_args(),
        )
        self.assertEqual(again["errorCode"], "already_initialized")
        self.assertEqual(len(absent.writes), 1)

    def test_initializer_will_not_overwrite_a_concurrent_creator(self):
        absent = Store(None)
        competing = settings.document({"daemon.coalesce_seconds": 99.0})
        def concurrent_create(_):
            absent.document = copy.deepcopy(competing)
            raise settings.SettingsError("provider_write_failed")
        absent.create = concurrent_create
        answer = settings.initialize(
            self.path, self.allowlist, editor.sha256(self.path.read_bytes()), store=absent,
            checker=lambda *_: {"ok": True}, accepted=lambda _: True, **self.identity_args(),
        )
        self.assertEqual(answer["errorCode"], "initialization_conflict")
        self.assertEqual(absent.document, competing)
        self.assertEqual(absent.writes, [])

    def test_initializer_requires_current_daemon_acceptance(self):
        absent = Store(None)
        answer = settings.initialize(
            self.path, self.allowlist, editor.sha256(self.path.read_bytes()), store=absent,
            checker=lambda *_: {"ok": True}, accepted=lambda _: False, **self.identity_args(),
        )
        self.assertEqual(answer["errorCode"], "runtime_not_accepted")
        self.assertEqual(absent.writes, [])

    def test_initializer_readback_failure_does_not_replay_create(self):
        absent = Store(None)
        absent.read_error_after_write = True
        answer = settings.initialize(
            self.path, self.allowlist, editor.sha256(self.path.read_bytes()), store=absent,
            checker=lambda *_: {"ok": True}, accepted=lambda _: True, **self.identity_args(),
        )
        self.assertEqual(answer["errorCode"], "readback_failed")
        self.assertTrue(answer["writeAttempted"])
        self.assertFalse(answer["readback"] or answer["ok"])
        self.assertEqual(len(absent.writes), 1)

    def test_canonical_reload_requires_exact_revision_in_new_status(self):
        state = self.path.parent / "state"
        (state / "listener").mkdir(parents=True)
        status = state / "listener" / "status.json"
        wanted = settings.revision(self.document)
        for actual in (None, "0" * 64, wanted):
            with self.subTest(actual=actual):
                status.write_text(json.dumps({
                    "reload_at": 1001.0, "reload_refused": [], "operator_settings_revision": actual,
                }))
                clock = iter(range(1000, 1200))
                with mock.patch.object(editor, "_cli", return_value=(0, "")), mock.patch.object(editor, "_status_path", return_value=str(status)):
                    receipt = editor.daemon_reload(
                        self.path, environ={editor.ENV_STATE_DIR: str(state)},
                        expected_revision=wanted, clock=lambda: next(clock), sleep=lambda _: None,
                    )
                self.assertEqual(receipt["confirmed"], actual == wanted)
                self.assertEqual(receipt["code"], "reloaded" if actual == wanted else "reload_revision_mismatch")

    def test_missing_bindings_remain_names_only_in_request_diagnostics(self):
        cases = (
            ({}, "save", settings.WRITE_BINDING),
            ({settings.READ_BINDING: "st." + secrets.token_urlsafe(32)}, "get", settings.PROJECT),
            ({settings.WRITE_BINDING: "st." + secrets.token_urlsafe(32)}, "save", settings.PROJECT),
        )
        for env, action, name in cases:
            with self.subTest(name=name), mock.patch.dict(os.environ, env, clear=True):
                request = {"action": action, "allowlist": dict(self.allowlist, schemaVersion=1)}
                if action == "save":
                    request.update(set={"daemon.coalesce_seconds": 2.0}, expectBaseSHA256=settings.revision(self.document))
                    request.update(self.identity())
                answer, code = editor.run_request(request, path=self.path)
                self.assertEqual(code, 1)
                self.assertEqual(answer["requiredBinding"], name)
                self.assertIn(name, answer["detail"])
                for value in env.values():
                    self.assertNotIn(value, json.dumps(answer))

    def test_duplicate_or_nonfinite_canonical_json_is_refused(self):
        for raw in (
            '{"schemaVersion":1,"schemaVersion":1,"settings":{"daemon.coalesce_seconds":1}}',
            '{"schemaVersion":1,"settings":{"daemon.coalesce_seconds":NaN}}',
        ):
            with self.subTest(raw=raw):
                with self.assertRaises(settings.SettingsError):
                    settings.decode(raw)

    def test_unsetting_an_optional_cap_requires_explicit_intent_and_preserves_siblings(self):
        self.path.write_text(self.original + 'budget = { wakes_per_day = 4, board_per_day = 0 }\n')
        self.document["settings"]["seat.GROK.budget"] = {"wakes_per_day": 4, "board_per_day": 0}
        self.store = Store(self.document)
        patch = {"seat.GROK.budget": {"board_per_day": 0}}
        with self.assertRaises(editor.Refusal) as caught:
            self.save(patch)
        self.assertEqual(caught.exception.code, "would_remove_keys")
        self.assertEqual(self.store.writes, [])
        answer = self.save(patch, remove=["seat.GROK.budget.wakes_per_day"])
        self.assertTrue(answer["ok"])
        self.assertEqual(answer["settings"]["seat.GROK.budget"], {"board_per_day": 0})
        self.assertNotIn("wakes_per_day", self.path.read_text())
        self.assertIn("board_per_day = 0", self.path.read_text())
        self.assertIn("owner_user_id = 12", self.path.read_text())

    def confirm(self, value=None, *, activated=True):
        value = value or self.store.document
        status = {"updated": time.time(), "instance": "server", "config_errors": [],
                  "operator_settings_snapshot_revision": settings.revision(value)}
        if activated:
            status["operator_settings_revision"] = settings.revision(value)
        self.status_path.write_text(json.dumps(status))

    def test_embedded_credentials_are_refused_before_checker_or_persistence(self):
        private = "gh" + "p_" + secrets.token_hex(20)
        checker = mock.Mock(return_value={"ok": True})
        with self.assertRaises(settings.SettingsError) as caught:
            self.save({"daemon.presence_topics": [["Alerts", "Use " + private]]}, checker=checker)
        self.assertEqual((caught.exception.code, caught.exception.field), ("secret_detected", "daemon.presence_topics"))
        self.assertNotIn(private, str(caught.exception))
        checker.assert_not_called()
        self.assertEqual(self.store.writes, [])
        self.assertEqual(self.path.read_text(), self.original)

    def test_only_the_own_loaded_binding_is_also_scanned_exactly(self):
        self.store.token = "st." + secrets.token_urlsafe(32)
        with self.assertRaises(settings.SettingsError) as caught:
            self.save({"daemon.presence_topics": [["Alerts", "Use " + self.store.token + " here"]]})
        self.assertEqual(caught.exception.code, "secret_detected")
        self.assertEqual(self.store.writes, [])

    def test_canonical_json_has_depth_and_node_limits_below_byte_limit(self):
        nested = []
        for _ in range(settings.MAX_DOCUMENT_DEPTH + 1):
            nested = [nested]
        for value, code in ((nested, "document_too_deep"),
                            ([0] * settings.MAX_DOCUMENT_NODES, "document_too_complex")):
            raw = json.dumps({"schemaVersion": 1, "settings": {"daemon.presence_topics": value}})
            self.assertLess(len(raw), settings.MAX_DOCUMENT_BYTES)
            with self.assertRaises(settings.SettingsError) as caught:
                settings.decode(raw)
            self.assertEqual(caught.exception.code, code)
        cyclic = []
        cyclic.append(cyclic)
        with self.assertRaises(settings.SettingsError):
            settings.encode(cyclic)

    def test_duplicate_success_only_reads_and_preserves_known_success_when_superseded(self):
        identity = self.identity_args()
        first = self.save(**identity)
        self.confirm()
        second = self.save(**identity)
        self.assertTrue(second["ok"])
        self.assertEqual((second["requestID"], second["outcome"]), (first["requestID"], "ok"))
        self.assertEqual(len(self.store.writes), 1)
        self.store.document["settings"]["daemon.coalesce_seconds"] = 3.0
        self.path.write_text(settings.overlay_text(self.original, self.allowlist, self.store.document["settings"]))
        self.confirm()
        changed = self.save(**identity)
        self.assertEqual((changed["errorCode"], changed["outcome"]), ("request_superseded", "ok"))
        self.assertFalse(changed["ok"])
        self.assertEqual(len(self.store.writes), 1)
        ledger = settings._ledger(self.path)
        self.assertEqual(ledger.lookup(settings.REQUEST_NAMESPACE, identity["request_id"])[0], "ok")
        with mock.patch.object(self.store, "read", side_effect=settings.SettingsError("provider_read_failed")):
            failed_read = self.save(**identity)
        self.assertEqual(failed_read["outcome"], "ok")
        self.assertFalse(failed_read["ok"])
        self.assertEqual(ledger.lookup(settings.REQUEST_NAMESPACE, identity["request_id"])[0], "ok")

    def test_same_id_with_changed_fingerprint_cannot_write_or_read_credentials(self):
        identity = self.identity_args()
        self.save(**identity)
        reader = mock.Mock(side_effect=AssertionError("collision must stop before provider access"))
        with mock.patch.object(self.store, "read", reader), self.assertRaises(settings.SettingsError) as caught:
            self.save({"daemon.coalesce_seconds": 3.0}, **identity)
        self.assertEqual(caught.exception.code, "request_id_conflict")
        reader.assert_not_called()
        self.assertEqual(len(self.store.writes), 1)

    def test_unknown_persistence_never_replays_and_requires_accepted_target_to_reconcile(self):
        identity = self.identity_args()
        first = self.save(**identity, reloader=lambda _: {"ok": True, "confirmed": False})
        self.assertEqual((first["errorCode"], first["outcome"]), ("runtime_pending", "unknown"))
        second = settings.reconcile(self.path, self.allowlist, identity["request_id"], identity["created_at"], store=self.store)
        self.assertEqual(second["outcome"], "unknown")
        self.assertFalse(second["ok"])
        self.confirm()
        final = settings.reconcile(self.path, self.allowlist, identity["request_id"], identity["created_at"], store=self.store)
        self.assertTrue(final["ok"] and final["runtimeApplied"])
        self.assertEqual((final["action"], final["operation"], final["result"], final["outcome"]), ("reconcile", "save", "saved", "ok"))
        self.assertEqual(len(self.store.writes), 1)

    def test_old_base_value_does_not_refuse_an_unknown_late_write(self):
        identity = self.identity_args()
        self.store.after_write = self.document
        first = self.save(**identity)
        self.assertEqual(first["outcome"], "unknown")
        self.confirm(self.document)
        result = settings.reconcile(self.path, self.allowlist, identity["request_id"], identity["created_at"], store=self.store)
        self.assertEqual((result["outcome"], result["errorCode"]), ("unknown", "outcome_unknown"))
        self.assertEqual(len(self.store.writes), 1)

    def test_absent_reconcile_durably_cancels_a_late_original_without_provider_access(self):
        identity = self.identity_args()
        store = mock.Mock(side_effect=AssertionError("no provider is needed for a cancellation"))
        result = settings.reconcile(self.path, self.allowlist, identity["request_id"], identity["created_at"], store=store)
        self.assertEqual((result["outcome"], result["errorCode"]), ("refused", "request_cancelled"))
        self.assertIs(result["cancelled"], True)
        late = self.save(**identity)
        self.assertEqual((late["outcome"], late["errorCode"]), ("refused", "request_cancelled"))
        store.read.assert_not_called()
        self.assertEqual(self.store.writes, [])

    def test_expired_write_identity_cannot_rearm_after_terminal_eviction(self):
        identity = self.identity_args()
        self.save(**identity)
        future = time.time() + settings.REQUEST_TTL_SECONDS + 2
        with self.assertRaises(settings.SettingsError) as caught:
            self.save(**identity, now=future)
        self.assertEqual(caught.exception.code, "request_expired")
        self.assertEqual(len(self.store.writes), 1)

    def test_fresh_id_allows_an_intentional_aba_toggle(self):
        for value in (2.0, 1.0, 2.0):
            before = settings.revision(self.store.document)
            answer = settings.save(self.path, self.allowlist, {"daemon.coalesce_seconds": value}, before,
                                   store=self.store, checker=lambda *_: {"ok": True},
                                   reloader=lambda _: {"ok": True, "confirmed": True, "revision": settings.revision(self.store.document)},
                                   **self.identity_args())
            self.assertTrue(answer["ok"])
        self.assertEqual(len(self.store.writes), 3)

    def test_corrupt_cached_metadata_never_reflects_or_rearms(self):
        identity = self.identity_args()
        self.save(**identity)
        private = "private corruption fixture"
        settings._ledger(self.path).settle(settings.REQUEST_NAMESPACE, identity["request_id"], "ok", json.dumps({"bad": private}))
        with self.assertRaises(settings.SettingsError) as caught:
            self.save(**identity)
        self.assertEqual(caught.exception.code, "request_record_invalid")
        self.assertNotIn(private, str(caught.exception))
        self.assertEqual(len(self.store.writes), 1)

    def test_initialization_reconciles_without_activating_the_reader(self):
        identity = self.identity_args()
        absent = Store(None)
        absent.read_error_after_write = True
        answer = settings.initialize(self.path, self.allowlist, editor.sha256(self.path.read_bytes()),
                                     store=absent, checker=lambda *_: {"ok": True}, accepted=lambda _: True,
                                     **identity)
        self.assertEqual(answer["outcome"], "unknown")
        absent.read_error_after_write = False
        self.confirm(absent.document, activated=False)
        result = settings.reconcile(self.path, self.allowlist, identity["request_id"], identity["created_at"], store=absent)
        self.assertEqual((result["result"], result["operation"], result["outcome"]), ("initialized", "initialize", "ok"))
        self.assertTrue(result["persisted"] and result["readback"])
        self.assertFalse(result["runtimeApplied"] or result["autosaveEnabled"])
        self.assertEqual(len(absent.writes), 1)

    def abandon(self, phase):
        identity = self.identity_args()
        base = settings.revision(self.document)
        target = copy.deepcopy(self.document)
        target["settings"]["daemon.coalesce_seconds"] = 2.0
        with editor._FolderLock(self.path.parent):
            intent = settings._Intent(self.path, "save", identity["request_id"], identity["created_at"],
                                      {"set": {"daemon.coalesce_seconds": 2.0}, "remove": [], "base": base})
            intent.begin(base, "0" * 64)
            intent.prepare(base, settings.revision(target))
            if phase == "sending":
                intent.mark("sending")
        return identity, target

    def test_abandoned_prepared_cancels_without_a_provider_read_and_fences_late_original(self):
        identity, _ = self.abandon("prepared")
        with mock.patch.object(self.store, "read", side_effect=AssertionError("Prepared never entered mutation")):
            cancelled = settings.reconcile(self.path, self.allowlist, identity["request_id"], identity["created_at"], store=self.store)
            late = self.save(**identity)
        self.assertEqual((cancelled["outcome"], cancelled["errorCode"]), ("refused", "request_cancelled"))
        self.assertEqual(late["errorCode"], "request_cancelled")
        self.assertTrue(self.save()["ok"])
        self.assertEqual(len(self.store.writes), 1)

    def test_abandoned_sending_blocks_other_ids_and_noops_until_exact_reconciliation(self):
        identity, target = self.abandon("sending")
        for patch in ({"daemon.coalesce_seconds": 3.0}, {"daemon.coalesce_seconds": 1.0}):
            blocked = self.save(patch)
            self.assertEqual((blocked["outcome"], blocked["errorCode"]), ("refused", "prior_outcome_unknown"))
        self.assertEqual(self.store.writes, [])
        unknown = settings.reconcile(self.path, self.allowlist, identity["request_id"], identity["created_at"], store=self.store)
        self.assertEqual(unknown["outcome"], "unknown")
        self.store.document = target
        self.path.write_text(settings.overlay_text(self.original, self.allowlist, target["settings"]))
        self.confirm()
        settled = settings.reconcile(self.path, self.allowlist, identity["request_id"], identity["created_at"], store=self.store)
        self.assertEqual(settled["outcome"], "ok")
        self.document = copy.deepcopy(target)
        self.assertTrue(self.save({"daemon.coalesce_seconds": 3.0})["ok"])
        self.assertEqual(len(self.store.writes), 1)

    def test_actual_mutation_entry_observes_durable_sending_checkpoint(self):
        identity = self.identity_args()
        original = self.store.write
        def crash(document):
            state, raw = settings._ledger(self.path).lookup(settings.REQUEST_NAMESPACE, identity["request_id"])
            self.assertEqual(state, "pending")
            self.assertEqual(json.loads(raw)["phase"], "sending")
            original(document)
            raise SystemExit("synthetic abrupt process exit after persistence")
        with mock.patch.object(self.store, "write", side_effect=crash), self.assertRaises(SystemExit):
            self.save(**identity)
        blocked = self.save({"daemon.coalesce_seconds": 3.0})
        self.assertEqual(blocked["errorCode"], "prior_outcome_unknown")
        unknown = settings.reconcile(self.path, self.allowlist, identity["request_id"], identity["created_at"], store=self.store)
        self.assertEqual(unknown["outcome"], "unknown")
        self.assertEqual(len(self.store.writes), 1)

    def test_reconciliation_cannot_cancel_an_active_lock_owner(self):
        identity = self.identity_args()
        with editor._FolderLock(self.path.parent), self.assertRaises(editor.Refusal):
            settings.reconcile(self.path, self.allowlist, identity["request_id"], identity["created_at"], store=self.store)
        self.assertIsNone(settings._ledger(self.path).lookup(settings.REQUEST_NAMESPACE, identity["request_id"]))

    def test_new_journal_directory_must_be_durable_before_provider_access(self):
        parent = self.path.parent.stat()
        original = os.fsync
        def fail_parent(descriptor):
            info = os.fstat(descriptor)
            if stat.S_ISDIR(info.st_mode) and (info.st_dev, info.st_ino) == (parent.st_dev, parent.st_ino):
                raise OSError("synthetic parent fsync failure")
            return original(descriptor)
        reader = mock.Mock(side_effect=AssertionError("no provider access before durable state"))
        with mock.patch.object(os, "fsync", side_effect=fail_parent), mock.patch.object(self.store, "read", reader):
            with self.assertRaises(settings.SettingsError) as caught:
                self.save()
        self.assertEqual(caught.exception.code, "request_storage_unavailable")
        reader.assert_not_called()
        self.assertEqual(self.store.writes, [])

    def test_two_callers_cannot_validate_or_write_under_the_same_folder_lock(self):
        entered, release = threading.Event(), threading.Event()
        def checker(*_):
            if not entered.is_set():
                entered.set()
                self.assertTrue(release.wait(5), "test did not release the first caller")
            return {"ok": True}
        with ThreadPoolExecutor(max_workers=1) as workers:
            first = workers.submit(self.save, checker=checker)
            try:
                self.assertTrue(entered.wait(5), "first caller did not enter validation")
                with self.assertRaises(editor.Refusal) as caught:
                    self.save({"daemon.coalesce_seconds": 3.0})
                self.assertEqual(caught.exception.code, "busy")
                self.assertEqual(self.store.writes, [])
            finally:
                release.set()
            self.assertTrue(first.result(timeout=5)["ok"])
        self.assertEqual(len(self.store.writes), 1)


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.core = types.SimpleNamespace(
            SecretsError=type("SecretsError", (Exception,), {}),
            fetch_project=mock.Mock(return_value={settings.KEY: settings.encode(settings.document({"daemon.coalesce_seconds": 1.0}))}),
            verify_service_token=mock.Mock(),
            _api_json=mock.Mock(return_value=(200, {})),
        )
        self.patch = mock.patch.dict(sys.modules, {"fleet_infisical": self.core})
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.env = {
            settings.PROJECT: "00000000-0000-4000-8000-000000000042",
            settings.WRITE_BINDING: "st." + secrets.token_urlsafe(32),
            settings.READ_BINDING: "st." + secrets.token_urlsafe(32),
            "INFISICAL_TOKEN": "st." + secrets.token_urlsafe(32),
            "INFISICAL_READ_PATHS": "/unrelated,/operator-settings",
        }

    def test_writer_reads_only_named_document_through_its_own_exact_grant(self):
        store = settings.InfisicalStore(self.env, writer=True)
        store.read()
        args, kwargs = self.core.fetch_project.call_args
        self.assertEqual(args[:2], (self.env[settings.PROJECT], "/operator-settings"))
        self.assertEqual(args[2]["INFISICAL_READ_PATHS"], "/operator-settings")
        self.assertEqual(kwargs["token"], self.env[settings.WRITE_BINDING])
        self.assertTrue(kwargs["writer"])
        self.assertEqual(kwargs["names"], (settings.KEY,))

    def test_read_or_ambient_binding_never_substitutes_for_writer(self):
        self.env.pop(settings.WRITE_BINDING)
        with self.assertRaises(settings.SettingsError) as caught:
            settings.InfisicalStore(self.env, writer=True)
        self.assertEqual(caught.exception.code, "writer_not_configured")
        self.assertEqual(caught.exception.required_binding, settings.WRITE_BINDING)
        self.core.fetch_project.assert_not_called()
        self.core._api_json.assert_not_called()

    def test_write_uses_shared_transport_after_writer_verification(self):
        store = settings.InfisicalStore(self.env, writer=True)
        value = settings.document({"daemon.coalesce_seconds": 1.0})
        store.write(value)
        self.core.verify_service_token.assert_called_once()
        args, kwargs = self.core._api_json.call_args
        self.assertEqual(args[:3], ("PATCH", "/v3/secrets/raw/OPERATOR_SETTINGS", self.env[settings.WRITE_BINDING]))
        self.assertEqual(args[3]["secretPath"], "/operator-settings")
        self.assertEqual(args[3]["environment"], "prod")
        self.assertEqual(args[3]["secretValue"], settings.encode(value))
        self.assertIsNotNone(kwargs["deadline"])

    def test_deleted_document_is_not_recreated_and_provider_error_is_not_reflected(self):
        self.core._api_json.return_value = (404, {"message": "private provider fixture"})
        store = settings.InfisicalStore(self.env, writer=True)
        with self.assertRaises(settings.SettingsError) as caught:
            store.write(settings.document({"daemon.coalesce_seconds": 1.0}))
        self.assertEqual(str(caught.exception), "provider_write_refused")
        self.assertEqual(caught.exception.status, 404)
        self.assertFalse(caught.exception.sent or caught.exception.retryable)
        self.assertEqual(self.core._api_json.call_count, 1)

    def test_initializer_transport_is_create_only_and_never_falls_back_to_patch(self):
        self.core._api_json.return_value = (400, {"message": "already exists fixture"})
        store = settings.InfisicalStore(self.env, writer=True)
        with self.assertRaises(settings.SettingsError):
            store.create(settings.document({"daemon.coalesce_seconds": 1.0}))
        self.assertEqual(self.core._api_json.call_count, 1)
        self.assertEqual(self.core._api_json.call_args.args[0], "POST")

    def test_mutation_statuses_distinguish_refusal_from_possible_persistence(self):
        private = "private provider diagnostic fixture"
        for status, unknown, retryable in ((401, False, False), (403, False, False), (409, False, False),
                                           (408, True, True), (429, True, True), (503, True, True), (0, True, True)):
            with self.subTest(status=status):
                self.core._api_json.return_value = (status, {"message": private})
                store = settings.InfisicalStore(self.env, writer=True)
                with self.assertRaises(settings.MutationError) as caught:
                    store.write(settings.document({"daemon.coalesce_seconds": 1.0}))
                error = caught.exception
                self.assertEqual((error.sent, error.retryable), (unknown, retryable))
                self.assertEqual(error.status, status or None)
                self.assertNotIn(private, str(error))

    def test_failed_writer_verification_proves_mutation_was_not_sent(self):
        self.core.verify_service_token.side_effect = self.core.SecretsError("private validation fixture")
        store = settings.InfisicalStore(self.env, writer=True)
        with self.assertRaises(settings.MutationError) as caught:
            store.write(settings.document({"daemon.coalesce_seconds": 1.0}))
        self.assertFalse(caught.exception.sent)
        self.core._api_json.assert_not_called()

    def test_slow_local_check_gets_fresh_io_phase_under_one_total_deadline(self):
        clock = [100.0]
        with mock.patch.object(settings.time, "monotonic", side_effect=lambda: clock[0]):
            store = settings.InfisicalStore(self.env, writer=True)
            budget = settings._Budget()
            budget.phase(store)
            store.read()
            budget.check(lambda *_: clock.__setitem__(0, 145.0) or {"ok": True}, None, None)
            budget.phase(store)
            store.read()
            self.assertEqual(self.core.fetch_project.call_args.kwargs["timeout"], 20.0)
            self.assertLessEqual(store.deadline, 100 + settings.MAX_TRANSACTION_SECONDS)
            clock[0] = 249.0
            with mock.patch.object(editor, "daemon_check", return_value={"ok": True}) as checker:
                budget.check(None, None, None)
                self.assertEqual(checker.call_args.kwargs["timeout"], 1.0)
            clock[0] = 251.0
            with self.assertRaises(settings.SettingsError) as caught:
                budget.phase(store)
            self.assertEqual(caught.exception.code, "transaction_timeout")


class SharedClientIntegrationTests(unittest.TestCase):
    def test_real_shared_verifier_and_named_reader_enforce_the_settings_writer_grant(self):
        import fleet_infisical
        project = "00000000-0000-4000-8000-000000000042"
        own_binding = "st." + secrets.token_urlsafe(32)
        env = {settings.PROJECT: project, settings.WRITE_BINDING: own_binding}
        value = settings.document({"daemon.coalesce_seconds": 1.0})
        permissions = ["read", "write"]
        calls = []
        def api(method, path, supplied, body, source, **kwargs):
            nonlocal value
            self.assertEqual(supplied, own_binding)
            self.assertEqual(source["INFISICAL_READ_PATHS"], settings.FOLDER)
            self.assertEqual(source["INFISICAL_WRITE_PATHS"], settings.FOLDER)
            calls.append((method, path))
            if path == "/v2/service-token":
                return 200, {"workspace": project, "permissions": permissions,
                             "scopes": [{"environment": "prod", "secretPath": settings.FOLDER}],
                             "expiresAt": "2099-01-01T00:00:00Z"}
            if method == "PATCH":
                value = settings.decode(body["secretValue"])
                return 200, {}
            self.assertIn("/raw/OPERATOR_SETTINGS?", path)
            self.assertIn("recursive=false", path)
            return 200, {"secret": {"secretKey": settings.KEY, "secretValue": settings.encode(value),
                                    "type": "shared", "workspace": project, "environment": "prod",
                                    "secretPath": settings.FOLDER}}
        with mock.patch.object(fleet_infisical, "_api_json", side_effect=api):
            store = settings.InfisicalStore(env, writer=True)
            self.assertEqual(store.read(), value)
            proposed = settings.document({"daemon.coalesce_seconds": 2.0})
            store.write(proposed)
            self.assertEqual(store.read(), proposed)
            self.assertEqual(sum(method == "PATCH" for method, _ in calls), 1)
            permissions[:] = ["read"]
            calls.clear()
            with self.assertRaises(settings.MutationError) as caught:
                store.write(proposed)
            self.assertFalse(caught.exception.sent)
            self.assertEqual(calls, [("GET", "/v2/service-token")])


class RequestContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from simple_tools.tests.test_eavesdropper_settings_edit import load_control
        cls.control = load_control()

    def parse(self, value, mode="yes"):
        if value.get("action") in ("save", "initialize", "reconcile"):
            value = dict({"requestID": str(uuid.uuid4()), "requestCreatedAt": int(time.time() * 1000)}, **value)
        ctx = types.SimpleNamespace(stdin=lambda: json.dumps(value).encode())
        return self.control.eavesdropper_settings_request_from_stdin(ctx, mode)

    def test_direct_cli_cannot_reach_canonical_writes_without_server_guards(self):
        with tempfile.TemporaryDirectory() as directory:
            for action in ("save", "initialize"):
                with self.subTest(action=action):
                    reader = mock.Mock(side_effect=AssertionError("no container call allowed"))
                    ctx = self.control.Context(
                        platform="linux", uid=os.getuid(), home=Path(directory),
                        environ={"PATH": "/usr/bin:/bin"}, reader=reader, stdin=lambda: b"",
                    )
                    argv = ["eavesdropper-settings", action, "--expect", "0" * 64, "--json"]
                    if action == "save":
                        argv += ["--set", "daemon.coalesce_seconds=2.0"]
                    out = io.StringIO()
                    code = self.control.main(argv, ctx=ctx, out=out)
                    self.assertEqual(code, 2)
                    self.assertEqual(json.loads(out.getvalue())["result"], "refused")
                    reader.assert_not_called()

    def test_save_and_initialization_require_revision_and_write_mode(self):
        expected = "0" * 64
        for action in ("save", "initialize"):
            value = {"action": action, "expectBaseSHA256": expected}
            if action == "save":
                value["set"] = {"daemon.coalesce_seconds": 2.0}
            parsed = self.parse(value)
            self.assertEqual((parsed[0], parsed[3]), (action, expected))
            with self.assertRaises(self.control.Refused):
                self.parse(value, "plan")
            del value["expectBaseSHA256"]
            with self.assertRaises(self.control.Refused):
                self.parse(value)

    def test_initializer_accepts_no_values_and_save_accepts_no_plan_hash(self):
        for value in (
            {"action": "initialize", "expectBaseSHA256": "0" * 64, "set": {"daemon.coalesce_seconds": 2.0}},
            {"action": "save", "expectBaseSHA256": "0" * 64, "expectPlanSHA256": "0" * 64, "set": {"daemon.coalesce_seconds": 2.0}},
        ):
            with self.subTest(action=value["action"]), self.assertRaises(self.control.Refused):
                self.parse(value)

    def test_save_carries_explicit_table_subkey_removals_to_the_existing_editor(self):
        value = {"action": "save", "expectBaseSHA256": "0" * 64,
                 "set": {"seat.GROK.budget": {"board_per_day": 0}},
                 "remove": ["seat.GROK.budget.wakes_per_day"]}
        self.assertEqual(self.parse(value)[2], value["remove"])


class DaemonCopyOwnershipTests(ServerHarness):
    def test_listener_and_entrypoint_strip_the_writer_but_keep_its_reader_separate(self):
        from simple_tools.tests.test_server_rollout import IE, FakeInfisical, INFISICAL_PROJECT_ID
        writer = "st." + secrets.token_urlsafe(32)
        reader = "st." + secrets.token_urlsafe(32)
        env = self.server_env(**{settings.WRITE_BINDING: writer})
        daemon = self.server_daemon(env=env)
        self.assertNotIn(settings.WRITE_BINDING, daemon.env)
        self.assertEqual(env[settings.WRITE_BINDING], writer)
        with FakeInfisical("unused", "unused", {}) as fake:
            built, _ = IE.build_env({
                "INFISICAL_TOKEN": fake.token,
                "EAVESDROPPER_WAKE_ROUTER_INFISICAL_TOKEN": fake.wake_token,
                "INFISICAL_PROJECT_ID": INFISICAL_PROJECT_ID,
                "INFISICAL_API_URL": fake.url,
                settings.WRITE_BINDING: writer, settings.READ_BINDING: reader,
            })
        self.assertNotIn(settings.WRITE_BINDING, built)
        self.assertEqual(built[settings.READ_BINDING], reader)

    def test_competing_constructor_reload_and_rebuild_cannot_write_verified_copy(self):
        import fcntl
        from simple_tools import config

        original_load = config.load
        canonical = settings.document({"daemon.coalesce_seconds": 20.0})

        def marked(*args, **kwargs):
            result = original_load(*args, **kwargs)
            result.operator_settings_source = "infisical"
            result.operator_settings_document = canonical
            result.operator_settings_revision = settings.revision(canonical)
            return result

        listener = self.state_dir / "listener"
        listener.mkdir(parents=True, exist_ok=True)
        with open(listener / "daemon.lock", "w") as running:
            fcntl.flock(running, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with mock.patch.object(config, "load", side_effect=marked), mock.patch.object(settings, "remember_verified") as remember:
                daemon = self.server_daemon(stderr=io.StringIO())
                self.assertTrue(daemon.deferred)
                remember.assert_not_called()
                daemon.reload()
                daemon.rebuild_from_disk()
                with mock.patch("simple_tools.daemon.signal.signal"):
                    self.assertEqual(daemon.run(), 1)
                remember.assert_not_called()
                self.assertIsNone(daemon._operator_settings_lock_fd)

    def test_waiting_snapshot_is_cached_only_after_actual_lock_and_rebuild(self):
        import fcntl
        from simple_tools import config

        original_load = config.load
        canonical = settings.document({"daemon.coalesce_seconds": 20.0})
        accepted = []
        current = []

        def marked(*args, **kwargs):
            result = original_load(*args, **kwargs)
            result.operator_settings_source = "infisical"
            result.operator_settings_document = canonical
            return result

        def remember(*args):
            daemon = current[0]
            self.assertIsNotNone(daemon._operator_settings_lock_fd)
            self.assertFalse(daemon.deferred)
            with open(daemon.lock_path()) as competing:
                with self.assertRaises(OSError):
                    fcntl.flock(competing, fcntl.LOCK_EX | fcntl.LOCK_NB)
            accepted.append(args)

        with mock.patch.object(config, "load", side_effect=marked), mock.patch.object(settings, "remember_verified", side_effect=remember):
            daemon = self.server_daemon(stderr=io.StringIO())
            current.append(daemon)
            daemon.deferred = True
            daemon.stop.set()
            with mock.patch("simple_tools.daemon.signal.signal"), mock.patch.object(daemon, "install_excepthooks"):
                self.assertEqual(daemon.run(), 0)
            self.assertEqual(len(accepted), 1)
            self.assertIsNone(daemon._operator_settings_lock_fd)


class InstalledPayloadTests(unittest.TestCase):
    def test_missing_canonical_module_refuses_a_new_managed_bundle(self):
        import test_fleet_cli_release as fixtures
        required = (
            "scripts/simple_tools/server/operator_settings.py",
            "scripts/simple_tools/operator_github_state.py",
            "scripts/simple_tools/server/eavesdropper_settings_edit.py",
            "scripts/fleet-ops-eavesdropper-settings.json",
        )
        for path in required:
            with self.subTest(path=path):
                self.assertIn(path, fixtures.release.EXACT)
                self.assertIn(path, fixtures.release.TOOL_REQUIRED)
                self.assertNotIn(path, fixtures.release.BASE_REQUIRED)
                bundle = fixtures.make_bundle()
                bundle["files"].pop(path)
                bundle["bundle_sha256"] = fixtures.release.manifest_digest(bundle["files"])
                with self.assertRaisesRegex(fixtures.release.Refused, "bad_file_set"):
                    fixtures.release.decode_bundle(json.dumps(bundle).encode())


if __name__ == "__main__":
    unittest.main()
