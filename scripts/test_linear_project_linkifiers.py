"""AFC-565:  Linear project codes as linkifiers, from Linear to Zulip and the repo files.

Nothing here touches the network:  Linear, Zulip, Infisical and GitHub are all faked.

Run:  cd scripts && python3 -m unittest test_linear_project_linkifiers
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import sys
import time
import unittest
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import linear_project_linkifiers as L
import zulip_linkifiers as ZL

HERE = Path(__file__).resolve().parent
SECRET = "webhook-secret-long-enough"


def sign(body: bytes, secret: str = SECRET) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def webhook_body(payload: dict, secret: str = SECRET) -> tuple[bytes, str]:
    body = json.dumps(payload).encode("utf-8")
    return body, sign(body, secret)


def project_payload(code="P-BF-6", slug="5afcbc055535", action="create", now=None):
    return {
        "type": "Project",
        "action": action,
        "webhookTimestamp": int(now if now is not None else time.time() * 1000),
        "data": {
            "identifier": code,
            "slugId": slug,
            "url": "https://linear.app/simple-with-us/project/thing-%s" % slug,
        },
    }


class WebhookTests(unittest.TestCase):
    def test_good_signature_returns_the_payload(self):
        body, sig = webhook_body(project_payload())
        payload = L.verify_linear_webhook(body, sig, SECRET, int(time.time() * 1000))
        self.assertEqual(payload["data"]["identifier"], "P-BF-6")

    def test_bad_signature_is_401(self):
        body, _ = webhook_body(project_payload())
        with self.assertRaises(L.WebhookRefused) as caught:
            L.verify_linear_webhook(body, "0" * 64, SECRET, int(time.time() * 1000))
        self.assertEqual((caught.exception.status, caught.exception.reason), (401, "bad_signature"))

    def test_missing_signature_is_401(self):
        body, _ = webhook_body(project_payload())
        with self.assertRaises(L.WebhookRefused) as caught:
            L.verify_linear_webhook(body, None, SECRET, int(time.time() * 1000))
        self.assertEqual(caught.exception.status, 401)

    def test_json_is_parsed_only_after_the_signature_matches(self):
        # Malformed JSON with a bad signature is a signature failure, not a JSON one.
        with self.assertRaises(L.WebhookRefused) as caught:
            L.verify_linear_webhook(b"{not json", "0" * 64, SECRET, int(time.time() * 1000))
        self.assertEqual((caught.exception.status, caught.exception.reason), (401, "bad_signature"))
        # The same body with a good signature is a JSON failure.
        with self.assertRaises(L.WebhookRefused) as caught:
            L.verify_linear_webhook(
                b"{not json", sign(b"{not json"), SECRET, int(time.time() * 1000)
            )
        self.assertEqual((caught.exception.status, caught.exception.reason), (400, "bad_json"))

    def test_stale_timestamp_is_401(self):
        old = project_payload(now=int(time.time() * 1000) - 61_000)
        body, sig = webhook_body(old)
        with self.assertRaises(L.WebhookRefused) as caught:
            L.verify_linear_webhook(body, sig, SECRET, int(time.time() * 1000))
        self.assertEqual(caught.exception.status, 401)
        fresh = project_payload(now=int(time.time() * 1000) - 59_000)
        body, sig = webhook_body(fresh)
        self.assertEqual(
            L.verify_linear_webhook(body, sig, SECRET, int(time.time() * 1000))["action"],
            "create",
        )

    def test_short_secret_refuses_everything_with_503(self):
        body, _ = webhook_body(project_payload())
        sig = sign(body, "short")
        with self.assertRaises(L.WebhookRefused) as caught:
            L.verify_linear_webhook(body, sig, "short", int(time.time() * 1000))
        self.assertEqual(
            (caught.exception.status, caught.exception.reason), (503, "not_configured")
        )


class SlugTests(unittest.TestCase):
    def test_trailing_slug_is_found(self):
        self.assertEqual(
            L.slug_from_url("https://linear.app/simple-with-us/project/rewrite-5afcbc055535"),
            "5afcbc055535",
        )

    def test_anything_else_is_none(self):
        self.assertIsNone(L.slug_from_url("https://linear.app/simple-with-us/project/rewrite"))
        self.assertIsNone(L.slug_from_url("https://linear.app/simple-with-us/project/x-XYZ"))
        self.assertIsNone(L.slug_from_url(None))
        self.assertIsNone(L.slug_from_url(""))


class FakeResp:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, *args):
        return json.dumps(self.payload).encode("utf-8")


class FakeOpener:
    def __init__(self, pages):
        self.pages = list(pages)
        self.requests = []

    def __call__(self, req, timeout=None):
        self.requests.append(req)
        body = json.loads(req.data.decode("utf-8"))
        cursor = body["variables"]["cursor"]
        index = 0 if cursor is None else 1
        return FakeResp(self.pages[index])


def linear_page(nodes, has_next=False, cursor="c1"):
    return {
        "data": {
            "projects": {
                "nodes": nodes,
                "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
            }
        }
    }


class FetchTests(unittest.TestCase):
    def test_pagination_validation_sorting_and_bare_auth(self):
        pages = [
            linear_page(
                [
                    {"identifier": "P-BF-6", "slugId": "5afcbc055535", "name": "Rewrite"},
                    {"identifier": "BF-1", "slugId": "5afcbc055535", "name": "bad code"},
                    {"identifier": "P-AFC-8", "slugId": "NOTHEX", "name": "bad slug"},
                ],
                has_next=True,
            ),
            linear_page([{"identifier": "P-AFC-7", "slugId": "9c28cf61dcc8", "name": "Muster"}]),
        ]
        opener = FakeOpener(pages)
        projects, skipped = L.fetch_projects_with_stats("lin_personal_key", opener=opener)
        self.assertEqual([p.code for p in projects], ["P-AFC-7", "P-BF-6"])
        self.assertEqual(skipped, 2)
        self.assertEqual(len(opener.requests), 2)
        for req in opener.requests:
            self.assertEqual(req.get_header("Authorization"), "lin_personal_key")
        second = json.loads(opener.requests[1].data.decode("utf-8"))
        self.assertEqual(second["variables"]["cursor"], "c1")

    def test_oauth_keys_go_as_bearer(self):
        opener = FakeOpener([linear_page([])])
        L.fetch_projects("lin_oauth_abc", opener=opener)
        self.assertEqual(opener.requests[0].get_header("Authorization"), "Bearer lin_oauth_abc")

    def test_graphql_errors_raise_with_the_message_only(self):
        class BadOpener:
            def __call__(self, req, timeout=None):
                return FakeResp(
                    {"errors": [{"message": "boom"}], "sensitive_blob": "S3CR3T-body-dump"}
                )

        with self.assertRaises(L.LinearError) as caught:
            L.fetch_projects("k", opener=BadOpener())
        self.assertIn("boom", str(caught.exception))
        self.assertNotIn("S3CR3T", str(caught.exception))

    def test_http_errors_raise_with_status_and_message(self):
        def opener(req, timeout=None):
            raise urllib.error.HTTPError(
                req.full_url, 403, "Forbidden", {}, io.BytesIO(b'{"message": "nope"}')
            )

        with self.assertRaises(L.LinearError) as caught:
            L.fetch_projects("k", opener=opener)
        self.assertIn("403", str(caught.exception))
        self.assertIn("nope", str(caught.exception))


class FakeZulip:
    def __init__(self, rules):
        self.rules = [dict(r) for r in rules]
        self.calls: list[tuple] = []
        self.next_id = max([r["id"] for r in rules] or [6000]) + 1

    def linkifiers(self):
        return [dict(r) for r in self.rules]

    def call(self, method, path, data=None):
        self.calls.append((method, path, dict(data or {})))
        if method == "POST" and path == "realm/filters":
            rule = {
                "id": self.next_id,
                "pattern": data["pattern"],
                "url_template": data["url_template"],
            }
            self.next_id += 1
            self.rules.append(rule)
            return {"id": rule["id"]}
        if method == "PATCH" and path.startswith("realm/filters/"):
            rid = int(path.rsplit("/", 1)[-1])
            for rule in self.rules:
                if rule["id"] == rid:
                    rule.update(data)
            return {}
        if method == "PATCH" and path == "realm/linkifiers":
            order = json.loads(data["ordered_linkifier_ids"])
            by_id = {r["id"]: r for r in self.rules}
            self.rules = [by_id[i] for i in order if i in by_id]
            return {}
        raise AssertionError("unexpected call %s %s" % (method, path))

    @property
    def writes(self):
        return [c for c in self.calls if c[0] in ("POST", "PATCH", "PUT", "DELETE")]


def generic_rule():
    pattern = next(p for i, p, _ in ZL.TARGET if i == ZL.GENERIC_ID)
    return {"id": ZL.GENERIC_ID, "pattern": pattern, "url_template": "https://x/{repo}/{id}"}


class SyncTests(unittest.TestCase):
    def test_add_edit_and_noop(self):
        zulip = FakeZulip(
            [
                generic_rule(),
                {
                    "id": 5158,
                    "pattern": "P-BF-6",
                    "url_template": "https://linear.app/simple-with-us/project/old-words-5afcbc055535",
                },
                {
                    "id": 5155,
                    "pattern": "P-AFC-7",
                    "url_template": L.project_url("9c28cf61dcc8"),
                },
            ]
        )
        projects = [
            L.Project("P-BF-6", "5afcbc055535", "Rewrite"),
            L.Project("P-AFC-7", "9c28cf61dcc8", "Muster"),
            L.Project("P-ZZ-1", "aaaaaaaaaaaaaaaa", "New"),
        ]
        result = L.sync_zulip(zulip, projects)
        kinds = {a[2]: a[0] for a in result}
        self.assertEqual(kinds, {"P-BF-6": "edit", "P-ZZ-1": "add"})
        self.assertNotIn("DELETE", [c[0] for c in zulip.calls])
        self.assertEqual(
            [r["id"] for r in zulip.rules][-1], ZL.GENERIC_ID, "the generic rule stays last"
        )
        self.assertEqual(result.ids["P-BF-6"], 5158)
        self.assertIsNotNone(result.ids["P-ZZ-1"])

    def test_rerun_with_nothing_changed_makes_zero_write_calls(self):
        live = [
            generic_rule(),
            {"id": 5158, "pattern": "P-BF-6", "url_template": L.project_url("5afcbc055535")},
        ]
        zulip = FakeZulip(live)
        projects = [L.Project("P-BF-6", "5afcbc055535", "Rewrite")]
        # Generic first means the first sync's restore moves it last;  start it last instead.
        zulip.rules = [zulip.rules[1], zulip.rules[0]]
        result = L.sync_zulip(zulip, projects)
        self.assertEqual(list(result), [])
        self.assertEqual(zulip.writes, [])
        self.assertEqual(result.ids, {"P-BF-6": 5158})

    def test_non_p_rules_are_never_touched_and_nothing_is_deleted(self):
        other = {"id": 5117, "pattern": "SWU/(?P<slug>x)", "url_template": "https://s/{slug}"}
        zulip = FakeZulip([generic_rule(), dict(other)])
        L.sync_zulip(zulip, [L.Project("P-BF-6", "5afcbc055535", "Rewrite")])
        self.assertIn(other, zulip.rules)
        self.assertNotIn("DELETE", [c[0] for c in zulip.calls])
        for method, path, data in zulip.calls:
            if path.startswith("realm/filters"):
                self.assertTrue(data["pattern"].startswith("P-"))

    def test_order_is_restored_when_a_call_fails(self):
        zulip = FakeZulip(
            [generic_rule(), {"id": 6001, "pattern": "X", "url_template": "https://x"}]
        )
        calls = []
        real = zulip.call

        def failing(method, path, data=None):
            calls.append((method, path))
            if path == "realm/filters":
                raise RuntimeError("zulip is down")
            return real(method, path, data)

        zulip.call = failing  # type: ignore[method-assign]
        with self.assertRaises(RuntimeError):
            L.sync_zulip(zulip, [L.Project("P-BF-6", "5afcbc055535", "Rewrite")])
        self.assertIn(("PATCH", "realm/linkifiers"), calls)


def target_of(text):
    namespace: dict = {}
    exec(compile(text, "<zulip_linkifiers>", "exec"), namespace)  # noqa: S102
    return namespace["TARGET"]


class RewriteTargetTests(unittest.TestCase):
    def setUp(self):
        self.text = (HERE / "zulip_linkifiers.py").read_text(encoding="utf-8")
        self.projects = [
            L.Project("P-BF-6", "5afcbc055535", "The BotFleet native macOS rewrite"),
            L.Project("P-ZZ-1", "aaaaaaaaaaaaaaaa", 'A "quoted"\n# name */ with \\ bits'),
        ]
        self.ids = {"P-BF-6": 5158, "P-ZZ-1": None}

    def test_generated_block_is_id_only_and_keeps_unknown_codes(self):
        out = L.rewrite_target(self.text, self.projects, self.ids)
        self.assertIn(L.TARGET_BEGIN, out)
        self.assertIn(L.TARGET_END, out)
        self.assertNotIn("# Linear projects, one literal rule each", out)
        rows = {code: (i, url) for i, code, url in target_of(out) if code.startswith("P-")}
        self.assertEqual(rows["P-BF-6"], (5158, L.project_url("5afcbc055535")))
        self.assertEqual(rows["P-ZZ-1"], (None, L.project_url("aaaaaaaaaaaaaaaa")))
        # Kept:  a code Linear no longer lists stays with its old row.
        self.assertIn("P-AFC-7", rows)
        before = {code: url for _, code, url in target_of(self.text)}
        self.assertEqual(rows["P-AFC-7"][1], before["P-AFC-7"])
        # Before the generic fallback, in (team, number) order.
        target = target_of(out)
        codes = [c for _, c, _ in target if c.startswith("P-")]
        self.assertEqual(codes, sorted(codes, key=L._code_sort_key))
        self.assertLess(
            max(target.index(r) for r in target if r[1].startswith("P-")),
            next(i for i, r in enumerate(target) if r[0] == ZL.GENERIC_ID),
        )

    def test_idempotent(self):
        once = L.rewrite_target(self.text, self.projects, self.ids)
        self.assertEqual(L.rewrite_target(once, self.projects, self.ids), once)

    def test_rows_fit_or_wrap(self):
        out = L.rewrite_target(self.text, self.projects, self.ids)
        block = out.split(L.TARGET_BEGIN)[1].split(L.TARGET_END)[0]
        for line in block.splitlines():
            self.assertLessEqual(len(line), 100)
        self.assertIn('(5158, "P-BF-6", "%s"),' % L.project_url("5afcbc055535"), block)


class RewritePathsTests(unittest.TestCase):
    def setUp(self):
        self.text = (HERE / "apple-notes-coding.sh").read_text(encoding="utf-8")
        self.projects = [
            L.Project("P-BF-6", "5afcbc055535", "The BotFleet native macOS rewrite"),
            L.Project("P-ZZ-1", "aaaaaaaaaaaaaaaa", 'A "quoted"\n# name */ with \\ bits'),
        ]

    def paths_of(self, text):
        import ast

        start = text.index("PROJECT_PATHS = {")
        depth = 0
        for offset, char in enumerate(text[start:]):
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return ast.literal_eval(
                        ast.parse(text[start : start + offset + 1]).body[0].value
                    )
        raise AssertionError("unclosed")

    def test_id_only_paths_and_kept_codes(self):
        out = L.rewrite_project_paths(self.text, self.projects)
        self.assertIn(L.TARGET_BEGIN, out)
        paths = self.paths_of(out)
        self.assertEqual(paths["P-BF-6"], "5afcbc055535")
        self.assertEqual(paths["P-ZZ-1"], "aaaaaaaaaaaaaaaa")
        self.assertEqual(paths["P-AFC-7"], self.paths_of(self.text)["P-AFC-7"])
        codes = list(paths)
        self.assertEqual(codes, sorted(codes, key=L._code_sort_key))

    def test_braces_and_shell_characters_in_a_name_cannot_break_the_block(self):
        hostile = [L.Project("P-ZZ-2", "bbbbbbbbbbbb", "Ops } { ${HOME} `id` $(x) PY")]
        once = L.rewrite_project_paths(self.text, self.projects + hostile)
        line = next(line for line in once.splitlines() if "which the fleet calls P-ZZ-2" in line)
        for bad in ("{", "}", "$", "`"):
            self.assertNotIn(bad, line)
        paths = self.paths_of(once)
        self.assertEqual(paths["P-ZZ-2"], "bbbbbbbbbbbb")
        self.assertEqual(paths["P-BF-6"], "5afcbc055535")
        self.assertEqual(L.rewrite_project_paths(once, self.projects + hostile), once)

    def test_the_rewritten_script_still_parses_as_shell(self):
        # PROJECT_PATHS sits inside a single-quoted python3 -c string:  one apostrophe in a
        # generated line would end that string and break the whole Notes script.
        import shutil
        import subprocess
        import tempfile

        bash = shutil.which("bash")
        if bash is None:
            self.skipTest("no bash")
        hostile = [L.Project("P-ZZ-3", "cccccccccccc", 'Jay\'s "quoted" } project')]
        out = L.rewrite_project_paths(self.text, self.projects + hostile)
        with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as handle:
            handle.write(out)
        proc = subprocess.run([bash, "-n", handle.name], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_hostile_names_are_neutralized(self):
        out = L.rewrite_project_paths(self.text, self.projects)
        line = next(line for line in out.splitlines() if "which the fleet calls P-ZZ-1" in line)
        self.assertTrue(line.startswith("    #  "))
        name = line.split("    #  ", 1)[1].split(", which the fleet calls")[0]
        for bad in ('"', "'", "#", "\\", "*/", "\n"):
            self.assertNotIn(bad, name)
        self.assertIn("A quoted", name)

    def test_idempotent(self):
        once = L.rewrite_project_paths(self.text, self.projects)
        self.assertEqual(L.rewrite_project_paths(once, self.projects), once)


class ParityAfterRewriteTests(unittest.TestCase):
    def test_parity_helpers_pass_on_rewritten_texts(self):
        import re

        target_text = (HERE / "zulip_linkifiers.py").read_text(encoding="utf-8")
        shell_text = (HERE / "apple-notes-coding.sh").read_text(encoding="utf-8")
        projects = [
            L.Project(code, slug, "Name %s" % code)
            for code, slug in [
                ("P-AFC-7", "9c28cf61dcc8"),
                ("P-BF-6", "5afcbc055535"),
                ("P-ZZ-9", "bbbbbbbbbbbb"),
            ]
        ]
        ids = {"P-AFC-7": 5155, "P-BF-6": 5158, "P-ZZ-9": None}
        new_target = L.rewrite_target(target_text, projects, ids)
        new_shell = L.rewrite_project_paths(shell_text, projects)
        rows = {code: (i, url) for i, code, url in target_of(new_target) if code.startswith("P-")}

        def paths_of(text):
            import ast

            start = text.index("PROJECT_PATHS = {")
            depth = 0
            for offset, char in enumerate(text[start:]):
                if char == "{":
                    depth += 1
                elif char == "}":
                    depth -= 1
                    if depth == 0:
                        end = start + offset + 1
                        break
            return ast.literal_eval(ast.parse(text[start:end]).body[0].value)

        paths = paths_of(new_shell)
        self.assertEqual(set(rows), set(paths))
        host = re.search(r'_env\("NOTES_LINKIFY_LINEAR_HOST", "([^"]+)"\)', new_shell).group(1)
        workspace = re.search(r'_env\("NOTES_LINEAR_WORKSPACE", "([^"]+)"\)', new_shell).group(1)
        for code, (_, template) in sorted(rows.items()):
            self.assertEqual(template, "https://%s/%s/project/%s" % (host, workspace, paths[code]))


def infisical_http(record, secrets, token="service-token-value"):
    seen = {}

    def http(method, url, headers=None, body=None):
        seen.setdefault("calls", []).append((method, url, dict(headers or {})))
        assert headers and headers.get("Authorization") == "Bearer " + token
        if url.endswith("/v2/service-token"):
            return 200, record
        if "/v3/secrets/raw" in url:
            assert "workspaceId=ws-1" in url
            assert "environment=prod" in url
            assert "secretPath=%2Foperator-linkifiers" in url
            return 200, secrets
        raise AssertionError(url)

    http.seen = seen
    return http


def good_record(**overrides):
    record = {
        "permissions": ["read"],
        "scopes": [{"environment": "prod", "secretPath": "/operator-linkifiers"}],
        "workspace": "ws-1",
    }
    record.update(overrides)
    return record


class SecretsTests(unittest.TestCase):
    def test_returns_only_allowlisted_names(self):
        secrets = {
            "secrets": [
                {"secretKey": name, "secretValue": "v-" + name}
                for name in (*L.SECRET_NAMES, "EXTRA")
            ]
        }
        found = L.load_secrets("service-token-value", http=infisical_http(good_record(), secrets))
        self.assertEqual(set(found), set(L.SECRET_NAMES))
        self.assertEqual(found["LINEAR_API_KEY"], "v-LINEAR_API_KEY")

    def test_missing_names_stay_absent(self):
        secrets = {"secrets": [{"secretKey": "LINEAR_API_KEY", "secretValue": "k"}]}
        found = L.load_secrets("service-token-value", http=infisical_http(good_record(), secrets))
        self.assertEqual(found, {"LINEAR_API_KEY": "k"})

    def test_write_tokens_are_refused(self):
        http = infisical_http(
            good_record(permissions=["read", "write"]), {"secrets": []}, token="tok"
        )
        with self.assertRaises(L.SecretsRefused) as caught:
            L.load_secrets("tok", http=http)
        self.assertEqual(caught.exception.reason, "token_not_read_only")

    def test_wrong_scope_is_refused(self):
        for scopes in (
            [{"environment": "prod", "secretPath": "/other"}],
            [{"environment": "dev", "secretPath": "/operator-linkifiers"}],
            [
                {"environment": "prod", "secretPath": "/operator-linkifiers"},
                {"environment": "prod", "secretPath": "/operator"},
            ],
        ):
            with self.subTest(scopes=scopes):
                http = infisical_http(good_record(scopes=scopes), {"secrets": []}, token="tok")
                with self.assertRaises(L.SecretsRefused) as caught:
                    L.load_secrets("tok", http=http)
                self.assertEqual(caught.exception.reason, "token_scope_mismatch")

    def test_token_file_refuses_group_readable_files(self):
        import os
        import tempfile

        fd, path = tempfile.mkstemp()
        try:
            os.write(fd, b"tok")
            os.close(fd)
            os.chmod(path, 0o640)
            with self.assertRaises(L.SecretsRefused) as caught:
                L.read_token_file(path)
            self.assertEqual(caught.exception.reason, "token_file_permissions")
            os.chmod(path, 0o600)
            self.assertEqual(L.read_token_file(path), "tok")
        finally:
            os.unlink(path)


GITHUB_TOKEN = "github-token-value"


class GitHubFake:
    def __init__(self, main_files, branch_exists=False, open_prs=None, fail=None):
        self.main_files = dict(main_files)
        self.branch_exists = branch_exists
        self.open_prs = open_prs if open_prs is not None else []
        self.fail = fail or {}
        self.requests = []

    def __call__(self, method, url, headers=None, body=None):
        assert headers["Authorization"] == "Bearer " + GITHUB_TOKEN
        assert headers["Accept"] == "application/vnd.github+json"
        assert headers["X-GitHub-Api-Version"] == "2022-11-28"
        assert headers["User-Agent"] == "operator-linkifiers"
        self.requests.append((method, url, json.loads(body) if body else None))
        base = "https://api.github.com/repos/" + L.REPO
        if url == base + "/git/ref/heads/main":
            return 200, {"object": {"sha": "main-sha"}}
        if "/contents/" in url:
            path = url.split("/contents/")[1].split("?")[0]
            import urllib.parse

            path = urllib.parse.unquote(path)
            if path not in self.main_files:
                return 404, {"message": "Not Found"}
            text = self.main_files[path]
            return 200, {"content": base64.b64encode(text.encode()).decode(), "encoding": "base64"}
        if url.startswith(base + "/git/ref/heads/operator/linear-projects-"):
            if self.branch_exists:
                return 200, {"object": {"sha": "branch-sha"}}
            return 404, {"message": "Not Found"}
        if url.startswith(base + "/pulls?"):
            return 200, self.open_prs
        if url == base + "/git/blobs":
            return 201, {"sha": "blob-%d" % len(self.requests)}
        if url == base + "/git/commits/main-sha":
            return 200, {"tree": {"sha": "base-tree"}}
        if url == base + "/git/trees":
            return 201, {"sha": "new-tree"}
        if url == base + "/git/commits":
            data = json.loads(body)
            assert data["parents"] == ["main-sha"]
            return 201, {"sha": "new-commit"}
        if url == base + "/git/refs":
            return 201, {}
        if url == base + "/pulls":
            data = json.loads(body)
            assert data["draft"] is False
            return 201, {"html_url": "https://github.com/x/pr/1", **data}
        if url in self.fail:
            status, payload = self.fail[url]
            return status, payload
        raise AssertionError("unexpected %s %s" % (method, url))


class PrTests(unittest.TestCase):
    def test_noop_when_unchanged(self):
        files = {L.TARGET_PATH: "t", L.PROJECT_PATHS_PATH: "p"}
        fake = GitHubFake(files)
        codes = [L.Project("P-BF-6", "5afcbc055535", "R")]
        self.assertIsNone(L.open_sync_pr(GITHUB_TOKEN, files, codes, http=fake))
        posts = [r for r in fake.requests if r[0] == "POST"]
        self.assertEqual(posts, [])

    def test_existing_branch_reuses_its_open_pr(self):
        pr = {"html_url": "https://github.com/x/pr/9", "number": 9}
        fake = GitHubFake(
            {L.TARGET_PATH: "old-t", L.PROJECT_PATHS_PATH: "old-p"},
            branch_exists=True,
            open_prs=[pr],
        )
        files = {L.TARGET_PATH: "new-t", L.PROJECT_PATHS_PATH: "old-p"}
        out = L.open_sync_pr(
            GITHUB_TOKEN, files, [L.Project("P-BF-6", "5afcbc055535", "R")], http=fake
        )
        self.assertEqual(out, pr)
        posts = [r for r in fake.requests if r[0] == "POST"]
        self.assertEqual(posts, [])

    def test_creates_one_commit_and_a_ready_pr(self):
        fake = GitHubFake({L.TARGET_PATH: "old-t", L.PROJECT_PATHS_PATH: "old-p"})
        files = {L.TARGET_PATH: "new-t", L.PROJECT_PATHS_PATH: "new-p"}
        codes = [L.Project("P-BF-6", "5afcbc055535", "R")]
        out = L.open_sync_pr(GITHUB_TOKEN, files, codes, http=fake)
        commits = [r for r in fake.requests if r[0] == "POST" and r[1].endswith("/git/commits")]
        pulls = [r for r in fake.requests if r[0] == "POST" and r[1].endswith("/pulls")]
        self.assertEqual(len(commits), 1)
        self.assertEqual(len(pulls), 1)
        self.assertIn("[AFC-565]", commits[0][2]["message"])
        self.assertIn("P-BF-6", pulls[0][2]["title"])
        body = pulls[0][2]["body"]
        self.assertIn("AFC-565", body)
        self.assertIn("ID-only", body)
        self.assertIn("P-BF-6 -> %s" % L.project_url("5afcbc055535"), body)
        self.assertIn("\u00a0 ", body)
        self.assertNotIn("&nbsp;", body)
        self.assertNotIn("  ", body.replace("\u00a0 ", ""))
        self.assertEqual(out["html_url"], "https://github.com/x/pr/1")
        ref = next(r for r in fake.requests if r[1].endswith("/git/refs"))
        self.assertTrue(ref[2]["ref"].startswith("refs/heads/operator/linear-projects-"))
        suffix = ref[2]["ref"].rsplit("-", 1)[-1]
        self.assertRegex(suffix, r"^[0-9a-f]{10}$")

    def test_branch_name_is_deterministic(self):
        files = {L.TARGET_PATH: "t", L.PROJECT_PATHS_PATH: "p"}
        self.assertEqual(L._branch_name(files), L._branch_name(dict(reversed(list(files.items())))))
        self.assertNotEqual(L._branch_name(files), L._branch_name({L.TARGET_PATH: "other"}))

    def test_errors_carry_status_and_message_but_never_the_token(self):
        def http(method, url, headers=None, body=None):
            if url.endswith("/git/ref/heads/main"):
                return 500, {"message": "backend oops"}
            raise AssertionError(url)

        with self.assertRaises(L.GitHubError) as caught:
            L.open_sync_pr(GITHUB_TOKEN, {L.TARGET_PATH: "t"}, [], http=http)
        self.assertIn("500", str(caught.exception))
        self.assertIn("backend oops", str(caught.exception))
        self.assertNotIn(GITHUB_TOKEN, str(caught.exception))


class ReconcileTests(unittest.TestCase):
    def test_write_files_rewrites_a_local_checkout(self):
        import tempfile

        target_text = (HERE / "zulip_linkifiers.py").read_text(encoding="utf-8")
        shell_text = (HERE / "apple-notes-coding.sh").read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "scripts").mkdir()
            (root / L.TARGET_PATH).write_text(target_text, encoding="utf-8")
            (root / L.PROJECT_PATHS_PATH).write_text(shell_text, encoding="utf-8")
            projects = [L.Project("P-BF-6", "5afcbc055535", "Rewrite")]
            summary = L.reconcile(
                {"LINEAR_API_KEY": "k"},
                write_files=True,
                repo_root=root,
                fetch_fn=lambda key: (projects, 1),
            )
            self.assertEqual(summary["codes"], ["P-BF-6"])
            self.assertEqual(summary["skipped"], 1)
            self.assertIn("project/5afcbc055535", (root / L.TARGET_PATH).read_text())
            self.assertIn('"P-BF-6": "5afcbc055535"', (root / L.PROJECT_PATHS_PATH).read_text())

    def test_pr_comes_from_github_main_not_the_checkout(self):
        target_text = (HERE / "zulip_linkifiers.py").read_text(encoding="utf-8")
        shell_text = (HERE / "apple-notes-coding.sh").read_text(encoding="utf-8")
        fake = GitHubFake({L.TARGET_PATH: target_text, L.PROJECT_PATHS_PATH: shell_text})
        projects = [L.Project("P-BF-6", "5afcbc055535", "Rewrite")]
        summary = L.reconcile(
            {"LINEAR_API_KEY": "k", "GITHUB_TOKEN": GITHUB_TOKEN},
            open_pr=True,
            http=fake,
            fetch_fn=lambda key: (projects, 0),
        )
        self.assertTrue(summary["pr_url"].startswith("https://github.com/"))
        blobs = [r for r in fake.requests if r[1].endswith("/git/blobs")]
        self.assertEqual(len(blobs), 2)
        self.assertIn("BEGIN linear-projects", blobs[0][2]["content"])


if __name__ == "__main__":
    unittest.main()
