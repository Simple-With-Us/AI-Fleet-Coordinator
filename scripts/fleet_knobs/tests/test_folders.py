"""Knobs in a named folder:  each folder is fetched on its own, and only when a read needs it.

The wake router's knobs live in /wake-router so its read token can be scoped to that folder
alone (a token scoped to "/" reaches every folder under it).  Root knobs must behave exactly as
they did before folders existed:  same fetch, same copy, same marker, same report."""

from __future__ import annotations

import json
import os
import time
from unittest import mock

from fleet_knobs import knobs, lkg, setter, source
from fleet_knobs.registry import BY_NAME, FOLDERS, REGISTRY, Spec, names_in

from ._fake import SIGNING, KnobTestCase, ScopedReadAPI

ROUTER = "/wake-router"
ROUTER_KEY = "WAKE_ROUTER_KEY"
KEY = "k3y-" + "0123456789abcdef" * 2
ROOT_VALUES = {"LANE_SWEEP_MODE": "act", "FLEET_FACTS_SIGNING_KEY": SIGNING}
ROUTER_VALUES = {"WAKE_ROUTER_MAX_ATTEMPTS": "9", ROUTER_KEY: KEY}


class RegistryTests(KnobTestCase):
    def test_the_router_knobs_and_only_they_are_in_its_folder(self):
        self.assertEqual(FOLDERS, ("/", ROUTER))
        # The router's knobs, and the Operator registry's address and key, which keep registry_api's
        # one name for each value.
        registry = {"SIMPLE_TOOLS_OPERATOR_REGISTRY_URL", "SIMPLE_TOOLS_OPERATOR_REGISTRY_KEY"}
        self.assertEqual(
            set(names_in(ROUTER)),
            {s.name for s in REGISTRY if s.name.startswith("WAKE_ROUTER_")} | registry,
        )
        self.assertIn(ROUTER_KEY, names_in(ROUTER))
        self.assertTrue(BY_NAME[ROUTER_KEY].secret)
        self.assertEqual(BY_NAME["LANE_SWEEP_MODE"].folder, "/")

    def test_a_folder_must_be_a_plain_path(self):
        for bad in ("", "wake-router", "/Wake", "/../x", "/a/", "/a b", "//a", "/a/../b"):
            with self.subTest(folder=bad), self.assertRaises(ValueError):
                Spec("X", "str", "T", default="", folder=bad)
        for good in ("/", "/a", "/wake-router", "/a/b-c"):
            self.assertEqual(Spec("X", "str", "T", default="", folder=good).folder, good)


