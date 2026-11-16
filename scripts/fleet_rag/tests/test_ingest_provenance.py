"""Receiver provenance refresh uses the existing ingest fixture without embedding again."""
import unittest

from fleet_rag.tests import test_ingest as ingest_fixture


class IngestProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.fixture = ingest_fixture.IngestTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)

    def test_unchanged_receiver_provenance_refreshes_without_embedding(self):
        fixture = self.fixture
        fixture.docs["doc"][0].extra.update(source_commit="old")
        fixture._run()
        calls = len(fixture.embed_calls)
        fixture.docs["doc"][0].extra.update(
            source_commit="new", source_aliases=["doc/library/legacy/a.md"]
        )
        fixture._run()
        self.assertEqual(len(fixture.embed_calls), calls)
        points = [
            point["payload"]
            for point in fixture.qd.points.values()
            if point["payload"].get("doc_id") == "doc/a"
        ]
        self.assertTrue(points)
        self.assertTrue(all(point.get("source_commit") == "new" for point in points))
        self.assertTrue(
            all("doc/library/legacy/a.md" in point.get("source_aliases", []) for point in points)
        )
