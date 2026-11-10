"""The Fx adapter of `platform-letter`: the fx CLI has no hook configuration, so the adapter
registers the seat's sessions from the store fx itself keeps (`~/.fx/sessions`), through the same
registry side every other platform uses.

The registry is a temporary one in the double (pair) letter space.  The API test runs a real registry
service over a loopback listener, so a letter that lands there has crossed the registry API transport.
No test depends on which pair the registry hands out first."""

from __future__ import annotations

import io
import json
import os
import secrets
import tempfile
import time
import unittest

from simple_tools import listener_api as API
from simple_tools import listener_registry as RG
from simple_tools import operator_registry as OR
from simple_tools import platform_letter as PL
from simple_tools import platform_letter_fx as FX

PAIR = r"^[A-Z]{2}$"


def conv(n: int) -> str:
    return "conv-%04d-0000-0000-0000-000000000000" % n


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = os.path.join(self.tmp.name, "home")
        self.state = os.path.join(self.tmp.name, "state")
        os.makedirs(self.home)
        self.db = os.path.join(self.state, "operator", "registry.sqlite3")
        OR.SessionRegistry(self.db).close_db()
        self.sessions = os.path.join(self.home, ".fx", "sessions")
        os.makedirs(self.sessions)

    def env(self, **extra: str) -> dict[str, str]:
        env = {"HOME": self.home, "SIMPLE_TOOLS_STATE_DIR": self.state}
        env.update(extra)
        return env

    def add_session(self, sid: str, *, title: str = "", updated: float | None = None) -> None:
        folder = os.path.join(self.sessions, sid)
        os.makedirs(folder)
        meta = {"title": title, "workspace_root": "/tmp/work", "updated_at": updated or time.time()}
        with open(os.path.join(folder, "session.json"), "w") as fh:
            json.dump(meta, fh)
        index_path = os.path.join(self.sessions, "index.json")
        index = json.load(open(index_path)) if os.path.exists(index_path) else {"sessions": []}
        index["sessions"].append({"id": sid})
        with open(index_path, "w") as fh:
            json.dump(index, fh)

    def sync(self, *flags: str, env: dict | None = None, now: float | None = None) -> dict:
        out = io.StringIO()
        code = FX.sync_main(list(flags), env if env is not None else self.env(), out, now=now)
        answer = json.loads(out.getvalue())
        self.assertEqual(code, 0, answer)
        return answer

    def lookup(self, sid: str) -> dict | None:
        reg = OR.SessionRegistry(self.db)
        try:
            return reg.lookup("fx", "mac", PL.SCOPE, sid)
        finally:
            reg.close_db()


