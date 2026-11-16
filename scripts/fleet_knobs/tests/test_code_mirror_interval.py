"""The shared mirror interval stays bounded and in its narrow consumer folder."""

import unittest

from fleet_knobs import values
from fleet_knobs.registry import names_in, spec_for


class CodeMirrorIntervalTests(unittest.TestCase):
    def test_interval_bounds_and_dedicated_scope(self):
        spec = spec_for("CODE_MIRROR_INTERVAL_SECONDS")
        self.assertEqual(spec.default, 300)
        self.assertEqual(spec.folder, "/code-mirrors")
        self.assertEqual(
            names_in("/code-mirrors"), [spec.name, "DOCS_SYNC_ENABLED", "DOCS_SYNC_CONFIGURATION"]
        )
        self.assertEqual(spec.hosts, "both")
        self.assertFalse(spec.secret)
        self.assertEqual(spec.local, "deny")
        for raw, expected in (("30", 30), ("300", 300), ("86400", 86400)):
            with self.subTest(raw=raw):
                self.assertEqual(values.parse(spec, raw), expected)
        for raw in ("29", "86401", "-1", "300.5", "true"):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                values.parse(spec, raw)
