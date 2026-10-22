import json
import tempfile
from pathlib import Path
from agent_sync.operator_github_state import LedgerError, NonceLedger
import threading
import unittest
import urllib.request
import urllib.error
from agent_sync.operator_github import Broker, BrokerHttp, CallerPolicy, Refused, signature
from agent_sync.operator_github_client import bound_command, ClientError, child_environment

KEY='a'*64
class Router:
    def __init__(self):self.calls=[]
    def token_for(self,*args):self.calls.append(args);return 'MOCK-TOKEN-NOT-A-CREDENTIAL'

class BrokerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.ledger=NonceLedger(Path(self.temp.name)/"replay")
        self.router=Router();self.key=KEY;self.allowed=True;self.audit=[]
        self.broker=Broker(lambda seat:self.key if seat=='CODEX' else None,
            lambda seat:CallerPolicy('swu-codex',{'simple-with-us/ai-fleet-coordinator':{'contents':'read','pull_requests':'write','metadata':'read'}}) if self.allowed else None,
            self.router,lambda slug:{'login':slug+'[bot]','id':123},clock=lambda:1000,audit=lambda **kw:self.audit.append(kw),enabled=True,nonce_ledger=self.ledger)
        self.path='/operator/v1/github/CODEX/token';self.body=json.dumps({'repo':'Simple-With-Us/AI-Fleet-Coordinator','permissions':{'contents':'read'}}).encode()
    def request(self,nonce='1'*64,body=None,stamp='1000',sig=None):
        b=self.body if body is None else body
        return self.broker.issue(self.path,stamp,nonce,sig or signature(KEY,self.path,stamp,nonce,b),b)
    def test_disabled(self):
        self.broker.enabled=False
        with self.assertRaises(Refused) as c:self.request()
        self.assertEqual(c.exception.status,404)
    def test_issue_bound_app(self):
        r=self.request();self.assertEqual(r['app_slug'],'swu-codex');self.assertEqual(self.router.calls[0][0],'swu-codex')
        self.assertFalse(any('token' in x for x in self.audit))
    def test_bad_signature(self):
        with self.assertRaises(Refused) as c:self.request(sig='0'*64)
        self.assertEqual(c.exception.status,401);self.assertEqual(self.router.calls,[])
    def test_replay(self):
        first=self.request()
        again=self.request()
        # Idempotency: a lost first response re-mints against the recorded
        # request; the ledger stores metadata only, never the token.
        self.assertEqual(again['token'],first['token']);self.assertEqual(len(self.router.calls),2)
        import sqlite3
        con=sqlite3.connect(self.ledger.path)
        stored=con.execute("select response from nonces").fetchone()[0];con.close()
        self.assertNotIn('MOCK-TOKEN',stored);self.assertIn('swu-codex',stored)
    def test_stale(self):
        with self.assertRaises(Refused):self.request(stamp='939')
    def test_future(self):
        with self.assertRaises(Refused):self.request(stamp='1061')
    def test_signed_body_cannot_change(self):
        sig=signature(KEY,self.path,'1000','1'*64,self.body)
        with self.assertRaises(Refused):self.request(body=self.body+b' ',sig=sig)
    def test_revocation_on_cache_hit(self):
        self.request();self.key=None
        with self.assertRaises(Refused):self.request(nonce='2'*64)
        self.assertEqual(len(self.router.calls),1)
    def test_policy_on_cache_hit(self):
        self.request();self.allowed=False
        with self.assertRaises(Refused):self.request(nonce='2'*64)
        self.assertEqual(len(self.router.calls),1)
    def test_other_seat(self):
        self.path='/operator/v1/github/CLAUDE/token'
        with self.assertRaises(Refused):self.request()
        self.assertEqual(self.router.calls,[])
    def test_cross_repo(self):
        b=json.dumps({'repo':'acme-owner/road-runner','permissions':{'contents':'read'}}).encode()
        with self.assertRaises(Refused) as c:self.request(body=b)
        self.assertEqual(c.exception.status,403)
    def test_credential_shaped_target_refused(self):
        b=json.dumps({'repo':'Simple-With-Us/ghp_dGhpc2lzYWZha2V0b2tlbg','permissions':{'contents':'read'}}).encode()
        with self.assertRaises(Refused) as c:self.request(body=b)
        self.assertEqual(c.exception.status,400)
        self.assertEqual(self.router.calls,[])
    def test_pull_requests_read_issues(self):
        b=json.dumps({'repo':'Simple-With-Us/AI-Fleet-Coordinator','permissions':{'pull_requests':'read'}}).encode()
        self.request(body=b)
        # permission_set adds the implicit metadata:read, as the GitHub API does
        self.assertEqual(self.router.calls[-1][2],{'pull_requests':'read','metadata':'read'})
    def test_above_permissions(self):
        b=json.dumps({'repo':'Simple-With-Us/AI-Fleet-Coordinator','permissions':{'contents':'write'}}).encode()
        with self.assertRaises(Refused):self.request(body=b)
    def test_client_app_selection_rejected(self):
        b=json.loads(self.body);b['app_slug']='swu-echo'
        with self.assertRaises(Refused):self.request(body=json.dumps(b).encode())
    def test_duplicate_fields(self):
        with self.assertRaises(Refused):self.request(body=b'{"repo":"a/b","repo":"c/d","permissions":{"contents":"read"}}')
    def test_capacity(self):
        from agent_sync import operator_github as m
        from unittest.mock import patch
        self.addCleanup(patch.stopall)
        patch.object(m, 'MAX_NONCES', 1).start()
        self.request(nonce='2'*64)
        with self.assertRaises(Refused) as c:self.request()
        self.assertEqual(c.exception.status,503)
    def test_restart_replay(self):
        first=self.request()
        self.broker.nonce_ledger=NonceLedger(Path(self.temp.name)/'replay')
        again=self.request()
        self.assertEqual(again['token'],first['token']);self.assertEqual(len(self.router.calls),2)
    def test_definite_provider_rejection_releases_nonce(self):
        from agent_sync.github_token_router import RoutingError
        calls=self.router.calls
        def fail_once(*args):
            calls.append(args)
            if len(calls)==1:raise RoutingError('GitHub request failed, HTTP 401', status=401)
            return 'MOCK-TOKEN-NOT-A-CREDENTIAL'
        self.router.token_for=fail_once
        with self.assertRaises(Refused) as c:self.request()
        self.assertEqual(c.exception.status,502)
        result=self.request()
        self.assertEqual(result['token'],'MOCK-TOKEN-NOT-A-CREDENTIAL')
    def test_unknown_outcome_refuses_same_nonce(self):
        from agent_sync.github_token_router import RoutingError
        def boom(*args):raise RoutingError('GitHub transport or response failed')
        self.router.token_for=boom
        with self.assertRaises(Refused) as c:self.request()
        self.assertEqual(c.exception.status,502)
        self.router.token_for=lambda *args:'MOCK-TOKEN-NOT-A-CREDENTIAL'
        with self.assertRaises(Refused) as c:self.request()
        self.assertEqual(c.exception.status,409)
        self.assertEqual(c.exception.status,409)
    def test_no_store_fails_closed(self):
        self.broker.nonce_ledger=None
        with self.assertRaises(Refused) as c:self.request()
        self.assertEqual(c.exception.status,503)
        self.assertEqual(self.router.calls,[])
    def test_http_no_auth(self):
        server=BrokerHttp(self.broker);server.start();self.addCleanup(server.close)
        url='http://%s:%s%s'%(*server.address,self.path)
        with self.assertRaises(urllib.error.HTTPError) as c:urllib.request.urlopen(urllib.request.Request(url,data=self.body,headers={'Content-Type':'application/json'}))
        self.assertEqual(c.exception.code,400)
    def test_http_signed(self):
        server=BrokerHttp(self.broker);server.start();self.addCleanup(server.close)
        url='http://%s:%s%s'%(*server.address,self.path)
        req=urllib.request.Request(url,data=self.body,headers={'Content-Type':'application/json','X-Operator-Timestamp':'1000','X-Operator-Nonce':'1'*64,'X-Operator-Signature':signature(KEY,self.path,'1000','1'*64,self.body)})
        with urllib.request.urlopen(req) as r:self.assertEqual(json.load(r)['app_slug'],'swu-codex')

