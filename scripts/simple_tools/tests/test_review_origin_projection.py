import secrets
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlencode
from unittest.mock import patch

from simple_tools import listener_registry as RG
from simple_tools import review_origin_projection as RP

SID = "019a1234-abcd-7123-8123-0123456789ab"
ROW = dict(seat="CODEX", state="active", label="AA", generation=7,
           verified_host="host", profile_scope="profile")


class ProjectionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name) / "missing.sqlite3")
        self.key = secrets.token_hex(24)
        self.opkey = secrets.token_hex(24)
        self.service = RG.RegistryService(self.path, {"OPERATOR": self.opkey},
                                         review_origin_read_key=self.key, clock=lambda: 1000)

    def call(self, *, fields=None, key=None, method="GET", path=RP.PATH, stamp="1000"):
        query = urlencode(fields if fields is not None else {"native_session_id": SID})
        headers = {"X-Simple-Tools-Timestamp": stamp,
                   "X-Simple-Tools-Signature": RP.signature(key or self.key, query, stamp)}
        return self.service.handle(method, path + "?" + query, headers, b"")

    def test_listener_route_with_only_dedicated_key_and_no_cache(self):
        import urllib.request
        import json
        from simple_tools import listener_api as API
        service = RG.RegistryService(self.path, {}, review_origin_read_key=self.key, clock=lambda: 1000)
        server = API.ListenerReadApi("127.0.0.1", 0, self.tmp.name, {}, registry=service, clock=lambda: 1000)
        server.start()
        self.addCleanup(server.close)
        host, port = server.address
        query = urlencode({"native_session_id": SID})
        request = urllib.request.Request(f"http://{host}:{port}{RP.PATH}?{query}", headers={
            "X-Simple-Tools-Timestamp": "1000", "X-Simple-Tools-Signature": RP.signature(self.key, query, "1000")})
        with patch.object(RP.OR, "registrations_for_native_id", return_value=[ROW]):
            with urllib.request.urlopen(request, timeout=3) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(response.headers["Cache-Control"], "no-store")
                self.assertEqual(json.load(response)["origin_session_claim"], SID)

    def test_exact_projection_and_minimization(self):
        with patch.object(RP.OR, "registrations_for_native_id", return_value=[ROW]) as lookup:
            status, answer = self.call()
            self.assertEqual(status, 200)
            self.assertEqual(answer, dict(origin_session_claim=SID, origin_letter_claim="AA", origin_generation_claim=7))
            lookup.assert_called_once_with(self.path, "codex", SID)

    def test_all_ineligible_and_ambiguous_rows_hold(self):
        for rows in ([], [ROW, ROW], [dict(ROW, seat="CLAUDE")], [dict(ROW, state="closed")],
                     [dict(ROW, label="A")], [dict(ROW, generation=True)],
                     [dict(ROW, generation=0)], [dict(ROW, profile_scope="")],
                     [dict(ROW, generation=2**53)]):
            with self.subTest(rows=rows), patch.object(RP.OR, "registrations_for_native_id", return_value=rows):
                self.assertEqual(self.call()[0], 409)

    def test_credentials_do_not_cross_auth_boundaries(self):
        with self.assertRaises(ValueError):
            RG.RegistryService(self.path, {"OPERATOR": self.key}, review_origin_read_key=self.key)
        self.assertEqual(self.call(key=self.opkey)[0], 401)
        self.assertEqual(self.call(stamp="699")[0], 401)
        self.assertEqual(self.call(method="POST")[0], 405)
        for op in RG.OPS:
            method = "GET" if op in RG.READ_OPS else "POST"
            path = RG.PREFIX + op
            headers = RG.headers(self.key, method, path, "", "OPERATOR", b"", 1000)
            self.assertEqual(self.service.handle(method, path, headers, b"")[0], 401, op)
        self.assertNotIn(self.key, self.service.keys.values())

    def test_only_exact_input_and_no_store_creation(self):
        for fields in ({"native_session_id": SID, "seat": "CODEX"}, {"native_session_id": SID.upper()},
                       {"native_session_id": SID.replace("-", "")}, {"session_id": SID}):
            self.assertEqual(self.call(fields=fields)[0], 400)
        self.assertEqual(self.call()[0], 503)
        self.assertFalse(Path(self.path).exists())
        disabled = RG.RegistryService(self.path, {}, clock=lambda: 1000)
        self.assertEqual(disabled.handle("GET", RP.PATH, {}, b"")[0], 404)

    def test_real_canonical_rows_close_reopen_and_cross_location_conflict(self):
        from simple_tools import operator_registry as OR
        registry = OR.SessionRegistry(self.path)
        registry.close_db()
        identity = dict(platform="codex", verified_host="host", profile_scope="profile", native_session_id=SID)
        opened = OR.register_session(self.path, **identity, seat="CODEX", source="test", issuer="OPERATOR")
        first = self.call()
        self.assertEqual(first[0], 200, first)
        self.assertEqual(first[1]["origin_letter_claim"], opened["label"])
        OR.close_session(self.path, **identity)
        self.assertEqual(self.call()[0], 409)
        self.assertEqual(self.service._op_reopen("OPERATOR", {"record_id": opened["record_id"]})[0], 200)
        self.assertEqual(self.call()[0], 200)
        OR.register_session(self.path, **dict(identity, verified_host="other-host"), seat="CODEX", source="test", issuer="OPERATOR")
        self.assertEqual(self.call()[0], 409)

    def test_query_is_signed_and_normal_routes_cannot_accept_projection_auth(self):
        self.assertEqual(self.call(path=RG.PREFIX + "resolve")[0], 404)
        self.assertEqual(self.call(fields=[("native_session_id", SID), ("native_session_id", SID)])[0], 400)


if __name__ == "__main__":
    unittest.main()
