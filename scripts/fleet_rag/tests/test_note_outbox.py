"""Crash, outage and replacement fixtures for private Notes delivery intent."""
from dataclasses import asdict
import fcntl
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from fleet_rag import note_outbox as box, note_upsert, sources


def document(text='A durable operational note body with enough content.', name='p1'):
    return sources.Doc('note/ABC/ICNote/'+name,'Note',text,'apple-note','fleet','note','CODEX')


class OutboxTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)/'outbox'

    def pending(self,receipt):
        return json.loads((self.root/(receipt['key']+'.json')).read_text())

    def test_durable_private_queue_and_outage_backoff(self):
        receipt=box.enqueue_id(document().doc_id,root=self.root,now=100)
        self.assertEqual(self.root.stat().st_mode&0o777,0o700)
        self.assertEqual((self.root/(receipt['key']+'.json')).stat().st_mode&0o777,0o600)
        def unavailable(_record):raise RuntimeError('provider-private-sentinel')
        result=box.drain(unavailable,root=self.root,now=100)
        self.assertEqual(result['pending'],1)
        self.assertEqual(self.pending(receipt)['next_attempt'],130)
        self.assertNotIn('provider-private-sentinel',json.dumps(self.pending(receipt)))
        self.assertNotIn('text_markdown',json.dumps(self.pending(receipt)))
        self.assertNotIn(document().text_markdown,json.dumps(self.pending(receipt)))
        delivery=mock.Mock(return_value=True)
        box.drain(delivery,root=self.root,now=110);delivery.assert_not_called()

    def test_persist_failure_and_full_spool_do_not_claim_queued(self):
        with mock.patch.object(box.os,'replace',side_effect=PermissionError('fixture')):
            with self.assertRaises(PermissionError):box.enqueue_id(document().doc_id,root=self.root)
        self.assertFalse(list(self.root.glob('*.json')))
        self.assertFalse(list(self.root.glob('.pending-*')))
        first=box.enqueue_id(document().doc_id,root=self.root)
        with mock.patch.object(box,'MAX_PENDING',1):
            with self.assertRaisesRegex(box.OutboxError,'outbox-full'):
                box.enqueue_id(document(name='p2').doc_id,root=self.root)
        self.assertTrue(self.pending(first))

    def test_old_ack_cannot_remove_newer_version(self):
        old=box.enqueue_id(document().doc_id,root=self.root)
        newer={}
        def deliver(_record):
            newer.update(box.enqueue_id(document().doc_id,root=self.root))
            return True
        result=box.drain(deliver,root=self.root)
        self.assertEqual(result['superseded'],1)
        self.assertNotEqual(old['version'],newer['version'])
        self.assertEqual(self.pending(newer)['version'],newer['version'])

    def test_crash_between_receipt_and_unlink_retries_idempotently(self):
        receipt=box.enqueue_id(document().doc_id,root=self.root)
        atomic=box._atomic
        def crash(directory,name,record):
            atomic(directory,name,record)
            if name.startswith('receipt-'):raise OSError('fixture crash')
        with mock.patch.object(box,'_atomic',side_effect=crash):
            self.assertEqual(box.drain(lambda _record:True,root=self.root)['item_failed'], 1)
        self.assertTrue(self.pending(receipt))
        result=box.drain(lambda _record:True,root=self.root)
        self.assertEqual(result['verified'],1)
        self.assertFalse((self.root/(receipt['key']+'.json')).exists())

    def test_explicit_source_delete_cancels_old_body(self):
        receipt=box.enqueue_id(document().doc_id,root=self.root)
        box.cancel(document().doc_id,root=self.root)
        callback=mock.Mock(return_value=True)
        box.drain(callback,root=self.root);callback.assert_not_called()
        self.assertFalse((self.root/(receipt['key']+'.json')).exists())
        with mock.patch.object(box, 'MAX_PENDING', 1):
            box.enqueue_id(document(name='p2').doc_id, root=self.root)

    def test_symlink_spool_or_pending_record_is_refused(self):
        self.root.symlink_to(Path(self.temp.name))
        with self.assertRaises(OSError):box.enqueue_id(document().doc_id,root=self.root)

    def test_malformed_record_is_quarantined_without_blocking_other_notes(self):
        box.enqueue_id(document().doc_id,root=self.root)
        malformed=self.root/('0'*64+'.json')
        malformed.write_text('{malformed-private-sentinel');malformed.chmod(0o600)
        result=box.drain(lambda _record:True,root=self.root)
        self.assertEqual((result['quarantined'],result['verified']),(1,1))
        self.assertFalse(malformed.exists())
        receipt=json.loads((self.root/('quarantine-'+'0'*64+'.json')).read_text())
        self.assertNotIn('private-sentinel',json.dumps(receipt))
        self.assertTrue((self.root/receipt['preserved_as']).exists())

    def test_duplicate_drainer_cannot_enter_provider_or_ack_while_note_lock_held(self):
        queued=box.enqueue_id(document().doc_id,root=self.root)
        with box._delivery_lock(self.root,queued['key']):
            deliver=mock.Mock(return_value=True)
            result=box.drain(deliver,root=self.root)
        deliver.assert_not_called();self.assertEqual(result['pending'],1)

    def test_coding_metadata_baseline_then_native_edit_queue_contains_no_title_or_body(self):
        row={'id':'x-coredata://ABC/ICNote/p1','name':'A private Coding title','modified':'first','created':'original'}
        first=box.reconcile_coding(root=self.root,metadata_reader=lambda:[row])
        self.assertTrue(first['baseline_only']);self.assertEqual(first['queued'],0)
        self.assertFalse(list(self.root.glob('[a-f0-9]'*64+'.json')))
        changed=box.reconcile_coding(root=self.root,metadata_reader=lambda:[{**row,'modified':'newer'}])
        self.assertEqual(changed['queued'],1)
        stored=(self.root/'coding-metadata.json').read_text()
        self.assertNotIn(row['name'],stored)
        self.assertNotIn('text_markdown',stored)
        before=stored
        with self.assertRaises(RuntimeError):
            box.reconcile_coding(root=self.root,metadata_reader=mock.Mock(side_effect=RuntimeError('provider-outage')))
        self.assertEqual((self.root/'coding-metadata.json').read_text(),before)

    def test_permanent_delivery_does_not_block_a_later_coding_update(self):
        row={'id':'x-coredata://ABC/ICNote/p1','name':'Title','modified':'first','created':'original'}
        box.reconcile_coding(root=self.root,metadata_reader=lambda:[row])
        changed=box.reconcile_coding(root=self.root,metadata_reader=lambda:[{**row,'modified':'newer'}])
        self.assertEqual(changed['queued'],1)
        # Mark the queued record permanently failed, then change the note again.
        queued_files=[p for p in self.root.glob('*.json') if p.name!='coding-metadata.json']
        self.assertEqual(len(queued_files),1)
        record=json.loads(queued_files[0].read_text())
        record['delivery_state']='permanent'
        queued_files[0].write_text(json.dumps(record))
        again=box.reconcile_coding(root=self.root,metadata_reader=lambda:[{**row,'modified':'third'}])
        self.assertEqual(again['queued'],1)


