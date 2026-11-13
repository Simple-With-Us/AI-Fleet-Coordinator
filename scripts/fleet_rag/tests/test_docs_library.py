"""Canonical Git provenance and hostile library metadata with real local Git fixtures."""
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
import subprocess
import tempfile
import unittest
from unittest import mock

from fleet_rag import docs_library as library, sources


class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.code=self.root/'Code';self.code.mkdir()
        self.docs=self.code/'Docs';self.docs.mkdir()
        self.repo=self.code/'Clutch';self.repo.mkdir()
        self.run_git('init','-q','-b','main')
        self.run_git('remote','add','origin','https://github.com/Simple-With-Us/Clutch.git')
        (self.repo/'docs').mkdir();(self.repo/'docs/current.md').write_text('# Current\n\nAuthoritative body.\n')
        self.run_git('add','.');self.run_git('commit','-q','-m','fixture')
        self.repositories=library.discover_repositories(self.code)
        self.raw=(self.repo/'docs/current.md').read_bytes()
        self.document=self.make_doc(self.repo/'docs/current.md')
        self.document.doc_id='doc/Clutch/docs/current.md'
        self.canonical={self.document.doc_id:(self.document,self.raw)}
        self.warnings=[]

    def run_git(self,*args):
        return subprocess.check_output(['/usr/bin/git','-C',str(self.repo),*args],env={**os.environ,'GIT_AUTHOR_NAME':'fixture','GIT_AUTHOR_EMAIL':'fixture@example.invalid','GIT_COMMITTER_NAME':'fixture','GIT_COMMITTER_EMAIL':'fixture@example.invalid'}).decode().strip()

    def make_doc(self,path):
        return sources.Doc('',path.stem,'','doc','fleet','doc','FLEET',path=str(path))

    def collection(self,data=None,record=None):
        child=self.docs/'Generated/Clutch';(child/'documents/docs').mkdir(parents=True)
        data=self.raw if data is None else data
        (child/'documents/docs/current.md').write_bytes(data)
        manifest={'schema_version':2,'generated_by':'collect-repo-documents.py','owner_repo':'Simple-With-Us/Clutch','repo':'Clutch','source_commit':self.repositories[0].commit,'files':{'documents/docs/current.md':{'source_path':'docs/current.md','source_commit':self.repositories[0].commit,'sha256':hashlib.sha256(data).hexdigest()}}}
        if record:manifest['files'].update(record)
        (child/'manifest.json').write_text(json.dumps(manifest))
        return child,manifest

    def read(self):
        return list(library.library_documents(self.docs,self.repositories,self.canonical,self.make_doc,lambda p:False,self.warnings.append))

    def test_exact_generated_and_legacy_copies_are_aliases_only(self):
        child,_=self.collection()
        (self.docs/'legacy.md').write_bytes(self.raw)
        (self.docs/'unique.txt').write_text('A unique legacy operational lesson.')
        result=self.read()
        self.assertEqual([d.doc_id for d in result],['doc/library/legacy/' + hashlib.sha256((self.docs/'unique.txt').read_bytes()).hexdigest()])
        self.assertEqual(set(self.document.extra['source_aliases']),{str(child/'documents/docs/current.md'),str(self.docs/'legacy.md')})

    def test_different_historical_body_remains_a_separate_document(self):
        self.collection(b'# Old\n\nA previous authoritative version.')
        result=self.read()
        self.assertEqual(len(result),1)
        self.assertTrue(result[0].doc_id.startswith('doc/library/history/'))
        self.assertIn('previous authoritative version',result[0].text_markdown)
        self.assertNotIn('source_aliases',self.document.extra)

    def test_large_tracked_generated_text_is_canonical_and_oversize_does_not_quarantine(self):
        raw = ('A supported human document paragraph.\n' * 15000).encode()
        self.assertGreater(len(raw), 400 * 1024)
        self.assertLess(len(raw), library.MAX_BYTES)
        (self.repo/'docs/current.md').write_bytes(raw)
        self.run_git('add', '.'); self.run_git('commit', '-q', '-m', 'large human document')
        self.repositories = library.discover_repositories(self.code)
        self.canonical = {}
        child, manifest = self.collection(raw)
        huge = b'A' * (library.MAX_BYTES + 1)
        (child/'documents/docs/oversize.md').write_bytes(huge)
        manifest['files']['documents/docs/oversize.md'] = {'source_path':'docs/oversize.md', 'sha256':hashlib.sha256(huge).hexdigest()}
        (child/'manifest.json').write_text(json.dumps(manifest))
        self.read()
        document, actual = self.canonical['doc/Clutch/docs/current.md']
        self.assertEqual(actual, raw)
        self.assertEqual(document.text_markdown.encode(), raw)
        self.assertEqual(document.extra['source_sha256'], hashlib.sha256(raw).hexdigest())
        self.assertEqual(document.extra['source_aliases'], [str(child/'documents/docs/current.md')])
        self.assertTrue(any('above bounded size cap' in warning for warning in self.warnings))
        self.assertFalse(any('no aliases trusted' in warning for warning in self.warnings))
        with mock.patch.object(sources.mirror, 'configured_root', return_value=None):
            selected = list(sources.iter_docs(fleet_repo=self.root/'absent',fleet_ops=self.root/'absent-ops',extra=[],code_dir=self.code,apps_dir=None,grok_home=None))
        self.assertEqual(sum(doc.doc_id == document.doc_id for doc in selected), 1)
        self.assertFalse(sources.take_source_read_receipt('doc')['source_complete'])

    def test_missing_generated_content_marks_source_incomplete_but_absent_manifest_does_not(self):
        child,_=self.collection()
        missing=child/'documents/docs/current.md'
        missing.unlink()
        legacy=self.docs/'ordinary-legacy';legacy.mkdir()
        (legacy/'lesson.txt').write_text('A legacy child without a manifest remains readable.')
        sources.take_warnings();sources.take_source_read_receipt('doc')
        with mock.patch.object(sources.mirror,'configured_root',return_value=None):
            list(sources.iter_docs(fleet_repo=self.root/'absent',fleet_ops=self.root/'absent-ops',extra=[],code_dir=self.code,apps_dir=None,grok_home=None))
        receipt=sources.take_source_read_receipt('doc')
        self.assertFalse(receipt['source_complete'])
        self.assertTrue(any('invalid or unreadable generated' in warning for warning in sources.take_warnings()))
        missing.write_bytes(self.raw)
        with mock.patch.object(sources.mirror,'configured_root',return_value=None):
            list(sources.iter_docs(fleet_repo=self.root/'absent',fleet_ops=self.root/'absent-ops',extra=[],code_dir=self.code,apps_dir=None,grok_home=None))
        self.assertTrue(sources.take_source_read_receipt('doc')['source_complete'])
        self.assertFalse(sources.take_warnings())

    def test_same_relative_path_in_server_and_fresh_mirror_keeps_both_bodies_through_ingest(self):
        from fleet_rag import ingest, mirror
        from fleet_rag.tests.test_ingest import FakeQdrant
        mirrored=self.root/'mirror'
        legacy=mirrored/'docs/legacy';legacy.mkdir(parents=True)
        bodies=('A unique server operational handoff with durable instructions.',
                'A distinct Mac operational handoff with historical decisions.')
        (self.docs/'handoff.md').write_text(bodies[0])
        (legacy/'handoff.md').write_text(bodies[1])
        (legacy/'exact-server-copy.md').write_text(bodies[0])
        def generate(limit=None):
            return sources.iter_docs(fleet_repo=self.root/'absent',fleet_ops=self.root/'absent-ops',extra=[],code_dir=self.code,apps_dir=None,grok_home=None,limit=limit)
        qd=FakeQdrant()
        with mock.patch.object(mirror,'configured_root',return_value=mirrored),mock.patch.object(mirror,'mirror_status',return_value=SimpleNamespace(fresh=True)),mock.patch.object(ingest,'GENERATORS',{'doc':generate}),mock.patch.object(ingest,'embed',side_effect=lambda cfg,texts:[[0.1]*4 for _ in texts]),mock.patch.object(ingest,'gitleaks_gate',return_value=set()),mock.patch.object(ingest,'EMBED_RATE',1e9):
            docs=list(generate())
            report=ingest.run('doc',prune=False,heartbeat=False,cfg={'fixture':True},qd=qd,state_path=self.root/'state.json',last_run_path=self.root/'last.json',log_path=self.root/'log.jsonl',log=lambda *_args:None)
        histories=[doc for doc in docs if doc.doc_id.startswith('doc/library/legacy/')]
        self.assertEqual({doc.text_markdown for doc in histories},set(bodies))
        self.assertEqual(len(histories),2)
        for doc in histories:
            digest=hashlib.sha256(doc.text_markdown.encode()).hexdigest()
            self.assertEqual(doc.doc_id,'doc/library/legacy/'+digest)
            self.assertEqual(doc.extra['source_sha256'],digest)
            self.assertEqual(doc.extra['library_relative_path'],'handoff.md')
        server=next(doc for doc in histories if doc.text_markdown==bodies[0])
        self.assertEqual(server.extra['source_aliases'],[str(legacy/'exact-server-copy.md')])
        self.assertTrue(report['ok'],report['errors'])
        self.assertTrue(report['source_read_receipts']['doc']['source_complete'])
        self.assertEqual(report['per_source']['doc']['docs_seen'],3)
        self.assertEqual(len(qd.points),3)
        for body in bodies:
            self.assertTrue(any(body in point['payload']['text'] for point in qd.points.values()))

    def test_invalid_last_manifest_record_commits_no_partial_aliases(self):
        self.collection(record={'documents/../../escape.md':{'source_path':'../../escape.md','sha256':'0'*64}})
        self.assertEqual(self.read(),[])
        self.assertNotIn('source_aliases',self.document.extra)
        self.assertTrue(self.warnings)

    def test_wrong_owner_or_hash_cannot_claim_an_alias(self):
        child,manifest=self.collection()
        manifest['owner_repo']='unapproved/Clutch'
        (child/'manifest.json').write_text(json.dumps(manifest))
        self.assertEqual(self.read(),[])
        self.assertNotIn('source_aliases',self.document.extra)
        manifest['owner_repo']='Simple-With-Us/Clutch'
        manifest['files']['documents/docs/current.md']['sha256']='0'*64
        (child/'manifest.json').write_text(json.dumps(manifest))
        self.assertEqual(self.read(),[])
        self.assertNotIn('source_aliases',self.document.extra)

    def test_paths_symlinks_and_parent_swap_never_escape_the_library(self):
        outside=self.root/'outside';outside.mkdir();(outside/'secret.md').write_text('not read')
        (self.docs/'link').symlink_to(outside,target_is_directory=True)
        for relative in ('../outside/secret.md','/absolute.md','x\x00.md','a\\b.md','link/secret.md'):
            with self.subTest(relative=relative),self.assertRaises((ValueError,OSError)):
                library.safe_file(self.docs,relative)
        rootlink=self.root/'root-link';rootlink.symlink_to(self.docs,target_is_directory=True)
        with self.assertRaises(ValueError):library.safe_file(rootlink,'link/secret.md')
        inside=self.docs/'inside';inside.mkdir();(inside/'secret.md').write_text('safe original')
        original_open=os.open
        def swap(name,flags,*args,**kwargs):
            if name=='inside':
                inside.rename(self.docs/'old-inside')
                inside.symlink_to(outside,target_is_directory=True)
            return original_open(name,flags,*args,**kwargs)
        with mock.patch.object(library.os,'open',side_effect=swap),self.assertRaises(OSError):
            library.safe_file(self.docs,'inside/secret.md')

    def test_discovery_includes_new_repos_and_normalizes_fleet_ops(self):
        self.run_git('remote','set-url','origin','https://github.com/simple-with-us/fleet-ops.git')
        repo=library.discover_repositories(self.code)[0]
        self.assertEqual((repo.owner,repo.name),('Simple-With-Us','Fleet-OPS'))
        self.run_git('remote','set-url','origin','https://github.com/unapproved/Clutch.git')
        self.assertEqual(library.discover_repositories(self.code),[])

    def test_git_redirect_environment_and_replace_objects_do_not_change_source(self):
        repository=self.repositories[0]
        original=self.run_git('rev-parse','HEAD:docs/current.md')
        replacement=self.repo/'replacement.txt';replacement.write_text('Replacement object must not be read.')
        replacement_id=self.run_git('hash-object','-w',str(replacement))
        self.run_git('replace',original,replacement_id)
        foreign=self.root/'foreign';foreign.mkdir()
        subprocess.run(['/usr/bin/git','-C',str(foreign),'init','-q'],check=True)
        library._tracked_tree.cache_clear()
        hostile={'GIT_DIR':str(foreign/'.git'),'GIT_WORK_TREE':str(foreign),
                 'GIT_CONFIG_COUNT':'1','GIT_CONFIG_KEY_0':'core.bare','GIT_CONFIG_VALUE_0':'true'}
        with mock.patch.dict(os.environ,hostile):
            self.assertEqual(repository.blob('docs/current.md'),self.raw)

    def test_failed_or_malformed_git_listing_is_not_an_empty_inventory(self):
        for payload in (None,b'malformed listing\x00'):
            library._tracked_tree.cache_clear()
            with mock.patch.object(library,'git',return_value=payload),self.assertRaises(library.GitInventoryError):
                library._tracked_tree(self.repo,'0'*40)

    def test_identical_unique_library_copies_share_one_history_doc(self):
        raw=b'# Historical\n\nA unique previous version.'
        self.collection(raw)
        (self.docs/'dated-copy.md').write_bytes(raw)
        (self.docs/'another-copy.md').write_bytes(raw)
        result=self.read()
        self.assertEqual(len(result),1)
        self.assertEqual(set(result[0].extra['source_aliases']),{str(self.docs/'dated-copy.md'),str(self.docs/'another-copy.md')})

    def test_iter_docs_reads_tracked_git_bytes_not_untracked_or_modified_bodies(self):
        (self.repo/'docs/current.md').write_text('uncommitted body must not replace Git')
        (self.repo/'docs/untracked.md').write_text('untracked body must not be read')
        result=list(sources.iter_docs(fleet_repo=self.root/'absent',fleet_ops=self.root/'absent-ops',extra=[],code_dir=self.code,apps_dir=None,grok_home=None))
        docs={d.doc_id:d for d in result}
        self.assertEqual(docs[self.document.doc_id].text_markdown,self.raw.decode())
        self.assertNotIn('doc/Clutch/docs/untracked.md',docs)

    def test_html_is_plain_text_and_code_logs_archives_are_excluded(self):
        (self.docs/'page.html').write_text('<h1>Guide</h1><script>hidden()</script><p>A legacy lesson.</p>')
        for name in ('code.py','run.log','session.jsonl','archive.zip'):(self.docs/name).write_text('excluded')
        result=self.read()
        self.assertEqual(len(result),1)
        self.assertIn('A legacy lesson.',result[0].text_markdown)
        self.assertNotIn('hidden()',result[0].text_markdown)

    def test_pdf_requires_existing_local_extractor_and_never_uses_a_provider(self):
        with mock.patch.object(library.shutil,'which',return_value=None),mock.patch.object(library.os,'access',return_value=False):
            with self.assertRaisesRegex(ValueError,'pdf-extractor-unavailable'):
                library.text_from_bytes(b'%PDF-1.4 fixture','.pdf')
        tool=self.root/'pdftotext'
        tool.write_text('#!/bin/sh\nprintf "A bounded extracted PDF lesson.\\n"\n');tool.chmod(0o700)
        with mock.patch.object(library.shutil,'which',return_value=str(tool)):
            self.assertEqual(library.text_from_bytes(b'%PDF-1.4 fixture','.pdf'),'A bounded extracted PDF lesson.\n')
            with self.assertRaisesRegex(ValueError,'pdf-input-too-large'):
                library.text_from_bytes(b'x'*(library.MAX_PDF_BYTES+1),'.pdf')

    def test_complete_pdf_source_and_ingest_keep_one_canonical_chunk_set(self):
        from fleet_rag import ingest
        from fleet_rag.tests.test_ingest import FakeQdrant
        raw=b'%PDF-1.4 bounded fixture document'
        source=self.repo/'docs/strategy.pdf';source.write_bytes(raw)
        self.run_git('add','.');self.run_git('commit','-q','-m','PDF fixture')
        repository=library.discover_repositories(self.code)[0]
        child=self.docs/'Generated/Clutch';(child/'documents/docs').mkdir(parents=True)
        (child/'documents/docs/strategy.pdf').write_bytes(raw)
        manifest={'schema_version':2,'generated_by':'collect-repo-documents.py','owner_repo':'Simple-With-Us/Clutch','repo':'Clutch','source_commit':repository.commit,'files':{'documents/docs/strategy.pdf':{'source_path':'docs/strategy.pdf','sha256':hashlib.sha256(raw).hexdigest()}}}
        (child/'manifest.json').write_text(json.dumps(manifest))
        (self.docs/'dated-strategy.pdf').write_bytes(raw)
        original_read=sources.read_text
        def refuse_raw_pdf(path,*args,**kwargs):
            self.assertNotEqual(Path(path).suffix,'.pdf','PDF bytes must never pass through the Markdown reader')
            return original_read(path,*args,**kwargs)
        with mock.patch.object(library,'_pdf_text',return_value='A bounded strategic framework extracted from the PDF document.'),mock.patch.object(sources,'read_text',side_effect=refuse_raw_pdf):
            docs=list(sources.iter_docs(fleet_repo=self.root/'absent',fleet_ops=self.root/'absent-ops',extra=[],code_dir=self.code,apps_dir=None,grok_home=None))
        pdfs=[doc for doc in docs if doc.doc_id.endswith('docs/strategy.pdf')]
        self.assertEqual(len(pdfs),1)
        self.assertEqual(pdfs[0].doc_id,'doc/Clutch/docs/strategy.pdf')
        self.assertEqual(pdfs[0].path,str(source))
        self.assertEqual(set(pdfs[0].extra['source_aliases']),{str(child/'documents/docs/strategy.pdf'),str(self.docs/'dated-strategy.pdf')})
        qd=FakeQdrant()
        with mock.patch.object(ingest,'GENERATORS',{'doc':lambda limit=None:iter(pdfs)}),mock.patch.object(ingest,'embed',side_effect=lambda cfg,texts:[[0.1]*4 for _ in texts]) as embed,mock.patch.object(ingest,'gitleaks_gate',return_value=set()),mock.patch.object(ingest,'EMBED_RATE',1e9):
            report=ingest.run('doc',prune=False,heartbeat=False,cfg={'fixture':True},qd=qd,state_path=self.root/'state.json',last_run_path=self.root/'last.json',log_path=self.root/'log.jsonl',log=lambda *_args:None)
        self.assertTrue(report['ok'])
        self.assertEqual(len(qd.points),1)
        self.assertEqual(report['per_source']['doc']['docs_seen'],1)
        self.assertEqual(sum(len(call.args[1]) for call in embed.call_args_list),1)
        payload=next(iter(qd.points.values()))['payload']
        self.assertEqual(payload['doc_id'],'doc/Clutch/docs/strategy.pdf')
        self.assertEqual(set(payload['also_paths']),set(pdfs[0].extra['source_aliases']))
