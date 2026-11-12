#!/usr/bin/env python3
"""Names, digests and gates for the Worker secret sync.  No network and no real secret."""

import importlib.util
import io
import pathlib
import unittest
from datetime import UTC, datetime

spec = importlib.util.spec_from_file_location(
    "worker_secret_sync",
    pathlib.Path("scripts/worker_secret_sync.py"),
)
sync = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sync)

NOW = datetime(2026, 11, 12, tzinfo=UTC)
ACCOUNT = "a" * 32
SERVICE = "st." + ("b" * 32)
CLOUDFLARE = "c" * 40
UNIVERSAL = "universal-auth-client-secret-must-not-be-used"
DIGEST_KEY = "k" * 32
AUD = "aud-value-should-not-print"
EXTRA = "extra-value/should.*not-print"
PROJECT = "9bf7417a-fbbb-42ca-870c-2b45207233f5"
FOLDER = "/workers/simple-tools-mcp"


def metadata():
    return {
        "id": "token-id",
        "name": "worker-secret-sync-ro",
        "workspace": PROJECT,
        "scopes": [{"environment": "prod", "secretPath": FOLDER}],
        "permissions": ["read"],
    }


def secret_row(name, value):
    return {
        "type": "shared",
        "secretKey": name,
        "secretValue": value,
        "workspace": PROJECT,
        "environment": "prod",
        "secretPath": FOLDER,
    }