class FetchTests(KnobTestCase):
    def paths(self):
        return [call["path"] for call in self.fake.calls()]

    def test_a_folder_knob_is_fetched_from_its_folder_with_only_its_names(self):
        self.fake.set(secrets={}, folders={ROUTER: ROUTER_VALUES})
        self.assertEqual(knobs.get("WAKE_ROUTER_MAX_ATTEMPTS"), 9)
        self.assertEqual(self.paths(), [ROUTER])
        self.assertEqual(self.fake.calls()[0]["names"], sorted(names_in(ROUTER)))
        self.assertEqual(knobs.resolve("WAKE_ROUTER_MAX_ATTEMPTS").source, "infisical")

    def test_root_knobs_still_come_from_the_root_with_only_root_names(self):
        self.fake.set(secrets={}, folders={"/": ROOT_VALUES, ROUTER: ROUTER_VALUES})
        self.assertEqual(knobs.get("LANE_SWEEP_MODE"), "act")
        self.assertEqual(self.paths(), ["/"])
        asked = self.fake.calls()[0]["names"]
        self.assertEqual(asked, sorted(names_in("/")))
        self.assertFalse([n for n in asked if n.startswith("WAKE_ROUTER_")])
        self.assertIn("FLEET_FACTS_SIGNING_KEY", asked)

    def test_only_the_folders_a_process_reads_are_fetched_each_once(self):
        self.fake.set(secrets={}, folders={"/": ROOT_VALUES, ROUTER: ROUTER_VALUES})
        knobs.get("WAKE_ROUTER_WORKERS")
        knobs.get("WAKE_ROUTER_MAX_ATTEMPTS")
        knobs.get(ROUTER_KEY)
        self.assertEqual(self.paths(), [ROUTER], "the root is never fetched for router knobs")
        knobs.get("LANE_SWEEP_MODE")
        knobs.get("CONSULT_ENABLED")
        self.assertEqual(self.paths(), [ROUTER, "/"], "one fetch per folder")

    def test_a_knob_held_in_the_wrong_folder_is_not_read(self):
        # Infisical holds the router's knob at the root (before it was moved):  the router's
        # read of its own folder does not see it, and a root read never asks for it.
        self.fake.set(secrets={}, folders={"/": {"WAKE_ROUTER_MAX_ATTEMPTS": "9"}, ROUTER: {}})
        knob = knobs.resolve("WAKE_ROUTER_MAX_ATTEMPTS")
        self.assertEqual((knob.value, knob.source, knob.reason), (5, "default", "unset"))

    def test_refresh_fetches_each_folder_again_on_its_next_read(self):
        self.fake.set(secrets={}, folders={"/": ROOT_VALUES, ROUTER: ROUTER_VALUES})
        knobs.get("LANE_SWEEP_MODE")
        knobs.get("WAKE_ROUTER_MAX_ATTEMPTS")
        knobs.refresh()
        knobs.get("WAKE_ROUTER_MAX_ATTEMPTS")
        self.assertEqual(self.paths(), ["/", ROUTER, ROUTER])

    def test_one_folders_failure_leaves_the_other_live(self):
        self.fake.set(
            secrets={},
            folders={"/": ROOT_VALUES, ROUTER: ROUTER_VALUES},
            folder_modes={"/": "fail"},
        )
        self.assertEqual(knobs.resolve("LANE_SWEEP_MODE").source, "default")
        self.assertTrue(knobs.resolve("LANE_SWEEP_MODE").stale)
        router = knobs.resolve("WAKE_ROUTER_MAX_ATTEMPTS")
        self.assertEqual((router.value, router.source, router.stale), (9, "infisical", False))
        self.assertEqual(knobs.folder_status(ROUTER), {"fetch": "ok"})
        self.assertEqual(knobs.folder_status("/")["fetch"], "failed")
        self.assertEqual(knobs.folder_status("/")["fetch_reason"], "scoped-read-failed")
        self.assertEqual(knobs.folder_status("/nowhere"), {"fetch": "not-attempted"})


class SecretTests(KnobTestCase):
    def test_a_folder_secret_fails_closed_when_its_folder_cannot_be_read(self):
        self.fake.set(secrets={}, folders={"/": ROOT_VALUES}, folder_modes={ROUTER: "fail"})
        self.assertEqual(knobs.get("FLEET_FACTS_SIGNING_KEY"), SIGNING, "the root is fine")
        with self.assertRaises(knobs.SecretUnavailable) as ctx:
            knobs.get(ROUTER_KEY)
        self.assertEqual(ctx.exception.reason, "unreachable:scoped-read-failed")

    def test_a_folder_secret_is_read_and_never_written(self):
        self.fake.set(secrets={}, folders={"/": ROOT_VALUES, ROUTER: ROUTER_VALUES})
        self.assertEqual(knobs.get(ROUTER_KEY), KEY)
        knobs.get("LANE_SWEEP_MODE")
        blob = b""
        for directory, _dirs, files in os.walk(self.home):
            for name in files:
                with open(os.path.join(directory, name), "rb") as fh:
                    blob += fh.read()
        self.assertTrue(blob)
        for forbidden in (KEY, SIGNING, ROUTER_KEY, "FLEET_FACTS_SIGNING_KEY"):
            self.assertNotIn(forbidden.encode(), blob)
        self.assertNotIn(KEY, json.dumps(knobs.report()))

    def test_a_planted_folder_copy_cannot_supply_its_secret(self):
        lkg.write({ROUTER_KEY: KEY, "WAKE_ROUTER_MAX_ATTEMPTS": "9"}, folder=ROUTER)
        self.fake.set(mode="fail")
        self.assertEqual(knobs.get("WAKE_ROUTER_MAX_ATTEMPTS"), 9)
        with self.assertRaises(knobs.SecretUnavailable):
            knobs.get(ROUTER_KEY)


