import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

root = Path(__file__).parent
spec = importlib.util.spec_from_file_location("diagnostic_native", root / "zcode_native.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class Diagnostics(unittest.TestCase):
    def test_only_fixed_labels_persist(self):
        self.assertEqual(
            m.compact_diagnostic(
                {
                    "stage": "subscribeConversationV4",
                    "code": "project.remote-project-required",
                }
            ),
            {
                "stage": "subscribeConversationV4",
                "code": "project.remote-project-required",
            },
        )
        for value in [
            None,
            [],
            {"stage": [], "code": {}},
            {"stage": "private-value", "code": "x" * 100000},
            {"stage": "verify", "code": "rpc-refused", "stack": "secret"},
        ]:
            self.assertEqual(
                m.compact_diagnostic(value),
                {"stage": "driver", "code": "refused-or-uncertain"},
            )

    def test_dispatch_diagnostic_does_not_enable_resubmit(self):
        with tempfile.TemporaryDirectory(dir="/dev/shm") as folder:
            calls = []
            binding = {
                "host": "server",
                "local": True,
                "session_id": "session",
                "workspace": "/fixture",
                "generation": 1,
            }

            def transport(request):
                calls.append(request["action"])
                return {
                    "ack": None,
                    "diagnostic": {
                        "stage": "subscribeConversationV4",
                        "code": "project.remote-project-required",
                    },
                }

            journal = m.NativeDelivery(Path(folder), transport, lambda _: True)
            try:
                result = journal.deliver(binding, "wake", "fixture", fresh=True)
                self.assertEqual(result["outcome"], "unknown")
                self.assertEqual(result["diagnostic"]["code"], "project.remote-project-required")
                stored = json.loads(next(Path(folder).glob("*.json")).read_text())
                self.assertEqual(stored["receipt"]["diagnostic"], result["diagnostic"])
                journal.deliver(binding, "wake", "fixture", fresh=True)
                self.assertEqual(calls, ["dispatch", "query"])
            finally:
                journal.close()


if __name__ == "__main__":
    unittest.main()