class DiffAndPolicy(unittest.TestCase):
    def test_diff_names_missing_only_and_differ(self):
        desired = {"ACCESS_AUD": "a" * 64, "KEPT": "b" * 64}
        stored = {"KEPT": "b" * 64, "ACCESS_AUD": "c" * 64}
        diff = sync.classify(desired, ["KEPT", "ACCESS_AUD", "LEFTOVER"], stored, ["ACCESS_AUD"])
        self.assertEqual(diff["missing_on_worker"], ())
        self.assertEqual(diff["only_on_worker"], ("LEFTOVER",))
        self.assertEqual(diff["differ"], ("ACCESS_AUD",))
        self.assertEqual(diff["not_allowlisted"], ())

    def test_missing_name_and_absent_digest_are_not_a_match(self):
        desired = {"ACCESS_AUD": "a" * 64, "NEW_NAME": "b" * 64}
        diff = sync.classify(desired, ["ACCESS_AUD"], None, ["ACCESS_AUD", "NEW_NAME"])
        self.assertEqual(diff["missing_on_worker"], ("NEW_NAME",))
        self.assertEqual(diff["only_on_worker"], ())
        self.assertEqual(diff["differ"], ("ACCESS_AUD",))

    def test_matching_digest_is_not_differ(self):
        digest = "ab" * 32
        diff = sync.classify(
            {"ACCESS_AUD": digest}, ["ACCESS_AUD"], {"ACCESS_AUD": digest}, ["ACCESS_AUD"]
        )
        self.assertEqual(diff["differ"], ())
        self.assertFalse(sync.drift(diff))

    def test_allowlist_blocks_puts_and_never_deletes(self):
        diff = {
            "missing_on_worker": ("NEW_NAME", "HIDDEN"),
            "only_on_worker": ("LEFTOVER",),
            "differ": ("ACCESS_AUD", "OTHER"),
            "not_allowlisted": ("HIDDEN", "OTHER"),
        }
        self.assertEqual(
            sync.puts_for_apply(diff, ["ACCESS_AUD", "NEW_NAME"]), ("ACCESS_AUD", "NEW_NAME")
        )
        self.assertEqual(sync.puts_for_apply(diff, []), ())
        self.assertNotIn("LEFTOVER", sync.puts_for_apply(diff, ["LEFTOVER", "ACCESS_AUD"]))

    def test_apply_is_off_unless_the_flag_and_the_exact_variable(self):
        self.assertFalse(sync.apply_enabled({}, False))
        self.assertFalse(sync.apply_enabled({}, True))
        self.assertFalse(sync.apply_enabled({sync.APPLY_ENV: "true"}, False))
        for value in ("TRUE", "True", "1", "yes", " true", "true ", ""):
            self.assertFalse(sync.apply_enabled({sync.APPLY_ENV: value}, True), value)
        self.assertTrue(sync.apply_enabled({sync.APPLY_ENV: "true"}, True))

    def test_digest_changes_with_the_value_and_does_not_contain_it(self):
        one = sync.per_key_digest(DIGEST_KEY, "ACCESS_AUD", AUD)
        two = sync.per_key_digest(DIGEST_KEY, "ACCESS_AUD", AUD + "x")
        same = sync.per_key_digest(DIGEST_KEY, "ACCESS_AUD", AUD)
        self.assertEqual(one, same)
        self.assertNotEqual(one, two)
        self.assertNotIn(AUD, one)
        self.assertEqual(len(one), 64)

    def test_digest_key_is_not_a_worker_secret(self):
        values = {sync.DIGEST_KEY_NAME: DIGEST_KEY, "ACCESS_AUD": AUD}
        digests = sync.desired_digests(values, DIGEST_KEY)
        self.assertEqual(set(digests), {"ACCESS_AUD"})
        diff = sync.classify(digests, [sync.DIGEST_KEY_NAME], None, ["ACCESS_AUD"])
        self.assertEqual(diff["only_on_worker"], (sync.DIGEST_KEY_NAME,))
        self.assertNotIn(
            sync.DIGEST_KEY_NAME, sync.puts_for_apply(diff, [sync.DIGEST_KEY_NAME, "ACCESS_AUD"])
        )

    def test_mask_is_literal_and_the_report_has_names_only(self):
        value = "secret.*value!!"
        text = mask_source = "prefix " + value + " suffix secretXvalueYY"
        masked = sync.mask_text(text, [value])
        self.assertNotIn(value, masked)
        self.assertIn("secretXvalueYY", masked)
        self.assertNotIn(value, sync.mask_text("p@ss/word blew up", ["p@ss/word"]))
        diff = sync.classify({"ACCESS_AUD": "a" * 64}, ["LEFTOVER"], None, [])
        report = sync.render_report(diff)
        self.assertIn("missing-on-worker: ACCESS_AUD", report)
        self.assertIn("only-on-worker: LEFTOVER", report)
        self.assertIn("differ: (none)", report)
        self.assertNotIn(AUD, report)
        self.assertNotIn(mask_source, report)

    def test_partial_infisical_read_raises_without_the_value(self):
        body = {
            "secrets": [secret_row("ACCESS_AUD", AUD)],
            "totalCount": 2,
        }
        with self.assertRaises(sync.SyncError) as caught:
            sync.parse_infisical_folder(body, PROJECT, FOLDER)
        self.assertEqual(str(caught.exception), "infisical-incomplete")
        self.assertNotIn(AUD, str(caught.exception))

    def test_one_hidden_secret_fails_the_whole_folder(self):
        body = {
            "secrets": [
                secret_row("ACCESS_AUD", AUD),
                {"type": "shared", "secretKey": "OTHER", "secretValueHidden": True},
            ],
            "totalCount": 2,
        }
        with self.assertRaises(sync.SyncError) as caught:
            sync.parse_infisical_folder(body, PROJECT, FOLDER)
        self.assertNotIn(AUD, str(caught.exception))

    def test_unresolved_reference_fails_closed(self):
        body = {"secrets": [secret_row("ACCESS_AUD", "${prod.other.ACCESS_AUD}")], "totalCount": 1}
        with self.assertRaises(sync.SyncError) as caught:
            sync.parse_infisical_folder(body, PROJECT, FOLDER)
        self.assertEqual(str(caught.exception), "unresolved-reference")
        self.assertNotIn("${prod", str(caught.exception))

    def test_settings_patch_keeps_secret_values_out_of_the_body(self):
        bindings = [
            {"type": "secret_text", "name": "ACCESS_AUD", "text": AUD},
            {"type": "plain_text", "name": "PUBLIC_HOST", "text": "agents.example"},
            {"type": "durable_object_namespace", "name": "SEAT_GATE", "class_name": "SeatGate"},
        ]
        outgoing = sync.bindings_for_digest_update(bindings, '{"v":1}')
        secret = next(item for item in outgoing if item["name"] == "ACCESS_AUD")
        self.assertEqual(secret, {"name": "ACCESS_AUD", "type": "secret_text"})
        plain = next(item for item in outgoing if item["name"] == "PUBLIC_HOST")
        self.assertEqual(plain["text"], "agents.example")
        digest = next(item for item in outgoing if item["name"] == sync.DIGEST_BINDING)
        self.assertEqual(digest["text"], '{"v":1}')
        self.assertEqual(
            [item["name"] for item in outgoing],
            ["ACCESS_AUD", "PUBLIC_HOST", "SEAT_GATE", sync.DIGEST_BINDING],
        )

    def test_unknown_secret_type_refuses_the_patch(self):
        bindings = [{"type": "secret_blob", "name": "ACCESS_AUD"}]
        with self.assertRaises(sync.SyncError):
            sync.bindings_for_digest_update(bindings, "{}")

    def test_corrupt_digest_record_is_not_treated_as_empty(self):
        body = {
            "success": True,
            "result": {
                "bindings": [{"type": "plain_text", "name": sync.DIGEST_BINDING, "text": "{"}]
            },
        }
        with self.assertRaises(sync.SyncError) as caught:
            sync.parse_stored_digests(body)
        self.assertEqual(str(caught.exception), "digest-record-unreadable")

    def test_paginated_worker_list_is_incomplete(self):
        body = {
            "success": True,
            "result": [{"name": "ACCESS_AUD", "type": "secret_text"}],
            "result_info": {"total_count": 2},
        }
        with self.assertRaises(sync.SyncError) as caught:
            sync.parse_worker_secret_names(body)
        self.assertEqual(str(caught.exception), "cloudflare-incomplete")
        self.assertNotIn("ACCESS_AUD", str(caught.exception))


