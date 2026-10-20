import unittest
from agent_sync.operator_github_secrets import NamedSecrets
from agent_sync.github_token_router import RoutingError

class Tests(unittest.TestCase):
    def setUp(self):
        self.calls=[]
        def read(path,name):self.calls.append((path,name));return 'mock'
        self.loader=NamedSecrets(lambda:None,['CODEX'],['swu-codex'],transport=read)
    def test_names_only(self):
        self.loader.caller_key('CODEX');self.loader.app_credentials('swu-codex')
        self.assertEqual(self.calls,[('/operator-github/callers','OPERATOR_GITHUB_CALLER_CODEX_HMAC'),('/github-apps','GH_APP_SWU_CODEX_ID'),('/github-apps','GH_APP_SWU_CODEX_PRIVATE_KEY')])
    def test_unknown_seat_no_read(self):
        self.assertIsNone(self.loader.caller_key('CLAUDE'));self.assertEqual(self.calls,[])
    def test_unknown_app_no_read(self):
        with self.assertRaises(RoutingError):self.loader.app_credentials('swu-echo')
        self.assertEqual(self.calls,[])
    def test_missing_bearer(self):
        with self.assertRaises(RoutingError):NamedSecrets(lambda:None,['CODEX'],[]).caller_key('CODEX')
