"""Tests for fleet_rag.doctor: the platform parity table and the contribution digest.

cd scripts && python3 -m unittest fleet_rag.tests.test_doctor -v
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import io
import json
import os
import pathlib
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

from fleet_rag import doctor, recall_api
from fleet_rag.core import FleetRagError, build_point
from fleet_rag.recall_api import FakeQdrant
from fleet_rag.tests._hermetic import HermeticCredentialsMixin

CLI = pathlib.Path(__file__).resolve().parents[2] / "recall"
INSTALLER = pathlib.Path(__file__).resolve().parents[2] / "install-fleet-rag.sh"
SEAMS = (
    "load_config",
    "embed",
    "embedder_healthy",
    "rerank_healthy",
    "Qdrant",
    "rerank",
    "gitleaks_flagged",
    "gitleaks_available",
)
NOW = 1_788_400_000_000
HOUR = 3_600_000
DAY = 86_400_000


def load_cli():
    loader = importlib.machinery.SourceFileLoader("recall_cli_doctor_test", str(CLI))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def write(path: pathlib.Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def healthy_home(
    root: pathlib.Path, with_hooks: bool = True, last_run_ok: bool = True, last_run_age_h: float = 5
) -> pathlib.Path:
    home = root / "home"
    server = {"mcpServers": {"fleet-recall": {"command": "python3", "args": ["x"]}}}
    write(home / ".claude.json", json.dumps(server))
    write(home / ".cursor" / "mcp.json", json.dumps(server))
    write(home / ".gemini" / "config" / "mcp_config.json", json.dumps(server))
    toml = '[mcp_servers.other]\ncommand = "x"\n\n[mcp_servers.fleet-recall]\ncommand = "python3"\n'
    write(home / ".codex" / "config.toml", toml)
    write(home / ".grok" / "config.toml", toml + "enabled = true\n")
    write(home / "apps" / "grok-acp-runtime" / "acp-home-config.toml", toml)
    for plat in (".claude", ".cursor", ".codex"):
        write(home / plat / "skills" / "fleet-recall" / "SKILL.md", "# skill\n")
    if with_hooks:
        write(home / ".claude" / "hooks" / "fleet-recall-session-start.sh", "#!/bin/sh\n")
        write(home / ".claude" / "hooks" / "fleet-recall-stop.py", "# hook\n")
        write(
            home / ".claude" / "settings.json",
            json.dumps(
                {
                    "hooks": {
                        "SessionStart": [
                            {"hooks": [{"type": "command", "command": "other.sh"}]},
                            {
                                "matcher": "startup|resume",
                                "hooks": [
                                    {
                                        "type": "command",
                                        "command": str(
                                            home / ".claude/hooks/fleet-recall-session-start.sh"
                                        ),
                                    }
                                ],
                            },
                        ],
                        "Stop": [
                            {
                                "hooks": [
                                    {
                                        "type": "command",
                                        "command": f"python3 {home}/.claude/hooks/fleet-recall-stop.py",
                                    }
                                ]
                            }
                        ],
                    }
                }
            ),
        )
    write(
        home / "apps" / "fleet-rag" / "state" / "last-run.json",
        json.dumps(
            {"ok": last_run_ok, "finished_at": NOW - int(last_run_age_h * HOUR), "run_id": "r1"}
        ),
    )
    return home


def fake_http(
    seat_routes=("/recall/stats", "/recall/search", "/recall/contribute"),
    routines=("Fleet RAG nightly ingest", "Fleet RAG weekly health + recall eval"),
    disabled=(),
    fail_urls=(),
):
    calls = []

    def get(url):
        calls.append(url)
        if url in fail_urls:
            raise ConnectionRefusedError("refused")
        if url == doctor.SEAT_MCP_HEALTH:
            return {"ok": True, "name": "seat-mcp", "recall": list(seat_routes)}
        if url == doctor.BOTFLEET_ROUTINES:
            return {
                "routines": [
                    {"id": str(i), "name": n, "enabled": n not in disabled}
                    for i, n in enumerate(routines)
                ],
                "runs": [],
            }
        raise AssertionError(f"unexpected url {url}")

    get.calls = calls
    return get


class SentinelQdrant:
    def __init__(self, age_h: float | None = 3, ok: bool = True):
        self.age_h, self.ok = age_h, ok

    def scroll(self, flt=None, limit=256, **_):
        if self.age_h is None:
            return iter(())
        return iter(
            [
                {
                    "id": "s",
                    "payload": {
                        "doc_id": "meta/ingest-status",
                        "updated_at": NOW - int(self.age_h * HOUR),
                        "ok": self.ok,
                    },
                }
            ]
        )


class BoomQdrant:
    def scroll(self, *a, **k):
        raise FleetRagError("HTTP 401 from host with a body that must not be echoed")


def by_check(rep: dict) -> dict[str, dict]:
    return {r["check"]: r for r in rep["rows"]}


class PlatformsReportTests(HermeticCredentialsMixin, unittest.TestCase):
    """Most of these call doctor.platforms_report() without a `rerank_check=` override, which
    falls back to doctor.default_rerank_check() -> recall_api.get_config() -> a REAL Infisical
    login using whatever this Mac's real fleet-secrets can supply.  On a
    CI runner (no fleet-secrets) that silently no-ops; on an owner's Mac it reads real credentials
    and can make a real, slow network call.  make_hermetic() (see _hermetic.py) closes that off
    regardless of what's actually on disk here.
    """

    def setUp(self):
        self.make_hermetic()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_all_green(self):
        home = healthy_home(self.root)
        rep = doctor.platforms_report(
            home,
            http_get=fake_http(),
            qdrant_factory=lambda: SentinelQdrant(),
            ssh_run=lambda *a: (_ for _ in ()).throw(AssertionError("no ssh")),
            rerank_check=lambda: True,
            now=NOW,
        )
        self.assertTrue(rep["ok"], rep)
        self.assertEqual(rep["counts"]["FAIL"], 0)
        self.assertEqual(rep["counts"]["WARN"], 0)
        rows = by_check(rep)
        expected = {
            "mcp:claude",
            "mcp:cursor",
            "mcp:gemini",
            "mcp:codex",
            "mcp:grok",
            "mcp:grok-acp",
            "skill:claude",
            "skill:cursor",
            "skill:codex",
            "hook:SessionStart",
            "hook:Stop",
            "seat-mcp:/health",
            "botfleet:Fleet RAG nightly ingest",
            "botfleet:Fleet RAG weekly health + recall eval",
            "ingest:last-run",
            "ingest:sentinel",
            "tei:rerank",
        }
        self.assertEqual(set(rows), expected)
        self.assertEqual(rows["tei:rerank"]["status"], "OK")
        self.assertNotIn("box:", "".join(rows))  # no --box, no ssh
        self.assertEqual(rows["ingest:last-run"]["detail"], "age_hours=5.0 ok=true")
        self.assertEqual(rows["ingest:sentinel"]["detail"], "age_hours=3.0 ok=true")
        self.assertIn("/recall/contribute", rows["seat-mcp:/health"]["detail"])

    def test_rerank_row_not_configured_warns(self):
        home = healthy_home(self.root)
        rep = doctor.platforms_report(
            home,
            http_get=fake_http(),
            qdrant_factory=lambda: SentinelQdrant(),
            rerank_check=lambda: None,
            now=NOW,
        )
        self.assertEqual(by_check(rep)["tei:rerank"]["status"], "WARN")
        self.assertIn("not configured", by_check(rep)["tei:rerank"]["detail"])

    def test_rerank_row_unreachable_fails(self):
        home = healthy_home(self.root)
        rep = doctor.platforms_report(
            home,
            http_get=fake_http(),
            qdrant_factory=lambda: SentinelQdrant(),
            rerank_check=lambda: False,
            now=NOW,
        )
        self.assertEqual(by_check(rep)["tei:rerank"]["status"], "FAIL")

    def test_rerank_row_check_error_warns_class_only(self):
        home = healthy_home(self.root)

        def boom():
            raise FleetRagError("HTTP 401 from host with a body that must not be echoed")

        rep = doctor.platforms_report(
            home,
            http_get=fake_http(),
            qdrant_factory=lambda: SentinelQdrant(),
            rerank_check=boom,
            now=NOW,
        )
        row = by_check(rep)["tei:rerank"]
        self.assertEqual(row["status"], "WARN")
        self.assertEqual(row["detail"], "check unavailable (FleetRagError)")

    def test_rerank_row_warns_instead_of_failing_when_direct_path_blocked(self):
        # direct_path_blocked=True (set by recall's cmd_doctor when Tailscale is believed down
        # with no operator override) must skip the TEI rerank probe entirely -- TEI_RERANK_URL
        # is a Tailscale-mesh address too, so probing it directly would falsely FAIL a healthy
        # box instead of reporting the known-skipped direct path.
        home = healthy_home(self.root)

        def boom():
            raise AssertionError("must not touch TEI rerank when the direct path is blocked")

        rep = doctor.platforms_report(
            home,
            http_get=fake_http(),
            qdrant_factory=lambda: SentinelQdrant(),
            rerank_check=boom,
            direct_path_blocked=True,
            now=NOW,
        )
        row = by_check(rep)["tei:rerank"]
        self.assertEqual(row["status"], "WARN")
        self.assertEqual(row["detail"], doctor.RERANK_BLOCKED_DETAIL)
        self.assertTrue(rep["ok"])  # a WARN row alone is not a FAIL
        self.assertEqual(rep["counts"]["FAIL"], 0)

    def test_rerank_row_probes_normally_when_direct_path_not_blocked(self):
        # direct_path_blocked=False (the default) must still probe and can still FAIL -- a
        # genuinely unreachable rerank endpoint stays a real FAIL when nothing says the direct
        # path itself is known-blocked.
        home = healthy_home(self.root)
        rep = doctor.platforms_report(
            home,
            http_get=fake_http(),
            qdrant_factory=lambda: SentinelQdrant(),
            rerank_check=lambda: False,
            direct_path_blocked=False,
            now=NOW,
        )
        row = by_check(rep)["tei:rerank"]
        self.assertEqual(row["status"], "FAIL")
        self.assertEqual(row["detail"], "unreachable")

    def test_default_rerank_check_without_credentials_is_not_configured(self):
        # No env, no handoff file reachable from this test HOME: get_config() raises, which the
        # default check treats the same as "not configured" rather than failing doctor entirely.
        with mock.patch.dict(os.environ, {}, clear=True):
            recall_api.reset_config_cache()
            self.assertIsNone(doctor.default_rerank_check())

    def test_output_has_no_values_only_names(self):
        home = healthy_home(self.root)
        rep = doctor.platforms_report(
            home, http_get=fake_http(), qdrant_factory=lambda: SentinelQdrant(), now=NOW
        )
        text = doctor.format_platforms(rep)
        self.assertNotIn(str(home), text)  # no absolute paths, only file names
        self.assertNotIn("http", text)
        self.assertIn("mcp.json has fleet-recall", text)
        self.assertIn("overall: ok", text)
        self.assertIn("STATUS", text.splitlines()[0])

    def test_missing_registration_and_absent_files(self):
        home = healthy_home(self.root, with_hooks=False)
        write(home / ".claude.json", json.dumps({"mcpServers": {"github": {}}}))
        (home / ".cursor" / "mcp.json").unlink()
        write(home / ".codex" / "config.toml", '[mcp_servers.x]\ncommand = "x"\n')
        (home / "apps" / "grok-acp-runtime" / "acp-home-config.toml").unlink()
        (home / ".codex" / "skills" / "fleet-recall" / "SKILL.md").unlink()
        rep = doctor.platforms_report(
            home, http_get=fake_http(), qdrant_factory=lambda: SentinelQdrant(), now=NOW
        )
        rows = by_check(rep)
        self.assertFalse(rep["ok"])
        self.assertEqual(rows["mcp:claude"]["status"], "FAIL")
        self.assertIn("missing fleet-recall", rows["mcp:claude"]["detail"])
        self.assertEqual(rows["mcp:cursor"]["status"], "FAIL")
        self.assertIn("absent", rows["mcp:cursor"]["detail"])
        self.assertEqual(rows["mcp:codex"]["status"], "FAIL")
        self.assertEqual(rows["mcp:grok-acp"]["status"], "WARN")  # optional runtime
        self.assertEqual(rows["mcp:grok"]["status"], "OK")
        self.assertEqual(rows["skill:codex"]["status"], "FAIL")
        self.assertEqual(rows["skill:claude"]["status"], "OK")
        self.assertEqual(rows["hook:SessionStart"]["status"], "WARN")
        self.assertEqual(rows["hook:Stop"]["status"], "WARN")
        self.assertIn("--hooks", rows["hook:Stop"]["detail"])

    def test_hook_installed_but_not_registered_is_warn(self):
        home = healthy_home(self.root)
        write(home / ".claude" / "settings.json", json.dumps({"hooks": {"Stop": []}}))
        rep = doctor.platforms_report(
            home, http_get=fake_http(), qdrant_factory=lambda: SentinelQdrant(), now=NOW
        )
        rows = by_check(rep)
        self.assertEqual(rows["hook:SessionStart"]["status"], "WARN")
        self.assertIn("not in settings.json", rows["hook:SessionStart"]["detail"])

    def test_hook_entry_must_equal_the_installers_command(self):
        home = healthy_home(self.root)
        hooks_dir = home / ".claude" / "hooks"
        settings = home / ".claude" / "settings.json"
        self.assertEqual(
            doctor.expected_hook_commands(hooks_dir),
            {
                "SessionStart": f"{hooks_dir}/fleet-recall-session-start.sh",
                "Stop": f"python3 {hooks_dir}/fleet-recall-stop.py",
            },
        )
        # A wrapper that mentions the file and a copy in another hooks dir are both foreign.
        write(
            settings,
            json.dumps(
                {
                    "hooks": {
                        "SessionStart": [
                            {
                                "hooks": [
                                    {
                                        "type": "command",
                                        "command": f"bash -lc '{hooks_dir}/fleet-recall-session-start.sh'",
                                    }
                                ]
                            }
                        ],
                        "Stop": [
                            {
                                "hooks": [
                                    {
                                        "type": "command",
                                        "command": f"python3 {hooks_dir}/fleet-recall-stop.py",
                                    }
                                ]
                            }
                        ],
                    }
                }
            ),
        )
        self.assertEqual(doctor.hooks_registered(settings), {"SessionStart": False, "Stop": True})
        self.assertEqual(doctor.hooks_foreign(settings), {"SessionStart": True, "Stop": False})
        rep = doctor.platforms_report(
            home, http_get=fake_http(), qdrant_factory=lambda: SentinelQdrant(), now=NOW
        )
        rows = by_check(rep)
        self.assertEqual(rows["hook:SessionStart"]["status"], "WARN")
        self.assertIn("foreign", rows["hook:SessionStart"]["detail"])
        self.assertNotIn(str(home), rows["hook:SessionStart"]["detail"])
        self.assertEqual(rows["hook:Stop"]["status"], "OK")
        write(
            settings,
            json.dumps(
                {
                    "hooks": {
                        "SessionStart": [
                            {
                                "hooks": [
                                    {
                                        "type": "command",
                                        "command": f"{hooks_dir}/fleet-recall-session-start.sh",
                                    }
                                ]
                            }
                        ],
                        "Stop": [
                            {
                                "hooks": [
                                    {
                                        "type": "command",
                                        "command": "python3 /elsewhere/hooks/fleet-recall-stop.py --quiet",
                                    }
                                ]
                            }
                        ],
                    }
                }
            ),
        )
        self.assertEqual(
            doctor.hooks_registered(settings, hooks_dir), {"SessionStart": True, "Stop": False}
        )
        self.assertEqual(
            doctor.hooks_foreign(settings, hooks_dir), {"SessionStart": False, "Stop": True}
        )
        rows = by_check(
            doctor.platforms_report(
                home, http_get=fake_http(), qdrant_factory=lambda: SentinelQdrant(), now=NOW
            )
        )
        self.assertEqual(rows["hook:Stop"]["status"], "WARN")
        self.assertIn("foreign", rows["hook:Stop"]["detail"])
        # Our hook sharing an entry with someone else's still counts as registered.
        write(
            settings,
            json.dumps(
                {
                    "hooks": {
                        "Stop": [
                            {
                                "hooks": [
                                    {
                                        "type": "command",
                                        "command": f"python3 {hooks_dir}/fleet-recall-stop.py",
                                    },
                                    {"type": "command", "command": "node /x/other.mjs"},
                                ]
                            }
                        ]
                    }
                }
            ),
        )
        self.assertEqual(doctor.hooks_registered(settings), {"SessionStart": False, "Stop": True})
        # Malformed shapes never raise.
        write(
            settings,
            json.dumps(
                {"hooks": {"Stop": "nope", "SessionStart": [1, {"hooks": "x"}, {"hooks": [2]}]}}
            ),
        )
        self.assertEqual(doctor.hooks_registered(settings), {"SessionStart": False, "Stop": False})
        self.assertEqual(doctor.hooks_foreign(settings), {"SessionStart": False, "Stop": False})

    def test_uninstall_keeps_a_file_a_foreign_wrapper_runs_and_doctor_warns_foreign(self):
        # Mirror of test_installer.sh "keep": --uninstall leaves fleet-recall-session-start.sh in
        # place because a wrapper entry still runs it, and the doctor renders that survivor as the
        # installed-but-foreign WARN rather than "not installed".
        home = self.root / "home"
        hooks_dir = home / ".claude" / "hooks"
        settings = home / ".claude" / "settings.json"
        write(
            settings,
            json.dumps(
                {
                    "hooks": {
                        "SessionStart": [
                            {
                                "hooks": [
                                    {
                                        "type": "command",
                                        "command": f"bash -lc '{hooks_dir}/fleet-recall-session-start.sh --quiet'",
                                    }
                                ]
                            }
                        ]
                    }
                }
            ),
        )
        env = {**os.environ, "HOME": str(home)}
        for args in (["--hooks"], ["--uninstall"]):
            proc = subprocess.run(
                ["bash", str(INSTALLER), *args],
                env=env,
                capture_output=True,
                text=True,
                timeout=120,
            )
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("kept-foreign fleet-recall-session-start.sh", proc.stdout)
        self.assertTrue((hooks_dir / "fleet-recall-session-start.sh").is_file())
        self.assertFalse((hooks_dir / "fleet-recall-stop.py").exists())
        self.assertEqual(doctor.hooks_registered(settings), {"SessionStart": False, "Stop": False})
        self.assertEqual(doctor.hooks_foreign(settings), {"SessionStart": True, "Stop": False})
        rows = by_check(
            doctor.platforms_report(
                home, http_get=fake_http(), qdrant_factory=lambda: SentinelQdrant(), now=NOW
            )
        )
        self.assertEqual(rows["hook:SessionStart"]["status"], "WARN")
        self.assertIn("foreign", rows["hook:SessionStart"]["detail"])
        self.assertEqual(rows["hook:Stop"]["status"], "WARN")
        self.assertIn("not installed", rows["hook:Stop"]["detail"])

    def test_installer_written_entries_are_recognised(self):
        # The one real cross-check: what install-fleet-rag.sh --hooks writes into a throwaway HOME
        # is exactly what the doctor accepts, so the two ownership tests cannot drift apart.
        home = self.root / "home"
        (home / ".claude").mkdir(parents=True)
        env = {**os.environ, "HOME": str(home)}
        proc = subprocess.run(
            ["bash", str(INSTALLER), "--hooks"],
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("settings.json: added", proc.stdout)
        settings = home / ".claude" / "settings.json"
        self.assertEqual(doctor.hooks_registered(settings), {"SessionStart": True, "Stop": True})
        self.assertEqual(doctor.hooks_foreign(settings), {"SessionStart": False, "Stop": False})
        rows = by_check(
            doctor.platforms_report(
                home, http_get=fake_http(), qdrant_factory=lambda: SentinelQdrant(), now=NOW
            )
        )
        self.assertEqual(rows["hook:SessionStart"]["status"], "OK")
        self.assertEqual(rows["hook:Stop"]["status"], "OK")

    def test_unreadable_json_is_fail(self):
        home = healthy_home(self.root)
        write(home / ".claude.json", "{not json")
        rep = doctor.platforms_report(
            home, http_get=fake_http(), qdrant_factory=lambda: SentinelQdrant(), now=NOW
        )
        self.assertEqual(by_check(rep)["mcp:claude"]["detail"], ".claude.json unreadable")

    def test_seat_mcp_missing_route_and_unreachable(self):
        home = healthy_home(self.root)
        rep = doctor.platforms_report(
            home,
            http_get=fake_http(seat_routes=("/recall/stats",)),
            qdrant_factory=lambda: SentinelQdrant(),
            now=NOW,
        )
        row = by_check(rep)["seat-mcp:/health"]
        self.assertEqual(row["status"], "FAIL")
        self.assertIn("/recall/search", row["detail"])
        self.assertIn("/recall/contribute", row["detail"])
        rep = doctor.platforms_report(
            home,
            http_get=fake_http(fail_urls=(doctor.SEAT_MCP_HEALTH,)),
            qdrant_factory=lambda: SentinelQdrant(),
            now=NOW,
        )
        row = by_check(rep)["seat-mcp:/health"]
        self.assertEqual(row["status"], "FAIL")
        self.assertEqual(row["detail"], "unreachable (ConnectionRefusedError)")

    def test_routines_missing_disabled_unreachable(self):
        home = healthy_home(self.root)
        rep = doctor.platforms_report(
            home,
            http_get=fake_http(
                routines=("Fleet RAG nightly ingest", "Other"),
                disabled=("Fleet RAG nightly ingest",),
            ),
            qdrant_factory=lambda: SentinelQdrant(),
            now=NOW,
        )
        rows = by_check(rep)
        self.assertEqual(rows["botfleet:Fleet RAG nightly ingest"]["detail"], "present, disabled")
        self.assertEqual(rows["botfleet:Fleet RAG nightly ingest"]["status"], "FAIL")
        self.assertEqual(
            rows["botfleet:Fleet RAG weekly health + recall eval"]["detail"], "missing"
        )
        rep = doctor.platforms_report(
            home,
            http_get=fake_http(fail_urls=(doctor.BOTFLEET_ROUTINES,)),
            qdrant_factory=lambda: SentinelQdrant(),
            now=NOW,
        )
        rows = by_check(rep)
        for name in doctor.REQUIRED_ROUTINES:
            self.assertEqual(rows[f"botfleet:{name}"]["status"], "FAIL")
            self.assertIn("api unreachable", rows[f"botfleet:{name}"]["detail"])

    def test_routine_names_match_case_insensitively_and_whitespace_normalized(self):
        # BotFleet's own UI shows Title Case ("Fleet RAG Nightly Ingest") while REQUIRED_ROUTINES
        # is lower-cased; either spelling -- plus stray whitespace -- must still count as present.
        home = healthy_home(self.root)
        rep = doctor.platforms_report(
            home,
            http_get=fake_http(
                routines=("Fleet RAG Nightly Ingest", "  Fleet   RAG Weekly Health + Recall Eval  ")
            ),
            qdrant_factory=lambda: SentinelQdrant(),
            now=NOW,
        )
        rows = by_check(rep)
        for name in doctor.REQUIRED_ROUTINES:
            self.assertEqual(rows[f"botfleet:{name}"]["status"], "OK", rows[f"botfleet:{name}"])
            self.assertEqual(rows[f"botfleet:{name}"]["detail"], "present, enabled")

    def test_last_run_stale_failed_absent(self):
        home = healthy_home(self.root, last_run_age_h=31)
        rep = doctor.platforms_report(
            home, http_get=fake_http(), qdrant_factory=lambda: SentinelQdrant(), now=NOW
        )
        row = by_check(rep)["ingest:last-run"]
        self.assertEqual(row["status"], "FAIL")
        self.assertIn("stale>30h", row["detail"])
        home = healthy_home(self.root, last_run_ok=False)
        rep = doctor.platforms_report(
            home, http_get=fake_http(), qdrant_factory=lambda: SentinelQdrant(), now=NOW
        )
        row = by_check(rep)["ingest:last-run"]
        self.assertEqual(row["status"], "FAIL")
        self.assertIn("ok=false", row["detail"])
        (home / "apps" / "fleet-rag" / "state" / "last-run.json").unlink()
        rep = doctor.platforms_report(
            home, http_get=fake_http(), qdrant_factory=lambda: SentinelQdrant(), now=NOW
        )
        self.assertEqual(
            by_check(rep)["ingest:last-run"]["detail"], "last-run.json absent or unreadable"
        )

    def test_sentinel_stale_failed_none_and_error_class_only(self):
        home = healthy_home(self.root)
        cases = [
            (SentinelQdrant(age_h=40), "FAIL", "stale>30h"),
            (SentinelQdrant(age_h=2, ok=False), "FAIL", "last run failed"),
            (SentinelQdrant(age_h=None), "FAIL", "none_yet"),
            (BoomQdrant(), "FAIL", "qdrant unreachable (FleetRagError)"),
        ]
        for q, status, needle in cases:
            rep = doctor.platforms_report(
                home, http_get=fake_http(), qdrant_factory=lambda q=q: q, now=NOW
            )
            row = by_check(rep)["ingest:sentinel"]
            self.assertEqual(row["status"], status, row)
            self.assertIn(needle, row["detail"])
            self.assertNotIn("echoed", json.dumps(rep))

        def factory_raises():
            raise ConnectionResetError("peer reset with host name inside")

        rep = doctor.platforms_report(
            home, http_get=fake_http(), qdrant_factory=factory_raises, now=NOW
        )
        self.assertEqual(
            by_check(rep)["ingest:sentinel"]["detail"], "qdrant unreachable (ConnectionResetError)"
        )

    def test_sentinel_warns_instead_of_failing_when_direct_path_blocked(self):
        # direct_path_blocked=True (set by recall's cmd_doctor when Tailscale is believed down
        # with no operator override) must skip the Qdrant attempt entirely, not just catch its
        # failure -- a real attempt on this Mac tonight would sit in a multi-minute retry storm.
        home = healthy_home(self.root)

        def boom():
            raise AssertionError("must not touch qdrant when the direct path is blocked")

        rep = doctor.platforms_report(
            home, http_get=fake_http(), qdrant_factory=boom, direct_path_blocked=True, now=NOW
        )
        row = by_check(rep)["ingest:sentinel"]
        self.assertEqual(row["status"], "WARN")
        self.assertEqual(row["detail"], doctor.DIRECT_PATH_BLOCKED_DETAIL)
        self.assertTrue(rep["ok"])  # a WARN row alone is not a FAIL
        self.assertEqual(rep["counts"]["FAIL"], 0)

    def test_box_rows_parsed_and_fail_propagates(self):
        home = healthy_home(self.root)
        out = (
            "OK  qdrant_backup_socratic-trade age_hours=3 file=socratic-trade-2026-09-02.snapshot\n"
            "WARN qdrant_backup_fleet-agents none_yet\n"
            "FAIL qdrant_ingest_sentinel stale>41h\n"
            "garbage line\n"
        )
        seen = []

        def ssh(host, cmd):
            seen.append((host, cmd))
            return 1, out

        rep = doctor.platforms_report(
            home,
            box=True,
            http_get=fake_http(),
            qdrant_factory=lambda: SentinelQdrant(),
            ssh_run=ssh,
            now=NOW,
        )
        self.assertEqual(seen, [(doctor.BOX_HOST, doctor.BOX_HEALTH)])
        rows = by_check(rep)
        self.assertEqual(rows["box:qdrant_backup_socratic-trade"]["status"], "OK")
        self.assertIn(
            "socratic-trade-2026-09-02.snapshot", rows["box:qdrant_backup_socratic-trade"]["detail"]
        )
        self.assertEqual(rows["box:qdrant_backup_fleet-agents"]["status"], "WARN")
        self.assertEqual(rows["box:qdrant_ingest_sentinel"]["status"], "FAIL")
        self.assertFalse(rep["ok"])
        self.assertNotIn("box:garbage", rows)

        def ssh_fail(host, cmd):
            raise TimeoutError("ssh hung")

        rep = doctor.platforms_report(
            home,
            box=True,
            http_get=fake_http(),
            qdrant_factory=lambda: SentinelQdrant(),
            ssh_run=ssh_fail,
            now=NOW,
        )
        self.assertEqual(by_check(rep)["box:ssh"]["detail"], "ssh failed (TimeoutError)")
        rep = doctor.platforms_report(
            home,
            box=True,
            http_get=fake_http(),
            qdrant_factory=lambda: SentinelQdrant(),
            ssh_run=lambda h, c: (255, ""),
            now=NOW,
        )
        self.assertEqual(by_check(rep)["box:ssh"]["detail"], "no health rows (exit 255)")

    def test_parse_box_output(self):
        rows = doctor.parse_box_output("OK  a x=1\nFAIL b\n\nnope\n")
        self.assertEqual(
            [(r["status"], r["check"], r["detail"]) for r in rows],
            [("OK", "box:a", "x=1"), ("FAIL", "box:b", "")],
        )

    def test_toml_header_variants(self):
        p = self.root / "c.toml"
        write(p, '[mcp_servers."fleet-recall"]\ncommand="x"\n')
        self.assertTrue(doctor.toml_has_server(p))
        write(p, "  [ mcp_servers.fleet-recall ]  \n")
        self.assertTrue(doctor.toml_has_server(p))
        write(p, "[mcp_servers.fleet-recall-other]\n")
        self.assertFalse(doctor.toml_has_server(p))
        self.assertIsNone(doctor.toml_has_server(self.root / "missing.toml"))


class ScrollingFake(FakeQdrant):
    """FakeQdrant plus the scroll() the digest needs."""

    def scroll(self, flt=None, limit=256, with_payload=True, with_vector=False):
        for p in self.points:
            if self._matches(p, flt):
                yield {"id": p["id"], "payload": p["payload"]}


def add_contrib(text: str, created_at: int, **payload) -> dict:
    base = {
        "source": "agent-contribution",
        "app": "fleet",
        "category": "lesson",
        "seat": "CLAUDE",
        "doc_id": f"contrib/CLAUDE/x/{len(FakeQdrant.points)}",
        "chunk_index": 0,
        "chunk_count": 1,
        "heading": "",
        "title": "",
        "url": "",
        "path": "",
        "created_at": created_at,
        "updated_at": created_at,
        "ingest_run": "contrib",
    }
    pt = build_point(text, {**base, **payload})
    FakeQdrant.points.append({"id": pt["id"], "payload": pt["payload"]})
    return pt


class DigestTests(unittest.TestCase):
    def setUp(self):
        self._saved = {k: getattr(recall_api, k) for k in SEAMS}
        recall_api.install_fake_backend(seed=True)
        recall_api.Qdrant = ScrollingFake
        add_contrib(
            "Alpha lesson first line.\nsecond line", NOW - 2 * DAY, title="Alpha", seat="GROK"
        )
        add_contrib(
            "Beta preference with no title, long enough to be a lesson body for sure.",
            NOW - 1 * DAY,
            category="preference",
            app="socratic-trade",
        )
        add_contrib(
            "Gamma runbook step number one for the fleet lane.",
            NOW - 3 * DAY,
            category="runbook",
            url="https://example.test/pr/1",
        )
        add_contrib("Old lesson outside the window entirely.", NOW - 20 * DAY, title="Old")
        add_contrib(
            "Newer alpha lesson in the same bucket as Alpha.", NOW - 1 * DAY, title="Alpha2"
        )
        # A doc chunk from the same window must not show up.
        pt = build_point(
            "doc chunk, not a contribution",
            {
                "source": "doc",
                "app": "fleet",
                "category": "doc",
                "seat": "CLAUDE",
                "doc_id": "d/1",
                "chunk_index": 0,
                "chunk_count": 1,
                "heading": "",
                "title": "",
                "url": "",
                "path": "",
                "created_at": NOW - DAY,
                "updated_at": NOW - DAY,
                "ingest_run": "x",
            },
        )
        FakeQdrant.points.append({"id": pt["id"], "payload": pt["payload"]})

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(recall_api, k, v)
        recall_api.reset_config_cache()

    def test_grouping_window_and_order(self):
        d = doctor.contribution_digest(ScrollingFake({}), days=7, now=NOW)
        self.assertEqual(d["total"], 4)
        self.assertEqual(list(d["apps"]), ["fleet", "socratic-trade"])
        self.assertEqual(list(d["apps"]["fleet"]), ["lesson", "runbook"])
        lessons = d["apps"]["fleet"]["lesson"]
        self.assertEqual([e["title"] for e in lessons], ["Alpha2", "Alpha"])  # newest first
        self.assertEqual(lessons[1]["seat"], "GROK")
        self.assertEqual(lessons[1]["date"], "2026-08-31")
        pref = d["apps"]["socratic-trade"]["preference"][0]
        self.assertEqual(
            pref["title"],
            "Beta preference with no title, long enough to be a lesson body for sure.",
        )
        self.assertEqual(d["apps"]["fleet"]["runbook"][0]["url"], "https://example.test/pr/1")
        self.assertNotIn("Old", json.dumps(d))
        self.assertNotIn("doc chunk", json.dumps(d))

    def test_app_filter_and_days(self):
        d = doctor.contribution_digest(ScrollingFake({}), days=7, app="Socratic-Trade", now=NOW)
        self.assertEqual(d["total"], 1)
        self.assertEqual(d["app"], "socratic-trade")
        d = doctor.contribution_digest(ScrollingFake({}), days=30, now=NOW)
        self.assertEqual(d["total"], 5)
        for bad in (0, -1, "7", True):
            with self.assertRaises(FleetRagError):
                doctor.contribution_digest(ScrollingFake({}), days=bad, now=NOW)

    def test_filter_carries_source_and_meta_exclusion(self):
        q = ScrollingFake({})
        seen = []
        real = q.scroll

        def spy(flt=None, **kw):
            seen.append(flt)
            return real(flt, **kw)

        q.scroll = spy
        doctor.contribution_digest(q, days=7, app="fleet", now=NOW)
        self.assertEqual(seen[0]["must_not"], [recall_api.meta_exclude()])
        self.assertIn({"key": "source", "match": {"value": "agent-contribution"}}, seen[0]["must"])
        self.assertIn({"key": "app", "match": {"value": "fleet"}}, seen[0]["must"])
        self.assertIn({"key": "created_at", "range": {"gte": NOW - 7 * DAY}}, seen[0]["must"])

    def test_format_digest(self):
        d = doctor.contribution_digest(ScrollingFake({}), days=7, now=NOW)
        text = doctor.format_digest(d)
        self.assertTrue(text.startswith("4 agent contribution(s) in the last 7 day(s)"))
        self.assertIn("fleet  (3)", text)
        self.assertIn("  lesson  (2)", text)
        self.assertIn("GROK", text)
        self.assertIn("https://example.test/pr/1", text)
        empty = doctor.contribution_digest(ScrollingFake({}), days=7, app="nothing-here", now=NOW)
        self.assertEqual(
            doctor.format_digest(empty),
            "0 agent contribution(s) in the last 7 day(s) for app nothing-here",
        )


class CliTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cli = load_cli()

    def setUp(self):
        self._saved = {k: getattr(recall_api, k) for k in SEAMS}
        recall_api.install_fake_backend(seed=True)
        recall_api.Qdrant = ScrollingFake
        self.tmpdir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmpdir.cleanup()
        for k, v in self._saved.items():
            setattr(recall_api, k, v)
        recall_api.reset_config_cache()

    def test_digest_is_a_subcommand_not_a_search(self):
        self.assertEqual(
            self.cli.normalize_argv(["digest", "--days", "3"]), ["digest", "--days", "3"]
        )
        add_contrib(
            "Lesson for the digest CLI test, long enough to count.", NOW - DAY, title="CLI lesson"
        )
        with mock.patch.object(doctor, "now_ms", lambda: NOW):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = self.cli.main(["digest", "--days", "3", "--json"])
            self.assertEqual(rc, 0)
            d = json.loads(buf.getvalue())
            self.assertEqual(d["total"], 1)
            self.assertEqual(d["apps"]["fleet"]["lesson"][0]["title"], "CLI lesson")
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = self.cli.main(["digest"])
            self.assertEqual(rc, 0)
            self.assertIn("CLI lesson", buf.getvalue())

    def test_doctor_platforms_exit_codes(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = healthy_home(pathlib.Path(tmp))
            with (
                mock.patch.object(doctor, "default_http_get", fake_http()),
                mock.patch.object(doctor, "default_qdrant_factory", lambda: SentinelQdrant()),
                mock.patch.object(doctor, "now_ms", lambda: NOW),
                mock.patch.dict(os.environ, {"HOME": str(home)}),
            ):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = self.cli.main(["doctor", "--platforms"])
                self.assertEqual(rc, 0, buf.getvalue())
                self.assertIn("overall: ok", buf.getvalue())
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = self.cli.main(["doctor", "--platforms", "--json"])
                self.assertEqual(rc, 0)
                self.assertTrue(json.loads(buf.getvalue())["ok"])
                (home / ".claude.json").unlink()
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = self.cli.main(["doctor", "--platforms"])
                self.assertEqual(rc, 1)
                self.assertIn("FAIL", buf.getvalue())
                self.assertIn(".claude.json absent", buf.getvalue())

    def test_search_flags_reach_recall_search(self):
        seen: dict = {}
        real = recall_api.recall_search

        def spy(query, **kw):
            seen.clear()
            seen.update(kw)
            return real(query, **kw)

        with mock.patch.object(recall_api, "recall_search", spy):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = self.cli.main(
                    [
                        "leaking credentials",
                        "--limit",
                        "2",
                        "--per-doc",
                        "2",
                        "--no-rerank",
                        "--no-lessons",
                    ]
                )
            self.assertEqual(rc, 0)
            self.assertEqual(
                (seen["per_doc"], seen["rerank"], seen["prefer_lessons"], seen["limit"]),
                (2, False, False, 2),
            )
            self.assertIn("2 hits  (hybrid)", buf.getvalue())
            self.assertNotIn("rerank=", buf.getvalue())
            with redirect_stdout(io.StringIO()):
                self.cli.main(["leaking credentials"])
            self.assertEqual(
                (seen["per_doc"], seen["rerank"], seen["prefer_lessons"]), (1, True, True)
            )
        err = io.StringIO()
        with mock.patch("sys.stderr", err), redirect_stdout(io.StringIO()):
            rc = self.cli.main(["leaking credentials", "--per-doc", "4"])
        self.assertEqual(rc, 2)
        self.assertIn("per_doc", err.getvalue())

    def _configure_fake_rerank(self):
        cfg = dict(recall_api.load_config())
        cfg.update({"TEI_RERANK_URL": "http://fake-rerank", "TEI_RERANK_API_KEY": "fake"})
        recall_api.load_config = lambda need_write=False, extra=(): cfg
        # Reverse the fused order so the rerank visibly changes the ranking.
        recall_api.rerank = lambda cfg, query, texts, timeout=None: [
            float(i) for i in range(len(texts))
        ]
        recall_api.reset_config_cache()

    def test_reranked_hits_print_rerank_and_fused_scores(self):
        self._configure_fake_rerank()
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = self.cli.main(["leaking credentials", "--limit", "2"])
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertIn("hits  (hybrid+rerank)", out)
        first = json.loads(out.splitlines()[2]).splitlines()[2]
        self.assertTrue(first.startswith("#1  "), first)
        self.assertIn("rerank=", first)
        self.assertIn("fused=", first)
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.cli.main(["leaking credentials", "--limit", "2", "--json"])
        res = json.loads(buf.getvalue())
        self.assertEqual(res["mode"], "hybrid+rerank")
        self.assertIn("rerank_score", res["hits"][0])
        self.assertEqual(res["hits"][0]["score"], res["hits"][0]["rerank_score"])
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.cli.main(["leaking credentials", "--limit", "2", "--no-rerank"])
        self.assertIn("hits  (hybrid)", buf.getvalue())
        self.assertNotIn("rerank=", buf.getvalue())

    def test_eval_flags(self):
        golden = pathlib.Path(self.tmpdir.name) / "golden.jsonl"
        golden.write_text(
            json.dumps(
                {"query": "leaking credentials handoff", "expect_text_contains": "global-api-keys"}
            )
            + "\n",
            encoding="utf-8",
        )
        calls: list[dict] = []
        real = recall_api.recall_search

        def spy(query, **kw):
            calls.append(kw)
            return real(query, **kw)

        with mock.patch.object(recall_api, "recall_search", spy):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = self.cli.main(
                    [
                        "eval",
                        "--golden",
                        str(golden),
                        "--k",
                        "3",
                        "--per-doc",
                        "2",
                        "--no-rerank",
                        "--no-lessons",
                        "--json",
                    ]
                )
            self.assertEqual(rc, 0)
            res = json.loads(buf.getvalue())
            self.assertEqual(res["recall_at_k"], 1.0)
            self.assertEqual(
                calls, [{"limit": 3, "per_doc": 2, "prefer_lessons": False, "rerank": False}]
            )
            calls.clear()
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = self.cli.main(["eval", "--golden", str(golden), "--per-doc", "2", "--compare"])
            self.assertEqual(rc, 0)
            self.assertIn("delta vs off/off", buf.getvalue())
            self.assertEqual(len(calls), 4)  # one run per configuration
            self.assertTrue(all(c["per_doc"] == 2 for c in calls))
            self.assertEqual(
                sorted((c["prefer_lessons"], c["rerank"]) for c in calls),
                [(False, False), (False, True), (True, False), (True, True)],
            )
            calls.clear()
            with redirect_stdout(io.StringIO()):
                rc = self.cli.main(["eval", "--golden", str(golden)])
            self.assertEqual(rc, 0)
            self.assertEqual(calls, [{"limit": 5, "per_doc": 1}])  # defaults: recall_search's own
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = self.cli.main(["eval", "--golden", str(golden), "--threshold", "1.5"])
        self.assertEqual(rc, 1)  # below threshold

    def test_box_without_platforms_is_usage_error(self):
        err = io.StringIO()
        with mock.patch("sys.stderr", err):
            rc = self.cli.main(["doctor", "--box"])
        self.assertEqual(rc, 2)
        self.assertIn("--platforms", err.getvalue())


if __name__ == "__main__":
    unittest.main()