class CommittedContract(unittest.TestCase):
    def test_one_folder_map_and_the_allowlist_is_names_only(self):
        folders = sync.load_folder_map()
        self.assertEqual(set(folders), {"simple-tools-mcp"})
        row = folders["simple-tools-mcp"]
        self.assertEqual(row["project_id"], PROJECT)
        self.assertEqual(row["environment"], "prod")
        self.assertEqual(row["folder"], FOLDER)
        self.assertEqual(row["token_env"], "WORKER_SECRET_SYNC_INFISICAL_TOKEN")
        names = sync.load_allowlist("simple-tools-mcp")
        self.assertIn("ACCESS_AUD", names)
        self.assertNotIn(sync.DIGEST_KEY_NAME, names)
        self.assertNotIn(sync.DIGEST_BINDING, names)
        raw = pathlib.Path("scripts/worker-secret-allowlists/simple-tools-mcp.json").read_text()
        self.assertNotIn(AUD, raw)
        self.assertNotIn("secretValue", raw)

    def test_workflow_keeps_apply_off_and_the_check_job_cannot_put(self):
        text = pathlib.Path(".github/workflows/worker-deploy.yml").read_text()
        self.assertNotRegex(text, r"WORKER_SECRET_SYNC_APPLY:\s*['\"]true['\"]")
        self.assertIn('if [ "${WORKER_SECRET_SYNC_APPLY:-}" = "true" ]; then', text)
        self.assertIn("vars.WORKER_SECRET_CHECK_ENABLED == 'true'", text)
        self.assertIn("vars.WORKER_DEPLOY_ENABLED == 'true'", text)
        check = text.split("secret-check:", 1)[1]
        run = check.split("run:", 1)[1]
        self.assertNotIn("--apply", run)
        self.assertIn("--strict", run)
        self.assertIn("scripts/worker-secret-folders.json", text)

    def test_sync_source_does_not_select_universal_auth(self):
        text = pathlib.Path("scripts/worker_secret_sync.py").read_text()
        self.assertNotIn("UNIVERSAL_AUTH", text)
        self.assertNotIn("INFISICAL_CLIENT_ID", text)
        self.assertNotIn("INFISICAL_CLIENT_SECRET", text)
        self.assertNotIn("clientSecret", text)


