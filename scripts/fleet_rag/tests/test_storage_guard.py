import json
import os
import subprocess
import time
import sys
import tempfile
import pathlib
import unittest
from unittest.mock import patch, Mock
from fleet_rag.storage_guard import StorageGuard, StorageGuardError
from fleet_rag import core


class StorageGuardTests(unittest.TestCase):
    def setUp(self):
        self.cfg = dict(endpoint='http://remote:6333', collection='fleet', storage_path='/volume/qdrant',
                        device='/dev/sdb', mount='/volume', reserve_bytes=100, write_allowance_bytes=50,
                        max_request_bytes=40, probe_argv=['probe'], probe_timeout_seconds=2,
                        max_age_seconds=5)
        self.info = {k: self.cfg[k] for k in ('endpoint', 'collection', 'storage_path', 'device', 'mount')}
        self.info.update(observed_at=1000, available_bytes=150)
        self.env = patch.dict(os.environ, {'FLEET_RAG_STORAGE_GUARD': json.dumps(self.cfg)});self.env.start()
        self.clock = patch('fleet_rag.storage_guard.time.time', return_value=1000);self.clock.start()
        self.probe = patch('fleet_rag.storage_guard.bounded_probe');self.run = self.probe.start()
        self.run.side_effect = lambda *a, **k: json.dumps(self.info).encode()
    def tearDown(self):
        self.probe.stop();self.clock.stop();self.env.stop()
    def guard(self):
        return StorageGuard.from_env('http://remote:6333', 'fleet')
    def test_exact_boundary_and_fresh_probe_every_time(self):
        g=self.guard();self.assertEqual(g.check(40),150);g.check(1);self.assertEqual(self.run.call_count,2)
    def test_low_headroom(self):
        self.info['available_bytes']=149
        with self.assertRaises(StorageGuardError):self.guard().check()
    def test_malformed_or_wrong_volume_or_old_evidence(self):
        for key,val in [('device','/dev/sda'),('mount','/'),('endpoint','http://client'),
                        ('collection','other'),('storage_path','/wrong'),('observed_at',994),
                        ('observed_at',1001),('observed_at',float('nan')),('available_bytes',True),
                        ('available_bytes',-1)]:
            old=self.info[key];self.info[key]=val
            with self.subTest(key=key,val=val),self.assertRaises(StorageGuardError):self.guard().check()
            self.info[key]=old
    def test_probe_failure_or_timeout(self):
        for exc in [OSError(),subprocess.TimeoutExpired('probe',2)]:
            self.run.side_effect=exc
            with self.assertRaises(StorageGuardError):self.guard().check()
    def test_invalid_probe_output(self):
        for data in [b'bad',b'[]']:
            self.run.side_effect=None;self.run.return_value=data
            with self.assertRaises(StorageGuardError):self.guard().check()
    def test_oversize_refuses_without_probe(self):
        with self.assertRaises(StorageGuardError):self.guard().check(41)
        self.run.assert_not_called()
    def test_request_cannot_exceed_write_allowance_even_with_larger_cap(self):
        self.cfg['max_request_bytes'] = 1000
        os.environ['FLEET_RAG_STORAGE_GUARD'] = json.dumps(self.cfg)
        self.guard().check(50)
        self.run.reset_mock()
        for size in (51, -1, True, 1.5):
            with self.subTest(size=size), self.assertRaises(StorageGuardError):
                self.guard().check(size)
        self.run.assert_not_called()

    def test_remote_paths_require_canonical_spelling(self):
        for path in ('/volume/', '/volume/../volume', '/volume/./qdrant', '//volume'):
            for field in ('storage_path', 'mount'):
                with self.subTest(field=field, path=path):
                    with patch.dict(os.environ, {'FLEET_RAG_STORAGE_GUARD': json.dumps({**self.cfg, field:path})}):
                        with self.assertRaises(StorageGuardError): self.guard()
        self.run.assert_not_called()

    def test_remote_alias_mismatch_does_not_resolve_on_client(self):
        self.info['storage_path'] = '/remote-symlink/qdrant'
        with patch('os.path.realpath', side_effect=AssertionError('must not resolve remote paths locally')):
            with self.assertRaises(StorageGuardError): self.guard().check()

    def test_guard_reuses_exact_serialized_request_length_on_retries(self):
        q=core.Qdrant({'QDRANT_URL':'http://remote:6333','QDRANT_FLEET_COLLECTION':'fleet','QDRANT_API_KEY':'test'})
        q.storage_guard = Mock()
        body={'payload': 'é'}
        expected = len(json.dumps(body).encode())
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b'{}'
        failure=core.urllib.error.HTTPError('http://remote',500,'retry',None,None)
        with patch.object(core.json, 'dumps', wraps=json.dumps) as encode, patch.object(core.urllib.request, 'urlopen', side_effect=[failure,response]) as net, patch.object(core.time, 'sleep'):
            q._call('/test', body=body, write=True)
            self.assertEqual(encode.call_count, 1)
            self.assertEqual(q.storage_guard.check.call_args_list, [((expected,),), ((expected,),)])
            self.assertEqual(net.call_count, 2)

    def test_bad_policies_fail_closed(self):
        for key,val in [('reserve_bytes',0),('max_age_seconds',float('inf')),('probe_argv',[]),
                        ('write_allowance_bytes',True),('endpoint','http://different')]:
            cfg={**self.cfg,key:val}
            with patch.dict(os.environ,{'FLEET_RAG_STORAGE_GUARD':json.dumps(cfg)}):
                with self.assertRaises(StorageGuardError):self.guard()
    def test_required_no_policy(self):
        with patch.dict(os.environ,{'FLEET_RAG_STORAGE_GUARD_REQUIRED':'1'}):
            os.environ.pop('FLEET_RAG_STORAGE_GUARD')
            with self.assertRaises(StorageGuardError):self.guard()
    def test_every_write_retry_rechecks_and_stops_before_network(self):
        self.cfg['max_request_bytes']=1000
        os.environ['FLEET_RAG_STORAGE_GUARD']=json.dumps(self.cfg)
        q=core.Qdrant({'QDRANT_URL':'http://remote:6333','QDRANT_FLEET_COLLECTION':'fleet','QDRANT_API_KEY':'test'})
        def fail(req,timeout):
            self.info['available_bytes']=0
            raise core.urllib.error.HTTPError(req.full_url,500,'full',None,None)
        with patch.object(core.urllib.request,'urlopen',side_effect=fail) as net,patch.object(core.time,'sleep'):
            with self.assertRaisesRegex(core.FleetRagError,'headroom'):q.set_payload(['id'],{})
            self.assertEqual(net.call_count,1)
        self.assertEqual(self.run.call_count,2)

    def test_all_mutation_methods_guard_before_network(self):
        self.info['available_bytes']=0
        q=core.Qdrant({'QDRANT_URL':'http://remote:6333','QDRANT_FLEET_COLLECTION':'fleet','QDRANT_API_KEY':'test'})
        with patch.object(core.urllib.request,'urlopen') as net:
            for action in [lambda:q.upsert([{'id':'x'}]),lambda:q.set_payload(['x'],{}),
                           lambda:q.delete_ids(['x']),lambda:q.delete_by_filter({'must':[]})]:
                with self.assertRaises(core.FleetRagError):action()
            net.assert_not_called()
    def test_reads_never_probe(self):
        q=core.Qdrant({'QDRANT_URL':'http://remote:6333','QDRANT_FLEET_COLLECTION':'fleet','QDRANT_API_KEY':'test'})
        with patch.object(core,'http_json',return_value={'result':{'status':'green'}}):
            self.assertEqual(q.info()['status'],'green')
        self.run.assert_not_called()


