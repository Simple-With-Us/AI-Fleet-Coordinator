"""Tests for peer-actions registry logging (AFC-745)."""

from __future__ import annotations

import io
import json
import unittest
from datetime import datetime
from unittest import mock
from urllib.error import HTTPError

from simple_tools import peer_actions as P


FAKE_SA = {
    "type": "service_account",
    "client_email": "fleet-sheets-sync@test.iam.gserviceaccount.com",
}


class FakeInfisical:
    def __init__(self, status: int = 200, value: str = json.dumps(FAKE_SA)) -> None:
        self.status = status
        self.value = value
        self.calls: list[str] = []

    def open(self, req, timeout=None):
        self.calls.append(req.full_url)
        if self.status != 200:
            raise HTTPError(req.full_url, self.status, "err", hdrs=None, fp=io.BytesIO(b"{}"))
        return io.BytesIO(json.dumps({"secret": {"secretValue": self.value}}).encode())


class FakeSheets:
    def __init__(self, statuses: list[int] | None = None) -> None:
        self.statuses = list(statuses or [200])
        self.bodies: list[bytes] = []

    def open(self, req, timeout=None):
        status = self.statuses.pop(0) if self.statuses else 200
        self.bodies.append(req.data or b"")
        headers = {"Retry-After": "1"} if status == 429 else {}
        payload = json.dumps({"updates": {"updatedRange": "'Peer-Actions Registry'!A42:F42", "updatedRows": 1}})
        if status != 200:
            raise HTTPError(req.full_url, status, "err", hdrs=headers, fp=io.BytesIO(b"{}"))
        return io.BytesIO(payload.encode())


class PeerActionsTests(unittest.TestCase):
    def test_build_entry_validates_seats_and_fields(self):
        entry = P.build_entry(
            acting_seat="gb-oracle",
            requesting_seat="CURSOR",
            request_source="please review PR 1",
            action_taken="commented with evidence",
            result="done",
            now=datetime(2026, 11, 11, 14, 15, 47, tzinfo=P.CENTRAL_TZ),
        )
        self.assertEqual(entry.acting_seat, "GB-ORACLE")
        self.assertTrue(entry.when.endswith(" CT"))

    def test_build_entry_rejects_multiline_request(self):
        with self.assertRaises(P.PeerActionsError) as caught:
            P.build_entry(
                acting_seat="GB-ORACLE",
                requesting_seat="CURSOR",
                request_source="line1\nline2",
                action_taken="x",
                result="done",
            )
        self.assertEqual(caught.exception.code, "invalid_argument")

    def test_load_service_account_from_env(self):
        env = {"GOOGLE_SHEETS_SA": json.dumps(FAKE_SA)}
        self.assertEqual(P.load_service_account_json(env)["client_email"], FAKE_SA["client_email"])

    def test_load_service_account_from_infisical(self):
        fake = FakeInfisical()
        opener = mock.Mock()
        opener.open = fake.open
        with mock.patch.object(P, "_http_opener", return_value=opener):
            with mock.patch.object(P, "_secret_from_file_or_env", return_value="st." + ("x" * 40)):
                with mock.patch.object(P, "_project_id", return_value="proj-test"):
                    sa = P.load_service_account_json({})
        self.assertEqual(sa["client_email"], FAKE_SA["client_email"])

    def test_log_row_dry_run_never_opens_network(self):
        entry = P.build_entry(
            acting_seat="GB-ORACLE",
            requesting_seat="CURSOR",
            request_source="r",
            action_taken="a",
            result="done",
        )
        with mock.patch.object(P, "_http_opener") as build:
            out = P.log_row(entry, dry_run=True, env={"GOOGLE_SHEETS_SA": json.dumps(FAKE_SA)})
            build.assert_not_called()
        self.assertTrue(out["dry_run"])
        self.assertEqual(len(out["row"]), 6)

    def test_append_retries_429(self):
        sheets = FakeSheets([429, 200])
        slept: list[float] = []

        def token():
            return "tok"

        client = P.SheetsClient(
            spreadsheet_id="sheet123",
            token_provider=token,
            opener=mock.Mock(open=sheets.open),
            sleep=slept.append,
        )
        updated = client.append(["when", "A", "B", "c", "d", "e"])
        self.assertEqual(updated["updates"]["updatedRows"], 1)
        self.assertEqual(len(slept), 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
