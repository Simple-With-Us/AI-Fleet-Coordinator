#!/usr/bin/env python3
"""Tests for scripts/fleet-work-backup/fleet_work_backup.py.

Hermetic:  every repository, remote, file and "home" lives in a temp directory;  git runs with no
user or system config;  restic and gitleaks are stub scripts, so nothing is uploaded and nothing
under the real home is read.  Secret-looking strings are assembled at run time so this file holds
none.  Runs on macOS and Linux (CI is ubuntu).

  python3 scripts/fleet-work-backup/test_fleet_work_backup.py
"""
from __future__ import annotations

import argparse
import errno
import fcntl
import importlib.util
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
GENV = dict(os.environ)
GENV.update({"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0",
             "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@example.invalid",
             "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@example.invalid"})
os.environ.update({k: GENV[k] for k in ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM", "GIT_TERMINAL_PROMPT")})
for _name in ("B2_FLEET_BUCKET_NAME", "B2_FLEET_WORK_KEY_ID", "B2_FLEET_WORK_APPLICATION_KEY",
              "RESTIC_FLEET_WORK_PASSWORD", "FLEET_WORK_BACKUP_ALLOW_MASTER", "BACKBLAZE_MASTER_KEY_ID",
              "BACKBLAZE_MASTER_APPLICATION_KEY"):
    os.environ.pop(_name, None)

spec = importlib.util.spec_from_file_location("fleet_work_backup", HERE / "fleet_work_backup.py")
fwb = importlib.util.module_from_spec(spec)
sys.modules["fleet_work_backup"] = fwb
spec.loader.exec_module(fwb)

# Secret-shaped strings, assembled so no literal secret sits in this file.
AWS = "AKIA" + "Q7ZP3KM9TX2WJ4HB"
PEM = "-----BEGIN " + "RSA PRIVATE KEY-----"
HIGH = "".join(["aB3dE5", "gH7jK9", "mN1pQ3", "sT5v7x", "Zq2"])
PW = "".join(["pw-", "Zx9Qw3", "Er5Ty7", "Ui1Op"])
KEYID = "".join(["005", "abcdef0123", "456789012"])
APPKEY = "".join(["K005", "AbCdEfGhIjKlMn", "OpQrStUvWx1"])


def git(cwd, *args, check=True):
    p = subprocess.run(["git", "-c", "commit.gpgsign=false", "-c", "init.defaultBranch=main", "-C", str(cwd), *args],
                       env=GENV, capture_output=True)
    if check and p.returncode != 0:
        raise AssertionError(f"git {args} failed: {p.stderr.decode()}")
    return p.stdout.decode().strip()


def write(path: Path, text: str = "x\n", mode: int | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    if mode is not None:
        path.chmod(mode)
    return path


def make_repo(home: Path, path: Path, remote: str | None = "origin") -> Path:
    """A repository with one pushed commit (a.txt, b.txt) and, when ``remote`` is set, a bare origin."""
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q")
    write(path / "a.txt", "a\n")
    write(path / "b.txt", "b\n")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "base")
    if remote:
        bare = home / "remotes" / f"{path.name}.git"
        bare.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "-c", "init.defaultBranch=main", "init", "-q", "--bare", str(bare)], env=GENV, check=True)
        git(path, "remote", "add", remote, str(bare))
        git(path, "push", "-q", "-u", remote, "main")
    return path


def snapshot_tree(root: Path) -> list:
    """Every file under a .git directory with its size and mtime.  The mtime of an object or pack file
    is zeroed:  git's own merge machinery refreshes the mtime of objects it finds again (so gc keeps
    them), and that is the one change a read of the repository may cause."""
    out = []
    for dp, dn, fn in os.walk(root):
        for f in fn:
            p = os.path.join(dp, f)
            st = os.lstat(p)
            rel = os.path.relpath(p, root)
            out.append((rel, st.st_size, 0 if rel.startswith("objects" + os.sep) else st.st_mtime_ns))
    return sorted(out)


