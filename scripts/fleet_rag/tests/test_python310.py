"""Keep the installed fleet-rag path runnable on its Python 3.10 floor."""

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class Python310CompatibilityTests(unittest.TestCase):
    def test_no_datetime_utc(self):
        offenders = [
            path.relative_to(ROOT).as_posix()
            for path in ROOT.rglob("*.py")
            if ("datetime." + "UTC") in path.read_text()
        ]
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
