"""Hosted CLI transport tests: no real network or credentials."""
import contextlib
import io
import os
import unittest
from unittest import mock
from simple_tools import peer_actions_cli as C
from simple_tools import cli as T

class HostedTests(unittest.TestCase):
    def run_cli(self, extra=(), env=None, who="WISE", error=None, bad_receipt=False):
        calls=[]
        def call(e, tool, args, write=False):
            calls.append((tool,args,write))
            if error: raise T.RemoteError(error)
            row=["server time",who,args["requesting_seat"],args["request"],args["action_taken"],args["result"]]
            if bad_receipt: row[1]="OTHER"
            return {"structuredContent":{"dry_run":args["dry_run"],"tab":"Peer-Actions Registry","row":row}}
        with mock.patch.dict(os.environ, env if env is not None else {"AGENT_SEAT":"WISE", "SIMPLE_TOOLS_BEARER":"test-bearer"},clear=True), \
             mock.patch.object(T,"_remote_write_identity",return_value=who) as identity, \
             mock.patch.object(T,"_remote_call",side_effect=call), \
             mock.patch.object(C.P,"log_row",side_effect=AssertionError("direct fallback")), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            rc=C.main(["log","--hosted","--json","--requesting","ECHO","--request","source","--action","checked","--result","done",*extra])
        return rc,calls,output.getvalue(),identity
    def test_valid_write_uses_own_identity(self):
        rc,calls,out,_=self.run_cli();self.assertEqual(rc,0);self.assertEqual(len(calls),1)
        self.assertEqual(calls[0][0],"peer_actions_log");self.assertEqual(calls[0][1]["identity"],"WISE");self.assertTrue(calls[0][2])
    def test_dry_run_is_not_a_write(self):
        rc,calls,_,_=self.run_cli(["--dry-run"]);self.assertEqual(rc,0);self.assertFalse(calls[0][2])
    def test_other_bearer_never_logs(self):
        rc,calls,_,_=self.run_cli(who="ECHO");self.assertEqual(rc,2);self.assertEqual(calls,[])
    def test_launcher_override_refused_before_identity(self):
        rc,calls,_,identity=self.run_cli(["--acting","WISE"],{"AGENT_LAUNCH_SEAT":"ECHO"});self.assertEqual(rc,2);identity.assert_not_called()
    def test_missing_local_seat_never_guesses(self):
        rc,calls,_,identity=self.run_cli(env={"PATH":"/usr/bin"});self.assertEqual(rc,2);identity.assert_not_called()
    def test_unknown_outcome_never_retries_or_falls_back(self):
        rc,calls,out,_=self.run_cli(error="outcome unknown; check before retry");self.assertEqual(rc,5);self.assertEqual(len(calls),1);self.assertIn("unknown",out)
    def test_receipt_mismatch_stops(self):
        rc,calls,out,_=self.run_cli(bad_receipt=True);self.assertEqual(rc,5);self.assertEqual(len(calls),1)
    def test_multiline_refused_before_identity(self):
        rc,calls,_,identity=self.run_cli(["--request","bad\nline"]);self.assertEqual(rc,2);identity.assert_not_called()

    def test_bom_trim_matches_server_before_write(self):
        rc,calls,_,_=self.run_cli(["--request","\ufeff source \ufeff"])
        self.assertEqual(rc,0);self.assertEqual(calls[0][1]["request"],"source")
    def test_bom_only_field_refused_before_identity(self):
        rc,calls,_,identity=self.run_cli(["--request","\ufeff \ufeff"])
        self.assertEqual(rc,2);identity.assert_not_called()

    def test_missing_bearer_is_not_configured_before_network(self):
        for value in (None, "", "   "):
            env={"AGENT_SEAT":"WISE"}
            if value is not None: env["SIMPLE_TOOLS_BEARER"]=value
            rc,calls,out,identity=self.run_cli(env=env)
            self.assertEqual(rc,3);self.assertIn("not_configured",out)
            identity.assert_not_called();self.assertEqual(calls,[])

if __name__=="__main__": unittest.main()
