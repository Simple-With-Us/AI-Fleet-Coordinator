import tempfile, unittest, pathlib, time
from unittest import mock
from simple_tools import wake_router as WR, zcode_wake as ZC

SID="sess_aaaa"
def row(gen=2, prs=({"repo":"o/r","number":1,"generation":2},)):
    return {"native_session_id":SID,"seat":"ZC","label":"II","generation":gen,"verified_host":"server","bound_prs":list(prs)}

class Harness:
    def __init__(self, cap=8, answer=None, turn=None):
        self.dir=pathlib.Path(tempfile.mkdtemp())
        self.answer=answer or {"found":True,**row()}
        self.calls=[]
        cfg=dict(WR.ROUTER_DEFAULTS,delivery_enabled=True,turn_max_wakes=cap)
        self.turns=[];self.singles=[]
        def dt(t,h,r,tid):
            self.turns.append((h.wake_id,[w.wake_id for w in r]))
            if turn: return turn(r)
            ids=tuple(w.wake_id for w in r)
            return WR.Turn(WR.Delivery("sent","resume",ack="unknown"),carried=ids,confirmed=ids)
        def d(t,w):
            self.singles.append(w.wake_id);return WR.Delivery("sent","resume")
        plat=WR.Platform("ZC","zcode","ZCode",ZC.SESSION_RE,d,deliver_turn=dt)
        self.r=WR.Router(plat,{},cfg,self.dir,lambda:("k"*40,""),never_ran=ZC.NEVER_RAN)
        self.t=time.time()
    def add(self,wid,msg,**kw):
        w=WR.Wake(wake_id=wid,message_id=msg,seat="ZC",required_session=SID,require_letter="II",require_generation=2,require_pr=kw.get("pr","o/r#1"))
        self.r.accept(w)
        tgt=WR.Target(SID,"II","server","","",2)
        pick=WR.Pick(target=tgt,picked="required-session",reason="required-session",link_note=WR.LINK_NOTES["required-session"])
        from dataclasses import asdict
        e=self.r._entries[wid];e["pick"]=asdict(pick);e["accepted_at"]=self.t+len(self.r._entries)*0.001
    def run(self):
        with mock.patch.object(WR.registry_api,"call",lambda *a,**k:self.answer):
            while self.r.run_once(): pass
    def status(self,wid):
        return (self.r.receipt(wid) or {})

class T(unittest.TestCase):
    def test_coalesces_contract_wakes_into_turn(self):
        h=Harness();[h.add("w%d"%i,100+i) for i in range(10)];h.run()
        self.assertEqual(h.turns[0],("w0",["w%d"%i for i in range(1,8)]))
        self.assertEqual(len(h.turns),2)  # 8 + 2
        for i in range(10): self.assertEqual(h.status("w%d"%i).get("status"),"delivered")
        self.assertEqual(h.singles,[])
    def test_cap1_is_off(self):
        h=Harness(cap=1);[h.add("w%d"%i,100+i) for i in range(3)];h.run()
        self.assertEqual(h.turns,[]);self.assertEqual(h.singles,["w0","w1","w2"])
    def test_rider_with_other_pr_unbound_stops_run(self):
        h=Harness();h.add("a",1);h.add("b",2);h.add("c",3,pr="o/r#9");h.add("d",4);h.run()
        self.assertEqual(h.turns[0],("a",["b"]))  # c unbound cuts the run; d cannot jump c
        self.assertNotIn("c",[x for t in h.turns for x in [t[0],*t[1]]])
    def test_stale_generation_blocks_all(self):
        h=Harness(answer={"found":True,**row(gen=3)});h.add("a",1);h.add("b",2);h.run()
        self.assertEqual(h.turns,[])
    def test_unknown_turn_closes_riders_unknown_no_redrive(self):
        h=Harness(turn=lambda r:WR.Turn(WR.Delivery("unknown","resume",code="resume-timeout"),unreadable=True))
        h.add("a",1);h.add("b",2);h.add("c",3);h.run()
        self.assertEqual(len(h.turns),1)
        for w in "abc": self.assertEqual(h.status(w).get("status"),"unknown")
        self.assertEqual(h.singles,[])
    def test_no_echo_riders_go_back_not_redriven_as_unknown(self):
        h=Harness(turn=lambda r:WR.Turn(WR.Delivery("sent","resume"),carried=(),confirmed=()))
        h.add("a",1);h.add("b",2);h.run()
        self.assertEqual(h.turns[0],("a",["b"]))
        self.assertEqual(h.status("a").get("status"),"delivered")
    def test_distinct_message_ids_preserved(self):
        h=Harness();h.add("a",11);h.add("b",12);h.run()
        self.assertEqual(h.status("a")["turn"]["message_ids"],[11,12])

