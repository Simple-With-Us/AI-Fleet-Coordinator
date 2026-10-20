import unittest
from unittest.mock import patch
from agent_sync.service_identity import operator_bot, operator_credentials, OPERATOR_BOT_ID, OPERATOR_EMAIL
from agent_sync import zulip as Z, identity as I, config as C

class Tests(unittest.TestCase):
    def test_exact_bot(self):self.assertTrue(operator_bot({'is_bot':True,'user_id':OPERATOR_BOT_ID,'email':OPERATOR_EMAIL}))
    def test_wrong_id(self):self.assertFalse(operator_bot({'is_bot':True,'user_id':1,'email':OPERATOR_EMAIL}))
    def test_old_admin(self):self.assertFalse(operator_bot({'is_bot':True,'user_id':OPERATOR_BOT_ID,'email':'admin-bot@simplewithus.zulipchat.com'}))
    def test_not_bot(self):self.assertFalse(operator_bot({'is_bot':False,'user_id':OPERATOR_BOT_ID,'email':OPERATOR_EMAIL}))
    def test_not_agent_roster(self):
        with self.assertRaises(I.UnknownSeat):I.strict_seat('OPERATOR')
    def test_credential_names(self):
        seen=[]
        def fetch(names):seen.extend(names);return {'ZULIP_OPERATOR_EMAIL':OPERATOR_EMAIL,'ZULIP_OPERATOR_API_KEY':'test-key'}
        c=operator_credentials({},fetch=fetch)
        self.assertEqual(c.email,OPERATOR_EMAIL);self.assertEqual(seen,['ZULIP_OPERATOR_EMAIL','ZULIP_OPERATOR_API_KEY'])
    def test_wrong_email_fails(self):
        with self.assertRaises(Z.CredentialError):operator_credentials({},fetch=lambda n:{'ZULIP_OPERATOR_EMAIL':'other@test','ZULIP_OPERATOR_API_KEY':'test'})
    def test_error_scrubbed(self):
        def fetch(n):raise ValueError('DO-NOT-LOG-SECRET')
        with self.assertRaises(Z.CredentialError) as c:operator_credentials({},fetch=fetch)
        self.assertNotIn('DO-NOT-LOG',str(c.exception))
    def test_no_env_fallback(self):
        from types import SimpleNamespace as NS
        with self.assertRaises(Z.CredentialError):C.seat_credentials(NS(seat='OPERATOR',creds='env'),{},None)

if __name__=='__main__':unittest.main()