class FakeCloud:
    def __init__(self, infisical_body):
        self.infisical_body = infisical_body
        self.worker_names = ["LEFTOVER", "ACCESS_AUD"]
        self.bindings = [
            {"type": "secret_text", "name": "LEFTOVER", "text": "leftover-should-not-print"},
            {"type": "secret_text", "name": "ACCESS_AUD", "text": AUD},
            {"type": "plain_text", "name": "PUBLIC_HOST", "text": "agents.example"},
        ]
        self.digest = None
        self.puts = []
        self.patches = []
        self.bearers = set()

    def __call__(self, method, url, token, payload, accept_status):
        if token == SERVICE:
            self.bearers.add("service")
        elif token == CLOUDFLARE:
            self.bearers.add("cloudflare")
        else:
            self.bearers.add("other")
        path = url.split("?", 1)[0]
        if path.endswith("/api/v2/service-token"):
            return metadata()
        if path.endswith("/api/v3/secrets/raw"):
            return self.infisical_body
        if path.endswith("/secrets") and method == "GET":
            return {
                "success": True,
                "result": [{"name": name, "type": "secret_text"} for name in self.worker_names],
            }
        if path.endswith("/secrets") and method == "PUT":
            self.puts.append(payload)
            if payload["name"] not in self.worker_names:
                self.worker_names.append(payload["name"])
            return {"success": True, "result": {"name": payload["name"], "type": "secret_text"}}
        if path.endswith("/settings") and method == "GET":
            bindings = [dict(item) for item in self.bindings if item["name"] != sync.DIGEST_BINDING]
            if self.digest is not None:
                bindings.append(
                    {"type": "plain_text", "name": sync.DIGEST_BINDING, "text": self.digest}
                )
            return {"success": True, "result": {"bindings": bindings}}
        if path.endswith("/settings") and method == "PATCH":
            self.patches.append(payload)
            for binding in payload["settings"]["bindings"]:
                if binding["name"] == sync.DIGEST_BINDING:
                    self.digest = binding["text"]
            return {"success": True, "result": {}}
        raise AssertionError(method + " " + path)

    def folder(self):
        return {
            "secrets": [
                secret_row(sync.DIGEST_KEY_NAME, DIGEST_KEY),
                secret_row("ACCESS_AUD", AUD),
                secret_row("EXTRA_UNLISTED", EXTRA),
            ],
            "totalCount": 3,
        }


def run_cli(argv, env, cloud, guard=None):
    stdout, stderr = io.StringIO(), io.StringIO()
    code = sync.cli(
        argv,
        account=ACCOUNT,
        guard=guard or (lambda _name: False),
        env=env,
        transport=cloud,
        now=NOW,
        stdout=stdout,
        stderr=stderr,
    )
    return code, stdout.getvalue(), stderr.getvalue()


