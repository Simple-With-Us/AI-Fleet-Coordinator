from __future__ import annotations

import base64
import copy
import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import remote_git_source as remote

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "remote_test_collector", ROOT / "scripts/collect-repo-documents.py"
)
assert SPEC is not None and SPEC.loader is not None
collector = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = collector
SPEC.loader.exec_module(collector)
EXPORT_SPEC = importlib.util.spec_from_file_location(
    "remote_test_exporter", ROOT / "scripts/export-repo-git-source.py"
)
assert EXPORT_SPEC is not None and EXPORT_SPEC.loader is not None
exporter = importlib.util.module_from_spec(EXPORT_SPEC)
EXPORT_SPEC.loader.exec_module(exporter)


def signed_proof(root: str) -> tuple[str, dict]:
    payload = f"tree {root}\nauthor Fixture <fixture@example.invalid> 1 +0000\ncommitter Fixture <fixture@example.invalid> 1 +0000\n\nFixture commit\n"
    signature = "-----BEGIN PGP SIGNATURE-----\n\nfixture\n-----END PGP SIGNATURE-----\n"
    headers, message = payload.split("\n\n", 1)
    raw = (headers + "\ngpgsig " + signature.replace("\n", "\n ") + "\n\n" + message).encode()
    return remote.object_id("commit", raw), {
        "verified": True,
        "reason": "valid",
        "payload": payload,
        "signature": signature,
    }


class ProviderFixture:
    def __init__(self, private: bool = False):
        self.owner_repo = "Simple-With-Us/Kodus-Config" if private else "fixture-owner/ExampleRepo"
        self.visibility = "private" if private else "public"
        self.files = {
            "README.md": (
                "100644",
                b"# Example\n[image](docs/chart.png) [evidence](docs/evidence.json) [source](src/app.py) [symlink](docs/link.png)\n",
            ),
            "docs/guide.md": ("100755", b"Guide.\n"),
            "docs/chart.png": ("100644", b"\x89PNG\r\n\x1a\nfixture"),
            "docs/evidence.json": ("100644", b'{"fixture":true}\n'),
            "docs/link.png": ("120000", b"../../outside.png"),
            "docs/fixtures/excluded.md": ("100644", b"fixture data"),
            "src/app.py": ("100644", b"print('excluded')\n"),
            ".env": ("100644", b"EXCLUDED_FIXTURE=value\n"),
            "vendor/excluded.md": ("100644", b"vendor data"),
            "backup/excluded.md": ("100644", b"backup data"),
            "backups/excluded.md": ("100644", b"plural backup data"),
            "archives/excluded.md": ("100644", b"plural archive data"),
        }
        self.calls = []
        nested: dict = {}
        for path, (mode, content) in self.files.items():
            current = nested
            parts = path.split("/")
            for part in parts[:-1]:
                current = current.setdefault(part, {})
            current[parts[-1]] = {
                "mode": mode,
                "type": "blob",
                "sha": remote.object_id("blob", content),
            }
        nested["submodule"] = {"mode": "160000", "type": "commit", "sha": "1" * 40}
        self.trees: dict = {}

        def tree(value: dict) -> str:
            entries = []
            for name, item in value.items():
                entries.append(
                    {
                        "path": name,
                        **(
                            item
                            if "mode" in item
                            else {"mode": "040000", "type": "tree", "sha": tree(item)}
                        ),
                    }
                )
            entries.sort(
                key=lambda item: item["path"].encode() + (b"/" if item["type"] == "tree" else b"\0")
            )
            raw = b"".join(
                ("40000" if item["mode"] == "040000" else item["mode"]).encode()
                + b" "
                + item["path"].encode()
                + b"\0"
                + bytes.fromhex(item["sha"])
                for item in entries
            )
            oid = remote.object_id("tree", raw)
            self.trees[oid] = entries
            return oid

        self.root = tree(nested)
        self.commit, self.proof = signed_proof(self.root)

    def call(self, tool: str, args: dict) -> dict:
        self.calls.append((tool, args))
        if args["repo"] != self.owner_repo:
            raise AssertionError("Unexpected repository")
        if tool == "gr_wf_repo_get":
            return {
                "repository": {
                    "id": 123,
                    "name": self.owner_repo.split("/")[1],
                    "full_name": self.owner_repo,
                    "visibility": self.visibility,
                    "private": self.visibility == "private",
                    "default_branch": "main",
                }
            }
        if tool == "gr_wf_branch_get":
            return {
                "branch": {
                    "name": "main",
                    "commit": {
                        "sha": self.commit,
                        "commit": {"tree": {"sha": self.root}, "verification": self.proof},
                    },
                }
            }
        if tool == "gr_wf_git_tree_get":
            return {
                "tree": {"sha": args["sha"], "truncated": False, "entries": self.trees[args["sha"]]}
            }
        if tool == "gr_wf_file_get":
            assert args["ref"] == self.commit
            path = args["path"]
            _, content = self.files[path]
            return {
                "path": path,
                "sha": remote.object_id("blob", content),
                "size": len(content),
                "encoding": "base64",
                "content": base64.b64encode(content).decode(),
            }
        raise AssertionError("Unexpected tool " + tool)

    def snapshot(self):
        return remote.export_snapshot(self.call, self.owner_repo, self.visibility, self.commit)


class RemoteGitSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT, prefix=".remote-docs-test-")
        self.base = Path(self.temp.name)
        self.sources = self.base / "sources"
        self.sources.mkdir(mode=0o700)
        self.sources.chmod(0o700)
        self.descriptor = self.sources / "snapshot.json"
        self.output = self.base / "Docs"
        self.provider = ProviderFixture()

    def tearDown(self):
        self.temp.cleanup()

    def export(self):
        snapshot = self.provider.snapshot()
        owner, name = snapshot.identity["owner_repo"].split("/")
        repo = collector.RepoInfo(
            self.descriptor,
            owner,
            name,
            snapshot.identity["source_ref"],
            snapshot.identity["source_commit"],
            snapshot,
        )
        selected, _, _ = collector._select_blobs(repo, collector._parse_tree(repo))
        snapshot.require_selected(set(selected))
        remote.write_descriptor(snapshot, self.descriptor)
        return snapshot

    def edit_descriptor(self, change, rebind=False):
        value = json.loads(self.descriptor.read_text())
        change(value)
        self.descriptor.write_bytes(remote.canonical(value))
        if rebind:
            path = self.descriptor.with_name(self.descriptor.name + ".receipt.json")
            receipt = json.loads(path.read_text())
            receipt["descriptor_sha256"] = remote.digest(value)
            path.write_bytes(remote.canonical(receipt))

    def assert_no_write(self):
        with self.assertRaises(collector.CollectionError):
            collector.collect(None, self.output, git_source_descriptors=[self.descriptor])
        self.assertFalse(self.output.exists())

    def test_export_requests_only_selected_files_then_imports_offline_idempotently(self):
        snapshot = self.export()
        requested = {args["path"] for tool, args in self.provider.calls if tool == "gr_wf_file_get"}
        self.assertEqual(
            requested, {"README.md", "docs/guide.md", "docs/chart.png", "docs/evidence.json"}
        )
        self.assertEqual(len(snapshot.entries), len(self.provider.files) + 1)
        with patch.object(
            collector, "_run_git", side_effect=AssertionError("Offline import used Git")
        ):
            collector.collect(None, self.output, git_source_descriptors=[self.descriptor])
            self.assertEqual(
                collector.collect(None, self.output, git_source_descriptors=[self.descriptor]), []
            )
        catalog = json.loads((self.output / "ExampleRepo/catalog.json").read_text())
        self.assertEqual(catalog["tracked_entry_count"], len(snapshot.entries))
        files = {item["path"]: item for item in catalog["files"]}
        self.assertIsNone(files["src/app.py"]["size_bytes"])
        self.assertIn("provider", files["src/app.py"]["size_unavailable_reason"])
        self.assertEqual(files["README.md"]["size_bytes"], len(self.provider.files["README.md"][1]))
        manifest = json.loads((self.output / "ExampleRepo/manifest.json").read_text())
        self.assertEqual(
            manifest["source_provenance"]["descriptor_sha256"],
            remote.digest(json.loads(self.descriptor.read_text())),
        )
        for path in self.output.rglob("*"):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700 if path.is_dir() else 0o600)

    def test_signed_commit_reconstruction_matches_git_and_preserves_signature_newline(self):
        root = "1" * 40
        commit, proof = signed_proof(root)
        headers, message = proof["payload"].split("\n\n", 1)
        raw = (
            headers + "\ngpgsig " + proof["signature"].replace("\n", "\n ") + "\n\n" + message
        ).encode()
        oid = (
            subprocess.run(
                ["git", "hash-object", "-t", "commit", "--stdin"],
                input=raw,
                stdout=subprocess.PIPE,
                check=True,
            )
            .stdout.decode()
            .strip()
        )
        self.assertEqual(commit, oid)
        self.assertEqual(remote.verify_commit(oid, proof), root)
        for field, value in (
            ("payload", proof["payload"] + "changed"),
            ("signature", proof["signature"].rstrip("\n")),
            ("verified", False),
            ("payload", None),
        ):
            with self.subTest(field=field), self.assertRaises(remote.SourceError):
                remote.verify_commit(commit, {**proof, field: value})

    def test_tree_order_and_mode_normalization_match_git_mktree(self):
        entries = [
            {"path": "a", "mode": "040000", "type": "tree", "sha": "1" * 40},
            {"path": "a.c", "mode": "100755", "type": "blob", "sha": "2" * 40},
            {"path": "a0", "mode": "100644", "type": "blob", "sha": "3" * 40},
        ]
        subprocess.run(["git", "init", "--quiet", str(self.base / "git")], check=True)
        raw = "".join(
            f"{item['mode']} {item['type']} {item['sha']}\t{item['path']}\n" for item in entries
        ).encode()
        oid = (
            subprocess.run(
                ["git", "-C", str(self.base / "git"), "mktree", "--missing"],
                input=raw,
                stdout=subprocess.PIPE,
                check=True,
            )
            .stdout.decode()
            .strip()
        )
        self.assertEqual(
            [item["path"] for item in remote.verify_tree(oid, entries)], ["a.c", "a", "a0"]
        )
        self.assertEqual(remote.verify_tree(remote.object_id("tree", b""), []), [])

    def test_tree_forgery_traversal_duplicate_and_modes_fail(self):
        root = self.provider.root
        original = self.provider.trees[root]
        for mutate in (
            lambda e: e[0].update(path="forged"),
            lambda e: e[0].update(path="../escape"),
            lambda e: e.append(e[0]),
            lambda e: e[0].update(mode="100666"),
            lambda e: e[0].update(type="commit"),
            lambda e: e[0].update(path="a/b"),
            lambda e: e[0].update(path="\udcff"),
        ):
            with self.subTest(mutate=mutate):
                entries = copy.deepcopy(original)
                mutate(entries)
                with self.assertRaises(remote.SourceError):
                    remote.verify_tree(root, entries)

    def test_receipt_rebinding_identity_and_object_proof_fail_before_any_write(self):
        self.export()
        original = self.descriptor.read_bytes()
        receipt = self.descriptor.with_name(self.descriptor.name + ".receipt.json")
        original_receipt = receipt.read_bytes()
        changes = [
            (lambda x: x["identity"].update(owner_repo="other-owner/ExampleRepo"), False),
            (lambda x: x["identity"].update(repository_id=456), False),
            (lambda x: x["identity"].update(visibility="private"), False),
            (lambda x: x["identity"].update(source_ref="refs/heads/other"), True),
            (lambda x: x["identity"].update(root_tree="0" * 40), True),
            (lambda x: x["commit_proof"].update(signature=None), True),
            (lambda x: x["trees"][self.provider.root][0].update(path="forged.md"), True),
            (lambda x: x["selected_blobs"]["README.md"].update(size=0), True),
            (lambda x: x["selected_blobs"]["README.md"].update(sha="0" * 40), True),
            (lambda x: x["selected_blobs"]["README.md"].update(content="AA=="), True),
            (lambda x: x["selected_blobs"].pop("README.md"), True),
        ]
        for change, rebind in changes:
            with self.subTest(change=change):
                self.descriptor.write_bytes(original)
                receipt.write_bytes(original_receipt)
                self.edit_descriptor(change, rebind)
                self.assert_no_write()

    def test_valid_hash_for_an_excluded_path_is_still_rejected(self):
        self.export()
        response = self.provider.call(
            "gr_wf_file_get",
            {"repo": self.provider.owner_repo, "path": "src/app.py", "ref": self.provider.commit},
        )
        self.edit_descriptor(
            lambda x: x["selected_blobs"].update({"src/app.py": response}), rebind=True
        )
        self.assert_no_write()

    def test_private_source_requires_exact_opt_in_and_stays_out_of_index_previews(self):
        self.provider = ProviderFixture(private=True)
        self.export()
        for opt_in in (None, "other-owner/Kodus-Config", "simple-with-us/kodus-config"):
            with self.subTest(opt_in=opt_in), self.assertRaises(collector.CollectionError):
                collector.collect(
                    None,
                    self.output,
                    git_source_descriptors=[self.descriptor],
                    private_docs_repo=opt_in,
                )
            self.assertFalse(self.output.exists())
        collector.collect(
            None,
            self.output,
            git_source_descriptors=[self.descriptor],
            private_docs_repo=self.provider.owner_repo,
        )
        manifest = json.loads((self.output / "Kodus-Config/manifest.json").read_text())
        self.assertEqual(manifest["private_docs_opt_in"], self.provider.owner_repo)
        with redirect_stderr(StringIO()):
            self.assertEqual(
                collector.main(
                    ["--git-source-descriptor", str(self.descriptor), "--print-indexes-json"]
                ),
                2,
            )

    def test_unmatched_private_opt_in_cannot_create_output_for_a_public_descriptor(self):
        self.export()
        for identity in ("fixture-owner/ExampleRepo", "Simple-With-Us/Kodus-Config"):
            with self.subTest(identity=identity), self.assertRaises(collector.CollectionError):
                collector.collect(
                    None,
                    self.output,
                    git_source_descriptors=[self.descriptor],
                    private_docs_repo=identity,
                )
            self.assertFalse(self.output.exists())

    def test_missing_receipt_permissions_oversize_and_duplicate_json_fail_closed(self):
        self.export()
        receipt = self.descriptor.with_name(self.descriptor.name + ".receipt.json")
        saved = receipt.read_bytes()
        receipt.unlink()
        self.assert_no_write()
        receipt.write_bytes(saved)
        receipt.chmod(0o600)
        self.descriptor.chmod(0o644)
        self.assert_no_write()
        self.descriptor.chmod(0o600)
        with patch.object(remote, "MAX_DESCRIPTOR_BYTES", 10):
            self.assert_no_write()
        self.descriptor.write_text('{"schema_version":1,"schema_version":1}')
        self.assert_no_write()

    def test_selected_blob_missing_oversize_truncation_and_rebound_path_fail(self):
        snapshot = self.provider.snapshot()
        entry = snapshot.entries["README.md"]
        original = self.provider.call(
            "gr_wf_file_get",
            {"repo": self.provider.owner_repo, "path": "README.md", "ref": self.provider.commit},
        )
        for delta in (
            {"content": None},
            {"size": remote.MAX_BLOB_BYTES + 1},
            {"content": ""},
            {"path": "docs/guide.md"},
            {"encoding": "none"},
            {"content": "not base64!"},
        ):
            with self.subTest(delta=delta), self.assertRaises(remote.SourceError):
                remote.verify_blob({**original, **delta}, entry)

    def test_near_ceiling_blob_accepts_github_sixty_column_base64(self):
        for size in (remote.MAX_BLOB_BYTES - 1, remote.MAX_BLOB_BYTES):
            with self.subTest(size=size):
                content = (b"bounded fixture\n" * (size // 16 + 1))[:size]
                oid = remote.object_id("blob", content)
                entry = {
                    "path": "docs/large.txt",
                    "mode": "100644",
                    "object_type": "blob",
                    "object_id": oid,
                }
                compact = base64.b64encode(content).decode("ascii")
                wrapped = (
                    "\n".join(compact[index : index + 60] for index in range(0, len(compact), 60))
                    + "\n"
                )
                self.assertGreater(len(wrapped), remote.MAX_BLOB_BYTES * 4 // 3 + 4096)
                response = {
                    "path": entry["path"],
                    "sha": oid,
                    "size": size,
                    "encoding": "base64",
                    "content": wrapped,
                }
                self.assertEqual(remote.verify_blob(response, entry), content)

    def test_excess_compact_or_raw_base64_and_invalid_encoding_fail_closed(self):
        content = b"small fixture"
        oid = remote.object_id("blob", content)
        entry = {
            "path": "docs/small.txt",
            "mode": "100644",
            "object_type": "blob",
            "object_id": oid,
        }
        response = {
            "path": entry["path"],
            "sha": oid,
            "size": len(content),
            "encoding": "base64",
            "content": base64.b64encode(content).decode("ascii"),
        }
        for encoded, message in (
            ("A" * (remote.MAX_COMPACT_BASE64_CHARS + 4), "compact encoding"),
            ("\n" * (remote.MAX_RAW_BASE64_CHARS + 1), "raw encoding"),
            (response["content"] + "!", "encoding is invalid"),
            (response["content"] + "\r\n", "encoding is invalid"),
        ):
            with self.subTest(message=message), self.assertRaisesRegex(remote.SourceError, message):
                remote.verify_blob({**response, "content": encoded}, entry)

    def test_provider_truncated_missing_and_oversized_tree_never_requests_content(self):
        for mode in ("truncated", "missing", "oversized"):

            def call(tool, args, mode=mode):
                data = self.provider.call(tool, args)
                if tool == "gr_wf_git_tree_get":
                    if mode == "truncated":
                        data["tree"]["truncated"] = True
                    elif mode == "missing":
                        data["tree"].pop("entries")
                    else:
                        data["tree"]["entries"] = data["tree"]["entries"] * 101
                return data

            with self.subTest(mode=mode), self.assertRaises(remote.SourceError):
                remote.export_snapshot(
                    call, self.provider.owner_repo, self.provider.visibility, self.provider.commit
                )
        self.assertFalse(any(tool == "gr_wf_file_get" for tool, _ in self.provider.calls))

    def test_wrong_provider_identity_branch_or_commit_fails_before_content_reads(self):
        for field in ("identity", "visibility", "branch", "commit"):

            def call(tool, args, field=field):
                data = self.provider.call(tool, args)
                if tool == "gr_wf_repo_get" and field == "identity":
                    data["repository"]["full_name"] = "other-owner/ExampleRepo"
                if tool == "gr_wf_repo_get" and field == "visibility":
                    data["repository"]["visibility"] = "private"
                if tool == "gr_wf_branch_get" and field == "branch":
                    data["branch"]["name"] = "other"
                if tool == "gr_wf_branch_get" and field == "commit":
                    data["branch"]["commit"]["sha"] = "0" * 40
                return data

            with self.subTest(field=field), self.assertRaises(remote.SourceError):
                remote.export_snapshot(
                    call, self.provider.owner_repo, self.provider.visibility, self.provider.commit
                )
        self.assertFalse(any(tool == "gr_wf_file_get" for tool, _ in self.provider.calls))

    def test_bad_source_cannot_mutate_an_existing_collection_or_create_a_lock(self):
        self.export()
        self.output.mkdir(mode=0o700)
        self.output.chmod(0o700)
        sentinel = self.output / "owner-document.md"
        sentinel.write_bytes(b"Preserve this document\n")
        sentinel.chmod(0o600)
        self.edit_descriptor(
            lambda value: value["selected_blobs"]["README.md"].update(content="truncated"),
            rebind=True,
        )
        with self.assertRaises(collector.CollectionError):
            collector.collect(None, self.output, git_source_descriptors=[self.descriptor])
        self.assertEqual(list(self.output.iterdir()), [sentinel])
        self.assertEqual(sentinel.read_bytes(), b"Preserve this document\n")

    def test_remote_sources_cannot_silently_replace_duplicate_local_identity(self):
        self.export()
        local = collector.RepoInfo(
            self.base / "local", "fixture-owner", "ExampleRepo", "origin/main", self.provider.commit
        )
        with (
            patch.object(collector, "_repo_info", return_value=local),
            self.assertRaisesRegex(collector.CollectionError, "duplicates"),
        ):
            collector.collect(
                None, self.output, repo_paths=[local.path], git_source_descriptors=[self.descriptor]
            )
        self.assertFalse(self.output.exists())

    def test_explicit_descriptor_does_not_discover_environment_code_root(self):
        self.export()
        with patch.object(
            collector,
            "_repo_info",
            side_effect=AssertionError("Discovered unrelated local repositories"),
        ):
            repositories, _ = collector.discover_repositories(
                ROOT, git_source_descriptors=[self.descriptor]
            )
        self.assertEqual([repo.name for repo in repositories], ["ExampleRepo"])

    def test_metadata_and_selected_content_limits_fail_closed(self):
        with patch.object(remote, "MAX_API_CALLS", 1), self.assertRaises(remote.SourceError):
            self.provider.snapshot()
        with patch.object(remote, "MAX_TREES", 1), self.assertRaises(remote.SourceError):
            self.provider.snapshot()
        snapshot = self.provider.snapshot()
        with patch.object(remote, "MAX_SELECTED_BYTES", 1), self.assertRaises(remote.SourceError):
            snapshot.read_blob("README.md")
        trees = copy.deepcopy(snapshot.trees)
        del trees[next(key for key in trees if key != snapshot.identity["root_tree"])]
        with self.assertRaises(remote.SourceError):
            remote.tracked_entries(snapshot.identity["root_tree"], trees)

    def test_cli_transport_passes_only_arguments_on_stdin_and_never_reads_credentials(self):
        def run(argv, **kwargs):
            self.assertEqual(
                argv, ["simple-git", "call", "gr_wf_repo_get", "--args-json", "-", "--json"]
            )
            self.assertEqual(json.loads(kwargs["input"]), {"repo": "fixture-owner/ExampleRepo"})
            kwargs["stdout"].write(
                json.dumps({"structured": {"repository": {"id": 123}}, "text": ""}).encode()
            )
            return subprocess.CompletedProcess(argv, 0)

        with (
            patch.dict(os.environ, {"SIMPLE_TOOLS_BEARER": "fixture-process-only"}),
            patch.object(exporter.subprocess, "run", side_effect=run),
            patch.object(Path, "read_text", side_effect=AssertionError("Read a credential path")),
        ):
            self.assertEqual(
                exporter.relay_read("gr_wf_repo_get", {"repo": "fixture-owner/ExampleRepo"}),
                {"repository": {"id": 123}},
            )
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(
                exporter.subprocess, "run", side_effect=AssertionError("Started without a bearer")
            ),
            self.assertRaises(remote.SourceError),
        ):
            exporter.relay_read("gr_wf_repo_get", {})
        with self.assertRaises(remote.SourceError):
            exporter.relay_read("gr_wf_commit_get", {})

    def test_export_cli_checks_expected_repository_id_before_blob_reads(self):
        with (
            patch.object(exporter, "relay_read", side_effect=self.provider.call),
            redirect_stderr(StringIO()),
        ):
            result = exporter.main(
                [
                    "--owner-repo",
                    self.provider.owner_repo,
                    "--visibility",
                    "public",
                    "--expected-repository-id",
                    "456",
                    "--commit",
                    self.provider.commit,
                    "--output",
                    str(self.descriptor),
                ]
            )
        self.assertEqual(result, 1)
        self.assertFalse(self.descriptor.exists())
        self.assertFalse(any(tool == "gr_wf_file_get" for tool, _ in self.provider.calls))
        with (
            patch.object(
                exporter,
                "relay_read",
                side_effect=AssertionError("Read with a public private-docs opt-in"),
            ),
            redirect_stderr(StringIO()),
        ):
            result = exporter.main(
                [
                    "--owner-repo",
                    self.provider.owner_repo,
                    "--visibility",
                    "public",
                    "--private-docs-repo",
                    self.provider.owner_repo,
                    "--expected-repository-id",
                    "123",
                    "--commit",
                    self.provider.commit,
                    "--output",
                    str(self.descriptor),
                ]
            )
        self.assertEqual(result, 1)


if __name__ == "__main__":
    unittest.main()
