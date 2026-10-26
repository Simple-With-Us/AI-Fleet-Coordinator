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
import contextlib
import errno
import fcntl
import hashlib
import importlib.util
import io
import json
import os
import plistlib
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
GENV = dict(os.environ)
GENV.update(
    {
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_AUTHOR_NAME": "T",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "T",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
    }
)
os.environ.update(
    {k: GENV[k] for k in ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM", "GIT_TERMINAL_PROMPT")}
)
for _name in (
    "B2_FLEET_BUCKET_NAME",
    "B2_FLEET_WORK_BUCKET_NAME",
    "B2_FLEET_WORK_KEY_ID",
    "B2_FLEET_WORK_APPLICATION_KEY",
    "RESTIC_FLEET_WORK_PASSWORD",
    "FLEET_WORK_BACKUP_ALLOW_MASTER",
    "BACKBLAZE_MASTER_KEY_ID",
    "BACKBLAZE_MASTER_APPLICATION_KEY",
):
    os.environ.pop(_name, None)

spec = importlib.util.spec_from_file_location("fleet_work_backup", HERE / "fleet_work_backup.py")
fwb = importlib.util.module_from_spec(spec)
sys.modules["fleet_work_backup"] = fwb
spec.loader.exec_module(fwb)

# Secret-shaped strings, assembled so no literal secret sits in this file.
AWS = "AKIA" + "Q7ZP3KM9TX2WJ4HB"
AWS2 = "AKIA" + "Z8Y7X6W5V4U3T2S1"
PEM = (
    "-----BEGIN "
    + "RSA PRIVATE KEY-----\n"
    + "MIIEowIBAAKCAQEA"
    + "q7Zp3Km9Tx2Wj4Hb"
    + "Lm8Vc5Rn1Yd6Fs0Gt"
)
HIGH = "".join(["aB3dE5", "gH7jK9", "mN1pQ3", "sT5v7x", "Zq2"])
PW = "".join(["pw-", "Zx9Qw3", "Er5Ty7", "Ui1Op"])
OPAQUE = "".join(["Zq8Lm", "2Vx7R", "t4Yp9", "Kd3Hs", "1Wb6N", "c0Fj5", "Gu1Wb", "6Nc0F", "p2Qa7"])
KEYID = "".join(["005", "abcdef0123", "456789012"])
B2TOKEN = "fake-key"  # mocked b2_authorize_account token;  not a real B2 authorization shape
APPKEY = "".join(["K005", "AbCdEfGhIjKlMn", "OpQrStUvWx1"])


def git(cwd, *args, check=True):
    p = subprocess.run(
        [
            "git",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "init.defaultBranch=main",
            "-C",
            str(cwd),
            *args,
        ],
        env=GENV,
        capture_output=True,
    )
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
        subprocess.run(
            ["git", "-c", "init.defaultBranch=main", "init", "-q", "--bare", str(bare)],
            env=GENV,
            check=True,
        )
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
            out.append(
                (rel, st.st_size, 0 if rel.startswith("objects" + os.sep) else st.st_mtime_ns)
            )
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
        write(self.dirty / "a.txt", "a changed\n")  # tracked change, fine
        write(self.dirty / "leaky.py", "k = 1\n")
        git(self.dirty, "add", "leaky.py")
        git(self.dirty, "commit", "-q", "-m", "leaky base")
        git(self.dirty, "push", "-q")
        write(self.dirty / "leaky.py", f'k = "{AWS}"\n')  # tracked change holding a key
        write(self.dirty / "tokenizer.py", "t = 1\n")  # untracked, broad name -> dropped
        write(self.dirty / "plain.txt", "plain untracked\n")
        write(self.dirty / "ignored.txt", "never\n")
        write(self.dirty / ".env", "SOMETHING=1\n")
        write(self.dirty / "id_rsa", "not really\n")
        write(self.dirty / "notes.md", f"key {AWS}\n")  # untracked, content secret
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
        cls.git_before = {
            p: snapshot_tree(p / ".git") for p in (cls.fx.unpushed, cls.fx.landed, cls.fx.dirty)
        }
        cls.stage, cls.disc, cls.wts, cls.groups, cls.files, cls.gate, _ = fwb.build_stage(
            cls.fx.cfg, cls.fx.cfg.stage
        )
        cls.rep = fwb.build_report(
            cls.fx.cfg, cls.disc, cls.wts, cls.groups, cls.stage, cls.files, cls.gate, 0
        )
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
        for n in (
            "pushed-clean",
            "unpushed",
            "flat-dirty",
            "stash-repo",
            "no-remote",
            "landed",
            "detached",
        ):
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
        return sorted(
            self.root / r for r in self.stage.items if r.startswith(f"git/{slug}/bundles/")
        )

    def test_bundles_belong_to_the_repository_not_to_a_worktree(self):
        g = self.group("unpushed")
        self.assertEqual(
            len(self.bundles_of("unpushed")), 1
        )  # main, claude/feature and claude/wt share a commit
        self.assertFalse(
            any(
                r.endswith(".bundle") and "/worktrees/" in r and f"/{g.slug}/" in r
                for r in self.stage.items
            )
        )

    def test_bundle_holds_unpushed_branches_and_restores_through_a_real_clone(self):
        (b,) = self.bundles_of("unpushed")
        heads = subprocess.run(
            ["git", "bundle", "list-heads", str(b)], env=GENV, capture_output=True, text=True
        ).stdout
        for ref in ("refs/heads/main", "refs/heads/claude/feature", "refs/heads/claude/wt"):
            self.assertIn(ref, heads)
        fresh = Path(self.fx.tmp) / "restore-unpushed"
        # file:// goes through the transport like GitHub does:  only reachable objects arrive
        git(
            Path(self.fx.tmp),
            "clone",
            "-q",
            f"file://{self.fx.home / 'remotes' / 'unpushed.git'}",
            str(fresh),
        )
        git(fresh, "bundle", "verify", str(b))
        git(fresh, "fetch", "-q", str(b), "refs/heads/*:refs/restored/*")
        self.assertEqual(git(fresh, "show", "refs/restored/claude/feature:feature.txt"), "feature")
        self.assertEqual(git(fresh, "show", "refs/restored/main:a.txt"), "a2")

    def test_group_metadata_names_the_bound_and_the_bundles(self):
        g = self.group("unpushed")
        meta = json.loads((self.root / f"git/{g.slug}/group.json").read_text())
        self.assertEqual(meta["bound"]["ref"], "refs/remotes/origin/main")
        self.assertEqual(
            meta["bound"]["sha"], git(self.fx.unpushed, "rev-parse", "refs/remotes/origin/main")
        )
        self.assertEqual(meta["network_remotes"], ["origin"])
        self.assertEqual(len(meta["bundles"]), 1)
        self.assertEqual(
            sorted(r["ref"] for r in meta["bundles"][0]["refs"]),
            ["refs/heads/claude/feature", "refs/heads/claude/wt", "refs/heads/main"],
        )

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
        self.assertTrue(
            any(r.endswith("head.bundle") and f"/{g.slug}/" in r for r in self.stage.items)
        )

    # (a) dirty work -----------------------------------------------------------------------
    def test_patch_per_file_applies_onto_the_recorded_head(self):
        g = self.group("flat-dirty")
        patches = self.rels(f"git/{g.slug}/worktrees/")
        patches = [r for r in patches if "/patches/" in r]
        self.assertEqual(len(patches), 1, patches)  # a.txt only:  leaky.py was dropped alone
        fresh = Path(self.fx.tmp) / "restore-dirty"
        git(
            Path(self.fx.tmp),
            "clone",
            "-q",
            f"file://{self.fx.home / 'remotes' / 'flat-dirty.git'}",
            str(fresh),
        )
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
        un = [
            r.split("/untracked/", 1)[1]
            for r in self.rels(f"git/{g.slug}/worktrees/")
            if "/untracked/" in r
        ]
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
        git(
            Path(self.fx.tmp),
            "clone",
            "-q",
            f"file://{self.fx.home / 'remotes' / 'stash-repo.git'}",
            str(fresh),
        )
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
        for p in (
            "leaky.py",
            "tokenizer.py",
            "notes.md",
            "config-token.json",
            "helper.sh",
            "foo.txt",
            ".env",
            "id_rsa",
        ):
            self.assertIn(p, paths)

    # (b) loose files ----------------------------------------------------------------------
    def test_loose_files_staged_and_junk_skipped(self):
        files = self.rels("files/")
        for want in (
            "files/apps/FLEET-PROTOCOL.md",
            "files/apps/notes/x.txt",
            "files/apps/ok-helper.sh",
            "files/apps/scratch-env/.env.example",
            "files/Code/loose.py",
        ):
            self.assertIn(want, files)
        for junk in (
            "node_modules",
            "run.log",
            "__pycache__",
            ".venv",
            "logs/today.txt",
            "secrets/foo.txt",
            "config-token.json",
            "helper.sh\x00",
            "lanes/",
        ):
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
        yes = [
            ".env",
            ".env.local",
            "prod.env",
            "a/b/.env.production",
            "x.pem",
            "AuthKey_AB12.p8",
            "k.key",
            "id_rsa",
            "id_ed25519.pub",
            "my-zuliprc",
            "global-api-keys",
            "a/.secrets/x",
            "a/.ssh/config",
            "credentials.json",
            "mytoken.txt",
            "app-secret.yml",
            "Foo.KEYCHAIN-DB",
            "infisical-machine-identity.json",
            ".dev.vars",
            ".dev.vars.production",
            "a/auth.json",
            "oauth_creds.json",
            "x/.envrc",
            "infra/prod.tfstate",
            "infra/terraform.tfstate.backup",
            "vars.tfvars",
            ".config/gh/hosts.yml",
            "gh/hosts.yml",
        ]
        no = [
            ".env.example",
            "src/app.py",
            "README.md",
            "a/b/c.txt",
            "environment.md",
            "ansible/hosts.yml",
            "hosts.yml",
            "src/authors.json",
        ]
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
        for name in (
            "config/client_secret.json",
            "src/service_token.txt",
            "deploy/secrets.yaml",
            "config/secrets/prod.yaml",
            "db/credentials.sqlite",
            "x/passwd.conf",
        ):
            self.assertTrue(fwb.is_secret_name(name, tracked=True), name)

    def test_content_patterns(self):
        for text in (
            AWS,
            PEM,
            "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8",
            "xoxb-" + "8271936450-Qm7ZkP3tXw",
            "sk-ant-" + "api03-Zq8Lm2Vx7Rt4Yp9Kd3Hs",
            "eyJhbGciOiJIUzI1NiJ9." + "eyJzdWIiOiIxMjM0NTY3ODkwIn0." + "abcdefghijk1234567",
            f"SERVICE_API_KEY={HIGH}",
            f'export DB_PASSWORD="{HIGH}"',
        ):
            self.assertIsNotNone(fwb.scan_bytes(text.encode()), text[:12])

    def test_a_json_credential_field_is_a_secret_but_a_placeholder_is_not(self):
        for text in (
            json.dumps({"refresh_token": OPAQUE}),
            json.dumps({"client_secret": OPAQUE}),
            '{\n  "api_key" : "' + OPAQUE + '"\n}',
        ):
            self.assertEqual(fwb.scan_bytes(text.encode()), "json-secret-field", text[:30])
        for text in (
            json.dumps({"access_token": "changeme"}),
            json.dumps({"password": "x" * 30}),
            json.dumps({"api_key": "${API_KEY_FROM_THE_ENVIRONMENT}"}),
            json.dumps({"token_type": "Bearer"}),
        ):
            self.assertIsNone(fwb.scan_bytes(text.encode()), text[:30])

    def test_a_pem_header_without_a_body_is_not_a_key(self):
        for text in (
            "-----BEGIN " + "PRIVATE KEY-----",
            "'-----BEGIN " + "RSA PRIVATE KEY-----\\n...\\n-----END RSA PRIVATE KEY-----'",
        ):
            self.assertIsNone(fwb.scan_bytes(text.encode()), text)
        self.assertEqual(fwb.scan_bytes(PEM.encode()), "private-key-block")
        as_json = PEM.replace("\n", "\\n")
        self.assertEqual(fwb.scan_bytes(as_json.encode()), "private-key-block")

    def test_documentation_placeholders_are_not_secrets(self):
        for text in (
            "AKIA" + "IOSFODNN7EXAMPLE",
            "sk_live_" + "x" * 24,
            "sk_test_" + "0" * 24,
            "ghp_" + "a" * 36,
            "AKIA" + "ABCDEFGHIJKLMNOP",
        ):
            self.assertIsNone(fwb.scan_bytes(text.encode()), text)
        self.assertIsNotNone(fwb.scan_bytes(("sk_live_" + "9f2b7c41d8e3a605b2c7f914").encode()))

    def test_content_patterns_leave_ordinary_text_alone(self):
        for text in (
            "API_KEY=changeme-changeme-changeme",
            "TOKEN=${TOKEN}",
            "const token = getToken();",
            "SERVICE_API_KEY=abcdefghijklmnopqrstuvwxyz",
            "see AKIA docs",
            "password: required",
            "STRIPE_SECRET_KEY=your-stripe-key-here-0123",
        ):
            self.assertIsNone(fwb.scan_bytes(text.encode()), text)

    def test_scan_file_covers_binary_and_chunk_seams(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x.bin"
            blob = b"\x00" * (8 * 1024 * 1024 - 5) + AWS.encode() + b"\x00" * 100
            p.write_bytes(blob)
            self.assertEqual(fwb.scan_file(p, 64 * 1024 * 1024), "aws-access-key-id")

    def test_url_credentials_are_stripped(self):
        self.assertEqual(
            fwb.strip_url_credentials("https://user:tok@github.com/a/b.git"),
            "https://github.com/a/b.git",
        )


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
        self.cfg.pressure_fn = lambda: (
            None
        )  # as on a Mac without the sysctl:  the swap rule stands alone
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
            "fi\nexit 0\n"
        )
        stub.chmod(0o755)
        os.environ["STUB_DIR"] = str(self.stubdir)
        os.environ.pop("STUB_FAIL", None)
        self.cfg.restic_bin = str(stub)
        fs_stub = (
            self.stubdir / "fleet-secrets"
        )  # `run [--only X] -- CMD`:  CMD with the fake creds as env
        fs_stub.write_text(
            "#!/bin/bash\n"
            '[ "$1" = run ] || exit 2\n'
            'shift; [ "$1" = -- ] && shift\n'
            f'set -a; . "{self.creds_file}"; set +a\n'
            'exec "$@"\n'
        )
        fs_stub.chmod(0o755)
        self.cfg.fleet_secrets_bin = str(fs_stub)
        self.set_creds()
        self.cfg.b2_http = self.fake_b2()  # never the real B2 API
        self.cfg.infisical_bin = str(
            self.tmp / "no-infisical"
        )  # never the real Infisical CLI (it is logged in to prod)
        self.args = argparse.Namespace(
            dry_run=False, json=None, keep_stage=False, ignore_pressure=False, skip_gitleaks=False
        )

    def fake_b2(
        self,
        prefix="fleet-work-backup/",
        buckets=("test-bucket",),
        caps=None,
        rules=None,
        auth_status=200,
    ):
        """An offline B2 API:  b2_authorize_account (v4 shape) and b2_list_buckets."""
        caps = (
            caps
            if caps is not None
            else ["listFiles", "readFiles", "writeFiles", "deleteFiles", "listBuckets"]
        )
        rules = (
            rules
            if rules is not None
            else [
                {
                    "fileNamePrefix": "fleet-work-backup/",
                    "daysFromHidingToDeleting": 14,
                    "daysFromUploadingToHiding": None,
                }
            ]
        )
        calls = []

        def http(method, url, headers, body):
            calls.append((method, url, dict(headers), body))
            if url.endswith("/b2api/v4/b2_authorize_account"):
                if auth_status != 200:
                    return auth_status, {"code": "unauthorized", "status": auth_status}
                return 200, {
                    "accountId": "acct",
                    "authorizationToken": B2TOKEN,
                    "apiInfo": {
                        "storageApi": {
                            "apiUrl": "https://api000.example.invalid",
                            "allowed": {
                                "buckets": [{"id": "b1", "name": n} for n in buckets],
                                "capabilities": caps,
                                "namePrefix": prefix,
                            },
                        }
                    },
                }
            if url.endswith("/b2api/v4/b2_list_buckets"):
                return 200, {"buckets": [{"bucketName": "test-bucket", "lifecycleRules": rules}]}
            return 404, {}

        http.calls = calls
        return http

    def tearDown(self):
        os.environ.pop("STUB_DIR", None)
        os.environ.pop("STUB_FAIL", None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def set_creds(
        self,
        names=(
            "B2_FLEET_WORK_BUCKET_NAME",
            "B2_FLEET_WORK_KEY_ID",
            "B2_FLEET_WORK_APPLICATION_KEY",
            "RESTIC_FLEET_WORK_PASSWORD",
        ),
    ):
        vals = {
            "B2_FLEET_WORK_BUCKET_NAME": '"test-bucket"',
            "B2_FLEET_WORK_KEY_ID": f'"{KEYID}"',
            "B2_FLEET_WORK_APPLICATION_KEY": f'"{APPKEY}"',
            "RESTIC_FLEET_WORK_PASSWORD": f'"{PW}"',
        }
        handoff = self.creds_file
        handoff.parent.mkdir(parents=True, exist_ok=True)
        handoff.write_text("OTHER=1\n" + "".join(f"{n}={vals[n]}\n" for n in names))
        handoff.chmod(0o600)

    @property
    def creds_file(self) -> Path:
        """What the fake ``fleet-secrets`` serves (a test double for Infisical Fleet-OPS)."""
        return self.tmp / "fake-fleet-secrets.env"

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
            'find "$root" -type f | sed "s|^$root/||" >> "$STUB_DIR/gl-files.log"\n' + body
        )
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
        for want in (
            "--keep-hourly 24",
            "--keep-daily 14",
            "--keep-weekly 8",
            "--keep-monthly 6",
            "--tag fleet-work",
            "--prune",
        ):
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
        self.assertIn(
            "REPO=s3:https://s3.eu-central-003.backblazeb2.com/test-bucket/fleet-work-backup", env
        )
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
        self.set_creds(names=("B2_FLEET_WORK_BUCKET_NAME", "B2_FLEET_WORK_KEY_ID"))
        self.assertEqual(fwb.cmd_run(self.cfg, self.args), 2)
        line = self.log()
        self.assertIn("reason=missing-credentials", line)
        self.assertIn("B2_FLEET_WORK_APPLICATION_KEY", line)
        self.assertIn("RESTIC_FLEET_WORK_PASSWORD", line)
        self.assertEqual(self.argv(), [])

    def test_the_bucket_name_has_no_fallback_to_the_shared_fleet_bucket(self):
        """The shared name in fleet-secrets or the environment must never point the backup at the shared bucket."""
        self.set_creds(
            names=(
                "B2_FLEET_WORK_KEY_ID",
                "B2_FLEET_WORK_APPLICATION_KEY",
                "RESTIC_FLEET_WORK_PASSWORD",
            )
        )
        handoff = self.creds_file
        with handoff.open("a") as fh:
            fh.write("B2_FLEET_BUCKET_NAME=jays-fleet-shared-eu\n")
        with mock.patch.dict(os.environ, {"B2_FLEET_BUCKET_NAME": "jays-fleet-shared-eu"}):
            env, missing = fwb.restic_env(self.cfg)
            self.assertIsNone(env)
            self.assertEqual(missing, ["B2_FLEET_WORK_BUCKET_NAME"])
            self.assertEqual(fwb.cmd_run(self.cfg, self.args), 2)
            self.assertIn("reason=missing-credentials", self.log())
            self.assertIn("B2_FLEET_WORK_BUCKET_NAME", self.log())
            self.assertNotIn("jays-fleet-shared-eu", self.log())
            self.assertEqual(self.argv(), [])
            probs = fwb.check_b2_setup(self.cfg)
            self.assertTrue(probs and probs[0][0])
            self.assertIn("B2_FLEET_WORK_BUCKET_NAME", probs[0][1])
        self.assertEqual(fwb.CRED_NAMES["bucket"], "B2_FLEET_WORK_BUCKET_NAME")

    def test_master_key_is_only_a_fallback_when_asked(self):
        handoff = self.creds_file
        handoff.write_text(
            f"B2_FLEET_WORK_BUCKET_NAME=b\nRESTIC_FLEET_WORK_PASSWORD={PW}\n"
            f"BACKBLAZE_MASTER_KEY_ID={KEYID}\nBACKBLAZE_MASTER_APPLICATION_KEY={APPKEY}\n"
        )
        env, missing = fwb.restic_env(self.cfg)
        self.assertIsNone(env)
        self.assertIn("B2_FLEET_WORK_KEY_ID", missing)
        with mock.patch.dict(os.environ, {"FLEET_WORK_BACKUP_ALLOW_MASTER": "1"}):
            env, missing = fwb.restic_env(self.cfg)
            self.assertIsNone(env)  # a backup or init never takes the master key
            env, missing = fwb.restic_env(self.cfg, allow_master=True)
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
        self.assertTrue(
            any(a.startswith("backup") for a in self.argv())
        )  # the readable part still goes up

    def test_gitleaks_findings_drop_the_flagged_file(self):
        stub = self.tmp / "gitleaks"
        stub.write_text(
            "#!/bin/bash\n"
            'while [ $# -gt 0 ]; do [ "$1" = "-r" ] && rep="$2"; [ "$1" = dir ] && root="$2"; shift; done\n'
            'printf \'[{"RuleID":"stub-rule","File":"%s/files/apps/doc.md"}]\' "$root" > "$rep"\nexit 0\n'
        )
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
            'printf \'[{"RuleID":"stub-rule","File":"%s/files/apps/doc.md"}]\' "$root" > "$rep"\nexit 0\n'
        )
        stub.chmod(0o755)
        self.cfg.gitleaks_bin = str(stub)
        self.cfg.state_dir.mkdir(parents=True, exist_ok=True)
        src = str(self.home / "apps" / "doc.md")
        (self.cfg.state_dir / "allow.txt").write_text(
            f"{src} @ {hashlib.sha1(b'doc' + bytes([10])).hexdigest()[:12]}\n"
        )
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
            'echo run >> "$STUB_DIR/gl-runs.log"\nprintf "[]" > "$rep"\nexit 0\n'
        )
        stub.chmod(0o755)
        return stub

    def test_scan_cache_means_a_second_run_rescans_only_what_changed(self):
        self.cfg.gitleaks_bin = str(self.counting_gitleaks())
        stage, *_, gate1, _ = fwb.build_stage(self.cfg, self.cfg.dry_stage)
        self.assertGreater(gate1["scanned"], 0)
        self.assertEqual(gate1["cached"], 0)
        first_files = (self.stubdir / "gl-files.log").read_text().split()
        self.assertIn("files/apps/doc.md", first_files)
        (self.stubdir / "gl-files.log").unlink()
        write(self.home / "apps" / "added.md", "new file\n")
        stage, *_, gate2, _ = fwb.build_stage(self.cfg, self.cfg.dry_stage)
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
        self.assertNotIn("files/apps/doc.md", stage.items)  # fail closed:  not uploaded unscanned
        self.assertTrue(
            any(
                d["path"].endswith("doc.md") and d["reason"] == "gitleaks-budget"
                for d in stage.deferred
            )
        )
        self.assertEqual(stage.dropped, [])  # a deferral is not a secret finding
        self.assertTrue(
            any(k.endswith(".bundle") for k in stage.items)
        )  # bundles were scanned before they were made
        self.cfg.gitleaks_budget_s = 1500  # the next run picks the file up
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
        self.assertIn(
            "--tag fleet-work --tag partial",
            " ".join(a for a in self.argv() if a.startswith("backup")),
        )

    def test_one_unreadable_file_does_not_block_the_files_around_it(self):
        write(self.home / "apps" / "poison.md", "p\n")
        write(self.home / "apps" / "zz-later.md", "later\n")
        stub = self.stub_gitleaks(
            'find "$root" -name poison.md | grep -q . && exit 2\nprintf "[]" > "$rep"\nexit 0\n'
        )
        self.cfg.gitleaks_bin = str(stub)
        stage, *_, gate, _ = fwb.build_stage(self.cfg, self.cfg.dry_stage)
        self.assertIn("files/apps/doc.md", stage.items)
        self.assertIn("files/apps/zz-later.md", stage.items)
        self.assertNotIn("files/apps/poison.md", stage.items)
        self.assertEqual([d["path"].rsplit("/", 1)[-1] for d in stage.deferred], ["poison.md"])
        self.assertEqual(gate["gitleaks_unreadable"], 1)

    def test_a_finding_that_maps_to_no_staged_file_is_an_error_not_a_pass(self):
        stub = self.stub_gitleaks(
            'printf \'[{"RuleID":"r","File":"/nowhere/else.txt"}]\' > "$rep"\nexit 0\n'
        )
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
        self.assertNotIn("files/apps/blob.bin", seen)  # gitleaks does not read binary content
        self.assertNotIn("files/apps/x.db", seen)
        self.assertIn("files/apps/blob.bin", stage.items)  # regex-scanned, kept, and counted
        self.assertIn("files/apps/x.db", stage.items)
        self.assertEqual(gate["regex_only"], 2)
        self.assertEqual(gate["deferred"], 0)
        limit = (
            (self.stubdir / "gl-argv.log")
            .read_text()
            .split("--max-target-megabytes ")[1]
            .split()[0]
        )
        self.assertEqual(
            int(limit), self.cfg.max_file_bytes // (1024 * 1024) + 1
        )  # nothing staged is skipped for size

    def test_a_binary_file_with_a_key_is_still_dropped_by_the_regex(self):
        write(self.home / "apps" / "blob.bin", "\0\1\2 " + AWS + "\n")
        stage, *_ = fwb.build_stage(self.cfg, self.cfg.dry_stage)
        self.assertNotIn("files/apps/blob.bin", stage.items)
        self.assertEqual(
            [d["reason"] for d in stage.dropped if d["path"].endswith("blob.bin")],
            ["content:aws-access-key-id"],
        )

    def test_a_reviewed_regex_hit_still_goes_through_gitleaks(self):
        write(self.home / "apps" / "rule.md", f"bad example: {AWS}\n")
        stage, *_ = fwb.build_stage(self.cfg, self.cfg.dry_stage)
        digest = next(d["digest"] for d in stage.dropped if d["path"].endswith("rule.md"))
        self.cfg.state_dir.mkdir(parents=True, exist_ok=True)
        (self.cfg.state_dir / "allow.txt").write_text(
            f"{self.home / 'apps' / 'rule.md'} @ {digest}\n"
        )
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
            f"# reviewed\n{src} @ {digest}\n{self.home / 'apps' / 'prod.env'} @ {'0' * 12}\n"
        )
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
        self.assertNotIn(
            "files/apps/rule.md", stage.items
        )  # an allowed hit is never cached as clean

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
        m = [
            l.strip()
            for l in detail.splitlines()
            if l.strip().startswith("allow-line ") and src in l
        ]
        self.assertEqual(len(m), 1)
        self.assertNotIn(AWS, detail)
        self.cfg.state_dir.mkdir(parents=True, exist_ok=True)
        (self.cfg.state_dir / "allow.txt").write_text(m[0][len("allow-line ") :] + "\n")
        stage, *_ = fwb.build_stage(self.cfg, self.cfg.dry_stage)
        self.assertIn("files/apps/rule.md", stage.items)

    def bundle_heads(self, stage):
        out = ""
        for k in stage.items:
            if k.endswith(".bundle"):
                out += subprocess.run(
                    ["git", "bundle", "list-heads", str(self.cfg.dry_stage / k)],
                    env=GENV,
                    capture_output=True,
                    text=True,
                ).stdout
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
        self.assertEqual(
            [(d["path"], d["digest"]) for d in stage.dropped if " ref " in d["path"]],
            [(label, tip[:12])],
        )
        self.cfg.state_dir.mkdir(parents=True, exist_ok=True)
        (self.cfg.state_dir / "allow.txt").write_text(f"{label} @ {tip[:12]}\n")
        stage, *_ = fwb.build_stage(self.cfg, self.cfg.dry_stage)
        self.assertTrue(any(k.endswith(".bundle") for k in stage.items))
        write(r / "more.txt", "the branch moved on\n")  # a new commit:  the reviewed tip is gone
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
        self.assertEqual(
            [d["path"].split(" ref ")[1] for d in stage.dropped if " ref " in d["path"]],
            ["refs/heads/leaky-branch"],
        )

    def test_a_new_pattern_or_scanner_version_discards_the_scan_cache(self):
        stage, *_, gate1, _ = fwb.build_stage(self.cfg, self.cfg.dry_stage)
        stage, *_, gate2, _ = fwb.build_stage(self.cfg, self.cfg.dry_stage)
        self.assertEqual(gate2["scanned"], 0)
        with mock.patch.dict(
            fwb.CONTENT_PATTERNS, {"new-pattern": fwb.re.compile(rb"zzz-never-matches")}
        ):
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
        stub.write_text(
            text.replace(
                "fi\nexit 0\n",
                'fi\nif [ "$1" = backup ] && [ -n "$STUB_RC3" ]; then exit 3; fi\nexit 0\n',
            )
        )
        os.environ["STUB_RC3"] = "1"
        try:
            self.assertEqual(fwb.cmd_run(self.cfg, self.args), 0)
        finally:
            os.environ.pop("STUB_RC3", None)
        line = self.log().splitlines()[-1]
        self.assertIn("status=partial", line)
        self.assertIn("restic-unreadable-files", line)
        self.assertFalse(
            any(a.startswith("forget") for a in self.argv())
        )  # a degraded run never drives retention
        self.assertIn(
            "tag --add partial abcdef0123456789", self.argv()
        )  # kept out of the good snapshots' retention

    def test_a_changed_file_is_rescanned_and_a_new_secret_is_caught(self):
        stage, *_, gate1, _ = fwb.build_stage(self.cfg, self.cfg.dry_stage)
        self.assertIn("files/apps/doc.md", stage.items)
        write(self.home / "apps" / "doc.md", f"token {AWS}\n")
        stage, *_, gate2, _ = fwb.build_stage(self.cfg, self.cfg.dry_stage)
        self.assertNotIn("files/apps/doc.md", stage.items)
        self.assertEqual(
            [d["reason"] for d in stage.dropped if d["path"].endswith("doc.md")],
            ["content:aws-access-key-id"],
        )

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
        self.assertFalse(
            [k for k in stage.items if k.endswith(".patch")]
        )  # no unreadable binary patch
        self.assertEqual(stage.items[raw[0]].src, str(self.r / "data.bin"))
        meta = json.loads(
            (self.cfg.dry_stage / f"{self.base}/worktrees")
            .glob("*/worktree.json")
            .__next__()
            .read_text()
        )
        self.assertEqual(meta["head"], git(self.r, "rev-parse", "HEAD"))

    def test_a_key_inside_a_modified_binary_is_caught_by_the_regex(self):
        (self.r / "data.bin").write_bytes(b"\0" + AWS.encode() + b"\n")
        stage = self.build()
        self.assertFalse([k for k in stage.items if "data.bin" in k])
        self.assertEqual(
            [d["reason"] for d in stage.dropped if d["path"].endswith("data.bin")],
            ["content:aws-access-key-id"],
        )
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
        keys = sorted(
            k.split(f"{self.base}/stashes/", 1)[1]
            for k in stage.items
            if "/stashes/" in k and "/binary/" in k
        )
        self.assertEqual(
            keys, ["01/tracked/binary/data.bin", "01/untracked/binary/new.bin"]
        )  # the clean stash (index 1)
        got = (self.cfg.dry_stage / f"{self.base}/stashes/01/tracked/binary/data.bin").read_bytes()
        self.assertEqual(got, b"\0 clean stash content\n")
        dropped = sorted(
            d["path"].rsplit(":", 1)[1] for d in stage.dropped if "#stash0" in d["path"]
        )
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
        self.assertIn(
            "secret-name",
            [d["reason"] for d in stage.dropped if d["path"].endswith("client_secret.bin")],
        )

    def test_an_oversize_stash_entry_is_skipped_before_git_produces_it(self):
        (self.r / "data.bin").write_bytes(b"\0" + b"x" * 5000)
        git(self.r, "stash", "push", "-q", "-m", "big")
        self.cfg.max_file_bytes = 1000
        stage = self.build()
        self.assertTrue(
            any("#stash0:data.bin" in f["path"] and f["size"] > 1000 for f in stage.large)
        )
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
        out = subprocess.run(
            ["bash", "-c", f'set -e\n{func}\ncd "{fresh}"\napply_all "{patches}"\n'],
            capture_output=True,
            text=True,
        )
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
        out = subprocess.run(
            ["git", "bundle", "list-heads", str(bundle)], env=GENV, capture_output=True, text=True
        ).stdout
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
        self.assertEqual(len(bundles), 2)  # main's own commit, and the feat-based branch
        for b in bundles:
            git(fresh, "bundle", "verify", str(b))
            git(fresh, "fetch", "-q", str(b), "refs/heads/*:refs/restored/*")
        self.assertEqual(git(fresh, "show", "refs/restored/local-on-feat:local.txt"), "local")
        self.assertEqual(
            git(fresh, "show", "refs/restored/local-on-feat:feat.txt"), "feat"
        )  # the deleted branch's commit travels along
        self.assertEqual(
            git(fresh, "show", "refs/restored/main:a.txt"), "changed and committed, not pushed"
        )

    def test_independent_branches_get_their_own_bundle_and_overlapping_ones_share(self):
        for name in ("a", "b"):
            git(self.r, "checkout", "-q", "-b", name, "origin/main")
            write(self.r / f"branch-{name}.txt", name + "\n")
            git(self.r, "add", "-A")
            git(self.r, "commit", "-q", "-m", name)
        git(self.r, "checkout", "-q", "main")
        git(self.r, "branch", "c")  # same commit as main:  overlaps it
        stage = self.build()
        by = {tuple(self.heads(b)): b for b in self.bundles(stage)}
        self.assertEqual(
            sorted(by), [("refs/heads/a",), ("refs/heads/b",), ("refs/heads/c", "refs/heads/main")]
        )

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
        self.assertEqual(len(first), 2)  # so restic stores an unchanged branch once

    def test_a_remote_that_is_a_local_path_does_not_count_as_pushed(self):
        lo = make_repo(self.home, self.home / "Code" / "local-origin", remote=None)
        bare = self.home / "remotes" / "local-origin.git"
        subprocess.run(
            ["git", "-c", "init.defaultBranch=main", "init", "-q", "--bare", str(bare)],
            env=GENV,
            check=True,
        )
        git(lo, "remote", "add", "origin", str(bare))
        git(lo, "push", "-q", "-u", "origin", "main")
        stage = self.build()
        g = next(g for g in self.groups if g.name == "local-origin")
        self.assertEqual(g.unpushed_refs, 1)  # on the same disk is not a backup
        self.assertFalse(g.no_remote)
        (b,) = [
            self.cfg.dry_stage / k
            for k in stage.items
            if k.endswith(".bundle") and f"/{g.slug}/" in k
        ]
        fresh = self.tmp / "from-bundle"
        git(self.tmp, "clone", "-q", str(b), str(fresh))  # whole history, no prerequisites
        self.assertEqual((fresh / "a.txt").read_text(), "a\n")
        self.assertTrue(any(n.startswith("local-only-remotes") for n in stage.notes))

    def proxied_child(self, scenario: str):
        """A flat lane cloned from a checkout that has a GitHub-looking remote;  returns (stage, group, parent, child,
        expected bound sha).  ``current``:  the clone is up to date.  ``stale``:  the checkout's main moved on.
        ``diverged``:  the clone's origin/main is a commit GitHub never got (the checkout's main was reset)."""
        parent = make_repo(self.home, self.home / "Code" / "parent")  # origin looks like GitHub
        base = git(parent, "rev-parse", "HEAD")
        if scenario == "diverged":
            write(parent / "never-pushed.txt", "only in the checkout's local main\n")
            git(parent, "add", "-A")
            git(parent, "commit", "-q", "-m", "local main only")
        child = self.home / "apps" / "child"
        git(self.tmp, "clone", "-q", str(parent), str(child))  # child's only remote is a local path
        child_main = git(child, "rev-parse", "refs/remotes/origin/main")
        if scenario == "diverged":
            git(parent, "reset", "-q", "--hard", "origin/main")
        if scenario in ("stale", "diverged"):
            write(parent / "moved-on.txt", "main moved on after the clone was made\n")
            git(parent, "add", "-A")
            git(parent, "commit", "-q", "-m", "main moves on")
            git(parent, "push", "-q")
        write(child / "child.txt", "work only in the clone\n")
        git(child, "add", "-A")
        git(child, "commit", "-q", "-m", "clone-only work")
        stage = self.build()
        want = {
            "current": git(parent, "rev-parse", "refs/remotes/origin/main"),
            "stale": child_main,
            "diverged": base,
        }[scenario]
        return stage, next(g for g in self.groups if g.name == "child"), parent, child, want

    def test_a_clone_of_a_local_checkout_is_bounded_by_that_checkouts_github_branch(self):
        for scenario in ("current", "stale", "diverged"):
            with self.subTest(scenario):
                for p in (
                    self.home / "Code" / "parent",
                    self.home / "apps" / "child",
                    self.home / "remotes" / "parent.git",
                ):
                    shutil.rmtree(p, ignore_errors=True)
                stage, g, parent, child, want = self.proxied_child(scenario)
                self.assertEqual(g.unpushed_refs, 1)
                meta = json.loads((self.cfg.dry_stage / f"git/{g.slug}/group.json").read_text())
                self.assertEqual(meta["network_remotes"], [])
                self.assertEqual(meta["bound"]["sha"], want)
                self.assertEqual(os.path.realpath(meta["bound"]["via"]), os.path.realpath(parent))
                self.assertEqual(meta["bound"]["via_url"], "https://example.invalid/parent.git")
                self.assertTrue(any(n.startswith("local-remote-proxied") for n in stage.notes))
                (b,) = [
                    self.cfg.dry_stage / k
                    for k in stage.items
                    if k.endswith(".bundle") and f"/{g.slug}/" in k
                ]
                fresh = self.tmp / f"from-github-{scenario}"
                git(
                    self.tmp,
                    "clone",
                    "-q",
                    f"file://{self.home / 'remotes' / 'parent.git'}",
                    str(fresh),
                )  # what GitHub serves
                git(fresh, "bundle", "verify", str(b))
                git(fresh, "fetch", "-q", str(b), "refs/heads/*:refs/restored/*")
                self.assertEqual(
                    git(fresh, "show", "refs/restored/main:child.txt"), "work only in the clone"
                )

    def test_network_urls(self):
        for url in (
            "https://github.com/a/b.git",
            "git@github.com:a/b.git",
            "ssh://git@github.com/a/b",
            "github.com:a/b",
            "git://example.org/x.git",
            "HTTPS://GITHUB.COM/a/b",
        ):
            self.assertTrue(fwb.is_network_url(url), url)
        for url in (
            "/Users/jay/Code/BotFleet",
            "file:///tmp/x.git",
            "../x",
            "./x",
            "~/x",
            "/Volumes/External/Autorotate.bundle",
        ):
            self.assertFalse(fwb.is_network_url(url), url)

    def commit(
        self, name: str, content: str, branch: str | None = None, start: str | None = None
    ) -> str:
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
                text = subprocess.run(
                    ["git", "bundle", "list-heads", str(self.cfg.dry_stage / k)],
                    env=GENV,
                    capture_output=True,
                    text=True,
                ).stdout
                out += [l.split()[1] for l in text.splitlines()]
        return sorted(out)

    def test_a_hit_commit_drops_the_branches_that_carry_it_and_only_those(self):
        git(self.r, "checkout", "-q", "-b", "bad", "origin/main")
        self.commit("doc.md", f"example {AWS}\n")
        self.commit("more.md", "fine\n", branch="stacked-on-bad")  # carries the bad commit
        self.commit("ok.md", "fine\n", branch="independent", start="origin/main")
        git(self.r, "checkout", "-q", "main")
        stage, *_ = fwb.build_stage(self.cfg, self.cfg.dry_stage)
        hits = [d for d in stage.dropped if " ref " in d["path"]]
        self.assertEqual(
            sorted(d["path"].split(" ref ")[1] for d in hits),
            ["refs/heads/bad", "refs/heads/stacked-on-bad"],
        )
        self.assertEqual({d["reason"] for d in hits}, {"content:aws-access-key-id"})
        self.assertTrue(all(len(d["digest"]) == 12 for d in hits))
        self.assertEqual(self.bundle_heads(stage), ["refs/heads/independent", "refs/heads/main"])

    def test_two_independent_leaky_branches_are_both_dropped(self):
        """refs_containing with several --contains arguments must mean "any of them", or the second branch is bundled."""
        for name, key in (("x", AWS), ("y", AWS2)):
            git(self.r, "checkout", "-q", "-b", name, "origin/main")
            self.commit(f"leak-{name}.md", f"example {key}\n")
        git(self.r, "checkout", "-q", "-b", "stacked-on-y")
        self.commit("more.md", "fine\n")
        git(self.r, "checkout", "-q", "main")
        stage, *_ = fwb.build_stage(self.cfg, self.cfg.dry_stage)
        dropped = sorted(d["path"].split(" ref ")[1] for d in stage.dropped if " ref " in d["path"])
        self.assertEqual(dropped, ["refs/heads/stacked-on-y", "refs/heads/x", "refs/heads/y"])
        self.assertEqual(self.bundle_heads(stage), ["refs/heads/main"])
        # the same answer from a batch of commits as from one commit at a time
        git_obj = fwb.Git(self.cfg)
        bad, _ = fwb.scan_commits(
            git_obj, str(self.r), ["--branches", "--not", "--remotes=origin"], 10**9
        )
        self.assertEqual(len(bad), 2)
        refs = {"refs/heads/x", "refs/heads/y", "refs/heads/stacked-on-y", "refs/heads/main"}
        self.assertEqual(
            fwb.refs_containing(git_obj, str(self.r), list(bad), refs),
            {"refs/heads/x", "refs/heads/y", "refs/heads/stacked-on-y"},
        )

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
            self.commit(
                f"f{i}.txt",
                "x" * pad + " " + AWS + " " + "y" * pad + "\n",
                branch=f"b{i}",
                start="main",
            )
            git(self.r, "checkout", "-q", "main")
        self.commit("clean.txt", "nothing\n", branch="clean", start="main")
        git(self.r, "checkout", "-q", "main")
        git_obj = fwb.Git(self.cfg)
        args = ["--branches", "--not", "--remotes=origin"]
        want, truncated = fwb.scan_commits(git_obj, str(self.r), args, 10**9)
        self.assertEqual(len(want), 4)
        self.assertFalse(truncated)
        for chunk in (1, 5, 64, 1000):
            with mock.patch.object(fwb, "SCAN_CHUNK", chunk):
                got, _ = fwb.scan_commits(git_obj, str(self.r), args, 10**9)
            self.assertEqual(got, want, chunk)

    def test_a_scan_that_hits_its_window_is_flagged_not_silent(self):
        self.cfg.bundle_scan_cap = 100
        with mock.patch.object(fwb, "SCAN_CHUNK", 50):  # the window is checked between reads
            stage, disc, *_ = fwb.build_stage(self.cfg, self.cfg.dry_stage)
            self.assertTrue(any(n.startswith("bundle-scan-partial") for n in stage.notes))
            self.assertEqual(fwb.cmd_run(self.cfg, self.args), 0)
        self.assertIn("warn=gitleaks-missing,bundle-scan-partial", self.log().splitlines()[-1])
        self.cfg.bundle_scan_cap = (
            512 * 1024 * 1024
        )  # a scan that read it all is not flagged, whatever the cap
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
        self.assertEqual(
            [x["path"] for x in stage.dropped if "detached HEAD" in x["path"]],
            [f"{d} (detached HEAD)"],
        )
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
        self.assertTrue(
            any(n.startswith("worktree-missing") and str(gone) in n for n in self.disc.notes)
        )

    def test_a_dangling_worktree_folder_is_kept_as_plain_files(self):
        pruned = self.home / "apps" / "pruned"
        pruned.mkdir(parents=True)
        (pruned / ".git").write_text("gitdir: /nonexistent/gone/.git/worktrees/x\n")
        write(pruned / "notes.txt", "work that was in a pruned worktree\n")
        stage = self.build()
        self.assertIn("files/apps/pruned/notes.txt", stage.items)

    def test_a_pruned_lane_under_lanes_is_kept_as_plain_files_and_warned(self):
        lane = self.home / "apps" / "lanes" / "Repo" / "claude-pruned"
        lane.mkdir(parents=True)
        (lane / ".git").write_text("gitdir: /nonexistent/gone/.git/worktrees/claude-pruned\n")
        write(lane / "src" / "work.py", "uncommitted work in a pruned lane\n")
        write(lane / ".env", f"TOKEN={HIGH}\n")
        write(lane / "node_modules" / "x.js", "x\n")
        stage = self.build()
        self.assertIn("files/apps/lanes/Repo/claude-pruned/src/work.py", stage.items)
        self.assertFalse([k for k in stage.items if k.endswith(".env") or "node_modules" in k])
        self.assertIn(str(lane / ".env"), [d["path"] for d in stage.dropped])
        self.assertEqual(fwb.cmd_run(self.cfg, self.args), 0)
        self.assertIn("dangling-worktree", self.log().splitlines()[-1])

    def test_runtime_session_stores_are_skipped_by_default(self):
        a = self.home / "apps"
        write(a / "grok-acp-runtime" / "acp-home" / "sessions" / "s1.json", "{}\n")
        write(a / "grok-acp-runtime" / "acp-home" / "memtrace" / "m.json", "{}\n")
        write(a / "grok-acp-runtime" / "acp-home" / "config.toml", "x=1\n")
        write(a / "fleet-rag" / "mined" / "chat" / "x.jsonl", "{}\n")
        write(a / "fleet-rag" / "mined" / "keep.json", "{}\n")
        write(a / "grok-acp-runtime" / "acp-home" / "auth.json", '{"k": 1}\n')
        stage = self.build()
        got = sorted(
            k
            for k in stage.items
            if k.startswith("files/apps/") and ("acp-home" in k or "mined" in k)
        )
        self.assertEqual(
            got,
            [
                "files/apps/fleet-rag/mined/keep.json",
                "files/apps/grok-acp-runtime/acp-home/config.toml",
            ],
        )
        self.assertTrue(any(n.startswith("skipped-by-default") for n in stage.notes))
        self.assertIn(
            "secret-name", [d["reason"] for d in stage.dropped if d["path"].endswith("auth.json")]
        )

    def test_the_two_named_session_stores_are_staged_and_memtrace_is_not_with_the_plist_settings(
        self,
    ):
        """The repo plist sets NO_DEFAULT_PATH_SKIPS=1 and EXTRA_SKIP_DIRS=memtrace:  acp-home/sessions and
        fleet-rag/mined/chat go through the normal gate, and a directory named memtrace is still skipped."""
        a = self.home / "apps"
        write(a / "grok-acp-runtime" / "acp-home" / "sessions" / "s1.json", "{}\n")
        write(a / "grok-acp-runtime" / "acp-home" / "memtrace" / "m.json", "{}\n")
        write(a / "grok-acp-runtime" / "acp-home" / "config.toml", "x=1\n")
        write(a / "fleet-rag" / "mined" / "chat" / "x.jsonl", "{}\n")
        write(a / "fleet-rag" / "mined" / "chat" / "x.log", "log line\n")
        write(a / "fleet-rag" / "mined" / "chat" / "secret.jsonl", f"AWS_KEY={AWS}\n")
        write(a / "grok-acp-runtime" / "acp-home" / "auth.json", '{"k": 1}\n')
        with (
            mock.patch.object(fwb, "SKIP_PATH_PARTS", ()),
            mock.patch.object(fwb, "SKIP_DIR_NAMES", fwb.SKIP_DIR_NAMES | {"memtrace"}),
        ):
            stage = self.build()
        got = sorted(
            k
            for k in stage.items
            if k.startswith("files/apps/") and ("acp-home" in k or "mined" in k)
        )
        self.assertEqual(
            got,
            [
                "files/apps/fleet-rag/mined/chat/x.jsonl",
                "files/apps/grok-acp-runtime/acp-home/config.toml",
                "files/apps/grok-acp-runtime/acp-home/sessions/s1.json",
            ],
        )  # memtrace stays out
        self.assertFalse(any(n.startswith("skipped-by-default") for n in stage.notes))
        dropped = {Path(d["path"]).name: d["reason"] for d in stage.dropped}
        self.assertEqual(dropped.get("auth.json"), "secret-name")  # the name filter still applies
        self.assertIn("secret.jsonl", dropped)  # and so does the content scan


