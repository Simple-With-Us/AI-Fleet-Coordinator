"""Synthetic only: no private token, actual HOME, production API, or environment credential."""

import copy
import hashlib
import http.client
import io
import json
import os
import tempfile
import concurrent.futures
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from agent_sync import notes as N
from agent_sync import notes_client as C
from agent_sync import notes_service_reader as R

PROJECT = "00000000-0000-4000-8000-000000000001"
TOKEN_ID = "00000000-0000-4000-8000-000000000002"
ORIGIN = "https://operator.example.test"
TOKEN = ".".join(("st", "synthetic", "reader", "suffix"))
VALUE = "synthetic-notes-key-" + "x" * 32
CONFIG = {"schema": 1, "service_token": TOKEN, "operator_api_url": ORIGIN, "project_id": PROJECT, "token_id": TOKEN_ID, "token_name": "operator-notes-read"}
META = {
    "_id": TOKEN_ID,
    "id": TOKEN_ID,
    "name": "operator-notes-read",
    "workspace": PROJECT,
    "scopes": [{"environment": "prod", "secretPath": "/operator"}],
    "permissions": ["read"],
    "expiresAt": "2027-01-24T20:42:59.026Z",
}
ROW = {
    "secretKey": R.KEY,
    "workspace": PROJECT,
    "environment": "prod",
    "secretPath": "/operator",
    "type": "shared",
    "secretValueHidden": False,
    "secretValue": VALUE,
}