class ClientTests(unittest.TestCase):
    def test_bind_list(self):self.assertEqual(bound_command(['gh','pr','list'],'a/b'),['gh','pr','list','--repo','a/b'])
    def test_cross_repo_option_rejected(self):
        with self.assertRaises(ClientError):bound_command(['gh','pr','list','--repo','c/d'],'a/b')
    def test_api_rejected(self):
        with self.assertRaises(ClientError):bound_command(['gh','api','/user'],'a/b')
    def test_remote_pr_url_rejected(self):
        with self.assertRaises(ClientError):bound_command(['gh','pr','view','https://github.com/c/d/pull/1'],'a/b')
    def test_write_command_rejected(self):
        with self.assertRaises(ClientError):bound_command(['gh','pr','merge','1'],'a/b')
    def test_identity_environment(self):
        result={'token':'MOCK','git_author_name':'swu-codex[bot]','git_author_email':'123+swu-codex[bot]@users.noreply.github.com'}
        env=child_environment({'GH_TOKEN':'old','GITHUB_TOKEN':'old','GIT_CONFIG_COUNT':'9','GIT_AUTHOR_NAME':'Jay','PATH':'/usr/bin'},result)
        self.assertNotIn('GITHUB_TOKEN',env);self.assertNotIn('GIT_CONFIG_COUNT',env)
        self.assertEqual(env['GIT_AUTHOR_NAME'],'swu-codex[bot]')