def restore_md_functions(*names: str) -> str:
    """The named shell functions from RESTORE.md, exactly as written there (indented four spaces)."""
    md = (HERE / "RESTORE.md").read_text()
    out = []
    for name in names:
        m = re.search(rf"(?ms)^    {name}\(\) \{{[^\n]*\n.*?^    \}}\n", md)
        if not m:
            raise AssertionError(f"RESTORE.md has no {name}()")
        out.append("\n".join(l[4:] for l in m.group(0).splitlines()))
    return "\n".join(out)


class AnchorCase(RunBase):
    """The commit a stash or a dirty worktree's patches apply to is bundled (anchors.bundle) when GitHub's default
    branch and the branch bundles do not hold it, so the RESTORE.md steps work after GitHub deletes a branch."""

    def setUp(self):
        super().setUp()
        self.r = self.home / "Code" / "r"
        git(self.r, "push", "-q")  # main is pushed:  only the anchors are under test
        self.bare = self.home / "remotes" / "r.git"

    def build(self):
        stage, disc, wts, groups, *_ = fwb.build_stage(self.cfg, self.cfg.dry_stage)
        self.group = groups[0]
        self.G = self.cfg.dry_stage / f"git/{self.group.slug}"
        return stage

    def fresh_clone(self) -> Path:
        fresh = self.tmp / "fresh"
        git(
            self.tmp, "clone", "-q", f"file://{self.bare}", str(fresh)
        )  # only what "GitHub" serves arrives
        return fresh

    def restore(self, cwd: Path, script: str) -> None:
        funcs = restore_md_functions("apply_all", "fetch_bundles")
        out = subprocess.run(
            ["bash", "-c", f'set -e\n{funcs}\ncd "{cwd}"\n{script}\n'],
            capture_output=True,
            text=True,
            env=GENV,
        )
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertNotIn("CANNOT VERIFY", out.stdout)
        self.assertNotIn("NEEDS HAND", out.stdout)

    def worktree_meta(self, path: Path) -> tuple[Path, dict]:
        for p in self.G.glob("worktrees/*/worktree.json"):
            meta = json.loads(p.read_text())
            if meta["path"] == str(path):
                return p.parent, meta
        raise AssertionError(f"no worktree.json for {path}")

    def stash_on_a_deleted_branch(self, extra=None) -> str:
        """A stash made on a branch that never left this Mac and was deleted afterwards:  its base is
        reachable only through refs/stash, which is never bundled."""
        git(self.r, "checkout", "-q", "-b", "tmp")
        write(self.r / "base.txt", "a base commit that never left this Mac\n")
        git(self.r, "add", "-A")
        git(self.r, "commit", "-q", "-m", "tmp base")
        write(self.r / "base.txt", "stashed edit\n")
        if extra:
            extra()
        git(self.r, "stash", "push", "-q", "-u", "-m", "on tmp")
        git(self.r, "checkout", "-q", "main")
        git(self.r, "branch", "-q", "-D", "tmp")
        return git(self.r, "rev-parse", "stash@{0}^1")

    def test_a_stash_on_a_deleted_branch_restores_through_its_anchor(self):
        base = self.stash_on_a_deleted_branch()
        self.build()
        meta = json.loads((self.G / "stashes/00/stash.json").read_text())
        self.assertEqual(meta["base"], base)
        self.assertEqual(meta["anchor"]["ref"], "refs/fwb/stash/00")
        self.assertEqual(meta["anchor"]["state"], "bundled")
        self.assertEqual(self.group.anchor_refs, 1)
        fresh = self.fresh_clone()
        self.assertNotEqual(
            git(fresh, "cat-file", "-t", base, check=False), "commit"
        )  # GitHub never had the base
        self.restore(
            fresh,
            f'fetch_bundles "{self.G}"\ngit checkout -q {base}\napply_all "{self.G}/stashes/00/tracked"',
        )
        self.assertEqual((fresh / "base.txt").read_text(), "stashed edit\n")

    def test_a_dirty_lane_on_a_pushed_branch_restores_after_github_deletes_the_branch(self):
        lane = self.home / "apps" / "lanes" / "r" / "claude-feat"
        lane.parent.mkdir(parents=True)
        git(self.r, "worktree", "add", "-q", "-b", "feat", str(lane), "origin/main")
        write(lane / "f.txt", "feature\n")
        git(lane, "add", "-A")
        git(lane, "commit", "-q", "-m", "feature")
        git(lane, "push", "-q", "origin", "feat")
        head = git(lane, "rev-parse", "HEAD")
        write(lane / "a.txt", "dirty on a branch GitHub will delete\n")
        self.build()
        wdir, meta = self.worktree_meta(lane)
        self.assertEqual(meta["anchor"]["state"], "bundled")
        git(self.bare, "branch", "-D", "feat")  # the squash merge's branch auto-delete
        git(self.bare, "gc", "-q", "--prune=now")
        fresh = self.fresh_clone()
        out = self.tmp / "restored-lane"
        self.restore(
            fresh,
            f'fetch_bundles "{self.G}"\ngit worktree add -q "{out}" {head}\ncd "{out}"\n'
            f'apply_all "{wdir}/patches"',
        )
        self.assertEqual((out / "a.txt").read_text(), "dirty on a branch GitHub will delete\n")
        self.assertEqual((out / "f.txt").read_text(), "feature\n")

    def test_a_dirty_lane_on_a_squash_landed_branch_restores_from_its_anchor(self):
        lane = self.home / "apps" / "lanes" / "r" / "claude-landed"
        lane.parent.mkdir(parents=True)
        git(self.r, "worktree", "add", "-q", "-b", "landed", str(lane), "origin/main")
        write(lane / "f.txt", "feature\n")
        git(lane, "add", "-A")
        git(lane, "commit", "-q", "-m", "feature, never pushed")
        head = git(lane, "rev-parse", "HEAD")
        write(self.r / "f.txt", "feature\n")  # main lands the same change as its own commit
        git(self.r, "add", "f.txt")
        git(self.r, "commit", "-q", "-m", "squash of landed")
        git(self.r, "push", "-q")
        write(lane / "a.txt", "dirty on a landed branch\n")
        self.build()
        self.assertEqual(self.group.landed_refs, 1)  # so no branch bundle carries the lane's HEAD
        wdir, meta = self.worktree_meta(lane)
        self.assertEqual(meta["anchor"]["state"], "bundled")
        fresh = self.fresh_clone()
        out = self.tmp / "restored-lane"
        self.restore(
            fresh,
            f'fetch_bundles "{self.G}"\ngit worktree add -q "{out}" {head}\ncd "{out}"\n'
            f'apply_all "{wdir}/patches"',
        )
        self.assertEqual((out / "a.txt").read_text(), "dirty on a landed branch\n")

    def test_anchors_never_write_to_the_real_repository(self):
        self.stash_on_a_deleted_branch()
        before = snapshot_tree(self.r / ".git")
        refs = git(self.r, "for-each-ref")
        self.build()
        self.assertTrue((self.G / "anchors.bundle").exists())
        self.assertEqual(git(self.r, "for-each-ref"), refs)
        self.assertNotIn("refs/fwb", refs)
        self.assertEqual(snapshot_tree(self.r / ".git"), before)
        self.assertEqual(
            list(self.cfg.cache_dir.glob("objs-*")), []
        )  # the scratch repository is gone

    def test_nothing_the_gate_dropped_from_a_stash_rides_along_in_the_anchor(self):
        def extra():
            write(self.r / "staged.txt", f"key {AWS}\n")
            git(self.r, "add", "staged.txt")  # in the stash's index commit
            write(self.r / ".env", f"TOKEN={HIGH}\n")  # in its untracked commit, a secret name

        self.stash_on_a_deleted_branch(extra)
        self.assertIn(
            AWS, git(self.r, "show", "stash@{0}^2:staged.txt")
        )  # anchoring the stash commit would carry it
        stage = self.build()
        self.assertTrue((self.G / "anchors.bundle").exists())
        reasons = {
            d["path"].rsplit(":", 1)[-1]: d["reason"]
            for d in stage.dropped
            if "#stash0" in d["path"]
        }
        self.assertEqual(reasons.get(".env"), "secret-name")
        self.assertTrue(
            any(r.startswith("content:") for k, r in reasons.items() if k != ".env"), reasons
        )
        fresh = self.fresh_clone()
        self.restore(fresh, f'fetch_bundles "{self.G}"')
        objects = subprocess.run(
            ["git", "-C", str(fresh), "cat-file", "--batch-all-objects", "--batch"],
            env=GENV,
            capture_output=True,
        ).stdout  # inflated, so a key cannot hide in zlib
        self.assertIn(b"a base commit that never left this Mac", objects)
        self.assertNotIn(AWS.encode(), objects)
        self.assertNotIn(HIGH.encode(), objects)

    def test_an_anchor_whose_history_holds_a_key_is_dropped_unless_pinned(self):
        git(self.r, "checkout", "-q", "-b", "tmp")
        write(self.r / "k.txt", f"{AWS}\n")
        git(self.r, "add", "-A")
        git(self.r, "commit", "-q", "-m", "leaky base")
        write(self.r / "a.txt", "edit\n")
        git(self.r, "stash", "push", "-q")
        git(self.r, "checkout", "-q", "main")
        git(self.r, "branch", "-q", "-D", "tmp")
        stage = self.build()
        self.assertFalse((self.G / "anchors.bundle").exists())
        (d,) = [d for d in stage.dropped if d["path"].endswith(" anchor stash/00")]
        self.assertEqual(d["reason"], "content:aws-access-key-id")
        self.assertEqual(
            json.loads((self.G / "stashes/00/stash.json").read_text())["anchor"]["state"], "dropped"
        )
        self.cfg.state_dir.mkdir(parents=True, exist_ok=True)
        (self.cfg.state_dir / "allow.txt").write_text(f"{d['path']} @ {d['digest']}\n")
        self.build()
        self.assertTrue((self.G / "anchors.bundle").exists())

    def test_a_base_github_or_a_branch_bundle_holds_needs_no_anchor(self):
        write(self.r / "a.txt", "edit on main\n")
        git(self.r, "stash", "push", "-q")  # base is origin/main
        git(
            self.r, "checkout", "-q", "-b", "local"
        )  # an unpushed branch:  its own bundle holds HEAD
        write(self.r / "l.txt", "l\n")
        git(self.r, "add", "-A")
        git(self.r, "commit", "-q", "-m", "local")
        write(self.r / "l.txt", "dirty\n")
        self.build()
        self.assertFalse((self.G / "anchors.bundle").exists())
        self.assertEqual(
            json.loads((self.G / "stashes/00/stash.json").read_text())["anchor"]["state"], "covered"
        )
        _, meta = self.worktree_meta(self.r)
        self.assertEqual(meta["anchor"]["state"], "covered")
        self.assertEqual(self.group.anchor_refs, 0)


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
        self.cfg.pressure_fn = lambda: None  # no sysctl:  the swap rule alone
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
        self.assertIn(
            "--tag fleet-work --tag partial", backup
        )  # kept out of the good snapshots' retention
        self.assertIn("forget=skipped-partial", line)
        self.assertFalse(
            any(a.startswith("forget") for a in self.argv())
        )  # no forget or prune on a partial run
        self.assertIn("not mounted", self.cfg.detail_log_path.read_text())
        (lanes / "Repo").unlink()
        self.assertEqual(fwb.cmd_run(self.cfg, self.args), 0)
        forget = next(
            a for a in self.argv() if a.startswith("forget")
        )  # a complete run does, per host and tag set
        self.assertIn("--group-by host,tags", forget)
        self.assertIn("status=ok", self.log().splitlines()[-1])

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

    def stub_agent_sync(self, rc: int = 0, dm_rc: int | None = None) -> Path:
        """A stand-in for agent-sync.  ``rc`` is the exit code of ``post``, ``dm_rc`` (default ``rc``) that of ``dm``.
        argv goes to as-argv.log, stdin to as-text.log (all calls) and to as-<subcommand>-text.log."""
        stub = self.tmp / "agent-sync"
        stub.write_text(
            "#!/bin/bash\n"
            'echo "$@" >> "$STUB_DIR/as-argv.log"\n'
            'f="$(mktemp)"; cat > "$f"\n'
            'for dest in as-text.log "as-$1-text.log"; do { cat "$f"; echo; echo "---"; } >> "$STUB_DIR/$dest"; done\n'
            'rm -f "$f"\n'
            f'if [ "$1" = dm ]; then exit {rc if dm_rc is None else dm_rc}; fi\n'
            f"exit {rc}\n"
        )
        stub.chmod(0o755)
        return stub

    def alerts(self, sub: str | None = None) -> list[str]:
        p = self.stubdir / (f"as-{sub}-text.log" if sub else "as-text.log")
        return [t.strip() for t in p.read_text().split("---") if t.strip()] if p.exists() else []

    def agent_sync_calls(self) -> list[str]:
        """One line per agent-sync call, its arguments joined with spaces, in call order."""
        p = self.stubdir / "as-argv.log"
        return p.read_text().splitlines() if p.exists() else []

    def test_pressure_and_low_disk_skips_count_as_not_ok_but_a_lock_skip_does_not(self):
        self.cfg.load_fn = lambda: 99.0
        fwb.cmd_run(self.cfg, self.args)
        self.assertEqual(fwb.read_state(self.cfg)["consecutive_not_ok"], 1)
        self.cfg.load_fn = lambda: 1.0
        self.cfg.min_free_bytes = 1 << 62
        fwb.cmd_run(self.cfg, self.args)
        self.assertEqual(fwb.read_state(self.cfg)["consecutive_not_ok"], 2)
        self.assertIn("reason=low-disk", self.log().splitlines()[-1])
        self.assertIn("not_ok_in_a_row=2", self.log().splitlines()[-1])
        fd = os.open(str(self.cfg.state_dir / "lock"), os.O_CREAT | os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fwb.cmd_run(self.cfg, self.args)  # another run is working:  not a missed backup
        finally:
            os.close(fd)
        self.assertEqual(fwb.read_state(self.cfg)["consecutive_not_ok"], 2)

    def test_runs_that_stay_not_ok_post_one_alert_then_once_a_day_and_say_when_it_is_over(self):
        self.cfg.agent_sync_bin = str(self.stub_agent_sync())
        self.cfg.alert_to = ("Jay",)
        t = [1_800_000_000.0]
        self.cfg.now_fn = lambda: t[0]
        os.environ["STUB_FAIL"] = "1"
        try:
            for _ in range(7):
                fwb.cmd_run(self.cfg, self.args)
                t[0] += 3 * 3600
            self.assertEqual(self.alerts(), [])
            self.cfg.load_fn = lambda: 99.0  # the eighth is a pressure skip:  it counts too
            fwb.cmd_run(self.cfg, self.args)
            self.cfg.load_fn = lambda: 1.0
            self.assertEqual(len(self.alerts()), 1)
            for _ in range(7):  # the rest of that day:  no more posts
                t[0] += 3 * 3600
                fwb.cmd_run(self.cfg, self.args)
            self.assertEqual(len(self.alerts()), 1)
            t[0] += 3 * 3600
            fwb.cmd_run(self.cfg, self.args)  # a day after the first alert
            self.assertEqual(len(self.alerts()), 2)
        finally:
            os.environ.pop("STUB_FAIL", None)
        t[0] += 3 * 3600
        fwb.cmd_run(self.cfg, self.args)  # complete again
        texts = self.alerts()
        self.assertEqual(len(texts), 3)
        self.assertIn("8 runs in a row with no complete snapshot", texts[0])
        self.assertIn("reason=load1=99.0>60", texts[0])
        self.assertIn(".\u00a0 ", texts[0])  # Zulip sentence gap
        self.assertIn("back to a complete snapshot", texts[2])
        argv = (self.stubdir / "as-argv.log").read_text().splitlines()
        self.assertEqual(
            argv[0], f"post --as CLAUDE --session fwbackup --topic {fwb.ALERT_TOPIC} --to Jay -"
        )
        for secret in (PW, KEYID, APPKEY):
            self.assertNotIn(secret, "".join(texts))
        self.assertNotIn("last_alert", fwb.read_state(self.cfg))

    def test_the_alert_uses_the_job_homes_agent_sync_only(self):
        self.assertEqual(
            fwb.agent_sync_path(self.cfg), str(self.home / ".local" / "bin" / "agent-sync")
        )
        self.assertFalse(
            fwb.post_alert(self.cfg, "x")
        )  # the temp home has none:  the suite never posts for real
        os.environ["STUB_FAIL"] = "1"
        try:
            for _ in range(8):
                fwb.cmd_run(self.cfg, self.args)
        finally:
            os.environ.pop("STUB_FAIL", None)
        self.assertIn("alert-not-posted after 8 runs", self.cfg.detail_log_path.read_text())

    def alert_cfg(
        self,
        dm: bool = True,
        rc: int = 0,
        dm_rc: int | None = None,
        note: str = "NOTE: please fix it.",
    ) -> None:
        self.cfg.agent_sync_bin = str(self.stub_agent_sync(rc, dm_rc))
        self.cfg.alert_to = ("BF-PLUMBER", "GB-PLUMBER")
        self.cfg.alert_dm_owner = dm
        self.cfg.alert_note = note

    def test_the_alert_goes_to_the_topic_and_to_the_owner_by_dm_and_only_the_post_carries_the_note(
        self,
    ):
        self.alert_cfg()
        failed: list = []
        self.assertTrue(fwb.post_alert(self.cfg, "Base text.", failed=failed))
        self.assertEqual(failed, [])
        calls = self.agent_sync_calls()
        self.assertEqual(
            calls,
            [
                f"post --as CLAUDE --session fwbackup --topic {fwb.ALERT_TOPIC} "
                f"--to BF-PLUMBER --to GB-PLUMBER -",
                "dm --as CLAUDE --session fwbackup --owner -",
            ],
        )
        dm = calls[1].split()
        self.assertTrue({"dm", "--as", "--owner"} <= set(dm))
        self.assertNotIn(
            "--to", dm
        )  # --owner is the owner pinned in listener.toml, never a typed name
        self.assertEqual(self.alerts("post"), ["Base text.\u00a0 NOTE: please fix it."])
        self.assertEqual(self.alerts("dm"), ["Base text."])

    def test_without_the_owner_dm_setting_only_the_topic_post_is_sent(self):
        self.alert_cfg(dm=False)
        self.assertTrue(fwb.post_alert(self.cfg, "Base text."))
        self.assertEqual(len(self.agent_sync_calls()), 1)
        self.assertTrue(self.agent_sync_calls()[0].startswith("post "))
        self.assertEqual(self.alerts("dm"), [])
        self.assertEqual(self.alerts("post"), ["Base text.\u00a0 NOTE: please fix it."])

    def test_with_no_note_the_topic_post_is_the_text_alone(self):
        self.alert_cfg(note="")
        self.assertTrue(fwb.post_alert(self.cfg, "Base text."))
        self.assertEqual(self.alerts("post"), ["Base text."])
        self.assertEqual(self.alerts("dm"), ["Base text."])

    def test_one_of_the_two_failing_still_counts_as_delivered_and_both_are_always_tried(self):
        self.alert_cfg(rc=0, dm_rc=1)  # the DM fails, the post lands
        failed: list = []
        self.assertTrue(fwb.post_alert(self.cfg, "Base text.", failed=failed))
        self.assertEqual(failed, ["dm"])
        self.assertEqual(len(self.agent_sync_calls()), 2)
        (self.stubdir / "as-argv.log").unlink()
        self.alert_cfg(rc=1, dm_rc=0)  # the post fails, the DM is still sent and lands
        failed = []
        self.assertTrue(fwb.post_alert(self.cfg, "Base text.", failed=failed))
        self.assertEqual(failed, ["post"])
        self.assertEqual(len(self.agent_sync_calls()), 2)
        self.assertTrue(self.agent_sync_calls()[1].startswith("dm "))

    def test_both_failing_is_false_and_the_run_notes_alert_not_posted_without_setting_last_alert(
        self,
    ):
        self.alert_cfg(rc=1, dm_rc=1)
        failed: list = []
        self.assertFalse(fwb.post_alert(self.cfg, "Base text.", failed=failed))
        self.assertEqual(failed, ["post", "dm"])
        state = {"consecutive_not_ok": 8, "last_ok_text": "never"}
        fwb.alert_after_line(self.cfg, state, "status=error")
        self.assertNotIn("last_alert", state)
        note = self.cfg.detail_log_path.read_text()
        self.assertIn("alert-not-posted after 8 runs", note)
        self.assertIn("not delivered:  post, dm", note)
        self.assertNotIn("alert-partly-posted", note)

    def test_no_agent_sync_binary_fails_both_and_never_raises(self):
        self.cfg.agent_sync_bin = str(self.tmp / "missing-agent-sync")
        self.cfg.alert_dm_owner = True
        failed: list = []
        self.assertFalse(fwb.post_alert(self.cfg, "x", failed=failed))
        self.assertEqual(failed, ["post", "dm"])

    def test_a_timeout_in_one_send_does_not_stop_the_other(self):
        self.alert_cfg()
        real = subprocess.run
        seen = []

        def run(cmd, *a, **kw):
            seen.append(cmd[1])
            if cmd[1] == "post":
                raise subprocess.TimeoutExpired(cmd, 60)
            return real(cmd, *a, **kw)

        with mock.patch.object(fwb.subprocess, "run", run):
            failed: list = []
            self.assertTrue(fwb.post_alert(self.cfg, "x", failed=failed))
        self.assertEqual(seen, ["post", "dm"])
        self.assertEqual(failed, ["post"])

    def test_one_delivered_sets_last_alert_and_notes_which_one_was_not(self):
        self.alert_cfg(rc=0, dm_rc=1)
        state = {"consecutive_not_ok": 8, "last_ok_text": "never"}
        fwb.alert_after_line(self.cfg, state, "status=error")
        self.assertIn("last_alert", state)
        self.assertIn(
            "alert-partly-posted after 8 runs (not delivered:  dm)",
            self.cfg.detail_log_path.read_text(),
        )

    def test_the_not_ok_alert_names_the_logs_and_the_recovery_message_goes_to_both_without_the_note(
        self,
    ):
        self.alert_cfg()
        state = {"consecutive_not_ok": 8, "last_ok_text": "never"}
        fwb.alert_after_line(self.cfg, state, "status=error reason=boom")
        post, dm = self.alerts("post")[0], self.alerts("dm")[0]
        self.assertTrue(post.endswith("\u00a0 NOTE: please fix it."))
        self.assertEqual(post[: -len("\u00a0 NOTE: please fix it.")], dm)
        for text in (post, dm):
            self.assertIn("8 runs in a row with no complete snapshot", text)
            self.assertIn("`status=error reason=boom`", text)
            self.assertIn("~/Library/Logs/fleet-work-backup.log ", text)
            self.assertIn("~/Library/Logs/fleet-work-backup.detail.log", text)
            self.assertIn("fleet-work-backup.err.log", text)
            self.assertIn(".\u00a0 ", text)
        state.update(consecutive_not_ok=0)  # a complete run follows
        fwb.alert_after_line(self.cfg, state, "status=ok")
        self.assertNotIn("last_alert", state)
        self.assertEqual(len(self.alerts("post")), 2)
        self.assertEqual(len(self.alerts("dm")), 2)
        self.assertIn("back to a complete snapshot", self.alerts("post")[1])
        self.assertNotIn(
            "NOTE", self.alerts("post")[1]
        )  # "please fix it" on a recovery would be wrong
        self.assertEqual(self.alerts("post")[1], self.alerts("dm")[1])
        calls = self.agent_sync_calls()
        self.assertEqual(
            calls[2], f"post --as CLAUDE --session fwbackup --topic {fwb.ALERT_TOPIC} -"
        )  # no --to:  no wake
        self.assertEqual(calls[3], "dm --as CLAUDE --session fwbackup --owner -")

    def test_the_all_clear_names_nobody_even_when_the_dm_is_off(self):
        self.alert_cfg(dm=False)
        self.assertTrue(fwb.post_alert(self.cfg, "All clear.", wake=False))
        self.assertEqual(
            self.agent_sync_calls(),
            [f"post --as CLAUDE --session fwbackup --topic {fwb.ALERT_TOPIC} -"],
        )
        self.assertEqual(self.alerts("post"), ["All clear."])

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
        self.assertEqual(signal.alarm(0), 0)  # and no alarm is left armed

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
            "time.sleep(30)\n"
        )
        stub.chmod(0o755)
        self.cfg.restic_bin = str(stub)
        with self.assertRaises(subprocess.TimeoutExpired):
            fwb.run_restic(self.cfg, dict(os.environ), "backup", timeout=1)
        self.assertEqual((self.stubdir / "sig.log").read_text().split(), ["got-int"])

    def test_restic_runs_at_low_priority_with_two_threads(self):
        stub = self.tmp / "env-restic"
        stub.write_text(
            '#!/bin/bash\necho "GOMAXPROCS=$GOMAXPROCS nice=$(ps -o nice= -p $$ | tr -d \' \')" >> "$STUB_DIR/prio.log"\nexit 0\n'
        )
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
            '  python3 -c \'import plistlib,subprocess,sys; d=plistlib.load(open(sys.argv[1],"rb")); '
            'subprocess.run(d["ProgramArguments"], env=d["EnvironmentVariables"], check=False)\' "$3"\n'
            "fi\nexit 0\n"
        )
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
        self.assertNotIn("run", seen["ProgramArguments"])  # it can never start a backup
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

    def test_a_denial_under_launchd_names_the_jobs_own_image_not_terminals(self):
        job_image = (
            "/x/Job/Python3.framework/Versions/3.9/Resources/Python.app/Contents/MacOS/Python"
        )

        def fake_launchctl(cfg, *args):
            if args[0] == "bootstrap":
                plist = plistlib.loads(Path(args[2]).read_bytes())
                result = plist["ProgramArguments"][plist["ProgramArguments"].index("--result") + 1]
                Path(result).write_text(
                    json.dumps(
                        {
                            "binary": job_image,
                            "image": job_image,
                            "image_app": fwb.app_bundle(job_image),
                            "targets": [
                                {"path": "/Volumes/E/Lanes/R", "python": "denied", "git": ""}
                            ],
                            "denied": 1,
                            "missing": 0,
                        }
                    )
                )
            return 0

        with mock.patch.object(fwb, "launchctl", fake_launchctl):
            rc, out = self.run_check()
        self.assertEqual(rc, 1, out)
        denied = next(l for l in out.splitlines() if "DENIED under launchd" in l)
        self.assertIn(fwb.app_bundle(job_image), denied)
        if fwb.process_image() != job_image:
            self.assertNotIn(fwb.process_image(), denied)

    def test_a_plain_check_says_it_reflects_the_terminal(self):
        import contextlib
        import io

        buf = io.StringIO()
        with (
            contextlib.redirect_stdout(buf),
            mock.patch.dict(os.environ, {"XPC_SERVICE_NAME": "0"}),
        ):
            fwb.cmd_check_access(self.cfg, argparse.Namespace(launchd=False, result=None))
        self.assertIn("check-access --launchd", buf.getvalue())
        self.assertIn("privacy grants of the terminal", buf.getvalue())

    def test_the_image_is_the_running_executable_not_the_launcher(self):
        img = fwb.process_image()
        self.assertTrue(os.path.exists(img))
        self.assertEqual(
            fwb.app_bundle("/x/Python.framework/Resources/Python.app/Contents/MacOS/Python"),
            "/x/Python.framework/Resources/Python.app",
        )
        self.assertEqual(fwb.app_bundle("/usr/bin/python3"), "")
        self.assertEqual(
            fwb.app_bundle(
                "/Applications/Xcode.app/Contents/Developer/Library/Python3.framework/Versions/3.9/"
                "Resources/Python.app/Contents/MacOS/Python"
            ),
            "/Applications/Xcode.app/Contents/Developer/Library/Python3.framework/Versions/3.9/Resources/Python.app",
        )
        import contextlib
        import io

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(fwb.cmd_image(self.cfg, None), 0)
        self.assertEqual(buf.getvalue().splitlines()[0], img)