class ReceiverTests(unittest.TestCase):
    setUp = OutboxTests.setUp
    pending = OutboxTests.pending
    def test_corrupt_hash_wrong_id_and_missing_chunk_never_ack(self):
        doc=document()
        rows,_=note_upsert.ingest.doc_rows(doc,'fixture')
        payload=rows[0]['payload']
        qd=mock.Mock()
        for bad in (None,{'id':'point','payload':{**payload,'text':'corrupted'}},{'id':'point','payload':{**payload,'doc_id':'note/OTHER'}},{'id':'point','payload':{**payload,'title':'outdated title'}}):
            qd.retrieve.return_value=bad
            self.assertFalse(note_upsert.receiver_verified(doc,qd,['point']))
        qd.retrieve.return_value={'id':'point','payload':payload}
        self.assertTrue(note_upsert.receiver_verified(doc,qd,['point']))

    def test_newer_provider_write_during_old_delivery_remains_pending_then_wins(self):
        from fleet_rag.tests.test_ingest import FakeQdrant
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)/'outbox';state=Path(temporary)/'state.json'
            current={'id':'x-coredata://ABC/ICNote/p1','name':'Note','text':'The first committed Coding note body.','modified':'2026-11-13T10:00:00','revision':'first'}
            box.enqueue_id('note/ABC/ICNote/p1',root=root)
            class Receiver(FakeQdrant):
                changed=False
                def upsert(self,points,wait=True):
                    result=super().upsert(points,wait)
                    if not self.changed:
                        self.changed=True
                        current.update(text='The newer committed Coding note body.',revision='newer')
                        box.enqueue_id('note/ABC/ICNote/p1',root=root)
                    return result
                def retrieve(self,point_id,**kwargs):
                    point=self.points.get(point_id)
                    return {'id':point_id,'payload':point['payload']} if point else None
            qd=Receiver()
            def read(_doc_id):return dict(current)
            with mock.patch.object(note_upsert,'STATE_PATH',state),mock.patch.object(note_upsert.core,'load_config',return_value={'fixture':True}),mock.patch.object(note_upsert.core,'Qdrant',return_value=qd),mock.patch.object(box,'prepare_document',side_effect=lambda doc:asdict(doc)),mock.patch.object(note_upsert.ingest,'embed',side_effect=lambda cfg,texts:[[0.1]*4 for _ in texts]),mock.patch.object(note_upsert.ingest,'gitleaks_gate',return_value=set()),mock.patch.object(note_upsert.ingest,'EMBED_RATE',1e9):
                deliver=lambda record:note_upsert.deliver_queued_note(record,root=root,read_current=read)
                first=box.drain(deliver,root=root)
                self.assertEqual(first['superseded'],1)
                self.assertTrue(list(root.glob('[a-f0-9]'*64+'.json')))
                second=box.drain(deliver,root=root)
            self.assertEqual(second['verified'],1)
            self.assertEqual(len(qd.points),1)
            payload=next(iter(qd.points.values()))['payload']
            self.assertEqual(payload['doc_id'],'note/ABC/ICNote/p1')
            self.assertIn('newer committed',payload['text'])

    def test_provider_read_failure_is_pending_without_loading_credentials(self):
        with mock.patch.object(note_upsert.core,'load_config') as config:
            with self.assertRaisesRegex(box.DeliveryError, '^delivery-outcome-unknown$'):
                note_upsert.deliver_queued_note({'schema':2,'doc_id':'note/ABC/ICNote/p1'},read_current=mock.Mock(side_effect=RuntimeError('private provider diagnostic')))
        config.assert_not_called()

    def test_original_secret_gate_runs_before_scrub_or_staging(self):
        from simple_tools import secretscan
        original = document()
        original.text_markdown = 'Original multiline field.\nStill the original field.'
        with mock.patch.object(secretscan, 'scan', side_effect=lambda text: 'detected' if text == original.text_markdown else None) as scanner, mock.patch('fleet_rag.scrub.scrub') as scrubber, mock.patch.object(box.tempfile, 'TemporaryDirectory') as staging:
            with self.assertRaisesRegex(box.DeliveryError, '^note-secret-gate-refused$'):
                box.prepare_document(original)
        self.assertEqual(original.text_markdown, scanner.call_args.args[0])
        scrubber.assert_not_called(); staging.assert_not_called()

    def test_item_filesystem_failure_does_not_block_later_record(self):
        receipts = [box.enqueue_id(document(name=name).doc_id, root=self.root) for name in ('p1', 'p2')]
        ordered = sorted(receipts, key=lambda receipt: receipt['key'])
        current = box.current
        def fault(root, key, version):
            if key == ordered[0]['key']:
                raise OSError('private-path-sentinel')
            return current(root, key, version)
        with mock.patch.object(box, 'current', side_effect=fault):
            result = box.drain(lambda record: True, root=self.root, limit=2)
        self.assertEqual((result['item_failed'], result['verified']), (1, 1))
        self.assertTrue(self.pending(ordered[0]))
        self.assertNotIn('private-path-sentinel', json.dumps(result))

    def test_delivery_failures_have_fixed_classification_and_permanent_hold(self):
        for status, category in ((408, 'retryable'), (429, 'retryable'), (503, 'retryable'), (403, 'permanent'), (None, 'outcome-unknown')):
            with self.subTest(status=status):
                queued = box.enqueue_id(document().doc_id, root=self.root, now=100)
                failure = RuntimeError('private-provider-sentinel')
                failure.status = status
                result = box.drain(mock.Mock(side_effect=failure), root=self.root, now=100)
                self.assertEqual(result[category], 1)
                self.assertEqual(self.pending(queued)['delivery_state'], category)
                self.assertNotIn('private-provider-sentinel', json.dumps(self.pending(queued)))
                if category == 'permanent':
                    callback = mock.Mock(return_value=True)
                    box.drain(callback, root=self.root, now=999999)
                    callback.assert_not_called()

    def test_actual_http_wrapper_preserves_typed_status_context_without_body_reflection(self):
        import io
        import urllib.error
        from fleet_rag import core
        for status, category in ((403, 'permanent'), (429, 'retryable'), (503, 'retryable')):
            with self.subTest(status=status):
                provider = urllib.error.HTTPError('https://fixture.invalid/points', status, 'private-diagnostic', {}, io.BytesIO(b'private-body-sentinel'))
                with mock.patch.object(core.urllib.request, 'urlopen', side_effect=provider):
                    try:
                        core.http_json('https://fixture.invalid/points', retries=0)
                    except core.FleetRagError as error:
                        failure = box.delivery_failure(error)
                    else:
                        self.fail('HTTP wrapper did not propagate failure')
                self.assertEqual(failure.category, category)
                self.assertNotIn('private', str(failure))
                self.assertIsNone(failure.__context__)

    def test_malformed_provider_snapshot_fails_before_credentials(self):
        for snapshot in (None, {}, {'id': 3}, {'id':'x-coredata://ABC/ICNote/p1','name':'Note','text':None,'revision':'r'},
                         {'id':'', 'name':'Note', 'text':'Body', 'revision':'r'},
                         {'id':'   ', 'name':'Note', 'text':'Body', 'revision':'r'}):
            with self.subTest(snapshot=snapshot), mock.patch.object(note_upsert.core, 'load_config') as config:
                with self.assertRaisesRegex(box.DeliveryError, '^provider-contract-invalid$'):
                    note_upsert.deliver_queued_note({'schema':2,'doc_id':'note/ABC/ICNote/p1'}, read_current=lambda _id: snapshot)
                config.assert_not_called()

    def test_child_timeout_and_missing_executable_are_fixed_statuses(self):
        import subprocess
        from simple_tools import notes
        for error, status in ((subprocess.TimeoutExpired('private-command', 1),124), (FileNotFoundError('private-path'),127)):
            with mock.patch.object(notes.subprocess, 'run', side_effect=error):
                result = notes.retry_rag_outbox({})
            self.assertEqual(result['exit_status'], status)
            self.assertNotIn('private', json.dumps(result))
            with mock.patch.object(box, 'enqueue_id', return_value={'key':'0' * 64}), mock.patch.object(notes.subprocess, 'run', side_effect=error):
                result = notes._fleet_rag_upsert({'action':'create'}, 'x-coredata://ABC/ICNote/p1', {})
            self.assertEqual(result['exit_status'], status)
            self.assertEqual(result['state'], 'queued')
            self.assertNotIn('private', json.dumps(result))

    def test_revision_change_without_a_new_event_holds_pending(self):
        from fleet_rag.core import content_hash
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)/'outbox';state=Path(temporary)/'state.json'
            doc=document();doc.seat='FLEET'
            queued=box.enqueue_id(doc.doc_id,root=root,now=100)
            state.write_text(json.dumps({doc.doc_id:{'doc_hash':content_hash(doc.text_markdown),'chunk_ids':['point'],'source':'apple-note'}}))
            row=note_upsert.ingest.doc_rows(doc,'fixture')[0][0]
            qd=mock.Mock();qd.retrieve.return_value={'id':'point','payload':row['payload']}
            snapshot={'id':'x-coredata://ABC/ICNote/p1','name':'Note','text':doc.text_markdown,'modified':'2026-11-13T10:00:00','revision':'before'}
            reader=mock.Mock(side_effect=[snapshot,{**snapshot,'revision':'after'}])
            with mock.patch.object(note_upsert,'STATE_PATH',state),mock.patch.object(note_upsert.core,'load_config',return_value={'fixture':True}),mock.patch.object(note_upsert.core,'Qdrant',return_value=qd),mock.patch.object(box,'prepare_document',side_effect=lambda doc:asdict(doc)):
                result=box.drain(lambda record:note_upsert.deliver_queued_note(record,root=root,read_current=reader),root=root,now=100)
            self.assertEqual(result['pending'],1)
            self.assertEqual(json.loads((root/(queued['key']+'.json')).read_text())['attempts'],1)