class CopyTests(KnobTestCase):
    @property
    def router_copy(self):
        return self.state_dir / "lkg-wake-router.json"

    def test_each_folder_has_its_own_copy_and_the_root_copy_is_unchanged(self):
        self.fake.set(secrets={}, folders={"/": ROOT_VALUES, ROUTER: ROUTER_VALUES})
        knobs.get("LANE_SWEEP_MODE")
        knobs.get("WAKE_ROUTER_MAX_ATTEMPTS")
        root = json.loads(self.lkg_file.read_text())
        router = json.loads(self.router_copy.read_text())
        self.assertEqual(root["knobs"], {"LANE_SWEEP_MODE": "act"})
        self.assertNotIn("path", root, "the root copy keeps the shape Muster reads")
        self.assertEqual(
            set(root), {"schema", "project", "environment", "saved_epoch", "saved_at", "knobs"}
        )
        self.assertEqual(router["knobs"], {"WAKE_ROUTER_MAX_ATTEMPTS": "9"})
        self.assertEqual(router["path"], ROUTER)
        self.assertEqual(sorted(os.listdir(self.state_dir)), ["lkg-wake-router.json", "lkg.json"])

    def test_a_router_only_process_never_touches_the_root_copy(self):
        self.fake.set(secrets={}, folders={"/": ROOT_VALUES})
        knobs.get("LANE_SWEEP_MODE")
        before = self.lkg_file.read_bytes()
        self.new_process()
        self.fake.set(secrets={}, folders={ROUTER: ROUTER_VALUES})
        knobs.get("WAKE_ROUTER_MAX_ATTEMPTS")
        self.assertEqual(self.lkg_file.read_bytes(), before)

    def test_an_outage_reads_each_folder_from_its_own_copy(self):
        self.fake.set(secrets={}, folders={"/": ROOT_VALUES, ROUTER: ROUTER_VALUES})
        knobs.get("LANE_SWEEP_MODE")
        knobs.get("WAKE_ROUTER_MAX_ATTEMPTS")
        self.new_process()
        self.fake.set(mode="fail")
        router = knobs.resolve("WAKE_ROUTER_MAX_ATTEMPTS")
        root = knobs.resolve("LANE_SWEEP_MODE")
        self.assertEqual((router.value, router.source, router.stale), (9, "lkg", True))
        self.assertEqual((root.value, root.source, root.stale), ("act", "lkg", True))

    def test_the_root_copy_never_answers_for_a_folder_knob(self):
        # An old root copy from before the move still holds a router knob:  it is not used.
        lkg.write({"WAKE_ROUTER_MAX_ATTEMPTS": "9"})
        self.fake.set(mode="fail")
        knob = knobs.resolve("WAKE_ROUTER_MAX_ATTEMPTS")
        self.assertEqual((knob.value, knob.source, knob.stale), (5, "default", True))
        self.assertIn("lkg-absent", knob.reason)

    def test_a_copy_of_another_folder_is_refused(self):
        lkg.write({"LANE_SWEEP_MODE": "act"})
        os.replace(self.lkg_file, self.router_copy)  # the root's copy under the router's name
        self.assertEqual(lkg.read(folder=ROUTER), (None, "wrong-folder"))
        lkg.write({"WAKE_ROUTER_MAX_ATTEMPTS": "9"}, folder=ROUTER)
        os.replace(self.router_copy, self.lkg_file)
        self.assertEqual(lkg.read(), (None, "wrong-folder"))

    def test_the_48_hour_limit_holds_for_a_folder_copy(self):
        lkg.write(
            {"WAKE_ROUTER_MAX_ATTEMPTS": "9"},
            now=time.time() - lkg.MAX_AGE_SECONDS - 60,
            folder=ROUTER,
        )
        self.fake.set(mode="fail")
        knob = knobs.resolve("WAKE_ROUTER_MAX_ATTEMPTS")
        self.assertEqual((knob.value, knob.source, knob.stale), (5, "default", True))
        self.assertIn("lkg-expired", knob.reason)

    def test_an_empty_folder_answer_keeps_that_folders_copy(self):
        self.fake.set(secrets={}, folders={ROUTER: {"WAKE_ROUTER_MAX_ATTEMPTS": "9"}})
        knobs.get("WAKE_ROUTER_MAX_ATTEMPTS")
        before = self.router_copy.read_bytes()
        self.new_process()
        self.fake.set(secrets={}, folders={ROUTER: {}})
        knob = knobs.resolve("WAKE_ROUTER_MAX_ATTEMPTS")
        self.assertEqual((knob.value, knob.source, knob.reason), (9, "lkg", "unreachable:empty"))
        self.assertEqual(self.router_copy.read_bytes(), before)

    def test_a_file_name_is_never_made_from_an_unchecked_folder(self):
        with self.assertRaises(ValueError):
            lkg.path(folder="/../../etc")

    def test_distinct_folders_never_share_a_file(self):
        # Kody on #1808:  joining segments with "--" mapped /a/b and /a--b to one file.
        folders = ("/", "/a", "/a/b", "/a--b", "/a-b", "/a/b/c", "/a/b--c", "/a--b/c", ROUTER)
        copies = {lkg.path(folder=f) for f in folders}
        markers = {lkg.failure_path(folder=f) for f in folders}
        self.assertEqual(len(copies), len(folders))
        self.assertEqual(len(markers), len(folders))
        self.assertTrue(lkg.path(folder=ROUTER).endswith("/lkg-wake-router.json"))
        lkg.write({"WAKE_ROUTER_MAX_ATTEMPTS": "9"}, folder="/a/b")
        self.assertEqual(lkg.read(folder="/a--b"), (None, "absent"))
        self.assertEqual(lkg.read(folder="/a/b")[1], "ok")


