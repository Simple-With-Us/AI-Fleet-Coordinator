import unittest
from native_receipts import classify
class Tests(unittest.TestCase):
 def setUp(self):
  self.b={'session_id':'sess'};self.m={'session_id':'sess','sourceCommandId':'cmd','role':'user','text':'fixture','message_id':'m'};self.t={'session_id':'sess','sourceCommandId':'cmd','parent_message_id':'m','state':'completed'};self.f={'session_id':'sess','parent_message_id':'m','alert_id':123,'status':'completed','output_nonempty':True}
 def test_ack_alone_proves_nothing(self):self.assertEqual(classify(self.b,'cmd','fixture')['native_input'],'unverified')
 def test_persisted_only(self):self.assertEqual(classify(self.b,'cmd','fixture',self.m)['native_input'],'persisted')
 def test_wrong_text_refused(self):self.assertEqual(classify(self.b,'cmd','other',self.m)['native_input'],'unverified')
 def test_wrong_session_refused(self):self.m['session_id']='other';self.assertEqual(classify(self.b,'cmd','fixture',self.m)['native_input'],'unverified')
 def test_wrong_command_refused(self):self.assertEqual(classify(self.b,'other','fixture',self.m)['native_input'],'unverified')
 def test_turn_not_fetch(self):self.assertEqual(classify(self.b,'cmd','fixture',self.m,self.t)['owning_alert'],'unverified')
 def test_parameter_only_fetch_unverified(self):self.assertEqual(classify(self.b,'cmd','fixture',self.m,self.t,self.f,123)['owning_alert'],'unverified')
 def test_wrong_alert_refused(self):self.assertEqual(classify(self.b,'cmd','fixture',self.m,self.t,self.f,456)['owning_alert'],'unverified')
 def test_empty_output_refused(self):self.f['output_nonempty']=False;self.assertEqual(classify(self.b,'cmd','fixture',self.m,self.t,self.f,123)['owning_alert'],'unverified')
if __name__=='__main__':unittest.main(verbosity=2)
