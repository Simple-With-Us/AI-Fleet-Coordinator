import datetime as dt
import tempfile
import threading
import unittest
from unittest import mock
from needs_owner import bar
from needs_owner.errors import BoardFailed,Refusal

class ReservationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.env={"HOME":self.tmp.name};self.now=dt.datetime.now(dt.timezone.utc);self.warn=[]
    def slot(self,seat="WISE",cap=1):
        return bar.Slot(bar.Policy("enforce",cap),self.env,seat,lambda:self.now,self.warn.append)
    def count(self,seat="WISE"):
        return bar.filings_in_window(self.env,seat,self.now.timestamp())
    def test_lock_is_free_during_io_and_other_seat_can_reserve(self):
        with self.slot() as first:
            first.check()
            with mock.patch.object(bar,"LOCK_WAIT_SECONDS",.05):
                with self.slot("OTHER") as second:second.check();second.record("AFC-2")
            first.record("AFC-1")
        self.assertEqual(self.count(),1);self.assertEqual(self.count("OTHER"),1)
    def test_last_slot_cannot_be_taken_twice(self):
        with self.slot() as first:
            first.check()
            with self.assertRaises(Refusal):
                with self.slot() as second:second.check()
            first.record("AFC-1")
        self.assertEqual(self.count(),1)
    def test_definite_no_write_failure_frees_reservation(self):
        with self.assertRaises(BoardFailed):
            with self.slot() as first:first.check();raise BoardFailed("denied",4,outcome_unknown=False)
        self.assertEqual(self.count(),0)
    def test_unknown_write_retains_reservation_and_lock_is_free(self):
        with self.assertRaises(BoardFailed):
            with self.slot() as first:first.check();raise BoardFailed("timeout",outcome_unknown=True)
        self.assertEqual(self.count(),1)
        with self.slot("OTHER") as second:second.check();second.record("AFC-2")
    def test_unexpected_error_conservatively_retains_reservation(self):
        with self.assertRaises(RuntimeError):
            with self.slot() as first:first.check();raise RuntimeError("unexpected")
        self.assertEqual(self.count(),1)
    def test_crashed_process_reservation_counts_until_window_expires(self):
        first=self.slot();first.__enter__();first.check()
        self.assertEqual(self.count(),1)
        self.now+=dt.timedelta(seconds=bar.WINDOW_SECONDS)
        self.assertEqual(self.count(),0)
    def test_duplicate_releases_without_count(self):
        with self.slot() as first:first.check()
        self.assertEqual(self.count(),0)
    def test_two_places_permit_two_in_flight_same_seat(self):
        with self.slot(cap=2) as a:
            a.check()
            with self.slot(cap=2) as b:b.check();b.record("AFC-2")
            a.record("AFC-1")
        self.assertEqual(self.count(),2)
    def test_reservation_write_failure_enforce_refuses_before_io(self):
        with mock.patch.object(bar.ledger,"write_private",side_effect=OSError):
            with self.assertRaises(Refusal):
                with self.slot() as a:a.check()
        self.assertEqual(self.count(),0)
    def test_finalize_failure_leaves_reservation_not_false_no_write(self):
        with self.slot() as a:
            a.check()
            with mock.patch.object(bar.ledger,"write_private",side_effect=OSError):a.record("AFC-1")
        self.assertEqual(self.count(),1)

    def test_storage_full_refuses_new_reservation_without_evicting_live_rows(self):
        with mock.patch.object(bar,"MAX_ROWS",2):
            with self.slot(cap=99) as first:
                first.check()
                with self.slot("OTHER",99) as second:
                    second.check()
                    before=bar._rows(self.env)
                    with self.assertRaises(Refusal):
                        with self.slot("THIRD",99) as third:third.check()
                    self.assertEqual(bar._rows(self.env),before)
                    second.record("AFC-2")
                first.record("AFC-1")
        self.assertEqual(len(bar._rows(self.env)),2)
        self.assertEqual(self.count(),1);self.assertEqual(self.count("OTHER"),1)
    def test_finalization_failure_warns_once_and_not_unknown_success(self):
        with self.slot() as a:
            a.check()
            with mock.patch.object(bar.ledger,"write_private",side_effect=OSError):a.record("AFC-1")
        self.assertEqual(len(self.warn),1)
        self.assertIn("finalize",self.warn[0]);self.assertNotIn("unknown",self.warn[0])
    def test_reservation_requests_fsync_and_fsync_error_refuses_before_io(self):
        real=bar.ledger.write_private
        with mock.patch.object(bar.ledger,"write_private",wraps=real) as write:
            with self.slot() as a:a.check();a.record("AFC-1")
        self.assertTrue(all(c.kwargs.get("durable") for c in write.call_args_list))
        with mock.patch.object(bar.ledger.os,"fsync",side_effect=OSError):
            with self.assertRaises(Refusal):
                with self.slot("OTHER") as a:a.check()
    def test_actual_durable_writer_syncs_file_then_directory(self):
        import os,stat
        calls=[];real=bar.ledger.os.fsync
        def sync(fd):
            calls.append("dir" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file")
            return real(fd)
        with mock.patch.object(bar.ledger.os,"fsync",side_effect=sync):
            with self.slot() as a:a.check();a.record("AFC-1")
        self.assertEqual(calls,["file","dir","file","dir"])

from .test_bar import BarCase,Q
from needs_owner import verbs

class PostWriteTests(BarCase):
    def test_definite_comment_failure_after_new_file_keeps_cap_count(self):
        w=self.world("enforce",1)
        def denied(*args,**kwargs):raise BoardFailed("comment denied",6)
        with mock.patch.object(verbs,"post_block_comment",side_effect=denied):
            failed=w.file(Q,"Yes","No",extra=("--why-owner","money"),env=w.env(FAKE_BOARD_DROP_FENCE="1"))
        self.assertEqual(failed.code,6,failed.err)
        self.assertEqual(len(w.board_state()["issues"]),1)
        self.assertEqual(bar.filings_in_window(w.env(),"CLAUDE",w.now.timestamp()),1)
        again=self.file("A different question?",why="money",w=w)
        self.assertEqual(again.code,2,again.err)
        self.assertEqual(len(w.board_state()["issues"]),1)
    def test_definite_read_failure_after_new_file_keeps_cap_count(self):
        w=self.world("enforce",1)
        with mock.patch.object(verbs.itemsmod,"fetch",side_effect=BoardFailed("read denied",6)):
            failed=self.file(why="money",w=w)
        self.assertEqual(failed.code,6,failed.err)
        self.assertEqual(len(w.board_state()["issues"]),1)
        self.assertEqual(bar.filings_in_window(w.env(),"CLAUDE",w.now.timestamp()),1)

    def test_rate_limit_after_file_output_keeps_created_issue_count(self):
        from needs_owner.proc import Runner,Result
        w=self.world("enforce",1)
        real=Runner.board
        def partial(runner,*args):
            result=real(runner,*args)
            if args[0]=="file" and result.ok:
                return Result(5,result.out,"attachment rate limit")
            return result
        with mock.patch.object(Runner,"board",partial):
            failed=self.file(why="money",w=w)
        self.assertEqual(failed.code,5,failed.err)
        self.assertEqual(len(w.board_state()["issues"]),1)
        self.assertEqual(bar.filings_in_window(w.env(),"CLAUDE",w.now.timestamp()),1)
