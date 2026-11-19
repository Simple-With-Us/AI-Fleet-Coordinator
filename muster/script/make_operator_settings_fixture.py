#!/usr/bin/env python3
"""Regenerate synthetic native receipts through the actual helper/editor transaction.

External provider and container calls use the backend's offline test harness. No
account, runtime config or live service is accessed. Run with --write to replace
only the generated fixture, or --check to compare protocol cases and provenance.
"""

import argparse
import contextlib
import hashlib
import json
import sys
import tempfile
import time
import uuid
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from simple_tools.server import eavesdropper_settings_edit as editor
from simple_tools.server import operator_settings as settings
from simple_tools.tests.test_operator_settings import AutosaveTests, Store

FIXTURE = ROOT / "muster/MusterTests/Fixtures/operator-settings-wire.json"
PRODUCERS = [
    "scripts/fleet-ops-control.py",
    "scripts/simple_tools/server/eavesdropper_settings_edit.py",
    "scripts/simple_tools/server/operator_settings.py",
    "scripts/simple_tools/operator_github_state.py",
]
SCHEMA = "scripts/fleet-ops-eavesdropper-settings.json"
CLOCK = 1_794_635_900.0


def generate():
    cases = {}

    @contextlib.contextmanager
    def harness():
        test = AutosaveTests()
        test.setUp()
        try:
            yield test
        finally:
            test.doCleanups()

    def identity(name):
        return dict(
            requestID=str(uuid.uuid5(uuid.NAMESPACE_URL, "operator-settings-fixture/" + name)),
            requestCreatedAt=int(CLOCK * 1000),
        )

    def wire(test, name, request=None, **kwargs):
        if request is not None:
            request = dict(identity(name), **request)
        response, code = test.canonical_wire(request, **kwargs)
        expected = {
            "get": (0, None),
            "save1": (0, None),
            "save2": (0, None),
            "runtime_pending": (1, "runtime_pending"),
            "reconcile_unknown": (1, "outcome_unknown"),
            "reconcile_saved": (0, None),
            "superseded": (1, "request_superseded"),
            "known_ok_read_failed": (1, "prior_result_unavailable"),
            "cancelled": (1, "request_cancelled"),
            "early_refused": (1, "request_refused"),
            "unactivated_get": (0, None),
            "initialized": (0, None),
            "bracket_get": (0, None),
            "bracket_true": (0, None),
            "bracket_false": (0, None),
        }
        actual = (code, response["eavesdropperSettings"].get("errorCode"))
        if actual != expected[name]:
            raise RuntimeError("Offline receipt case did not reach its intended outcome: " + name)
        cases[name] = dict(request=request, response=response, exitCode=code)
        return response["eavesdropperSettings"]

    with (
        tempfile.TemporaryDirectory(prefix=".operator-fixture-", dir=Path.home()) as private,
        mock.patch.object(tempfile, "tempdir", private),
        mock.patch.object(time, "time", return_value=CLOCK),
    ):
        with harness() as test:
            test.status_path.write_text(
                json.dumps({"channel_names": ["Work-In-Progress", "Builds", "Alerts"]})
            )
            wire(test, "get")
            for name, value in [("save1", 2.0), ("save2", 3.0), ("runtime_pending", 4.0)]:
                request = dict(
                    action="save",
                    set={"daemon.coalesce_seconds": value},
                    expectBaseSHA256=settings.revision(test.store.document),
                )
                extra = (
                    dict(
                        reloader=lambda _: dict(ok=True, confirmed=False, code="reload_unconfirmed")
                    )
                    if name == "runtime_pending"
                    else {}
                )
                wire(test, name, request, **extra)
            pending = cases["runtime_pending"]["request"]
            request = dict(
                action="reconcile",
                requestID=pending["requestID"],
                requestCreatedAt=pending["requestCreatedAt"],
            )
            wire(test, "reconcile_unknown", request)
            test.confirm()
            wire(test, "reconcile_saved", request)
            first = cases["save1"]["request"]
            wire(test, "superseded", first)
            with mock.patch.object(
                test.store, "read", side_effect=settings.SettingsError("provider_read_failed")
            ):
                wire(test, "known_ok_read_failed", first)
            wire(test, "cancelled", dict(action="reconcile"))
            wire(
                test,
                "early_refused",
                dict(
                    action="save",
                    set={"daemon.coalesce_seconds": 5.0},
                    expectBaseSHA256=settings.revision(test.store.document),
                ),
                store_error=settings.SettingsError(
                    "writer_not_configured", required_binding=settings.WRITE_BINDING
                ),
            )
        with harness() as test:
            wire(test, "unactivated_get", reader_enabled=False)
            test.store = Store(None)
            snapshot = settings.accepted_snapshot(
                editor._parse(test.original, "fixture"), test.allowlist
            )
            test.status_path.write_text(
                json.dumps(
                    dict(
                        updated=time.time(),
                        instance="server",
                        config_errors=[],
                        operator_settings_snapshot_revision=settings.revision(snapshot),
                    )
                )
            )
            wire(
                test,
                "initialized",
                dict(action="initialize", expectBaseSHA256=editor.sha256(test.path.read_bytes())),
                reader_enabled=False,
            )
        with harness() as test:
            wire(test, "bracket_get")
            for name, value in [("bracket_true", True), ("bracket_false", False)]:
                wire(
                    test,
                    name,
                    dict(
                        action="save",
                        set={"seat.GROK.bracket_recipient_wakes": value},
                        expectBaseSHA256=settings.revision(test.store.document),
                    ),
                )
    return dict(
        synthetic=True,
        producer="real fleet-ops-control.main -> editor.run_request -> canonical transaction",
        module_sha256=hashlib.sha256(
            (ROOT / "scripts/simple_tools/server/operator_settings.py").read_bytes()
        ).hexdigest(),
        cases=cases,
        producer_sha256={p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in PRODUCERS},
        schema_path=SCHEMA,
        schema_sha256=hashlib.sha256((ROOT / SCHEMA).read_bytes()).hexdigest(),
    )


def semantic(value):
    """Only generated audit IDs vary. All protocol identities stay deterministic."""
    if isinstance(value, dict):
        return {k: semantic(v) for k, v in value.items() if k != "auditID"}
    if isinstance(value, list):
        return [semantic(v) for v in value]
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    flags = parser.add_mutually_exclusive_group(required=True)
    flags.add_argument("--write", action="store_true")
    flags.add_argument("--check", action="store_true")
    args = parser.parse_args()
    generated = generate()
    if args.write:
        FIXTURE.write_text(json.dumps(generated, indent=2) + "\n")
        print("Wrote 15 offline helper/editor receipt cases.")
        return 0
    if semantic(generated) != semantic(json.loads(FIXTURE.read_text())):
        print("Operator settings fixture differs from actual offline generation.", file=sys.stderr)
        return 1
    print("All 15 offline receipts and producer/schema hashes match.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
