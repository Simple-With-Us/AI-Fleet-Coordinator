"""The ZCode adapter of `platform-letter` (OPS-1048): Operator session letters and "XX: Title" names for ZCode
chats on the server and the Mac.  ZCode's own store is a temporary SQLite file shaped like the real
`~/.zcode/cli/db/db.sqlite` (`session`), the registry is a temporary one in the double (pair) letter space, and the
hook config is a temporary `~/.zcode/cli/config.json` that already carries another tool's hooks.  No test depends
on which pair the registry hands out first, only on its being a pair, and on the child landing after its parent in
the parent's row."""

from __future__ import annotations

import io
import json
import os
import sqlite3
import subprocess
import tempfile
import time
import unittest

from simple_tools import listener_api as API
from simple_tools import listener_registry as RG
from simple_tools import operator_registry as OR
from simple_tools import platform_letter as PL
from simple_tools import platform_letter_zcode as Z

PAIR = r"^[A-Z]{2}$"

SESSION_DDL = (
    "CREATE TABLE session ("
    " id text primary key,"
    " project_id text not null default '',"
    " parent_id text,"
    " slug text not null default '',"
    " directory text not null default '',"
    " title text not null default '',"
    " time_created integer not null default 0,"
    " time_updated integer not null default 0,"
    " task_type text not null default 'interactive',"
    " title_source text not null default 'first_input',"
    " time_title_updated integer,"
    " time_archived integer,"
    " time_deleted integer)"
)


def sess(n: int) -> str:
    return "sess_%010d-0000-0000-0000-000000000000" % n


