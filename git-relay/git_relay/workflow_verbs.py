"""Inactive read, search and workflow verbs for a PR-backlog seat.

Not registered in server startup, policy action lists or the Worker. A broker that has
already authenticated a seat constructs it through ``for_action``. It never accepts a
caller token, URL, raw query string or arbitrary path: every argument is validated into
a fixed REST path. Each verb asks the existing minter for only the permissions it needs
and is refused when the seat's ceiling does not cover them. Text returned by GitHub is
untrusted data. No credential leaves this module.

Verbs (action names carry the wf_ prefix so they cannot collide with registered names):
  reads   wf_pr_list wf_pr_files wf_file_get wf_tree wf_branches wf_commits wf_code_search
          wf_runs wf_run_jobs
  writes  wf_update_branch (atomic expected_head_sha), wf_run_rerun, wf_run_cancel,
          wf_run_approve, wf_file_put (non-default branch only), wf_merge_guarded
          (atomic expected_head_sha)
Check-run reads need the checks permission, which is outside the shared ceiling; run and
job conclusions (actions:read) are the check source here, so required-check state beyond
workflow jobs is not covered. Job logs are zip redirects the client refuses to follow, so
only job and step conclusions are returned.
"""

import base64
import binascii
import re

from .secret_scan import find_secret
from .server import _perm_within_ceiling


class Refused(ValueError):
    """Fixed public refusal; never wraps upstream text."""


class Unknown(RuntimeError):
    """Own App credential unavailable."""


_SHA = re.compile(r"[0-9a-f]{40}")
_REF = re.compile(r"[A-Za-z0-9._/-]{1,200}")
MAX_FILE_B64 = 256 * 1024
MAX_PAGE = 50
MAX_PROJECT = 100

# action -> (permissions needed, writes)
SPECS = {
    "wf_pr_list": ({"pull_requests": "read"}, False),
    "wf_pr_files": ({"pull_requests": "read"}, False),
    "wf_file_get": ({"contents": "read"}, False),
    "wf_tree": ({"contents": "read"}, False),
    "wf_branches": ({"contents": "read"}, False),
    "wf_commits": ({"contents": "read"}, False),
    "wf_code_search": ({"contents": "read"}, False),
    "wf_runs": ({"actions": "read"}, False),
    "wf_run_jobs": ({"actions": "read"}, False),
    "wf_update_branch": ({"pull_requests": "write", "contents": "write"}, True),
    "wf_run_rerun": ({"actions": "write"}, True),
    "wf_run_cancel": ({"actions": "write"}, True),
    "wf_run_approve": ({"actions": "write"}, True),
    "wf_file_put": ({"contents": "write"}, True),
    "wf_merge_guarded": ({"contents": "write", "pull_requests": "write"}, True),
}
ACTIONS = tuple(SPECS)


def _int(args, key, low=1, high=10**12):
    value = args.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise Refused(f"invalid {key}")
    return value


def _sha(args, key):
    value = args.get(key)
    if not isinstance(value, str) or not _SHA.fullmatch(value):
        raise Refused(f"invalid {key}")
    return value


def _ref(args, key, required=True):
    value = args.get(key)
    if value is None and not required:
        return None
    if (
        not isinstance(value, str)
        or not _REF.fullmatch(value)
        or ".." in value
        or value.startswith(("/", "-"))
        or value.endswith(("/", ".lock"))
    ):
        raise Refused(f"invalid {key}")
    return value


def _path(args, key="path"):
    value = args.get(key)
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 1024
        or value.startswith("/")
        or "\\" in value
        or "\x00" in value
        or any(part in ("", ".", "..") for part in value.split("/"))
    ):
        raise Refused(f"invalid {key}")
    return value


def _enum(args, key, allowed, default=None):
    value = args.get(key, default)
    if value not in allowed:
        raise Refused(f"invalid {key}")
    return value


def _page(args):
    return _int(args, "per_page", 1, MAX_PAGE) if "per_page" in args else 30


def _only(args, allowed):
    extra = set(args) - set(allowed)
    if extra:
        raise Refused("unexpected argument")


