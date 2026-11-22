"""The finite runtime must include the relay's complete relative-import closure."""

import ast
import runpy
import unittest
from pathlib import Path


class RuntimeInventory(unittest.TestCase):
    def test_explicit_runtime_inventory_covers_all_local_module_imports(self):
        relay = Path(__file__).resolve().parents[2]
        builder = runpy.run_path(str(relay / "build-runtime.py"))
        modules = set(builder["MODULES"])
        files = builder["FILES"]
        self.assertEqual(len(modules), len(builder["MODULES"]))
        expected = {
            "git-relay/bin/git-relay": "100755",
            "scripts/fleet_infisical.py": "100644",
            **{f"git-relay/git_relay/{name}.py": "100644" for name in modules},
        }
        self.assertEqual(files, expected)
        missing = {}
        for name in modules:
            tree = ast.parse((relay / "git_relay" / f"{name}.py").read_text())
            imports = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.level:
                    self.assertEqual(node.level, 1, f"unexpected package escape in {name}")
                    if node.module:
                        imports.add(node.module.split(".")[0])
                    else:
                        imports.update(alias.name for alias in node.names)
            if imports - modules:
                missing[name] = sorted(imports - modules)
        self.assertEqual(missing, {}, "runtime inventory omits imported relay modules")
        self.assertTrue({"pr_owners", "service_read"} <= modules)


if __name__ == "__main__":
    unittest.main()
