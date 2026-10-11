"""The contract with the digest (open_loops, unit C2-1):  what the publisher writes is what `open-loops` reads.

One synthetic Mac (the open_loops "standard world") is published by the real cycle into a fake repository.  The
`gb/mac-facts.json` that lands there is then read the way a cloud host reads it:  `open_loops.macfacts.load`, which
calls the real `fleet-facts verify` (here, `publish.verify_file`), and `build_facts(mac_facts=...)`.  A tampered file,
the wrong key and an old file must all leave the Mac parts Missing, never zero."""
import json
import os
from datetime import timedelta
from pathlib import Path
from unittest import mock

from fleet_facts import publish as p
from fleet_facts import signing
from fleet_facts.runner import Result
from open_loops import macfacts
from open_loops.facts import build_facts
from open_loops.tests.builders import NOW, Sandbox
from open_loops.tests.scenes import standard_world

from .ghfake import REPO, FakeGitHub

SIGNER = "fixture-signing-key-0123456789abcdefABCDEF"
OTHER = "another-fixture-key-9876543210zyxwvuTSRQ"


class Contract(Sandbox):
    def setUp(self):
        super().setUp()
        self.world = standard_world(self.home, self.bin)
        self.gh = FakeGitHub()
        self.program = self.bin / "open-loops"
        self.program.write_text("#!/bin/sh\nexit 99\n")
        self.program.chmod(0o755)
        self.publish()

    def machine(self, argv, *, timeout, env=None, cwd=None):
        name = os.path.basename(argv[0])
        if name == "gh" and "graphql" not in argv:
            return self.gh(argv, timeout=timeout)
        if name == "open-loops":
            Path(argv[argv.index("--out") + 1]).write_text("# Full list\n", encoding="utf-8")
            return Result(0, "", "")
        return self.world.runner()(argv, timeout=timeout, env=env, cwd=cwd)

    def publish(self):
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()):
            outcome = p.run_cycle(run=self.machine, now=NOW, key_provider=lambda: SIGNER, load1=lambda: 1.0, sleep=lambda s: None, repo=REPO, gh="gh",
                                  apps_provider=lambda: [])
        self.assertEqual((outcome.result, outcome.reason), ("published", None))

    def consumer(self, argv, *, timeout, env=None, cwd=None):
        if os.path.basename(argv[0]) == "fleet-facts" and argv[1] == "verify":
            code, _ = p.verify_file(argv[2], dict(env or {}))
            return Result(code, "", "")
        return self.world.runner()(argv, timeout=timeout, env=env, cwd=cwd)

    def facts(self, rel: str, *, key: str | None = SIGNER, now=NOW, data: bytes | None = None):
        path = self.root / ("read-" + rel.replace("/", "-"))
        path.write_bytes(data if data is not None else self.gh.written(rel))
        path.chmod(0o600)
        environ = {macfacts.KEY_ENV: key} if key else {}
        return build_facts(no_mac=True, mac_facts=str(path), run=self.consumer, now=now, sleep=lambda s: None, environ=environ)

    def test_the_grok_bot_copy_verifies_and_supplies_lanes_and_c1(self):
        facts = self.facts("gb/mac-facts.json")
        self.assertEqual(facts["sources"]["mac_facts"], {"status": "ok", "reason": None})
        self.assertEqual(facts["sources"]["lanes"]["status"], "ok")
        self.assertEqual(facts["sources"]["autopush"]["status"], "ok")
        self.assertEqual(facts["missing"], [])
        self.assertEqual(facts["not_on_github"]["snapshot_only"]["count"], 1)
        self.assertEqual(facts["not_on_github"]["snapshot_only"]["items"][0]["lane"], "claude-snap")
        self.assertEqual(facts["not_on_github"]["armed_local_work"]["count"], 1)
        self.assertEqual(facts["not_on_github"]["refusals"]["count"], 1)
        self.assertEqual(facts["not_on_github"]["refusals"]["items"][0]["lane"], "claude-gate")
        names = {i["lane"] for i in facts["unpushed_over_a_day"]["items"]}
        self.assertEqual(names, {"claude-stale", "claude-never"})

    def test_what_a_cloud_host_sees_equals_what_the_mac_itself_sees(self):
        full = self.facts("mac-facts.json")
        minimized = self.facts("gb/mac-facts.json")
        for section in ("not_on_github", "unpushed_over_a_day", "decisions"):
            self.assertEqual(minimized[section], full[section], section)
        self.assertEqual(minimized["missing"], full["missing"])

    def test_the_needs_owner_list_rides_along(self):
        facts = self.facts("gb/mac-facts.json")
        self.assertEqual(facts["sources"]["needs_owner"]["status"], "ok")
        self.assertEqual(facts["decisions"]["answered_awaiting_close"], 1)

    def test_the_copy_has_no_path_and_the_consumer_still_names_every_lane(self):
        text = self.gh.written("gb/mac-facts.json").decode()
        self.assertNotIn("/work/lanes", text)
        document = json.loads(text)
        self.assertNotIn('"path', json.dumps(document["lanes"]))
        self.assertIn("claude-stale", text)

    def test_a_tampered_file_leaves_the_mac_parts_missing(self):
        document = json.loads(self.gh.written("gb/mac-facts.json"))
        document["lanes"]["attention"] = []
        facts = self.facts("gb/mac-facts.json", data=(json.dumps(document) + "\n").encode())
        self.assertEqual(facts["sources"]["mac_facts"]["status"], "absent")
        self.assertIn("did not verify", facts["sources"]["mac_facts"]["reason"])
        self.assertEqual({m["source"] for m in facts["missing"]}, {"mac_facts", "lanes", "autopush"})
        self.assertIsNone(facts["unpushed_over_a_day"]["count"])

    def test_the_wrong_key_and_no_key_leave_them_missing_too(self):
        wrong = self.facts("gb/mac-facts.json", key=OTHER)
        self.assertEqual(wrong["sources"]["mac_facts"]["status"], "absent")
        self.assertIn("did not verify", wrong["sources"]["mac_facts"]["reason"])
        none = self.facts("gb/mac-facts.json", key=None)
        self.assertIn("is not set", none["sources"]["mac_facts"]["reason"])

    def test_a_file_older_than_nine_hours_is_stale(self):
        facts = self.facts("gb/mac-facts.json", now=NOW + timedelta(hours=10))
        self.assertEqual(facts["sources"]["mac_facts"]["status"], "absent")
        self.assertIn("stale, built", facts["sources"]["mac_facts"]["reason"])

    def test_a_file_signed_for_the_other_path_still_verifies_without_a_path_check(self):
        """Moving `mac-facts.json` to `gb/mac-facts.json` verifies for `fleet-facts verify FILE`;  `--path` is how a caller pins the path."""
        full = self.gh.written("mac-facts.json")
        code, _ = p.verify_file(str(self._write(full)), {signing.KEY_ENV: SIGNER})
        self.assertEqual(code, 0)
        code, _ = p.verify_file(str(self._write(full)), {signing.KEY_ENV: SIGNER}, signed_for="gb/mac-facts.json")
        self.assertEqual(code, 1)

    def _write(self, data: bytes) -> Path:
        path = self.root / "moved.json"
        path.write_bytes(data)
        return path

    def test_the_two_documents_are_signed_for_their_own_paths(self):
        for rel in ("mac-facts.json", "gb/mac-facts.json"):
            document = json.loads(self.gh.written(rel))
            self.assertEqual(document["signed_for"], rel)
            self.assertTrue(signing.verify(document, SIGNER, signed_for=rel))
            self.assertFalse(signing.verify(document, SIGNER, signed_for="gb/other.json"))


if __name__ == "__main__":
    import unittest
    unittest.main()
