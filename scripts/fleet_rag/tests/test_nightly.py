"""Nightly job, mirror push, and install gates (AFC-687).  No network and no launchctl."""

from __future__ import annotations

import contextlib
import io
import json
import fcntl
import os
import stat
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from fleet_rag import mirror, mirror_install, nightly_job
from fleet_rag.mirror import MirrorError
from fleet_secrets.core import SecretsError

TOKEN = "st." + ("a" * 24)
SECRET = "ghp_" + ("b" * 36)
REPO = Path(__file__).resolve().parents[3]


LINEAR_PROJECT = "00000000-0000-4000-8000-00000000000a"
RECALL_PROJECT = "00000000-0000-4000-8000-00000000000b"


def _token(path: Path) -> None:
    path.write_text(TOKEN + "\n", encoding="utf-8")
    os.chmod(path, 0o600)


def _fetch(project, path, token, names):  # noqa: ANN001
    assert token == TOKEN
    assert project
    if path == "/":
        return {name: f"value-{name}" for name in names}
    if path == "/linear":
        return {name: f"value-{name}" for name in names}
    return {name: f"value-{name}" for name in names}


def _fresh_mirror(env: dict, root: Path) -> None:
    live = root / "current"
    live.mkdir(parents=True)
    (live / ".complete").write_text(
        json.dumps({"complete": True, "at": int(time.time()), "files": {}}) + "\n",
        encoding="utf-8",
    )
    env[mirror.MIRROR_ENV] = str(live)


def _ready(tmp: Path) -> dict:
    recall = tmp / "recall.token"
    linear = tmp / "linear.token"
    _token(recall)
    _token(linear)
    return {
        "FLEET_RAG_NIGHTLY": "1",
        "HOME": str(tmp),
        nightly_job.TOKEN_FILE_RECALL: str(recall),
        nightly_job.TOKEN_FILE_LINEAR: str(linear),
        nightly_job.LINEAR_PROJECT_ENV: LINEAR_PROJECT,
        "FLEET_RAG_NIGHTLY_LOCK": str(tmp / "nightly.lock"),
        "FLEET_RAG_NIGHTLY_SUMMARY": str(tmp / "last.json"),
        "ZULIP_SITE": "https://example.zulipchat.com",
        "ZULIP_EMAIL": "fleet-rag-nightly@example.com",
        "ZULIP_API_KEY": "not-a-real-key",
    }