class ReaderTests(unittest.TestCase):
    def setUp(self):
        pins = {k: CONFIG[k] for k in ("project_id", "token_id", "token_name", "operator_api_url")}
        commitment = hashlib.sha256(json.dumps(pins, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        patch = mock.patch.object(R, "DEPLOYMENT_SHA256", commitment)
        patch.start()
        self.addCleanup(patch.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.parent = self.home / ".config/operator-notes"
        self.parent.mkdir(parents=True, mode=0o700)
        self.path = self.parent / "reader.json"
        self.write(CONFIG)

    def write(self, obj):
        self.path.write_text(json.dumps(obj))
        self.path.chmod(0o600)

    def load(self):
        return R.load(self.home)

    def reader(self, meta=None, row=None):
        self.calls = []

        def request(path):
            self.calls.append(path)
            return (
                copy.deepcopy(META if meta is None else meta)
                if len(self.calls) == 1
                else {"secret": copy.deepcopy(ROW if row is None else row)}
            )

        return R.Reader(CONFIG, clock=lambda: 1800000000, request=request)

    def test_private_config(self):
        self.assertEqual(self.load().origin, CONFIG["operator_api_url"])

    def test_absent(self):
        self.path.unlink()
        self.assertIsNone(self.load())

    def test_group_readable(self):
        self.path.chmod(0o640)
        with self.assertRaises(R.ReaderError):
            self.load()

    def test_unsafe_parent(self):
        self.parent.chmod(0o755)
        with self.assertRaises(R.ReaderError):
            self.load()

    def test_symlink(self):
        self.path.rename(self.parent / "other")
        self.path.symlink_to(self.parent / "other")
        with self.assertRaises(R.ReaderError):
            self.load()

    def test_parent_symlink(self):
        self.parent.rename(self.parent.with_name("other"))
        self.parent.symlink_to(self.parent.with_name("other"))
        with self.assertRaises(R.ReaderError):
            self.load()

    def test_hardlink(self):
        os.link(self.path, self.parent / "other")
        with self.assertRaises(R.ReaderError):
            self.load()

    def test_fifo(self):
        self.path.unlink()
        os.mkfifo(self.path, 0o600)
        with self.assertRaises(R.ReaderError):
            self.load()

    def test_oversized(self):
        self.path.write_text("x" * 16385)
        with self.assertRaises(R.ReaderError):
            self.load()

    def test_duplicate(self):
        self.path.write_text('{"schema":1,"schema":1}')
        with self.assertRaises(R.ReaderError):
            self.load()

    def test_schema_and_token(self):
        for change in (
            {"schema": True},
            {"schema": 2},
            {"service_token": "nope"},
            {"service_token": TOKEN + " extra"},
            {"extra": "x"},
        ):
            c = dict(CONFIG)
            c.update(change)
            self.write(c)
            with self.subTest(change=list(change)), self.assertRaises(R.ReaderError):
                self.load()

    def test_url_validation(self):
        for url in (
            "http://bad.test",
            "https://u:p@bad.test",
            "https://good.test/path",
            "https://good.test?q=x",
            "https://good.test#x",
            "https://",

            ORIGIN + "\n",
            ORIGIN + "\t",
            "https://operator.\njays.services",
        ):
            c = dict(CONFIG, operator_api_url=url)
            self.write(c)
            with self.subTest(url=url), self.assertRaises(R.ReaderError):
                self.load()

    def test_exact_point_read(self):
        r = self.reader()
        self.assertEqual(r.key(), VALUE)
        self.assertEqual(self.calls[0], "/v2/service-token")
        self.assertIn("/v3/secrets/raw/" + R.KEY + "?", self.calls[1])
        self.assertIn("include_imports=false", self.calls[1])
        self.assertIn("expandSecretReferences=false", self.calls[1])

    def test_metadata_mismatch_stops_before_key(self):
        for field, value in (
            ("_id", "wrong"),
            ("id", "wrong"),
            ("workspace", "wrong"),
            ("name", "wrong"),
            ("permissions", ["read", "write"]),
            ("permissions", []),
            ("scopes", [{"environment": "prod", "secretPath": "/"}]),
            ("expiresAt", "2000-01-01T00:00:00Z"),
            ("expiresAt", "bad"),
        ):
            m = copy.deepcopy(META)
            m[field] = value
            r = self.reader(meta=m)
            with self.subTest(field=field), self.assertRaises(R.ReaderError):
                r.key()
            self.assertEqual(len(self.calls), 1)

    def test_key_provenance(self):
        for field, value in (
            ("secretKey", "WRONG"),
            ("workspace", "wrong"),
            ("environment", "dev"),
            ("secretPath", "/"),
            ("type", "personal"),
            ("secretValueHidden", True),
            ("secretValueHidden", 0),
            ("secretValue", "${REF}"),
            ("secretValue", "short"),
        ):
            row = dict(ROW)
            row[field] = value
            with self.subTest(field=field), self.assertRaises(R.ReaderError):
                self.reader(row=row).key()

    def test_missing_hidden_is_refused(self):
        row = dict(ROW)
        del row["secretValueHidden"]
        with self.assertRaises(R.ReaderError):
            self.reader(row=row).key()

    def test_no_credential_repr(self):
        self.assertNotIn(TOKEN, repr(self.reader()))

    def test_nonenv_consumer(self):
        r = self.reader()
        with (
            mock.patch.object(C.SR, "load", return_value=r),
            mock.patch.object(C, "base_url", side_effect=AssertionError("legacy URL path")),
            mock.patch.object(
                C, "fleet_secrets_get", side_effect=AssertionError("legacy key path")
            ),
        ):
            c = C.operator_client({})
            self.assertEqual(c.principal, "SERVER")
            self.assertEqual(c.base, CONFIG["operator_api_url"])
            self.assertEqual(c._key(), VALUE)

    def test_bad_config_no_fallback(self):
        with (
            mock.patch.object(C.SR, "load", side_effect=R.ReaderError("reader-config-invalid")),
            mock.patch.object(C, "base_url", side_effect=AssertionError("fallback")),
        ):
            with self.assertRaises(N.NotesError):
                C.operator_client({})

    def test_runner_not_rerouted(self):
        with (
            mock.patch.object(
                C.SR, "load", side_effect=AssertionError("SERVER reader used by RUNNER")
            ),
            mock.patch.object(C, "base_url", return_value=CONFIG["operator_api_url"]),
        ):
            self.assertEqual(C.operator_client({}, "RUNNER").principal, "RUNNER")

    def test_transport_header_only(self):
        r = self.reader()
        seen = []

        class Response(io.BytesIO):
            status = 200

        class Opener:
            def open(self, req, timeout):
                seen.append(req)
                return Response(b"{}")

        with mock.patch.object(R.urllib.request, "build_opener", return_value=Opener()):
            self.assertEqual(r._get("/v2/service-token"), {})
        self.assertEqual(
            seen[0].get_header("Authorization"), "Bearer " + ".".join(TOKEN.split(".")[:3])
        )
        self.assertNotIn(TOKEN, seen[0].full_url)
        self.assertIsNone(seen[0].data)

    def test_transport_bounded(self):
        r = self.reader()

        class Response(io.BytesIO):
            status = 200

        class Opener:
            def open(self, req, timeout):
                return Response(b"x" * (R.MAX_BYTES + 1))

        with (
            mock.patch.object(R.urllib.request, "build_opener", return_value=Opener()),
            self.assertRaisesRegex(R.ReaderError, "oversized"),
        ):
            r._get("/v2/service-token")

    def test_transport_rejects_redirect(self):
        handler = R._NoRedirect()
        self.assertIsNone(handler.redirect_request(None, None, None, None, None, None))

    def test_transport_error_redacted(self):
        r = self.reader()

        class Opener:
            def open(self, req, timeout):
                raise urllib.error.URLError(TOKEN)

        with (
            mock.patch.object(R.urllib.request, "build_opener", return_value=Opener()),
            self.assertRaises(R.ReaderError) as raised,
        ):
            r._get("/v2/service-token")
        self.assertNotIn(TOKEN, str(raised.exception))

    def test_private_metadata_commitment_blocks_other_destination(self):
        config = dict(CONFIG, operator_api_url="https://unqualified.example.test")
        self.write(config)
        with self.assertRaisesRegex(R.ReaderError, "deployment-unqualified"):
            self.load()

    def test_private_metadata_commitment_blocks_other_identity(self):
        for field in ("project_id", "token_id", "token_name"):
            config = dict(CONFIG)
            config[field] = "other-reader" if field == "token_name" else "00000000-0000-4000-8000-000000000099"
            self.write(config)
            with self.subTest(field=field), self.assertRaisesRegex(R.ReaderError, "deployment-unqualified"):
                self.load()

    def test_http_exceptions_open_and_read_are_redacted(self):
        for phase in ("open", "read"):
            for kind in (http.client.BadStatusLine, http.client.IncompleteRead):
                failure = kind(TOKEN if kind is http.client.BadStatusLine else TOKEN.encode())
                class Response(io.BytesIO):
                    status = 200
                    def read(self, size):
                        raise failure
                class Opener:
                    def open(self, req, timeout):
                        if phase == "open":
                            raise failure
                        return Response()
                with self.subTest(phase=phase, kind=kind):
                    reader = self.reader()
                    with mock.patch.object(R.urllib.request, "build_opener", return_value=Opener()):
                        with self.assertRaisesRegex(R.ReaderError, "reader-api-failed") as caught:
                            reader._get("/v2/service-token")
                        self.assertNotIn(TOKEN, str(caught.exception))
                        # The real SERVER adapter must preserve redaction as NotesError.
                        reader._request = reader._get
                        with mock.patch.object(C.SR, "load", return_value=reader):
                            client = C.operator_client({})
                            with self.assertRaises(N.NotesError) as caught:
                                client._key()
                            self.assertNotIn(TOKEN, caught.exception.message)

    def test_cache_one_validation_pair_per_ttl_and_forget(self):
        reader = self.reader()
        for _ in range(50):
            self.assertEqual(reader.key(), VALUE)
        self.assertEqual(len(self.calls), 2)
        reader.forget()
        self.calls.clear()
        self.assertEqual(reader.key(), VALUE)
        self.assertEqual(len(self.calls), 2)

    def test_cache_expiry_does_not_serve_stale_on_failure(self):
        reader = self.reader()
        now = [1800000000.0]
        reader._clock = lambda: now[0]
        self.assertEqual(reader.key(), VALUE)
        reader._monotonic = lambda: reader._cache_started + R.KEY_CACHE_S
        reader._request = lambda path: (_ for _ in ()).throw(R.ReaderError("reader-api-failed"))
        with self.assertRaises(R.ReaderError):
            reader.key()
        self.assertIsNone(reader._cached_key)

    def test_cache_shortened_by_token_expiry(self):
        now = [1800000000.0]
        meta = dict(META, expiresAt="2027-01-15T08:01:00Z")
        # Use a derived time so this assertion is independent of the calendar conversion.
        expiry = R.dt.datetime.fromisoformat(meta["expiresAt"].replace("Z", "+00:00")).timestamp()
        now[0] = expiry - 60
        reader = self.reader(meta=meta)
        reader._clock = lambda: now[0]
        self.assertEqual(reader.key(), VALUE)
        self.assertEqual(reader._cache_expiry, expiry)
        now[0] = expiry - 30
        self.calls.clear()
        with self.assertRaisesRegex(R.ReaderError, "metadata-invalid"):
            reader.key()

    def test_cache_invalidated_by_private_file_change(self):
        reader = self.load()
        reader._cached_key = VALUE
        reader._cache_expiry = 10**12
        self.write(dict(CONFIG, service_token=TOKEN.replace("suffix", "rotated")))
        with self.assertRaisesRegex(R.ReaderError, "config-changed"):
            reader.key()
        self.assertIsNone(reader._cached_key)

    def test_operator_401_forgets_without_retry(self):
        reader = self.reader()
        def failed(req, timeout):
            raise urllib.error.HTTPError(req.full_url, 401, "unauthorized", {}, io.BytesIO(b"{}"))
        with mock.patch.object(C.SR, "load", return_value=reader):
            client = C.operator_client({}, opener=failed)
            self.assertEqual(client.call("POST", "/notes/v1/requests", payload={})[0], 401)
        self.assertIsNone(reader._cached_key)
        self.assertEqual(len(self.calls), 2)

    def test_cache_concurrent_callers_share_one_validation_pair(self):
        reader = self.reader()
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            values = list(pool.map(lambda _: reader.key(), range(32)))
        self.assertEqual(values, [VALUE] * 32)
        self.assertEqual(len(self.calls), 2)

    def test_config_removed_drops_cached_key(self):
        reader = self.load()
        reader._cached_key = VALUE
        reader._cache_expiry = 10**12
        self.path.unlink()
        with self.assertRaisesRegex(R.ReaderError, "config-changed"):
            reader.key()
        self.assertIsNone(reader._cached_key)

    def test_monotonic_ttl_bounds_backward_wall_correction(self):
        reader = self.reader()
        wall, tick = [1800000000.0], [100.0]
        reader._clock = lambda: wall[0]
        reader._monotonic = lambda: tick[0]
        self.assertEqual(reader.key(), VALUE)
        wall[0] -= 3600
        tick[0] += R.KEY_CACHE_S
        reader._request = lambda path: (_ for _ in ()).throw(R.ReaderError("refresh-required"))
        with self.assertRaisesRegex(R.ReaderError, "refresh-required"):
            reader.key()
        self.assertIsNone(reader._cached_key)

    def test_forward_wall_correction_checks_utc_expiry(self):
        reader = self.reader()
        wall, tick = [1800000000.0], [100.0]
        reader._clock = lambda: wall[0]
        reader._monotonic = lambda: tick[0]
        self.assertEqual(reader.key(), VALUE)
        wall[0] = reader._cache_expiry - 30
        self.calls.clear()
        with self.assertRaisesRegex(R.ReaderError, "metadata-invalid"):
            reader.key()
        self.assertIsNone(reader._cached_key)

    def test_monotonic_600_boundary_refreshes(self):
        reader = self.reader()
        tick = [100.0]
        reader._monotonic = lambda: tick[0]
        self.assertEqual(reader.key(), VALUE)
        tick[0] += R.KEY_CACHE_S - 0.001
        self.assertEqual(reader.key(), VALUE)
        self.assertEqual(len(self.calls), 2)
        tick[0] += 0.001
        self.calls.clear()
        self.assertEqual(reader.key(), VALUE)
        self.assertEqual(len(self.calls), 2)

    def test_slow_point_fetch_crossing_expiry_buffer_never_returns_key(self):
        for after in (-30, 10):
            reader = self.reader()
            expiry = R.dt.datetime.fromisoformat(META["expiresAt"].replace("Z", "+00:00")).timestamp()
            wall = [expiry - 60]
            reader._clock = lambda: wall[0]
            request = reader._request
            def slow(path):
                result = request(path)
                if path.startswith("/v3/"):
                    wall[0] = expiry + after
                return result
            reader._request = slow
            with self.subTest(after=after), self.assertRaisesRegex(R.ReaderError, "metadata-expired"):
                reader.key()
            self.assertIsNone(reader._cached_key)
