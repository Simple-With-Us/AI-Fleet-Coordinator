"""Host load helpers for Mac wake backpressure."""
from __future__ import annotations

import unittest

from agent_sync import host_load as HL


class HostLoadTests(unittest.TestCase):
    def test_host_busy_uses_load_per_core(self) -> None:
        load = HL.HostLoad(load1=29.0, cores=10)
        self.assertFalse(HL.host_busy(load, max_per_core=3.0))
        self.assertTrue(HL.host_busy(HL.HostLoad(load1=30.0, cores=10), max_per_core=3.0))
        self.assertIsNone(HL.host_busy_reason(load, max_per_core=3.0))
        reason = HL.host_busy_reason(HL.HostLoad(load1=40.0, cores=10), max_per_core=3.0)
        self.assertEqual(reason, "Host is busy (load 4 per core)")

    def test_a_missing_reading_never_blocks(self) -> None:
        self.assertFalse(HL.host_busy(None, max_per_core=3.0))
        self.assertIsNone(HL.host_busy_reason(None, max_per_core=3.0))


if __name__ == "__main__":
    unittest.main()
