import base64
import gzip
import io
import unittest
import urllib.error
import zipfile
import zlib
from email.message import Message

try:
    from test_workflow_verbs import REPO, Client, Policy
except ImportError:
    from .test_workflow_verbs import REPO, Client, Policy

from git_relay.github_client import GitHubClient, RelayGitHubError
from git_relay.server import MAX_BINARY_REQUEST_BYTES, MAX_REQUEST_BYTES
from git_relay.workflow_verbs import Refused, WorkflowVerbs

ASSET = {"id": 7, "name": "a.bin", "state": "uploaded", "size": 3}
TOKEN = "ghp_" + "a" * 30


def hdrs(**kw):
    m = Message()
    for k, v in kw.items():
        m[k.replace("_", "-")] = v
    return m


class Resp:
    def __init__(self, status, body=b"", headers=None):
        self.status, self.body, self.headers = status, body, headers or hdrs()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def getcode(self):
        return self.status

    def read(self, n=-1):
        out, self.body = (self.body, b"") if n < 0 else (self.body[:n], self.body[n:])
        return out


def redirecting(location, second):
    seen = []

    def opener(req, timeout):
        seen.append(req)
        if len(seen) == 1:
            raise urllib.error.HTTPError(req.full_url, 302, "Found", hdrs(Location=location), None)
        return second

    return opener, seen


class DownloadClient(unittest.TestCase):
    def test_bearer_only_to_api_origin_and_range_requested(self):
        loc = "https://release-assets.githubusercontent.com/x?sig=1"
        opener, seen = redirecting(loc, Resp(206, b"abc", hdrs(Content_Range="bytes 2-4/10")))
        c = GitHubClient("owner-unused", opener=opener)
        data, total = c.download_chunk(
            "/repos/o/r/releases/assets/7", "own", 2, 3, "application/octet-stream"
        )
        self.assertEqual((data, total), (b"abc", 10))
        self.assertEqual(seen[0].get_header("Authorization"), "Bearer own")
        self.assertTrue(seen[0].full_url.startswith("https://api.github.com/"))
        self.assertEqual(seen[0].get_header("Accept"), "application/octet-stream")
        self.assertEqual(seen[1].full_url, loc)
        self.assertIsNone(seen[1].get_header("Authorization"))
        self.assertEqual(seen[1].get_header("Range"), "bytes=2-4")

    def test_untrusted_redirect_targets_refused_before_second_request(self):
        for loc in [
            "http://objects.githubusercontent.com/x",
            "https://evil.example/x",
            "https://githubusercontent.com.evil.example/x",
            "https://user:pw@objects.githubusercontent.com/x",
            "https://objects.githubusercontent.com:8443/x",
            "https://evilgithubusercontent.com/x",
            "https://objects.githubusercontent.com/x y",
            None,
        ]:
            opener, seen = redirecting(loc, Resp(200, b"x"))
            c = GitHubClient("t", opener=opener)
            with self.assertRaises(RelayGitHubError, msg=loc):
                c.download_chunk("/repos/o/r/actions/jobs/1/logs", "own", 0, 10)
            self.assertEqual(len(seen), 1, loc)

    def test_non_redirect_and_errors(self):
        c = GitHubClient("t", opener=lambda req, timeout: Resp(200, b"{}"))
        with self.assertRaises(RelayGitHubError):
            c.download_chunk("/repos/o/r/actions/jobs/1/logs", "own", 0, 10)

        def gone(req, timeout):
            raise urllib.error.HTTPError(
                req.full_url, 410, "Gone", hdrs(), io.BytesIO(b'{"message":"expired"}')
            )

        with self.assertRaises(RelayGitHubError) as cm:
            GitHubClient("t", opener=gone).download_chunk(
                "/repos/o/r/actions/jobs/1/logs", "own", 0, 10
            )
        self.assertEqual(cm.exception.status, 410)
        for bad in [(-1, 5), (0, 0), (0, 256 * 1024 + 1), (True, 5)]:
            with self.assertRaises(ValueError):
                c.download_chunk("/repos/o/r/x", "own", *bad)
        with self.assertRaises(ValueError):
            c.download_chunk("/repos/o/r/x", "", 0, 5)
        with self.assertRaises(ValueError):
            c.download_chunk("https://evil.example/x", "own", 0, 5)

    def test_200_fallback_skips_and_bounds(self):
        loc = "https://pipelines.actions.githubusercontent.com/x"
        opener, _ = redirecting(loc, Resp(200, b"0123456789", hdrs(Content_Length="10")))
        data, total = GitHubClient("t", opener=opener).download_chunk("/repos/o/r/x", "own", 4, 3)
        self.assertEqual((data, total), (b"456", 10))
        opener, _ = redirecting(loc, Resp(200, b"0123456789", hdrs(Content_Length="10")))
        with self.assertRaises(RelayGitHubError) as error:
            GitHubClient("t", opener=opener).download_chunk(
                "/repos/o/r/x", "own", 512 * 1024 * 1024, 3
            )
        self.assertEqual(error.exception.status, 416)
        opener, _ = redirecting(loc, Resp(206, b"abcdef", hdrs(Content_Range="bytes 0-2/10")))
        with self.assertRaises(RelayGitHubError):
            GitHubClient("t", opener=opener).download_chunk("/repos/o/r/x", "own", 0, 3)
        opener, _ = redirecting(loc, Resp(206, b"abc", hdrs(Content_Range="bytes 5-7/10")))
        with self.assertRaises(RelayGitHubError):
            GitHubClient("t", opener=opener).download_chunk("/repos/o/r/x", "own", 0, 3)


