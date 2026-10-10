"""FleetLink aliases share the composer's app identity in create, filter and stats."""
import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("mac_collab_normalization", HERE / "mac-collab-server.py")
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)

ALIASES = ("fleetlink", "FleetLink", "fleetlink.online", "FleetLink.Online", "fleet link", "FL", " fl ")


class AppNormalizationTests(unittest.TestCase):
    def test_fleetlink_aliases_are_canonical(self):
        for alias in ALIASES:
            with self.subTest(alias=alias):
                self.assertEqual(server.normalize_app(alias), "fleetlink")

    def test_other_apps_and_unknown_values_are_unchanged(self):
        for value, expected in (("BF", "botfleet"), ("AFC", "fleet-infra"), ("New App", "new app"), ("", "")):
            with self.subTest(value=value):
                self.assertEqual(server.normalize_app(value), expected)

    def test_create_filter_and_stats_use_one_bucket(self):
        # No network or live board: real handlers and SQLite against a temporary DB.
        with tempfile.TemporaryDirectory() as temp, patch.object(server, "authorized", return_value="OWNER"):
            with patch.object(server, "DB_PATH", Path(temp) / "findings.db"):
                server.init_db()
                handler = object.__new__(server.Handler)
                handler._send = lambda status, body: (status, body)
                for index, alias in enumerate(ALIASES):
                    handler._read_json_body = lambda alias=alias, index=index: ({"app": alias, "title": f"Finding {index}"}, None)
                    status, body = handler._handle_finding_create()
                    self.assertEqual(status, 201)
                    self.assertEqual(body["app"], "fleetlink")
                for alias in ALIASES:
                    status, body = handler._handle_findings_list({"app": [alias]})
                    self.assertEqual(status, 200)
                    self.assertEqual(body["total_matching"], len(ALIASES))
                    self.assertEqual({row["app"] for row in body["findings"]}, {"fleetlink"})
                status, body = handler._handle_findings_stats()
                self.assertEqual(status, 200)
                self.assertEqual(body["apps"], ["fleetlink"])


if __name__ == "__main__":
    unittest.main()
