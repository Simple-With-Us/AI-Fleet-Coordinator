"""Receiver provenance refresh uses the existing ingest fixture without embedding again."""
import unittest
from unittest import mock

from fleet_rag import ingest
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

    def test_many_unchanged_documents_share_receiver_verification_batch(self):
        fixture = self.fixture
        fixture.docs['doc'] = [ingest_fixture.make_doc('doc/' + str(index), (f'Unique operational body {index}. ' * 20), extra={'source_commit':'old'}) for index in range(30)]
        fixture._run()
        for document in fixture.docs['doc']:
            document.extra['source_commit'] = 'new'
        with mock.patch.object(fixture.qd, '_call', wraps=fixture.qd._call) as calls:
            fixture._run()
        verification = [call for call in calls.call_args_list if call.args[1].get('with_payload') is True]
        self.assertEqual(len(verification), 1)
        self.assertTrue(all(point['payload']['source_commit'] == 'new' for point in fixture.qd.points.values()))

    def test_shared_primary_owner_is_not_reassigned_and_corruption_holds_source(self):
        fixture = self.fixture
        fixture.docs['doc'][0].extra['source_commit'] = 'old'
        fixture._run()
        point = next(point for point in fixture.qd.points.values() if point['payload']['doc_id'] == 'doc/a')
        point['payload']['doc_id'] = 'doc/other-primary'
        fixture.docs['doc'][0].extra['source_commit'] = 'new'
        fixture._run()
        self.assertEqual(point['payload']['doc_id'], 'doc/other-primary')
        self.assertEqual(point['payload']['source_commit'], 'old')
        point['payload']['text'] = 'corrupted receiver body'
        self.assertFalse(fixture._run()['ok'])

    def test_prune_requires_positive_new_state_ownership_without_marking_legacy(self):
        state = {'doc/legacy': {'source':'doc'}, 'doc/owned': {'source':'doc', 'source_reconciliation_marker':'ingest-v1'}, 'doc/library/history/x': {'source':'doc', 'source_reconciliation_marker':'ingest-v1'}}
        self.assertEqual(ingest._prune_candidates('doc', set(), state), ['doc/owned'])
        fixture = self.fixture
        fixture._run()
        state = ingest.load_state(fixture.state)
        self.assertEqual(state['doc/a']['source_reconciliation_marker'], 'ingest-v1')
        state['doc/a'].pop('source_reconciliation_marker')
        ingest.save_state(state, fixture.state)
        fixture.docs['doc'][0].text_markdown += '\nA changed committed body.'
        fixture._run()
        self.assertNotIn('source_reconciliation_marker', ingest.load_state(fixture.state)['doc/a'])

    def test_corrupt_canonical_preserves_valid_duplicate_and_its_state(self):
        fixture = self.fixture
        body = 'The identical operational body shared by two documents. ' * 6
        first = ingest_fixture.make_doc('doc/a', body, title='Alpha')
        second = ingest_fixture.make_doc('doc/b', body, title='Beta')
        fixture.docs['doc'] = [first, second]
        fixture._run(prune=False)
        state = ingest.load_state(fixture.state)
        primary = state['doc/a']['chunk_ids'][0]
        duplicate = ingest.doc_rows(second, 'receiver-regression')[0][0]
        duplicate_id = duplicate['id']
        self.assertNotEqual(primary, duplicate_id)
        fixture.qd.points[duplicate_id] = {
            'id': duplicate_id, 'payload': duplicate['payload'], 'vector': [0.1] * 4,
        }
        state['doc/b']['chunk_ids'] = [duplicate_id]
        ingest.save_state(state, fixture.state)
        fixture.qd.points[primary]['payload']['text'] = 'Corrupt receiver body'
        self.assertFalse(fixture._run(prune=False)['ok'])
        self.assertIn(duplicate_id, fixture.qd.points)
        self.assertNotIn(duplicate_id, fixture.qd.deleted)
        self.assertEqual(ingest.load_state(fixture.state)['doc/b']['chunk_ids'], [duplicate_id])