class Cli(unittest.TestCase):
    def env(self, apply=None):
        values = {
            "WORKER_SECRET_SYNC_INFISICAL_TOKEN": SERVICE,
            "CLOUDFLARE_API_TOKEN": CLOUDFLARE,
            "INFISICAL_UNIVERSAL_AUTH_CLIENT_ID": "client-id-must-not-be-used",
            "INFISICAL_UNIVERSAL_AUTH_CLIENT_SECRET": UNIVERSAL,
            "INFISICAL_CLIENT_SECRET": UNIVERSAL,
        }
        if apply is not None:
            values[sync.APPLY_ENV] = apply
        return values

    def test_dry_run_lists_names_and_does_not_put(self):
        cloud = FakeCloud(None)
        cloud.infisical_body = cloud.folder()
        code, out, err = run_cli(["simple-tools-mcp", "--apply"], self.env(), cloud)
        self.assertEqual(code, 0, err)
        self.assertEqual(cloud.puts, [])
        self.assertEqual(cloud.patches, [])
        self.assertIn("differ: ACCESS_AUD", out)
        self.assertIn("missing-on-worker: EXTRA_UNLISTED", out)
        self.assertIn("only-on-worker: LEFTOVER", out)
        self.assertIn("not-allowlisted: EXTRA_UNLISTED", out)
        self.assertIn("apply: off", out)
        self.assertNotIn(AUD, out)
        self.assertNotIn(EXTRA, out)
        self.assertNotIn(DIGEST_KEY, out)
        self.assertNotIn(UNIVERSAL, out)
        self.assertNotIn(SERVICE, out)
        self.assertEqual(cloud.bearers, {"service", "cloudflare"})
        self.assertEqual(err, "")

    def test_strict_dry_run_exits_nonzero_on_drift(self):
        cloud = FakeCloud(None)
        cloud.infisical_body = cloud.folder()
        code, _out, err = run_cli(["--strict", "simple-tools-mcp"], self.env(), cloud)
        self.assertEqual(code, 3, err)
        self.assertEqual(cloud.puts, [])

    def test_apply_puts_only_the_allowlisted_name(self):
        cloud = FakeCloud(None)
        cloud.infisical_body = cloud.folder()
        code, out, err = run_cli(["--apply", "simple-tools-mcp"], self.env("true"), cloud)
        self.assertEqual(code, 0, err)
        self.assertEqual([item["name"] for item in cloud.puts], ["ACCESS_AUD"])
        self.assertEqual(cloud.puts[0]["text"], AUD)
        self.assertEqual(cloud.puts[0]["type"], "secret_text")
        self.assertNotIn(AUD, out)
        self.assertNotIn(EXTRA, out)
        self.assertIn("apply: wrote ACCESS_AUD", out)
        self.assertNotIn("apply: wrote EXTRA_UNLISTED", out)
        self.assertNotIn("apply: wrote LEFTOVER", out)
        secrets = [
            binding
            for binding in cloud.patches[0]["settings"]["bindings"]
            if binding["type"] == "secret_text"
        ]
        self.assertTrue(secrets)
        self.assertTrue(all("text" not in binding for binding in secrets))
        self.assertEqual(cloud.bearers, {"service", "cloudflare"})
        self.assertNotIn(UNIVERSAL, out + err)

    def test_guard_trip_puts_nothing(self):
        cloud = FakeCloud(None)
        cloud.infisical_body = cloud.folder()
        code, _out, err = run_cli(
            ["--apply", "simple-tools-mcp"],
            self.env("true"),
            cloud,
            guard=lambda _name: True,
        )
        self.assertEqual(code, 1)
        self.assertIn("guard-tripped", err)
        self.assertEqual(cloud.puts, [])

    def test_partial_read_puts_nothing(self):
        cloud = FakeCloud({"secrets": [secret_row("ACCESS_AUD", AUD)], "totalCount": 2})
        code, out, err = run_cli(["--apply", "simple-tools-mcp"], self.env("true"), cloud)
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("infisical-incomplete", err)
        self.assertNotIn(AUD, err)
        self.assertEqual(cloud.puts, [])
        self.assertNotIn("cloudflare", cloud.bearers)

    def test_missing_token_does_not_open_a_connection(self):
        class Opener:
            def open(self, *_args, **_kwargs):
                raise AssertionError("network")

        stderr = io.StringIO()
        original = sync.urllib.request.build_opener
        sync.urllib.request.build_opener = lambda *_args, **_kwargs: Opener()
        try:
            code = sync.cli(
                ["simple-tools-mcp"],
                account=ACCOUNT,
                guard=lambda _name: False,
                env={"INFISICAL_UNIVERSAL_AUTH_CLIENT_SECRET": UNIVERSAL},
                now=NOW,
                stdout=io.StringIO(),
                stderr=stderr,
            )
        finally:
            sync.urllib.request.build_opener = original
        self.assertEqual(code, 1)
        self.assertIn("service-token-required", stderr.getvalue())
        self.assertNotIn(UNIVERSAL, stderr.getvalue())

    def test_deploy_entry_uses_the_sync_and_stays_off_without_the_variable(self):
        deploy_spec = importlib.util.spec_from_file_location(
            "deploy_mapped_worker",
            pathlib.Path("scripts/deploy-mapped-worker.py"),
        )
        deploy = importlib.util.module_from_spec(deploy_spec)
        deploy_spec.loader.exec_module(deploy)
        cloud = FakeCloud(None)
        cloud.infisical_body = cloud.folder()
        module = deploy.load_secret_sync()
        stdout, stderr = io.StringIO(), io.StringIO()
        code = module.cli(
            ["--apply", "simple-tools-mcp"],
            account=ACCOUNT,
            guard=lambda _name: False,
            env=self.env(),
            transport=cloud,
            now=NOW,
            stdout=stdout,
            stderr=stderr,
        )
        self.assertEqual(code, 0, stderr.getvalue())
        self.assertIn("apply: off", stdout.getvalue())
        self.assertEqual(cloud.puts, [])
        self.assertIn("--secrets", pathlib.Path("scripts/deploy-mapped-worker.py").read_text())


if __name__ == "__main__":
    unittest.main()