class MarkerTests(KnobTestCase):
    def test_each_folder_has_its_own_failed_fetch_marker(self):
        self.fake.set(secrets={}, folders={"/": ROOT_VALUES}, folder_modes={ROUTER: "fail"})
        knobs.get("WAKE_ROUTER_MAX_ATTEMPTS")
        self.assertTrue((self.state_dir / "fetch-failed-wake-router.json").exists())
        self.assertFalse((self.state_dir / "fetch-failed.json").exists())
        # A sibling process:  the router's marker does not skip the root's fetch.
        self.new_process(keep_failure_marker=True)
        self.assertEqual(knobs.resolve("LANE_SWEEP_MODE").source, "infisical")
        router = knobs.resolve("WAKE_ROUTER_MAX_ATTEMPTS")
        self.assertEqual(knobs.folder_status(ROUTER)["fetch_reason"], "recent-scoped-read-failed")
        self.assertTrue(router.stale)
        self.assertEqual([c["path"] for c in self.fake.calls()], [ROUTER, "/"])

    def test_a_good_fetch_clears_only_its_own_marker(self):
        lkg.record_failure("timeout")
        lkg.record_failure("timeout", folder=ROUTER)
        self.fake.set(secrets={}, folders={ROUTER: ROUTER_VALUES})
        knobs.refresh()  # ignore the markers, as a deliberate retry does
        knobs.get("WAKE_ROUTER_MAX_ATTEMPTS")
        self.assertFalse((self.state_dir / "fetch-failed-wake-router.json").exists())
        self.assertTrue((self.state_dir / "fetch-failed.json").exists())


class ReportTests(KnobTestCase):
    def test_a_root_only_process_reports_as_before(self):
        self.fake.set(secrets=ROOT_VALUES)
        knobs.get("LANE_SWEEP_MODE")
        rep = knobs.report()
        self.assertEqual(set(rep), {"schema", "fetch", "stale", "knobs"})
        self.assertEqual(rep["fetch"], "ok")

    def test_a_process_reading_both_reports_each_folder(self):
        self.fake.set(secrets={}, folders={"/": ROOT_VALUES}, folder_modes={ROUTER: "fail"})
        knobs.get("LANE_SWEEP_MODE")
        knobs.get("WAKE_ROUTER_MAX_ATTEMPTS")
        rep = knobs.report()
        self.assertEqual((rep["fetch"], rep["fetch_reason"]), ("failed", "scoped-read-failed"))
        self.assertEqual(rep["folders"]["/"]["fetch"], "ok")
        self.assertEqual(rep["folders"][ROUTER]["fetch"], "failed")
        self.assertEqual(rep["folders"][ROUTER]["lkg"], "absent")
        self.assertTrue(rep["stale"])

    def test_a_router_only_process_names_its_folder(self):
        self.fake.set(secrets={}, folders={ROUTER: ROUTER_VALUES})
        knobs.get("WAKE_ROUTER_MAX_ATTEMPTS")
        rep = knobs.report()
        self.assertEqual(rep["fetch"], "ok")
        self.assertEqual(list(rep["folders"]), [ROUTER])


