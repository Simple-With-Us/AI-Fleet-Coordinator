import dataclasses
import importlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

fixture = importlib.import_module("test_integration")
builder = importlib.import_module("native_fixture_package.native_qualification_builder")


@dataclasses.dataclass(frozen=True)
class Platform:
    deliver: object
    deliver_turn: object
    exclude: object = None


class Delivery:
    def __init__(self, outcome, route="", code=""):
        self.outcome, self.route, self.code = outcome, route, code


class Turn:
    def __init__(self, delivery, **kw):
        self.delivery = delivery


class BuilderTests(unittest.TestCase):
    def test_exact_callback_wiring_and_unchanged_base_config(self):
        with tempfile.TemporaryDirectory(dir="/dev/shm") as tmp:
            root = Path(tmp)
            calls = []
            captured = {}
            base = Platform(
                lambda *a: calls.append("base-single") or Delivery("sent"),
                lambda *a: calls.append("base-turn") or Turn(Delivery("sent")),
            )

            def platform(env, config, **kw):
                self.assertFalse(config["native_delivery_review_only"])
                return base

            def prompt(*a, **kw):
                calls.append(("native", a, kw))
                return Delivery("unknown")

            adapter = SimpleNamespace(
                router_platform=platform,
                state_dir=lambda e: root,
                _recheck=lambda *a: None,
                _session_binding=lambda *a: (
                    "ok",
                    {"id": "sess_fixture", "directory": tmp},
                ),
                _subagent_ids=lambda *a: ("ok", set()),
                _deliver_prompt=prompt,
                WR=SimpleNamespace(Delivery=Delivery, Turn=Turn),
            )

            def qualify(*args):
                captured["native"] = args[5]
                captured["fallback"] = args[6]
                captured["render"] = args[7]

                def run(target, head, riders, turn, **kw):
                    return args[5](target, head, args[7](head, turn), turn)

                return run

            config = {"workspace": tmp, "native_delivery_review_only": False}
            spec = {
                "target": {
                    "session_id": "sess_fixture",
                    "host": "server",
                    "letter": "II",
                    "generation": 2,
                },
                "workspace": tmp,
                "epoch": 100,
                "expires": 200,
            }
            driver = SimpleNamespace()
            with (
                patch.object(builder, "Driver", return_value=driver),
                patch.object(builder, "Qualification", side_effect=qualify),
                patch.object(builder, "fresh", return_value=True),
            ):
                p, q, d, c = builder.build(adapter, {"HOME": tmp}, config, spec)
                target = SimpleNamespace(**spec["target"])
                head = SimpleNamespace(wake_id="ra-new", message_id=123)
                p.deliver(target, head)
                native = calls[-1]
                self.assertEqual(native[0], "native")
                self.assertTrue(callable(native[2]["native_driver"]))
                self.assertEqual(native[2]["evidence_ids"], ("ra-new",))
                self.assertTrue(native[1][1]["native_delivery_review_only"])
                self.assertEqual(
                    native[1][3],
                    builder.notification(head, "t-" + builder.digest({"wake_key": "ra-new"})[:16]),
                )
                self.assertEqual(config, {"workspace": tmp, "native_delivery_review_only": False})
                self.assertIs(p.exclude, base.exclude)
                p.deliver_turn(target, head, (head,), "t-1234567890123456")
                self.assertEqual(calls[-1], "base-turn")
                self.assertTrue(c(target, tmp))
                self.assertFalse(c(target, "/wrong"))

    def test_notification_has_only_validated_metadata(self):
        h = SimpleNamespace(
            wake_id="ra-new",
            message_id=123,
            topic="injected",
            sender="other",
            text="external",
        )
        text = builder.notification(h, "t-1234567890123456")
        self.assertIn("message_id 123.", text)
        for word in ["injected", "external", "other"]:
            self.assertNotIn(word, text)

    def test_launcher_changes_only_zcode_builder_and_no_recursive_base(self):
        launch = importlib.import_module("native_fixture_package.native_qualification_launch")
        original = lambda *a, **k: "original"
        zcode = SimpleNamespace(router_platform=original)
        candidate = SimpleNamespace()
        spec = {"fixture": True}
        calls = []

        def build(*a, **k):
            calls.append((a, k))
            self.assertIs(a[0].router_platform, original)
            return ("qualified", "q", "driver", "custody")

        launch.install(zcode, candidate, SimpleNamespace(build=build), spec)
        self.assertEqual(zcode.router_platform({"HOME": "/fixture"}, {"existing": 1}), "qualified")
        self.assertEqual(len(calls), 1)
        self.assertIs(calls[0][0][3], spec)

    def test_final_driver_verifier_enforces_expiry(self):
        source = Path(builder.__file__).read_text()
        import ast

        tree = ast.parse(source)
        build = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "build")
        native = next(
            n for n in build.body if isinstance(n, ast.FunctionDef) and n.name == "native"
        )
        captured = []
        now = [101]

        def driver(*a, **kw):
            captured.append(kw["verify"]({}))
            now[0] = 200
            captured.append(kw["verify"]({}))
            return {}

        def prompt(*a, **kw):
            return kw["native_driver"]({}, verify=lambda b: True)

        env = {
            "env": {},
            "registry_context": dict,
            "adapter": SimpleNamespace(_deliver_prompt=prompt),
            "spec": {"epoch": 100, "expires": 200},
            "time": SimpleNamespace(time=lambda: now[0]),
            "driver": driver,
            "local_config": {},
            "config": {},
            "state": Path("/fixture"),
            "fresh": lambda *a: True,
        }
        exec(
            compile(ast.Module(body=[native], type_ignores=[]), "<native-deadline>", "exec"),
            env,
        )
        env["native"](
            SimpleNamespace(session_id="sess_fixture"),
            SimpleNamespace(wake_id="ra-new"),
            "text",
            "turn",
        )
        self.assertEqual(captured, [True, False])

    def test_callback_specific_fallback_matrix(self):
        with tempfile.TemporaryDirectory(dir="/dev/shm") as tmp:
            root = Path(tmp)
            for n in ["pending", "done"]:
                (root / n).mkdir()
            calls = []
            base = Platform(
                lambda *a: calls.append(("single", a)) or Delivery("sent"),
                lambda *a: calls.append(("turn", a)) or Turn(Delivery("sent")),
            )
            target = SimpleNamespace(
                session_id="sess_fixture", host="server", letter="II", generation=2
            )
            head = SimpleNamespace(
                wake_id="ra-new", message_id=123, link_note="required-session metadata"
            )
            adapter = SimpleNamespace(
                router_platform=lambda *a, **k: base,
                state_dir=lambda e: root,
                _recheck=lambda *a: None,
                _session_binding=lambda *a: (
                    "ok",
                    {"id": target.session_id, "directory": tmp},
                ),
                _subagent_ids=lambda *a: ("ok", set()),
                _deliver_prompt=lambda *a, **k: Delivery("unknown"),
                WR=SimpleNamespace(Delivery=Delivery, Turn=Turn),
            )
            spec = {
                "target": vars(target),
                "workspace": tmp,
                "epoch": 100,
                "expires": 200,
            }
            with patch.object(builder, "Driver", return_value=SimpleNamespace()):
                p, q, d, c = builder.build(adapter, {"HOME": tmp}, {}, spec)
            q.clock = lambda: 101
            turn = "t-1234567890123456"
            try:
                # Both paths preserve original args, including head-only turn metadata.
                for case in ["unrelated", "expired", "ineligible", "later"]:
                    selected = target
                    q.clock = lambda: 101
                    if case == "unrelated":
                        selected = SimpleNamespace(**{**vars(target), "session_id": "sess_other"})
                    if case == "expired":
                        q.clock = lambda: 200
                    if case == "later":
                        import json

                        from native_fixture_package.zcode_native import digest

                        text = "reserved"
                        claim = {
                            "version": 1,
                            "target": vars(target),
                            "workspace": tmp,
                            "wake_id": "ra-earlier",
                            "message_id": 122,
                            "notification_turn_id": turn,
                            "text": text,
                            "text_sha256": digest(text),
                            "epoch": 100,
                            "expires": 200,
                            "reserved_at": 101,
                        }
                        f = q.store.folder / "slot.claim"
                        f.write_text(json.dumps(claim))
                        f.chmod(0o600)
                    p.deliver(selected, head)
                    self.assertEqual(calls[-1], ("single", (selected, head)))
                    result = p.deliver_turn(selected, head, (), turn)
                    self.assertIsInstance(result, Turn)
                    self.assertEqual(calls[-1], ("turn", (selected, head, (), turn)))
                    if case == "later":
                        (q.store.folder / "slot.claim").unlink()
            finally:
                q.close()

    def test_event_driven_driver_retains_both_binding_checks(self):
        import ast

        tree = ast.parse(Path(builder.__file__).read_text())
        build = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "build")
        native = next(
            n for n in build.body if isinstance(n, ast.FunctionDef) and n.name == "native"
        )
        now = [1000000]
        captured = []

        def driver(*a, **kw):
            captured.append(kw["verify"]({}))
            now[0] = 99
            captured.append(kw["verify"]({}))
            return {}

        def prompt(*a, **kw):
            return kw["native_driver"]({}, verify=lambda b: True)

        env = {
            "env": {},
            "registry_context": dict,
            "adapter": SimpleNamespace(_deliver_prompt=prompt),
            "spec": {"epoch": 100, "expires": None},
            "time": SimpleNamespace(time=lambda: now[0]),
            "driver": driver,
            "local_config": {},
            "config": {},
            "state": Path("/fixture"),
            "fresh": lambda *a: True,
        }
        exec(
            compile(ast.Module(body=[native], type_ignores=[]), "<event-driven>", "exec"),
            env,
        )
        env["native"](
            SimpleNamespace(session_id="sess_fixture"),
            SimpleNamespace(wake_id="ra-new"),
            "text",
            "turn",
        )
        self.assertEqual(captured, [True, False])

    def test_live_registry_context_refresh_missing_and_ownership_refusal(self):
        from collections.abc import Mapping

        class Settings(dict):
            registry = {"address": "registry-one", "credential": "scope-one"}

            def registry_env(self, raw):
                owner = self

                class Live(Mapping):
                    def __getitem__(self, key):
                        return owner.registry[key] if key in owner.registry else raw[key]

                    def __iter__(self):
                        return iter(set(raw) | set(owner.registry))

                    def __len__(self):
                        return len(set(raw) | set(owner.registry))

                return Live()

        with tempfile.TemporaryDirectory(dir="/dev/shm") as tmp:
            config = Settings()
            calls = []
            captured = {}
            target = SimpleNamespace(
                session_id="sess_fixture", host="server", letter="II", generation=2
            )
            row = {"id": target.session_id, "directory": tmp}
            expected = ["registry-one", "scope-one"]

            def recheck(context, selected):
                calls.append((context.get("address"), context.get("credential")))
                return (
                    None
                    if calls[-1] == tuple(expected) and selected.generation == 2
                    else Delivery("refused")
                )

            def prompt(context, *a, **kw):
                captured["context"] = context
                return Delivery("unknown")

            def qualify(*args):
                captured["native"] = args[5]
                return SimpleNamespace()

            adapter = SimpleNamespace(
                router_platform=lambda *a, **kw: Platform(lambda *a: None, lambda *a: None),
                state_dir=lambda e: Path(tmp),
                _recheck=recheck,
                _session_binding=lambda *a: ("ok", row),
                _subagent_ids=lambda *a: ("ok", set()),
                _deliver_prompt=prompt,
                WR=SimpleNamespace(Delivery=Delivery, Turn=Turn),
            )
            spec = {"target": vars(target), "workspace": tmp, "epoch": 100, "expires": None}
            raw = {"HOME": tmp}
            with (
                patch.object(builder, "Driver"),
                patch.object(builder, "Qualification", side_effect=qualify),
                patch.object(builder, "fresh", return_value=True),
            ):
                platform, qualifier, driver, custody = builder.build(adapter, raw, config, spec)
                self.assertTrue(custody(target, tmp))
                captured["native"](
                    target, SimpleNamespace(wake_id="ra-fresh"), "notification", "turn"
                )
                live = captured["context"]
                self.assertEqual(live["address"], "registry-one")
                config.registry = {"address": "registry-two", "credential": "scope-two"}
                expected[:] = ["registry-two", "scope-two"]
                self.assertEqual(live["address"], "registry-two")
                self.assertTrue(custody(target, tmp))
                captured["native"](
                    target, SimpleNamespace(wake_id="ra-next"), "notification", "turn"
                )
                self.assertEqual(captured["context"]["credential"], "scope-two")
                config.registry = {}
                self.assertFalse(custody(target, tmp))
                config.registry = {"address": "registry-two", "credential": "scope-two"}
                self.assertFalse(custody(SimpleNamespace(**{**vars(target), "generation": 3}), tmp))
                row["directory"] = "/wrong"
                self.assertFalse(custody(target, tmp))
                self.assertEqual(raw, {"HOME": tmp})