class DL(Client):
    def __init__(self, *replies, chunk=(b"abc", 3)):
        super().__init__(*replies)
        self.chunk, self.dl = chunk, []

    def download_chunk(self, path, bearer, offset, length, accept="x"):
        self.dl.append((path, bearer, offset, length, accept))
        return self.chunk


def handle(action, client):
    return WorkflowVerbs(client, Policy(), "swu-wise", "own", "swu-wise", action, REPO)


class VerbTests(unittest.TestCase):
    def test_asset_download_preread_then_range(self):
        c = DL(ASSET)
        out = handle("wf_release_asset_download", c).run(
            "wf_release_asset_download", {"repo": REPO, "asset_id": 7}
        )
        self.assertEqual(base64.b64decode(out["content_b64"]), b"abc")
        self.assertEqual((out["total"], out["complete"], out["next_offset"]), (3, True, 3))
        self.assertEqual(
            c.dl[0],
            (f"/repos/{REPO}/releases/assets/7", "own", 0, 256 * 1024, "application/octet-stream"),
        )
        for reply in [{**ASSET, "id": 8}, {**ASSET, "state": "open"}]:
            with self.assertRaises(RelayGitHubError):
                handle("wf_release_asset_download", DL(reply)).run(
                    "wf_release_asset_download", {"repo": REPO, "asset_id": 7}
                )

    def test_artifact_and_run_logs(self):
        c = DL({"id": 5, "expired": False}, chunk=(b"PK", 10))
        out = handle("wf_artifact_download", c).run(
            "wf_artifact_download", {"repo": REPO, "artifact_id": 5, "offset": 0, "length": 2}
        )
        self.assertEqual((out["complete"], out["next_offset"], out["artifact_id"]), (False, 2, 5))
        with self.assertRaises(Refused):
            handle("wf_artifact_download", DL({"id": 5, "expired": True})).run(
                "wf_artifact_download", {"repo": REPO, "artifact_id": 5}
            )
        c = DL(chunk=(b"zip", 3))
        out = handle("wf_run_logs", c).run("wf_run_logs", {"repo": REPO, "run_id": 9})
        self.assertEqual(c.dl[0][0], f"/repos/{REPO}/actions/runs/9/logs")
        self.assertEqual(out["run_id"], 9)

    def test_job_log_never_splits_a_character(self):
        data = "ab\u00e9".encode()  # 4 bytes; ask for first 3 of a larger log
        c = DL(chunk=(data[:3], 100))
        out = handle("wf_job_log", c).run("wf_job_log", {"repo": REPO, "job_id": 4})
        self.assertEqual((out["content"], out["next_offset"], out["complete"]), ("ab", 2, False))
        c = DL(chunk=(data, 4))
        out = handle("wf_job_log", c).run("wf_job_log", {"repo": REPO, "job_id": 4})
        self.assertEqual((out["content"], out["complete"]), ("ab\u00e9", True))

    def test_bad_arguments_refused(self):
        for action, args in [
            ("wf_job_log", {"job_id": 1, "url": "https://x"}),
            ("wf_job_log", {"job_id": 1, "length": 262145}),
            ("wf_job_log", {"job_id": 1, "offset": -1}),
            ("wf_run_logs", {"run_id": True}),
        ]:
            with self.assertRaises(Refused):
                handle(action, DL()).run(action, {"repo": REPO, **args})

    def upload(self, content, **extra):
        c = DL(
            {
                "id": 3,
                "upload_url": f"https://uploads.github.com/repos/{REPO}/releases/3/assets{{?name,label}}",
            }
        )
        seen = []
        c.upload_release_asset = lambda *a: seen.append(a) or {**ASSET, "size": len(content)}
        args = {
            "repo": REPO,
            "release_id": 3,
            "name": "a.bin",
            "content_b64": base64.b64encode(content).decode(),
            **extra,
        }
        out = handle("wf_release_asset_upload_binary", c).run(
            "wf_release_asset_upload_binary", args
        )
        return out, seen, c

    def test_binary_upload_and_scan(self):
        out, seen, _ = self.upload(b"\x00\x01\xff\xfe")
        self.assertEqual(seen[0][:5], (REPO, 3, "a.bin", b"\x00\x01\xff\xfe", "own"))
        self.assertEqual(seen[0][5], 180 * 1024)
        bad_zip = io.BytesIO()
        with zipfile.ZipFile(bad_zip, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("x/y.txt", "token " + TOKEN)
        ok_zip = io.BytesIO()
        with zipfile.ZipFile(ok_zip, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("x/y.txt", "fine")
        nested = io.BytesIO()
        with zipfile.ZipFile(nested, "w") as z:
            z.writestr("inner.zip", bad_zip.getvalue())
        self.upload(ok_zip.getvalue())
        refused = [
            b"\x00" + TOKEN.encode(),
            TOKEN.encode("utf-16-le"),
            bad_zip.getvalue(),
            nested.getvalue(),
            gzip.compress(TOKEN.encode()),
            b"\x28\xb5\x2f\xfd" + b"\x00" * 8,  # zstd: not scannable
            b"7z\xbc\xaf\x27\x1c" + b"\x00" * 8,
            b"x" * (180 * 1024 + 1),
            b"",
        ]
        for content in refused:
            with self.assertRaises(Refused, msg=content[:8]):
                self.upload(content)

    def test_zip_bomb_refused(self):
        z = io.BytesIO()
        with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("big", b"\x00" * (9 * 1024 * 1024))
        self.assertLess(len(z.getvalue()), 180 * 1024)
        with self.assertRaises(Refused):
            self.upload(z.getvalue())
        with self.assertRaises(Refused):
            self.upload(gzip.compress(b"\x00" * (9 * 1024 * 1024)))

    def test_zip_expansion_budget_is_cumulative(self):
        from git_relay import workflow_verbs

        z = io.BytesIO()
        with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as zf:
            for i in range(2):
                zf.writestr(f"m{i}", b"\x00" * (5 * 1024 * 1024))
        self.assertLess(len(z.getvalue()), 180 * 1024)
        with self.assertRaises(Refused):
            self.upload(z.getvalue())
        # One budget spans every member and level: two 5 MiB reads cannot both pass.
        budget = workflow_verbs._new_budget()
        workflow_verbs._bounded_read(io.BytesIO(b"\x00" * (5 * 1024 * 1024)), budget)
        with self.assertRaises(Refused):
            workflow_verbs._bounded_read(io.BytesIO(b"\x00" * (5 * 1024 * 1024)), budget)
        # Members count against one shared member budget too.
        many = io.BytesIO()
        with zipfile.ZipFile(many, "w") as zf:
            for i in range(501):
                zf.writestr(f"f{i}", b"x")
        with self.assertRaises(Refused):
            self.upload(many.getvalue())

    def test_corrupt_compressed_members_fail_closed(self):
        z = io.BytesIO()
        with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("x.txt", b"hello world " * 200)
        raw = bytearray(z.getvalue())
        raw[40:48] = b"\xff" * 8  # inside the deflate stream
        with self.assertRaises(Refused):
            self.upload(bytes(raw))
        from git_relay import workflow_verbs

        class Boom:
            def read(self, n=-1):
                raise zlib.error("bad stream")

        ok_zip = io.BytesIO()
        with zipfile.ZipFile(ok_zip, "w") as zf:
            zf.writestr("x.txt", "fine")
        orig = zipfile.ZipFile.open
        zipfile.ZipFile.open = lambda self, *a, **k: Boom_ctx()

        class Boom_ctx:
            def __enter__(self):
                return Boom()

            def __exit__(self, *a):
                return False

        try:
            with self.assertRaises(Refused):
                workflow_verbs._scan_asset_bytes(ok_zip.getvalue())
        finally:
            zipfile.ZipFile.open = orig

    def test_secret_shaped_asset_names_refused(self):
        for action, extra in [
            ("wf_release_asset_upload_binary", {"release_id": 3, "content_b64": "aGk="}),
            ("wf_release_asset_update", {"asset_id": 7, "expected_name": "a.bin"}),
        ]:
            with self.assertRaises(Refused):
                handle(action, DL(ASSET)).run(action, {"repo": REPO, "name": TOKEN, **extra})

    def test_artifact_expired_must_be_exactly_false(self):
        for meta in [
            {"id": 5, "expired": True},
            {"id": 5, "expired": None},
            {"id": 5},
            {"id": 5, "expired": 0},
        ]:
            with self.assertRaises(Refused, msg=meta):
                handle("wf_artifact_download", DL(meta)).run(
                    "wf_artifact_download", {"repo": REPO, "artifact_id": 5}
                )

    def test_job_log_tiny_length_still_advances(self):
        data = "\u00e9b".encode()  # 3 bytes
        for length in (1, 2):
            c = DL(chunk=(data[: length + 3], 100))
            out = handle("wf_job_log", c).run(
                "wf_job_log", {"repo": REPO, "job_id": 4, "length": length}
            )
            self.assertEqual((out["content"], out["next_offset"]), ("\u00e9", 2), length)
            self.assertGreater(c.dl[0][3], length)
        four = "\U0001f600".encode()
        out = handle("wf_job_log", DL(chunk=(four, 100))).run(
            "wf_job_log", {"repo": REPO, "job_id": 4, "length": 1}
        )
        self.assertEqual((out["content"], out["next_offset"]), ("\U0001f600", 4))

    def test_binary_upload_name_and_base64(self):
        c = DL()
        for name, enc in [("../x", "aGk="), ("a.bin", "not base64!"), ("a.bin.", "aGk=")]:
            with self.assertRaises(Refused):
                handle("wf_release_asset_upload_binary", c).run(
                    "wf_release_asset_upload_binary",
                    {"repo": REPO, "release_id": 3, "name": name, "content_b64": enc},
                )

    def test_delete_fenced_on_name_and_not_replayed_by_verb(self):
        c = DL(ASSET, {})
        out = handle("wf_release_asset_delete", c).run(
            "wf_release_asset_delete", {"repo": REPO, "asset_id": 7, "expected_name": "a.bin"}
        )
        self.assertEqual(out, {"deleted": True, "asset_id": 7})
        self.assertEqual(c.calls[1][:2], ("DELETE", f"/repos/{REPO}/releases/assets/7"))
        self.assertEqual(c.calls[1][3], "own")
        c = DL(ASSET)
        with self.assertRaises(Refused):
            handle("wf_release_asset_delete", c).run(
                "wf_release_asset_delete", {"repo": REPO, "asset_id": 7, "expected_name": "other"}
            )
        self.assertEqual(len(c.calls), 1)  # no DELETE sent
        c = DL(ASSET, {"message": "unexpected body"})
        with self.assertRaises(RelayGitHubError):
            handle("wf_release_asset_delete", c).run(
                "wf_release_asset_delete", {"repo": REPO, "asset_id": 7, "expected_name": "a.bin"}
            )

    def test_update_fenced_and_validated(self):
        c = DL(ASSET, {**ASSET, "name": "b.bin"})
        out = handle("wf_release_asset_update", c).run(
            "wf_release_asset_update",
            {"repo": REPO, "asset_id": 7, "expected_name": "a.bin", "name": "b.bin", "label": "L"},
        )
        self.assertEqual(out["asset"]["name"], "b.bin")
        self.assertEqual(
            c.calls[1],
            ("PATCH", f"/repos/{REPO}/releases/assets/7", {"name": "b.bin", "label": "L"}, "own"),
        )
        base = {"repo": REPO, "asset_id": 7, "expected_name": "a.bin"}
        for extra in [
            {},
            {"name": "a/b"},
            {"label": TOKEN},
            {"label": "x" * 256},
            {"state": "open"},
            {"label": "a\nb"},
        ]:
            with self.assertRaises(Refused, msg=extra):
                handle("wf_release_asset_update", DL(ASSET)).run(
                    "wf_release_asset_update", {**base, **extra}
                )
        with self.assertRaises(RelayGitHubError):  # response name mismatch is unverifiable
            handle("wf_release_asset_update", DL(ASSET, ASSET)).run(
                "wf_release_asset_update", {**base, "name": "b.bin"}
            )

    def test_request_bounds_only_binary_upload_is_large(self):
        self.assertEqual(MAX_REQUEST_BYTES, 64 * 1024)
        self.assertGreater(MAX_BINARY_REQUEST_BYTES, 4 * ((180 * 1024 + 2) // 3))


if __name__ == "__main__":
    unittest.main()