class WorkflowVerbs:
    def __init__(self, client, policy, seat, bearer, app_slug):
        expected, _ = policy.app_identity(seat)
        if not bearer or app_slug != expected or app_slug != seat:
            raise Refused("own App required")
        self.client, self.policy, self.seat, self.bearer = client, policy, seat, bearer

    @classmethod
    def for_action(cls, client, policy, minter, seat, action, repo):
        """Seat is already authenticated by the broker; authorize once, mint least permission."""
        if action not in SPECS:
            raise Refused("unsupported workflow action")
        try:
            client._check_repo(repo)
        except ValueError:
            raise Refused("invalid repository") from None
        if policy.authorize(seat, action, repo):
            raise Refused("action or repository denied")
        app_slug, ceiling = policy.app_identity(seat)
        required = dict(SPECS[action][0])
        required.setdefault("metadata", "read")
        if _perm_within_ceiling(required, ceiling):
            raise Refused("seat permission ceiling insufficient")
        try:
            token, _ = minter.token_for(app_slug, repo, required)
        except Exception:
            raise Unknown("own App credential unavailable") from None
        return cls(client, policy, seat, token, app_slug)

    def _get(self, path):
        return self.client._call("GET", path, None, bearer=self.bearer)

    def run(self, action, args):
        if not isinstance(args, dict) or action not in SPECS:
            raise Refused("unsupported workflow action")
        repo = args.get("repo")
        if not isinstance(repo, str):
            raise Refused("invalid repo")
        try:
            self.client._check_repo(repo)
        except ValueError:
            raise Refused("invalid repo") from None
        return getattr(self, action)(repo, args)

    # ---- reads ----
    def wf_pr_list(self, repo, args):
        _only(args, ("repo", "state", "head", "base", "per_page"))
        state = _enum(args, "state", ("open", "closed", "all"), "open")
        q = f"state={state}&per_page={_page(args)}"
        if "base" in args:
            q += "&base=" + _ref(args, "base")
        data = self._get(f"/repos/{repo}/pulls?{q}")
        return {
            "pulls": [
                {
                    "number": p.get("number"),
                    "title": p.get("title"),
                    "draft": p.get("draft"),
                    "head_ref": (p.get("head") or {}).get("ref"),
                    "head_sha": (p.get("head") or {}).get("sha"),
                    "base_ref": (p.get("base") or {}).get("ref"),
                    "author": (p.get("user") or {}).get("login"),
                    "updated_at": p.get("updated_at"),
                    "auto_merge": p.get("auto_merge") is not None,
                }
                for p in (data if isinstance(data, list) else [])[:MAX_PROJECT]
            ],
            "complete": False,
        }

    def wf_pr_files(self, repo, args):
        _only(args, ("repo", "number", "per_page"))
        number = _int(args, "number")
        data = self._get(f"/repos/{repo}/pulls/{number}/files?per_page={_page(args)}")
        return {
            "files": [
                {k: f.get(k) for k in ("filename", "status", "additions", "deletions", "sha")}
                for f in (data if isinstance(data, list) else [])[:MAX_PROJECT]
            ],
            "complete": False,
        }

    def wf_file_get(self, repo, args):
        _only(args, ("repo", "path", "ref"))
        path, ref = _path(args), _ref(args, "ref")
        data = self._get(f"/repos/{repo}/contents/{path}?ref={ref}")
        if not isinstance(data, dict) or data.get("type") != "file":
            raise Refused("not a file")
        return {
            "path": data.get("path"),
            "sha": data.get("sha"),
            "size": data.get("size"),
            "encoding": data.get("encoding"),
            "content": data.get("content") if data.get("size", 0) <= 512 * 1024 else None,
        }

    def wf_tree(self, repo, args):
        _only(args, ("repo", "ref"))
        ref = _ref(args, "ref")
        data = self._get(f"/repos/{repo}/git/trees/{ref}?recursive=1")
        tree = data.get("tree") if isinstance(data, dict) else None
        return {
            "sha": (data or {}).get("sha"),
            "truncated": bool((data or {}).get("truncated")) or len(tree or []) > 2000,
            "entries": [
                {"path": t.get("path"), "type": t.get("type"), "sha": t.get("sha")}
                for t in (tree or [])[:2000]
            ],
        }

    def wf_branches(self, repo, args):
        _only(args, ("repo", "per_page"))
        data = self._get(f"/repos/{repo}/branches?per_page={_page(args)}")
        return {
            "branches": [
                {"name": b.get("name"), "sha": (b.get("commit") or {}).get("sha")}
                for b in (data if isinstance(data, list) else [])[:MAX_PROJECT]
            ],
            "complete": False,
        }

    def wf_commits(self, repo, args):
        _only(args, ("repo", "ref", "path", "per_page"))
        q = f"sha={_ref(args, 'ref')}&per_page={_page(args)}"
        if "path" in args:
            q += "&path=" + _path(args).replace("/", "%2F")
        data = self._get(f"/repos/{repo}/commits?{q}")
        return {
            "commits": [
                {
                    "sha": c.get("sha"),
                    "message": ((c.get("commit") or {}).get("message") or "")[:200],
                    "author": (c.get("author") or {}).get("login"),
                    "date": ((c.get("commit") or {}).get("author") or {}).get("date"),
                }
                for c in (data if isinstance(data, list) else [])[:MAX_PROJECT]
            ],
            "complete": False,
        }

    def wf_code_search(self, repo, args):
        _only(args, ("repo", "term", "per_page"))
        term = args.get("term")
        if (
            not isinstance(term, str)
            or not re.fullmatch(r"[A-Za-z0-9_.\- ]{1,128}", term)
            or re.search(r"\b(repo|org|user|path):", term)
        ):
            raise Refused("invalid term")
        q = (term + f" repo:{repo}").replace(" ", "+")
        data = self._get(f"/search/code?q={q}&per_page={_page(args)}")
        return {
            "total": (data or {}).get("total_count"),
            "items": [
                {"path": i.get("path"), "sha": i.get("sha")}
                for i in ((data or {}).get("items") or [])[:MAX_PROJECT]
            ],
            "complete": False,
        }

    def wf_runs(self, repo, args):
        _only(args, ("repo", "branch", "head_sha", "status", "per_page"))
        q = f"per_page={_page(args)}"
        if "branch" in args:
            q += "&branch=" + _ref(args, "branch")
        if "head_sha" in args:
            q += "&head_sha=" + _sha(args, "head_sha")
        if "status" in args:
            q += "&status=" + _enum(
                args, "status", ("queued", "in_progress", "completed", "success", "failure")
            )
        data = self._get(f"/repos/{repo}/actions/runs?{q}")
        return {
            "runs": [
                {
                    k: r.get(k)
                    for k in (
                        "id",
                        "name",
                        "head_branch",
                        "head_sha",
                        "event",
                        "status",
                        "conclusion",
                        "run_attempt",
                        "created_at",
                    )
                }
                for r in ((data or {}).get("workflow_runs") or [])[:MAX_PROJECT]
            ],
            "complete": False,
        }

    def wf_run_jobs(self, repo, args):
        _only(args, ("repo", "run_id", "per_page"))
        run_id = _int(args, "run_id")
        data = self._get(f"/repos/{repo}/actions/runs/{run_id}/jobs?per_page={_page(args)}")
        return {
            "jobs": [
                {
                    "id": j.get("id"),
                    "name": j.get("name"),
                    "status": j.get("status"),
                    "conclusion": j.get("conclusion"),
                    "steps": [
                        {k: s.get(k) for k in ("name", "status", "conclusion")}
                        for s in (j.get("steps") or [])[:100]
                    ],
                }
                for j in ((data or {}).get("jobs") or [])[:MAX_PROJECT]
            ],
            "complete": False,
        }

    # ---- writes (each uses an atomic or idempotent upstream guard) ----
    def wf_update_branch(self, repo, args):
        _only(args, ("repo", "number", "expected_head_sha"))
        number, sha = _int(args, "number"), _sha(args, "expected_head_sha")
        data = self.client._call(
            "PUT",
            f"/repos/{repo}/pulls/{number}/update-branch",
            {"expected_head_sha": sha},
            bearer=self.bearer,
        )
        return {"accepted": True, "message": str((data or {}).get("message", ""))[:200]}

    def _run_post(self, repo, args, suffix):
        _only(args, ("repo", "run_id"))
        run_id = _int(args, "run_id")
        self.client._call(
            "POST", f"/repos/{repo}/actions/runs/{run_id}/{suffix}", None, bearer=self.bearer
        )
        return {"requested": suffix, "run_id": run_id}

    def wf_run_rerun(self, repo, args):
        return self._run_post(repo, args, "rerun-failed-jobs")

    def wf_run_cancel(self, repo, args):
        return self._run_post(repo, args, "cancel")

    def wf_run_approve(self, repo, args):
        return self._run_post(repo, args, "approve")

    def wf_file_put(self, repo, args):
        _only(args, ("repo", "path", "branch", "message", "content_b64", "prior_sha"))
        path, branch = _path(args), _ref(args, "branch")
        if path.startswith(".github/workflows/"):
            raise Refused("workflow files are not writable here")
        message = args.get("message")
        content = args.get("content_b64")
        if not isinstance(message, str) or not 1 <= len(message) <= 500 or "\x00" in message:
            raise Refused("invalid message")
        if not isinstance(content, str) or not 0 < len(content) <= MAX_FILE_B64:
            raise Refused("invalid content")
        try:
            raw = base64.b64decode(content, validate=True)
        except (binascii.Error, ValueError):
            raise Refused("invalid content") from None
        if find_secret(message) or find_secret(raw.decode("utf-8", "replace")):
            raise Refused("credential-shaped content refused")
        repo_info = self._get(f"/repos/{repo}")
        default = repo_info.get("default_branch") if isinstance(repo_info, dict) else None
        if not default or branch == default:
            raise Refused("default branch is not writable")
        payload = {"message": message, "content": content, "branch": branch}
        if "prior_sha" in args:
            payload["sha"] = _sha(args, "prior_sha")
        data = self.client._call(
            "PUT", f"/repos/{repo}/contents/{path}", payload, bearer=self.bearer
        )
        return {
            "path": path,
            "branch": branch,
            "commit": ((data or {}).get("commit") or {}).get("sha"),
        }

    def wf_merge_guarded(self, repo, args):
        _only(args, ("repo", "number", "method", "expected_head_sha"))
        number, sha = _int(args, "number"), _sha(args, "expected_head_sha")
        method = _enum(args, "method", ("squash", "merge", "rebase"))
        data = self.client._call(
            "PUT",
            f"/repos/{repo}/pulls/{number}/merge",
            {"merge_method": method, "sha": sha},
            bearer=self.bearer,
        )
        return {"merged": (data or {}).get("merged"), "sha": (data or {}).get("sha")}
