"""Offline installer checks; the root filesystem and the installing account's home are simulated."""

import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import types
import unittest
import uuid
from pathlib import Path
from unittest import mock

# Standalone discovery also needs the resolved repo-only fixture package.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fleet_infisical_cli_tests.installed_writer_fixture import assert_installed_writer
from fleet_knobs.tests._fake import ScopedReadAPI

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent
SETTINGS = json.dumps({"applyEnabled": False})


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve() / "root"
        self.root.mkdir()
        self.module = types.ModuleType("installer_test")
        text = (HERE / "install-resource-control.sh").read_text()
        body = text.split("<<'PY'\n", 1)[1].rsplit("\nPY\n", 1)[0]
        exec(compile(body, "install-resource-control.sh", "exec"), self.module.__dict__)
        self.module.ROOT = str(self.root)
        # The account running the tests is the installing account, and never counts as root,
        # even where the tests themselves run as root.
        self.module.ROOT_UID = -1
        self.home = self.root / "home/owner"
        self.home.mkdir(parents=True)
        for folder in (self.root / "home", self.home):
            folder.chmod(0o755)  # whatever the umask:  the installer refuses a group-writable one
        self.module.home_components = lambda uid: ("home", "owner")
        self.destination = self.home / ".local/lib/fleet-resource-control"

    def install(self, source=SCRIPTS):
        with (
            mock.patch.object(self.module.os, "geteuid", return_value=os.getuid()),
            mock.patch.object(self.module.os, "fsync"),
            mock.patch.object(self.module.sys, "platform", "linux"),
        ):
            return self.module.install(source)

    def test_fixed_payload_modes_isolated_imports_and_preserved_state(self):
        credential = self.home / ".config/fleet-tools/resource-policy-token"
        credential.parent.mkdir(parents=True)
        credential.write_text("untouched-test-credential")
        credential.chmod(0o600)
        state = self.root / "run/fleet-coding-resources/status.json"
        state.parent.mkdir(parents=True)
        state.write_text('{"untouched":true}')
        self.install()
        expected = {
            "fleet-ops-control.py",
            "fleet-ops-control.json",
            "fleet-ops-eavesdropper-settings.json",
            "fleet_resource_control.py",
            "fleet_seat_inventory.py",
            "muster_approval.py",
            "fleet_infisical.py",
            "fleet_infisical_cli.py",
            "muster_trust_files.py",
            "reduced_status.py",
            "hetzner/__init__.py",
            "hetzner/fleet_coding_memory.py",
            "hetzner/coding_resources.py",
            "simple_tools/__init__.py",
            "simple_tools/operator_github_state.py",
        }
        actual = {
            str(path.relative_to(self.destination))
            for path in self.destination.rglob("*")
            if path.is_file()
        }
        self.assertEqual(actual, expected)
        for path in (
            self.destination,
            self.destination / "hetzner",
            self.destination / "simple_tools",
        ):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o755)
        for relative in expected:
            path = self.destination / relative
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o644)
            self.assertEqual(path.stat().st_uid, os.getuid())
        self.assertEqual(credential.read_text(), "untouched-test-credential")
        self.assertEqual(state.read_text(), '{"untouched":true}')
        # The `--from-muster` modules are installed byte for byte, so that the shared /etc/muster
        # reader is the same file wherever it is installed.
        self.assertEqual(
            (self.destination / "muster_approval.py").read_bytes(),
            (SCRIPTS / "muster_approval.py").read_bytes(),
        )
        # The reader has one source file, in muster_agent/, and is installed flat beside the helper.
        self.assertEqual(
            (self.destination / "muster_trust_files.py").read_bytes(),
            (SCRIPTS / "muster_agent" / "muster_trust_files.py").read_bytes(),
        )
        trap = self.root / "untrusted-cwd"
        trap.mkdir()
        (trap / "fleet_resource_control.py").write_text("raise RuntimeError('untrusted import')")
        # The two `--from-muster` modules import `cryptography` (the system package on the server);
        # a machine without it still checks every other import, then reports the test skipped.
        has_cryptography = importlib.util.find_spec("cryptography") is not None
        muster_imports = ",muster_approval,muster_trust_files" if has_cryptography else ""
        code = (
            "import sys,runpy;sys.path.insert(0,sys.argv[1]);"
            "import fleet_resource_control,fleet_seat_inventory,reduced_status,fleet_infisical,fleet_infisical_cli,hetzner.fleet_coding_memory"
            + muster_imports
            + ";runpy.run_path(sys.argv[1]+'/fleet-ops-control.py');print('imports-ok')"
        )
        result = subprocess.run(
            [sys.executable, "-I", "-c", code, str(self.destination)],
            cwd=trap,
            env={"PYTHONPATH": str(trap)},
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "imports-ok")
        for name in ("__init__.py", "operator_github_state.py"):
            self.assertEqual(
                (self.destination / "simple_tools" / name).read_bytes(),
                (SCRIPTS / "simple_tools" / name).read_bytes(),
            )
        assert_installed_writer(self, self.destination, self.home)
        self.install()  # Existing protected installation can be refreshed without state changes.
        self.assertEqual(credential.read_text(), "untouched-test-credential")
        if not has_cryptography:
            self.skipTest(
                "cryptography is not installed: muster_approval and muster_trust_files were not "
                "imported (everything else in this test ran)"
            )

    def test_installed_reader_uses_checked_sibling_and_real_bounded_worker(self):
        project = str(uuid.uuid4())
        coolify_token = "synthetic-coolify-token-value"
        trap = self.root / "reader-untrusted-cwd"
        trap.mkdir()
        home = self.root / "reader-home"
        if home.is_symlink():
            raise ValueError(f"refusing symlink: {home}")
        home.mkdir(mode=0o700)

        def invoke(api, payload=None):
            code = r"""
import importlib.util, json, os, pathlib, sys, types
installed = pathlib.Path(sys.argv[1])
sys.path.insert(0, str(installed))
foreign = types.ModuleType("fleet_infisical")
foreign.bounded_fetch_project = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("foreign reader used"))
sys.modules["fleet_infisical"] = foreign
spec = importlib.util.spec_from_file_location("installed_control", installed / "fleet-ops-control.py")
control = importlib.util.module_from_spec(spec)
spec.loader.exec_module(control)
calls = []
ctx = control.Context(
    platform="linux", uid=os.getuid(), home=pathlib.Path(os.environ["TEST_HOME"]),
    environ={key: value for key, value in os.environ.items() if key in {
        "FLEET_OPS_CONTROL_INFISICAL_TOKEN", "INFISICAL_FLEET_OPS_PROJECT_ID",
        "FLEET_SECRETS_API", "INFISICAL_ENVIRONMENT", "INFISICAL_ENV",
    }},
    http=lambda url, headers: calls.append(url) or 200,
)
answer, status = control.check_token(ctx)
print(json.dumps({
    "answer": answer, "status": status, "httpCalls": calls,
    "foreignStillLoaded": sys.modules.get("fleet_infisical") is foreign,
    "ledgerImported": "simple_tools.operator_github_state" in sys.modules,
}))
"""
            env = {
                "PATH": str(trap),
                "TEST_HOME": str(home),
                "FLEET_OPS_CONTROL_INFISICAL_TOKEN": api.token,
                "INFISICAL_FLEET_OPS_PROJECT_ID": project,
                "FLEET_SECRETS_API": "http://127.0.0.1:%d/api" % api.server.server_address[1],
                "INFISICAL_ENVIRONMENT": "prod",
            }
            result = subprocess.run(
                [sys.executable, "-I", "-c", code, str(payload or self.destination)],
                cwd=trap,
                env=env,
                capture_output=True,
                text=True,
                timeout=45,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn(api.token, result.stdout)
            self.assertNotIn(api.token, " ".join(result.args))
            return json.loads(result.stdout)

        # The consumer receives only a synthetic protected process binding.  This route uses
        # the copied sibling source and the real disposable worker against the loopback fixture.
        self.install()
        with ScopedReadAPI(
            {"COOLIFY_API_TOKEN": coolify_token},
            project=project,
            path="/coolify",
        ) as api:
            api.record_override = {"secretValueHidden": False}
            success = invoke(api)
            self.assertEqual(success["status"], 0, success)
            self.assertEqual(len(success["httpCalls"]), 1)
            self.assertTrue(success["foreignStillLoaded"])
            self.assertFalse(success["ledgerImported"])
            self.assertEqual(
                api.requests,
                [
                    "/api/v2/service-token",
                    "/api/v3/secrets/raw/COOLIFY_API_TOKEN?workspaceId="
                    + project
                    + "&environment=prod&secretPath=%2Fcoolify&viewSecretValue=true"
                    "&expandSecretReferences=false&recursive=false&include_imports=false",
                ],
            )

        # A real read-only payload needs no compatibility package or writer ledger in either
        # interpreter.  The bounded worker executes only this copied canonical source.
        reader_only = self.root / "read-only-payload"
        if reader_only.is_symlink():
            raise ValueError(f"refusing symlink: {reader_only}")
        reader_only.mkdir(mode=0o700)
        for name in ("fleet-ops-control.py", "fleet_infisical.py"):
            target = reader_only / name
            target.write_bytes((self.destination / name).read_bytes())
            target.chmod(0o600)
        with ScopedReadAPI(
            {"COOLIFY_API_TOKEN": coolify_token}, project=project, path="/coolify"
        ) as api:
            api.record_override = {"secretValueHidden": False}
            isolated = invoke(api, reader_only)
            self.assertEqual(isolated["status"], 0, isolated)
            self.assertFalse(isolated["ledgerImported"])
            self.assertEqual(len(api.requests), 2)
        self.assertEqual(list(reader_only.rglob("*.sqlite*")), [])

        # Bad scope is refused by the canonical grant verifier before the injected Coolify
        # request can observe any token.
        with ScopedReadAPI(
            {"COOLIFY_API_TOKEN": coolify_token},
            project=project,
            path="/wrong-folder",
        ) as api:
            api.record_override = {"secretValueHidden": False}
            refused = invoke(api)
            self.assertEqual(refused["status"], 1, refused)
            self.assertEqual(refused["httpCalls"], [])
            self.assertEqual(api.requests, ["/api/v2/service-token"])

        # A broken installed canonical sibling fails closed before either provider request.
        reader = self.destination / "fleet_infisical.py"
        original = reader.read_bytes()
        try:
            reader.unlink()
            with ScopedReadAPI(
                {"COOLIFY_API_TOKEN": coolify_token}, project=project, path="/coolify"
            ) as api:
                missing = invoke(api)
                self.assertEqual(missing["status"], 1, missing)
                self.assertEqual(missing["httpCalls"], [])
                self.assertEqual(api.requests, [])
            reader.write_text("raise RuntimeError('broken copied reader')\n")
            reader.chmod(0o644)
            with ScopedReadAPI(
                {"COOLIFY_API_TOKEN": coolify_token},
                project=project,
                path="/coolify",
            ) as api:
                api.record_override = {"secretValueHidden": False}
                broken = invoke(api)
                self.assertEqual(broken["status"], 1, broken)
                self.assertEqual(broken["httpCalls"], [])
                self.assertEqual(api.requests, [])
        finally:
            reader.write_bytes(original)

        self.assertEqual(list(home.rglob("*")), [])

    def test_symlink_destination_directory_refused(self):
        lib = self.home / ".local/lib"
        lib.mkdir(parents=True)
        for folder in (self.home / ".local", lib):
            folder.chmod(0o755)
        outside = self.root / "outside"
        outside.mkdir()
        self.destination.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(OSError):
            self.install()
        self.assertFalse(list(outside.iterdir()))

    def test_writable_destination_directory_refused(self):
        self.destination.mkdir(parents=True)
        for folder in (self.home / ".local", self.home / ".local/lib"):
            folder.chmod(0o755)
        self.destination.chmod(0o777)
        with self.assertRaises(RuntimeError):
            self.install()
        self.assertFalse(list(self.destination.iterdir()))

    def test_a_writable_folder_above_the_installation_is_refused_before_any_write(self):
        for folder in (self.root / "home", self.home):
            with self.subTest(folder=folder.name):
                folder.chmod(0o775)
                with self.assertRaises(RuntimeError):
                    self.install()
                self.assertFalse((self.home / ".local").exists())
                folder.chmod(0o755)
        self.install()  # the same tree with protected folders installs

    def test_a_missing_home_directory_is_never_created(self):
        self.module.home_components = lambda uid: ("home", "nobody-here")
        with self.assertRaises(OSError):
            self.install()
        self.assertFalse((self.root / "home/nobody-here").exists())

    def test_an_existing_private_lib_folder_is_kept_private(self):
        lib = self.home / ".local/lib"
        lib.mkdir(parents=True)
        (self.home / ".local").chmod(0o755)
        lib.chmod(0o700)
        self.install()
        self.assertEqual(stat.S_IMODE(lib.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.destination.stat().st_mode), 0o755)

    def test_the_eavesdropper_settings_allowlist_is_installed_and_must_have_its_switch(self):
        source = self.minimal_source()
        self.install(source)
        self.assertEqual(
            json.loads((self.destination / "fleet-ops-eavesdropper-settings.json").read_text()),
            {"applyEnabled": False},
        )
        (source / "fleet-ops-eavesdropper-settings.json").write_text(json.dumps({"settings": []}))
        with self.assertRaises(RuntimeError):
            self.install(source)

    REPOSITORY_FROM = (False, "the repository")

    @staticmethod
    def usable(value, **extra):
        """An installed allowlist the settings editor would accept, with this switch."""
        doc = {"schemaVersion": 1, "applyEnabled": value, "seat": {}, "tables": {}}
        doc.update(extra)
        return json.dumps(doc)

    def test_a_fresh_install_takes_the_repositorys_apply_enabled(self):
        repository = (SCRIPTS / "fleet-ops-eavesdropper-settings.json").read_bytes()
        self.assertIs(json.loads(repository)["applyEnabled"], False)
        self.assertEqual(self.install(), self.REPOSITORY_FROM)
        installed = self.destination / "fleet-ops-eavesdropper-settings.json"
        self.assertEqual(installed.read_bytes(), repository)

    def test_an_update_keeps_the_owners_apply_enabled_and_every_other_byte(self):
        repository = (SCRIPTS / "fleet-ops-eavesdropper-settings.json").read_bytes()
        self.install()
        installed = self.destination / "fleet-ops-eavesdropper-settings.json"
        # The owner turned apply on after the first install.
        turned_on = repository.replace(b'"applyEnabled": false', b'"applyEnabled": true', 1)
        self.assertNotEqual(turned_on, repository)
        installed.write_bytes(turned_on)
        self.assertEqual(self.install(), (True, "the installed copy"))
        self.assertEqual(installed.read_bytes(), turned_on)
        self.assertIs(json.loads(installed.read_bytes())["applyEnabled"], True)
        self.assertEqual(stat.S_IMODE(installed.stat().st_mode), 0o644)
        # An installed false stays false, with the repository's bytes.
        installed.write_bytes(repository)
        self.assertEqual(self.install(), (False, "the installed copy"))
        self.assertEqual(installed.read_bytes(), repository)

    LEGACY_FROM = "the root-owned copy in /usr/local/lib/fleet-resource-control"

    def make_legacy(self, value, file_mode=0o644, folder_mode=0o755, text=None):
        """The root-owned install the move replaces, owned here by the test account."""
        self.module.LEGACY_OWNER_UID = os.getuid()
        shutil.rmtree(self.root / "usr", ignore_errors=True)
        legacy = self.root / "usr/local/lib/fleet-resource-control"
        legacy.mkdir(parents=True)
        self.root.chmod(0o755)
        for folder in (
            self.root / "usr",
            self.root / "usr/local",
            self.root / "usr/local/lib",
            legacy,
        ):
            folder.chmod(folder_mode)
        settings = legacy / "fleet-ops-eavesdropper-settings.json"
        settings.write_text(self.usable(value) if text is None else text)
        settings.chmod(file_mode)
        return legacy

    def test_the_move_from_the_root_owned_install_keeps_its_apply_enabled(self):
        repository = (SCRIPTS / "fleet-ops-eavesdropper-settings.json").read_bytes()
        self.make_legacy(True)
        self.assertEqual(self.install(), (True, self.LEGACY_FROM))
        installed = self.destination / "fleet-ops-eavesdropper-settings.json"
        expected = repository.replace(b'"applyEnabled": false', b'"applyEnabled": true', 1)
        self.assertEqual(installed.read_bytes(), expected)
        # The root-owned copy is only read, never changed, and nothing is written beside it.
        legacy = (
            self.root / "usr/local/lib/fleet-resource-control/fleet-ops-eavesdropper-settings.json"
        )
        self.assertEqual(legacy.read_text(), self.usable(True))
        self.assertEqual(os.listdir(legacy.parent), ["fleet-ops-eavesdropper-settings.json"])

    def test_a_new_installed_copy_outranks_the_root_owned_one(self):
        self.install()
        self.make_legacy(True)
        self.assertEqual(self.install(), (False, "the installed copy"))
        installed = self.destination / "fleet-ops-eavesdropper-settings.json"
        self.assertIs(json.loads(installed.read_text())["applyEnabled"], False)

    def test_an_untrusted_or_unusable_root_owned_copy_is_never_kept(self):
        def group_writable_file():
            self.make_legacy(True, file_mode=0o664)

        def group_writable_folder():
            self.make_legacy(True, folder_mode=0o775)

        def another_owner():
            self.make_legacy(True)
            self.module.LEGACY_OWNER_UID = os.getuid() + 1

        def linked_folder():
            elsewhere = self.make_legacy(True)
            target = self.root / "elsewhere"
            shutil.rmtree(target, ignore_errors=True)
            elsewhere.rename(target)
            (self.root / "usr/local/lib/fleet-resource-control").symlink_to(target)

        def linked_file():
            legacy = self.make_legacy(True)
            target = self.root / "elsewhere.json"
            (legacy / "fleet-ops-eavesdropper-settings.json").rename(target)
            (legacy / "fleet-ops-eavesdropper-settings.json").symlink_to(target)

        def refused_by_the_editor():
            self.make_legacy(True, text=json.dumps({"applyEnabled": True}))

        cases = (
            group_writable_file,
            group_writable_folder,
            another_owner,
            linked_folder,
            linked_file,
            refused_by_the_editor,
        )
        for arrange in cases:
            with self.subTest(case=arrange.__name__):
                shutil.rmtree(self.home / ".local", ignore_errors=True)
                arrange()
                self.assertEqual(self.install(), self.REPOSITORY_FROM)
                installed = self.destination / "fleet-ops-eavesdropper-settings.json"
                self.assertIs(json.loads(installed.read_text())["applyEnabled"], False)

    def test_an_update_keeps_the_value_when_the_key_text_is_not_unique(self):
        # Two "applyEnabled" keys in the text:  the installer rewrites the parsed document
        # instead of editing text, and still sets only the top-level switch.
        source = self.minimal_source()
        doubled = {"applyEnabled": False, "example": {"applyEnabled": False}}
        (source / "fleet-ops-eavesdropper-settings.json").write_text(json.dumps(doubled))
        self.install(source)
        installed = self.destination / "fleet-ops-eavesdropper-settings.json"
        installed.write_text(self.usable(True))
        self.assertEqual(self.install(source), (True, "the installed copy"))
        self.assertEqual(
            json.loads(installed.read_text()),
            {"applyEnabled": True, "example": {"applyEnabled": False}},
        )

    def test_an_unusable_installed_copy_takes_the_repositorys_value(self):
        repository = (SCRIPTS / "fleet-ops-eavesdropper-settings.json").read_bytes()
        self.install()
        installed = self.destination / "fleet-ops-eavesdropper-settings.json"
        too_large = self.usable(True, notes=["x" * (64 * 1024)]).encode()
        for unusable in (
            b"not json",
            b"[]",
            b"{}",
            self.usable("yes").encode(),
            # The settings editor refuses these whole, so their switch never took effect.
            json.dumps({"applyEnabled": True}).encode(),
            self.usable(True, schemaVersion=2).encode(),
            self.usable(True, unknown=1).encode(),
            self.usable(True, tables={"seat": {}}).encode(),
            too_large,
        ):
            with self.subTest(unusable=unusable[:60]):
                installed.write_bytes(unusable)
                self.assertEqual(self.install(), self.REPOSITORY_FROM)
                self.assertEqual(installed.read_bytes(), repository)

    def test_a_linked_or_writable_installed_copy_fails_the_installation(self):
        # A copy owned by another account fails the same way (write_file's owner check), but a
        # test account cannot make one without root, so it is not arranged here.
        self.install()
        installed = self.destination / "fleet-ops-eavesdropper-settings.json"
        installed.chmod(0o664)
        with self.assertRaises(RuntimeError):
            self.install()
        installed.unlink()
        outside = self.root / "outside.json"
        outside.write_text(self.usable(True))
        installed.symlink_to(outside)
        with self.assertRaises(RuntimeError):
            self.install()
        self.assertEqual(outside.read_text(), self.usable(True))

    def test_a_copy_in_the_install_folder_stops_the_root_owned_one_from_being_read(self):
        # Once the install folder has a settings file, the move is over:  an unusable one takes
        # the repository's value, never a stale root-owned one.
        repository = (SCRIPTS / "fleet-ops-eavesdropper-settings.json").read_bytes()
        self.install()
        self.make_legacy(True)
        installed = self.destination / "fleet-ops-eavesdropper-settings.json"
        installed.write_bytes(b"not json")
        self.assertEqual(self.install(), self.REPOSITORY_FROM)
        self.assertEqual(installed.read_bytes(), repository)

    def test_symlink_installed_file_refused_without_touching_target(self):
        self.destination.mkdir(parents=True)
        outside = self.root / "outside"
        outside.write_text("untouched")
        (self.destination / "fleet-ops-control.py").symlink_to(outside)
        with self.assertRaises(RuntimeError):
            self.install()
        self.assertEqual(outside.read_text(), "untouched")

    def test_symlink_source_package_refused_before_destination_created(self):
        source = self.root / "source"
        source.mkdir()
        for name in (
            "fleet-ops-control.py",
            "fleet_resource_control.py",
            "fleet_seat_inventory.py",
            "reduced_status.py",
            "muster_approval.py",
            "fleet_infisical.py",
            "fleet_infisical_cli.py",
        ):
            (source / name).write_text("pass\n")
        (source / "muster_agent").mkdir()
        (source / "muster_agent" / "muster_trust_files.py").write_text("pass\n")
        (source / "fleet-ops-control.json").write_text(json.dumps({"schemaVersion": 1}))
        (source / "fleet-ops-eavesdropper-settings.json").write_text(SETTINGS)
        (source / "hetzner").symlink_to(SCRIPTS / "hetzner", target_is_directory=True)
        with self.assertRaises(OSError):
            self.install(source)
        self.assertFalse(self.destination.exists())

    def test_symlinked_reducer_source_refused_before_destination_created(self):
        source = self.root / "source"
        source.mkdir()
        for name in (
            "fleet-ops-control.py",
            "fleet_resource_control.py",
            "fleet_seat_inventory.py",
            "muster_approval.py",
            "fleet_infisical.py",
            "fleet_infisical_cli.py",
        ):
            (source / name).write_text("pass\n")
        (source / "muster_agent").mkdir()
        (source / "muster_agent" / "muster_trust_files.py").write_text("pass\n")
        (source / "fleet-ops-control.json").write_text(json.dumps({"schemaVersion": 1}))
        (source / "fleet-ops-eavesdropper-settings.json").write_text(SETTINGS)
        for package, names in (
            ("hetzner", ("fleet_coding_memory.py", "coding_resources.py")),
            ("simple_tools", ("__init__.py", "operator_github_state.py")),
        ):
            (source / package).mkdir()
            for name in names:
                (source / package / name).write_text("pass\n")
        real = self.root / "real-reduced-status.py"
        real.write_text("pass\n")
        (source / "reduced_status.py").symlink_to(real)
        with self.assertRaises(OSError):
            self.install(source)
        self.assertFalse(self.destination.exists())
        # The same source with a plain file installs, so the refusal was the symlink.
        (source / "reduced_status.py").unlink()
        (source / "reduced_status.py").write_text("pass\n")
        self.install(source)
        self.assertEqual((self.destination / "reduced_status.py").read_text(), "pass\n")

    def test_the_reducer_sits_beside_the_helper_in_the_scripts_folder(self):
        self.assertTrue((SCRIPTS / "reduced_status.py").is_file())
        self.assertTrue((SCRIPTS / "fleet-ops-control.py").is_file())
        self.assertFalse((SCRIPTS / "simple_tools" / "reduced_status.py").exists())

    def test_missing_writer_ledger_is_refused_before_destination_created(self):
        source = self.minimal_source()
        (source / "simple_tools/operator_github_state.py").unlink()
        with self.assertRaises(FileNotFoundError):
            self.install(source)
        self.assertFalse(self.destination.exists())

    def test_symlinked_writer_ledger_is_refused_before_destination_created(self):
        source = self.minimal_source()
        ledger = source / "simple_tools/operator_github_state.py"
        ledger.unlink()
        ledger.symlink_to(SCRIPTS / "simple_tools/operator_github_state.py")
        with self.assertRaises(OSError):
            self.install(source)
        self.assertFalse(self.destination.exists())

    def minimal_source(self, trust_text="pass\n"):
        """A small valid source tree.  `trust_text` None leaves out the `muster_agent` folder."""
        source = self.root / "source"
        source.mkdir()
        for name in (
            "fleet-ops-control.py",
            "fleet_resource_control.py",
            "fleet_seat_inventory.py",
            "reduced_status.py",
            "muster_approval.py",
            "fleet_infisical.py",
            "fleet_infisical_cli.py",
        ):
            (source / name).write_text("pass\n")
        (source / "fleet-ops-control.json").write_text(json.dumps({"schemaVersion": 1}))
        (source / "fleet-ops-eavesdropper-settings.json").write_text(SETTINGS)
        if trust_text is not None:
            (source / "muster_agent").mkdir()
            (source / "muster_agent" / "muster_trust_files.py").write_text(trust_text)
        for package, names in (
            ("hetzner", ("fleet_coding_memory.py", "coding_resources.py")),
            ("simple_tools", ("__init__.py", "operator_github_state.py")),
        ):
            (source / package).mkdir()
            for name in names:
                (source / package / name).write_text("pass\n")
        return source

    def test_the_trust_files_reader_comes_from_muster_agent_only_and_is_installed_flat(self):
        source = self.minimal_source("REAL = 1\n")
        # A flat file of the same name in the source folder is never the one that is installed.
        (source / "muster_trust_files.py").write_text("DECOY = 1\n")
        self.install(source)
        self.assertEqual((self.destination / "muster_trust_files.py").read_text(), "REAL = 1\n")
        self.assertFalse((self.destination / "muster_agent").exists())

    def test_a_source_without_muster_agent_or_with_a_linked_one_is_refused(self):
        source = self.minimal_source(None)
        (source / "muster_trust_files.py").write_text("DECOY = 1\n")
        with self.assertRaises(OSError):
            self.install(source)
        self.assertFalse(self.destination.exists())
        real = self.root / "elsewhere"
        real.mkdir()
        (real / "muster_trust_files.py").write_text("pass\n")
        (source / "muster_agent").symlink_to(real, target_is_directory=True)
        with self.assertRaises(OSError):
            self.install(source)
        self.assertFalse(self.destination.exists())

    def test_the_other_installed_copy_must_be_the_same_bytes_when_it_is_present(self):
        source = self.minimal_source("SAME = 1\n")
        self.install(source)  # no muster-agent copy exists: nothing to compare
        other = self.root / "usr/local/lib/muster-agent"
        other.mkdir(parents=True)
        (other / "muster_trust_files.py").write_text("SAME = 1\n")
        self.install(source)  # the same bytes: fine
        (other / "muster_trust_files.py").write_text("OTHER = 1\n")
        with self.assertRaises(RuntimeError):
            self.install(source)
        (other / "muster_trust_files.py").unlink()
        (other / "muster_trust_files.py").symlink_to(self.root / "elsewhere")
        with self.assertRaises(OSError):
            self.install(source)

    def test_a_linked_muster_agent_installation_folder_is_never_followed(self):
        # The link leads to a folder with the very same bytes, so that a link that is followed
        # would pass the comparison: only the refusal to follow it stops the install.
        source = self.minimal_source("SAME = 1\n")
        real = self.root / "elsewhere-agent"
        real.mkdir()
        (real / "muster_trust_files.py").write_text("SAME = 1\n")
        lib = self.root / "usr/local/lib"
        lib.mkdir(parents=True)
        (lib / "muster-agent").symlink_to(real, target_is_directory=True)
        with self.assertRaises(OSError):
            self.install(source)
        # The same folder as a real one, with the same bytes, is compared and passes.
        (lib / "muster-agent").unlink()
        (lib / "muster-agent").mkdir()
        (lib / "muster-agent" / "muster_trust_files.py").write_text("SAME = 1\n")
        self.install(source)

    def test_the_repository_has_exactly_one_source_of_the_trust_files_reader(self):
        listed = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=SCRIPTS.parent,
            capture_output=True,
            check=False,
        )
        if listed.returncode != 0:
            self.skipTest("not a git checkout")
        names = [
            path.decode()
            for path in listed.stdout.split(b"\0")
            if path.endswith(b"muster_trust_files.py")
        ]
        self.assertEqual(names, ["scripts/muster_agent/muster_trust_files.py"])
        text = (HERE / "install-resource-control.sh").read_text()
        # The installer names that one source path, and no other place the file could come from.
        self.assertIn("os.open('muster_agent', DIRECTORY_FLAGS, dir_fd=source)", text)
        self.assertIn("read_source(agent, 'muster_trust_files.py')", text)
        self.assertEqual(text.count("read_source(source, 'muster_trust_files.py')"), 0)

    def test_root_or_nonlinux_refused_before_any_destination_write(self):
        for uid, platform in ((0, "linux"), (os.getuid() or 1000, "darwin")):
            with (
                self.subTest(uid=uid, platform=platform),
                mock.patch.object(self.module, "ROOT_UID", 0),
                mock.patch.object(self.module.os, "geteuid", return_value=uid),
                mock.patch.object(self.module.sys, "platform", platform),
            ):
                with self.assertRaises(RuntimeError):
                    self.module.install(SCRIPTS)
                self.assertFalse(self.destination.exists())
                self.assertFalse((self.home / ".local").exists())

    def test_the_install_folder_is_below_the_account_databases_home_not_home_env(self):
        text = (HERE / "install-resource-control.sh").read_text()
        self.assertIn("pwd.getpwuid(uid).pw_dir", text)
        self.assertNotIn("environ", text)
        self.assertIn("DESTINATION = ('.local', 'lib', 'fleet-resource-control')", text)
        # The root-owned folder is named once, as the move's read-only source, and every line that
        # uses it reads:  nothing is ever installed there.
        legacy = "('usr', 'local', 'lib', 'fleet-resource-control')"
        self.assertEqual(text.count(legacy), 1)
        self.assertIn("LEGACY_DESTINATION = " + legacy, text)
        uses = [
            line.strip()
            for line in text.splitlines()
            if "LEGACY_DESTINATION" in line and not line.startswith("LEGACY_DESTINATION = ")
        ]
        self.assertEqual(
            uses,
            [
                "Only the fixed LEGACY_DESTINATION is read.  Every folder from ROOT down is opened without",
                "for component in LEGACY_DESTINATION:",
                "kept_from = 'the root-owned copy in /' + '/'.join(LEGACY_DESTINATION)",
            ],
        )


if __name__ == "__main__":
    unittest.main()