if __name__=='__main__':unittest.main()

class ReviewFixTests(unittest.TestCase):
    """Regression tests for the second Kody review round on 19ddfb2a."""

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.ledger=NonceLedger(Path(self.temp.name)/"replay")
        self.router=Router();self.audit=[]
        # The policy stub mirrors load_policy:  None for seats outside the
        # allowlist, so the broker can tell an unknown seat (401, no oracle)
        # from a policy-known seat whose secret cannot be resolved (503).
        self.broker=Broker(lambda seat:'a'*64 if seat=='CODEX' else None,
            lambda seat:CallerPolicy('swu-codex',{'example-org/widget-repo':{'contents':'read','metadata':'read'}}) if seat=='CODEX' else None,
            self.router,lambda slug:{'login':slug+'[bot]','id':123},clock=lambda:1000,audit=lambda **kw:self.audit.append(kw),enabled=True,nonce_ledger=self.ledger)
        self.path='/operator/v1/github/CODEX/token';self.body=json.dumps({'repo':'example-org/widget-repo','permissions':{'contents':'read'}}).encode()
    def request(self,nonce='1'*64):
        return self.broker.issue(self.path,'1000',nonce,signature('a'*64,self.path,'1000',nonce,self.body),self.body)
    def test_unknown_seat_is_401_not_a_provisioning_oracle(self):
        self.path='/operator/v1/github/WISE/token'
        body=json.dumps({'repo':'example-org/widget-repo','permissions':{'contents':'read'}}).encode()
        with self.assertRaises(Refused) as c:
            self.broker.issue(self.path,'1000','9'*64,signature('a'*64,self.path,'1000','9'*64,body),body)
        self.assertEqual(c.exception.status,401)
        self.assertEqual(c.exception.reason,'authentication failed')
    def test_unresolvable_key_for_policy_seat_is_503_naming_variable(self):
        # A policy-known seat whose caller secret the adapter cannot resolve
        # is a provisioning outage, not an authentication failure.
        self.broker.key_for=lambda seat:None
        with self.assertRaises(Refused) as c:
            self.request()
        self.assertEqual(c.exception.status,503)
        self.assertIn('OPERATOR_GITHUB_CALLER_CODEX_HMAC',c.exception.reason)
    def test_malformed_key_is_503_naming_per_seat_variable(self):
        self.broker.key_for=lambda seat:'tooshort'
        with self.assertRaises(Refused) as c:
            self.request()
        self.assertEqual(c.exception.status,503)
        self.assertIn('OPERATOR_GITHUB_CALLER_CODEX_HMAC',c.exception.reason)
    def test_claimed_replay_blocks_a_second_replay(self):
        first=self.request()
        # A concurrent replay claims the ok row first;  the broker's own replay must refuse.
        claimed=self.ledger.begin('CODEX','1'*64,1061,1000,100)
        self.assertEqual(claimed[0],'repeat-claimed')
        with self.assertRaises(Refused) as c:self.request()
        self.assertEqual(c.exception.status,409)
        self.assertEqual(first['token'],'MOCK-TOKEN-NOT-A-CREDENTIAL')
    def test_replay_restores_row_when_policy_now_denies(self):
        self.request()
        self.broker.policy_for=lambda seat:None
        with self.assertRaises(Refused) as c:self.request()
        self.assertEqual(c.exception.status,403)
        import sqlite3
        con=sqlite3.connect(self.ledger.path)
        outcome=con.execute("select outcome from nonces").fetchone()[0];con.close()
        self.assertEqual(outcome,'ok')
    def test_replay_definite_rejection_releases_nonce(self):
        from agent_sync.github_token_router import RoutingError
        self.request()
        def fail(*args):raise RoutingError('GitHub request failed, HTTP 401',status=401)
        self.router.token_for=fail
        with self.assertRaises(Refused) as c:self.request()
        self.assertEqual(c.exception.status,502)
        self.router.token_for=Router().token_for
        self.assertEqual(self.request()['token'],'MOCK-TOKEN-NOT-A-CREDENTIAL')
    def test_settle_failure_audits_outcome_unknown(self):
        self.request()
        def boom(*args,**kw):raise LedgerError('replay storage unavailable')
        self.ledger.settle=boom
        with self.assertRaises(LedgerError):self.request(nonce='2'*64)
        self.assertEqual(self.audit[-1]['outcome'],'outcome_unknown')

