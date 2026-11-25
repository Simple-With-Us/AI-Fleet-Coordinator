import secrets
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlencode
from simple_tools import operator_registry as OR
from simple_tools import review_origin_projection as RP
from simple_tools import listener_registry as RG

SID='sess_7e170443-7fad-4343-acfe-95a4c34377be'
class ZcodeOriginTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=str(Path(self.tmp.name)/'registry.db')
        OR.SessionRegistry(self.path).close_db()
        self.key=secrets.token_hex(24);self.codexkey=secrets.token_hex(24)
        self.service=RG.RegistryService(self.path,{},review_origin_read_key=self.codexkey,review_origin_zc_read_key=self.key,clock=lambda:1000)
    def call(self,session=SID,key=None,path=RP.ZC_PATH):
        query=urlencode({'native_session_id':session})
        return self.service.handle('GET',path+'?'+query,{'X-Simple-Tools-Timestamp':'1000','X-Simple-Tools-Signature':RP.signature(key or self.key,query,'1000',path=path)},b'')
    def test_actual_canonical_zcode_binding_and_credential_separation(self):
        identity=dict(platform='zcode',verified_host='server',profile_scope='/home/jay/.zcode',native_session_id=SID)
        row=OR.register_session(self.path,**identity,seat='ZC',source='test',issuer='OPERATOR')
        status,result=self.call();self.assertEqual(status,200)
        self.assertEqual(result['origin_session_claim'],SID)
        self.assertEqual(result['origin_letter_claim'],row['label'])
        self.assertEqual(result['origin_generation_claim'],row['generation'])
        self.assertEqual(self.call(key=self.codexkey)[0],401)
        self.assertEqual(self.call(path=RP.PATH)[0],401)
        OR.register_session(self.path,**dict(identity,verified_host='other'),seat='ZC',source='test',issuer='OPERATOR')
        self.assertEqual(self.call()[0],409)
    def test_no_guess_or_new_registry_and_no_reused_key(self):
        self.assertEqual(self.call()[0],409)
        self.assertEqual(self.call(session=SID.removeprefix('sess_'))[0],400)
        with self.assertRaises(ValueError): RG.RegistryService(self.path,{},review_origin_read_key=self.key,review_origin_zc_read_key=self.key)
        hidden=RG.RegistryService(self.path,{},clock=lambda:1000)
        self.assertEqual(hidden.handle('GET',RP.ZC_PATH,{},b'')[0],404)
