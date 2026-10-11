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
import hashlib
import importlib.util
import json
import os
import plistlib
import re
import shutil
import signal
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
AWS2 = "AKIA" + "Z8Y7X6W5V4U3T2S1"
PEM = "-----BEGIN " + "RSA PRIVATE KEY-----\n" + "MIIEowIBAAKCAQEA" + "q7Zp3Km9Tx2Wj4Hb" + "Lm8Vc5Rn1Yd6Fs0Gt"
HIGH = "".join(["aB3dE5", "gH7jK9", "mN1pQ3", "sT5v7x", "Zq2"])
PW = "".join(["pw-", "Zx9Qw3", "Er5Ty7", "Ui1Op"])
OPAQUE = "".join(["Zq8Lm", "2Vx7R", "t4Yp9", "Kd3Hs", "1Wb6N", "c0Fj5", "Gu1Wb", "6Nc0F", "p2Qa7"])
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
        # The tool must see a network remote (a local-path remote is the same disk and does not count as
        # pushed);  insteadOf lets git serve that URL from the local bare repo.
        url = f"https://example.invalid/{path.name}.git"
        git(path, "remote", "add", remote, url)
        git(path, "config", f"url.{bare}.insteadOf", url)
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
    def bundles_of(self, name):
        slug = self.group(name).slug
        return sorted(self.root / r for r in self.stage.items if r.startswith(f"git/{slug}/bundles/"))

    def test_bundles_belong_to_the_repository_not_to_a_worktree(self):
        g = self.group("unpushed")
        self.assertEqual(len(self.bundles_of("unpushed")), 1)   # main, claude/feature and claude/wt share a commit
        self.assertFalse(any(r.endswith(".bundle") and "/worktrees/" in r and f"/{g.slug}/" in r for r in self.stage.items))

    def test_bundle_holds_unpushed_branches_and_restores_through_a_real_clone(self):
        (b,) = self.bundles_of("unpushed")
        heads = subprocess.run(["git", "bundle", "list-heads", str(b)], env=GENV, capture_output=True, text=True).stdout
        for ref in ("refs/heads/main", "refs/heads/claude/feature", "refs/heads/claude/wt"):
            self.assertIn(ref, heads)
        fresh = Path(self.fx.tmp) / "restore-unpushed"
        # file:// goes through the transport like GitHub does:  only reachable objects arrive
        git(Path(self.fx.tmp), "clone", "-q", f"file://{self.fx.home / 'remotes' / 'unpushed.git'}", str(fresh))
        git(fresh, "bundle", "verify", str(b))
        git(fresh, "fetch", "-q", str(b), "refs/heads/*:refs/restored/*")
        self.assertEqual(git(fresh, "show", "refs/restored/claude/feature:feature.txt"), "feature")
        self.assertEqual(git(fresh, "show", "refs/restored/main:a.txt"), "a2")

    def test_group_metadata_names_the_bound_and_the_bundles(self):
        g = self.group("unpushed")
        meta = json.loads((self.root / f"git/{g.slug}/group.json").read_text())
        self.assertEqual(meta["bound"]["ref"], "refs/remotes/origin/main")
        self.assertEqual(meta["bound"]["sha"], git(self.fx.unpushed, "rev-parse", "refs/remotes/origin/main"))
        self.assertEqual(meta["network_remotes"], ["origin"])
        self.assertEqual(len(meta["bundles"]), 1)
        self.assertEqual(sorted(r["ref"] for r in meta["bundles"][0]["refs"]), ["refs/heads/claude/feature", "refs/heads/claude/wt", "refs/heads/main"])

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
        git(Path(self.fx.tmp), "clone", "-q", f"file://{self.fx.home / 'remotes' / 'flat-dirty.git'}", str(fresh))
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
        git(Path(self.fx.tmp), "clone", "-q", f"file://{self.fx.home / 'remotes' / 'stash-repo.git'}", str(fresh))
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
               "mytoken.txt", "app-secret.yml", "Foo.KEYCHAIN-DB", "infisical-machine-identity.json",
               ".dev.vars", ".dev.vars.production", "a/auth.json", "oauth_creds.json", "x/.envrc", "infra/prod.tfstate",
               "infra/terraform.tfstate.backup", "vars.tfvars", ".config/gh/hosts.yml", "gh/hosts.yml"]
        no = [".env.example", "src/app.py", "README.md", "a/b/c.txt", "environment.md", "ansible/hosts.yml", "hosts.yml",
              "src/authors.json"]
        for p in yes:
            self.assertTrue(fwb.is_secret_name(p), p)
        for p in no:
            self.assertFalse(fwb.is_secret_name(p), p)

    def test_tracked_patches_skip_the_broad_globs_for_source_and_prose_only(self):
        self.assertTrue(fwb.is_secret_name("src/.env", tracked=True))
        self.assertTrue(fwb.is_secret_name("deploy/server.pem", tracked=True))
        self.assertTrue(fwb.is_secret_name("src/auth.json", tracked=True))
        self.assertFalse(fwb.is_secret_name("src/tokenizer.py", tracked=True))
        self.assertFalse(fwb.is_secret_name("src/secrets_policy.md", tracked=True))
        self.assertFalse(fwb.is_secret_name("src/secrets/policy.py", tracked=True))
        self.assertTrue(fwb.is_secret_name("src/tokenizer.py", tracked=False))
        # data and config files named like a secret are dropped even when git tracks them
        for name in ("config/client_secret.json", "src/service_token.txt", "deploy/secrets.yaml", "config/secrets/prod.yaml",
                     "db/credentials.sqlite", "x/passwd.conf"):
            self.assertTrue(fwb.is_secret_name(name, tracked=True), name)

    def test_content_patterns(self):
        for text in (AWS, PEM, "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8", "xoxb-" + "8271936450-Qm7ZkP3tXw",
                     "sk-ant-" + "api03-Zq8Lm2Vx7Rt4Yp9Kd3Hs",
                     "eyJhbGciOiJIUzI1NiJ9." + "eyJzdWIiOiIxMjM0NTY3ODkwIn0." + "abcdefghijk1234567",
                     f"SERVICE_API_KEY={HIGH}", f'export DB_PASSWORD="{HIGH}"'):
            self.assertIsNotNone(fwb.scan_bytes(text.encode()), text[:12])

    def test_a_json_credential_field_is_a_secret_but_a_placeholder_is_not(self):
        for text in (json.dumps({"refresh_token": OPAQUE}), json.dumps({"client_secret": OPAQUE}),
                     '{\n  "api_key" : "' + OPAQUE + '"\n}'):
            self.assertEqual(fwb.scan_bytes(text.encode()), "json-secret-field", text[:30])
        for text in (json.dumps({"access_token": "changeme"}), json.dumps({"password": "x" * 30}),
                     json.dumps({"api_key": "${API_KEY_FROM_THE_ENVIRONMENT}"}), json.dumps({"token_type": "Bearer"})):
            self.assertIsNone(fwb.scan_bytes(text.encode()), text[:30])

    def test_a_pem_header_without_a_body_is_not_a_key(self):
        for text in ("-----BEGIN " + "PRIVATE KEY-----", "'-----BEGIN " + "RSA PRIVATE KEY-----\\n...\\n-----END RSA PRIVATE KEY-----'"):
            self.assertIsNone(fwb.scan_bytes(text.encode()), text)
        self.assertEqual(fwb.scan_bytes(PEM.encode()), "private-key-block")
        as_json = PEM.replace("\n", "\\n")
        self.assertEqual(fwb.scan_bytes(as_json.encode()), "private-key-block")

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


