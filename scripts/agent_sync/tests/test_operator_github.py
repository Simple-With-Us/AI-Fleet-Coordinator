import json
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
        self.router=Router();self.key=KEY;self.allowed=True;self.audit=[]
        self.broker=Broker(lambda seat:self.key if seat=='CODEX' else None,
            lambda seat:CallerPolicy('swu-codex',{'simple-with-us/ai-fleet-coordinator':{'contents':'read','pull_requests':'write','metadata':'read'}}) if self.allowed else None,
            self.router,lambda slug:{'login':slug+'[bot]','id':123},clock=lambda:1000,audit=lambda **kw:self.audit.append(kw),enabled=True)
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
        self.request()
        with self.assertRaises(Refused) as c:self.request()
        self.assertEqual(c.exception.status,409);self.assertEqual(len(self.router.calls),1)
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
        b=json.dumps({'repo':'jaywedgeworth22/Fleet-OPS','permissions':{'contents':'read'}}).encode()
        with self.assertRaises(Refused) as c:self.request(body=b)
        self.assertEqual(c.exception.status,403)
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
        self.broker.nonces={('CODEX',str(i)):2000 for i in range(m.MAX_NONCES)}
        with self.assertRaises(Refused) as c:self.request()
        self.assertEqual(c.exception.status,503)
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
