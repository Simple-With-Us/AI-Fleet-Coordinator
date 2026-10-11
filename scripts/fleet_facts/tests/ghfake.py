"""A scripted `gh` for the F7 tests:  an in-memory private repository behind the same argv the publisher uses.

It answers `repo view`, the paged `api` listings (webhooks, deploy keys, the organization's app installations and the
repositories of a selected installation) and the contents API (GET with a blob sha, PUT from an `--input` file), and it
records every call.  A PUT body is read from the file `--input` names, so a test can see what was sent and that it was
not on the command line.  Nothing here is real:  the repository is `example-org/example-data`.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os

from fleet_facts.runner import Result

REPO = "example-org/example-data"


def blob_sha(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\x00" % len(data) + data).hexdigest()


def pages(items: list, key: str | None, per_page: int = 2) -> str:
    """What `gh api --paginate` prints:  one JSON document per page, one after another."""
    chunks = [items[i:i + per_page] for i in range(0, max(len(items), 1), per_page)] or [[]]
    docs = [({key: chunk, "total_count": len(items)} if key else chunk) for chunk in chunks]
    return "\n".join(json.dumps(d) for d in docs)


class FakeGitHub:
    def __init__(self, repo: str = REPO) -> None:
        self.repo = repo
        self.visibility = "PRIVATE"
        self.archived = False
        self.fork = False
        self.hooks: list = []
        self.keys: list = []
        self.hooks_error = False
        self.keys_error = False
        self.installs: list[dict] = []                 # {"id": 1, "app_slug": "x", "repository_selection": "all"|"selected"}
        self.installs_error = False
        self.selected: dict[int, list[str] | None] = {}    # installation id -> repository full names (None = unreadable)
        self.files: dict[str, bytes] = {}
        self.put_fail: dict[str, list[str]] = {}       # path -> HTTP codes to fail with, one per attempt
        self.get_fail: dict[str, list[str]] = {}
        self.calls: list[list[str]] = []
        self.puts: list[dict] = []                     # {"path", "body", "argv", "mode"}
        self.repo_view_error = False

    # -- the Runner interface -------------------------------------------------------------------------
    def __call__(self, argv, *, timeout, env=None, cwd=None) -> Result:
        self.calls.append(list(argv))
        args = list(argv[1:])
        if args[:2] == ["repo", "view"]:
            if self.repo_view_error:
                return Result(1, "", "gh: Could not resolve to a Repository (HTTP 404)")
            return Result(0, json.dumps({"visibility": self.visibility, "isArchived": self.archived, "isFork": self.fork}), "")
        if args[:1] != ["api"]:
            return Result(1, "", "fake gh: unexpected command")
        endpoint = next((a for a in args[1:] if a.startswith(("repos/", "orgs/", "user/"))), "")
        # Like the real gh:  a field (-f or -F) turns the request into a POST unless --method GET is given, and the listing
        # endpoints answer a POST with 404.
        method = args[args.index("--method") + 1] if "--method" in args else (args[args.index("-X") + 1] if "-X" in args else None)
        if method is None and any(a in ("-f", "-F", "--input") for a in args):
            method = "POST"
        if method not in (None, "GET", "PUT"):
            return Result(1, "", "gh: Not Found (HTTP 404)")
        if endpoint == "repos/%s/hooks" % self.repo:
            return Result(1, "", "gh: Must have admin rights to Repository. (HTTP 403)") if self.hooks_error else Result(0, pages(self.hooks, None), "")
        if endpoint == "repos/%s/keys" % self.repo:
            return Result(1, "", "gh: Not Found (HTTP 404)") if self.keys_error else Result(0, pages(self.keys, None), "")
        if endpoint == "orgs/%s/installations" % self.repo.split("/")[0]:
            return Result(1, "", "gh: Resource not accessible (HTTP 403)") if self.installs_error else Result(0, pages(self.installs, "installations"), "")
        if endpoint.startswith("user/installations/") and endpoint.endswith("/repositories"):
            install_id = int(endpoint.split("/")[2])
            names = self.selected.get(install_id)
            if names is None:
                return Result(1, "", "gh: Resource not accessible by personal access token (HTTP 403)")
            return Result(0, pages([{"full_name": n} for n in names], "repositories"), "")
        prefix = "repos/%s/contents/" % self.repo
        if endpoint.startswith(prefix):
            rel = endpoint[len(prefix):]
            if method == "PUT":
                return self._put(rel, args)
            return self._get(rel)
        return Result(1, "", "fake gh: unknown endpoint")

    def _get(self, rel: str) -> Result:
        failing = self.get_fail.get(rel)
        if failing:
            code = failing.pop(0)
            return Result(1, "", "gh: failure (HTTP %s)" % code)
        if rel not in self.files:
            return Result(1, "", "gh: Not Found (HTTP 404)")
        return Result(0, json.dumps({"type": "file", "path": rel, "sha": blob_sha(self.files[rel])}), "")

    def _put(self, rel: str, args: list[str]) -> Result:
        path = args[args.index("--input") + 1]
        mode = os.stat(path).st_mode & 0o777
        with open(path, encoding="utf-8") as fh:
            body = json.load(fh)
        self.puts.append({"path": rel, "body": body, "argv": list(args), "mode": mode, "file": path})
        failing = self.put_fail.get(rel)
        if failing:
            code = failing.pop(0)
            return Result(1, "", "gh: failure (HTTP %s)" % code)
        current = blob_sha(self.files[rel]) if rel in self.files else None
        if body.get("sha") != current:
            return Result(1, "", "gh: %s does not match (HTTP 409)" % rel)
        self.files[rel] = base64.b64decode(body["content"])
        return Result(0, json.dumps({"content": {"sha": blob_sha(self.files[rel])}}), "")

    # -- helpers for the tests -------------------------------------------------------------------------
    def put_paths(self) -> list[str]:
        return [p["path"] for p in self.puts]

    def written(self, rel: str) -> bytes:
        return self.files[rel]

    def write_calls(self) -> list[list[str]]:
        return [c for c in self.calls if "-X" in c or "--method" in c and c[c.index("--method") + 1] != "GET"]