class ZCTurn(unittest.TestCase):
    def test_platform_declares_turn(self):
        p=ZC.router_platform({},{"workspace":"/x"});self.assertTrue(callable(p.deliver_turn))
    def test_prompt_names_members(self):
        h=WR.Wake("w0",message_id=5,reason="required-session",picked="required-session",link_note=WR.LINK_NOTES["required-session"])
        r=WR.Wake("w1",message_id=6,reason="required-session",picked="required-session",link_note=WR.LINK_NOTES["required-session"])
        got={}
        def fake(env,cfg,sid,prompt,to,**k):
            got.update(p=prompt,to=to);return WR.Delivery("sent","resume",note="zcode-exit-0")
        with mock.patch.object(ZC,"_deliver_prompt",fake):
            t=ZC.deliver_turn_local({},{"resume_timeout_seconds":900},WR.Target(SID,"II","server","","",2),h,(r,),"t-0123456789abcdef")
        self.assertIn("w0",got["p"]);self.assertIn("w1",got["p"]);self.assertEqual(got["to"],1800)
        self.assertEqual(t.carried,("w1",));self.assertEqual(t.confirmed,())
    def test_unknown_and_refused_unreadable(self):
        h=WR.Wake("w0");r=WR.Wake("w1")
        for out in("unknown","refused"):
            with mock.patch.object(ZC,"_deliver_prompt",lambda *a,**k:WR.Delivery(out,"resume",code="x")):
                t=ZC.deliver_turn_local({},{},WR.Target(SID,"II","server","","",2),h,(r,),"t-0123456789abcdef")
            self.assertTrue(t.unreadable)
    def test_sent_turn_never_confirms_riders(self):
        with mock.patch.object(ZC,"_deliver_prompt",lambda *a,**k:WR.Delivery("sent","resume")):
            t=ZC.deliver_turn_local({},{},WR.Target(SID,"II","server","","",2),WR.Wake("w0"),(WR.Wake("w1"),),"t-0123456789abcdef")
        self.assertEqual(t.carried,("w1",));self.assertEqual(t.confirmed,())
    def test_zc_riders_close_unknown_not_delivered(self):
        h=Harness(turn=lambda r:WR.Turn(WR.Delivery("sent","resume"),carried=tuple(w.wake_id for w in r),confirmed=()))
        h.add("a",1);h.add("b",2);h.add("c",3);h.run()
        self.assertEqual(h.status("a")["status"],"delivered")
        for w in "bc": self.assertEqual(h.status(w)["status"],"unknown");self.assertEqual(h.status(w)["code"],"turn-unconfirmed")
    def test_busy_failed_not_unreadable(self):
        for out in("busy","failed"):
            with mock.patch.object(ZC,"_deliver_prompt",lambda *a,**k:WR.Delivery(out,"resume",code="x")):
                t=ZC.deliver_turn_local({},{},WR.Target(SID,"II","server","","",2),WR.Wake("w0"),(WR.Wake("w1"),),"t-0123456789abcdef")
            self.assertFalse(t.unreadable);self.assertIsNone(t.carried)
if __name__=="__main__":unittest.main()
