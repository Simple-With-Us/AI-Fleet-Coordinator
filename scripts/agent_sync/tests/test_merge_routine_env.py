"""merge_routine_env:  a http-wake seat's routine URL and key reach the daemon's environment
from Infisical through the same one fleet-secrets fetch the seat pairs use, so a Mac daemon
with no folder loader still resolves a routine (OPS-1048).  The fetch is a stand-in in every
test;  no test reads Infisical, and no value is ever logged."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from agent_sync import config as C
from agent_sync import daemon as D


def seat(name: str, *, routine: C.RoutineConfig | None) -> "object":
    """A stand-in runner:  merge_routine_env reads only `runner.cfg.routine`, so the test
    names just that instead of building a full SeatConfig."""
    return type("Runner", (), {"cfg": type("Cfg", (), {"routine": routine})()})()


def routine(url_env: str, key_env: str) -> C.RoutineConfig:
    return C.RoutineConfig(
        url_env=url_env,
        key_env=key_env,
        method="POST",
        header="Authorization",
        auth="bearer",
        timeout=15,
        cost_usd=0.0,
        signature_prefix="",
    )


class MergeRoutineEnv(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = {"HOME": self.tmp.name}

    def test_values_land_in_the_env(self) -> None:
        seats = {
            "ZC": seat("ZC", routine=routine("ZC_ROUTINE_URL", "ZC_ROUTINE_KEY")),
        }
        asked = []

        def fetch(names):
            asked.extend(names)
            return {names[0]: "https://bridge.example/wake/ZC", names[1]: "k" * 32}

        got = D.merge_routine_env(seats, self.env, Path(self.tmp.name), fetch=fetch)
        self.assertEqual(sorted(asked), ["ZC_ROUTINE_KEY", "ZC_ROUTINE_URL"])
        self.assertEqual(self.env["ZC_ROUTINE_URL"], "https://bridge.example/wake/ZC")
        self.assertEqual(self.env["ZC_ROUTINE_KEY"], "k" * 32)
        self.assertEqual(got, ["ZC_ROUTINE_URL", "ZC_ROUTINE_KEY"])

    def test_names_the_store_does_not_hold_stay_absent(self) -> None:
        seats = {"ZC": seat("ZC", routine=routine("ZC_ROUTINE_URL", "ZC_ROUTINE_KEY"))}
        D.merge_routine_env(
            seats, self.env, Path(self.tmp.name), fetch=lambda names: {"ZC_ROUTINE_URL": "https://x"}
        )
        self.assertEqual(self.env.get("ZC_ROUTINE_URL"), "https://x")
        self.assertNotIn("ZC_ROUTINE_KEY", self.env)

    def test_empty_values_never_overwrite(self) -> None:
        self.env["ZC_ROUTINE_URL"] = "https://keep"
        seats = {"ZC": seat("ZC", routine=routine("ZC_ROUTINE_URL", "ZC_ROUTINE_KEY"))}
        D.merge_routine_env(seats, self.env, Path(self.tmp.name), fetch=lambda names: {"ZC_ROUTINE_URL": ""})
        self.assertEqual(self.env["ZC_ROUTINE_URL"], "https://keep")

    def test_no_routines_asks_nothing(self) -> None:
        seats = {"ZC": seat("ZC", routine=None), "MM": seat("MM", routine=None)}

        def fetch(names):  # a fetch here means the caller asked for nothing
            raise AssertionError("fetch called with %r" % (names,))

        self.assertEqual(D.merge_routine_env(seats, self.env, Path(self.tmp.name), fetch=fetch), [])

    def test_a_refused_fetch_lets_the_caller_swallow(self) -> None:
        seats = {"ZC": seat("ZC", routine=routine("ZC_ROUTINE_URL", "ZC_ROUTINE_KEY"))}

        def fetch(names):
            raise RuntimeError("fleet-secrets missing")

        with self.assertRaises(RuntimeError):
            D.merge_routine_env(seats, self.env, Path(self.tmp.name), fetch=fetch)


if __name__ == "__main__":
    unittest.main()
