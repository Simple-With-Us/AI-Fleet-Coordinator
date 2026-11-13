"""Canonical autosave fences, actual readback and partial runtime outcomes."""

from __future__ import annotations

import copy
import io
import json
import os
import secrets
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

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
        source = Path(__file__).resolve().parents[2] / "fleet-ops-eavesdropper-settings.json"
        self.allowlist = editor.validate_allowlist(json.loads(source.read_text()))
        self.document = settings.document({
            "daemon.coalesce_seconds": 1.0,
            "daemon.presence_topics": [["Work-In-Progress", "A Topic"]],
            "seat.GROK.wake": "inbox",
        })
        self.store = Store(self.document)

    def save(self, patch=None, **kwargs):
        return settings.save(
            self.path, self.allowlist, patch or {"daemon.coalesce_seconds": 2.0},
            settings.revision(self.document), store=self.store,
            checker=kwargs.pop("checker", lambda *_: {"ok": True}),
            reloader=kwargs.pop("reloader", lambda *_: {
                "ok": True, "confirmed": True, "code": "reloaded",
                "revision": settings.revision(self.store.document),
            }),
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
        }
        for current in (invalid, settings.document({private: 1})):
            with self.subTest(secret_is_key=private in current["settings"]):
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
            checker=lambda *_: {"ok": True}, accepted=lambda _: True,
        )
        self.assertTrue(answer["ok"])
        self.assertEqual(answer["result"], "initialized")
        self.assertTrue(answer["persisted"] and answer["readback"])
        self.assertFalse(answer["runtimeApplied"])
        self.assertEqual(len(absent.writes), 1)
        again = settings.initialize(
            self.path, self.allowlist, editor.sha256(self.path.read_bytes()), store=absent,
            checker=lambda *_: {"ok": True}, accepted=lambda _: True,
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
            checker=lambda *_: {"ok": True}, accepted=lambda _: True,
        )
        self.assertEqual(answer["errorCode"], "initialization_conflict")
        self.assertEqual(absent.document, competing)
        self.assertEqual(absent.writes, [])

    def test_initializer_requires_current_daemon_acceptance(self):
        absent = Store(None)
        answer = settings.initialize(
            self.path, self.allowlist, editor.sha256(self.path.read_bytes()), store=absent,
            checker=lambda *_: {"ok": True}, accepted=lambda _: False,
        )
        self.assertEqual(answer["errorCode"], "runtime_not_accepted")
        self.assertEqual(absent.writes, [])

    def test_initializer_readback_failure_does_not_replay_create(self):
        absent = Store(None)
        absent.read_error_after_write = True
        answer = settings.initialize(
            self.path, self.allowlist, editor.sha256(self.path.read_bytes()), store=absent,
            checker=lambda *_: {"ok": True}, accepted=lambda _: True,
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
                with mock.patch.object(editor, "_cli", return_value=(0, "")):
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
        self.assertEqual(str(caught.exception), "provider_write_failed")
        self.assertEqual(self.core._api_json.call_count, 1)

    def test_initializer_transport_is_create_only_and_never_falls_back_to_patch(self):
        self.core._api_json.return_value = (400, {"message": "already exists fixture"})
        store = settings.InfisicalStore(self.env, writer=True)
        with self.assertRaises(settings.SettingsError):
            store.create(settings.document({"daemon.coalesce_seconds": 1.0}))
        self.assertEqual(self.core._api_json.call_count, 1)
        self.assertEqual(self.core._api_json.call_args.args[0], "POST")


class RequestContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from simple_tools.tests.test_eavesdropper_settings_edit import load_control
        cls.control = load_control()

    def parse(self, value, mode="yes"):
        ctx = types.SimpleNamespace(stdin=lambda: json.dumps(value).encode())
        return self.control.eavesdropper_settings_request_from_stdin(ctx, mode)

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