class Fixture:
    """A fake home with a spread of checkouts and loose files."""

    def __init__(self):
        self.tmp = tempfile.mkdtemp(prefix="fwb-test-")
        self.home = Path(self.tmp) / "home"
        self.home.mkdir()
        h = self.home
        # 1. pushed and clean:  must leave nothing behind
        self.clean = make_repo(h, h / "Code" / "pushed-clean")
        # 2. unpushed commit on main plus a feature branch, with a linked dirty worktree
        self.unpushed = make_repo(h, h / "Code" / "unpushed")
        write(self.unpushed / "a.txt", "a2\n")
        git(self.unpushed, "commit", "-q", "-am", "local only on main")
        git(self.unpushed, "checkout", "-q", "-b", "claude/feature")
        write(self.unpushed / "feature.txt", "feature\n")
        git(self.unpushed, "add", "-A")
        git(self.unpushed, "commit", "-q", "-m", "feature work")
        git(self.unpushed, "checkout", "-q", "main")
        self.lane = h / "apps" / "lanes" / "unpushed" / "claude-wt"
        self.lane.parent.mkdir(parents=True)
        git(self.unpushed, "worktree", "add", "-q", "-b", "claude/wt", str(self.lane))
        write(self.lane / "a.txt", "dirty in the lane\n")
        write(self.lane / "lane-new.txt", "new in the lane\n")
        # 3. flat dirty checkout with the secret cases
        self.dirty = make_repo(h, h / "apps" / "flat-dirty")
        write(self.dirty / ".gitignore", "ignored.txt\n")
        git(self.dirty, "add", "-A")
        git(self.dirty, "commit", "-q", "-m", "ignore")
        git(self.dirty, "push", "-q")
        write(self.dirty / "a.txt", "a changed\n")                       # tracked change, fine
        write(self.dirty / "leaky.py", "k = 1\n")
        git(self.dirty, "add", "leaky.py")
        git(self.dirty, "commit", "-q", "-m", "leaky base")
        git(self.dirty, "push", "-q")
        write(self.dirty / "leaky.py", f'k = "{AWS}"\n')                  # tracked change holding a key
        write(self.dirty / "tokenizer.py", "t = 1\n")                     # untracked, broad name -> dropped
        write(self.dirty / "plain.txt", "plain untracked\n")
        write(self.dirty / "ignored.txt", "never\n")
        write(self.dirty / ".env", "SOMETHING=1\n")
        write(self.dirty / "id_rsa", "not really\n")
        write(self.dirty / "notes.md", f"key {AWS}\n")                    # untracked, content secret
        write(self.dirty / "sub" / "keep.txt", "keep\n")
        write(self.dirty / ".env.example", "SOMETHING=\n")
        # 4. two stashes, the second with an untracked file
        self.stash = make_repo(h, h / "apps" / "stash-repo")
        write(self.stash / "a.txt", "A1\n")
        git(self.stash, "stash", "push", "-q", "-m", "one")
        write(self.stash / "b.txt", "B1\n")
        write(self.stash / "new.txt", "untracked in stash\n")
        git(self.stash, "stash", "push", "-q", "-u", "-m", "two")
        # 5. no remote at all
        self.noremote = make_repo(h, h / "apps" / "no-remote", remote=None)
        write(self.noremote / "more.txt", "m\n")
        git(self.noremote, "add", "-A")
        git(self.noremote, "commit", "-q", "-m", "second")
        # 6. a branch whose change already landed on main (squash-merge look-alike)
        self.landed = make_repo(h, h / "apps" / "landed")
        git(self.landed, "checkout", "-q", "-b", "claude/landed")
        write(self.landed / "f.txt", "feature\n")
        git(self.landed, "add", "-A")
        git(self.landed, "commit", "-q", "-m", "feature branch commit")
        git(self.landed, "checkout", "-q", "main")
        write(self.landed / "f.txt", "feature\n")
        git(self.landed, "add", "-A")
        git(self.landed, "commit", "-q", "-m", "squashed")
        git(self.landed, "push", "-q")
        # 7. detached HEAD with a commit no remote has
        self.detached = make_repo(h, h / "apps" / "detached")
        git(self.detached, "checkout", "-q", "--detach")
        write(self.detached / "d.txt", "d\n")
        git(self.detached, "add", "-A")
        git(self.detached, "commit", "-q", "-m", "detached work")
        # 8. a worktree whose gitdir is gone
        dangling = h / "apps" / "lanes" / "old" / "dangling"
        dangling.mkdir(parents=True)
        (dangling / ".git").write_text("gitdir: /nonexistent/gone/.git/worktrees/x\n")
        # loose files (b)
        a = h / "apps"
        write(a / "AGENT-SYNC.md", "protocol\n")
        write(a / "notes" / "x.txt", "note\n")
        write(a / "node_modules" / "pkg" / "index.js", "x\n")
        write(a / "run.log", "log\n")
        write(a / "__pycache__" / "m.pyc", "x\n")
        write(a / ".venv" / "lib" / "x.py", "x\n")
        write(a / "logs" / "today.txt", "x\n")
        write(a / "secrets" / "foo.txt", "s\n")
        write(a / "config-token.json", "{}\n")
        write(a / "helper.sh", f"export SERVICE_API_KEY={HIGH}\n")
        write(a / "ok-helper.sh", "echo hi\n")
        write(a / "bigfile.bin", "0" * (2 * 1024 * 1024))
        (a / "link-to-notes").symlink_to(a / "notes")
        (a / "scratch-env").mkdir()
        write(a / "scratch-env" / ".env.example", "A=\n")
        db = a / "mac-collab" / "findings.db"
        db.parent.mkdir(parents=True)
        self.keep_open = sqlite3.connect(db)
        self.keep_open.execute("pragma journal_mode=wal")
        self.keep_open.execute("create table t(x)")
        self.keep_open.executemany("insert into t values (?)", [(i,) for i in range(50)])
        self.keep_open.commit()
        write(h / "Code" / "loose.py", "print('hi')\n")
        self.cfg = fwb.Config(home=h)
        self.cfg.gitleaks_bin = None
        self.cfg.max_file_bytes = 1024 * 1024
        self.cfg.min_free_bytes = 1
        self.cfg.workers = 2

    def close(self):
        try:
            self.keep_open.close()
        except sqlite3.Error:
            pass
        shutil.rmtree(self.tmp, ignore_errors=True)