class Base(unittest.TestCase):
    """A home with a ZCode store, and the Operator's registry under the state folder."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = os.path.join(self.tmp.name, "home")
        self.state = os.path.join(self.tmp.name, "state")
        os.makedirs(os.path.join(self.home, ".zcode", "cli", "db"))
        self.store = os.path.join(self.home, ".zcode", "cli", "db", "db.sqlite")
        conn = sqlite3.connect(self.store)
        with conn:
            conn.execute(SESSION_DDL)
        conn.close()
        self.db = os.path.join(self.state, "operator", "registry.sqlite3")
        OR.SessionRegistry(self.db).close_db()

    def env(self, **extra: str) -> dict[str, str]:
        env = {"HOME": self.home, "SIMPLE_TOOLS_STATE_DIR": self.state}
        env.update(extra)
        return env

    def add_session(self, sid: str, *, parent: str = "", task: str = "interactive",
                    title: str = "", title_source: str = "first_input") -> None:
        conn = sqlite3.connect(self.store)
        with conn:
            conn.execute(
                "INSERT INTO session (id, parent_id, task_type, title, title_source, directory,"
                " time_created, time_updated) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (sid, parent or None, task, title, title_source, "/tmp/work", 1, 1),
            )
        conn.close()

    def run_hook(self, payload: dict, *, location: str = "mac", now: float | None = None,
                 env: dict | None = None) -> tuple[int, str]:
        out = io.StringIO()
        code = PL.hook(
            Z.SPEC,
            env if env is not None else self.env(),
            io.StringIO(json.dumps(payload)),
            out,
            location_flag=location,
            now=time.time() if now is None else now,
        )
        return code, out.getvalue()

    def rows(self) -> list[tuple]:
        conn = sqlite3.connect(self.store)
        rows = conn.execute(
            "SELECT id, title, title_source FROM session ORDER BY id"
        ).fetchall()
        conn.close()
        return rows


class Spec(Base):
    def test_spec_fields(self) -> None:
        spec = PL.load_spec("zcode")
        self.assertIs(spec, Z.SPEC)
        self.assertEqual(spec.platform, "zcode")
        self.assertEqual(spec.seat_for({}, self.env()), "ZC")
        self.assertEqual(spec.display, "ZCode")
        self.assertEqual(spec.locations, ("mac", "server"))
        self.assertEqual(spec.off_variable, "ZCODE_LETTER")
        self.assertEqual(spec.events["SessionStart"], "start")
        self.assertEqual(spec.events["UserPromptSubmit"], "prompt")
        self.assertEqual(spec.events["Stop"], "end")
        self.assertEqual(spec.close_reasons, ())
        self.assertIn("Stop", spec.quiet_events)


class Accessors(Base):
    def test_parent_kind_titles_from_store(self) -> None:
        parent, child = sess(1), sess(2)
        self.add_session(parent, title="Mutex semantics")
        self.add_session(child, parent=parent, task="subagent_child", title="Search breadth")
        payload = {"session_id": child, "hook_event_name": "SessionStart"}
        self.assertEqual(Z.zcode_parent(payload, self.env()), parent)
        self.assertEqual(Z.zcode_kind(payload, self.env()), "subagent")
        self.assertEqual(Z.zcode_stored_title(payload, self.env()), "Search breadth")
        self.assertEqual(
            Z.zcode_kind({"session_id": parent, "hook_event_name": "SessionStart"}, self.env()),
            "chat",
        )

    def test_unknown_session_is_a_chat_without_parent(self) -> None:
        payload = {"session_id": sess(99), "hook_event_name": "SessionStart"}
        self.assertEqual(Z.zcode_kind(payload, self.env()), "chat")
        self.assertEqual(Z.zcode_parent(payload, self.env()), "")

    def test_payload_title_wins_over_store(self) -> None:
        sid = sess(3)
        self.add_session(sid, title="from store")
        payload = {"session_id": sid, "title": "from payload"}
        self.assertEqual(Z.zcode_current_title(payload, self.env()), "from payload")
        self.assertEqual(Z.zcode_current_title({"session_id": sid}, self.env()), "from store")

    def test_chat_payload_carries_start_shape(self) -> None:
        payload = Z.zcode_chat_payload(sess(4))
        self.assertEqual(payload["hook_event_name"], "SessionStart")
        self.assertEqual(payload["session_id"], sess(4))
        self.assertEqual(payload["source"], "startup")


class HookFlow(Base):
    def test_start_registers_a_pair_and_takes_a_letter(self) -> None:
        sid = sess(10)
        self.add_session(sid, title="Fix the wake path")
        code, out = self.run_hook(
            {"session_id": sid, "hook_event_name": "SessionStart", "source": "startup"}
        )
        self.assertEqual(code, 0)
        registry = OR.SessionRegistry(self.db)
        record = registry.lookup("zcode", "mac", "default", sid)
        self.assertTrue(record)
        self.assertRegex(record["label"], PAIR)
        self.assertEqual(record["seat"], "ZC")

    def test_child_takes_the_pair_after_its_parent(self) -> None:
        parent, child = sess(11), sess(12)
        self.add_session(parent, title="Parent work")
        self.add_session(child, parent=parent, task="subagent_child", title="Search the repo")
        self.run_hook(
            {"session_id": parent, "hook_event_name": "SessionStart", "source": "startup"}
        )
        self.run_hook(
            {"session_id": child, "hook_event_name": "SessionStart", "source": "startup"}
        )
        registry = OR.SessionRegistry(self.db)
        mom = registry.lookup("zcode", "mac", "default", parent)
        kid = registry.lookup("zcode", "mac", "default", child)
        row = "ABCDEFGHIJKLMNOPQRSTUVWXYZ".index(mom["label"][0])
        self.assertRegex(mom["label"], PAIR)
        self.assertEqual(mom["label"], mom["label"][0] * 2)  # a main chat takes a doubled pair
        self.assertEqual(kid["label"][0], mom["label"][0])  # the child sits in the parent's row
        self.assertNotEqual(kid["label"], mom["label"])
        self.assertEqual(kid["label"], "%s%s" % (mom["label"][0], "ABCDEFGHIJKLMNOPQRSTUVWXYZ"[row + 1]))

    def test_stop_touches_and_never_closes(self) -> None:
        sid = sess(13)
        self.add_session(sid, title="Long chat")
        self.run_hook(
            {"session_id": sid, "hook_event_name": "SessionStart", "source": "startup"}
        )
        code, out = self.run_hook({"session_id": sid, "hook_event_name": "Stop"})
        self.assertEqual(code, 0)
        registry = OR.SessionRegistry(self.db)
        record = registry.lookup("zcode", "mac", "default", sid)
        self.assertTrue(record)  # a live label after Stop: the registration was not closed

    def test_off_switch_and_launcher_take_no_letter(self) -> None:
        sid = sess(14)
        self.add_session(sid)
        self.run_hook(
            {"session_id": sid, "hook_event_name": "SessionStart", "source": "startup"},
            env=self.env(ZCODE_LETTER="0"),
        )
        registry = OR.SessionRegistry(self.db)
        self.assertIsNone(registry.lookup("zcode", "mac", "default", sid))

    def test_hook_never_fails_the_chat(self) -> None:
        code, out = self.run_hook({"hook_event_name": "SessionStart", "session_id": "not-a-session"})
        self.assertEqual(code, 0)


class ServerLocation(Base):
    def test_server_location_without_url_or_remote_is_a_quiet_retry(self) -> None:
        sid = sess(40)
        self.add_session(sid)
        code, out = self.run_hook(
            {"session_id": sid, "hook_event_name": "SessionStart", "source": "startup"},
            location="server",
            env=self.env(),
        )
        self.assertEqual(code, 0)
        self.assertIn("No Operator letter yet", out)
        registry = OR.SessionRegistry(self.db)
        self.assertIsNone(registry.lookup("zcode", "server", "default", sid))


class ApiTransport(Base):
    """A hook installed --location server on a host off the registry, with the registry API
    URL in its environment:  the hook calls the API itself and the record lands attributed
    to the location it was installed under."""

    def setUp(self) -> None:
        super().setUp()
        import secrets

        self.operator_key = secrets.token_hex(24)
        self.service = RG.RegistryService(
            self.db, {"OPERATOR": self.operator_key}
        )
        self.server = API.ListenerReadApi(
            "127.0.0.1", 0, self.state, {}, registry=self.service
        )
        self.server.start()
        self.addCleanup(self.server.close)
        host, port = self.server.address
        self.api_env = self.env(
            SIMPLE_TOOLS_OPERATOR_REGISTRY_URL="http://%s:%d" % (host, port),
            SIMPLE_TOOLS_OPERATOR_REGISTRY_PRINCIPAL="OPERATOR",
            SIMPLE_TOOLS_OPERATOR_REGISTRY_KEY=self.operator_key,
        )

    def test_server_hook_registers_through_the_api(self) -> None:
        sid = sess(50)
        self.add_session(sid, title="Wake me over the API")
        code, out = self.run_hook(
            {"session_id": sid, "hook_event_name": "SessionStart", "source": "startup"},
            location="server",
            env=self.api_env,
        )
        self.assertEqual(code, 0)
        record = OR.SessionRegistry(self.db).lookup("zcode", "server", "default", sid)
        self.assertTrue(record, out)
        self.assertRegex(record["label"], PAIR)

    def test_api_unreachable_is_the_quiet_retry(self) -> None:
        import secrets

        sid = sess(51)
        self.add_session(sid)
        dead = self.api_env | {
            "SIMPLE_TOOLS_OPERATOR_REGISTRY_URL": "http://127.0.0.1:1",
        }
        code, out = self.run_hook(
            {"session_id": sid, "hook_event_name": "SessionStart", "source": "startup"},
            location="server",
            env=dead,
        )
        self.assertEqual(code, 0)
        self.assertIn("No Operator letter yet", out)


class SweepAgainstApi(Base):
    """The dropped-session diff against a real registry API:  a registration whose session is
    ACTIVE in the store is never closed (the false positive Kody's follow-up caught), and one
    whose row the store has dropped is closed by the diff."""

    def setUp(self) -> None:
        super().setUp()
        import secrets

        self.operator_key = secrets.token_hex(24)
        self.service = RG.RegistryService(self.db, {"OPERATOR": self.operator_key})
        self.server = API.ListenerReadApi(
            "127.0.0.1", 0, self.state, {}, registry=self.service
        )
        self.server.start()
        self.addCleanup(self.server.close)
        host, port = self.server.address
        self.api_env = self.env(
            SIMPLE_TOOLS_OPERATOR_REGISTRY_URL="http://%s:%d" % (host, port),
            SIMPLE_TOOLS_OPERATOR_REGISTRY_PRINCIPAL="OPERATOR",
            SIMPLE_TOOLS_OPERATOR_REGISTRY_KEY=self.operator_key,
        )

    def _register(self, sid: str) -> None:
        OR.register_session(
            self.db,
            platform="zcode",
            verified_host="server",
            profile_scope="default",
            native_session_id=sid,
            seat="ZC",
            source="sweep test",
            issuer=OR.ISSUER_OPERATOR,
        )

    def test_active_registration_is_not_closed_and_dropped_one_is(self) -> None:
        active, dropped, archived = sess(60), sess(61), sess(62)
        self.add_session(active, title="Still live")
        self.add_session(archived, title="Done")
        conn = sqlite3.connect(self.store)
        with conn:
            conn.execute("UPDATE session SET time_archived = 2 WHERE id = ?", (archived,))
        conn.close()
        self._register(active)
        self._register(dropped)
        self._register(archived)
        out = io.StringIO()
        code = Z.main(["sweep", "--apply", "--location", "server"], env=self.api_env, stdout=out)
        self.assertEqual(code, 0, out.getvalue())
        registry = OR.SessionRegistry(self.db)
        live = registry.lookup("zcode", "server", "default", active)
        self.assertTrue(live and live["label"])  # untouched: it still holds its letter
        self.assertEqual(registry.lookup("zcode", "server", "default", dropped)["state"], "closed")
        self.assertEqual(registry.lookup("zcode", "server", "default", archived)["state"], "closed")


class TitleWriter(Base):
    def test_rename_writes_generated_title(self) -> None:
        sid = sess(20)
        self.add_session(sid, title="Words the prompt gave")
        request = PL.TitleRequest(
            platform=Z.PLATFORM,
            session_id=sid,
            label="AA",
            title="AA: Words the prompt gave",
            words="Words the prompt gave",
            current="Words the prompt gave",
            reason="start",
            data={"_now": 1700000000},
            env=self.env(),
        )
        self.assertTrue(Z.zcode_rename(request))
        rows = dict((r[0], (r[1], r[2])) for r in self.rows())
        self.assertEqual(rows[sid], ("AA: Words the prompt gave", "generated"))

    def test_rename_never_writes_over_a_user_rename(self) -> None:
        sid = sess(21)
        self.add_session(sid, title="Jay renamed this", title_source="custom")
        request = PL.TitleRequest(
            platform=Z.PLATFORM,
            session_id=sid,
            label="AA",
            title="AA: Jay renamed this",
            words="Jay renamed this",
            current="Jay renamed this",
            reason="start",
            data={},
            env=self.env(),
        )
        self.assertTrue(Z.zcode_rename(request))
        rows = dict((r[0], (r[1], r[2])) for r in self.rows())
        self.assertEqual(rows[sid], ("Jay renamed this", "custom"))

    def test_rename_writes_over_custom_when_the_letter_is_already_there(self) -> None:
        sid = sess(22)
        self.add_session(sid, title="AA: Jay kept the letter", title_source="custom")
        request = PL.TitleRequest(
            platform=Z.PLATFORM,
            session_id=sid,
            label="AA",
            title="AA: Jay kept the letter",
            words="Jay kept the letter",
            current="AA: Jay kept the letter",
            reason="rename",
            data={},
            env=self.env(),
        )
        self.assertTrue(Z.zcode_rename(request))
        rows = dict((r[0], (r[1], r[2])) for r in self.rows())
        self.assertEqual(rows[sid], ("AA: Jay kept the letter", "generated"))


class Install(Base):
    def setUp(self) -> None:
        super().setUp()
        self.config = os.path.join(self.home, ".zcode", "cli", "config.json")
        with open(self.config, "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "hooks": {
                        "events": {
                            "PreToolUse": [
                                {
                                    "matcher": "Bash",
                                    "hooks": [
                                        {
                                            "type": "command",
                                            "command": "/opt/lane-tools/lane-guard-hook --format claude",
                                            "timeout": 5,
                                        }
                                    ],
                                }
                            ]
                        },
                        "enabled": True,
                    }
                },
                fh,
            )

    def install(self, *argv: str) -> tuple[int, str]:
        out = io.StringIO()
        code = PL.install_main(["--platform", "zcode", *argv], self.env(), out)
        return code, out.getvalue()

    def test_install_adds_entries_and_copies_wrapper(self) -> None:
        code, out = self.install("--location", "server")
        self.assertEqual(code, 0)
        answer = json.loads(out.splitlines()[-1])
        self.assertEqual(answer["changed"], ["SessionStart", "UserPromptSubmit", "Stop"])
        self.assertEqual(answer["wrapper"], os.path.join(self.home, ".local/bin/zc-letter/platform-letter"))
        self.assertTrue(os.path.isfile(answer["wrapper"]))
        doc = json.load(open(self.config))
        events = doc["hooks"]["events"]
        self.assertTrue(doc["hooks"]["enabled"])  # the user's flag survives
        names = []
        for entry in events["SessionStart"]:
            for hook in entry["hooks"]:
                words = hook["command"].split()
                self.assertEqual(os.path.basename(words[0]), "platform-letter")
                self.assertIn("--platform", words)
                self.assertIn("zcode", words)
                names.append(hook["command"])
        self.assertEqual(len(names), 1)
        # the other tool's hook keeps its matcher and its command
        self.assertEqual(events["PreToolUse"][0]["matcher"], "Bash")
        self.assertIn("lane-guard-hook", events["PreToolUse"][0]["hooks"][0]["command"])

    def test_install_is_idempotent_and_check_passes(self) -> None:
        self.install("--location", "server")
        code, out = self.install("--location", "server")
        answer = json.loads(out.splitlines()[-1])
        self.assertEqual(answer["changed"], [])
        code, out = self.install("--location", "server", "--check")
        self.assertEqual(code, 0)

    def test_remove_takes_only_ours(self) -> None:
        self.install("--location", "server")
        code, out = self.install("--remove")
        self.assertEqual(code, 0)
        doc = json.load(open(self.config))
        events = doc["hooks"]["events"]
        self.assertNotIn("SessionStart", events)
        self.assertNotIn("UserPromptSubmit", events)
        self.assertNotIn("Stop", events)
        self.assertIn("PreToolUse", events)
        self.assertTrue(os.path.isfile(os.path.join(self.home, ".local/bin/zc-letter/platform-letter")))


class Wrapper(Base):
    WRAPPER = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__)))),
        "zcode-letter-hook",
        "platform-letter",
    )

    def test_wrapper_exists_and_is_executable(self) -> None:
        self.assertTrue(os.path.isfile(self.WRAPPER))
        self.assertTrue(os.access(self.WRAPPER, os.X_OK))

    def test_wrapper_is_a_quiet_noop_without_a_checkout(self) -> None:
        result = subprocess.run(
            [self.WRAPPER, "hook", "--platform", "zcode"],
            env={"HOME": self.home, "PATH": "/usr/bin:/bin"},
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")

    def test_wrapper_sources_env_and_runs_the_real_cli(self) -> None:
        # A fake checkout carrying a platform-letter that prints what it was given, and an
        # env file with a marker export, prove both halves of the wrapper's one job.
        runtime = os.path.join(self.home, "apps", "lanes", "_managed", "fleet", "simple-tools-runtime", "scripts")
        os.makedirs(runtime)
        real = os.path.join(runtime, "platform-letter")
        seen = os.path.join(self.tmp.name, "seen.txt")
        body = (
            "#!/bin/sh\n"
            ': > "%s"\n' % seen
            + 'for w in "$@"; do echo "$w" >> "%s"; done\n' % seen
            + 'printenv REGISTRY_MARK >> "%s" 2>/dev/null\n' % seen
        )
        with open(real, "w", encoding="utf-8") as fh:
            fh.write(body)
        os.chmod(real, 0o755)
        envfile = os.path.join(self.home, ".local", "bin", "mm-registry-env")
        os.makedirs(os.path.dirname(envfile))
        with open(envfile, "w", encoding="utf-8") as fh:
            fh.write("export REGISTRY_MARK=sourced\n")
        result = subprocess.run(
            [self.WRAPPER, "hook", "--platform", "zcode", "--location", "server"],
            env={"HOME": self.home, "PATH": "/usr/bin:/bin"},
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        seen_words = open(seen).read().split()
        self.assertEqual(
            seen_words[:5], ["hook", "--platform", "zcode", "--location", "server"]
        )
        self.assertIn("sourced", seen_words)


class Sweep(Base):
    def test_sweep_reports_archived_and_deleted(self) -> None:
        keep, archived, deleted = sess(30), sess(31), sess(32)
        self.add_session(keep)
        self.add_session(archived, title="done")
        self.add_session(deleted, title="gone")
        conn = sqlite3.connect(self.store)
        with conn:
            conn.execute("UPDATE session SET time_archived = 2 WHERE id = ?", (archived,))
            conn.execute("UPDATE session SET time_deleted = 3 WHERE id = ?", (deleted,))
        conn.close()
        out = io.StringIO()
        code = Z.main(["sweep", "--location", "server"], env=self.env(), stdout=out)
        self.assertEqual(code, 0)
        answer = json.loads(out.getvalue())
        self.assertEqual(answer["platform"], "zcode")
        self.assertEqual(
            sorted(item["session_id"] for item in answer["closed"]),
            sorted([archived, deleted]),
        )
        self.assertTrue(all(item.get("would_close") for item in answer["closed"]))


if __name__ == "__main__":
    unittest.main()
