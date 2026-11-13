"""Run selection uses the actual verifier and JSON transport against synthetic HTTP responses."""

import io
import json
import secrets
import unittest
import uuid
from types import SimpleNamespace
from unittest import mock
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

import fleet_infisical as core
import fleet_infisical_cli as cli


class RunNamedReadTests(unittest.TestCase):
    def setUp(self):
        self.project = str(uuid.uuid4())
        # Fresh synthetic values never authenticate to a real provider.
        self.token = "st." + secrets.token_urlsafe(32)
        self.values = {"ALPHA": secrets.token_urlsafe(24), "BETA": secrets.token_urlsafe(24)}
        self.values["UNREQUESTED"] = "${prod.other.UNRESOLVED}"
        self.env = {
            "HOME": "/synthetic-home",
            "PATH": "/synthetic-path",
            "KEEP": "existing",
            "ALPHA": "old-value",
            core.ENV_PROJECT: self.project,
            core.ENV_TOKEN: self.token,
            core.ENV_WRITE_TOKEN: "st." + secrets.token_urlsafe(32),
        }
        self.metadata = dict(
            workspace=self.project,
            permissions=["read"],
            scopes=[dict(environment="prod", secretPath="/")],
            expiresAt="2099-01-01T00:00:00Z",
        )
        self.requests, self.executions = [], []
        for patch in (
            mock.patch.object(core, "_opener", return_value=SimpleNamespace(open=self.http)),
            mock.patch.object(
                cli.os, "execvpe", side_effect=lambda *args: self.executions.append(args)
            ),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def http(self, request, *, timeout):
        self.assertGreater(timeout, 0)
        self.assertEqual(request.method, "GET")
        self.assertEqual(request.get_header("Authorization"), "Bearer " + self.token)
        parsed = urlsplit(request.full_url)
        base = urlsplit(core.API_BASE)
        self.assertEqual((parsed.scheme, parsed.netloc), (base.scheme, base.netloc))
        endpoint = parsed.path.removeprefix(base.path.rstrip("/"))
        self.requests.append(endpoint)
        if endpoint == "/v2/service-token":
            payload = self.metadata
        else:
            query = parse_qs(parsed.query)
            self.assertEqual(query["workspaceId"], [self.project])
            self.assertEqual(query["environment"], ["prod"])
            self.assertEqual(query["secretPath"], ["/"])
            self.assertEqual(query["viewSecretValue"], ["true"])
            for name in ("expandSecretReferences", "recursive", "include_imports"):
                self.assertEqual(query[name], ["false"])

            def record(name):
                return dict(
                    secretKey=name,
                    secretValue=self.values[name],
                    workspace=self.project,
                    environment="prod",
                    secretPath="/",
                    type="shared",
                )

            if endpoint == "/v3/secrets/raw":
                payload = {"secrets": [record(name) for name in self.values]}
            else:
                name = endpoint.rsplit("/", 1)[-1]
                if name not in self.values:
                    error = HTTPError(
                        request.full_url, 404, "synthetic missing key", {}, io.BytesIO(b"{}")
                    )
                    self.addCleanup(error.close)
                    raise error
                payload = {"secret": record(name)}
        response = io.BytesIO(json.dumps(payload).encode("utf-8"))
        response.status = 200
        return response

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        code = cli.main(["run", *args, "--", "synthetic-command", "argument"], self.env, out, err)
        for value in (self.token, *self.values.values()):
            self.assertNotIn(value, out.getvalue() + err.getvalue())
        return code, out.getvalue(), err.getvalue()

    def test_only_requests_selected_names_and_preserves_child_environment_contract(self):
        original = dict(self.env)
        self.assertEqual(self.run_cli("--only", "BETA,ALPHA,ALPHA"), (0, "", ""))
        self.assertEqual(self.requests[0], "/v2/service-token")
        self.assertCountEqual(self.requests[1:], ["/v3/secrets/raw/ALPHA", "/v3/secrets/raw/BETA"])
        command, argv, child = self.executions[0]
        self.assertEqual((command, argv), ("synthetic-command", ["synthetic-command", "argument"]))
        self.assertEqual(
            {name: child[name] for name in ("ALPHA", "BETA")},
            {name: self.values[name] for name in ("ALPHA", "BETA")},
        )
        self.assertEqual(
            (child["HOME"], child["PATH"], child["KEEP"]),
            ("/synthetic-home", "/synthetic-path", "existing"),
        )
        self.assertFalse(set(core.AUTH_ENV_VARS) & child.keys())
        self.assertNotIn("UNREQUESTED", child)
        self.assertEqual(self.env, original)

    def test_plain_run_still_refuses_an_unresolved_folder_value(self):
        code, out, err = self.run_cli()
        self.assertEqual((code, out), (5, ""))
        self.assertIn("unresolved-reference", err)
        self.assertEqual(self.requests, ["/v2/service-token", "/v3/secrets/raw"])
        self.assertFalse(self.executions)

    def test_only_missing_name_fails_before_exec(self):
        code, out, err = self.run_cli("--only", "ALPHA,MISSING")
        self.assertEqual((code, out), (3, ""))
        self.assertIn("MISSING", err)
        self.assertNotIn("/v3/secrets/raw", self.requests)
        self.assertFalse(self.executions)

    def test_only_unresolved_requested_name_fails_before_exec(self):
        self.assertEqual(self.run_cli("--only", "UNREQUESTED")[0], 5)
        self.assertFalse(self.executions)

    def test_reserved_name_is_refused_before_any_provider_access(self):
        for name in ("PATH", "HOME", "INFISICAL_TOKEN", "LD_PRELOAD"):
            with self.subTest(name=name):
                self.assertEqual(self.run_cli("--only", name)[0], 2)
        self.assertFalse(self.requests)
        self.assertFalse(self.executions)

    def test_wrong_scope_is_refused_before_named_value_access(self):
        self.metadata["scopes"] = [dict(environment="prod", secretPath="/other")]
        self.assertEqual(self.run_cli("--only", "ALPHA")[0], 4)
        self.assertEqual(self.requests, ["/v2/service-token"])
        self.assertFalse(self.executions)


if __name__ == "__main__":
    unittest.main()