class RunBase(unittest.TestCase):
    """A temp home with one repository (an unpushed commit), a doc, credentials and a stub restic."""

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
        self.cfg.pressure_fn = lambda: None     # as on a Mac without the sysctl:  the swap rule stands alone
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
            '  [ -n "$STUB_WRONG_PW" ] && { echo "Fatal: wrong password or no key found" >&2; exit 1; }\n'
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

    def stub_gitleaks(self, body: str, name: str = "gitleaks-stub") -> Path:
        """A gitleaks stub:  answers 'version', and otherwise runs ``body`` with $root (the batch folder) and
        $rep (the report path) set.  Every call's arguments go to $STUB_DIR/gl-argv.log."""
        stub = self.tmp / name
        stub.write_text(
            "#!/bin/bash\n"
            'echo "$@" >> "$STUB_DIR/gl-argv.log"\n'
            'while [ $# -gt 0 ]; do [ "$1" = "-r" ] && rep="$2"; [ "$1" = dir ] && root="$2"; shift; done\n'
            '[ -z "$root" ] && { echo stub-1.0; exit 0; }\n'
            'find "$root" -type f | sed "s|^$root/||" >> "$STUB_DIR/gl-files.log"\n'
            + body)
        stub.chmod(0o755)
        return stub


class RunCase(RunBase):
    """cmd_run end to end against a stub restic."""

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

    def test_allow_list_also_covers_a_gitleaks_finding(self):
        stub = self.tmp / "gitleaks"
        stub.write_text(
            "#!/bin/bash\n"
            'while [ $# -gt 0 ]; do [ "$1" = "-r" ] && rep="$2"; [ "$1" = dir ] && root="$2"; shift; done\n'
            'printf \'[{"RuleID":"stub-rule","File":"%s/files/apps/doc.md"}]\' "$root" > "$rep"\nexit 0\n')
        stub.chmod(0o755)
        self.cfg.gitleaks_bin = str(stub)
        self.cfg.state_dir.mkdir(parents=True, exist_ok=True)
        src = str(self.home / "apps" / "doc.md")
        (self.cfg.state_dir / "allow.txt").write_text(f"{src} @ {hashlib.sha1(b'doc' + bytes([10])).hexdigest()[:12]}\n")
        stage, *_ = fwb.build_stage(self.cfg, self.cfg.dry_stage)
        self.assertIn("files/apps/doc.md", stage.items)
        self.assertEqual(stage.dropped, [])
        self.assertEqual(stage.allowed, [src])

    def counting_gitleaks(self) -> Path:
        stub = self.tmp / "gitleaks-count"
        stub.write_text(
            "#!/bin/bash\n"
            'while [ $# -gt 0 ]; do [ "$1" = "-r" ] && rep="$2"; [ "$1" = dir ] && root="$2"; shift; done\n'
            '[ -z "$root" ] && { echo stub-1.0; exit 0; }\n'
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

    def test_regenerated_patches_hit_the_scan_cache_by_content(self):
        r = self.home / "Code" / "r"
        write(r / "b.txt", "b edited\n")
        stage, *_, gate1, _ = fwb.build_stage(self.cfg, self.cfg.dry_stage)
        self.assertTrue(any(i.kind == "patch" for i in stage.items.values()))
        stage, *_, gate2, _ = fwb.build_stage(self.cfg, self.cfg.dry_stage)
        self.assertEqual(gate2["scanned"], 0)
        write(r / "b.txt", "b edited again\n")
        stage, *_, gate3, _ = fwb.build_stage(self.cfg, self.cfg.dry_stage)
        self.assertEqual(gate3["scanned"], 1)

    def test_gitleaks_budget_spent_defers_the_files_it_did_not_clear(self):
        self.cfg.gitleaks_bin = str(self.counting_gitleaks())
        self.cfg.gitleaks_budget_s = 0
        stage, *_, gate, _ = fwb.build_stage(self.cfg, self.cfg.dry_stage)
        self.assertGreater(gate["deferred"], 0)
        self.assertEqual(gate["gitleaks_pending"], gate["deferred"])
        self.assertFalse((self.stubdir / "gl-runs.log").exists())
        self.assertNotIn("files/apps/doc.md", stage.items)              # fail closed:  not uploaded unscanned
        self.assertTrue(any(d["path"].endswith("doc.md") and d["reason"] == "gitleaks-budget" for d in stage.deferred))
        self.assertEqual(stage.dropped, [])                             # a deferral is not a secret finding
        self.assertTrue(any(k.endswith(".bundle") for k in stage.items))   # bundles were scanned before they were made
        self.cfg.gitleaks_budget_s = 1500                               # the next run picks the file up
        stage, *_, gate, _ = fwb.build_stage(self.cfg, self.cfg.dry_stage)
        self.assertIn("files/apps/doc.md", stage.items)
        self.assertEqual(gate["deferred"], 0)

    def test_a_gitleaks_failure_defers_instead_of_uploading(self):
        stub = self.stub_gitleaks('echo "boom" >&2\nexit 2\n')
        self.cfg.gitleaks_bin = str(stub)
        stage, *_, gate, _ = fwb.build_stage(self.cfg, self.cfg.dry_stage)
        self.assertNotIn("files/apps/doc.md", stage.items)
        self.assertEqual(gate["deferred"], len(stage.deferred))
        self.assertTrue(all(d["reason"] == "gitleaks-unreadable" for d in stage.deferred))
        self.assertIn("unreadable", gate["gitleaks_error"])
        self.assertEqual(fwb.cmd_run(self.cfg, self.args), 0)
        line = self.log().splitlines()[-1]
        self.assertIn("status=partial", line)
        self.assertIn("gate=gitleaks-error", line)
        self.assertIn("warn=gitleaks-error", line)
        self.assertIn("--tag fleet-work --tag partial", " ".join(a for a in self.argv() if a.startswith("backup")))

    def test_one_unreadable_file_does_not_block_the_files_around_it(self):
        write(self.home / "apps" / "poison.md", "p\n")
        write(self.home / "apps" / "zz-later.md", "later\n")
        stub = self.stub_gitleaks('find "$root" -name poison.md | grep -q . && exit 2\nprintf "[]" > "$rep"\nexit 0\n')
        self.cfg.gitleaks_bin = str(stub)
        stage, *_, gate, _ = fwb.build_stage(self.cfg, self.cfg.dry_stage)
        self.assertIn("files/apps/doc.md", stage.items)
        self.assertIn("files/apps/zz-later.md", stage.items)
        self.assertNotIn("files/apps/poison.md", stage.items)
        self.assertEqual([d["path"].rsplit("/", 1)[-1] for d in stage.deferred], ["poison.md"])
        self.assertEqual(gate["gitleaks_unreadable"], 1)

    def test_a_finding_that_maps_to_no_staged_file_is_an_error_not_a_pass(self):
        stub = self.stub_gitleaks('printf \'[{"RuleID":"r","File":"/nowhere/else.txt"}]\' > "$rep"\nexit 0\n')
        self.cfg.gitleaks_bin = str(stub)
        stage, *_, gate, _ = fwb.build_stage(self.cfg, self.cfg.dry_stage)
        self.assertEqual(gate["gitleaks_error"], "unmapped-finding")
        self.assertNotIn("files/apps/doc.md", stage.items)

    def test_gitleaks_is_told_to_read_every_staged_size_and_is_not_given_binary_files(self):
        write(self.home / "apps" / "blob.bin", "\0\1\2 binary\n")
        con = sqlite3.connect(self.home / "apps" / "x.db")
        con.execute("create table t(x)")
        con.commit()
        con.close()
        stub = self.stub_gitleaks('printf "[]" > "$rep"\nexit 0\n')
        self.cfg.gitleaks_bin = str(stub)
        stage, *_, gate, _ = fwb.build_stage(self.cfg, self.cfg.dry_stage)
        seen = (self.stubdir / "gl-files.log").read_text().split()
        self.assertIn("files/apps/doc.md", seen)
        self.assertNotIn("files/apps/blob.bin", seen)           # gitleaks does not read binary content
        self.assertNotIn("files/apps/x.db", seen)
        self.assertIn("files/apps/blob.bin", stage.items)       # regex-scanned, kept, and counted
        self.assertIn("files/apps/x.db", stage.items)
        self.assertEqual(gate["regex_only"], 2)
        self.assertEqual(gate["deferred"], 0)
        limit = (self.stubdir / "gl-argv.log").read_text().split("--max-target-megabytes ")[1].split()[0]
        self.assertEqual(int(limit), self.cfg.max_file_bytes // (1024 * 1024) + 1)    # nothing staged is skipped for size

    def test_a_binary_file_with_a_key_is_still_dropped_by_the_regex(self):
        write(self.home / "apps" / "blob.bin", "\0\1\2 " + AWS + "\n")
        stage, *_ = fwb.build_stage(self.cfg, self.cfg.dry_stage)
        self.assertNotIn("files/apps/blob.bin", stage.items)
        self.assertEqual([d["reason"] for d in stage.dropped if d["path"].endswith("blob.bin")], ["content:aws-access-key-id"])

    def test_a_reviewed_regex_hit_still_goes_through_gitleaks(self):
        write(self.home / "apps" / "rule.md", f"bad example: {AWS}\n")
        stage, *_ = fwb.build_stage(self.cfg, self.cfg.dry_stage)
        digest = next(d["digest"] for d in stage.dropped if d["path"].endswith("rule.md"))
        self.cfg.state_dir.mkdir(parents=True, exist_ok=True)
        (self.cfg.state_dir / "allow.txt").write_text(f"{self.home / 'apps' / 'rule.md'} @ {digest}\n")
        self.cfg.gitleaks_bin = str(self.stub_gitleaks('printf "[]" > "$rep"\nexit 0\n'))
        stage, *_ = fwb.build_stage(self.cfg, self.cfg.dry_stage)
        self.assertIn("files/apps/rule.md", stage.items)
        self.assertIn("files/apps/rule.md", (self.stubdir / "gl-files.log").read_text().split())

    def test_allow_list_lets_a_reviewed_content_hit_through_but_never_a_secret_name(self):
        write(self.home / "apps" / "rule.md", f"bad example: {AWS}\n")
        write(self.home / "apps" / "prod.env", "A=1\n")
        stage, *_ = fwb.build_stage(self.cfg, self.cfg.dry_stage)
        self.assertNotIn("files/apps/rule.md", stage.items)
        src = str(self.home / "apps" / "rule.md")
        digest = next(d["digest"] for d in stage.dropped if d["path"] == src)
        self.assertRegex(digest, r"^[0-9a-f]{12}$")
        self.cfg.state_dir.mkdir(parents=True, exist_ok=True)
        (self.cfg.state_dir / "allow.txt").write_text(
            f"# reviewed\n{src} @ {digest}\n{self.home / 'apps' / 'prod.env'} @ {'0' * 12}\n")
        stage, *_ = fwb.build_stage(self.cfg, self.cfg.dry_stage)
        self.assertIn("files/apps/rule.md", stage.items)
        self.assertEqual(stage.allowed, [src])
        self.assertNotIn("files/apps/prod.env", stage.items)
        # a real key pasted into the file after the review:  the pin no longer matches
        write(self.home / "apps" / "rule.md", f"bad example: {AWS}\nand now {AWS2}\n")
        stage, *_ = fwb.build_stage(self.cfg, self.cfg.dry_stage)
        self.assertNotIn("files/apps/rule.md", stage.items)
        self.assertEqual(stage.allow_stale, [src])
        # the owner took the line out
        (self.cfg.state_dir / "allow.txt").write_text("# nothing allowed\n")
        write(self.home / "apps" / "rule.md", f"bad example: {AWS}\n")
        stage, *_ = fwb.build_stage(self.cfg, self.cfg.dry_stage)
        self.assertNotIn("files/apps/rule.md", stage.items)    # an allowed hit is never cached as clean

    def test_an_allow_line_without_a_digest_is_ignored_and_reported(self):
        write(self.home / "apps" / "rule.md", f"bad example: {AWS}\n")
        src = str(self.home / "apps" / "rule.md")
        self.cfg.state_dir.mkdir(parents=True, exist_ok=True)
        (self.cfg.state_dir / "allow.txt").write_text(f"{src}\n")
        stage, *_ = fwb.build_stage(self.cfg, self.cfg.dry_stage)
        self.assertNotIn("files/apps/rule.md", stage.items)
        self.assertEqual(stage.allow_unpinned, [src])

    def test_the_detail_log_prints_the_exact_line_to_paste_into_allow_txt(self):
        write(self.home / "apps" / "rule.md", f"bad example: {AWS}\n")
        self.assertEqual(fwb.cmd_run(self.cfg, self.args), 0)
        detail = self.cfg.detail_log_path.read_text()
        src = str(self.home / "apps" / "rule.md")
        m = [l.strip() for l in detail.splitlines() if l.strip().startswith("allow-line ") and src in l]
        self.assertEqual(len(m), 1)
        self.assertNotIn(AWS, detail)
        self.cfg.state_dir.mkdir(parents=True, exist_ok=True)
        (self.cfg.state_dir / "allow.txt").write_text(m[0][len("allow-line "):] + "\n")
        stage, *_ = fwb.build_stage(self.cfg, self.cfg.dry_stage)
        self.assertIn("files/apps/rule.md", stage.items)

    def bundle_heads(self, stage):
        out = ""
        for k in stage.items:
            if k.endswith(".bundle"):
                out += subprocess.run(["git", "bundle", "list-heads", str(self.cfg.dry_stage / k)], env=GENV,
                                      capture_output=True, text=True).stdout
        return out

    def test_an_unpushed_bundle_with_a_content_hit_is_dropped_unless_pinned_in_allow_txt(self):
        r = self.home / "Code" / "r"
        write(r / "doc.md", f"example {AWS}\n")
        git(r, "add", "-A")
        git(r, "commit", "-q", "-m", "doc with a key-shaped string")
        stage, *_ = fwb.build_stage(self.cfg, self.cfg.dry_stage)
        self.assertFalse(any(k.endswith(".bundle") for k in stage.items))
        label = os.path.realpath(r / ".git") + " ref refs/heads/main"
        tip = git(r, "rev-parse", "HEAD")
        self.assertEqual([(d["path"], d["digest"]) for d in stage.dropped if " ref " in d["path"]], [(label, tip[:12])])
        self.cfg.state_dir.mkdir(parents=True, exist_ok=True)
        (self.cfg.state_dir / "allow.txt").write_text(f"{label} @ {tip[:12]}\n")
        stage, *_ = fwb.build_stage(self.cfg, self.cfg.dry_stage)
        self.assertTrue(any(k.endswith(".bundle") for k in stage.items))
        write(r / "more.txt", "the branch moved on\n")           # a new commit:  the reviewed tip is gone
        git(r, "add", "-A")
        git(r, "commit", "-q", "-m", "more")
        stage, *_ = fwb.build_stage(self.cfg, self.cfg.dry_stage)
        self.assertFalse(any(k.endswith(".bundle") for k in stage.items))
        self.assertEqual(stage.allow_stale, [label])

    def test_one_dirty_branch_costs_only_that_branch_not_the_repository(self):
        r = self.home / "Code" / "r"
        git(r, "checkout", "-q", "-b", "clean-branch")
        write(r / "clean.txt", "fine\n")
        git(r, "add", "-A")
        git(r, "commit", "-q", "-m", "clean work")
        git(r, "checkout", "-q", "main")
        git(r, "checkout", "-q", "-b", "leaky-branch")
        write(r / "leak.md", f"example {AWS}\n")
        git(r, "add", "-A")
        git(r, "commit", "-q", "-m", "leaky work")
        git(r, "checkout", "-q", "main")
        stage, *_ = fwb.build_stage(self.cfg, self.cfg.dry_stage)
        heads = self.bundle_heads(stage)
        self.assertIn("refs/heads/clean-branch", heads)
        self.assertIn("refs/heads/main", heads)
        self.assertNotIn("leaky-branch", heads)
        self.assertEqual([d["path"].split(" ref ")[1] for d in stage.dropped if " ref " in d["path"]], ["refs/heads/leaky-branch"])

    def test_a_new_pattern_or_scanner_version_discards_the_scan_cache(self):
        stage, *_, gate1, _ = fwb.build_stage(self.cfg, self.cfg.dry_stage)
        stage, *_, gate2, _ = fwb.build_stage(self.cfg, self.cfg.dry_stage)
        self.assertEqual(gate2["scanned"], 0)
        with mock.patch.dict(fwb.CONTENT_PATTERNS, {"new-pattern": fwb.re.compile(rb"zzz-never-matches")}):
            fwb.PREFILTER["new-pattern"] = (b"zzz",)
            try:
                stage, *_, gate3, _ = fwb.build_stage(self.cfg, self.cfg.dry_stage)
            finally:
                fwb.PREFILTER.pop("new-pattern", None)
        self.assertGreater(gate3["scanned"], 0)
        self.assertEqual(gate3["cached"], 0)

    def test_an_unexpected_error_still_leaves_a_log_line_and_no_stage(self):
        with mock.patch.object(fwb, "discover", side_effect=RuntimeError("boom with /some/path")):
            self.assertEqual(fwb.cmd_run(self.cfg, self.args), 5)
        line = self.log().splitlines()[-1]
        self.assertIn("status=error reason=exception:RuntimeError", line)
        self.assertNotIn("boom", line)
        self.assertFalse(self.cfg.stage.exists())
        self.assertIn("RuntimeError", self.cfg.detail_log_path.read_text())
        self.assertEqual(self.argv(), [])

    def test_restic_rc3_with_a_snapshot_is_partial_not_an_error(self):
        stub = self.stubdir / "restic"
        text = stub.read_text()
        stub.write_text(text.replace("fi\nexit 0\n", "fi\nif [ \"$1\" = backup ] && [ -n \"$STUB_RC3\" ]; then exit 3; fi\nexit 0\n"))
        os.environ["STUB_RC3"] = "1"
        try:
            self.assertEqual(fwb.cmd_run(self.cfg, self.args), 0)
        finally:
            os.environ.pop("STUB_RC3", None)
        line = self.log().splitlines()[-1]
        self.assertIn("status=partial", line)
        self.assertIn("restic-unreadable-files", line)
        self.assertTrue(any(a.startswith("forget") for a in self.argv()))
        self.assertIn("tag --add partial abcdef0123456789", self.argv())   # kept out of the good snapshots' retention

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


class BinaryCase(RunBase):
    """A binary change is never staged as git's binary patch (zlib and base85, unreadable to both scanners):
    the raw new content is staged instead, so the gate reads it."""

    def setUp(self):
        super().setUp()
        self.r = self.home / "Code" / "r"
        (self.r / "data.bin").write_bytes(b"\0\1\2 original\n")
        git(self.r, "add", "data.bin")
        git(self.r, "commit", "-q", "-m", "add a binary file")
        git(self.r, "push", "-q")
        self.cfg.max_file_bytes = 1024 * 1024

    def build(self):
        stage, disc, wts, groups, *_ = fwb.build_stage(self.cfg, self.cfg.dry_stage)
        self.group = groups[0]
        self.base = f"git/{self.group.slug}"
        return stage

    def staged_bytes(self, stage):
        return b"".join(p.read_bytes() for p in self.cfg.dry_stage.rglob("*") if p.is_file())

    def test_a_modified_tracked_binary_is_staged_raw_and_restorable(self):
        new = b"\0\1\2 new content \xff\xfe\n"
        (self.r / "data.bin").write_bytes(new)
        stage = self.build()
        raw = [k for k in stage.items if k.endswith("/binary/data.bin")]
        self.assertEqual(len(raw), 1)
        self.assertEqual(stage.items[raw[0]].kind, "binary")
        self.assertEqual((self.cfg.dry_stage / raw[0]).read_bytes(), new)
        self.assertFalse([k for k in stage.items if k.endswith(".patch")])       # no unreadable binary patch
        self.assertEqual(stage.items[raw[0]].src, str(self.r / "data.bin"))
        meta = json.loads((self.cfg.dry_stage / f"{self.base}/worktrees").glob("*/worktree.json").__next__().read_text())
        self.assertEqual(meta["head"], git(self.r, "rev-parse", "HEAD"))

    def test_a_key_inside_a_modified_binary_is_caught_by_the_regex(self):
        (self.r / "data.bin").write_bytes(b"\0" + AWS.encode() + b"\n")
        stage = self.build()
        self.assertFalse([k for k in stage.items if "data.bin" in k])
        self.assertEqual([d["reason"] for d in stage.dropped if d["path"].endswith("data.bin")], ["content:aws-access-key-id"])
        self.assertNotIn(AWS.encode(), self.staged_bytes(stage))

    def test_binary_files_in_a_stash_and_a_stash_u_are_scanned_too(self):
        (self.r / "data.bin").write_bytes(b"\0 clean stash content\n")
        (self.r / "new.bin").write_bytes(b"\0 clean untracked\n")
        git(self.r, "stash", "push", "-q", "-u", "-m", "clean")
        (self.r / "data.bin").write_bytes(b"\0" + AWS.encode() + b"\n")
        (self.r / "leak.bin").write_bytes(b"\0" + AWS2.encode() + b"\n")
        git(self.r, "stash", "push", "-q", "-u", "-m", "leaky")
        stage = self.build()
        blob = self.staged_bytes(stage)
        self.assertNotIn(AWS.encode(), blob)
        self.assertNotIn(AWS2.encode(), blob)
        keys = sorted(k.split(f"{self.base}/stashes/", 1)[1] for k in stage.items if "/stashes/" in k and "/binary/" in k)
        self.assertEqual(keys, ["01/tracked/binary/data.bin", "01/untracked/binary/new.bin"])    # the clean stash (index 1)
        got = (self.cfg.dry_stage / f"{self.base}/stashes/01/tracked/binary/data.bin").read_bytes()
        self.assertEqual(got, b"\0 clean stash content\n")
        dropped = sorted(d["path"].rsplit(":", 1)[1] for d in stage.dropped if "#stash0" in d["path"])
        self.assertEqual(dropped, ["data.bin", "leak.bin"])

    def test_a_deleted_tracked_binary_is_listed_not_patched(self):
        os.unlink(self.r / "data.bin")
        stage = self.build()
        lst = [k for k in stage.items if k.endswith("binary-deleted.txt")]
        self.assertEqual(len(lst), 1)
        self.assertEqual((self.cfg.dry_stage / lst[0]).read_text().split(), ["data.bin"])
        self.assertFalse([k for k in stage.items if k.endswith(".patch")])

    def test_a_tracked_binary_with_a_secret_name_is_dropped_by_name(self):
        (self.r / "client_secret.bin").write_bytes(b"\0 v1\n")
        git(self.r, "add", "client_secret.bin")
        git(self.r, "commit", "-q", "-m", "add")
        git(self.r, "push", "-q")
        (self.r / "client_secret.bin").write_bytes(b"\0 v2 not matched by any pattern\n")
        stage = self.build()
        self.assertFalse([k for k in stage.items if "client_secret" in k])
        self.assertIn("secret-name", [d["reason"] for d in stage.dropped if d["path"].endswith("client_secret.bin")])

    def test_an_oversize_stash_entry_is_skipped_before_git_produces_it(self):
        (self.r / "data.bin").write_bytes(b"\0" + b"x" * 5000)
        git(self.r, "stash", "push", "-q", "-m", "big")
        self.cfg.max_file_bytes = 1000
        stage = self.build()
        self.assertTrue(any("#stash0:data.bin" in f["path"] and f["size"] > 1000 for f in stage.large))
        self.assertFalse([k for k in stage.items if "/stashes/" in k and "data.bin" in k])

    def test_the_restore_md_steps_bring_back_text_binary_and_deleted_files(self):
        """RESTORE.md's apply_all, run as written, against a fresh clone of the pushed state."""
        md = (HERE / "RESTORE.md").read_text()
        m = re.search(r"(?ms)^    apply_all\(\) \{[^\n]*\n.*?^    \}\n", md)
        self.assertIsNotNone(m)
        func = "\n".join(l[4:] for l in m.group(0).splitlines())
        (self.r / "gone.bin").write_bytes(b"\0 to be deleted\n")
        git(self.r, "add", "gone.bin")
        git(self.r, "commit", "-q", "-m", "add gone.bin")
        git(self.r, "push", "-q")
        write(self.r / "a.txt", "edited text\n")
        (self.r / "data.bin").write_bytes(b"\0\1 changed binary \xff\n")
        os.unlink(self.r / "gone.bin")
        stage = self.build()
        patches = next(self.cfg.dry_stage / k for k in stage.items if k.endswith(".patch")).parent
        fresh = self.tmp / "fresh"
        git(self.tmp, "clone", "-q", f"file://{self.home / 'remotes' / 'r.git'}", str(fresh))
        out = subprocess.run(["bash", "-c", f'set -e\n{func}\ncd "{fresh}"\napply_all "{patches}"\n'],
                             capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertNotIn("NEEDS HAND", out.stdout)
        self.assertEqual((fresh / "a.txt").read_text(), "edited text\n")
        self.assertEqual((fresh / "data.bin").read_bytes(), b"\0\1 changed binary \xff\n")
        self.assertFalse((fresh / "gone.bin").exists())

    def test_a_text_file_still_gets_a_patch_that_applies(self):
        write(self.r / "a.txt", "edited\n")
        stage = self.build()
        patches = [k for k in stage.items if k.endswith(".patch")]
        self.assertEqual(len(patches), 1)
        fresh = self.tmp / "fresh"
        git(self.tmp, "clone", "-q", f"file://{self.home / 'remotes' / 'r.git'}", str(fresh))
        git(fresh, "apply", str(self.cfg.dry_stage / patches[0]))
        self.assertEqual((fresh / "a.txt").read_text(), "edited\n")


class BundleCase(RunBase):
    def setUp(self):
        super().setUp()
        self.r = self.home / "Code" / "r"

    def build(self):
        stage, disc, wts, groups, *_ = fwb.build_stage(self.cfg, self.cfg.dry_stage)
        self.groups = groups
        self.disc = disc
        return stage

    def bundles(self, stage):
        return sorted(self.cfg.dry_stage / k for k in stage.items if k.endswith(".bundle"))

    def heads(self, bundle):
        out = subprocess.run(["git", "bundle", "list-heads", str(bundle)], env=GENV, capture_output=True, text=True).stdout
        return sorted(l.split()[1] for l in out.splitlines())

    def test_a_restore_still_works_after_github_deletes_a_pushed_branch(self):
        # A local branch stacked on a pushed feature branch.  GitHub deletes the feature branch after a
        # squash merge and a later clone cannot fetch its commits:  the bundle must not need them.
        git(self.r, "checkout", "-q", "-b", "feat", "origin/main")
        write(self.r / "feat.txt", "feat\n")
        git(self.r, "add", "-A")
        git(self.r, "commit", "-q", "-m", "feat")
        git(self.r, "push", "-q", "origin", "feat")
        git(self.r, "checkout", "-q", "-b", "local-on-feat")
        write(self.r / "local.txt", "local\n")
        git(self.r, "add", "-A")
        git(self.r, "commit", "-q", "-m", "local only")
        git(self.r, "checkout", "-q", "main")
        stage = self.build()
        bare = self.home / "remotes" / "r.git"
        git(bare, "branch", "-D", "feat")
        git(bare, "gc", "-q", "--prune=now")
        fresh = self.tmp / "fresh"
        git(self.tmp, "clone", "-q", f"file://{bare}", str(fresh))
        self.assertEqual(git(fresh, "branch", "-r", "--list", "origin/feat"), "")
        bundles = self.bundles(stage)
        self.assertEqual(len(bundles), 2)        # main's own commit, and the feat-based branch
        for b in bundles:
            git(fresh, "bundle", "verify", str(b))
            git(fresh, "fetch", "-q", str(b), "refs/heads/*:refs/restored/*")
        self.assertEqual(git(fresh, "show", "refs/restored/local-on-feat:local.txt"), "local")
        self.assertEqual(git(fresh, "show", "refs/restored/local-on-feat:feat.txt"), "feat")   # the deleted branch's commit travels along
        self.assertEqual(git(fresh, "show", "refs/restored/main:a.txt"), "changed and committed, not pushed")

    def test_independent_branches_get_their_own_bundle_and_overlapping_ones_share(self):
        for name in ("a", "b"):
            git(self.r, "checkout", "-q", "-b", name, "origin/main")
            write(self.r / f"branch-{name}.txt", name + "\n")
            git(self.r, "add", "-A")
            git(self.r, "commit", "-q", "-m", name)
        git(self.r, "checkout", "-q", "main")
        git(self.r, "branch", "c")                 # same commit as main:  overlaps it
        stage = self.build()
        by = {tuple(self.heads(b)): b for b in self.bundles(stage)}
        self.assertEqual(sorted(by), [("refs/heads/a",), ("refs/heads/b",), ("refs/heads/c", "refs/heads/main")])

    def test_bundles_are_byte_identical_between_runs(self):
        git(self.r, "checkout", "-q", "-b", "other", "origin/main")
        write(self.r / "o.txt", "o\n")
        git(self.r, "add", "-A")
        git(self.r, "commit", "-q", "-m", "other")
        git(self.r, "checkout", "-q", "main")
        stage = self.build()
        first = {b.name: hashlib.sha1(b.read_bytes()).hexdigest() for b in self.bundles(stage)}
        stage = self.build()
        second = {b.name: hashlib.sha1(b.read_bytes()).hexdigest() for b in self.bundles(stage)}
        self.assertEqual(first, second)
        self.assertEqual(len(first), 2)     # so restic stores an unchanged branch once

    def test_a_remote_that_is_a_local_path_does_not_count_as_pushed(self):
        lo = make_repo(self.home, self.home / "Code" / "local-origin", remote=None)
        bare = self.home / "remotes" / "local-origin.git"
        subprocess.run(["git", "-c", "init.defaultBranch=main", "init", "-q", "--bare", str(bare)], env=GENV, check=True)
        git(lo, "remote", "add", "origin", str(bare))
        git(lo, "push", "-q", "-u", "origin", "main")
        stage = self.build()
        g = next(g for g in self.groups if g.name == "local-origin")
        self.assertEqual(g.unpushed_refs, 1)              # on the same disk is not a backup
        self.assertFalse(g.no_remote)
        (b,) = [self.cfg.dry_stage / k for k in stage.items if k.endswith(".bundle") and f"/{g.slug}/" in k]
        fresh = self.tmp / "from-bundle"
        git(self.tmp, "clone", "-q", str(b), str(fresh))     # whole history, no prerequisites
        self.assertEqual((fresh / "a.txt").read_text(), "a\n")
        self.assertTrue(any(n.startswith("local-only-remotes") for n in stage.notes))

    def test_a_clone_of_a_local_checkout_is_bounded_by_that_checkouts_github_branch(self):
        parent = make_repo(self.home, self.home / "Code" / "parent")           # origin looks like GitHub
        child = self.home / "apps" / "child"
        git(self.tmp, "clone", "-q", str(parent), str(child))                   # child's only remote is a local path
        stale_main = git(child, "rev-parse", "refs/remotes/origin/main")
        write(parent / "moved-on.txt", "the checkout's main moved on after the clone was made\n")      # the clone is now stale
        git(parent, "add", "-A")
        git(parent, "commit", "-q", "-m", "main moves on")
        git(parent, "push", "-q")
        write(child / "child.txt", "work only in the clone\n")
        git(child, "add", "-A")
        git(child, "commit", "-q", "-m", "clone-only work")
        stage = self.build()
        g = next(g for g in self.groups if g.name == "child")
        self.assertEqual(g.unpushed_refs, 1)
        meta = json.loads((self.cfg.dry_stage / f"git/{g.slug}/group.json").read_text())
        self.assertEqual(meta["network_remotes"], [])
        self.assertEqual(meta["bound"]["sha"], stale_main)         # the newest commit the clone has that GitHub holds
        self.assertNotEqual(stale_main, git(parent, "rev-parse", "refs/remotes/origin/main"))
        self.assertEqual(meta["bound"]["via_url"], "https://example.invalid/parent.git")
        self.assertEqual(os.path.realpath(meta["bound"]["via"]), os.path.realpath(parent))
        self.assertTrue(any(n.startswith("local-remote-proxied") for n in stage.notes))
        self.assertFalse(any(n.startswith("local-only-remotes") and "child" in n for n in stage.notes))
        (b,) = [self.cfg.dry_stage / k for k in stage.items if k.endswith(".bundle") and f"/{g.slug}/" in k]
        fresh = self.tmp / "from-github"
        git(self.tmp, "clone", "-q", f"file://{self.home / 'remotes' / 'parent.git'}", str(fresh))   # what GitHub would serve
        git(fresh, "bundle", "verify", str(b))
        git(fresh, "fetch", "-q", str(b), "refs/heads/*:refs/restored/*")
        self.assertEqual(git(fresh, "show", "refs/restored/main:child.txt"), "work only in the clone")
        self.assertLess(b.stat().st_size, 2000)                                 # one commit, not the whole history

    def test_network_urls(self):
        for url in ("https://github.com/a/b.git", "git@github.com:a/b.git", "ssh://git@github.com/a/b",
                    "github.com:a/b", "git://example.org/x.git", "HTTPS://GITHUB.COM/a/b"):
            self.assertTrue(fwb.is_network_url(url), url)
        for url in ("/Users/jay/Code/BotFleet", "file:///tmp/x.git", "../x", "./x", "~/x",
                    "/Volumes/External/Autorotate.bundle"):
            self.assertFalse(fwb.is_network_url(url), url)

    def commit(self, name: str, content: str, branch: str | None = None, start: str | None = None) -> str:
        if branch:
            git(self.r, "checkout", "-q", "-b", branch, *([start] if start else []))
        write(self.r / name, content)
        git(self.r, "add", "-A")
        git(self.r, "commit", "-q", "-m", f"add {name}")
        return git(self.r, "rev-parse", "HEAD")

    def bundle_heads(self, stage) -> list[str]:
        out = []
        for k in stage.items:
            if k.endswith(".bundle"):
                text = subprocess.run(["git", "bundle", "list-heads", str(self.cfg.dry_stage / k)], env=GENV,
                                      capture_output=True, text=True).stdout
                out += [l.split()[1] for l in text.splitlines()]
        return sorted(out)

    def test_a_hit_commit_drops_the_branches_that_carry_it_and_only_those(self):
        git(self.r, "checkout", "-q", "-b", "bad", "origin/main")
        self.commit("doc.md", f"example {AWS}\n")
        self.commit("more.md", "fine\n", branch="stacked-on-bad")           # carries the bad commit
        self.commit("ok.md", "fine\n", branch="independent", start="origin/main")
        git(self.r, "checkout", "-q", "main")
        stage, *_ = fwb.build_stage(self.cfg, self.cfg.dry_stage)
        hits = [d for d in stage.dropped if " ref " in d["path"]]
        self.assertEqual(sorted(d["path"].split(" ref ")[1] for d in hits), ["refs/heads/bad", "refs/heads/stacked-on-bad"])
        self.assertEqual({d["reason"] for d in hits}, {"content:aws-access-key-id"})
        self.assertTrue(all(len(d["digest"]) == 12 for d in hits))
        self.assertEqual(self.bundle_heads(stage), ["refs/heads/independent", "refs/heads/main"])

    def test_a_secret_in_a_pushed_commit_does_not_cost_the_local_branch(self):
        git(self.r, "checkout", "-q", "-b", "feat", "origin/main")
        self.commit("pushed.md", f"already on GitHub {AWS}\n")
        git(self.r, "push", "-q", "origin", "feat")
        self.commit("local.md", "only here\n", branch="local-on-feat")
        git(self.r, "checkout", "-q", "main")
        stage, *_ = fwb.build_stage(self.cfg, self.cfg.dry_stage)
        self.assertEqual([d for d in stage.dropped if " ref " in d["path"]], [])
        self.assertIn("refs/heads/local-on-feat", self.bundle_heads(stage))

    def test_the_commit_scanner_gives_the_same_answer_with_tiny_read_chunks(self):
        for i, pad in enumerate((0, 1, 37, 4000)):
            self.commit(f"f{i}.txt", "x" * pad + " " + AWS + " " + "y" * pad + "\n", branch=f"b{i}", start="main")
            git(self.r, "checkout", "-q", "main")
        self.commit("clean.txt", "nothing\n", branch="clean", start="main")
        git(self.r, "checkout", "-q", "main")
        git_obj = fwb.Git(self.cfg)
        args = ["--branches", "--not", "--remotes=origin"]
        want, truncated = fwb.scan_commits(git_obj, str(self.r), args, 10 ** 9)
        self.assertEqual(len(want), 4)
        self.assertFalse(truncated)
        for chunk in (1, 5, 64, 1000):
            with mock.patch.object(fwb, "SCAN_CHUNK", chunk):
                got, _ = fwb.scan_commits(git_obj, str(self.r), args, 10 ** 9)
            self.assertEqual(got, want, chunk)

    def test_a_scan_that_hits_its_window_is_flagged_not_silent(self):
        self.cfg.bundle_scan_cap = 100
        with mock.patch.object(fwb, "SCAN_CHUNK", 50):         # the window is checked between reads
            stage, disc, *_ = fwb.build_stage(self.cfg, self.cfg.dry_stage)
            self.assertTrue(any(n.startswith("bundle-scan-partial") for n in stage.notes))
            self.assertEqual(fwb.cmd_run(self.cfg, self.args), 0)
        self.assertIn("warn=gitleaks-missing,bundle-scan-partial", self.log().splitlines()[-1])
        self.cfg.bundle_scan_cap = 512 * 1024 * 1024            # a scan that read it all is not flagged, whatever the cap
        stage, *_ = fwb.build_stage(self.cfg, self.cfg.dry_stage)
        self.assertFalse(any(n.startswith("bundle-scan-partial") for n in stage.notes))

    def test_a_detached_head_bundle_is_scanned_like_a_branch(self):
        d = make_repo(self.home, self.home / "Code" / "det")
        git(d, "checkout", "-q", "--detach")
        write(d / "k.md", f"example {AWS}\n")
        git(d, "add", "-A")
        git(d, "commit", "-q", "-m", "leaky detached")
        stage = self.build()
        self.assertFalse([k for k in stage.items if k.endswith("head.bundle")])
        self.assertEqual([x["path"] for x in stage.dropped if "detached HEAD" in x["path"]], [f"{d} (detached HEAD)"])
        git(d, "checkout", "-q", "main")
        git(d, "checkout", "-q", "--detach")
        write(d / "k2.md", "fine\n")
        git(d, "add", "-A")
        git(d, "commit", "-q", "-m", "clean detached")
        stage = self.build()
        self.assertTrue([k for k in stage.items if k.endswith("head.bundle")])

    def test_a_linked_worktree_outside_the_scanned_folders_is_found_through_git(self):
        away = self.home / "elsewhere" / "wt"
        away.parent.mkdir(parents=True)
        git(self.r, "worktree", "add", "-q", "-b", "away", str(away))
        write(away / "a.txt", "dirty far away\n")
        gone = self.home / "elsewhere" / "gone"
        git(self.r, "worktree", "add", "-q", "-b", "gone", str(gone))
        shutil.rmtree(gone)
        stage = self.build()
        self.assertTrue([k for k in stage.items if "/patches/" in k and k.endswith(".patch")])
        self.assertTrue(any(n.startswith("worktree-missing") and str(gone) in n for n in self.disc.notes))

    def test_a_dangling_worktree_folder_is_kept_as_plain_files(self):
        pruned = self.home / "apps" / "pruned"
        pruned.mkdir(parents=True)
        (pruned / ".git").write_text("gitdir: /nonexistent/gone/.git/worktrees/x\n")
        write(pruned / "notes.txt", "work that was in a pruned worktree\n")
        stage = self.build()
        self.assertIn("files/apps/pruned/notes.txt", stage.items)

    def test_runtime_session_stores_are_skipped_by_default(self):
        a = self.home / "apps"
        write(a / "grok-acp-runtime" / "acp-home" / "sessions" / "s1.json", "{}\n")
        write(a / "grok-acp-runtime" / "acp-home" / "memtrace" / "m.json", "{}\n")
        write(a / "grok-acp-runtime" / "acp-home" / "config.toml", "x=1\n")
        write(a / "fleet-rag" / "mined" / "chat" / "x.jsonl", "{}\n")
        write(a / "fleet-rag" / "mined" / "keep.json", "{}\n")
        write(a / "grok-acp-runtime" / "acp-home" / "auth.json", '{"k": 1}\n')
        stage = self.build()
        got = sorted(k for k in stage.items if k.startswith("files/apps/") and ("acp-home" in k or "mined" in k))
        self.assertEqual(got, ["files/apps/fleet-rag/mined/keep.json", "files/apps/grok-acp-runtime/acp-home/config.toml"])
        self.assertTrue(any(n.startswith("skipped-by-default") for n in stage.notes))
        self.assertIn("secret-name", [d["reason"] for d in stage.dropped if d["path"].endswith("auth.json")])


class RunBehaviourCase(RunBase):
    def test_a_full_swap_alone_does_not_skip_a_quiet_mac(self):
        self.cfg.swap_fn = lambda: 0.97
        self.cfg.pressure_fn = lambda: 1
        self.assertIsNone(fwb.pressure_reason(self.cfg))
        self.cfg.pressure_fn = lambda: 2
        self.assertEqual(fwb.pressure_reason(self.cfg), "swap=97%>=95%,pressure=2")
        self.cfg.swap_fn = lambda: 0.1
        self.cfg.pressure_fn = lambda: 4
        self.assertEqual(fwb.pressure_reason(self.cfg), "memory-pressure=4")
        self.cfg.pressure_fn = lambda: None            # no sysctl:  the swap rule alone
        self.cfg.swap_fn = lambda: 0.96
        self.assertTrue(fwb.pressure_reason(self.cfg).startswith("swap=96%"))

    def test_the_gate_fields_are_in_the_run_line(self):
        self.assertEqual(fwb.cmd_run(self.cfg, self.args), 0)
        line = self.log().splitlines()[-1]
        self.assertIn("gate=regex-only", line)
        self.assertIn("warn=gitleaks-missing", line)
        self.assertIn("deferred=0", line)
        self.assertRegex(line, r"regex_only=\d+")
        self.cfg.gitleaks_bin = str(self.stub_gitleaks('printf "[]" > "$rep"\nexit 0\n'))
        self.assertEqual(fwb.cmd_run(self.cfg, self.args), 0)
        line = self.log().splitlines()[-1]
        self.assertIn("gate=gitleaks", line)
        self.assertNotIn("gate=gitleaks-error", line)
        self.assertNotIn("gitleaks-missing", line)
        self.assertIn("status=ok", line)

    def test_a_lane_link_onto_an_unmounted_volume_makes_the_run_partial(self):
        lanes = self.home / "apps" / "lanes"
        lanes.mkdir(parents=True)
        (lanes / "Repo").symlink_to("/Volumes/fwb-test-not-mounted/Lanes/Repo")
        self.assertEqual(fwb.cmd_run(self.cfg, self.args), 0)
        line = self.log().splitlines()[-1]
        self.assertIn("status=partial", line)
        self.assertIn("warn=volume-missing", line)
        backup = next(a for a in self.argv() if a.startswith("backup"))
        self.assertIn("--tag fleet-work --tag partial", backup)      # kept out of the good snapshots' retention
        forget = next(a for a in self.argv() if a.startswith("forget"))
        self.assertIn("--group-by host,tags", forget)
        self.assertIn("not mounted", self.cfg.detail_log_path.read_text())

    def test_a_good_run_carries_no_partial_tag(self):
        fwb.cmd_run(self.cfg, self.args)
        self.assertNotIn("partial", next(a for a in self.argv() if a.startswith("backup")))

    def test_a_wrong_password_is_its_own_reason(self):
        os.environ["STUB_WRONG_PW"] = "1"
        try:
            self.assertEqual(fwb.cmd_run(self.cfg, self.args), 4)
        finally:
            os.environ.pop("STUB_WRONG_PW", None)
        self.assertIn("status=error reason=wrong-password", self.log())

    def test_runs_that_are_not_ok_are_counted_and_warn_after_a_day(self):
        os.environ["STUB_FAIL"] = "1"
        for _ in range(8):
            fwb.cmd_run(self.cfg, self.args)
        lines = self.log().splitlines()
        self.assertNotIn("warn=", lines[6])
        self.assertIn("not_ok_in_a_row=8", lines[7])
        self.assertIn("warn=no-complete-backup-for-about-24h", lines[7])
        self.assertIn("last_ok=never", lines[7])
        os.environ.pop("STUB_FAIL")
        fwb.cmd_run(self.cfg, self.args)
        self.assertEqual(fwb.read_state(self.cfg)["consecutive_not_ok"], 0)

    def test_skip_gitleaks_is_refused_outside_a_dry_run(self):
        self.args.skip_gitleaks = True
        self.assertEqual(fwb.cmd_run(self.cfg, self.args), 2)
        self.assertIn("reason=skip-gitleaks-is-for-a-dry-run", self.log())
        self.assertEqual(self.argv(), [])

    def test_the_alarm_handler_is_restored_after_a_run(self):
        self.cfg.max_runtime = 3600
        before = signal.getsignal(signal.SIGALRM)
        self.assertEqual(fwb.cmd_run(self.cfg, self.args), 0)
        self.assertEqual(signal.getsignal(signal.SIGALRM), before)
        self.assertEqual(signal.alarm(0), 0)           # and no alarm is left armed

    def test_a_timeout_removes_the_stage(self):
        with mock.patch.object(fwb, "build_stage", side_effect=fwb.Timeout()):
            self.cfg.stage.mkdir(parents=True)
            self.assertEqual(fwb.cmd_run(self.cfg, self.args), 3)
        self.assertFalse(self.cfg.stage.exists())

    def test_a_restic_timeout_sends_sigint_first_so_the_lock_is_released(self):
        stub = self.tmp / "slow-restic"
        stub.write_text(
            "#!/usr/bin/env python3\n"
            "import os, signal, sys, time\n"
            "def h(*_):\n"
            "    open(os.environ['STUB_DIR'] + '/sig.log', 'a').write('got-int\\n')\n"
            "    sys.exit(130)\n"
            "signal.signal(signal.SIGINT, h)\n"
            "time.sleep(30)\n")
        stub.chmod(0o755)
        self.cfg.restic_bin = str(stub)
        with self.assertRaises(subprocess.TimeoutExpired):
            fwb.run_restic(self.cfg, dict(os.environ), "backup", timeout=1)
        self.assertEqual((self.stubdir / "sig.log").read_text().split(), ["got-int"])

    def test_restic_runs_at_low_priority_with_two_threads(self):
        stub = self.tmp / "env-restic"
        stub.write_text("#!/bin/bash\necho \"GOMAXPROCS=$GOMAXPROCS nice=$(ps -o nice= -p $$ | tr -d ' ')\" >> \"$STUB_DIR/prio.log\"\nexit 0\n")
        stub.chmod(0o755)
        self.cfg.restic_bin = str(stub)
        fwb.run_restic(self.cfg, dict(os.environ), "snapshots")
        out = (self.stubdir / "prio.log").read_text()
        self.assertIn("GOMAXPROCS=2", out)
        self.assertRegex(out, r"nice=(1[0-9]|[2-9][0-9])")

    def test_a_repository_override_needs_only_the_password(self):
        self.cfg.repository = str(self.tmp / "repo")
        self.set_creds(names=("RESTIC_FLEET_WORK_PASSWORD",))
        env, missing = fwb.restic_env(self.cfg)
        self.assertEqual(missing, [])
        self.assertEqual(env["RESTIC_REPOSITORY"], str(self.tmp / "repo"))
        self.assertNotIn("AWS_ACCESS_KEY_ID", {k for k in env if k not in os.environ})


class LaunchdCase(RunBase):
    """check-access --launchd:  a one-shot launchd job reports what the schedule can read.  launchctl is a stub
    that runs the job's command directly, so the plumbing is covered;  whether macOS grants the volume to a
    launchd job is only known on the Mac itself."""

    def setUp(self):
        super().setUp()
        self.cfg.check_wait_s = 20
        stub = self.tmp / "launchctl"
        stub.write_text(
            "#!/bin/bash\n"
            'echo "$@" >> "$STUB_DIR/launchctl.log"\n'
            'if [ "$1" = bootstrap ]; then\n'
            '  [ -n "$STUB_BOOTSTRAP_FAIL" ] && exit 1\n'
            '  cp "$3" "$STUB_DIR/seen.plist"\n'
            '  [ -n "$STUB_NO_RESULT" ] && exit 0\n'
            "  python3 -c 'import plistlib,subprocess,sys; d=plistlib.load(open(sys.argv[1],\"rb\")); "
            "subprocess.run(d[\"ProgramArguments\"], env=d[\"EnvironmentVariables\"], check=False)' \"$3\"\n"
            "fi\nexit 0\n")
        stub.chmod(0o755)
        self.cfg.launchctl_bin = str(stub)

    def run_check(self) -> tuple[int, str]:
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = fwb.check_via_launchd(self.cfg)
        return rc, buf.getvalue()

    def test_the_job_is_one_shot_separately_labelled_and_removed_afterwards(self):
        rc, out = self.run_check()
        self.assertEqual(rc, 0, out)
        log = (self.stubdir / "launchctl.log").read_text().splitlines()
        target = f"gui/{os.getuid()}/com.jay.fleet-work-backup.check"
        self.assertEqual(log[0], f"bootout {target}")
        self.assertTrue(log[1].startswith(f"bootstrap gui/{os.getuid()} "))
        self.assertEqual(log[-1], f"bootout {target}")
        seen = plistlib.loads((self.stubdir / "seen.plist").read_bytes())
        self.assertEqual(seen["Label"], "com.jay.fleet-work-backup.check")
        self.assertTrue(seen["RunAtLoad"])
        self.assertEqual(seen["ProgramArguments"][0], "/usr/bin/python3")
        self.assertEqual(seen["ProgramArguments"][2:4], ["check-access", "--result"])
        self.assertNotIn("run", seen["ProgramArguments"])         # it can never start a backup
        self.assertEqual(seen["EnvironmentVariables"]["HOME"], str(self.home))
        self.assertFalse(self.cfg.check_plist_path.exists())
        self.assertIn("launchd job ran as:", out)
        self.assertEqual(self.argv(), [])

    def test_an_unmounted_volume_is_reported_from_the_job(self):
        lanes = self.home / "apps" / "lanes"
        lanes.mkdir(parents=True)
        (lanes / "Repo").symlink_to("/Volumes/fwb-test-not-mounted/Lanes/Repo")
        rc, out = self.run_check()
        self.assertEqual(rc, 1, out)
        self.assertIn("not mounted", out)

    def test_a_bootstrap_failure_and_a_silent_job_are_errors(self):
        os.environ["STUB_BOOTSTRAP_FAIL"] = "1"
        try:
            rc, out = self.run_check()
        finally:
            os.environ.pop("STUB_BOOTSTRAP_FAIL")
        self.assertEqual(rc, 2)
        self.assertIn("bootstrap failed", out)
        self.cfg.check_wait_s = 1
        os.environ["STUB_NO_RESULT"] = "1"
        try:
            rc, out = self.run_check()
        finally:
            os.environ.pop("STUB_NO_RESULT")
        self.assertEqual(rc, 2)
        self.assertIn("no result from the launchd job", out)
        self.assertFalse(self.cfg.check_plist_path.exists())

    def test_a_plain_check_says_it_reflects_the_terminal(self):
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), mock.patch.dict(os.environ, {"XPC_SERVICE_NAME": "0"}):
            fwb.cmd_check_access(self.cfg, argparse.Namespace(launchd=False, result=None))
        self.assertIn("check-access --launchd", buf.getvalue())
        self.assertIn("privacy grants of the terminal", buf.getvalue())

    def test_the_image_is_the_running_executable_not_the_launcher(self):
        img = fwb.process_image()
        self.assertTrue(os.path.exists(img))
        self.assertEqual(fwb.app_bundle("/x/Python.framework/Resources/Python.app/Contents/MacOS/Python"),
                         "/x/Python.framework/Resources/Python.app")
        self.assertEqual(fwb.app_bundle("/usr/bin/python3"), "")
        self.assertEqual(fwb.app_bundle("/Applications/Xcode.app/Contents/Developer/Library/Python3.framework/Versions/3.9/"
                                        "Resources/Python.app/Contents/MacOS/Python"),
                         "/Applications/Xcode.app/Contents/Developer/Library/Python3.framework/Versions/3.9/Resources/Python.app")
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(fwb.cmd_image(self.cfg, None), 0)
        self.assertEqual(buf.getvalue().splitlines()[0], img)


@unittest.skipUnless(shutil.which("restic"), "restic not installed")
class RealResticCase(RunBase):
    """init, backup, check and restore against a real restic and a local repository (CI installs restic)."""

    def test_a_backup_round_trips_and_the_restored_bundle_verifies(self):
        self.cfg.restic_bin = "restic"
        self.cfg.repository = str(self.tmp / "repo")
        self.set_creds(names=("RESTIC_FLEET_WORK_PASSWORD",))
        self.assertEqual(fwb.cmd_init(self.cfg, self.args), 0)
        self.assertEqual(fwb.cmd_run(self.cfg, self.args), 0)
        line = self.log().splitlines()[-1]
        self.assertIn("status=ok", line, line)
        self.assertFalse(self.cfg.stage.exists())
        target = self.tmp / "restored"
        ns = argparse.Namespace(restic_args=["--", "restore", "latest", "--tag", "fleet-work", "--target", str(target)])
        self.assertEqual(fwb.cmd_restic(self.cfg, ns), 0)
        stage = next(target.rglob("MANIFEST.json")).parent     # restic keeps the absolute path it was given
        self.assertEqual((stage / "files/apps/doc.md").read_text(), "doc\n")
        (b,) = sorted(stage.glob("git/*/bundles/*.bundle"))
        fresh = self.tmp / "fresh"
        git(self.tmp, "clone", "-q", f"file://{self.home / 'remotes' / 'r.git'}", str(fresh))
        git(fresh, "bundle", "verify", str(b))
        git(fresh, "fetch", "-q", str(b), "refs/heads/*:refs/restored/*")
        self.assertEqual(git(fresh, "show", "refs/restored/main:a.txt"), "changed and committed, not pushed")
        self.assertEqual(fwb.cmd_restic(self.cfg, argparse.Namespace(restic_args=["--", "check"])), 0)
        self.assertEqual(fwb.cmd_snapshots(self.cfg, self.args), 0)


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
        self.assertEqual(res["binary"], fwb.process_image())
        self.assertIn(fwb.process_image(), fwb.removable_hint(self.cfg))


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
        self.assertIn("<string>/usr/bin/python3</string>", plist)       # Apple-signed:  a privacy grant survives updates
        self.assertIn("<key>Nice</key>", plist)
        self.assertIn("<key>LowPriorityIO</key>", plist)
        self.assertIn("fleet-work-backup.out.log", plist)


if __name__ == "__main__":
    unittest.main(verbosity=2)