class StageCase(unittest.TestCase):
    """One build_stage over the shared fixture."""

    @classmethod
    def setUpClass(cls):
        cls.fx = Fixture()
        cls.git_before = {p: snapshot_tree(p / ".git") for p in (cls.fx.unpushed, cls.fx.landed, cls.fx.dirty)}
        cls.stage, cls.disc, cls.wts, cls.groups, cls.files, cls.gate, _ = fwb.build_stage(cls.fx.cfg, cls.fx.cfg.stage)
        cls.rep = fwb.build_report(cls.fx.cfg, cls.disc, cls.wts, cls.groups, cls.stage, cls.files, cls.gate, 0)
        cls.root = cls.fx.cfg.stage

    @classmethod
    def tearDownClass(cls):
        cls.fx.close()

    def rels(self, prefix=""):
        return sorted(r for r in self.stage.items if r.startswith(prefix))

    def group(self, name):
        return next(g for g in self.groups if g.name == name)

    def stage_text(self):
        return b"".join(p.read_bytes() for p in self.root.rglob("*") if p.is_file())

    # discovery ---------------------------------------------------------------------------
    def test_discovery_finds_each_checkout_once(self):
        names = sorted(os.path.basename(p) for p in self.disc.checkouts)
        self.assertEqual(names.count("claude-wt"), 1)
        for n in ("pushed-clean", "unpushed", "flat-dirty", "stash-repo", "no-remote", "landed", "detached"):
            self.assertEqual(names.count(n), 1, n)

    def test_dangling_gitdir_is_noted_not_fatal(self):
        self.assertTrue(any(n.startswith("dangling-gitdir") for n in self.disc.notes))
        self.assertFalse(any(p.endswith("dangling") for p in self.disc.checkouts))

    def test_linked_worktree_shares_its_repository_group(self):
        g = self.group("unpushed")
        self.assertEqual(g.worktrees, 2)

    # (a) unpushed refs --------------------------------------------------------------------
    def test_one_bundle_per_repository_not_per_worktree(self):
        bundles = [r for r in self.rels("git/") if r.endswith("unpushed.bundle") and f"/{self.group('unpushed').slug}/" in r]
        self.assertEqual(len(bundles), 1)

    def test_bundle_holds_unpushed_branches_and_verifies(self):
        b = self.root / f"git/{self.group('unpushed').slug}/unpushed.bundle"
        heads = subprocess.run(["git", "bundle", "list-heads", str(b)], env=GENV, capture_output=True, text=True).stdout
        for ref in ("refs/heads/main", "refs/heads/claude/feature", "refs/heads/claude/wt"):
            if ref == "refs/heads/claude/wt":
                continue  # the lane branch points at pushed history
            self.assertIn(ref, heads)
        fresh = Path(self.fx.tmp) / "restore-unpushed"
        git(Path(self.fx.tmp), "clone", "-q", str(self.fx.home / "remotes" / "unpushed.git"), str(fresh))
        git(fresh, "bundle", "verify", str(b))
        git(fresh, "fetch", "-q", str(b), "refs/heads/*:refs/restored/*")
        self.assertEqual(git(fresh, "show", "refs/restored/claude/feature:feature.txt"), "feature")
        self.assertEqual(git(fresh, "show", "refs/restored/main:a.txt"), "a2")

    def test_clean_pushed_repo_stages_nothing(self):
        self.assertEqual(self.rels(f"git/{self.group('pushed-clean').slug}/"), [])
        self.assertFalse(self.group("pushed-clean").has_work)

    def test_landed_branch_is_skipped(self):
        g = self.group("landed")
        self.assertEqual(g.landed_refs, 1)
        self.assertEqual(g.unpushed_refs, 0)
        self.assertFalse(g.bundle)

    def test_no_remote_repo_is_flagged_and_fully_bundled(self):
        g = self.group("no-remote")
        self.assertTrue(g.no_remote)
        self.assertTrue(g.bundle)
        self.assertIn("no-remote", self.rep["groups_no_remote"])

    def test_detached_head_commit_is_bundled(self):
        g = self.group("detached")
        self.assertTrue(any(r.endswith("head.bundle") and f"/{g.slug}/" in r for r in self.stage.items))

    # (a) dirty work -----------------------------------------------------------------------
    def test_patch_per_file_applies_onto_the_recorded_head(self):
        g = self.group("flat-dirty")
        patches = self.rels(f"git/{g.slug}/worktrees/")
        patches = [r for r in patches if "/patches/" in r]
        self.assertEqual(len(patches), 1, patches)  # a.txt only:  leaky.py was dropped alone
        fresh = Path(self.fx.tmp) / "restore-dirty"
        git(Path(self.fx.tmp), "clone", "-q", str(self.fx.home / "remotes" / "flat-dirty.git"), str(fresh))
        git(fresh, "apply", str(self.root / patches[0]))
        self.assertEqual((fresh / "a.txt").read_text(), "a changed\n")

    def test_worktree_metadata_records_head_and_branch(self):
        g = self.group("flat-dirty")
        meta = [r for r in self.rels(f"git/{g.slug}/worktrees/") if r.endswith("worktree.json")]
        data = json.loads((self.root / meta[0]).read_text())
        self.assertEqual(data["head"], git(self.fx.dirty, "rev-parse", "HEAD"))
        self.assertEqual(data["branch"], "main")

    def test_untracked_files_follow_git_ignore_and_name_filter(self):
        g = self.group("flat-dirty")
        un = [r.split("/untracked/", 1)[1] for r in self.rels(f"git/{g.slug}/worktrees/") if "/untracked/" in r]
        self.assertIn("plain.txt", un)
        self.assertIn("sub/keep.txt", un)
        self.assertIn(".env.example", un)
        for bad in ("ignored.txt", ".env", "id_rsa", "tokenizer.py", "notes.md"):
            self.assertNotIn(bad, un)

    def test_tracked_patch_keeps_a_file_the_broad_names_would_drop(self):
        d = self.fx.dirty
        write(d / "token-utils.py", "v = 1\n")
        git(d, "add", "token-utils.py")
        git(d, "commit", "-q", "-m", "tracked token util")
        write(d / "token-utils.py", "v = 2\n")
        try:
            cfg = self.fx.cfg
            stage, _, _, groups, _, _, _ = fwb.build_stage(cfg, Path(self.fx.tmp) / "stage-tracked")
            self.assertTrue(any("token-utils.py" in r for r in stage.items))
        finally:
            git(d, "reset", "-q", "--hard", "HEAD~1")
            write(d / "a.txt", "a changed\n")
            write(d / "leaky.py", f'k = "{AWS}"\n')

    def test_second_worktree_untracked_and_change(self):
        g = self.group("unpushed")
        files = self.rels(f"git/{g.slug}/worktrees/")
        self.assertTrue(any(r.endswith("lane-new.txt") for r in files))
        self.assertTrue(any("/patches/" in r for r in files))

    def test_both_stashes_are_captured_and_restorable(self):
        g = self.group("stash-repo")
        self.assertEqual(g.stashes, 2)
        base = git(self.fx.stash, "rev-parse", "HEAD")
        fresh = Path(self.fx.tmp) / "restore-stash"
        git(Path(self.fx.tmp), "clone", "-q", str(self.fx.home / "remotes" / "stash-repo.git"), str(fresh))
        sdir = self.root / f"git/{g.slug}/stashes"
        meta0 = json.loads((sdir / "00/stash.json").read_text())
        meta1 = json.loads((sdir / "01/stash.json").read_text())
        self.assertEqual(meta0["base"], base)
        self.assertEqual(meta1["subject"].split(": ")[-1], "one")
        # stash 01 ("one") changed a.txt;  stash 00 ("two") changed b.txt and added new.txt
        git(fresh, "checkout", "-q", base)
        for patch in sorted((sdir / "01/tracked").glob("*.patch")):
            git(fresh, "apply", str(patch))
        self.assertEqual((fresh / "a.txt").read_text(), "A1\n")
        git(fresh, "checkout", "-q", "--", ".")
        for part in ("tracked", "untracked"):
            for patch in sorted((sdir / f"00/{part}").glob("*.patch")):
                git(fresh, "apply", str(patch))
        self.assertEqual((fresh / "b.txt").read_text(), "B1\n")
        self.assertEqual((fresh / "new.txt").read_text(), "untracked in stash\n")

    # the gate ------------------------------------------------------------------------------
    def test_no_secret_value_reaches_the_stage(self):
        blob = self.stage_text()
        for needle in (AWS.encode(), PEM.encode(), HIGH.encode()):
            self.assertNotIn(needle, blob)

    def test_dropped_list_names_paths_and_reasons_only(self):
        reasons = {d["reason"] for d in self.stage.dropped}
        self.assertIn("secret-name", reasons)
        self.assertTrue(any(r.startswith("content:") for r in reasons))
        text = json.dumps(self.stage.dropped)
        self.assertNotIn(AWS, text)
        self.assertNotIn(HIGH, text)
        paths = " ".join(d["path"] for d in self.stage.dropped)
        for p in ("leaky.py", "tokenizer.py", "notes.md", "config-token.json", "helper.sh", "foo.txt", ".env", "id_rsa"):
            self.assertIn(p, paths)

    # (b) loose files ----------------------------------------------------------------------
    def test_loose_files_staged_and_junk_skipped(self):
        files = self.rels("files/")
        for want in ("files/apps/AGENT-SYNC.md", "files/apps/notes/x.txt", "files/apps/ok-helper.sh",
                     "files/apps/scratch-env/.env.example", "files/Code/loose.py"):
            self.assertIn(want, files)
        for junk in ("node_modules", "run.log", "__pycache__", ".venv", "logs/today.txt", "secrets/foo.txt",
                     "config-token.json", "helper.sh\x00", "lanes/"):
            self.assertFalse(any(junk in r for r in files if r != "files/apps/ok-helper.sh"), junk)
        self.assertNotIn("files/apps/helper.sh", files)

    def test_checkouts_are_not_staged_as_loose_files(self):
        self.assertFalse(any("flat-dirty" in r for r in self.rels("files/")))

    def test_symlink_is_kept_as_a_symlink(self):
        p = self.root / "files/apps/link-to-notes"
        self.assertTrue(p.is_symlink())

    def test_large_file_is_listed_not_staged(self):
        self.assertTrue(any(f["path"].endswith("bigfile.bin") for f in self.stage.large))
        self.assertNotIn("files/apps/bigfile.bin", self.stage.items)

    def test_live_sqlite_is_snapshotted_with_wal_content(self):
        rel = "files/apps/mac-collab/findings.db"
        self.assertIn(rel, self.stage.items)
        self.assertEqual(self.stage.items[rel].kind, "sqlite")
        con = sqlite3.connect(self.root / rel)
        try:
            self.assertEqual(con.execute("select count(*) from t").fetchone()[0], 50)
        finally:
            con.close()
        self.assertFalse(any(r.endswith(("-wal", "-shm")) for r in self.stage.items))

    # reading other seats' repos is read-only ---------------------------------------------
    def test_git_dirs_are_untouched(self):
        for path, before in self.git_before.items():
            self.assertEqual(snapshot_tree(path / ".git"), before, path)

    def test_report_counts(self):
        r = self.rep
        self.assertEqual(r["groups_with_unpushed"], 2)  # unpushed, no-remote
        self.assertEqual(r["landed_refs_skipped"], 1)
        self.assertEqual(r["stashes"], 2)
        self.assertGreater(r["staged_bytes"], 0)
        self.assertEqual(r["gate"]["gitleaks"], False)


