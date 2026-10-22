"""Every simple-chat command line open-loops builds is accepted by the REAL simple-chat argument parser.

The plan's trap:  `dm-read` has `--with NAME` and no `--owner`.  These tests import `agent_sync.cli.build_parser()`
and parse each line, so a flag simple-chat does not have fails here and not at 7:00am."""
import contextlib
import io
import unittest

from agent_sync import cli as agent_cli

from open_loops import send

EXE = "/x/simple-chat"


def parse(argv):
    return agent_cli.build_parser().parse_args(argv[1:])


class Parity(unittest.TestCase):
    def test_whoami(self):
        args = parse(send.argv_whoami(EXE))
        self.assertEqual(args.command, "whoami")
        self.assertTrue(args.json)

    def test_dm_read_uses_with_owner(self):
        args = parse(send.argv_dm_read(EXE))
        self.assertEqual((args.command, args.with_, args.limit, args.json), ("dm-read", ["owner"], 30, True))
        self.assertIn("--with", send.argv_dm_read(EXE))
        self.assertNotIn("--owner", send.argv_dm_read(EXE))

    def test_the_real_parser_really_does_refuse_dm_read_owner(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parse([EXE, "dm-read", "--owner", "--json"])

    def test_dm(self):
        args = parse(send.argv_dm(EXE, "/tmp/f"))
        self.assertEqual((args.command, args.owner, args.body_file, args.session, args.text), ("dm", True, "/tmp/f", "openloop", []))

    def test_post_the_heartbeat(self):
        argv = send.argv_post(EXE, send.heartbeat_topic("FFX-100"), "/tmp/f")
        args = parse(argv)
        self.assertEqual((args.command, args.topic, args.session, args.body_file, args.to, args.fleet), (
            "post", "FFX-100 open-loops heartbeat", "openloop", "/tmp/f", [], False))

    def test_read_topic(self):
        args = parse(send.argv_read_topic(EXE, "FFX-100 open-loops heartbeat"))
        self.assertEqual((args.command, args.topic, args.limit, args.include_self), ("read", "FFX-100 open-loops heartbeat", 10, True))

    def test_the_session_label_fits_simple_chats_eight_character_cut(self):
        self.assertLessEqual(len(send.SESSION), 8)

    def test_the_gatherers_reads_parse_too(self):
        from open_loops import zulip_topics
        self.assertEqual(parse(zulip_topics.argv_topics(EXE)).command, "topics")
        args = parse(zulip_topics.argv_read(EXE, "FFX-1 x"))
        self.assertEqual((args.command, args.limit, args.include_self), ("read", zulip_topics.READ_LIMIT, True))


if __name__ == "__main__":
    unittest.main()
