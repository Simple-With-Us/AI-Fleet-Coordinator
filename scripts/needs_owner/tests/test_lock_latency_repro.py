import threading,time
from unittest import mock
from needs_owner import bar, verbs
from .test_bar import BarCase

class LatencyRepro(BarCase):
    def test_slow_network_pins_other_seat_without_exhausted_cap(self):
        w=self.world("enforce",5)
        entered=threading.Event();done=[];real=verbs.file_new
        def slow(*args,**kwargs):
            entered.set();time.sleep(.6)
            return real(*args,**kwargs)
        def first():done.append(self.file("First question?",why="money",w=w))
        with mock.patch.object(bar,"LOCK_WAIT_SECONDS",.08),mock.patch.object(bar,"LOCK_POLL_SECONDS",.01),mock.patch.object(verbs,"file_new",slow):
            t=threading.Thread(target=first);t.start();self.assertTrue(entered.wait(2))
            second=self.file("Other seat question?",why="money",w=w,seat="WISE")
            t.join(10)
        self.assertEqual(second.code,0,second.err)
        self.assertNotIn("filings lock: timeout",second.err)
        self.assertEqual(len(done),1);self.assertEqual(done[0].code,0,done[0].err)
        self.assertEqual(bar.filings_in_window(w.env(),"WISE",w.now.timestamp()),1)