class NightlyJobTests(unittest.TestCase):
    def test_disabled_does_not_fetch_sync_or_alert(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = _ready(Path(tmp))
            env["FLEET_RAG_NIGHTLY"] = "0"

            def boom(*args, **kwargs):  # noqa: ANN002, ANN003
                raise AssertionError("disabled job called a side effect")

            summary = nightly_job.execute(env, read_file=boom, fetch=boom, sync=boom, ingest_runner=boom, post=boom)
        self.assertFalse(summary["ran"])
        self.assertEqual(summary["alert"], "quiet")
        self.assertTrue(summary["ok"])

    def test_success_is_quiet_and_a_guard_trip_or_failure_alerts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = _ready(Path(tmp))
            _fresh_mirror(env, Path(tmp) / "mirror")
            before = os.environ.get("QDRANT_API_KEY")
            seen: dict[str, str] = {}

            def ingest_ok():
                seen["require"] = os.environ.get("FLEET_RAG_REQUIRE_CODE_ROOT", "")
                seen["qdrant"] = os.environ.get("QDRANT_API_KEY", "")
                return 0, {"ok": True, "errors": [], "guard_trips": [], "warnings": []}

            posts: list[dict] = []
            summary = nightly_job.execute(
                env,
                read_file=nightly_job.read_token_file,
                fetch=_fetch,
                sync=lambda: 0,
                ingest_runner=ingest_ok,
                post=lambda site, email, key, fields: posts.append(fields),
            )
        self.assertTrue(summary["ok"])
        self.assertEqual(summary["alert"], "quiet")
        self.assertEqual(posts, [])
        self.assertEqual(seen["require"], "1")
        self.assertEqual(seen["qdrant"], "value-QDRANT_API_KEY")
        self.assertNotIn("value-QDRANT_API_KEY", json.dumps(summary))
        self.assertEqual(os.environ.get("QDRANT_API_KEY"), before)

        def ingest_guard():
            return 0, {
                "ok": True,
                "errors": [],
                "guard_trips": [{"source": "skill", "reason": "stale"}],
                "warnings": [],
            }

        with tempfile.TemporaryDirectory() as tmp:
            posts.clear()
            env = _ready(Path(tmp))
            _fresh_mirror(env, Path(tmp) / "mirror")
            summary = nightly_job.execute(
                env,
                read_file=nightly_job.read_token_file,
                fetch=_fetch,
                sync=lambda: 0,
                ingest_runner=ingest_guard,
                post=lambda site, email, key, fields: posts.append(fields),
            )
        self.assertTrue(summary["ok"])
        self.assertEqual(summary["alert"], "posted")
        self.assertIn("prune guard", posts[0]["content"])
        self.assertIn("\u00a0 ", posts[0]["content"])
        self.assertEqual(posts[0]["to"], "OPS")

    def test_failure_alerts_and_withholds_a_secret(self) -> None:
        def ingest_bad():
            return 1, {"ok": False, "errors": [f"blew up {SECRET}"], "guard_trips": [], "warnings": []}

        posts: list[dict] = []
        with tempfile.TemporaryDirectory() as tmp:
            summary = nightly_job.execute(
                _ready(Path(tmp)),
                read_file=nightly_job.read_token_file,
                fetch=_fetch,
                sync=lambda: 0,
                ingest_runner=ingest_bad,
                post=lambda site, email, key, fields: posts.append(fields),
            )
        self.assertFalse(summary["ok"])
        self.assertEqual(summary["errors"], ["withheld"])
        self.assertNotIn(SECRET, json.dumps(summary))
        self.assertNotIn(SECRET, posts[0]["content"])
        self.assertIn("failed", posts[0]["content"])

    def test_unit_owned_sync_reads_the_result_instead_of_starting_it(self) -> None:
        seen: list[list[str]] = []
        now = 10_000.0

        def show(result="success", status="0", started_s=now - 60, active="inactive"):  # noqa: ANN001
            def run(argv):  # noqa: ANN001
                seen.append(argv)
                return (
                    f"ActiveState={active}\nResult={result}\nExecMainStatus={status}\n"
                    f"ExecMainStartTimestampMonotonic={int(started_s * 1_000_000)}\n"
                )
            return run

        def check(**kw):  # noqa: ANN003
            return nightly_job.unit_sync_result(show(**kw), now_monotonic=lambda: now)

        self.assertEqual(check(), 0)
        self.assertEqual(check(result="exit-code"), 1)
        self.assertEqual(check(status="1"), 1)
        # A success left over from an earlier run is not tonight's sync.
        self.assertEqual(check(started_s=now - nightly_job.SYNC_MAX_AGE_S - 1), 1)
        self.assertEqual(check(started_s=0), 1)
        self.assertEqual(check(active="activating"), nightly_job.SYNC_BUSY)
        self.assertEqual(
            nightly_job.unit_sync_result(lambda argv: "", now_monotonic=lambda: now),
            nightly_job.SYNC_UNKNOWN,
        )
        self.assertTrue(all("start" not in argv for argv in seen))
        self.assertEqual(seen[0][:3], ["systemctl", "show", "code-main-sync.service"])
        self.assertIn("ActiveState", seen[0][3])

    def test_unit_flag_routes_code_sync_to_the_result_read(self) -> None:
        with mock.patch.object(nightly_job, "unit_sync_result", return_value=0) as read, mock.patch.object(
            nightly_job.subprocess, "run", side_effect=AssertionError("started a unit")
        ):
            self.assertEqual(nightly_job.code_sync(None, {nightly_job.SYNC_BY_UNIT_ENV: "1"}), 0)
        read.assert_called_once()

    def _show(self, *, now: float, age_s: float = 60, active: str = "inactive", result: str = "success") -> str:
        started = int((now - age_s) * 1_000_000)
        return (
            f"ActiveState={active}\nResult={result}\nExecMainStatus=0\n"
            f"ExecMainStartTimestampMonotonic={started}\n"
        )

    def test_a_recent_timer_success_is_accepted_without_starting(self) -> None:
        """The Nov 11 failure: User=jay called systemctl start and was denied, after the timer had succeeded."""
        now = 50_000.0
        started: list[list[str]] = []

        def show(argv):  # noqa: ANN001
            self.assertEqual(argv[:3], ["systemctl", "show", "code-main-sync.service"])
            self.assertNotIn("start", argv)
            return self._show(now=now)

        code = nightly_job.code_sync(
            None,
            {},
            show=show,
            start=lambda argv: started.append(argv) or (1, "Access denied"),
            monotonic=lambda: now,
            euid=1000,
        )
        self.assertEqual(code, 0)
        self.assertEqual(started, [])

    def test_access_denied_rereads_and_keeps_a_fresh_success(self) -> None:
        now = 50_000.0
        calls = {"n": 0}

        def show(argv):  # noqa: ANN001
            calls["n"] += 1
            if calls["n"] == 1:
                return self._show(now=now, age_s=nightly_job.SYNC_MAX_AGE_S + 10)
            return self._show(now=now, age_s=30)

        code = nightly_job.code_sync(
            None,
            {nightly_job.SYNC_START_ENV: "1"},
            show=show,
            start=lambda argv: (1, "Failed to start code-main-sync.service: Interactive authentication required.\n"),
            monotonic=lambda: now,
            euid=1000,
        )
        self.assertEqual(code, 0)
        self.assertEqual(calls["n"], 2)

    def test_unprivileged_stale_sync_skips_ingest_and_does_not_start(self) -> None:
        now = 50_000.0
        started: list[list[str]] = []

        def show(argv):  # noqa: ANN001
            return self._show(now=now, age_s=nightly_job.SYNC_MAX_AGE_S + 5)

        code = nightly_job.code_sync(
            None,
            {},
            show=show,
            start=lambda argv: started.append(argv) or (0, ""),
            monotonic=lambda: now,
            euid=1000,
        )
        self.assertEqual(code, 1)
        self.assertEqual(started, [])

        called: list[int] = []

        def ingest_runner():
            called.append(1)
            return 0, {"ok": True}

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            nightly_job, "unit_sync_result", return_value=1
        ), mock.patch.object(
            nightly_job, "invoke_start", side_effect=AssertionError("started a unit")
        ), mock.patch.object(nightly_job.os, "geteuid", return_value=1000):
            summary = nightly_job.execute(
                _ready(Path(tmp)),
                read_file=nightly_job.read_token_file,
                fetch=_fetch,
                sync=None,
                ingest_runner=ingest_runner,
                post=lambda *args: None,
            )
        self.assertEqual(called, [])
        self.assertFalse(summary["ok"])
        self.assertEqual(summary["errors"], ["code-main-sync-exit-1"])
        self.assertEqual(summary["code_sync_exit"], 1)

    def test_access_denied_with_no_fresh_run_exits_1_before_ingest(self) -> None:
        called: list[int] = []
        posts: list[dict] = []

        def ingest_runner():
            called.append(1)
            return 0, {"ok": True}

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            nightly_job, "unit_sync_result", return_value=1
        ), mock.patch.object(
            nightly_job, "invoke_start", return_value=(1, "Failed to start code-main-sync.service: Access denied\n")
        ), mock.patch.object(nightly_job.os, "geteuid", return_value=0):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                summary = nightly_job.execute(
                    _ready(Path(tmp)),
                    read_file=nightly_job.read_token_file,
                    fetch=_fetch,
                    sync=None,
                    ingest_runner=ingest_runner,
                    post=lambda site, email, key, fields: posts.append(fields),
                )
        self.assertEqual(called, [])
        self.assertEqual(summary["errors"], ["code-main-sync-exit-1"])
        self.assertEqual(summary["code_sync_exit"], 1)
        self.assertIn("start denied", err.getvalue())
        self.assertIn("code-main-sync exited 1", posts[0]["content"])
        self.assertEqual(summary["alert"], "posted")

    def test_a_running_unit_is_polled_and_then_accepted(self) -> None:
        clock = {"t": 80_000.0}
        calls = {"n": 0}

        def show(argv):  # noqa: ANN001
            calls["n"] += 1
            if calls["n"] == 1:
                return self._show(now=clock["t"], active="activating")
            return self._show(now=clock["t"], age_s=15)

        code = nightly_job.code_sync(
            None,
            {},
            show=show,
            start=lambda argv: (_ for _ in ()).throw(AssertionError("started a unit")),
            sleep=lambda seconds: clock.__setitem__("t", clock["t"] + seconds),
            monotonic=lambda: clock["t"],
            euid=1000,
        )
        self.assertEqual(code, 0)
        self.assertEqual(calls["n"], 2)

    def test_root_owned_stamp_covers_an_unanswered_show_only(self) -> None:
        now = 1_000_000.0
        fresh = {"ok": True, "exit": 0, "at": now - 20}
        self.assertTrue(
            nightly_job.stamp_is_fresh(fresh, now, mode=0o644, uid=0, euid=1000, symlink=False)
        )
        self.assertFalse(
            nightly_job.stamp_is_fresh(fresh, now, mode=0o664, uid=0, euid=1000, symlink=False)
        )
        self.assertFalse(
            nightly_job.stamp_is_fresh(fresh, now, mode=0o644, uid=1000, euid=1000, symlink=False)
        )
        self.assertFalse(
            nightly_job.stamp_is_fresh(fresh, now, mode=0o644, uid=0, euid=1000, symlink=True)
        )
        stale = {"ok": True, "exit": 0, "at": now - nightly_job.SYNC_MAX_AGE_S - 1}
        self.assertFalse(
            nightly_job.stamp_is_fresh(stale, now, mode=0o644, uid=0, euid=1000, symlink=False)
        )
        failed = {"ok": False, "exit": 1, "at": now - 20}
        self.assertFalse(
            nightly_job.stamp_is_fresh(failed, now, mode=0o644, uid=0, euid=1000, symlink=False)
        )

        def show(argv):  # noqa: ANN001
            return ""

        with mock.patch.object(nightly_job, "state_sync_result", return_value=0) as stamp:
            code = nightly_job.code_sync(
                None,
                {nightly_job.SYNC_STATE_ENV: "/var/lib/fleet/code-main-sync.json"},
                show=show,
                start=lambda argv: (_ for _ in ()).throw(AssertionError("started a unit")),
                monotonic=lambda: now,
                euid=1000,
            )
        self.assertEqual(code, 0)
        stamp.assert_called()

        def failed_show(argv):  # noqa: ANN001
            return self._show(now=now, result="exit-code")

        with mock.patch.object(nightly_job, "state_sync_result", return_value=0):
            code = nightly_job.code_sync(
                None,
                {},
                show=failed_show,
                start=lambda argv: (_ for _ in ()).throw(AssertionError("started a unit")),
                monotonic=lambda: now,
                euid=1000,
            )
        self.assertEqual(code, 1)

    def test_service_unit_owns_the_code_sync_dependency(self) -> None:
        unit = (REPO / "scripts" / "systemd" / "fleet-rag-nightly.service").read_text(encoding="utf-8")
        self.assertIn("Wants=code-main-sync.service", unit)
        self.assertRegex(unit, r"(?m)^After=.*code-main-sync\.service")
        self.assertIn(f"Environment={nightly_job.SYNC_BY_UNIT_ENV}=1", unit)

    def test_code_sync_failure_does_not_ingest(self) -> None:
        called = []

        def ingest_runner():
            called.append(1)
            return 0, {"ok": True}

        posts: list[dict] = []
        with tempfile.TemporaryDirectory() as tmp:
            summary = nightly_job.execute(
                _ready(Path(tmp)),
                read_file=nightly_job.read_token_file,
                fetch=_fetch,
                sync=lambda: 3,
                ingest_runner=ingest_runner,
                post=lambda site, email, key, fields: posts.append(fields),
            )
        self.assertEqual(called, [])
        self.assertFalse(summary["ok"])
        self.assertEqual(summary["code_sync_exit"], 3)
        self.assertEqual(summary["alert"], "posted")
        self.assertIn("code-main-sync exited 3", posts[0]["content"])

    def test_each_fetch_uses_its_own_project(self) -> None:
        fetched: list[tuple] = []

        def fetch(project, path, token, names):  # noqa: ANN001
            fetched.append((project, path))
            return {name: f"value-{name}" for name in names}

        with tempfile.TemporaryDirectory() as tmp:
            env = _ready(Path(tmp))
            env[nightly_job.RECALL_PROJECT_ENV] = RECALL_PROJECT
            nightly_job.load_credentials(env, read_file=nightly_job.read_token_file, fetch=fetch)
        self.assertEqual(fetched, [(RECALL_PROJECT, "/"), (LINEAR_PROJECT, "/linear")])

    def test_linear_project_falls_back_to_the_coordinator_file_then_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = _ready(Path(tmp))
            del env[nightly_job.LINEAR_PROJECT_ENV]
            env.pop("INFISICAL_AI_FLEET_COORDINATOR_PROJECT_ID", None)
            with self.assertRaises(nightly_job.NightlyError) as ctx:
                nightly_job.linear_project_id(env)
            self.assertEqual(ctx.exception.reason, "linear-project-unset")
            sources = Path(tmp) / ".simple-tools"
            sources.mkdir()
            (sources / "ai-fleet-coordinator-project-id").write_text(LINEAR_PROJECT + "\n")
            self.assertEqual(nightly_job.linear_project_id(env), LINEAR_PROJECT)

    def test_machine_identity_refuses_before_the_recall_fetch(self) -> None:
        fetched: list[tuple] = []

        def fetch(project, path, token, names):  # noqa: ANN001
            fetched.append((path, names))
            return {}

        with tempfile.TemporaryDirectory() as tmp:
            env = _ready(Path(tmp))
            env["INFISICAL_CLIENT_SECRET"] = TOKEN
            summary = nightly_job.execute(
                env,
                read_file=nightly_job.read_token_file,
                fetch=fetch,
                sync=lambda: 0,
                ingest_runner=lambda: (_ for _ in ()).throw(AssertionError("ingested")),
                post=lambda *args: None,
            )
        self.assertEqual(fetched, [])
        self.assertFalse(summary["ok"])
        self.assertEqual(summary["errors"], ["machine-identity:INFISICAL_CLIENT_SECRET"])
        self.assertNotIn(TOKEN, json.dumps(summary))

    def test_the_recall_file_defaults_to_the_server_path_and_reads_the_assignment(self) -> None:
        self.assertEqual(
            nightly_job.recall_token_path({}),
            Path("/home/jay/.secrets/fleet-recall-infisical.env"),
        )
        self.assertEqual(
            nightly_job.recall_token_path({nightly_job.TOKEN_FILE_RECALL: "/tmp/other.env"}),
            Path("/tmp/other.env"),
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = _ready(root)
            recall = root / "fleet-recall-infisical.env"
            recall.write_text(
                f'export {nightly_job.RECALL_TOKEN_KEY}="{TOKEN}"\n',
                encoding="utf-8",
            )
            os.chmod(recall, 0o600)
            env[nightly_job.TOKEN_FILE_RECALL] = str(recall)
            summary = nightly_job.execute(
                env,
                read_file=nightly_job.read_token_file,
                fetch=_fetch,
                sync=lambda: 0,
                ingest_runner=lambda: (0, {"ok": True, "errors": [], "guard_trips": [], "warnings": []}),
                post=lambda *args: None,
            )
        self.assertTrue(summary["ok"])
        self.assertEqual(summary["errors"], [])

    def test_token_file_mode_and_symlink_are_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = _ready(root)
            os.chmod(env[nightly_job.TOKEN_FILE_RECALL], 0o644)
            summary = nightly_job.execute(
                env,
                read_file=nightly_job.read_token_file,
                fetch=_fetch,
                sync=lambda: 0,
                ingest_runner=lambda: (_ for _ in ()).throw(AssertionError("ingested")),
                post=lambda *args: None,
            )
        self.assertEqual(summary["errors"], ["token-file-mode"])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = _ready(root)
            link = root / "link.token"
            link.symlink_to(env[nightly_job.TOKEN_FILE_RECALL])
            env[nightly_job.TOKEN_FILE_RECALL] = str(link)
            summary = nightly_job.execute(
                env,
                read_file=nightly_job.read_token_file,
                fetch=_fetch,
                sync=lambda: 0,
                ingest_runner=lambda: (_ for _ in ()).throw(AssertionError("ingested")),
                post=lambda *args: None,
            )
        self.assertEqual(summary["errors"], ["token-file"])

    def test_a_missing_linear_pair_refuses(self) -> None:
        def fetch(project, path, token, names):  # noqa: ANN001
            if path == "/":
                return {name: "x" for name in names}
            return {}

        with tempfile.TemporaryDirectory() as tmp:
            summary = nightly_job.execute(
                _ready(Path(tmp)),
                read_file=nightly_job.read_token_file,
                fetch=fetch,
                sync=lambda: 0,
                ingest_runner=lambda: (_ for _ in ()).throw(AssertionError("ingested")),
                post=lambda *args: None,
            )
        self.assertEqual(summary["errors"], ["missing:linear-seat-pair"])

    def test_an_infisical_error_that_looks_like_a_secret_is_withheld(self) -> None:
        def fetch(project, path, token, names):  # noqa: ANN001
            raise SecretsError(f"denied {SECRET}")

        with tempfile.TemporaryDirectory() as tmp:
            summary = nightly_job.execute(
                _ready(Path(tmp)),
                read_file=nightly_job.read_token_file,
                fetch=fetch,
                sync=lambda: 0,
                ingest_runner=lambda: (0, {}),
                post=lambda *args: None,
            )
        self.assertEqual(summary["errors"], ["withheld"])
        self.assertNotIn(SECRET, json.dumps(summary))

    def test_lock_busy_is_quiet(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = _ready(Path(tmp))
            lock = Path(env["FLEET_RAG_NIGHTLY_LOCK"])
            lock.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                summary = nightly_job.execute(
                    env,
                    fetch=lambda *a: (_ for _ in ()).throw(AssertionError("fetched")),
                    sync=lambda: (_ for _ in ()).throw(AssertionError("synced")),
                    ingest_runner=lambda: (_ for _ in ()).throw(AssertionError("ingested")),
                )
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)
        self.assertEqual(summary["warnings"], ["lock-busy"])
        self.assertFalse(summary["ran"])
        self.assertEqual(summary["alert"], "quiet")

    def test_an_unconfigured_mirror_is_a_guard_and_is_not_a_live_directory(self) -> None:
        seen = {}

        def ingest_runner():
            seen["mirror"] = os.environ.get("FLEET_RAG_MIRROR")
            return 0, {"ok": True, "errors": [], "guard_trips": [], "warnings": []}

        with tempfile.TemporaryDirectory() as tmp:
            summary = nightly_job.execute(
                _ready(Path(tmp)),
                read_file=nightly_job.read_token_file,
                fetch=_fetch,
                sync=lambda: 0,
                ingest_runner=ingest_runner,
                post=lambda *args: None,
            )
        self.assertTrue(str(seen["mirror"]).endswith("mirror-unconfigured"))
        self.assertFalse(Path(seen["mirror"]).exists())
        self.assertEqual(summary["guard_trips"], [{"source": "mirror", "reason": "unconfigured"}])
        self.assertEqual(summary["alert"], "posted")

    def test_http_zulip_site_is_refused(self) -> None:
        with self.assertRaises(nightly_job.NightlyError):
            nightly_job.zulip_post("http://example.com", "a@example.com", "key", {})

    def test_without_zulip_credentials_alert_is_unconfigured(self) -> None:
        posts: list[dict] = []

        def ingest_bad():
            return 1, {"ok": False, "errors": ["nope"], "guard_trips": [], "warnings": []}

        with tempfile.TemporaryDirectory() as tmp:
            env = _ready(Path(tmp))
            _fresh_mirror(env, Path(tmp) / "mirror")
            store = Path(env["FLEET_RAG_NIGHTLY_SUMMARY"])
            store.write_text(
                json.dumps({"alert_fingerprint": "ok=0|errors:nope", "alert_streak": 2}) + "\n",
                encoding="utf-8",
            )
            for key in ("ZULIP_SITE", "ZULIP_EMAIL", "ZULIP_API_KEY"):
                env.pop(key, None)
            summary = nightly_job.execute(
                env,
                read_file=nightly_job.read_token_file,
                fetch=_fetch,
                sync=lambda: 0,
                ingest_runner=ingest_bad,
                post=lambda site, email, key, fields: posts.append(fields),
            )
        self.assertEqual(summary["alert"], "unconfigured")
        self.assertEqual(posts, [])
        self.assertEqual(summary.get("alert_streak"), 2)
        self.assertEqual(summary.get("alert_fingerprint"), "ok=0|errors:nope")

    def test_zulip_credentials_load_from_infisical(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            zulip_token = root / "zulip.token"
            _token(zulip_token)

            def fetch(project, path, token, names):  # noqa: ANN001
                if path == "/fleet-rag-nightly":
                    return {
                        "ZULIP_SITE": "https://example.zulipchat.com",
                        "ZULIP_EMAIL": "rag-bot@example.com",
                        "ZULIP_API_KEY": "not-a-real-key",
                    }
                return {name: f"value-{name}" for name in names}

            env = _ready(root)
            for key in ("ZULIP_SITE", "ZULIP_EMAIL", "ZULIP_API_KEY"):
                env.pop(key, None)
            env[nightly_job.TOKEN_FILE_ZULIP] = str(zulip_token)
            env["FLEET_RAG_ZULIP_INFISICAL_PROJECT"] = RECALL_PROJECT
            env["FLEET_RAG_ZULIP_INFISICAL_PATH"] = "/fleet-rag-nightly"
            loaded = nightly_job.load_credentials(
                env, read_file=nightly_job.read_token_file, fetch=fetch
            )
        self.assertEqual(loaded["ZULIP_EMAIL"], "rag-bot@example.com")

    def test_the_same_failure_on_consecutive_nights_posts_still_failing(self) -> None:
        posts: list[dict] = []

        def ingest_bad():
            return 1, {"ok": False, "errors": ["nope"], "guard_trips": [], "warnings": []}

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = _ready(root)
            _fresh_mirror(env, root / "mirror")
            fingerprint = "ok=0|errors:nope"
            store = Path(env["FLEET_RAG_NIGHTLY_SUMMARY"])
            store.parent.mkdir(parents=True, exist_ok=True)
            store.write_text(
                json.dumps(
                    {
                        "ok": False,
                        "alert_fingerprint": fingerprint,
                        "alert_streak": 1,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            summary = nightly_job.execute(
                env,
                read_file=nightly_job.read_token_file,
                fetch=_fetch,
                sync=lambda: 0,
                ingest_runner=ingest_bad,
                post=lambda site, email, key, fields: posts.append(fields),
            )
        self.assertEqual(summary["alert_streak"], 2)
        self.assertIn("still failing, night 2", posts[0]["content"])

    def test_a_failed_alert_post_does_not_change_the_job_exit(self) -> None:
        def ingest_bad():
            return 1, {"ok": False, "errors": ["nope"], "guard_trips": [], "warnings": []}

        def boom(*args, **kwargs):  # noqa: ANN002, ANN003
            raise RuntimeError("zulip down")

        with tempfile.TemporaryDirectory() as tmp:
            env = _ready(Path(tmp))
            summary = nightly_job.execute(
                env,
                read_file=nightly_job.read_token_file,
                fetch=_fetch,
                sync=lambda: 0,
                ingest_runner=ingest_bad,
                post=boom,
            )
            self.assertEqual(summary["alert"], "failed")
            self.assertFalse(summary["ok"])
            with mock.patch.dict(os.environ, {**env, "FLEET_RAG_NIGHTLY": "1"}, clear=True):
                with mock.patch.object(nightly_job, "execute", return_value=summary), mock.patch.object(
                    nightly_job, "write_summary"
                ):
                    self.assertEqual(nightly_job.main(), 1)

    def test_a_stale_mirror_alerts_before_ingest_guard_trips_merge(self) -> None:
        posts: list[dict] = []
        now = int(time.time())

        def ingest_runner():
            return 0, {"ok": True, "errors": [], "guard_trips": [], "warnings": []}

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = _ready(root)
            live = root / "mirror" / "current"
            live.mkdir(parents=True)
            mirror.write_stamp(live, now - 40 * 3600, {})
            env[mirror.MIRROR_ENV] = str(live)
            summary = nightly_job.execute(
                env,
                read_file=nightly_job.read_token_file,
                fetch=_fetch,
                sync=lambda: 0,
                ingest_runner=ingest_runner,
                post=lambda site, email, key, fields: posts.append(fields),
            )
        self.assertTrue(summary["ok"])
        self.assertEqual(summary["alert"], "posted")
        self.assertIn("mirror is stale", posts[0]["content"])

    def test_units_ship_disabled(self) -> None:
        service = (REPO / "scripts/systemd/fleet-rag-nightly.service").read_text(encoding="utf-8")
        timer = (REPO / "scripts/systemd/fleet-rag-nightly.timer").read_text(encoding="utf-8")
        example = (REPO / "scripts/systemd/fleet-rag-nightly.env.example").read_text(encoding="utf-8")
        plist = (REPO / "scripts/launchd/com.jay.fleet-rag-mirror.plist").read_text(encoding="utf-8")
        needle = "/mnt/" + "fleet-extra"
        self.assertNotIn("[Install]", service)
        self.assertIn("ConditionPathExists=/etc/fleet/rag-nightly.env", service)
        self.assertIn("FLEET_RAG_REQUIRE_CODE_ROOT=1", service)
        self.assertIn("TimeoutStartSec=4h", service)
        self.assertNotIn(needle, service)
        self.assertIn("02:30:00 America/Chicago", timer)
        self.assertIn("Persistent=true", timer)
        self.assertIn("FLEET_RAG_NIGHTLY=0", example)
        self.assertIn("/home/jay/.secrets/fleet-recall-infisical.env", example)
        self.assertNotIn("systemctl enable", example)
        self.assertNotIn(needle, example)
        self.assertNotIn(needle, plist)
        self.assertIn("<key>RunAtLoad</key>\n  <false/>", plist)
        self.assertIn("@PYTHON@", plist)
        self.assertEqual(
            nightly_job.INGEST_ARGV,
            ["--source", "linear,doc,skill,memory", "--prune", "--no-heartbeat"],
        )


class MirrorTests(unittest.TestCase):
    def test_delete_goes_to_incoming_only(self) -> None:
        argv = mirror.rsync_argv(Path("/tmp/skills"), "host:/srv/incoming/skills/claude/", delete=True)
        self.assertIn("--delete", argv)
        self.assertNotIn("-L", argv)
        self.assertTrue(argv[-1].startswith("host:/srv/incoming/"))
        kept = mirror.rsync_argv(Path("/tmp/skills"), "host:/srv/incoming/skills/claude/", delete=False)
        self.assertNotIn("--delete", kept)

    def test_rsync_creates_the_nested_incoming_tree(self) -> None:
        argv = mirror.rsync_argv(Path("/tmp/skills"), "host:/srv/incoming/skills/claude/", delete=True)
        self.assertIn("--mkpath", argv)
        rsync = shutil.which("rsync")
        if not rsync:
            self.skipTest("rsync not installed")
        probe = subprocess.run([rsync, "--help"], capture_output=True, text=True, check=False)
        if "--mkpath" not in probe.stdout:
            self.skipTest("this rsync has no --mkpath")
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src"
            src.mkdir()
            (src / "SKILL.md").write_text("x\n", encoding="utf-8")
            incoming = Path(tmp) / "incoming"
            incoming.mkdir()
            dest = str(incoming / "skills" / "claude") + "/"
            argv = mirror.rsync_argv(src, dest, delete=True)
            argv[0] = rsync
            proc = subprocess.run(argv, capture_output=True, text=True, check=False)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertTrue((incoming / "skills" / "claude" / "SKILL.md").is_file())

    def test_a_failed_rsync_does_not_promote(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / ".claude" / "skills").mkdir(parents=True)
            calls: list[list[str]] = []

            def run(argv, text):  # noqa: ANN001
                calls.append(argv)
                if argv[0] == "rsync":
                    return 1
                return 0

            result = mirror.push(
                home=home,
                ssh_target="server",
                remote_root="/srv/mirror",
                remote_python="/usr/bin/python3",
                remote_script="/srv/repo/scripts/fleet_rag/mirror.py",
                run=run,
            )
        self.assertFalse(result.promoted)
        self.assertTrue(result.reason.startswith("rsync-failed"))
        self.assertFalse(any("promote" in part for argv in calls for part in argv))

    def test_a_missing_local_tree_is_preserved_not_deleted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            calls: list[str] = []

            def run(argv, text):  # noqa: ANN001
                calls.append(argv[-1])
                return 0

            result = mirror.push(
                home=Path(tmp),
                ssh_target="server",
                remote_root="/srv/mirror",
                remote_python="/usr/bin/python3",
                remote_script="/srv/repo/scripts/fleet_rag/mirror.py",
                run=run,
            )
        self.assertTrue(result.promoted)
        preserves = [line for line in calls if "cp -a" in line]
        self.assertGreaterEqual(len(preserves), 5)
        for line in preserves:
            self.assertIn("/incoming/", line)
            self.assertNotIn("rm -rf -- '/srv/mirror/current", line)

    def test_count_mismatch_does_not_swap_and_a_match_does(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            live = root / "current"
            incoming = root / "incoming"
            (live / "skills").mkdir(parents=True)
            (live / "skills" / "keep.txt").write_text("old", encoding="utf-8")
            (incoming / "skills").mkdir(parents=True)
            (incoming / "skills" / "new.txt").write_text("new", encoding="utf-8")
            with self.assertRaises(MirrorError):
                mirror.promote(incoming, live, {"skills": 5}, time.time())
            self.assertEqual((live / "skills" / "keep.txt").read_text(encoding="utf-8"), "old")
            self.assertTrue((incoming / "skills" / "new.txt").is_file())
            counts = mirror.promote(incoming, live, {"skills": 1}, time.time())
            self.assertEqual(counts, {"skills": 1})
            self.assertEqual((live / "skills" / "new.txt").read_text(encoding="utf-8"), "new")
            self.assertFalse(incoming.exists())
            self.assertFalse((root / "current.previous").exists())
            stamp = json.loads((live / ".complete").read_text(encoding="utf-8"))
            self.assertTrue(stamp["complete"])

    def test_a_disabled_push_does_not_run_a_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "mirror.json"
            cfg.write_text(
                json.dumps(
                    {
                        "enabled": False,
                        "ssh_target": "server",
                        "remote_root": "/srv/mirror",
                        "remote_script": "/srv/repo/scripts/fleet_rag/mirror.py",
                    }
                ),
                encoding="utf-8",
            )
            os.chmod(cfg, 0o600)
            with mock.patch.object(mirror.subprocess, "run", side_effect=AssertionError("ran")):
                self.assertEqual(mirror._cmd_push(cfg), 0)

    def test_status_reasons(self) -> None:
        now = 1_700_000_000
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(mirror.mirror_status(root / "missing", now, 36 * 3600).reason, "missing")
            self.assertEqual(mirror.mirror_status(root, now, 36 * 3600).reason, "missing-stamp")
            (root / ".complete").write_text("{", encoding="utf-8")
            self.assertEqual(mirror.mirror_status(root, now, 36 * 3600).reason, "bad-stamp")
            mirror.write_stamp(root, now - 40 * 3600, {})
            self.assertEqual(mirror.mirror_status(root, now, 36 * 3600).reason, "stale")
            mirror.write_stamp(root, now - 3600, {})
            status = mirror.mirror_status(root, now, 36 * 3600)
            self.assertTrue(status.fresh)
            self.assertEqual(status.reason, "fresh")


class InstallTests(unittest.TestCase):
    def test_apply_without_enable_does_not_bootstrap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            home.mkdir()

            def boom(path):  # noqa: ANN001
                raise AssertionError("bootstrapped")

            result = mirror_install.apply(
                home=home,
                ssh_target="server",
                remote_root="/srv/mirror",
                remote_script="/srv/repo/scripts/fleet_rag/mirror.py",
                enable=False,
                python=Path("/opt/homebrew/bin/python3"),
                config_path=root / "mirror.json",
                plist_path=root / "agent.plist",
                log_path=home / "Library" / "Logs" / "fleet-rag-mirror.log",
                bootstrap=boom,
            )
            payload = json.loads((root / "mirror.json").read_text(encoding="utf-8"))
            plist = (root / "agent.plist").read_text(encoding="utf-8")
            mode = stat.S_IMODE((root / "mirror.json").stat().st_mode)
        self.assertFalse(payload["enabled"])
        self.assertFalse(result["bootstrapped"])
        self.assertEqual(mode, 0o600)
        self.assertIn("/opt/homebrew/bin/python3", plist)
        self.assertNotIn("@PYTHON@", plist)
        self.assertIn("<false/>", plist)
        self.assertNotIn("launchctl", plist)
        self.assertNotIn("st.", json.dumps(payload))

    def test_enable_bootstraps_and_a_bad_target_does_not(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            home.mkdir()
            called: list[Path] = []
            result = mirror_install.apply(
                home=home,
                ssh_target="server",
                remote_root="/srv/mirror",
                remote_script="/srv/repo/scripts/fleet_rag/mirror.py",
                enable=True,
                config_path=root / "mirror.json",
                plist_path=root / "agent.plist",
                bootstrap=lambda path: called.append(path) or 0,
            )
            self.assertTrue(result["bootstrapped"])
            self.assertEqual(called, [root / "agent.plist"])
            self.assertTrue(json.loads((root / "mirror.json").read_text(encoding="utf-8"))["enabled"])
            with self.assertRaises(MirrorError):
                mirror_install.apply(
                    home=home,
                    ssh_target="server;rm",
                    remote_root="/srv/mirror",
                    remote_script="/srv/repo/scripts/fleet_rag/mirror.py",
                    bootstrap=lambda path: (_ for _ in ()).throw(AssertionError("bootstrapped")),
                )


if __name__ == "__main__":
    unittest.main()
