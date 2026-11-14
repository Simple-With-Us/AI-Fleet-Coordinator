"""Weekly reports collection report.  The Qdrant client is a fake.

cd scripts && python3 -m unittest fleet_rag.tests.test_collection_health -v
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import stat
from pathlib import Path
from unittest import mock

from fleet_rag import collection_health
from fleet_rag.core import FleetRagError
from fleet_rag.nightly_job import MACHINE_IDENTITY_ENV
from fleet_rag.tests._hermetic import HermeticTestCase

NOW = _dt.datetime(2026, 11, 15, 12, 30, tzinfo=_dt.timezone.utc)
GENERATED = "2026-11-15T12:30:00Z"
REPO = Path(__file__).resolve().parents[3]


def _point(
    point_id: str,
    source: str,
    doc_id: str | None,
    updated_at: int | None,
    text: str = "chunk body that must stay out of the report",
) -> dict:
    payload: dict = {"source": source, "text": text}
    if doc_id is not None:
        payload["doc_id"] = doc_id
    if updated_at is not None:
        payload["updated_at"] = updated_at
    return {"id": point_id, "payload": payload}


class FakeQdrant:
    """Pages nothing itself: collection_health.scroll is one iterator, as core.Qdrant.scroll is."""

    def __init__(self, points: list[dict], collection: str = "reports"):
        self.collection = collection
        self.points = points
        self.calls: list[dict] = []

    def scroll(self, flt=None, limit=256, with_payload=True, with_vector=False):
        self.calls.append(
            {
                "filter": flt,
                "limit": limit,
                "with_payload": with_payload,
                "with_vector": with_vector,
            }
        )
        if with_vector:
            raise AssertionError("vectors are out of scope")
        for point in self.points:
            yield point


class CollectionHealthTests(HermeticTestCase):
    def setUp(self) -> None:
        super().setUp()
        # HermeticTestCase already points HOME at a throwaway directory.
        self.home = Path(os.environ["HOME"])
        self.directory = self.home / "report"
        self.manifest = self.home / "ingest-state.json"

    def env(self, **extra: str) -> dict[str, str]:
        base = {
            "HOME": str(self.home),
            "FLEET_RAG_COLLECTION_HEALTH": "1",
            "FLEET_RAG_COLLECTION_HEALTH_DIR": str(self.directory),
            "FLEET_RAG_INGEST_STATE": str(self.manifest),
        }
        base.update(extra)
        return base

    def write_manifest(self, state: dict) -> None:
        self.manifest.write_text(json.dumps(state), encoding="utf-8")

    def run_report(self, points: list[dict], client: FakeQdrant | None = None, **extra: str) -> dict:
        active = client if client is not None else FakeQdrant(points)
        result = collection_health.execute(self.env(**extra), client=active, now=NOW)
        self.assertTrue(result["ok"], result)
        return json.loads((self.directory / "latest.json").read_text(encoding="utf-8"))

    def test_machine_identity_names_match_the_nightly_job(self) -> None:
        self.assertEqual(collection_health.MACHINE_IDENTITY_ENV, MACHINE_IDENTITY_ENV)

    def test_disabled_run_does_not_scroll_or_write(self) -> None:
        client = FakeQdrant([_point("p", "doc", "doc/a", 1)])
        env = self.env()
        env["FLEET_RAG_COLLECTION_HEALTH"] = "0"
        result = collection_health.execute(env, client=client, now=NOW)
        self.assertFalse(result["enabled"])
        self.assertTrue(result["ok"])
        self.assertEqual(client.calls, [])
        self.assertFalse((self.directory / "latest.json").exists())

    def test_counts_ranges_orphans_and_ignores_chunk_text(self) -> None:
        self.write_manifest(
            {
                "doc/kept": {
                    "doc_hash": "abc",
                    "chunk_ids": ["p1"],
                    "source": "doc",
                },
                "_linear_sync": {"kind": "linear-sync", "updated_at_cursor": "2026-11-01T00:00:00Z"},
                "not-a-document": "cursor",
            }
        )
        points = [
            _point("p1", "doc", "doc/kept", 1_700_000_000_000),
            _point("p2", "doc", "doc/kept", 1_800_000_000_000),
            _point("p3", "doc", "doc/orphan", 1_600_000_000_000),
            _point("p4", "agent-contribution", "contrib/SEAT/2026-11-01/abcd1234", 1_750_000_000_000),
            _point("p5", "meta", "meta/ingest-status", 1_790_000_000_000),
            _point("p6", "doc", None, 1_710_000_000_000),
        ]
        client = FakeQdrant(points)
        report = self.run_report(points, client=client)
        self.assertEqual(client.calls[0]["with_vector"], False)
        self.assertEqual(client.calls[0]["with_payload"], ["source", "doc_id", "updated_at"])
        self.assertIsNone(client.calls[0]["filter"])
        self.assertEqual(report["collection"], "reports")
        self.assertTrue(report["read_only"])
        self.assertEqual(report["points"], 6)
        self.assertEqual(report["docs"], 4)
        self.assertEqual(report["missing_doc_id_points"], 1)
        doc = report["by_source"]["doc"]
        self.assertEqual(doc["points"], 4)
        self.assertEqual(doc["docs"], 2)
        self.assertEqual(doc["oldest_updated_at"], 1_600_000_000_000)
        self.assertEqual(doc["newest_updated_at"], 1_800_000_000_000)
        self.assertEqual(report["manifest"], {"available": True, "documents": 1})
        orphans = report["orphans"]
        self.assertTrue(orphans["available"])
        self.assertEqual(orphans["docs"], 3)
        self.assertEqual(orphans["points"], 4)
        self.assertEqual(orphans["missing_doc_id_points"], 1)
        self.assertEqual(orphans["by_source"]["doc"]["doc_ids"], ["doc/orphan"])
        self.assertIn("meta/ingest-status", orphans["by_source"]["meta"]["doc_ids"])
        self.assertNotIn("_linear_sync", json.dumps(orphans))
        blob = (self.directory / "latest.json").read_text(encoding="utf-8")
        blob += (self.directory / "latest.md").read_text(encoding="utf-8")
        self.assertNotIn("chunk body that must stay out of the report", blob)
        self.assertEqual(stat.S_IMODE((self.directory / "latest.json").stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((self.directory / "latest.md").stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.directory.stat().st_mode), 0o700)
        markdown = (self.directory / "latest.md").read_text(encoding="utf-8")
        self.assertIn("doc/orphan", markdown)
        self.assertIn("No previous report was available.", markdown.replace("\u00a0", " "))

    def test_growth_against_the_previous_report(self) -> None:
        self.write_manifest({"doc/kept": {"doc_hash": "abc", "chunk_ids": ["p1"], "source": "doc"}})
        first = self.run_report([_point("p1", "doc", "doc/kept", 1000)])
        self.assertFalse(first["growth"]["available"])
        second_points = [
            _point("p1", "doc", "doc/kept", 1000),
            _point("p2", "doc", "doc/new", 2000),
        ]
        later = _dt.datetime(2026, 11, 22, 12, 30, tzinfo=_dt.timezone.utc)
        result = collection_health.execute(
            self.env(), client=FakeQdrant(second_points), now=later
        )
        self.assertTrue(result["ok"])
        report = json.loads((self.directory / "latest.json").read_text(encoding="utf-8"))
        self.assertEqual(report["generated_at"], "2026-11-22T12:30:00Z")
        self.assertTrue(report["growth"]["available"])
        self.assertEqual(report["growth"]["previous_generated_at"], GENERATED)
        self.assertEqual(report["growth"]["points"], 1)
        self.assertEqual(report["growth"]["docs"], 1)
        self.assertEqual(report["growth"]["by_source"]["doc"], {"points": 1, "docs": 1})
        self.assertEqual(report["growth"]["orphans"], {"points": 1, "docs": 1})
        markdown = (self.directory / "latest.md").read_text(encoding="utf-8")
        self.assertIn("Compared with the report from 2026-11-15T12:30:00Z.", markdown)
        self.assertIn("Points changed by +1 and documents changed by +1.", markdown)

    def test_missing_manifest_skips_orphans_and_still_writes(self) -> None:
        report = self.run_report([_point("p1", "doc", "doc/a", 5)])
        self.assertEqual(report["manifest"]["reason"], "missing")
        self.assertFalse(report["orphans"]["available"])
        self.assertEqual(report["points"], 1)
        self.assertNotIn("doc_ids", report["orphans"])
        raw_markdown = (self.directory / "latest.md").read_text(encoding="utf-8")
        self.assertIn("\u00a0 ", raw_markdown)
        markdown = raw_markdown.replace("\u00a0", " ")
        self.assertIn("Orphaned chunks were not computed.", markdown)

    def test_unreadable_manifest_is_not_a_failed_report(self) -> None:
        self.manifest.write_text("{", encoding="utf-8")
        report = self.run_report([_point("p1", "skill", "skill/a", 5)])
        self.assertEqual(report["manifest"]["reason"], "unreadable")
        self.assertFalse(report["orphans"]["available"])
        self.assertTrue(report["ok"])

    def test_symlink_manifest_is_not_read(self) -> None:
        target = self.home / "elsewhere.json"
        target.write_text(json.dumps({"doc/a": {"doc_hash": "x", "chunk_ids": []}}), encoding="utf-8")
        self.manifest.symlink_to(target)
        report = self.run_report([_point("p1", "doc", "doc/a", 5)])
        self.assertEqual(report["manifest"]["reason"], "unreadable")
        self.assertFalse(report["orphans"]["available"])

    def test_failure_keeps_the_previous_report(self) -> None:
        self.write_manifest({})
        self.run_report([_point("p1", "doc", "doc/a", 5)])
        before = (self.directory / "latest.json").read_text(encoding="utf-8")

        class Boom:
            collection = "reports"

            def scroll(self, *args, **kwargs):
                raise FleetRagError(
                    "HTTP 503 from qdrant.example:6333/collections/reports/points/scroll: "
                    "upstream body must not be stored"
                )

        result = collection_health.execute(self.env(), client=Boom(), now=NOW)
        self.assertFalse(result["ok"])
        self.assertNotIn("upstream", result["error"])
        self.assertEqual((self.directory / "latest.json").read_text(encoding="utf-8"), before)
        error = json.loads((self.directory / "last-error.json").read_text(encoding="utf-8"))
        self.assertFalse(error["ok"])
        self.assertNotIn("upstream", error["error"])
        self.assertIn("HTTP 503", error["error"])

    def test_busy_lock_does_not_replace_the_report(self) -> None:
        self.write_manifest({})
        self.run_report([_point("p1", "doc", "doc/a", 5)])
        before = (self.directory / "latest.json").read_text(encoding="utf-8")
        client = FakeQdrant([_point("p2", "doc", "doc/b", 9)])

        def busy(*_args, **_kwargs):
            raise BlockingIOError

        original = collection_health.fcntl.flock
        collection_health.fcntl.flock = busy
        try:
            result = collection_health.execute(self.env(), client=client, now=NOW)
        finally:
            collection_health.fcntl.flock = original
        self.assertEqual(result.get("lock"), "busy")
        self.assertEqual(client.calls, [])
        self.assertEqual((self.directory / "latest.json").read_text(encoding="utf-8"), before)

    def test_read_config_uses_the_environment_and_does_not_fetch(self) -> None:
        def fetch(_names):
            raise AssertionError("fetch")

        cfg = collection_health.read_config(
            {
                "QDRANT_URL": "http://qdrant.example:6333",
                "QDRANT_FLEET_COLLECTION": "reports",
                "QDRANT_READONLY_API_KEY": "readonly-key",
                "QDRANT_API_KEY": "write-key",
            },
            fetch,
        )
        self.assertEqual(cfg["QDRANT_READONLY_API_KEY"], "readonly-key")
        self.assertNotIn("TEI_API_KEY", cfg)

    def test_read_config_fetches_only_when_a_read_key_is_missing(self) -> None:
        seen: dict = {}

        def fetch(names):
            seen["names"] = names
            return {
                "QDRANT_URL": "http://qdrant.example:6333",
                "QDRANT_FLEET_COLLECTION": "reports",
                "QDRANT_READONLY_API_KEY": "readonly-key",
                "TEI_API_KEY": "should-not-stick",
            }

        cfg = collection_health.read_config({"HOME": str(self.home)}, fetch)
        self.assertEqual(seen["names"], collection_health.READ_NAMES)
        self.assertEqual(cfg["QDRANT_READONLY_API_KEY"], "readonly-key")
        self.assertNotIn("TEI_API_KEY", cfg)

    def test_machine_identity_refuses_before_a_fetch(self) -> None:
        def fetch(_names):
            raise AssertionError("fetch")

        with self.assertRaises(collection_health.CollectionHealthError) as caught:
            collection_health.read_config(
                {
                    "INFISICAL_CLIENT_ID": "present",
                    "QDRANT_URL": "http://qdrant.example:6333",
                    "QDRANT_FLEET_COLLECTION": "reports",
                    "QDRANT_API_KEY": "write-key",
                },
                fetch,
            )
        self.assertEqual(caught.exception.reason, "machine-identity:INFISICAL_CLIENT_ID")

    def test_direct_primary_api_does_not_borrow_an_ambient_project_binding(self) -> None:
        import fleet_infisical as shared
        from fleet_knobs.tests._fake import fixture_service_token

        env = {
            "HOME": str(self.home),
            "INFISICAL_TOKEN": fixture_service_token(),
            "INFISICAL_AI_FLEET_COORDINATOR_TOKEN": fixture_service_token(),
            "INFISICAL_WRITE_TOKEN": fixture_service_token(),
        }
        with (
            mock.patch.dict(os.environ, env, clear=True),
            mock.patch.object(
                shared, "bounded_fetch_project", side_effect=AssertionError("ambient reader used")
            ) as fetch,
        ):
            with self.assertRaises(collection_health.CollectionHealthError) as caught:
                collection_health.read_config(env)
            fetch.assert_not_called()
        self.assertIn("FLEET_RECALL_INFISICAL_TOKEN", caught.exception.reason)

    def test_main_disabled_exits_zero(self) -> None:
        env = self.env()
        env["FLEET_RAG_COLLECTION_HEALTH"] = ""
        with _env(env):
            self.assertEqual(collection_health.main([]), 0)

    def test_units_match_the_nightly_install(self) -> None:
        service = (REPO / "scripts/systemd/fleet-rag-collection-health.service").read_text(
            encoding="utf-8"
        )
        timer = (REPO / "scripts/systemd/fleet-rag-collection-health.timer").read_text(
            encoding="utf-8"
        )
        example = (REPO / "scripts/systemd/fleet-rag-collection-health.env.example").read_text(
            encoding="utf-8"
        )
        directives = [
            line.strip()
            for line in service.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        self.assertNotIn("[Install]", directives)
        self.assertFalse(any(line.startswith("User=") for line in directives))
        self.assertIn("ConditionPathExists=/etc/fleet/rag-collection-health.env", service)
        self.assertIn("Wants=code-main-sync.service", service)
        self.assertIn("After=network-online.target code-main-sync.service", service)
        self.assertIn("collection_health.py", service)
        self.assertIn("Sun *-*-* 06:30:00 America/Chicago", timer)
        self.assertIn("Persistent=true", timer)
        self.assertIn("WantedBy=timers.target", timer)
        self.assertIn("FLEET_RAG_COLLECTION_HEALTH=0", example)
        self.assertNotIn("st.", example)
        self.assertIn("fleet-rag-collection-health.service", (
            REPO / "scripts/fleet_app_processes.py"
        ).read_text(encoding="utf-8"))


class _env:
    def __init__(self, values: dict[str, str]) -> None:
        self.values = values
        self._saved: dict[str, str | None] = {}

    def __enter__(self):
        for key, value in self.values.items():
            self._saved[key] = os.environ.get(key)
            os.environ[key] = value
        return self

    def __exit__(self, *_exc) -> None:
        for key, previous in self._saved.items():
            if previous is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = previous
