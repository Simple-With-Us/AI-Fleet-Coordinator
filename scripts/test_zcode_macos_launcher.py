import importlib.util
import pathlib
import unittest

p = pathlib.Path(__file__).parents[1] / "zcode-macos-wake-launcher.py"
if not p.exists():
    p = pathlib.Path(__file__).with_name("zcode-macos-wake-launcher.py")
spec = importlib.util.spec_from_file_location("mac_launcher", p)
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


class LauncherTests(unittest.TestCase):
    def test_exact_arguments_and_no_environment_mutation(self):
        env = {"HOME": "/Users/example", "PATH": "/usr/bin:/bin"}
        calls = []
        args = ["--cwd", "/Users/example/Repo", "--resume", "sess_exact", "-p", "literal $message"]
        launcher.launch(args, env, exec_fn=lambda *v: calls.append(v), is_file=lambda _: True)
        binary, actual, child = calls[0]
        self.assertEqual(binary, launcher.NODE)
        self.assertEqual(actual, [launcher.NODE, launcher.CLI, *args])
        self.assertEqual(child[launcher.BUILTIN_ENV], launcher.BUILTIN)
        self.assertEqual(env, {"HOME": "/Users/example", "PATH": "/usr/bin:/bin"})
        self.assertNotIn("ZCODE_PERSONAL_PROVIDER_CONFIG_FILE", child)

    def test_explicit_paths_and_other_environment_are_preserved(self):
        env = {
            launcher.BUILTIN_ENV: "/existing/builtin.json",
            "ZCODE_PERSONAL_PROVIDER_CONFIG_FILE": "/existing/personal.json",
        }
        calls = []
        launcher.launch([], env, exec_fn=lambda *v: calls.append(v), is_file=lambda _: True)
        self.assertEqual(calls[0][2], env)

    def test_empty_builtin_uses_bundled_resource(self):
        calls = []
        launcher.launch(
            [],
            {launcher.BUILTIN_ENV: "  "},
            exec_fn=lambda *v: calls.append(v),
            is_file=lambda _: True,
        )
        self.assertEqual(calls[0][2][launcher.BUILTIN_ENV], launcher.BUILTIN)

    def test_missing_any_resource_never_launches(self):
        for missing in [launcher.NODE, launcher.CLI, launcher.BUILTIN]:
            calls = []
            with self.assertRaisesRegex(RuntimeError, "^zcode-macos-launch-resource-unavailable$"):
                launcher.launch(
                    [], {}, exec_fn=lambda *v: calls.append(v), is_file=lambda p: p != missing
                )
            self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