class ProviderReadTests(unittest.TestCase):
    def test_coding_metadata_cap_precedes_provider_property_reads_and_refuses_overcap_before_enqueue(self):
        from fleet_rag import notes_export
        row=notes_export.US.join(('x-coredata://ABC/ICNote/p1','Title','modified','created'))+notes_export.RS
        with tempfile.TemporaryDirectory() as temporary,mock.patch.object(notes_export,'_osascript',return_value=row) as run:
            result=notes_export.list_current_coding_metadata()
            self.assertEqual(len(result),1)
            script=run.call_args.args[0]
            self.assertIn('folder "Coding" of account "iCloud"',script)
            self.assertIn('set metadataLimit to 10000',script)
            self.assertIn('set outputLimit to 2097152',script)
            self.assertLess(script.index('if (count of notes of f) > metadataLimit'),script.index('set ids to id of every note of f'))
            self.assertNotIn('body of',script)
            self.assertEqual(run.call_args.kwargs,{'timeout':15,'isolated_env':True})
            run.return_value=row*(notes_export.MAX_CODING_NOTES+1)
            with mock.patch.object(box,'enqueue_id') as enqueue,mock.patch.object(notes_export,'read_current_coding_note') as body_reader:
                with self.assertRaisesRegex(notes_export.NotesUnavailable,'coding-metadata-too-large'):
                    box.reconcile_coding(root=Path(temporary)/'outbox')
            enqueue.assert_not_called();body_reader.assert_not_called()
            self.assertFalse((Path(temporary)/'outbox').exists())

    def test_read_is_coding_only_bounded_and_revision_includes_content(self):
        from fleet_rag import notes_export
        raw_id='x-coredata://ABC/ICNote/p1'
        reply=notes_export.US.join((raw_id,'Title','2026-11-13T10:00:00','<div>Title</div><div>Body lesson.</div>'))
        with mock.patch.object(notes_export,'_osascript',return_value=reply) as run:
            first=notes_export.read_current_coding_note('note/ABC/ICNote/p1')
        self.assertIn('folder "Coding"',run.call_args.args[0])
        self.assertEqual(run.call_args.kwargs,{'timeout':30,'isolated_env':True})
        self.assertNotIn('Title',first['text'])
        with mock.patch.object(notes_export,'_osascript',return_value=reply.replace('Body lesson.','Updated lesson.')):
            second=notes_export.read_current_coding_note('note/ABC/ICNote/p1')
        self.assertNotEqual(first['revision'],second['revision'])