class BoundedProbeTests(unittest.TestCase):
    def test_oversized_stdout_stops_while_process_is_running(self):
        from fleet_rag.storage_guard import bounded_probe
        with self.assertRaises(ValueError):
            bounded_probe([sys.executable,'-c','import os,time;os.write(1,b"x"*20000);time.sleep(30)'],2)
    def test_stderr_is_discarded_not_buffered_or_deadlocked(self):
        from fleet_rag.storage_guard import bounded_probe
        self.assertEqual(bounded_probe([sys.executable,'-c',
                         'import os;os.write(2,b"x"*1000000);os.write(1,b"{}")'],2),b'{}')
    def test_timeout_kills_and_reaps_probe(self):
        from fleet_rag.storage_guard import bounded_probe
        with self.assertRaises(subprocess.TimeoutExpired):
            bounded_probe([sys.executable,'-c','import time;time.sleep(30)'],0.05)
    def test_nonzero_and_exact_byte_limit(self):
        from fleet_rag.storage_guard import bounded_probe
        with self.assertRaises(ValueError):
            bounded_probe([sys.executable,'-c','raise SystemExit(1)'],2)
        self.assertEqual(len(bounded_probe([sys.executable,'-c','import os;os.write(1,b"x"*16384)'],2)),16384)

    @unittest.skipUnless(sys.platform.startswith('linux'), 'Linux /proc live-child assertion')
    def test_descendant_holding_stdout_dies_on_deadline(self):
        from fleet_rag.storage_guard import bounded_probe
        with tempfile.TemporaryDirectory() as tmp:
            pidfile=pathlib.Path(tmp)/'pid'
            program=('import os,time,pathlib;pid=os.fork();'
                     'pathlib.Path('+repr(str(pidfile))+').write_text(str(pid)) if pid else None;'
                     'time.sleep(30) if not pid else None')
            with self.assertRaises(subprocess.TimeoutExpired):
                bounded_probe([sys.executable,'-c',program],0.2)
            pid=int(pidfile.read_text())
            for _ in range(40):
                try:
                    state=pathlib.Path('/proc/'+str(pid)+'/stat').read_text().split()[2]
                except FileNotFoundError:
                    break
                if state == 'Z':
                    break  # terminated, pending init reaping; no live survivor
                time.sleep(0.01)
            else:
                self.fail('probe descendant survived group cleanup')
    def test_unsupported_platform_refuses_before_launch(self):
        from fleet_rag.storage_guard import bounded_probe
        with patch('fleet_rag.storage_guard.os.name','nt'),patch('fleet_rag.storage_guard.subprocess.Popen') as launch:
            with self.assertRaises(OSError):bounded_probe(['probe'],1)
            launch.assert_not_called()
