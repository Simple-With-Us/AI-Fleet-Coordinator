"""send:  the seat check, the lint, the duplicate check, the DM and the heartbeat, against a fake agent-sync that
runs every command line through the real agent-sync argument parser."""
import json
import os
import unittest
from io import StringIO
from unittest import mock

from open_loops import send
from open_loops.facts import build_facts

from .builders import NOW, SCRIPTS, Sandbox, install_shim, write_scenario
from .scenes import make_dm, standard_world

C2 = "FFX-100"
ME = 77
WHOAMI = {"full_name": "BF Deployer", "user_id": ME, "is_bot": True, "seat": "BF-DEPLOYER", "bot_seat": "BF-DEPLOYER", "verified": True, "problem": None}


class SendTest(Sandbox):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(os.environ, {"OL_SCENARIO": str(self.root / "scenario.json"), "OL_LOG": str(self.log), "OL_SCRIPTS": str(SCRIPTS)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.facts = build_facts(run=standard_world(self.home, self.bin).runner(), now=NOW, sleep=lambda s: None)
        install_shim(self.bin, "agent-sync", "agent-sync")                  # after the scripted world's placeholder programs
        self.facts_file = self.write("facts.json", json.dumps(self.facts))
        self.dm_text = make_dm(self.facts)
        self.dm_file = self.write("dm.md", self.dm_text)
        self.agent = {"whoami": dict(WHOAMI), "dm_read": [], "read": {}, "post_rc": 0}
        self.scenario = {"agent-sync": self.agent}

    def send(self, **kw):
        write_scenario(self.root / "scenario.json", self.scenario)
        kw.setdefault("c2_id", C2)
        out, err = StringIO(), StringIO()
        code = send.run_send(str(self.dm_file), str(self.facts_file), out=out, err=err, **kw)
        return code, out.getvalue(), err.getvalue()

    def verbs(self):
        return [c["argv"][0] for c in self.calls() if "argv" in c]

    def call(self, verb):
        return [c for c in self.calls() if c.get("argv", [None])[0] == verb]


class Sends(SendTest):
    def test_the_whole_path(self):
        code, out, err = self.send()
        self.assertEqual((code, err), (0, ""), out)
        self.assertEqual(self.verbs(), ["whoami", "dm-read", "dm", "post"])
        dm = self.call("dm")[0]
        self.assertEqual(dm["argv"][:5], ["dm", "--owner", "--session", "openloop", "--json"])
        self.assertEqual(dm["body"], self.dm_text)
        self.assertIn("  ", dm["body"])                                         # sentence gaps are U+00A0 plus a space
        self.assertEqual((dm["body_mode"], dm["dir_mode"]), ("0o600", "0o700"))      # a private copy, so the checked bytes are the sent bytes
        self.assertNotIn(self.dm_text[:30], " ".join(dm["argv"]))                    # the text is never on a command line
        beat = self.call("post")[0]
        self.assertEqual(beat["argv"][beat["argv"].index("--topic") + 1], C2 + " open-loops heartbeat")
        self.assertEqual(beat["argv"][beat["argv"].index("--session") + 1], "openloop")
        self.assertNotIn("--to", beat["argv"])                                        # no mention
        self.assertIn(self.facts["facts_id"], beat["body"])
        self.assertNotIn("@", beat["body"])
        self.assertNotIn("://", beat["body"])
        self.assertIn("characters", beat["body"])
        self.assertIn("Sources ok:  github, linear", beat["body"].replace(" ", " "))
        self.assertRegex(out, r"sent id 4242 to the owner")
        self.assertIn("heartbeat posted in %s open-loops heartbeat" % C2, out)

    def test_a_hostile_source_name_cannot_mention_anyone_in_the_heartbeat(self):
        facts = dict(self.facts, sources=dict(self.facts["sources"], **{"@**all** hi": {"status": "ok", "reason": None}}),
                     missing=[{"source": "@**all** gone", "reason": "x"}])
        body = send.heartbeat_body(facts, 100)
        self.assertNotIn("@**", body)
        self.assertIn("hi", body)
        self.assertIn("gone", body)

    def test_two_ascii_space_gaps_are_sent_as_nbsp_gaps(self):
        self.dm_file = self.write("dm2.md", make_dm(self.facts, spaces="  "))
        code, _, _ = self.send()
        self.assertEqual(code, 0)
        self.assertEqual(self.call("dm")[0]["body"], self.dm_text)

    def test_dry_run_checks_and_sends_nothing(self):
        code, out, _ = self.send(dry_run=True)
        self.assertEqual(code, 0)
        self.assertEqual(self.verbs(), ["whoami", "dm-read"])
        self.assertIn("dry run", out)


class Refuses(SendTest):
    def test_a_wrong_seat_is_refused_and_nothing_else_runs(self):
        self.agent["whoami"]["seat"] = "CLAUDE"
        self.agent["whoami"]["bot_seat"] = "CLAUDE"
        code, out, err = self.send()
        self.assertEqual(code, send.EXIT_REFUSED)
        self.assertIn("only BF-Deployer sends the digest", err)
        self.assertEqual(self.verbs(), ["whoami"])

    def test_each_part_of_the_identity_must_hold(self):
        for change in ({"verified": False}, {"bot_seat": "GB-DEPLOYER"}, {"seat": None}, {"user_id": None}):
            self.agent["whoami"] = dict(WHOAMI, **change)
            code, _, err = self.send()
            self.assertEqual(code, send.EXIT_REFUSED, change)
        self.assertEqual(set(self.verbs()), {"whoami"})

    def test_whoami_failing_is_a_refusal(self):
        self.agent["whoami_rc"] = 3
        code, _, err = self.send()
        self.assertEqual(code, send.EXIT_REFUSED)
        self.assertEqual(self.verbs(), ["whoami"])

    def test_no_agent_sync_is_a_refusal(self):
        (self.bin / "agent-sync").unlink()
        code, _, err = self.send()
        self.assertEqual(code, send.EXIT_REFUSED)
        self.assertIn("agent-sync is not installed", err)

    def test_a_dm_that_fails_the_lint_is_not_sent(self):
        self.dm_file = self.write("bad.md", make_dm(self.facts, extra="go to https://evil.test/login"))
        code, out, err = self.send()
        self.assertEqual(code, send.EXIT_LINT)
        self.assertIn("did not make", out)
        self.assertEqual(self.verbs(), ["whoami"])

    def test_no_c2_id_refuses_before_anything_is_sent(self):
        for value in (None, "", "heartbeat", "ffx-1", "FFX-1 x"):
            code, _, err = self.send(c2_id=value)
            self.assertEqual(code, send.EXIT_USAGE, value)
            self.assertIn("no C2 id", err)
        self.assertNotIn("dm", self.verbs())
        self.assertNotIn("post", self.verbs())

    def test_a_bad_dm_file_is_a_usage_error(self):
        self.dm_file.chmod(0o666)
        code, _, err = self.send()
        self.assertEqual(code, send.EXIT_USAGE)
        self.assertIn("written by others", err)
        link = self.root / "link.md"
        link.symlink_to(self.dm_file)
        self.dm_file = link
        self.assertEqual(self.send()[0], send.EXIT_USAGE)
        self.assertEqual(self.verbs(), [])

    def test_a_bad_facts_file_is_a_usage_error(self):
        self.facts_file = self.write("nofacts.json", json.dumps({"schema": 2}))
        code, _, err = self.send()
        self.assertEqual(code, send.EXIT_USAGE)
        self.assertIn("not an open-loops facts document", err)


class Duplicates(SendTest):
    def sent_by_me(self, content, sender=ME):
        return {"id": 5, "sender_id": sender, "content": content, "dm": True}

    def test_a_dm_that_already_carries_the_facts_id_is_not_resent(self):
        self.agent["dm_read"] = [self.sent_by_me("older"), self.sent_by_me("[BF-DEPLOYER·openloop→OWNER] ... Facts %s." % self.facts["facts_id"])]
        code, out, _ = self.send()
        self.assertEqual(code, 0)
        self.assertNotIn("dm", self.verbs())
        self.assertIn("already sent", out)

    def test_the_heartbeat_is_still_posted_when_the_dm_was_already_out(self):
        self.agent["dm_read"] = [self.sent_by_me("Facts %s." % self.facts["facts_id"])]
        code, out, _ = self.send()
        self.assertEqual(code, 0)
        self.assertEqual(self.verbs(), ["whoami", "dm-read", "read", "post"])

    def test_and_not_posted_twice(self):
        self.agent["dm_read"] = [self.sent_by_me("Facts %s." % self.facts["facts_id"])]
        self.agent["read"] = {C2 + " open-loops heartbeat": [{"content": "Heartbeat for %s. fine" % self.facts["facts_id"]}]}
        code, out, _ = self.send()
        self.assertEqual(code, 0)
        self.assertEqual(self.verbs(), ["whoami", "dm-read", "read"])
        self.assertIn("heartbeat already in", out)

    def test_an_unreadable_heartbeat_topic_is_exit_6(self):
        self.agent["dm_read"] = [self.sent_by_me("Facts %s." % self.facts["facts_id"])]
        self.agent["read_rc"] = {C2 + " open-loops heartbeat": 5}
        code, _, err = self.send()
        self.assertEqual(code, send.EXIT_HEARTBEAT)
        self.assertNotIn("post", self.verbs())

    def test_the_owner_quoting_the_id_is_not_a_duplicate(self):
        self.agent["dm_read"] = [self.sent_by_me("what is %s?" % self.facts["facts_id"], sender=3)]
        code, _, _ = self.send()
        self.assertEqual(code, 0)
        self.assertIn("dm", self.verbs())

    def test_another_digest_the_same_day_is_a_duplicate_unless_again(self):
        earlier = self.facts["facts_id"].replace("-0700-", "-0645-")
        self.agent["dm_read"] = [self.sent_by_me("Facts %s." % earlier)]
        code, out, _ = self.send()
        self.assertEqual(code, 0)
        self.assertNotIn("dm", self.verbs())
        self.assertIn("--again", out)
        open(self.log, "w").close()
        code, _, _ = self.send(again=True)
        self.assertEqual(code, 0)
        self.assertIn("dm", self.verbs())

    def test_yesterdays_digest_is_not_a_duplicate(self):
        yesterday = self.facts["facts_id"].replace("20261011", "20261010")
        self.agent["dm_read"] = [self.sent_by_me("Facts %s." % yesterday)]
        self.assertEqual(self.send()[0], 0)
        self.assertIn("dm", self.verbs())

    def test_again_never_resends_the_same_facts_id(self):
        self.agent["dm_read"] = [self.sent_by_me("Facts %s." % self.facts["facts_id"])]
        self.send(again=True)
        self.assertNotIn("dm", self.verbs())

    def test_a_failed_duplicate_check_stops_the_send(self):
        self.agent["dm_read_rc"] = 5
        code, _, err = self.send()
        self.assertEqual(code, send.EXIT_SEND)
        self.assertIn("could not check for a duplicate", err)
        self.assertNotIn("dm", self.verbs())

    def test_a_dm_read_that_is_not_json_stops_the_send(self):
        def broken(text):
            raise ValueError("not an object")
        with mock.patch.object(send, "_json_lines", broken):
            code, _, err = self.send()
        self.assertEqual(code, send.EXIT_SEND)
        self.assertNotIn("dm", self.verbs())


class Failures(SendTest):
    def test_a_failed_send_is_exit_5_and_no_heartbeat(self):
        self.agent.update(dm_rc=5, dm_stderr="agent-sync: Zulip said no\n")
        code, _, err = self.send()
        self.assertEqual(code, send.EXIT_SEND)
        self.assertIn("was not sent", err)
        self.assertNotIn("may have been delivered", err)
        self.assertEqual(self.verbs(), ["whoami", "dm-read", "dm"])

    def test_a_network_failure_after_the_request_left_is_not_retried(self):
        self.agent.update(dm_rc=6, dm_stderr="agent-sync: network error\n")
        code, _, err = self.send()
        self.assertEqual(code, send.EXIT_SEND)
        self.assertIn("may have been delivered", err)
        self.assertEqual(self.verbs().count("dm"), 1)

    def test_the_dm_out_but_no_heartbeat_is_exit_6(self):
        self.agent.update(post_rc=5, post_stderr="agent-sync: topic refused\n")
        code, out, err = self.send()
        self.assertEqual(code, send.EXIT_HEARTBEAT)
        self.assertIn("the DM is out but the heartbeat was not posted", err)
        self.assertIn("sent id 4242", out)


if __name__ == "__main__":
    unittest.main()