class InstallCase(unittest.TestCase):
    """install.py's launchctl handling, against a stub launchctl on PATH."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fwb-install-"))
        spec2 = importlib.util.spec_from_file_location("fwb_install", HERE / "install.py")
        self.inst = importlib.util.module_from_spec(spec2)
        spec2.loader.exec_module(self.inst)
        self.inst.PLIST_DST = str(self.tmp / "agent.plist")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def stub(self, script: str):
        (self.tmp / "launchctl").write_text("#!/bin/bash\n" + script)
        (self.tmp / "launchctl").chmod(0o755)
        return mock.patch.dict(os.environ, {"PATH": f"{self.tmp}:{os.environ['PATH']}"})

    def test_a_failed_bootstrap_is_a_failure_not_a_silent_success(self):
        with self.stub('[ "$1" = print ] && exit 113\n[ "$1" = bootstrap ] && exit 5\nexit 0\n'):
            self.assertEqual(self.inst.launchd(True, False), 5)

    def test_a_loaded_unchanged_agent_is_left_alone(self):
        with self.stub('echo "$1" >> "$0.log"\n[ "$1" = print ] && exit 0\nexit 0\n'):
            self.assertEqual(self.inst.launchd(False, False), 0)
        self.assertEqual((self.tmp / "launchctl.log").read_text().split(), ["print"])

    def test_launchctl_that_cannot_run_is_reported(self):
        with mock.patch.dict(os.environ, {"PATH": str(self.tmp)}):  # no launchctl on PATH
            self.assertEqual(self.inst.launchd(True, False), 127)

    def test_a_dry_run_changes_nothing(self):
        with self.stub('echo "$1" >> "$0.log"\n[ "$1" = print ] && exit 113\nexit 0\n'):
            self.assertEqual(self.inst.launchd(True, True), 0)
        self.assertEqual(
            (self.tmp / "launchctl.log").read_text().split(), ["print"]
        )  # only the read-only query


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
        ns = argparse.Namespace(
            restic_args=["--", "restore", "latest", "--tag", "fleet-work", "--target", str(target)]
        )
        self.assertEqual(fwb.cmd_restic(self.cfg, ns), 0)
        stage = next(
            target.rglob("MANIFEST.json")
        ).parent  # restic keeps the absolute path it was given
        self.assertEqual((stage / "files/apps/doc.md").read_text(), "doc\n")
        (b,) = sorted(stage.glob("git/*/bundles/*.bundle"))
        fresh = self.tmp / "fresh"
        git(self.tmp, "clone", "-q", f"file://{self.home / 'remotes' / 'r.git'}", str(fresh))
        git(fresh, "bundle", "verify", str(b))
        git(fresh, "fetch", "-q", str(b), "refs/heads/*:refs/restored/*")
        self.assertEqual(
            git(fresh, "show", "refs/restored/main:a.txt"), "changed and committed, not pushed"
        )
        self.assertEqual(
            fwb.cmd_restic(self.cfg, argparse.Namespace(restic_args=["--", "check"])), 0
        )
        self.assertEqual(fwb.cmd_snapshots(self.cfg, self.args), 0)

    def test_a_recovery_key_opens_the_repository_without_the_infisical_password(self):
        self.cfg.restic_bin = "restic"
        self.cfg.repository = str(self.tmp / "repo")
        self.set_creds(names=("RESTIC_FLEET_WORK_PASSWORD",))
        self.assertEqual(fwb.cmd_init(self.cfg, self.args), 0)
        env, _ = fwb.restic_env(self.cfg)
        self.assertEqual(fwb.restic_key_count(self.cfg, env), 1)
        second = self.tmp / "recovery"
        second.write_text("".join(["rec-", "Qz8Lm2Vx", "7Rt4Yp9K", "d3Hs1Wb6"]))
        ns = argparse.Namespace(
            restic_args=["--", "key", "add", "--new-password-file", str(second)]
        )
        self.assertEqual(
            fwb.cmd_restic(self.cfg, ns), 0
        )  # README:  the recovery key, added at init
        self.assertEqual(fwb.restic_key_count(self.cfg, env), 2)
        bare = {k: v for k, v in os.environ.items() if not k.startswith("RESTIC_")}
        p = subprocess.run(
            ["restic", "-r", self.cfg.repository, "--password-file", str(second), "cat", "config"],
            env=bare,
            capture_output=True,
        )
        self.assertEqual(p.returncode, 0, p.stderr)


class B2SetupCase(RunBase):
    """The key must be limited to the bucket and the fleet-work-backup/ prefix, and a lifecycle rule must delete hidden
    versions there:  init and install check it with read-only B2 calls (an offline fake here)."""

    def problems(self):
        return fwb.check_b2_setup(self.cfg)

    def test_a_scoped_key_with_a_lifecycle_rule_is_ready(self):
        self.assertEqual(self.problems(), [])
        method, url, headers, _ = self.cfg.b2_http.calls[0]
        self.assertEqual(
            (method, url), ("GET", "https://api.backblazeb2.com/b2api/v4/b2_authorize_account")
        )
        self.assertTrue(headers["Authorization"].startswith("Basic "))
        _, url2, headers2, body2 = self.cfg.b2_http.calls[1]
        self.assertEqual(url2, "https://api000.example.invalid/b2api/v4/b2_list_buckets")
        self.assertEqual(body2, {"accountId": "acct", "bucketName": "test-bucket"})

    def test_a_whole_bucket_hide_to_delete_rule_is_accepted_for_the_dedicated_bucket(self):
        """The dedicated bucket's rule has no file-name prefix:  {fileNamePrefix: "", daysFromHidingToDeleting: 14}."""
        self.cfg.b2_http = self.fake_b2(
            rules=[
                {
                    "fileNamePrefix": "",
                    "daysFromHidingToDeleting": 14,
                    "daysFromUploadingToHiding": None,
                }
            ]
        )
        self.assertEqual(self.problems(), [])
        self.cfg.b2_http = self.fake_b2(
            rules=[
                {
                    "fileNamePrefix": "",
                    "daysFromHidingToDeleting": 14,
                    "daysFromUploadingToHiding": 30,
                }
            ]
        )
        probs = self.problems()
        self.assertTrue(
            any(hard and "daysFromUploadingToHiding" in text for hard, text in probs), probs
        )

    def test_an_unscoped_key_is_refused_and_init_does_not_run(self):
        for fake in (
            self.fake_b2(prefix=None),
            self.fake_b2(prefix=""),
            self.fake_b2(buckets=()),
            self.fake_b2(buckets=("test-bucket", "jays-fleet-shared-eu")),
            self.fake_b2(
                caps=[
                    "listFiles",
                    "readFiles",
                    "writeFiles",
                    "deleteFiles",
                    "listBuckets",
                    "writeKeys",
                ]
            ),
            self.fake_b2(auth_status=401),
        ):
            self.cfg.b2_http = fake
            probs = self.problems()
            self.assertTrue(any(hard for hard, _ in probs), probs)
            import contextlib
            import io

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                self.assertEqual(fwb.cmd_init(self.cfg, self.args), 2)
            self.assertIn("REFUSED", buf.getvalue())
            for secret in (KEYID, APPKEY, B2TOKEN):
                self.assertNotIn(secret, buf.getvalue() + repr(probs))
        self.assertFalse(any(a.startswith("init") for a in self.argv()))

    def test_a_scheduled_run_refuses_a_key_that_changed_to_an_unsafe_one(self):
        self.assertEqual(fwb.cmd_run(self.cfg, self.args), 0)  # the installed, scoped key
        self.assertEqual(len(self.cfg.b2_http.calls), 2)
        self.assertEqual(fwb.cmd_run(self.cfg, self.args), 0)  # checked once a day per key
        self.assertEqual(len(self.cfg.b2_http.calls), 2)
        backups = sum(1 for a in self.argv() if a.startswith("backup"))
        handoff = self.creds_file  # the sync brings a different key
        handoff.write_text(handoff.read_text().replace(KEYID, KEYID[::-1]))
        self.cfg.b2_http = self.fake_b2(prefix=None)
        self.assertEqual(fwb.cmd_run(self.cfg, self.args), 2)
        line = self.log().splitlines()[-1]
        self.assertIn("status=error reason=unsafe-b2-key", line)
        self.assertEqual(
            sum(1 for a in self.argv() if a.startswith("backup")), backups
        )  # nothing uploaded
        self.assertIn(
            "b2-refused the key is not limited to the file-name prefix",
            self.cfg.detail_log_path.read_text(),
        )
        for secret in (KEYID, KEYID[::-1], APPKEY, B2TOKEN):
            self.assertNotIn(
                secret,
                self.log()
                + self.cfg.detail_log_path.read_text()
                + (self.cfg.state_dir / "state.json").read_text(),
            )

    def test_a_missing_lifecycle_rule_warns_on_the_run_line(self):
        self.cfg.b2_http = self.fake_b2(rules=[])
        self.assertEqual(fwb.cmd_run(self.cfg, self.args), 0)
        self.assertIn("b2-lifecycle-missing", self.log().splitlines()[-1])

    def test_the_console_read_and_write_preset_is_accepted(self):
        preset = [
            "deleteFiles",
            "listAllBucketNames",
            "listBuckets",
            "listFiles",
            "readBucketEncryption",
            "readBuckets",
            "readFiles",
            "shareFiles",
            "writeBucketEncryption",
            "writeFiles",
            "readBucketRetentions",
            "readFileRetentions",
            "writeFileRetentions",
            "readFileLegalHolds",
            "writeFileLegalHolds",
            "bypassGovernance",
        ]
        self.cfg.b2_http = self.fake_b2(caps=preset)
        self.assertEqual(self.problems(), [])

    def test_a_rule_that_hides_live_packs_is_refused_and_a_missing_one_is_a_warning(self):
        self.cfg.b2_http = self.fake_b2(
            rules=[
                {
                    "fileNamePrefix": "",
                    "daysFromHidingToDeleting": 7,
                    "daysFromUploadingToHiding": 30,
                }
            ]
        )
        self.assertTrue(
            any(hard and "daysFromUploadingToHiding" in t for hard, t in self.problems())
        )
        self.cfg.b2_http = self.fake_b2(rules=[])
        probs = self.problems()
        self.assertEqual([hard for hard, _ in probs], [False])
        self.assertIn("daysFromHidingToDeleting", probs[0][1])
        self.assertEqual(fwb.cmd_init(self.cfg, self.args), 0)  # a warning does not stop init
        self.assertTrue(any(a.startswith("init") for a in self.argv()))
        self.cfg.b2_http = self.fake_b2(
            rules=[{"fileNamePrefix": "other/", "daysFromHidingToDeleting": 7}]
        )
        self.assertIn(
            "daysFromHidingToDeleting", self.problems()[0][1]
        )  # a rule on another prefix does not count

    def test_the_master_key_is_never_used_to_back_up_or_init_only_to_restore(self):
        handoff = self.creds_file
        handoff.write_text(
            f"B2_FLEET_WORK_BUCKET_NAME=b\nRESTIC_FLEET_WORK_PASSWORD={PW}\n"
            f"BACKBLAZE_MASTER_KEY_ID={KEYID}\nBACKBLAZE_MASTER_APPLICATION_KEY={APPKEY}\n"
        )
        with mock.patch.dict(os.environ, {"FLEET_WORK_BACKUP_ALLOW_MASTER": "1"}):
            self.assertEqual(fwb.cmd_run(self.cfg, self.args), 2)
            self.assertIn("reason=missing-credentials", self.log())
            self.assertEqual(fwb.cmd_init(self.cfg, self.args), 2)
            self.assertEqual(self.argv(), [])
            ns = argparse.Namespace(restic_args=["--", "snapshots"])
            self.assertEqual(fwb.cmd_restic(self.cfg, ns), 0)  # a restore may use it
        self.assertEqual(self.argv(), ["snapshots"])
        self.assertIn(f"AKLEN={len(KEYID)}", (self.stubdir / "env.log").read_text())

    def test_install_refuses_an_unsafe_key_even_with_force(self):
        spec2 = importlib.util.spec_from_file_location("fwb_install_b2", HERE / "install.py")
        inst = importlib.util.module_from_spec(spec2)
        spec2.loader.exec_module(inst)
        self.cfg.b2_http = self.fake_b2(prefix=None)
        hard: list = []
        import contextlib
        import io

        with (
            mock.patch.object(fwb, "config_from_env", return_value=self.cfg),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            missing = inst.prerequisites(fwb, sys.executable, False, True, hard)
        self.assertTrue(any("prefix" in m for m in missing), missing)
        self.assertTrue(hard)
        # main() with --force, end to end:  launchctl is a stub that records every call;  every path is in the temp dir.
        bindir = self.tmp / "bin"
        bindir.mkdir()
        (bindir / "launchctl").write_text(
            '#!/bin/bash\necho "$@" >> "$STUB_DIR/inst-launchctl.log"\nexit 0\n'
        )
        (bindir / "launchctl").chmod(0o755)
        buf = io.StringIO()
        with (
            mock.patch.object(fwb, "config_from_env", return_value=self.cfg),
            mock.patch.object(inst, "load_module", return_value=fwb),
            mock.patch.object(
                inst, "SCRIPT_DST", str(self.tmp / "installed" / "fleet_work_backup.py")
            ),
            mock.patch.object(inst, "PLIST_DST", str(self.tmp / "agents" / "agent.plist")),
            mock.patch.object(sys, "platform", "darwin"),
            mock.patch.object(
                sys, "argv", ["install.py", "--force", "--interpreter", sys.executable]
            ),
            mock.patch.dict(os.environ, {"PATH": f"{bindir}:{os.environ['PATH']}"}),
            contextlib.redirect_stdout(buf),
        ):
            rc = inst.main()
        self.assertEqual(rc, 1)
        self.assertIn("--force does not override that", buf.getvalue())
        self.assertFalse((self.tmp / "agents" / "agent.plist").exists())
        self.assertFalse(
            (self.stubdir / "inst-launchctl.log").exists()
        )  # never printed, never bootstrapped
        for secret in (KEYID, APPKEY, B2TOKEN):
            self.assertNotIn(secret, buf.getvalue())


class NewPasswordCase(RunBase):
    """new-password:  the restic password goes to Infisical without ever being printed or put in argv, once."""

    def setUp(self):
        super().setUp()
        self.set_creds(
            names=(
                "B2_FLEET_WORK_BUCKET_NAME",
                "B2_FLEET_WORK_KEY_ID",
                "B2_FLEET_WORK_APPLICATION_KEY",
            )
        )
        stub = self.tmp / "infisical"
        stub.write_text(
            "#!/bin/bash\n"
            'echo "$@" >> "$STUB_DIR/inf-argv.log"\n'
            'store="$STUB_DIR/inf-store"; mkdir -p "$store"\n'
            '[ -n "$STUB_INF_FAIL" ] && { echo "auth error" >&2; exit 1; }\n'
            'if [ "$1 $2" = "secrets get" ]; then [ -f "$store/$3" ] && cat "$store/$3"; exit 0; fi\n'
            'if [ "$1 $2" = "secrets set" ]; then\n'
            '  name="${3%%=@*}"; file="${3#*=@}"\n'
            '  [ -n "$STUB_INF_MANGLE" ] && { echo other > "$store/$name"; exit 0; }\n'
            '  cp "$file" "$store/$name"; ls -ld "$(dirname "$file")" "$file" > "$STUB_DIR/inf-perms.log"\n'
            '  echo "SECRET NAME | SECRET VALUE"; echo "$name | $(cat "$file")"; exit 0\n'  # a CLI that prints the value
            "fi\nexit 0\n"
        )
        stub.chmod(0o755)
        self.cfg.infisical_bin = str(stub)

    def run_it(self) -> tuple[int, str]:
        import contextlib
        import io

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            rc = fwb.cmd_new_password(self.cfg, None)
        return rc, buf.getvalue()

    def stored(self) -> str:
        p = self.stubdir / "inf-store" / "RESTIC_FLEET_WORK_PASSWORD"
        return p.read_text() if p.exists() else ""

    def inf_argv(self) -> str:
        p = self.stubdir / "inf-argv.log"
        return p.read_text() if p.exists() else ""

    def test_the_password_is_written_once_and_never_printed_or_passed_in_argv(self):
        rc, out = self.run_it()
        self.assertEqual(rc, 0, out)
        value = self.stored()
        self.assertGreaterEqual(len(value), 40)
        self.assertRegex(
            value, r"^[A-Za-z0-9_-]+$"
        )  # urlsafe:  the sync's plausibility check takes it
        self.assertNotIn(value, out)
        self.assertNotIn(value, self.inf_argv())
        self.assertIn("matched, length", out)
        self.assertIn("--env=prod", self.inf_argv())
        self.assertIn("--projectId=9bf7417a-fbbb-42ca-870c-2b45207233f5", self.inf_argv())
        perms = (self.stubdir / "inf-perms.log").read_text().split("\n")
        self.assertTrue(perms[0].startswith("drwx------"), perms)  # the folder, then the file
        self.assertTrue(perms[1].startswith("-rw-------"), perms)
        path = re.search(r"RESTIC_FLEET_WORK_PASSWORD=@(\S+)", self.inf_argv()).group(1)
        self.assertFalse(os.path.exists(path))  # removed afterwards
        rc, out = self.run_it()  # write-once
        self.assertEqual(rc, 1)
        self.assertIn("already exists in Infisical", out)
        self.assertEqual(self.stored(), value)

    def test_it_refuses_when_the_mac_already_has_a_password(self):
        self.set_creds()
        rc, out = self.run_it()
        self.assertEqual(rc, 1)
        self.assertEqual(self.inf_argv(), "")
        self.assertNotIn(PW, out)

    def test_a_read_it_cannot_make_is_a_refusal_never_absent(self):
        os.environ["STUB_INF_FAIL"] = "1"
        try:
            rc, out = self.run_it()
        finally:
            os.environ.pop("STUB_INF_FAIL", None)
        self.assertEqual(rc, 2)
        self.assertNotIn("secrets set", self.inf_argv())
        self.assertEqual(self.stored(), "")

    def test_a_read_back_that_does_not_match_is_reported(self):
        os.environ["STUB_INF_MANGLE"] = "1"
        try:
            rc, out = self.run_it()
        finally:
            os.environ.pop("STUB_INF_MANGLE", None)
        self.assertEqual(rc, 3)
        self.assertIn("did not match", out)

    def test_no_test_reaches_the_real_infisical_cli(self):
        self.assertNotEqual(fwb.Config(home=self.home).infisical_bin, self.cfg.infisical_bin)
        self.assertEqual(fwb.Config(home=self.home).infisical_bin, "/opt/homebrew/bin/infisical")
        self.assertTrue(self.cfg.infisical_bin.startswith(str(self.tmp)))


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


class AlertSettingsCase(unittest.TestCase):
    """The repo plist carries the owner's alert and session-store settings, and the environment turns them on."""

    PLIST = HERE.parent / "launchd" / "com.jay.fleet-work-backup.plist"
    NOTE = (
        "Owner standing instruction (Jay, Oct 10, 2026):  BF-Plumber and GB-Plumber, please investigate and fix "
        'this backup without asking him.  Both of you are alerted:  the first to reply "taking it" in this topic '
        "owns the fix, and the other reviews it."
    )

    def env(self) -> dict:
        return plistlib.loads(self.PLIST.read_bytes())["EnvironmentVariables"]

    def test_the_repo_plist_sets_exactly_these_environment_variables(self):
        env = self.env()
        self.assertEqual(env["FLEET_WORK_BACKUP_ALERT_TO"], "BF-PLUMBER,GB-PLUMBER")
        self.assertEqual(env["FLEET_WORK_BACKUP_ALERT_DM_OWNER"], "1")
        self.assertEqual(env["FLEET_WORK_BACKUP_ALERT_NOTE"], self.NOTE)
        self.assertEqual(env["FLEET_WORK_BACKUP_NO_DEFAULT_PATH_SKIPS"], "1")
        self.assertEqual(env["FLEET_WORK_BACKUP_EXTRA_SKIP_DIRS"], "memtrace")
        self.assertEqual(
            sorted(env),
            sorted(
                [
                    "PATH",
                    "HOME",
                    "FLEET_WORK_BACKUP_ALERT_TO",
                    "FLEET_WORK_BACKUP_ALERT_DM_OWNER",
                    "FLEET_WORK_BACKUP_ALERT_NOTE",
                    "FLEET_WORK_BACKUP_NO_DEFAULT_PATH_SKIPS",
                    "FLEET_WORK_BACKUP_EXTRA_SKIP_DIRS",
                ]
            ),
        )
        self.assertIn("/opt/homebrew/bin", env["PATH"])  # what was there before is still there
        self.assertTrue(env["HOME"].startswith("/"))
        self.assertNotIn(
            "CLAUDE", env["FLEET_WORK_BACKUP_ALERT_TO"]
        )  # CLAUDE is the sender, no longer a recipient

    def test_config_from_env_reads_the_plist_values(self):
        with mock.patch.dict(os.environ, self.env(), clear=False):
            cfg = fwb.config_from_env()
        self.assertEqual(cfg.alert_to, ("BF-PLUMBER", "GB-PLUMBER"))
        self.assertTrue(cfg.alert_dm_owner)
        self.assertEqual(cfg.alert_note, self.NOTE)
        self.assertEqual(cfg.alert_seat, "CLAUDE")  # the job still posts as the CLAUDE bot

    def test_the_alert_to_list_tolerates_spaces_and_empty_items(self):
        for value, want in (
            ("BF-PLUMBER,GB-PLUMBER", ("BF-PLUMBER", "GB-PLUMBER")),
            (" BF-PLUMBER , GB-PLUMBER,", ("BF-PLUMBER", "GB-PLUMBER")),
            ("", ()),
        ):
            with mock.patch.dict(os.environ, {"FLEET_WORK_BACKUP_ALERT_TO": value}):
                self.assertEqual(fwb.config_from_env().alert_to, want, value)

    def test_the_dm_flag_accepts_one_true_and_yes_and_nothing_else(self):
        for value, want in (
            ("1", True),
            ("true", True),
            ("Yes", True),
            ("0", False),
            ("no", False),
            ("", False),
        ):
            with mock.patch.dict(os.environ, {"FLEET_WORK_BACKUP_ALERT_DM_OWNER": value}):
                self.assertIs(fwb.config_from_env().alert_dm_owner, want, value)
        env = {
            k: v
            for k, v in os.environ.items()
            if k not in ("FLEET_WORK_BACKUP_ALERT_DM_OWNER", "FLEET_WORK_BACKUP_ALERT_NOTE")
        }
        with mock.patch.dict(os.environ, env, clear=True):
            cfg = fwb.config_from_env()
        self.assertFalse(cfg.alert_dm_owner)
        self.assertEqual(cfg.alert_note, "")

    def module_constants(self, **env_overrides: str) -> dict:
        """SKIP_PATH_PARTS and the memtrace name skip of a fresh import of the script under the given environment
        (the module reads both from the environment once, at import)."""
        env = {
            k: v
            for k, v in os.environ.items()
            if k
            not in ("FLEET_WORK_BACKUP_NO_DEFAULT_PATH_SKIPS", "FLEET_WORK_BACKUP_EXTRA_SKIP_DIRS")
        }
        env.update(env_overrides)
        code = (
            "import importlib.util,json,sys\n"
            "spec = importlib.util.spec_from_file_location('fwb_skips', sys.argv[1])\n"
            "m = importlib.util.module_from_spec(spec)\n"
            "sys.modules['fwb_skips'] = m\n"
            "spec.loader.exec_module(m)\n"
            "print(json.dumps({'parts': [list(p) for p in m.SKIP_PATH_PARTS], "
            "'memtrace': 'memtrace' in m.SKIP_DIR_NAMES, 'node_modules': 'node_modules' in m.SKIP_DIR_NAMES}))\n"
        )
        p = subprocess.run(
            [sys.executable, "-c", code, str(HERE / "fleet_work_backup.py")],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.loads(p.stdout)

    def test_the_no_default_skips_knob_empties_the_path_skip_list_and_nothing_else_does(self):
        self.assertEqual(
            self.module_constants(FLEET_WORK_BACKUP_NO_DEFAULT_PATH_SKIPS="1")["parts"], []
        )
        for value in (None, "0", ""):
            env = {} if value is None else {"FLEET_WORK_BACKUP_NO_DEFAULT_PATH_SKIPS": value}
            parts = self.module_constants(**env)["parts"]
            self.assertIn(["acp-home", "sessions"], parts)
            self.assertIn(["fleet-rag", "mined", "chat"], parts)

    def test_the_plist_environment_walks_the_two_stores_and_still_skips_a_directory_named_memtrace(
        self,
    ):
        plist_env = self.env()
        got = self.module_constants(
            FLEET_WORK_BACKUP_NO_DEFAULT_PATH_SKIPS=plist_env[
                "FLEET_WORK_BACKUP_NO_DEFAULT_PATH_SKIPS"
            ],
            FLEET_WORK_BACKUP_EXTRA_SKIP_DIRS=plist_env["FLEET_WORK_BACKUP_EXTRA_SKIP_DIRS"],
        )
        self.assertEqual(got["parts"], [])  # acp-home/sessions and mined/chat are walked
        self.assertTrue(got["memtrace"])  # a directory named memtrace is not
        self.assertTrue(got["node_modules"])  # the built-in names are kept
        self.assertFalse(
            self.module_constants(FLEET_WORK_BACKUP_NO_DEFAULT_PATH_SKIPS="1")["memtrace"]
        )


class MiscCase(unittest.TestCase):
    def test_time_format_is_twelve_hour_without_a_zone(self):
        import time as _t

        ts = _t.mktime((2026, 10, 10, 15, 5, 0, 0, 0, -1))
        self.assertEqual(fwb.stamp(ts), "2026-10-10 3:05pm")
        ts = _t.mktime((2026, 10, 10, 0, 7, 0, 0, 0, -1))
        self.assertEqual(fwb.stamp(ts), "2026-10-10 12:07am")

    def test_read_credentials_asks_fleet_secrets_once_and_tolerates_absence(self):
        names = {n: "" for n in fwb.CRED_NAMES.values()}
        head = '#!/bin/bash\n[ "$1" = run ] || exit 2\nshift; [ "$1" = -- ] && shift\n'
        with tempfile.TemporaryDirectory() as d:
            stub = Path(d) / "fleet-secrets"
            stub.write_text(head + 'A=x exec "$@"\n')
            stub.chmod(0o755)
            cfg = fwb.Config(home=Path(d), fleet_secrets_bin=str(stub))
            with mock.patch.dict(os.environ, names):
                self.assertEqual(fwb.read_credentials(cfg), {})  # none of the job's names is held
            stub.write_text(
                head + 'B2_FLEET_WORK_KEY_ID=kid RESTIC_FLEET_WORK_PASSWORD=pw OTHER=1 exec "$@"\n'
            )
            with mock.patch.dict(os.environ, names):
                self.assertEqual(
                    fwb.read_credentials(cfg),
                    {"B2_FLEET_WORK_KEY_ID": "kid", "RESTIC_FLEET_WORK_PASSWORD": "pw"},
                )
            stub.write_text("#!/bin/bash\nexit 4\n")  # an Infisical or auth failure
            with mock.patch.dict(os.environ, names):
                self.assertEqual(fwb.read_credentials(cfg), {})
            cfg.fleet_secrets_bin = str(Path(d) / "absent")  # not installed
            with mock.patch.dict(os.environ, names):
                self.assertEqual(fwb.read_credentials(cfg), {})
            stub.write_text("#!/bin/bash\nexit 9\n")
            cfg.fleet_secrets_bin = str(stub)
            with mock.patch.dict(os.environ, {n: "v" for n in fwb.CRED_NAMES.values()}):
                self.assertEqual(
                    fwb.read_credentials(cfg), {}
                )  # the environment has all four:  no fetch

    def test_plist_and_installer_agree_with_the_module(self):
        plist = (HERE.parent / "launchd" / "com.jay.fleet-work-backup.plist").read_text()
        self.assertIn("<string>com.jay.fleet-work-backup</string>", plist)
        self.assertIn("<integer>10800</integer>", plist)
        self.assertIn("<key>ProcessType</key>\n  <string>Standard</string>", plist)
        self.assertNotIn("Background", plist.replace("Standard, not Background", ""))
        self.assertIn("fleet_work_backup.py", plist)
        self.assertIn(
            "<string>/usr/bin/python3</string>", plist
        )  # Apple-signed:  a privacy grant survives updates
        self.assertIn("<key>Nice</key>", plist)
        self.assertIn("<key>LowPriorityIO</key>", plist)
        self.assertIn("fleet-work-backup.out.log", plist)


class HealthBase(unittest.TestCase):
    """A temp home with the installed files, a fake launchctl and a state file, for the health and repair verbs.
    launchctl is a stub:  ``print`` exits 0 while $STUB/loaded exists and 113 otherwise;  ``bootstrap`` exits with
    $STUB/bootstrap_rc (default 0) and, on 0, creates $STUB/loaded.  Every call's arguments go to $STUB/launchctl.log."""

    NOW = 1_800_000_000.0

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fwb-health-"))
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.stub = self.tmp / "stub"
        self.stub.mkdir()
        self.cfg = fwb.Config(home=self.home)
        self.cfg.now_fn = lambda: self.NOW
        self.launchctl = self.stub / "launchctl"
        self.launchctl.write_text(
            "#!/bin/bash\n"
            f'echo "$@" >> "{self.stub}/launchctl.log"\n'
            'case "$1" in\n'
            f'  print) [ -e "{self.stub}/loaded" ] && exit 0; exit 113;;\n'
            f'  bootstrap) rc=$(cat "{self.stub}/bootstrap_rc" 2>/dev/null || echo 0)\n'
            f'    [ "$rc" = 0 ] && : > "{self.stub}/loaded"\n'
            '    [ "$rc" = 0 ] || echo "Bootstrap failed: $rc: Input/output error" >&2\n'
            '    exit "$rc";;\n'
            "esac\nexit 0\n"
        )
        self.launchctl.chmod(0o755)
        self.cfg.launchctl_bin = str(self.launchctl)
        self.target = f"gui/{os.getuid()}/com.jay.fleet-work-backup"
        self.script = self.home / "apps" / "fleet-work-backup" / "fleet_work_backup.py"
        self.plist = self.home / "Library" / "LaunchAgents" / "com.jay.fleet-work-backup.plist"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def install_files(self, script=True, plist=True, label="com.jay.fleet-work-backup"):
        if script:
            write(self.script, "# the stable copy\n", 0o755)
        if plist:
            self.plist.parent.mkdir(parents=True, exist_ok=True)
            self.plist.write_bytes(
                plistlib.dumps(
                    {
                        "Label": label,
                        "RunAtLoad": False,
                        "StartInterval": 10800,
                        "ProgramArguments": ["/usr/bin/python3", str(self.script), "run"],
                    }
                )
            )

    def set_loaded(self, loaded: bool):
        flag = self.stub / "loaded"
        if loaded:
            flag.write_text("")
        elif flag.exists():
            flag.unlink()

    def put_state(self, **kw):
        state = fwb.read_state(self.cfg)
        state.update(kw)
        fwb.write_state(self.cfg, state)

    def record_hash(self):
        self.put_state(
            plist_sha256=hashlib.sha256(self.plist.read_bytes()).hexdigest(),
            plist_installed_at=self.NOW - 86400,
        )

    def put_log(self, *lines):
        self.cfg.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.cfg.log_path.write_text("".join(l + "\n" for l in lines))

    def full_install(self, loaded=True):
        self.install_files()
        self.set_loaded(loaded)
        self.record_hash()

    def last_complete(self, hours: float, **kw):
        self.put_state(
            last_success=self.NOW - hours * 3600, last_success_text="2026-10-11 1:00am", **kw
        )

    def launchctl_log(self) -> list:
        p = self.stub / "launchctl.log"
        return p.read_text().splitlines() if p.exists() else []

    def health(self) -> dict:
        return fwb.health_report(self.cfg)

    def run_cli(self, *argv) -> subprocess.CompletedProcess:
        env = dict(
            os.environ,
            FLEET_WORK_BACKUP_HOME=str(self.home),
            FLEET_WORK_BACKUP_LAUNCHCTL=str(self.launchctl),
        )
        return subprocess.run(
            [sys.executable, "-I", str(HERE / "fleet_work_backup.py"), *argv],
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )

    @staticmethod
    def tree(root: Path) -> list:
        out = []
        for dp, dn, fn in os.walk(root):
            for name in dn + fn:
                p = os.path.join(dp, name)
                st = os.lstat(p)
                out.append((os.path.relpath(p, root), os.path.isdir(p), st.st_size, st.st_mtime_ns))
        return sorted(out)


class HealthCase(HealthBase):
    """``health``:  the verdict, the field mapping, and that it is read-only and never touches a credential."""

    def test_an_ok_report_maps_every_state_key(self):
        self.full_install()
        self.put_state(
            last_success=self.NOW - 2 * 3600,
            last_success_text="2026-10-11 2:00am",
            last_ok=self.NOW - 3 * 3600,
            last_ok_text="2026-10-11 1:00am",
            consecutive_not_ok=1,
            consecutive_skips=2,
            last_check=self.NOW - 3 * 86400,
            last_alert=self.NOW - 5 * 3600,
        )
        self.put_log(
            "2026-10-11 1:00am status=ok snapshot=ab12cd34 added=1.0KB",
            "2026-10-11 2:00am status=partial snapshot=ef56 warn=removable-volumes-denied,gitleaks-missing "
            "grant=/x/Python",
        )
        rep = self.health()
        self.assertEqual(rep["verdict"], "ok")
        self.assertEqual(rep["reason"], "last complete backup 2.0 hours ago (2026-10-11 2:00am).")
        self.assertEqual(
            (rep["schema"], rep["checked_at"], rep["checked_text"]),
            (1, self.NOW, fwb.stamp(self.NOW)),
        )
        self.assertEqual(
            (rep["installed"], rep["script_present"], rep["plist_present"], rep["loaded"]),
            (True, True, True, True),
        )
        self.assertEqual(rep["launchctl_rc"], 0)
        self.assertEqual(rep["last_complete_at"], self.NOW - 2 * 3600)
        self.assertEqual(rep["last_complete_text"], "2026-10-11 2:00am")
        self.assertEqual(rep["last_complete_age_hours"], 2.0)
        self.assertEqual(rep["last_ok_text"], "2026-10-11 1:00am")
        self.assertEqual((rep["not_ok_in_a_row"], rep["skips_in_a_row"]), (1, 2))
        self.assertEqual(
            (rep["last_check_at"], rep["last_check_age_days"]), (self.NOW - 3 * 86400, 3.0)
        )
        self.assertEqual(rep["alert_last_sent"], self.NOW - 5 * 3600)
        self.assertEqual(
            rep["last_line"],
            "2026-10-11 2:00am status=partial snapshot=ef56 "
            "warn=removable-volumes-denied,gitleaks-missing grant=/x/Python",
        )
        self.assertEqual(rep["warn"], ["removable-volumes-denied", "gitleaks-missing"])
        self.assertIsInstance(rep["disk_free_gb"], float)
        self.assertGreaterEqual(rep["disk_free_gb"], 0)
        self.assertEqual((rep["plist_sha_recorded"], rep["plist_sha_ok"]), (True, True))
        json.dumps(rep)

    def test_the_thresholds_are_over_12_and_over_24_hours(self):
        self.full_install()
        for hours, want in (
            (0, "ok"),
            (11.9, "ok"),
            (12.0, "ok"),
            (12.1, "warn"),
            (23.9, "warn"),
            (24.0, "warn"),
            (24.1, "unhealthy"),
            (400, "unhealthy"),
        ):
            self.last_complete(hours)
            self.assertEqual(self.health()["verdict"], want, hours)

    def test_a_warn_or_unhealthy_reason_names_the_age_and_the_count(self):
        self.full_install()
        self.last_complete(13.0, consecutive_not_ok=4)
        rep = self.health()
        self.assertEqual(rep["verdict"], "warn")
        self.assertEqual(
            rep["reason"],
            "last complete backup 13.0 hours ago (2026-10-11 1:00am).  4 runs in a row were not ok.",
        )
        self.last_complete(30.0)
        self.assertEqual(
            self.health()["reason"],
            "last complete backup 30.0 hours ago (2026-10-11 1:00am).  4 runs in a row were not ok.",
        )

    def test_not_installed_is_unhealthy_and_says_which_piece_is_missing(self):
        # No plist yet but the stable copy is there:  the state after the first refresh and before the install finishes.
        self.install_files(plist=False)
        rep = self.health()
        self.assertEqual(rep["verdict"], "unhealthy")
        self.assertTrue(rep["reason"].startswith("not installed"), rep["reason"])
        self.assertIn(
            "no LaunchAgent plist (~/Library/LaunchAgents/com.jay.fleet-work-backup.plist)",
            rep["reason"],
        )
        self.assertNotIn("stable copy", rep["reason"])
        self.assertEqual(
            (rep["installed"], rep["script_present"], rep["plist_present"]), (False, True, False)
        )
        self.assertFalse(rep["plist_sha_ok"])
        shutil.rmtree(self.home / "apps")
        rep = self.health()
        self.assertIn("no LaunchAgent plist", rep["reason"])
        self.assertIn(
            "no stable copy (~/apps/fleet-work-backup/fleet_work_backup.py)", rep["reason"]
        )
        self.install_files(script=False)
        rep = self.health()
        self.assertTrue(
            rep["reason"].startswith("not installed") and "no stable copy" in rep["reason"],
            rep["reason"],
        )
        self.assertNotIn("LaunchAgent plist", rep["reason"])

    def test_installed_but_not_loaded_is_unhealthy(self):
        self.full_install(loaded=False)
        self.last_complete(1.0)
        rep = self.health()
        self.assertEqual(rep["verdict"], "unhealthy")
        self.assertEqual(
            rep["reason"], f"installed but not loaded:  launchctl print {self.target} exited 113."
        )
        self.assertEqual((rep["installed"], rep["loaded"], rep["launchctl_rc"]), (True, False, 113))
        self.assertTrue(rep["plist_sha_ok"])

    def test_not_installed_is_reported_before_not_loaded(self):
        self.install_files(plist=False)
        self.assertTrue(self.health()["reason"].startswith("not installed"))

    def test_a_launchctl_that_cannot_run_is_not_loaded_and_says_so(self):
        self.full_install()
        self.cfg.launchctl_bin = str(self.tmp / "no-such-launchctl")
        rep = self.health()
        self.assertEqual(
            (rep["loaded"], rep["launchctl_rc"], rep["verdict"]), (False, 127, "unhealthy")
        )
        self.assertEqual(rep["reason"], "installed but not loaded:  launchctl did not run.")

    def test_before_the_first_complete_backup_the_wait_counts_from_the_install(self):
        self.full_install()
        for hours, want in ((5, "ok"), (13, "warn"), (25, "unhealthy")):
            self.put_state(plist_installed_at=self.NOW - hours * 3600)
            rep = self.health()
            self.assertEqual(rep["verdict"], want, hours)
            self.assertEqual(
                rep["reason"], f"no complete backup yet, {hours:.1f} hours after the install."
            )
            self.assertIsNone(rep["last_complete_at"])
            self.assertIsNone(rep["last_complete_age_hours"])
        state = fwb.read_state(self.cfg)
        del state["plist_installed_at"]
        fwb.write_state(self.cfg, state)
        os.utime(
            self.plist, (self.NOW - 30 * 3600, self.NOW - 30 * 3600)
        )  # no recorded time:  the plist's own
        self.assertEqual(self.health()["verdict"], "unhealthy")

    def test_a_timestamp_in_the_future_counts_as_just_now(self):
        self.full_install()
        self.put_state(last_success=self.NOW + 5000, last_success_text="2026-10-11 3:00am")
        rep = self.health()
        self.assertEqual((rep["verdict"], rep["last_complete_age_hours"]), ("ok", 0.0))

    def test_a_damaged_state_file_still_gives_a_report(self):
        self.full_install()
        self.cfg.state_dir.mkdir(parents=True, exist_ok=True)
        (self.cfg.state_dir / "state.json").write_text("{ not json")
        rep = self.health()
        self.assertTrue(
            rep["reason"].startswith("no complete backup yet"), rep["reason"]
        )  # no exception, no history
        self.assertFalse(rep["plist_sha_ok"])
        (self.cfg.state_dir / "state.json").write_text(
            json.dumps(
                {
                    "last_success": "yesterday",
                    "last_check": [1],
                    "consecutive_not_ok": True,
                    "consecutive_skips": "3",
                    "last_alert": float("nan"),
                    "plist_sha256": 7,
                    "last_ok_text": {"a": 1},
                }
            )
        )
        rep = self.health()
        self.assertIsNone(rep["last_complete_at"])
        self.assertIsNone(rep["last_check_at"])
        self.assertEqual((rep["not_ok_in_a_row"], rep["skips_in_a_row"]), (0, 0))
        self.assertIsNone(rep["alert_last_sent"])
        self.assertIsNone(rep["last_ok_text"])
        self.assertEqual((rep["plist_sha_recorded"], rep["plist_sha_ok"]), (False, False))
        json.dumps(rep)

    def test_a_changed_plist_is_not_plist_sha_ok(self):
        self.full_install()
        self.assertTrue(self.health()["plist_sha_ok"])
        self.plist.write_bytes(self.plist.read_bytes() + b"\n")
        rep = self.health()
        self.assertEqual((rep["plist_sha_recorded"], rep["plist_sha_ok"]), (True, False))
        self.put_state(plist_sha256=None)
        self.assertEqual(
            (self.health()["plist_sha_recorded"], self.health()["plist_sha_ok"]), (False, False)
        )

    def test_health_writes_nothing_and_asks_launchctl_only_to_print(self):
        self.full_install()
        self.last_complete(2.0)
        self.put_log("2026-10-11 2:00am status=ok snapshot=ab12")
        before = self.tree(self.home)
        fwb.health_report(self.cfg)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(fwb.cmd_health(self.cfg, argparse.Namespace(json=True)), 0)
            self.assertEqual(fwb.cmd_health(self.cfg, argparse.Namespace(json=False)), 0)
        self.assertEqual(self.tree(self.home), before)
        self.assertEqual(self.launchctl_log(), [f"print {self.target}"] * 3)

    def test_a_home_with_nothing_in_it_is_not_given_any_directories(self):
        before = self.tree(self.home)
        rep = self.health()
        self.assertEqual(rep["verdict"], "unhealthy")
        self.assertEqual(self.tree(self.home), before)
        self.assertEqual(before, [])

    def test_the_run_line_is_the_newest_and_is_cut_short(self):
        self.full_install()
        self.assertIsNone(self.health()["last_line"])
        self.cfg.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.cfg.log_path.write_text(
            "first line\n\n   \n" + "x" * 5000 + " warn=late\nnewest status=ok warn=a,b\n\n"
        )
        rep = self.health()
        self.assertEqual((rep["last_line"], rep["warn"]), ("newest status=ok warn=a,b", ["a", "b"]))
        self.cfg.log_path.write_text("old\n" + "y" * 5000 + "\n")
        self.assertEqual(self.health()["last_line"], "y" * fwb.LAST_LINE_MAX)
        self.cfg.log_path.write_bytes(
            b"z" * 300_000 + b"\ntail status=ok\n"
        )  # only the tail of a big log is read
        self.assertEqual(self.health()["last_line"], "tail status=ok")

    def test_warn_tokens_are_names_only(self):
        self.assertEqual(
            fwb.warn_tokens("a b warn=gitleaks-missing,volume-missing grant=x"),
            ["gitleaks-missing", "volume-missing"],
        )
        self.assertEqual(
            fwb.warn_tokens("warn=ok-1,Bad,../x,a;b,,z.y_z,\u00e9t\u00e9"), ["ok-1", "z.y_z"]
        )
        self.assertEqual(fwb.warn_tokens("status=ok nowarn=zzz"), [])
        self.assertEqual(fwb.warn_tokens(""), [])

    def test_no_credential_value_appears_and_none_is_read(self):
        self.full_install()
        self.last_complete(1.0)
        fakes = {
            "B2_FLEET_WORK_BUCKET_NAME": "fwb-bucket-" + "q7zp3km9tx2w",
            "B2_FLEET_WORK_KEY_ID": KEYID,
            "B2_FLEET_WORK_APPLICATION_KEY": APPKEY,
            "RESTIC_FLEET_WORK_PASSWORD": PW,
        }
        handoff = self.home / "fake-fleet-secrets.env"
        write(handoff, "".join(f'{k}="{v}"\n' for k, v in fakes.items()), 0o600)
        leaked = "".join(["ghp_", "aB3dE5gH7jK9", "mN1pQ3sT5v7x", "Zq2R4tY6uW8e"])
        self.put_log(
            "2026-10-11 1:00am status=error reason=missing-credentials names=B2_FLEET_WORK_KEY_ID,RESTIC_FLEET_WORK_PASSWORD",
            f"2026-10-11 2:00am status=error reason=restic-backup-failed detail=denied for {leaked} warn=b2-unchecked",
        )
        trap = AssertionError("health touched a credential, restic, B2 or Infisical")
        with (
            mock.patch.dict(os.environ, fakes),
            mock.patch.object(fwb, "read_credentials", side_effect=trap),
            mock.patch.object(fwb, "restic_env", side_effect=trap),
            mock.patch.object(fwb, "run_restic", side_effect=trap),
            mock.patch.object(fwb, "b2_call", side_effect=trap),
            mock.patch.object(fwb, "infisical_get", side_effect=trap),
        ):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                fwb.cmd_health(self.cfg, argparse.Namespace(json=True))
                fwb.cmd_health(self.cfg, argparse.Namespace(json=False))
        out = buf.getvalue()
        for name, value in fakes.items():
            self.assertNotIn(value, out, name)
        self.assertNotIn(leaked, out)
        self.assertNotIn("denied", out)
        self.assertIn("withheld", json.loads(out[: out.index("\nfleet-work-backup:")])["last_line"])

    def test_the_names_in_a_run_line_are_not_withheld(self):
        self.full_install()
        self.put_log(
            "2026-10-11 1:00am status=error reason=missing-credentials names=B2_FLEET_WORK_KEY_ID,RESTIC_FLEET_WORK_PASSWORD"
        )
        self.assertIn(
            "names=B2_FLEET_WORK_KEY_ID,RESTIC_FLEET_WORK_PASSWORD", self.health()["last_line"]
        )

    def test_the_stable_copy_prints_json_and_exits_zero_whatever_the_verdict(self):
        p = self.run_cli("health", "--json")  # python3 -I, as the Plumbers run the installed copy
        self.assertEqual(p.returncode, 0, p.stderr)
        rep = json.loads(p.stdout)
        self.assertEqual(rep["verdict"], "unhealthy")
        self.assertTrue(rep["reason"].startswith("not installed"), rep["reason"])
        self.full_install()
        self.put_state(last_success=time.time() - 3600, last_success_text="just now")
        rep = json.loads(self.run_cli("health", "--json").stdout)
        self.assertEqual((rep["verdict"], rep["loaded"], rep["plist_sha_ok"]), ("ok", True, True))
        p = self.run_cli("health")
        self.assertEqual(p.returncode, 0)
        self.assertIn("fleet-work-backup: verdict: ok", p.stdout)

    def test_the_new_verbs_are_listed_beside_the_old_ones(self):
        out = self.run_cli("--help").stdout
        for verb in (
            "health",
            "repair",
            "run",
            "init",
            "check-access",
            "image",
            "new-password",
            "snapshots",
            "restic",
        ):
            self.assertIn(verb, out)


class RepairCase(HealthBase):
    """``repair --reload``:  one launchctl bootstrap, only for the installed plist, only when it is not loaded and the
    hash install.py recorded still matches.  Anything else does nothing and says why."""

    def bootstrap_calls(self) -> list:
        return [l for l in self.launchctl_log() if l.startswith("bootstrap")]

    def test_it_bootstraps_only_the_installed_plist_when_not_loaded_and_the_hash_matches(self):
        self.full_install(loaded=False)
        plist_before = self.plist.read_bytes()
        mtime_before = self.plist.stat().st_mtime_ns
        state_before = fwb.read_state(self.cfg)
        code, msg = fwb.repair_reload(self.cfg)
        self.assertEqual(code, 0, msg)
        self.assertIn("bootstrapped com.jay.fleet-work-backup", msg)
        self.assertIn("it is loaded now", msg)
        self.assertEqual(
            self.launchctl_log(),
            [
                f"print {self.target}",
                f"bootstrap gui/{os.getuid()} {self.plist}",
                f"print {self.target}",
            ],
        )
        self.assertEqual(self.plist.read_bytes(), plist_before)  # never edited
        self.assertEqual(self.plist.stat().st_mtime_ns, mtime_before)
        self.assertEqual(fwb.read_state(self.cfg), state_before)  # and no state written
        self.assertTrue(self.health()["loaded"])

    def test_an_already_loaded_job_is_left_alone(self):
        self.full_install(loaded=True)
        code, msg = fwb.repair_reload(self.cfg)
        self.assertEqual(code, 0)
        self.assertIn("already loaded", msg)
        self.assertEqual(self.launchctl_log(), [f"print {self.target}"])

    def test_a_changed_plist_is_never_reloaded(self):
        self.full_install(loaded=False)
        self.plist.write_bytes(self.plist.read_bytes().replace(b"10800", b"60"))
        code, msg = fwb.repair_reload(self.cfg)
        self.assertEqual(code, 1)
        self.assertIn("did nothing", msg)
        self.assertIn("not the plist install.py installed", msg)
        self.assertEqual(self.bootstrap_calls(), [])
        self.assertFalse((self.stub / "loaded").exists())

    def test_a_plist_with_no_recorded_hash_is_never_reloaded(self):
        self.install_files()
        self.set_loaded(False)
        code, msg = fwb.repair_reload(self.cfg)
        self.assertEqual(code, 1)
        self.assertIn("has not recorded a plist hash", msg)
        self.assertEqual(self.bootstrap_calls(), [])
        self.put_state(plist_sha256="")
        self.assertEqual(fwb.repair_reload(self.cfg)[0], 1)
        self.put_state(plist_sha256=["not", "a", "string"])
        self.assertEqual(fwb.repair_reload(self.cfg)[0], 1)
        self.assertEqual(self.bootstrap_calls(), [])

    def test_a_hash_that_matches_a_plist_with_another_label_is_refused(self):
        self.install_files(label="com.example.something-else")
        self.set_loaded(False)
        self.record_hash()
        code, msg = fwb.repair_reload(self.cfg)
        self.assertEqual(code, 1)
        self.assertIn("does not carry the label com.jay.fleet-work-backup", msg)
        self.assertEqual(self.bootstrap_calls(), [])
        self.plist.write_text("<plist>not a plist")
        self.record_hash()
        self.assertEqual(fwb.repair_reload(self.cfg)[0], 1)
        self.assertEqual(self.bootstrap_calls(), [])

    def test_a_job_that_is_not_installed_is_never_bootstrapped(self):
        code, msg = fwb.repair_reload(self.cfg)
        self.assertEqual(code, 1)
        self.assertIn("not installed", msg)
        self.install_files(script=False)
        self.record_hash()
        code, msg = fwb.repair_reload(self.cfg)
        self.assertEqual(code, 1)
        self.assertIn("fleet_work_backup.py", msg)
        shutil.rmtree(self.home / "Library")
        self.install_files(plist=False)
        self.assertEqual(fwb.repair_reload(self.cfg)[0], 1)
        self.assertEqual(self.bootstrap_calls(), [])

    def test_a_failed_bootstrap_is_reported_and_nothing_else_runs(self):
        self.full_install(loaded=False)
        (self.stub / "bootstrap_rc").write_text("5")
        code, msg = fwb.repair_reload(self.cfg)
        self.assertEqual(code, 2)
        self.assertIn("exited 5 (Bootstrap failed: 5: Input/output error)", msg)
        self.assertIn("still not loaded", msg)
        self.assertEqual(
            self.launchctl_log(),
            [
                f"print {self.target}",
                f"bootstrap gui/{os.getuid()} {self.plist}",
                f"print {self.target}",
            ],
        )

    def test_a_bootstrap_that_failed_but_left_the_job_loaded_is_told_as_it_was(self):
        self.full_install(loaded=False)
        (self.stub / "bootstrap_rc").write_text("0")
        with mock.patch.object(
            fwb, "launchctl_capture", return_value=(5, "Bootstrap failed: 5: already loaded")
        ):
            self.set_loaded(True)  # someone else loaded it between our check and our bootstrap
            with mock.patch.object(fwb, "launchd_loaded", side_effect=[(False, 113), (True, 0)]):
                code, msg = fwb.repair_reload(self.cfg)
        self.assertEqual(code, 0)
        self.assertIn("it is loaded now", msg)
        self.assertIn("launchctl bootstrap exited 5: Bootstrap failed: 5: already loaded", msg)

    def test_a_launchctl_that_cannot_run_is_not_guessed_at(self):
        self.full_install(loaded=False)
        self.cfg.launchctl_bin = str(self.tmp / "no-such-launchctl")
        code, msg = fwb.repair_reload(self.cfg)
        self.assertEqual(code, 1)
        self.assertIn("launchctl did not run", msg)

    def test_it_runs_launchctl_and_nothing_else_and_reads_no_credential(self):
        self.full_install(loaded=False)
        write(self.home / "fake-fleet-secrets.env", f'RESTIC_FLEET_WORK_PASSWORD="{PW}"\n', 0o600)
        trap = AssertionError("repair touched a credential, restic, B2, Infisical or the installer")
        real_run = subprocess.run
        ran = []

        def spy(argv, *a, **kw):
            ran.append(list(argv))
            return real_run(argv, *a, **kw)

        opened = []
        real_open = open

        def spy_open(file, *a, **kw):
            opened.append(str(file))
            return real_open(file, *a, **kw)

        with (
            mock.patch.object(fwb, "read_credentials", side_effect=trap),
            mock.patch.object(fwb, "restic_env", side_effect=trap),
            mock.patch.object(fwb, "run_restic", side_effect=trap),
            mock.patch.object(fwb, "b2_call", side_effect=trap),
            mock.patch.object(fwb, "infisical_get", side_effect=trap),
            mock.patch.object(fwb.subprocess, "run", spy),
            mock.patch("builtins.open", spy_open),
        ):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                self.assertEqual(fwb.cmd_repair(self.cfg, argparse.Namespace(reload=True)), 0)
        self.assertEqual({argv[0] for argv in ran}, {str(self.launchctl)})
        self.assertEqual([argv[1] for argv in ran], ["print", "bootstrap", "print"])
        self.assertFalse(
            [f for f in opened if ".secrets" in f or "fake-fleet-secrets" in f], opened
        )
        self.assertIn("fleet-work-backup: repair --reload: bootstrapped", buf.getvalue())
        self.assertNotIn(PW, buf.getvalue())

    def test_the_cli_needs_the_reload_flag_and_exits_with_the_result(self):
        p = self.run_cli("repair")
        self.assertEqual(p.returncode, 2)
        self.assertIn("--reload", p.stderr)
        self.assertEqual(self.launchctl_log(), [])
        self.full_install(loaded=False)
        p = self.run_cli("repair", "--reload")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("repair --reload: bootstrapped", p.stdout)
        p = self.run_cli("repair", "--reload")  # now loaded:  nothing to do, still a success
        self.assertEqual(p.returncode, 0)
        self.assertIn("nothing to do", p.stdout)
        self.assertEqual(len(self.bootstrap_calls()), 1)
        self.plist.write_bytes(self.plist.read_bytes() + b" ")
        self.set_loaded(False)
        p = self.run_cli("repair", "--reload")
        self.assertEqual(p.returncode, 1)
        self.assertEqual(len(self.bootstrap_calls()), 1)


class InstallHashCase(HealthBase):
    """install.py records the sha256 of the plist it installs, and ``repair --reload`` trusts only that."""

    def setUp(self):
        super().setUp()
        spec2 = importlib.util.spec_from_file_location("fwb_install_hash", HERE / "install.py")
        self.inst = importlib.util.module_from_spec(spec2)
        spec2.loader.exec_module(self.inst)
        self.inst.PLIST_DST = str(self.plist)
        self.inst.SCRIPT_DST = str(self.script)
        self.launchd_calls = []

        def fake_launchd(changed, dry):
            self.launchd_calls.append((changed, dry, (self.cfg.state_dir / "state.json").exists()))
            return 0

        patches = [
            mock.patch.dict(os.environ, {"FLEET_WORK_BACKUP_HOME": str(self.home)}),
            mock.patch.object(sys, "platform", "darwin"),
            mock.patch.object(self.inst, "load_module", return_value=fwb),
            mock.patch.object(self.inst, "prerequisites", return_value=[]),
            mock.patch.object(self.inst, "launchd", side_effect=fake_launchd),
            mock.patch.object(self.inst, "interpreter_image", return_value=("", "")),
        ]
        for p in patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in reversed(patches)])

    def install(self, *argv) -> int:
        buf = io.StringIO()
        with mock.patch.object(sys, "argv", ["install.py", *argv]), contextlib.redirect_stdout(buf):
            rc = self.inst.main()
        self.out = buf.getvalue()
        return rc

    def installed_hash(self) -> str:
        return hashlib.sha256(self.plist.read_bytes()).hexdigest()

    def test_install_records_the_hash_of_the_plist_it_installed_before_loading_it(self):
        self.assertEqual(self.install(), 0)
        state = fwb.read_state(self.cfg)
        self.assertEqual(state["plist_sha256"], self.installed_hash())
        self.assertIsInstance(state["plist_installed_at"], float)
        self.assertEqual(
            self.launchd_calls, [(True, False, True)]
        )  # the state file was there when launchd ran
        self.assertIn("recorded the installed plist's sha256", self.out)
        # ... and that is the hash repair --reload trusts
        self.set_loaded(False)
        code, msg = fwb.repair_reload(self.cfg)
        self.assertEqual(code, 0, msg)
        self.assertEqual(len([l for l in self.launchctl_log() if l.startswith("bootstrap")]), 1)

    def test_the_hash_is_of_the_file_on_disk_with_the_chosen_interpreter(self):
        self.assertEqual(self.install("--interpreter", "/opt/homebrew/bin/python3"), 0)
        self.assertIn(b"/opt/homebrew/bin/python3", self.plist.read_bytes())
        self.assertEqual(fwb.read_state(self.cfg)["plist_sha256"], self.installed_hash())

    def test_other_state_is_kept_and_the_install_time_moves_only_with_the_hash(self):
        self.put_state(last_success=123.0, consecutive_not_ok=3)
        self.install()
        self.put_state(plist_installed_at=111.0)
        first = self.installed_hash()
        self.install()  # the same plist again
        state = fwb.read_state(self.cfg)
        self.assertEqual((state["plist_sha256"], state["plist_installed_at"]), (first, 111.0))
        self.assertEqual((state["last_success"], state["consecutive_not_ok"]), (123.0, 3))
        self.install("--interpreter", "/opt/homebrew/bin/python3")  # a different plist
        state = fwb.read_state(self.cfg)
        self.assertNotEqual(state["plist_sha256"], first)
        self.assertEqual(state["plist_sha256"], self.installed_hash())
        self.assertGreater(state["plist_installed_at"], 111.0)

    def test_a_dry_run_skip_launchd_and_a_blocked_install_record_nothing(self):
        self.assertEqual(self.install("--dry-run"), 0)
        self.assertFalse((self.cfg.state_dir / "state.json").exists())
        self.assertFalse(self.plist.exists())
        self.assertEqual(self.install("--skip-launchd"), 0)
        self.assertFalse((self.cfg.state_dir / "state.json").exists())
        with mock.patch.object(
            self.inst, "prerequisites", return_value=["restic (brew install restic)"]
        ):
            self.assertEqual(self.install(), 0)
            self.assertFalse(
                (self.cfg.state_dir / "state.json").exists()
            )  # nothing was installed, so nothing is trusted
            self.assertEqual(
                self.install("--force"), 0
            )  # --force installs the plist, so it is recorded
        self.assertEqual(fwb.read_state(self.cfg)["plist_sha256"], self.installed_hash())

    def test_a_state_file_that_cannot_be_written_does_not_stop_the_install(self):
        with mock.patch.object(fwb, "write_state", side_effect=OSError("disk full")):
            self.assertEqual(self.install(), 0)
        self.assertIn("could not record", self.out)
        self.assertEqual(len(self.launchd_calls), 1)
        self.assertTrue(self.plist.exists())
        self.set_loaded(False)
        self.assertEqual(fwb.repair_reload(self.cfg)[0], 1)  # without a hash, repair refuses


if __name__ == "__main__":
    unittest.main(verbosity=2)