class FxTests(Base):
    def test_spec_fields(self) -> None:
        spec = PL.load_spec("fx")
        self.assertIs(spec, FX.SPEC)
        self.assertEqual((spec.seat, spec.source), ("FX", "fx-letter-sync"))

    def test_install_refuses_because_fx_has_no_hooks(self) -> None:
        out = io.StringIO()
        code = PL.install_main(["--platform", "fx"], self.env(), out)
        self.assertEqual(code, 2)
        self.assertIn("no hook configuration", out.getvalue())

    def test_a_dry_run_registers_nothing(self) -> None:
        sid = conv(21)
        self.add_session(sid, title="Fx work")
        answer = self.sync()
        self.assertEqual(answer["registered"], [{"session_id": sid, "would_register": True}])
        self.assertIsNone(self.lookup(sid))

    def test_sync_registers_live_sessions_and_is_idempotent(self) -> None:
        sid = conv(22)
        self.add_session(sid, title="Fx work")
        answer = self.sync("--apply")
        self.assertEqual(answer["failed"], [])
        record = self.lookup(sid)
        self.assertIsNotNone(record)
        self.assertRegex(record["label"], PAIR)
        self.assertEqual(record["title"], "Fx work")
        again = self.sync("--apply")
        self.assertEqual(self.lookup(sid)["label"], record["label"])
        self.assertEqual(again["registered"][0]["outcome"], "resumed")

    def test_a_stale_session_is_closed_by_the_next_sync(self) -> None:
        sid = conv(23)
        self.add_session(sid, title="Old work", updated=time.time() - 1000)
        self.sync("--apply", now=time.time())
        self.assertIsNotNone(self.lookup(sid))
        answer = self.sync("--apply", now=time.time() + PL.STALE_SECONDS + 60)
        self.assertEqual(answer["closed"], [sid])
        self.assertEqual(self.lookup(sid)["state"], "closed")

    def test_a_session_deleted_from_the_store_is_closed_by_the_next_sync(self) -> None:
        sid = conv(25)
        self.add_session(sid, title="Gone soon")
        self.sync("--apply")
        self.assertEqual(self.lookup(sid)["state"], "active")
        import shutil

        shutil.rmtree(os.path.join(self.sessions, sid))
        with open(os.path.join(self.sessions, "index.json"), "w") as fh:
            json.dump({"sessions": []}, fh)
        answer = self.sync("--apply")
        self.assertEqual(answer["closed"], [sid])
        self.assertEqual(self.lookup(sid)["state"], "closed")

    def test_a_session_with_no_readable_time_is_neither_registered_nor_closed(self) -> None:
        sid = conv(26)
        self.add_session(sid, title="Live")
        self.sync("--apply")
        with open(os.path.join(self.sessions, sid, "session.json"), "w") as fh:
            json.dump({"title": "Live"}, fh)
        answer = self.sync("--apply", now=time.time() + PL.STALE_SECONDS + 60)
        self.assertEqual(answer["closed"], [])
        self.assertEqual(self.lookup(sid)["state"], "active")
        fresh = conv(27)
        folder = os.path.join(self.sessions, fresh)
        os.makedirs(folder)
        with open(os.path.join(folder, "session.json"), "w") as fh:
            json.dump({"title": "No time"}, fh)
        with open(os.path.join(self.sessions, "index.json")) as fh:
            index = json.load(fh)
        index["sessions"].append({"id": fresh})
        with open(os.path.join(self.sessions, "index.json"), "w") as fh:
            json.dump(index, fh)
        answer = self.sync("--apply")
        self.assertNotIn(fresh, [r["session_id"] for r in answer["registered"]])
        self.assertIsNone(self.lookup(fresh))

    def test_a_registration_held_under_another_seat_is_never_closed(self) -> None:
        sid = conv(28)
        self.add_session(sid, title="Operator's", updated=time.time() - 1000)
        out = io.StringIO()
        argv = ["register", "--platform", "fx", "--location", "mac", "--session-id", sid]
        code = PL.registry_main(argv + ["--seat", "OPERATOR", "--source", "test"], env=self.env(), stdout=out)
        self.assertEqual(code, 0, out.getvalue())
        answer = self.sync("--apply", now=time.time() + PL.STALE_SECONDS + 60)
        self.assertEqual(answer["closed"], [])
        self.assertEqual(self.lookup(sid)["state"], "active")

    def test_fx_kind_reads_the_one_session_named(self) -> None:
        sid = conv(29)
        self.add_session(sid, title="Kind")
        self.assertEqual(FX.fx_kind({"session_id": sid}, self.env()), "chat")
        self.assertEqual(FX.fx_kind({"session_id": conv(30)}, self.env()), "headless")
        self.assertEqual(FX.fx_kind({"session_id": "../" + sid}, self.env()), "headless")


class FxApiTests(Base):
    def test_sync_registers_through_the_registry_api(self) -> None:
        key = secrets.token_hex(24)
        service = RG.RegistryService(self.db, {"OPERATOR": key})
        server = API.ListenerReadApi("127.0.0.1", 0, self.state, {}, registry=service)
        server.start()
        self.addCleanup(server.close)
        host, port = server.address
        env = self.env(
            SIMPLE_TOOLS_OPERATOR_REGISTRY_URL="http://%s:%d" % (host, port),
            SIMPLE_TOOLS_OPERATOR_REGISTRY_PRINCIPAL="OPERATOR",
            SIMPLE_TOOLS_OPERATOR_REGISTRY_KEY=key,
        )
        sid = conv(24)
        self.add_session(sid, title="Fx over the API")
        answer = self.sync("--apply", env=env)
        self.assertEqual(answer["failed"], [])
        record = self.lookup(sid)
        self.assertIsNotNone(record)
        self.assertEqual(record["seat"], "FX")


if __name__ == "__main__":
    unittest.main()