class ScopedTokenTests(KnobTestCase):
    """The real verifier against a loopback API whose token is scoped to one folder."""

    def run_on(self, api):
        os.environ.update(api.env)
        real = mock.patch.object(source, "HELPER_LOADER", source._load_helper)
        real.start()
        self.addCleanup(real.stop)

    def test_a_token_scoped_to_the_router_folder_reads_its_knobs_and_nothing_else(self):
        with ScopedReadAPI(ROUTER_VALUES, path=ROUTER) as api:
            self.run_on(api)
            self.assertEqual(knobs.get("WAKE_ROUTER_MAX_ATTEMPTS"), 9)
            self.assertEqual(knobs.get(ROUTER_KEY), KEY)
            root = knobs.resolve("LANE_SWEEP_MODE")
        self.assertEqual((root.source, root.stale), ("default", True))
        self.assertEqual(knobs.folder_status("/")["fetch_reason"], "scoped-read-failed")
        asked = [p for p in api.requests if p.startswith("/api/v3/")]
        self.assertTrue(asked)
        self.assertTrue(all("secretPath=%2Fwake-router" in p for p in asked), asked)

    def test_a_root_scoped_token_does_not_read_the_router_folder_through_the_verifier(self):
        with ScopedReadAPI(ROUTER_VALUES, path="/") as api:
            self.run_on(api)
            knob = knobs.resolve("WAKE_ROUTER_MAX_ATTEMPTS")
        self.assertEqual((knob.source, knob.stale), ("default", True))
        self.assertEqual(knobs.folder_status(ROUTER)["fetch_reason"], "scoped-read-failed")


class SetterTests(KnobTestCase):
    def test_a_write_and_a_delete_go_to_the_knobs_folder(self):
        set_argv = setter.write_argv("infisical", "WAKE_ROUTER_MAX_ATTEMPTS", "9")
        self.assertIn("--path=/wake-router", set_argv)
        self.assertIn("--path=/", setter.write_argv("infisical", "LANE_SWEEP_MODE", "act"))
        self.assertIn(
            "--path=/wake-router", setter.delete_argv("infisical", "WAKE_ROUTER_MAX_ATTEMPTS")
        )
        self.assertIn("--path=/", setter.delete_argv("infisical", "LANE_SWEEP_MODE"))

    def test_the_write_token_is_checked_against_the_knobs_folder(self):
        seen = []

        def verify(token, environ, folder="/"):
            seen.append(folder)

        def runner(argv, **_kw):
            return 0, b""

        def reader(name):
            return (None, "default", False, "unset")

        token = "st." + "w" * 40
        with mock.patch.object(setter, "verify_writer", side_effect=verify):
            for name, value in (("WAKE_ROUTER_MAX_ATTEMPTS", "9"), ("LANE_SWEEP_MODE", "act")):
                answer, code = setter.set_knob(
                    name,
                    value,
                    "yes",
                    environ={setter.ENV_WRITE_TOKEN: token},
                    runner=runner,
                    reader=reader,
                    find_cli=lambda: "/bin/infisical",
                    audit_file=str(self.root / "audit.jsonl"),
                )
                self.assertEqual((answer["result"], code), ("done", 0), answer["detail"])
        self.assertEqual(seen, [ROUTER, "/"])

    def test_a_write_patches_the_knobs_own_copy(self):
        lkg.write({"WAKE_ROUTER_MAX_ATTEMPTS": "5"}, folder=ROUTER)
        lkg.write({"LANE_SWEEP_MODE": "report"})
        root_before = self.lkg_file.read_bytes()
        how = setter.refresh_lkg(
            "WAKE_ROUTER_MAX_ATTEMPTS",
            "9",
            lambda _name: (None, "default", True, "unreachable:x"),
            BY_NAME["WAKE_ROUTER_MAX_ATTEMPTS"],
            9,
        )
        self.assertEqual(how, "patched")
        doc, _note = lkg.read(folder=ROUTER)
        self.assertEqual(doc["knobs"], {"WAKE_ROUTER_MAX_ATTEMPTS": "9"})
        self.assertEqual(self.lkg_file.read_bytes(), root_before)
        self.assertEqual(
            setter.refresh_lkg_unset(
                "WAKE_ROUTER_MAX_ATTEMPTS", lambda _name: (None, "default", True, "x")
            ),
            "patched",
        )
        self.assertEqual(lkg.read(folder=ROUTER)[0]["knobs"], {})
