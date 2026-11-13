"""Installed closure runs independently and refuses unsafe replacement input."""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import stat

SCRIPTS = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("mirror_installer", SCRIPTS/"install-code-mirrors-runtime.py")
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class InstalledRuntimeTests(unittest.TestCase):
    def test_installed_help_and_local_ref_sweep_need_no_other_packages(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dest = root/"installed"
            installer.install(SCRIPTS.parent, dest, "0"*40)
            entry = dest/"scripts/fleet_lanes/code_main_sync.py"
            env = {"HOME": str(root), "PATH": os.environ["PATH"], "FLEET_CODE_ROOT":str(root/"Code"),
                   "FLEET_APPS_ROOT":str(root/"apps"), "FLEET_LANES_ROOT":str(root/"apps/lanes")}
            result = subprocess.run([sys.executable,"-I",str(entry),"--help"],env=env,capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            repo = root/"Code/BotFleet"
            repo.mkdir(parents=True)
            def git(*args):
                return subprocess.run(["/usr/bin/git","-C",str(repo),*args],env={**env,"GIT_AUTHOR_NAME":"fixture","GIT_AUTHOR_EMAIL":"fixture@example.invalid","GIT_COMMITTER_NAME":"fixture","GIT_COMMITTER_EMAIL":"fixture@example.invalid"},capture_output=True,text=True,check=True).stdout.strip()
            git("init","-q","-b","main")
            git("commit","-q","--allow-empty","-m","fixture")
            git("remote","add","origin","https://github.com/Simple-With-Us/BotFleet.git")
            git("update-ref","refs/remotes/origin/main",git("rev-parse","HEAD"))
            git("branch","--set-upstream-to=origin/main","main")
            result = subprocess.run([sys.executable,"-I",str(entry),"--dry-run"],env=env,capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertIn("current=1",result.stdout)
            self.assertFalse(list(dest.rglob("__pycache__")))
            # Dynamic settings helper discovery must find this bundle's own core.
            code = "import sys;sys.path.insert(0,"+repr(str(dest/"scripts"))+");from fleet_knobs import source;assert source._load_helper() is not None"
            result = subprocess.run([sys.executable,"-I","-B","-c",code],env=env,capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            upgraded = installer.install(SCRIPTS.parent, dest, "1"*40)
            self.assertTrue(Path(upgraded["backup"]).is_dir())
            self.assertFalse(list(dest.rglob("__pycache__")))

    def test_failed_upgrade_restores_executable_entrypoint_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp)/"installed"
            installer.install(SCRIPTS.parent,dest,"0"*40)
            entry = dest/"scripts/code-main-keeper.sh"
            previous = stat.S_IMODE(entry.stat().st_mode)
            rename = os.rename
            def fail_stage(source, target):
                if Path(source).name.startswith(".code-mirrors-stage-"):
                    raise OSError("fixture install failure")
                return rename(source,target)
            with mock.patch.object(installer.os,"rename",side_effect=fail_stage), self.assertRaises(OSError):
                installer.install(SCRIPTS.parent,dest,"1"*40)
            self.assertEqual(stat.S_IMODE(entry.stat().st_mode),previous)
            self.assertTrue(os.access(entry,os.X_OK))

    def test_missing_dependency_and_unknown_existing_files_are_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(ValueError):
                installer.install(root/"missing-source",root/"missing-dest","0"*40)
            self.assertFalse((root/"missing-dest").exists())
            dest = root/"installed"
            installer.install(SCRIPTS.parent,dest,"0"*40)
            unknown = dest/"private-config"
            unknown.write_text("preserve")
            with self.assertRaises(ValueError):
                installer.install(SCRIPTS.parent,dest,"1"*40)
            self.assertEqual(unknown.read_text(),"preserve")

    def test_backup_maintenance_failure_restores_complete_active_bundle(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            dest=root/"installed"
            installer.install(SCRIPTS.parent,dest,"0"*40)
            manifest=(dest/"manifest.json").read_bytes()
            before={p.relative_to(dest).as_posix():(p.read_bytes(),stat.S_IMODE(p.stat().st_mode)) for p in dest.rglob("*") if p.is_file()}
            chmod=os.chmod
            def fail_backup(path,mode,*args,**kwargs):
                if Path(path).name.startswith("installed.backup-"):
                    raise PermissionError("fixture backup maintenance failure")
                return chmod(path,mode,*args,**kwargs)
            with mock.patch.object(installer.os,"chmod",side_effect=fail_backup),self.assertRaises(PermissionError):
                installer.install(SCRIPTS.parent,dest,"1"*40)
            self.assertTrue(dest.is_dir())
            self.assertEqual((dest/"manifest.json").read_bytes(),manifest)
            self.assertEqual({p.relative_to(dest).as_posix():(p.read_bytes(),stat.S_IMODE(p.stat().st_mode)) for p in dest.rglob("*") if p.is_file()},before)
            self.assertTrue(os.access(dest/"scripts/code-main-keeper.sh",os.X_OK))
            self.assertFalse(list(root.glob(".code-mirrors-stage-*")))
            self.assertFalse(list(root.glob("installed.backup-*")))
