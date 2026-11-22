import unittest,tempfile,os,json
from unittest import mock
from pathlib import Path
from simple_tools import wake_router as WR
from simple_tools.tests.test_wake_router import Http,SID_A,SID_B,row

class ServiceCredential(Http):
    def setUp(self):
        super().setUp()
        self.service_key='service-'+'x'*48
    def setup_service(self):
        r=self.router();r._review_key=lambda:(self.service_key,'');return r,self.serve(r)
    def service_body(self,**over):
        return self.body(wake_id='ra-one',require_session=SID_A,require_letter='AA',require_generation=1,require_pr='Simple-With-Us/HogHunter#7',channel='review-alerts',**over)
    def test_service_exact_wake_is_durable_and_receipt_scoped(self):
        r,p=self.setup_service();status,a=self.request(p,body=self.service_body(),key=self.service_key)
        self.assertEqual(status,202);self.assertEqual(a['credential_scope'],'review-alerts')
        self.assertEqual(self.request(p,'/receipt/CLAUDE/ra-one',key=self.service_key,method='GET')[0],200)
        self.assertEqual(r._entries['ra-one']['wake']['credential_scope'],'review-alerts')
        r.accept(self.wake('legacy'))
        self.assertEqual(self.request(p,'/receipt/CLAUDE/legacy',key=self.service_key,method='GET')[0],404)
    def test_service_missing_constraints_refused(self):
        r,p=self.setup_service()
        for field in ['require_session','require_letter','require_generation','require_pr','channel']:
            b=self.service_body();b.pop(field)
            self.assertEqual(self.request(p,body=b,key=self.service_key)[0],403,field)
        self.assertFalse(r._entries)
    def test_scope_spoofing_ignored(self):
        r,p=self.setup_service();b=self.service_body();b['wake_id']='legacy';b['credential_scope']='review-alerts'
        self.assertEqual(self.request(p,body=b,key=self.key)[0],202)
        self.assertEqual(r._entries['legacy']['wake']['credential_scope'],'')
    def test_routine_cannot_reserve_service_namespace(self):
        r,p=self.setup_service();self.assertEqual(self.request(p,body=self.service_body(),key=self.key)[0],403)
        self.assertFalse(r._entries)
    def test_service_test_wake_reserves_nothing(self):
        r,p=self.setup_service();b=self.service_body();b['test']=True
        status,a=self.request(p,body=b,key=self.service_key);self.assertEqual(status,200);self.assertTrue(a['test']);self.assertFalse(r._entries);self.assertFalse(r._pending('ra-one').exists());self.assertFalse(self.delivered)
    def test_wrong_key_cannot_submit(self):
        r,p=self.setup_service();self.assertEqual(self.request(p,body=self.service_body(),key='wrong')[0],401)
    def test_key_collision_refuses_service(self):
        r,p=self.setup_service();r._review_key=lambda:(self.key,'')
        self.assertEqual(self.request(p,body=self.service_body(),key=self.key)[0],503)
    def test_service_works_without_routine_key(self):
        r,p=self.setup_service();self.key_answer=('','missing')
        self.assertEqual(self.request(p,body=self.service_body(),key=self.service_key)[0],202)
    def test_persisted_terminal_scope_survives_restart(self):
        self.registry.rows=[row(SID_A,"AA","work",generation=1,bound_prs=[{"repo":"Simple-With-Us/HogHunter","number":7,"generation":1}])]
        r,p=self.setup_service();self.request(p,body=self.service_body(),key=self.service_key);self.drain(r)
        self.assertEqual(self.done('ra-one')['credential_scope'],'review-alerts')
        fresh=self.router();fresh._review_key=lambda:(self.service_key,'');p2=self.serve(fresh)
        self.assertEqual(self.request(p2,'/receipt/CLAUDE/ra-one',key=self.service_key,method='GET')[0],200)

class ProtectedFile(unittest.TestCase):
    def test_protected_file_and_rotation(self):
        with tempfile.TemporaryDirectory()as d:
            p=Path(d)/'credential';p.write_text('a'*48);p.chmod(0o600);env={'WAKE_ROUTER_REVIEW_ALERTS_CREDENTIAL_FILE':str(p)}
            self.assertEqual(WR.review_alert_key(env),('a'*48,''));p.write_text('b'*48);self.assertEqual(WR.review_alert_key(env),('b'*48,''))
            p.chmod(0o644);self.assertFalse(WR.review_alert_key(env)[0]);p.chmod(0o600)
            q=Path(d)/'link';q.symlink_to(p);self.assertFalse(WR.review_alert_key({'WAKE_ROUTER_REVIEW_ALERTS_CREDENTIAL_FILE':str(q)})[0])
    def test_unconfigured_or_relative_refused(self):
        self.assertFalse(WR.review_alert_key({})[0]);self.assertFalse(WR.review_alert_key({'WAKE_ROUTER_REVIEW_ALERTS_CREDENTIAL_FILE':'relative'})[0])

    def test_missing_unreadable_shape_and_directory(self):
        with tempfile.TemporaryDirectory()as d:
            env={"WAKE_ROUTER_REVIEW_ALERTS_CREDENTIAL_FILE":str(Path(d)/"missing")}
            self.assertEqual(WR.review_alert_key(env)[1],"credential-unavailable")
            p=Path(d)/"credential";p.write_text("bad");p.chmod(0o600)
            env["WAKE_ROUTER_REVIEW_ALERTS_CREDENTIAL_FILE"]=str(p)
            self.assertEqual(WR.review_alert_key(env)[1],"credential-shape")
            with mock.patch.object(WR.os,"open",side_effect=PermissionError):
                self.assertEqual(WR.review_alert_key(env)[1],"credential-unavailable")
            env["WAKE_ROUTER_REVIEW_ALERTS_CREDENTIAL_FILE"]=d
            self.assertEqual(WR.review_alert_key(env)[1],"credential-custody")
