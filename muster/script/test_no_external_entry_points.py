"""Muster exposes no way for another process to start an apply.

AFC-391:  no URL scheme, AppleScript, App Intents or Shortcuts entry point may expose apply.
This checks the project definition and the Swift sources for the declarations that would
create one, and the built Info.plist too when MUSTER_BUILT_APP names a built bundle.
"""

import os
import pathlib
import plistlib
import re
import unittest

MUSTER = pathlib.Path(__file__).resolve().parent.parent

FORBIDDEN_PLIST_KEYS = (
    "CFBundleURLTypes",
    "NSAppleScriptEnabled",
    "OSAScriptingDefinition",
    "NSServices",
    "NSUserActivityTypes",
    "CFBundleDocumentTypes",
)

FORBIDDEN_SWIFT = (
    r"\.onOpenURL",
    r"handlesExternalEvents",
    r"\bAppIntent\b",
    r"\bAppShortcutsProvider\b",
    r"\bAppEntity\b",
    r"\bNSAppleScript\b",
    r"\bNSAppleEventManager\b",
    r"application\(\s*_\s*\w+\s*:\s*NSApplication\s*,\s*open",
    r"\bNSUserActivity\b",
    r"\bonContinueUserActivity\b",
)


class NoExternalEntryPointTests(unittest.TestCase):
    def test_project_definition_declares_no_entry_point_keys(self):
        text = (MUSTER / "project.yml").read_text()
        for key in FORBIDDEN_PLIST_KEYS:
            self.assertNotIn(key, text, key)
        self.assertIsNone(
            re.search(r"(?<![A-Z_])INFOPLIST_FILE", text),
            "a hand-written Info.plist could declare one",
        )
        self.assertIn("GENERATE_INFOPLIST_FILE: YES", text)

    def test_no_hand_written_info_plist_or_intent_definitions(self):
        sources = MUSTER / "Muster"
        for pattern in ("*.plist", "*.sdef", "*.intentdefinition", "*.entitlements"):
            self.assertEqual(list(sources.rglob(pattern)), [], pattern)

    def test_swift_sources_use_no_entry_point_api(self):
        for path in (MUSTER / "Muster").rglob("*.swift"):
            code = "\n".join(
                line for line in path.read_text().splitlines() if not line.lstrip().startswith("//")
            )
            for pattern in FORBIDDEN_SWIFT:
                self.assertIsNone(re.search(pattern, code), "%s matches %s" % (path.name, pattern))

    @unittest.skipUnless(
        os.environ.get("MUSTER_BUILT_APP"), "set MUSTER_BUILT_APP to a built Muster.app"
    )
    def test_built_bundle_info_plist(self):
        info = pathlib.Path(os.environ["MUSTER_BUILT_APP"]) / "Contents" / "Info.plist"
        data = plistlib.loads(info.read_bytes())
        for key in FORBIDDEN_PLIST_KEYS:
            self.assertNotIn(key, data, key)


if __name__ == "__main__":
    unittest.main()