class CallerKeyTests(unittest.TestCase):
    def test_env_var_is_used_first(self):
        import os
        from unittest.mock import patch
        from agent_sync.operator_github_client import caller_key
        with patch.dict(os.environ,{'OPERATOR_GITHUB_CALLER_ECHO_HMAC':'e'*32}):
            with patch('agent_sync.operator_github_client.subprocess.run') as run:
                self.assertEqual(caller_key('ECHO'),'e'*32)
                run.assert_not_called()
    def test_fleet_secrets_fallback(self):
        import os
        from unittest.mock import patch
        from agent_sync.operator_github_client import caller_key
        class Done:
            returncode=0
            stdout=b'f'*32+b'\n'
        with patch.dict(os.environ,{},clear=True):
            with patch('agent_sync.operator_github_client.subprocess.run',return_value=Done()) as run:
                self.assertEqual(caller_key('ECHO'),'f'*32)
                args=run.call_args[0][0]
                self.assertEqual(args,['fleet-secrets','get','OPERATOR_GITHUB_CALLER_ECHO_HMAC'])
    def test_missing_everywhere_names_variable_not_value(self):
        import os
        from unittest.mock import patch
        from agent_sync.operator_github_client import caller_key
        class Done:
            returncode=3
            stdout=b''
        with patch.dict(os.environ,{},clear=True):
            with patch('agent_sync.operator_github_client.subprocess.run',return_value=Done()):
                with self.assertRaises(ClientError) as c:caller_key('ECHO')
        self.assertIn('OPERATOR_GITHUB_CALLER_ECHO_HMAC',str(c.exception))
    def test_short_key_refused(self):
        import os
        from unittest.mock import patch
        from agent_sync.operator_github_client import caller_key
        with patch.dict(os.environ,{'OPERATOR_GITHUB_CALLER_ECHO_HMAC':'short'}):
            with self.assertRaises(ClientError):caller_key('ECHO')
    def test_empty_env_var_falls_back_to_fleet_secrets(self):
        import os
        from unittest.mock import patch
        from agent_sync.operator_github_client import caller_key
        class Done:
            returncode=0
            stdout=b'g'*32+b'\n'
        with patch.dict(os.environ,{'OPERATOR_GITHUB_CALLER_ECHO_HMAC':''}):
            with patch('agent_sync.operator_github_client.subprocess.run',return_value=Done()) as run:
                self.assertEqual(caller_key('ECHO'),'g'*32)
                run.assert_called_once()

class RunBoundedTests(unittest.TestCase):
    def test_missing_executable_exits_127(self):
        from agent_sync.operator_github_client import run_bounded
        def popen(*a,**k):raise FileNotFoundError()
        with self.assertRaises(SystemExit) as c:run_bounded(['gh'],{},popen=popen)
        self.assertEqual(c.exception.code,127)
    def test_unexpected_wait_failure_reaps_child(self):
        import subprocess as sp
        from agent_sync.operator_github_client import run_bounded
        class FakeProc:
            def __init__(self):self.terminated=False
            def poll(self):return None
            def wait(self,timeout=None):raise RuntimeError('wait broke')
            def terminate(self):self.terminated=True
        proc=FakeProc()
        with self.assertRaises(RuntimeError):
            run_bounded(['gh'],{},popen=lambda *a,**k:proc)
        self.assertTrue(proc.terminated)
    def test_success_returns_status(self):
        from agent_sync.operator_github_client import run_bounded
        class FakeProc:
            def poll(self):return 0
            def wait(self,timeout=None):return 0
        self.assertEqual(run_bounded(['gh'],{},popen=lambda *a,**k:FakeProc()),0)