class NameAndContentCase(unittest.TestCase):
    def test_secret_names(self):
        yes = [".env", ".env.local", "prod.env", "a/b/.env.production", "x.pem", "AuthKey_AB12.p8", "k.key", "id_rsa",
               "id_ed25519.pub", "my-zuliprc", "global-api-keys", "a/.secrets/x", "a/.ssh/config", "credentials.json",
               "mytoken.txt", "app-secret.yml", "Foo.KEYCHAIN-DB", "infisical-machine-identity.json"]
        no = [".env.example", "src/app.py", "README.md", "a/b/c.txt", "environment.md"]
        for p in yes:
            self.assertTrue(fwb.is_secret_name(p), p)
        for p in no:
            self.assertFalse(fwb.is_secret_name(p), p)

    def test_tracked_patches_skip_only_the_broad_globs(self):
        self.assertTrue(fwb.is_secret_name("src/.env", tracked=True))
        self.assertTrue(fwb.is_secret_name("deploy/server.pem", tracked=True))
        self.assertFalse(fwb.is_secret_name("src/tokenizer.py", tracked=True))
        self.assertFalse(fwb.is_secret_name("src/secrets_policy.md", tracked=True))
        self.assertTrue(fwb.is_secret_name("src/tokenizer.py", tracked=False))

    def test_content_patterns(self):
        for text in (AWS, PEM, "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8", "xoxb-" + "8271936450-Qm7ZkP3tXw",
                     "sk-ant-" + "api03-Zq8Lm2Vx7Rt4Yp9Kd3Hs",
                     "eyJhbGciOiJIUzI1NiJ9." + "eyJzdWIiOiIxMjM0NTY3ODkwIn0." + "abcdefghijk1234567",
                     f"SERVICE_API_KEY={HIGH}", f'export DB_PASSWORD="{HIGH}"'):
            self.assertIsNotNone(fwb.scan_bytes(text.encode()), text[:12])

    def test_documentation_placeholders_are_not_secrets(self):
        for text in ("AKIA" + "IOSFODNN7EXAMPLE", "sk_live_" + "x" * 24, "sk_test_" + "0" * 24,
                     "ghp_" + "a" * 36, "AKIA" + "ABCDEFGHIJKLMNOP"):
            self.assertIsNone(fwb.scan_bytes(text.encode()), text)
        self.assertIsNotNone(fwb.scan_bytes(("sk_live_" + "9f2b7c41d8e3a605b2c7f914").encode()))

    def test_content_patterns_leave_ordinary_text_alone(self):
        for text in ("API_KEY=changeme-changeme-changeme", "TOKEN=${TOKEN}", "const token = getToken();",
                     "SERVICE_API_KEY=abcdefghijklmnopqrstuvwxyz", "see AKIA docs", "password: required",
                     "STRIPE_SECRET_KEY=your-stripe-key-here-0123"):
            self.assertIsNone(fwb.scan_bytes(text.encode()), text)

    def test_scan_file_covers_binary_and_chunk_seams(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x.bin"
            blob = b"\x00" * (8 * 1024 * 1024 - 5) + AWS.encode() + b"\x00" * 100
            p.write_bytes(blob)
            self.assertEqual(fwb.scan_file(p, 64 * 1024 * 1024), "aws-access-key-id")

    def test_url_credentials_are_stripped(self):
        self.assertEqual(fwb.strip_url_credentials("https://user:tok@github.com/a/b.git"), "https://github.com/a/b.git")


class RunCase(unittest.TestCase):
    """cmd_run end to end against a stub restic."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fwb-run-"))
        self.home = self.tmp / "home"
        (self.home / "apps").mkdir(parents=True)
        write(self.home / "apps" / "doc.md", "doc\n")
        make_repo(self.home, self.home / "Code" / "r")
        write(self.home / "Code" / "r" / "a.txt", "changed and committed, not pushed\n")
        git(self.home / "Code" / "r", "commit", "-q", "-am", "unpushed")
        self.cfg = fwb.Config(home=self.home)
        self.cfg.gitleaks_bin = None
        self.cfg.min_free_bytes = 1
        self.cfg.workers = 2
        self.cfg.max_runtime = 0
        self.cfg.load_fn = lambda: 1.0
        self.cfg.swap_fn = lambda: 0.1
        self.cfg.now_fn = lambda: 1_800_000_000.0
        self.stubdir = self.tmp / "stub"
        self.stubdir.mkdir()
        stub = self.stubdir / "restic"
        stub.write_text(
            "#!/bin/bash\n"
            'echo "$@" >> "$STUB_DIR/argv.log"\n'
            '{ echo "REPO=${RESTIC_REPOSITORY:-}"; echo "PWLEN=${#RESTIC_PASSWORD}"; echo "AKLEN=${#AWS_ACCESS_KEY_ID}";'
            ' echo "SKLEN=${#AWS_SECRET_ACCESS_KEY}"; echo "REGION=${AWS_DEFAULT_REGION:-}"; } >> "$STUB_DIR/env.log"\n'
            'if [ "$1" = backup ]; then\n'
            '  [ -n "$STUB_FAIL" ] && { echo "boom" >&2; exit 1; }\n'
            '  ls "${@: -1}" > "$STUB_DIR/stage-listing.txt"\n'
            '  echo \'{"message_type":"status"}\'\n'
            '  echo \'{"message_type":"summary","snapshot_id":"abcdef0123456789","data_added":4096,"total_files_processed":3}\'\n'
            "fi\nexit 0\n")
        stub.chmod(0o755)
        os.environ["STUB_DIR"] = str(self.stubdir)
        os.environ.pop("STUB_FAIL", None)
        self.cfg.restic_bin = str(stub)
        self.set_creds()
        self.args = argparse.Namespace(dry_run=False, json=None, keep_stage=False, ignore_pressure=False, skip_gitleaks=False)

    def tearDown(self):
        os.environ.pop("STUB_DIR", None)
        os.environ.pop("STUB_FAIL", None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def set_creds(self, names=("B2_FLEET_BUCKET_NAME", "B2_FLEET_WORK_KEY_ID", "B2_FLEET_WORK_APPLICATION_KEY",
                               "RESTIC_FLEET_WORK_PASSWORD")):
        vals = {"B2_FLEET_BUCKET_NAME": '"test-bucket"', "B2_FLEET_WORK_KEY_ID": f'"{KEYID}"',
                "B2_FLEET_WORK_APPLICATION_KEY": f'"{APPKEY}"', "RESTIC_FLEET_WORK_PASSWORD": f'"{PW}"'}
        handoff = self.home / ".secrets" / "global-api-keys"
        handoff.parent.mkdir(parents=True, exist_ok=True)
        handoff.write_text("OTHER=1\n" + "".join(f"{n}={vals[n]}\n" for n in names))
        handoff.chmod(0o600)

    def log(self) -> str:
        p = self.cfg.log_path
        return p.read_text() if p.exists() else ""

    def argv(self) -> list[str]:
        p = self.stubdir / "argv.log"
        return p.read_text().splitlines() if p.exists() else []

    def test_dry_run_uploads_nothing_and_reports(self):
        out = self.tmp / "report.json"
        self.args.dry_run, self.args.json = True, str(out)
        self.assertEqual(fwb.cmd_run(self.cfg, self.args), 0)
        self.assertEqual(self.argv(), [])
        self.assertFalse(self.cfg.log_path.exists())
        rep = json.loads(out.read_text())
        self.assertEqual(rep["unpushed_refs"], 1)
        self.assertEqual(rep["non_git_files"], 1)
        self.assertFalse(self.cfg.dry_stage.exists())

    def test_real_run_backs_up_and_logs_one_line(self):
        self.assertEqual(fwb.cmd_run(self.cfg, self.args), 0)
        argv = self.argv()
        backup = next(a for a in argv if a.startswith("backup"))
        for want in ("--tag fleet-work", "--host jay-mac", "--ignore-inode", "--json"):
            self.assertIn(want, backup)
        self.assertTrue(backup.endswith(str(self.cfg.stage)))
        forget = next(a for a in argv if a.startswith("forget"))
        for want in ("--keep-hourly 24", "--keep-daily 14", "--keep-weekly 8", "--keep-monthly 6", "--tag fleet-work",
                     "--prune"):
            self.assertIn(want, forget)
        lines = self.log().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertIn("status=ok", lines[0])
        self.assertIn("snapshot=abcdef01", lines[0])
        self.assertRegex(lines[0], r"^\d{4}-\d\d-\d\d \d{1,2}:\d\d(am|pm) ")
        self.assertFalse(self.cfg.stage.exists())

    def test_credentials_go_to_restic_through_the_environment_only(self):
        fwb.cmd_run(self.cfg, self.args)
        for line in self.argv():
            for secret in (PW, KEYID, APPKEY):
                self.assertNotIn(secret, line)
        env = (self.stubdir / "env.log").read_text()
        self.assertIn("REPO=s3:https://s3.eu-central-003.backblazeb2.com/test-bucket/fleet-work-backup", env)
        self.assertIn(f"PWLEN={len(PW)}", env)
        self.assertIn(f"AKLEN={len(KEYID)}", env)
        self.assertIn(f"SKLEN={len(APPKEY)}", env)
        self.assertIn("REGION=eu-central-003", env)
        for secret in (PW, KEYID, APPKEY):
            self.assertNotIn(secret, self.log())
            self.assertNotIn(secret, env)

    def test_prune_runs_at_most_once_a_day(self):
        t = [1_800_000_000.0]
        self.cfg.now_fn = lambda: t[0]
        fwb.cmd_run(self.cfg, self.args)
        t[0] += 3 * 3600
        fwb.cmd_run(self.cfg, self.args)
        forgets = [a for a in self.argv() if a.startswith("forget")]
        self.assertEqual(len(forgets), 2)
        self.assertIn("--prune", forgets[0])
        self.assertNotIn("--prune", forgets[1])
        t[0] += 22 * 3600
        fwb.cmd_run(self.cfg, self.args)
        self.assertIn("--prune", [a for a in self.argv() if a.startswith("forget")][2])

    def test_a_scheduled_run_never_inits(self):
        fwb.cmd_run(self.cfg, self.args)
        self.assertFalse(any(a.startswith("init") for a in self.argv()))
        fwb.cmd_init(self.cfg, self.args)
        self.assertTrue(any(a.startswith("init") for a in self.argv()))

    def test_restic_passthrough_uses_the_job_environment(self):
        ns = argparse.Namespace(restic_args=["--", "snapshots", "--tag", "fleet-work"])
        self.assertEqual(fwb.cmd_restic(self.cfg, ns), 0)
        self.assertEqual(self.argv(), ["snapshots --tag fleet-work"])
        self.assertIn(f"PWLEN={len(PW)}", (self.stubdir / "env.log").read_text())

    def test_restic_failure_is_an_error_line(self):
        os.environ["STUB_FAIL"] = "1"
        self.assertEqual(fwb.cmd_run(self.cfg, self.args), 4)
        self.assertIn("status=error reason=restic-backup-failed", self.log())
        self.assertFalse(any(a.startswith("forget") for a in self.argv()))

    def test_missing_credentials_name_the_keys_only(self):
        self.set_creds(names=("B2_FLEET_BUCKET_NAME", "B2_FLEET_WORK_KEY_ID"))
        self.assertEqual(fwb.cmd_run(self.cfg, self.args), 2)
        line = self.log()
        self.assertIn("reason=missing-credentials", line)
        self.assertIn("B2_FLEET_WORK_APPLICATION_KEY", line)
        self.assertIn("RESTIC_FLEET_WORK_PASSWORD", line)
        self.assertEqual(self.argv(), [])

    def test_master_key_is_only_a_fallback_when_asked(self):
        handoff = self.home / ".secrets" / "global-api-keys"
        handoff.write_text(f"B2_FLEET_BUCKET_NAME=b\nRESTIC_FLEET_WORK_PASSWORD={PW}\n"
                           f"BACKBLAZE_MASTER_KEY_ID={KEYID}\nBACKBLAZE_MASTER_APPLICATION_KEY={APPKEY}\n")
        env, missing = fwb.restic_env(self.cfg)
        self.assertIsNone(env)
        self.assertIn("B2_FLEET_WORK_KEY_ID", missing)
        with mock.patch.dict(os.environ, {"FLEET_WORK_BACKUP_ALLOW_MASTER": "1"}):
            env, missing = fwb.restic_env(self.cfg)
        self.assertEqual(missing, [])
        self.assertEqual(env["AWS_ACCESS_KEY_ID"], KEYID)

    def test_restic_missing(self):
        self.cfg.restic_bin = str(self.tmp / "no-such-restic")
        self.assertEqual(fwb.cmd_run(self.cfg, self.args), 2)
        self.assertIn("reason=restic-missing", self.log())

    def test_pressure_gate_skips_and_counts(self):
        self.cfg.load_fn = lambda: 90.0
        self.assertEqual(fwb.cmd_run(self.cfg, self.args), 0)
        self.assertIn("status=skip reason=load1=90.0>60", self.log())
        self.assertEqual(self.argv(), [])
        self.cfg.load_fn = lambda: 1.0
        self.cfg.swap_fn = lambda: 0.97
        fwb.cmd_run(self.cfg, self.args)
        self.assertIn("reason=swap=97%>=95%", self.log())
        self.assertIn("consecutive=2", self.log())

    def test_pressure_gate_warns_after_a_day_of_skips(self):
        self.cfg.load_fn = lambda: 99.0
        for _ in range(8):
            fwb.cmd_run(self.cfg, self.args)
        last = self.log().splitlines()[-1]
        self.assertIn("consecutive=8", last)
        self.assertIn("warn=no-backup-for-about-24h", last)
        self.assertNotIn("warn=", self.log().splitlines()[6])

    def test_success_resets_the_skip_counter(self):
        self.cfg.load_fn = lambda: 99.0
        fwb.cmd_run(self.cfg, self.args)
        self.cfg.load_fn = lambda: 1.0
        fwb.cmd_run(self.cfg, self.args)
        self.assertEqual(fwb.read_state(self.cfg)["consecutive_skips"], 0)

    def test_overlapping_run_is_refused_by_the_lock(self):
        self.cfg.state_dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.cfg.state_dir / "lock"), os.O_CREAT | os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(fwb.cmd_run(self.cfg, self.args), 0)
        finally:
            os.close(fd)
        self.assertIn("status=skip reason=lock-held", self.log())
        self.assertEqual(self.argv(), [])

    def test_unreadable_external_volume_is_reported_clearly(self):
        real_scandir = os.scandir
        lanes = self.home / "apps" / "lanes"
        (lanes / "Repo").mkdir(parents=True)

        def fake(path):
            if str(path).endswith("lanes/Repo"):
                raise PermissionError(errno.EPERM, "Operation not permitted", str(path))
            return real_scandir(path)

        with mock.patch.object(fwb.os, "scandir", fake):
            self.assertEqual(fwb.cmd_run(self.cfg, self.args), 0)
        line = self.log().splitlines()[-1]
        self.assertIn("status=partial", line)
        self.assertIn("warn=removable-volumes-denied", line)
        self.assertIn(f"grant={fwb.python_binary()}", line)
        self.assertIn("Removable Volumes", self.cfg.detail_log_path.read_text())
        self.assertTrue(any(a.startswith("backup") for a in self.argv()))  # the readable part still goes up

    def test_gitleaks_findings_drop_the_flagged_file(self):
        stub = self.tmp / "gitleaks"
        stub.write_text(
            "#!/bin/bash\n"
            'while [ $# -gt 0 ]; do [ "$1" = "-r" ] && rep="$2"; [ "$1" = dir ] && root="$2"; shift; done\n'
            'printf \'[{"RuleID":"stub-rule","File":"%s/files/apps/doc.md"}]\' "$root" > "$rep"\nexit 0\n')
        stub.chmod(0o755)
        self.cfg.gitleaks_bin = str(stub)
        self.args.dry_run, self.args.json = True, str(self.tmp / "r.json")
        fwb.cmd_run(self.cfg, self.args)
        rep = json.loads((self.tmp / "r.json").read_text())
        self.assertTrue(rep["gate"]["gitleaks"])
        self.assertEqual([d["reason"] for d in rep["dropped"]], ["gitleaks:stub-rule"])
        self.assertEqual(rep["non_git_files"], 1)
        self.assertNotIn("files/apps/doc.md", [f["rel"] for f in rep["top_files"]])

    def counting_gitleaks(self) -> Path:
        stub = self.tmp / "gitleaks-count"
        stub.write_text(
            "#!/bin/bash\n"
            'while [ $# -gt 0 ]; do [ "$1" = "-r" ] && rep="$2"; [ "$1" = dir ] && root="$2"; shift; done\n'
            'find "$root" -type f | sed "s|^$root/||" >> "$STUB_DIR/gl-files.log"\n'
            'echo run >> "$STUB_DIR/gl-runs.log"\nprintf "[]" > "$rep"\nexit 0\n')
        stub.chmod(0o755)
        return stub

    def test_scan_cache_means_a_second_run_rescans_only_what_changed(self):
        self.cfg.gitleaks_bin = str(self.counting_gitleaks())
        stage, *_ , gate1, _ = fwb.build_stage(self.cfg, self.cfg.dry_stage)
        self.assertGreater(gate1["scanned"], 0)
        self.assertEqual(gate1["cached"], 0)
        first_files = (self.stubdir / "gl-files.log").read_text().split()
        self.assertIn("files/apps/doc.md", first_files)
        (self.stubdir / "gl-files.log").unlink()
        write(self.home / "apps" / "added.md", "new file\n")
        stage, *_ , gate2, _ = fwb.build_stage(self.cfg, self.cfg.dry_stage)
        self.assertGreaterEqual(gate2["cached"], 1)
        again = (self.stubdir / "gl-files.log").read_text().split()
        self.assertIn("files/apps/added.md", again)
        self.assertNotIn("files/apps/doc.md", again)

    def test_gitleaks_budget_leaves_the_rest_pending_not_lost(self):
        self.cfg.gitleaks_bin = str(self.counting_gitleaks())
        self.cfg.gitleaks_budget_s = 0
        stage, *_ , gate, _ = fwb.build_stage(self.cfg, self.cfg.dry_stage)
        self.assertGreater(gate["gitleaks_pending"], 0)
        self.assertFalse((self.stubdir / "gl-runs.log").exists())
        self.assertIn("files/apps/doc.md", stage.items)  # regex-scanned, still staged

    def test_allow_list_lets_a_reviewed_content_hit_through_but_never_a_secret_name(self):
        write(self.home / "apps" / "rule.md", f"bad example: {AWS}\n")
        write(self.home / "apps" / "prod.env", "A=1\n")
        stage, *_ = fwb.build_stage(self.cfg, self.cfg.dry_stage)
        self.assertNotIn("files/apps/rule.md", stage.items)
        src = str(self.home / "apps" / "rule.md")
        self.cfg.state_dir.mkdir(parents=True, exist_ok=True)
        (self.cfg.state_dir / "allow.txt").write_text(f"# reviewed\n{src}\n{self.home / 'apps' / 'prod.env'}\n")
        stage, *_ = fwb.build_stage(self.cfg, self.cfg.dry_stage)
        self.assertIn("files/apps/rule.md", stage.items)
        self.assertEqual(stage.allowed, [src])
        self.assertNotIn("files/apps/prod.env", stage.items)

    def test_an_unpushed_bundle_with_a_content_hit_is_dropped_unless_allowed(self):
        r = self.home / "Code" / "r"
        write(r / "doc.md", f"example {AWS}\n")
        git(r, "add", "-A")
        git(r, "commit", "-q", "-m", "doc with a key-shaped string")
        stage, *_ = fwb.build_stage(self.cfg, self.cfg.dry_stage)
        self.assertFalse(any(k.endswith("unpushed.bundle") for k in stage.items))
        label = os.path.realpath(r / ".git") + " (unpushed commits)"
        self.assertEqual([d["path"] for d in stage.dropped if "unpushed commits" in d["path"]], [label])
        self.cfg.state_dir.mkdir(parents=True, exist_ok=True)
        (self.cfg.state_dir / "allow.txt").write_text(label + "\n")
        stage, *_ = fwb.build_stage(self.cfg, self.cfg.dry_stage)
        self.assertTrue(any(k.endswith("unpushed.bundle") for k in stage.items))

    def test_a_changed_file_is_rescanned_and_a_new_secret_is_caught(self):
        stage, *_ , gate1, _ = fwb.build_stage(self.cfg, self.cfg.dry_stage)
        self.assertIn("files/apps/doc.md", stage.items)
        write(self.home / "apps" / "doc.md", f"token {AWS}\n")
        stage, *_ , gate2, _ = fwb.build_stage(self.cfg, self.cfg.dry_stage)
        self.assertNotIn("files/apps/doc.md", stage.items)
        self.assertEqual([d["reason"] for d in stage.dropped if d["path"].endswith("doc.md")], ["content:aws-access-key-id"])

    @unittest.skipUnless(shutil.which("gitleaks"), "gitleaks not installed")
    def test_real_gitleaks_runs_cleanly_over_a_benign_stage(self):
        self.cfg.gitleaks_bin = "gitleaks"
        self.args.dry_run, self.args.json = True, str(self.tmp / "r.json")
        fwb.cmd_run(self.cfg, self.args)
        rep = json.loads((self.tmp / "r.json").read_text())
        self.assertTrue(rep["gate"]["gitleaks"])
        self.assertEqual(rep["gate"]["gitleaks_error"], "")
        self.assertEqual(rep["dropped"], [])


class AccessCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fwb-access-"))
        (self.tmp / "apps" / "lanes").mkdir(parents=True)
        self.cfg = fwb.Config(home=self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_nothing_to_check_without_volume_links(self):
        self.assertEqual(fwb.cmd_check_access(self.cfg, None), 0)

    def test_unmounted_volume(self):
        (self.cfg.lanes / "Repo").symlink_to("/Volumes/fwb-test-not-mounted/Lanes/Repo")
        self.assertEqual(fwb.probe_external(self.cfg)["missing"], 1)
        self.assertEqual(fwb.cmd_check_access(self.cfg, None), 1)

    def test_denied_volume_names_the_interpreter(self):
        (self.cfg.lanes / "Repo").symlink_to("/Volumes/fwb-test-denied/Lanes/Repo")
        real = os.listdir

        def fake(path="."):
            if "fwb-test-denied" in str(path):
                raise PermissionError(errno.EPERM, "Operation not permitted", str(path))
            return real(path)

        with mock.patch.object(fwb.os, "listdir", fake):
            res = fwb.probe_external(self.cfg)
        self.assertEqual(res["denied"], 1)
        self.assertEqual(res["binary"], os.path.realpath(sys.executable))
        self.assertIn(os.path.realpath(sys.executable), fwb.removable_hint(self.cfg))


class MiscCase(unittest.TestCase):
    def test_time_format_is_twelve_hour_without_a_zone(self):
        import time as _t
        ts = _t.mktime((2026, 10, 10, 15, 5, 0, 0, 0, -1))
        self.assertEqual(fwb.stamp(ts), "2026-10-10 3:05pm")
        ts = _t.mktime((2026, 10, 10, 0, 7, 0, 0, 0, -1))
        self.assertEqual(fwb.stamp(ts), "2026-10-10 12:07am")

    def test_read_handoff_strips_quotes_and_comments(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "k"
            p.write_text('# c\nA="x y"\nexport B=\'z\'\nbad line\nC=1\n')
            self.assertEqual(fwb.read_handoff(p), {"A": "x y", "B": "z", "C": "1"})

    def test_plist_and_installer_agree_with_the_module(self):
        plist = (HERE.parent / "launchd" / "com.jay.fleet-work-backup.plist").read_text()
        self.assertIn("<string>com.jay.fleet-work-backup</string>", plist)
        self.assertIn("<integer>10800</integer>", plist)
        self.assertIn("<key>ProcessType</key>\n  <string>Standard</string>", plist)
        self.assertNotIn("Background", plist.replace("Standard, not Background", ""))
        self.assertIn("fleet_work_backup.py", plist)
        self.assertIn("fleet-work-backup.out.log", plist)


if __name__ == "__main__":
    unittest.main(verbosity=2)
