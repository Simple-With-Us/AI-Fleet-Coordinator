"""Inactive read, search and workflow verbs for a PR-backlog seat.

Not registered in server startup, policy action lists or the Worker. A broker that has
already authenticated a seat constructs it through ``for_action``. It never accepts a
caller token, URL, raw query string or arbitrary path: every argument is validated into
a fixed REST path. Each verb asks the existing minter for only the permissions it needs
and is refused when the seat's ceiling does not cover them. Text returned by GitHub is
untrusted data. No credential leaves this module.

Verbs (action names carry the wf_ prefix so they cannot collide with registered names):
  reads   wf_pr_list wf_pr_files wf_file_get wf_tree wf_branches wf_commits wf_code_search
          wf_runs wf_run_jobs wf_checks (needs checks:read, refused unless the ceiling carries it)
  writes  wf_update_branch (atomic expected_head_sha), wf_run_rerun, wf_run_cancel,
          wf_run_approve, wf_file_put and wf_files_put (non-default branch only; the multi-file one is one commit through git-data, atomic on expected_head_sha), wf_merge_guarded
          (atomic expected_head_sha)
Check-run reads (wf_checks) need the checks permission, which is outside the shared
ceiling, so that verb is refused until a seat's ceiling carries checks:read; without it
run and job conclusions (actions:read) are the check source and non-Actions checks are
not covered. Job logs are zip redirects the client refuses to follow, so
only job and step conclusions are returned.
"""

import base64
import binascii
import re
from urllib.parse import quote

from .github_client import RelayGitHubError
from .secret_scan import find_secret
from .server import PreReadUnavailable, _perm_within_ceiling


class Refused(ValueError):
    """Fixed public refusal; never wraps upstream text."""


class CeilingDenied(Refused):
    """The seat's App ceiling does not cover the verb's permissions (a 403, not a bad request)."""


class WriteOutcomeUnknown(RuntimeError):
    """A write was sent but its result cannot be verified."""


class Unknown(RuntimeError):
    """Own App credential unavailable."""


_SHA = re.compile(r"[0-9a-f]{40}")
_REF = re.compile(r"[A-Za-z0-9._/-]{1,200}")
_PATH = re.compile(r"[A-Za-z0-9._/+@~-]{1,1024}")
_REPO = re.compile(r"[A-Za-z0-9._-]{1,100}/[A-Za-z0-9._-]{1,100}")
MAX_FILE_B64 = 256 * 1024
MAX_PAGE = 50
MAX_PROJECT = 100
MAX_FILES = 20
MAX_FILES_B64 = 512 * 1024

# action -> (permissions needed, writes)
SPECS = {
    "wf_deployments": ({"deployments": "read"}, False),
    "wf_deployment_get": ({"deployments": "read"}, False),
    "wf_deployment_statuses": ({"deployments": "read"}, False),
    "wf_deployment_create": ({"deployments": "write"}, True),
    "wf_deployment_status_create": ({"deployments": "write"}, True),
    "wf_deployment_delete": ({"deployments": "write"}, True),
    "wf_labels_set": ({"issues": "write"}, True),
    "wf_reviewers_request": ({"pull_requests": "write"}, True),
    "wf_reviewers_remove": ({"pull_requests": "write"}, True),
    "wf_release_create": ({"contents": "write"}, True),
    "wf_release_update": ({"contents": "write"}, True),
    "wf_environments": ({"actions": "read"}, False),
    "wf_environment_get": ({"actions": "read"}, False),
    "wf_file_delete": ({"contents": "write"}, True),
    "wf_issue_list": ({"issues": "read"}, False),
    "wf_issue_get": ({"issues": "read"}, False),
    "wf_issue_create": ({"issues": "write"}, True),
    "wf_issue_update": ({"issues": "write"}, True),
    "wf_issue_comment": ({"issues": "write"}, True),
    "wf_issue_comment_update": ({"issues": "write"}, True),
    "wf_issue_comment_delete": ({"issues": "write"}, True),
    "wf_review_comment_update": ({"pull_requests": "write"}, True),
    "wf_review_comment_delete": ({"pull_requests": "write"}, True),
    "wf_release_list": ({"contents": "read"}, False),
    "wf_release_get": ({"contents": "read"}, False),
    "wf_release_delete": ({"contents": "write"}, True),
    "wf_branch_update": ({"contents": "write"}, True),
    "wf_branch_delete": ({"contents": "write"}, True),
    "wf_tag_create": ({"contents": "write"}, True),
    "wf_tag_get": ({"contents": "read"}, False),
    "wf_tag_delete": ({"contents": "write"}, True),
    "wf_pr_draft": ({"pull_requests": "write"}, True),
    "wf_pr_ready": ({"pull_requests": "write"}, True),
    "wf_automerge_enable": ({"pull_requests": "write"}, True),
    "wf_automerge_disable": ({"pull_requests": "write"}, True),
    "wf_review_threads": ({"pull_requests": "read"}, False),
    "wf_assignees_add": ({"issues": "write"}, True),
    "wf_assignees_remove": ({"issues": "write"}, True),
    "wf_issue_lock": ({"issues": "write"}, True),
    "wf_issue_unlock": ({"issues": "write"}, True),
    "wf_label_create": ({"issues": "write"}, True),
    "wf_label_delete": ({"issues": "write"}, True),
    "wf_pr_get": ({"pull_requests": "read"}, False),
    "wf_pr_reviews": ({"pull_requests": "read"}, False),
    "wf_pr_comments": ({"pull_requests": "read"}, False),
    "wf_issue_comments": ({"issues": "read"}, False),
    "wf_commit_get": ({"contents": "read"}, False),
    "wf_compare": ({"contents": "read"}, False),
    "wf_branch_get": ({"contents": "read"}, False),
    "wf_repo_get": ({"metadata": "read"}, False),
    "wf_labels": ({"issues": "read"}, False),
    "wf_workflows": ({"actions": "read"}, False),
    "wf_run_get": ({"actions": "read"}, False),
    "wf_run_artifacts": ({"actions": "read"}, False),
    "wf_workflow_enable": ({"actions": "write"}, True),
    "wf_workflow_disable": ({"actions": "write"}, True),
    "wf_run_delete": ({"actions": "write"}, True),
    "wf_artifact_delete": ({"actions": "write"}, True),
    "wf_release_assets": ({"contents": "read"}, False),
    "wf_release_asset_get": ({"contents": "read"}, False),
    "wf_release_asset_upload": ({"contents": "write"}, True),
    "wf_review_submit": ({"pull_requests": "write"}, True),
    "wf_review_inline": ({"pull_requests": "write"}, True),
    "wf_branch_create": ({"contents": "write"}, True),
    "wf_pr_list": ({"pull_requests": "read"}, False),
    "wf_pr_files": ({"pull_requests": "read"}, False),
    "wf_file_get": ({"contents": "read"}, False),
    "wf_tree": ({"contents": "read"}, False),
    "wf_branches": ({"contents": "read"}, False),
    "wf_commits": ({"contents": "read"}, False),
    "wf_code_search": ({"contents": "read"}, False),
    "wf_runs": ({"actions": "read"}, False),
    "wf_run_jobs": ({"actions": "read"}, False),
    # Needs checks:read, outside today's shared ceiling: refused at for_action until a
    # seat's ceiling explicitly carries it. Never widened here.
    "wf_checks": ({"checks": "read"}, False),
    "wf_update_branch": ({"pull_requests": "write", "contents": "write"}, True),
    "wf_run_rerun": ({"actions": "write"}, True),
    "wf_run_cancel": ({"actions": "write"}, True),
    "wf_run_approve": ({"actions": "write"}, True),
    "wf_file_put": ({"contents": "write"}, True),
    "wf_files_put": ({"contents": "write"}, True),
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
        or not _PATH.fullmatch(value)
        or value.startswith("/")
        or any(part in ("", ".", "..") for part in value.split("/"))
    ):
        raise Refused(f"invalid {key}")
    return value


def _workflow_path(path):
    """A relay write targeting the workflows dir itself, its parent, or anything under it."""
    return path == ".github" or path == ".github/workflows" or path.startswith(".github/workflows/")


def _enum(args, key, allowed, default=None):
    value = args.get(key, default)
    if value not in allowed:
        raise Refused(f"invalid {key}")
    return value


def _page(args):
    return _int(args, "per_page", 1, MAX_PAGE) if "per_page" in args else 30


def _obj(data):
    if not isinstance(data, dict):
        raise RelayGitHubError(502, "unexpected upstream shape")
    return data


def _sub(data, key):
    """A nested upstream object; any other shape is an upstream error, never a definitely-unsent refusal."""
    if not isinstance(data, dict):
        raise RelayGitHubError(502, "unexpected upstream shape")
    value = data.get(key)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise RelayGitHubError(502, "unexpected upstream shape")
    return value


def _arr(data):
    if not isinstance(data, list):
        raise RelayGitHubError(502, "unexpected upstream shape")
    return data


def _dicts(value):
    """Entries of an upstream list; a non-list or a non-object entry is an upstream error."""
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, dict) for v in value):
        raise RelayGitHubError(502, "unexpected upstream shape")
    return value


def _only(args, allowed):
    extra = set(args) - set(allowed)
    if extra:
        raise Refused("unexpected argument")


_NEW_READS = frozenset(
    {
        "wf_pr_get",
        "wf_pr_reviews",
        "wf_pr_comments",
        "wf_issue_comments",
        "wf_commit_get",
        "wf_compare",
        "wf_branch_get",
        "wf_repo_get",
        "wf_labels",
        "wf_workflows",
        "wf_run_get",
        "wf_run_artifacts",
    }
)


def _validate_read(action, result):
    """Bound untrusted JSON and verify each newly exposed endpoint's core shape."""

    def bad():
        raise RelayGitHubError(502, "workflow read receipt unverifiable")

    stack = [(result, 0)]
    nodes = size = 0
    while stack:
        value, depth = stack.pop()
        nodes += 1
        if nodes > 500 or depth > 6:
            bad()
        if isinstance(value, str):
            size += len(value.encode("utf-8"))
        elif isinstance(value, dict):
            if len(value) > 100 or not all(isinstance(k, str) for k in value):
                bad()
            size += sum(len(k) for k in value)
            stack.extend((v, depth + 1) for v in value.values())
        elif isinstance(value, list):
            if len(value) > 250:
                bad()
            stack.extend((v, depth + 1) for v in value)
        elif value is not None and type(value) not in (bool, int, float):
            bad()
        if size > 65536:
            bad()

    def fields(value, shape):
        if not isinstance(value, dict):
            bad()
        for k, check in shape.items():
            if k not in value or not check(value[k]):
                bad()
        return True

    positive = lambda v: type(v) is int and v > 0
    nonnegative = lambda v: type(v) is int and v >= 0
    text = lambda v: isinstance(v, str)
    sha = lambda v: isinstance(v, str) and bool(_SHA.fullmatch(v))
    boolean = lambda v: type(v) is bool
    seq = lambda v, check, limit=50: (
        isinstance(v, list) and len(v) <= limit and all(check(x) for x in v)
    )
    commit = lambda v: fields(
        v,
        {
            "sha": sha,
            "commit": lambda x: fields(x, {"message": text}),
            "parents": lambda x: seq(x, lambda y: fields(y, {"sha": sha}), 100),
        },
    )
    comment = lambda v: fields(v, {"id": positive, "body": lambda x: text(x) and len(x) <= 32768})
    schemas = {
        "wf_pr_get": {
            "pull": lambda v: fields(
                v,
                {
                    "id": positive,
                    "number": positive,
                    "state": lambda x: x in ("open", "closed"),
                    "head": lambda x: fields(x, {"sha": sha}),
                    "base": lambda x: fields(x, {"sha": sha}),
                },
            )
        },
        "wf_pr_reviews": {
            "reviews": lambda v: seq(
                v, lambda x: fields(x, {"id": positive, "state": text, "commit_id": sha})
            ),
            "complete": boolean,
        },
        "wf_pr_comments": {"comments": lambda v: seq(v, comment), "complete": boolean},
        "wf_issue_comments": {"comments": lambda v: seq(v, comment), "complete": boolean},
        "wf_commit_get": {"commit": commit},
        "wf_compare": {
            "comparison": lambda v: fields(
                v,
                {
                    "status": text,
                    "ahead_by": nonnegative,
                    "behind_by": nonnegative,
                    "total_commits": nonnegative,
                    "commits": lambda x: seq(x, commit, 250),
                },
            )
        },
        "wf_branch_get": {
            "branch": lambda v: fields(
                v,
                {
                    "name": lambda x: text(x) and bool(x),
                    "commit": lambda x: fields(x, {"sha": sha}),
                },
            )
        },
        "wf_repo_get": {
            "repository": lambda v: fields(
                v,
                {
                    "id": positive,
                    "name": lambda x: text(x) and bool(x),
                    "full_name": lambda x: text(x) and len(x) >= 3,
                    "default_branch": lambda x: text(x) and bool(x),
                    "private": boolean,
                },
            )
        },
        "wf_labels": {
            "labels": lambda v: seq(
                v,
                lambda x: fields(
                    x,
                    {
                        "id": positive,
                        "name": text,
                        "color": lambda y: text(y) and bool(re.fullmatch(r"[0-9a-fA-F]{6}", y)),
                    },
                ),
            ),
            "complete": boolean,
        },
        "wf_workflows": {
            "workflows": lambda v: fields(
                v,
                {
                    "total_count": nonnegative,
                    "workflows": lambda x: seq(
                        x,
                        lambda y: fields(
                            y, {"id": positive, "name": text, "path": text, "state": text}
                        ),
                    ),
                },
            )
        },
        "wf_run_get": {
            "run": lambda v: fields(
                v,
                {
                    "id": positive,
                    "head_sha": sha,
                    "status": text,
                    "conclusion": lambda x: x is None or text(x),
                },
            )
        },
        "wf_run_artifacts": {
            "artifacts": lambda v: fields(
                v,
                {
                    "total_count": nonnegative,
                    "artifacts": lambda x: seq(
                        x,
                        lambda y: fields(
                            y,
                            {
                                "id": positive,
                                "name": text,
                                "size_in_bytes": nonnegative,
                                "expired": boolean,
                            },
                        ),
                    ),
                },
            )
        },
    }
    shape = schemas[action]
    if not isinstance(result, dict) or set(result) != set(shape):
        bad()
    fields(result, shape)
    return result


class WorkflowVerbs:
    def __init__(self, client, policy, seat, bearer, app_slug, action, repo):
        expected, _ = policy.app_identity(seat)
        if not bearer or app_slug != expected or app_slug != seat:
            raise Refused("own App required")
        if action not in SPECS or not isinstance(repo, str) or not _REPO.fullmatch(repo):
            raise Refused("action and repository binding required")
        self.client, self.policy, self.seat, self.bearer = client, policy, seat, bearer
        # The handle is minted for exactly one authorized action and repository.
        self.action, self.repo = action, repo

    @classmethod
    def check_shape(cls, action, args, repo):
        """Run a verb's own argument checks with no network, before any durable state.

        The verb is run against a probe client that stops at its first upstream call, so a
        malformed request raises Refused here and a well-formed one passes without a mint,
        an outcome row or an upstream call.
        """
        if action not in SPECS or not isinstance(args, dict):
            raise Refused("unsupported workflow action")
        if not isinstance(repo, str) or not _REPO.fullmatch(repo):
            raise Refused("invalid repository")

        class _Reached(Exception):
            pass

        class _Probe:
            def __getattr__(self, name):
                def stop(*a, **k):
                    raise _Reached

                return stop

        handle = cls.__new__(cls)
        handle.client, handle.policy, handle.seat, handle.bearer = _Probe(), None, "probe", "probe"
        handle.action, handle.repo = action, repo
        try:
            handle.run(action, args)
        except _Reached:
            return

    @classmethod
    def for_action(cls, client, policy, minter, seat, action, repo, preauthorized=False):
        """Seat is already authenticated by the broker; authorize once, mint least permission.

        preauthorized is for Relay.handle only: it has just run policy.authorize for this
        exact seat, action and repo, and a second call would count the hourly quota twice.
        """
        if action not in SPECS:
            raise Refused("unsupported workflow action")
        if not isinstance(repo, str) or not _REPO.fullmatch(repo):
            raise Refused("invalid repository")
        if not preauthorized and policy.authorize(seat, action, repo):
            raise Refused("action or repository denied")
        app_slug, ceiling = policy.app_identity(seat)
        required = dict(SPECS[action][0])
        required.setdefault("metadata", "read")
        if _perm_within_ceiling(required, ceiling):
            raise CeilingDenied("seat permission ceiling insufficient")
        try:
            token, _ = minter.token_for(app_slug, repo, required)
        except Exception:
            raise Unknown("own App credential unavailable") from None
        return cls(client, policy, seat, token, app_slug, action, repo)

    def _get(self, path):
        return self.client._call("GET", path, None, bearer=self.bearer)

    def run(self, action, args):
        if not isinstance(args, dict) or action not in SPECS:
            raise Refused("unsupported workflow action")
        # Authorization happened once, in for_action, for exactly this action and repo.
        if action != self.action or args.get("repo") != self.repo:
            raise Refused("action or repository not authorized for this handle")
        result = getattr(self, action)(self.repo, args)
        return _validate_read(action, result) if action in _NEW_READS else result

    # Fixed issue endpoints only.  No assignee, label, milestone or admin expansion.
    @staticmethod
    def _issue_summary(data):
        if not isinstance(data, dict):
            raise Refused("unexpected issue response")
        if "pull_request" in data:
            raise Refused("pull requests are not issue lifecycle targets")
        if (
            isinstance(data.get("number"), bool)
            or not isinstance(data.get("number"), int)
            or data["number"] < 1
            or data.get("state") not in ("open", "closed")
        ):
            raise Refused("unexpected issue response")
        return {k: data.get(k) for k in ("number", "state", "title", "updated_at", "html_url")}

    @staticmethod
    def _issue_text(args, key, required=True):
        value = args.get(key)
        if value is None and not required:
            return None
        if not isinstance(value, str) or not value.strip() or len(value.encode("utf-8")) > 32768:
            raise Refused(f"invalid {key}")
        if find_secret(value):
            raise Refused("credential-shaped outbound text")
        return value

    def wf_issue_list(self, repo, args):
        _only(args, ("repo", "state", "per_page"))
        state = _enum(args, "state", ("open", "closed", "all"), "open")
        data = _arr(self._get(f"/repos/{repo}/issues?state={state}&per_page={_page(args)}"))
        return {
            "issues": [
                self._issue_summary(x)
                for x in data[:MAX_PROJECT]
                if not isinstance(x, dict) or "pull_request" not in x
            ],
            "complete": False,
        }

    def wf_issue_get(self, repo, args):
        _only(args, ("repo", "number"))
        number = _int(args, "number")
        return {"issue": self._issue_summary(self._get(f"/repos/{repo}/issues/{number}"))}

    def wf_issue_create(self, repo, args):
        _only(args, ("repo", "title", "body"))
        title = self._issue_text(args, "title")
        body = self._issue_text(args, "body")
        result = self.client._call(
            "POST", f"/repos/{repo}/issues", {"title": title, "body": body}, bearer=self.bearer
        )
        try:
            summary = self._issue_summary(result)
            if (
                not isinstance(summary["number"], int)
                or isinstance(summary["number"], bool)
                or summary["state"] not in ("open", "closed")
            ):
                raise Refused("unexpected issue response")
        except (Refused, KeyError, TypeError) as exc:
            raise WriteOutcomeUnknown("issue write outcome unverifiable") from exc
        return {"issue": summary}

    def _issue_before_write(self, repo, number):
        # Only this GET is caught.  No upstream write has been attempted here.
        # Existing held UNKNOWN rows are never cleared or reclassified.
        # Transient matches Relay.handle's classification (transport/timeout,
        # 408, 5xx):  any of them must release the key, never hold it unknown.
        try:
            data = self._get(f"/repos/{repo}/issues/{number}")
        except RelayGitHubError as exc:
            if exc.status in (0, 408) or exc.status >= 500:
                raise PreReadUnavailable("issue pre-read unavailable; no write attempted") from None
            raise
        return self._issue_summary(data)

    def wf_issue_update(self, repo, args):
        _only(args, ("repo", "number", "title", "body", "state"))
        number = _int(args, "number")
        payload = {}
        for key in ("title", "body"):
            if key in args:
                payload[key] = self._issue_text(args, key)
        if "state" in args:
            payload["state"] = _enum(args, "state", ("open", "closed"))
        if not payload:
            raise Refused("issue update requires a change")
        # Read rejects PR targets.  This is not an atomic compare-and-swap claim.
        self._issue_before_write(repo, number)
        result = self.client._call(
            "PATCH", f"/repos/{repo}/issues/{number}", payload, bearer=self.bearer
        )
        try:
            summary = self._issue_summary(result)
            if (
                not isinstance(summary["number"], int)
                or isinstance(summary["number"], bool)
                or summary["state"] not in ("open", "closed")
            ):
                raise Refused("unexpected issue response")
        except (Refused, KeyError, TypeError) as exc:
            raise WriteOutcomeUnknown("issue write outcome unverifiable") from exc
        return {"issue": summary}

    def wf_issue_comment(self, repo, args):
        _only(args, ("repo", "number", "body"))
        number = _int(args, "number")
        body = self._issue_text(args, "body")
        self._issue_before_write(repo, number)
        result = self.client._call(
            "POST", f"/repos/{repo}/issues/{number}/comments", {"body": body}, bearer=self.bearer
        )
        if (
            not isinstance(result, dict)
            or isinstance(result.get("id"), bool)
            or not isinstance(result.get("id"), int)
        ):
            raise WriteOutcomeUnknown("issue comment outcome unverifiable")
        return {"comment": {k: result.get(k) for k in ("id", "html_url")}}

    @staticmethod
    def _environment_name(args):
        name = args.get("name")
        if (
            not isinstance(name, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._/-]{0,99}", name)
            or any(x in (".", "..", "") for x in name.split("/"))
            or find_secret(name)
        ):
            raise Refused("invalid environment name")
        return name

    @staticmethod
    def _environment_summary(data, expected=None):
        if (
            not isinstance(data, dict)
            or type(data.get("id")) is not int
            or data["id"] < 1
            or not isinstance(data.get("name"), str)
            or len(data["name"].encode("utf-8")) > 255
            or not data["name"]
            or (expected is not None and data["name"] != expected)
            or any(
                not isinstance(data.get(k), str) or len(data[k]) > 100
                for k in ("created_at", "updated_at")
            )
        ):
            raise RelayGitHubError(502, "environment receipt unverifiable")
        if find_secret(data["name"]):
            raise Refused("environment name contains credential pattern")
        # Optional protection fields are NOT invented as empty/disabled when absent.
        rules = data.get("protection_rules")
        types = None
        if rules is not None:
            if (
                not isinstance(rules, list)
                or len(rules) > 20
                or any(
                    not isinstance(x, dict)
                    or not isinstance(x.get("type"), str)
                    or not 1 <= len(x["type"]) <= 100
                    for x in rules
                )
            ):
                raise RelayGitHubError(502, "environment protection summary unverifiable")
            types = [x["type"] for x in rules]
        policy = data.get("deployment_branch_policy")
        if policy is not None:
            if not isinstance(policy, dict) or any(
                type(policy.get(k)) is not bool
                for k in ("protected_branches", "custom_branch_policies")
            ):
                raise RelayGitHubError(502, "environment branch policy unverifiable")
            policy = {k: policy[k] for k in ("protected_branches", "custom_branch_policies")}
        return {
            **{k: data[k] for k in ("id", "name", "created_at", "updated_at")},
            "protection_rule_types": types,
            "deployment_branch_policy": policy,
        }

    def wf_environments(self, repo, args):
        _only(args, ("repo", "per_page", "page"))
        limit = _page(args)
        page = _int(args, "page", 1, 1000) if "page" in args else 1
        data = self._get(f"/repos/{repo}/environments?per_page={limit}&page={page}")
        if (
            not isinstance(data, dict)
            or type(data.get("total_count")) is not int
            or data["total_count"] < 0
            or not isinstance(data.get("environments"), list)
        ):
            raise RelayGitHubError(502, "environments page unverifiable")
        return {
            "environments": [self._environment_summary(x) for x in data["environments"][:limit]],
            "total_count": data["total_count"],
            "page": page,
            "has_more": page * limit < data["total_count"],
        }

    def wf_environment_get(self, repo, args):
        _only(args, ("repo", "name"))
        name = self._environment_name(args)
        return {
            "environment": self._environment_summary(
                self._get(f"/repos/{repo}/environments/{quote(name, safe='')}"), name
            )
        }

    def _asset_summary(self, data):
        if (
            not isinstance(data, dict)
            or isinstance(data.get("id"), bool)
            or not isinstance(data.get("id"), int)
            or data["id"] < 1
        ):
            raise RelayGitHubError(502, "invalid release asset summary")
        if (
            not isinstance(data.get("name"), str)
            or not 1 <= len(data["name"]) <= 255
            or data.get("state") not in ("uploaded", "open", "starter")
        ):
            raise RelayGitHubError(502, "invalid release asset summary")
        if (
            isinstance(data.get("size"), bool)
            or not isinstance(data.get("size"), int)
            or data["size"] < 0
        ):
            raise RelayGitHubError(502, "invalid release asset size")
        return {
            k: data.get(k)
            for k in (
                "id",
                "name",
                "state",
                "size",
                "content_type",
                "digest",
                "browser_download_url",
            )
        }

    def wf_release_assets(self, repo, args):
        _only(args, ("repo", "release_id", "per_page"))
        release_id, page = _int(args, "release_id"), _page(args)
        data = self._get(f"/repos/{repo}/releases/{release_id}/assets?per_page={page}")
        if not isinstance(data, list) or len(data) > page:
            raise RelayGitHubError(502, "invalid release asset list")
        return {"assets": [self._asset_summary(x) for x in data], "complete": len(data) < page}

    def wf_release_asset_get(self, repo, args):
        _only(args, ("repo", "asset_id"))
        asset_id = _int(args, "asset_id")
        data = self._asset_summary(self._get(f"/repos/{repo}/releases/assets/{asset_id}"))
        if data["id"] != asset_id:
            raise RelayGitHubError(502, "release asset identity mismatch")
        return {"asset": data}

    def wf_release_asset_upload(self, repo, args):
        _only(args, ("repo", "release_id", "name", "content_b64"))
        release_id = _int(args, "release_id")
        name = args.get("name")
        if (
            not isinstance(name, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}", name)
            or name.endswith(".")
        ):
            raise Refused("invalid asset name")
        encoded = args.get("content_b64")
        if not isinstance(encoded, str) or not 1 <= len(encoded) <= 43692:
            raise Refused("invalid asset content")
        try:
            content = base64.b64decode(encoded, validate=True)
            text = content.decode("utf-8")
        except (ValueError, UnicodeError, binascii.Error):
            raise Refused("asset must be bounded UTF-8 text") from None
        if not 1 <= len(content) <= 32768 or "\x00" in text or find_secret(text):
            raise Refused("invalid asset content")
        # Text-only slice avoids disguising credentials inside opaque binary archives.
        try:
            release = _obj(self._get(f"/repos/{repo}/releases/{release_id}"))
        except RelayGitHubError as exc:
            if exc.status in (0, 408) or exc.status >= 500:
                raise PreReadUnavailable(
                    "release pre-read unavailable; no write attempted"
                ) from None
            raise
        if (
            release.get("id") != release_id
            or isinstance(release.get("id"), bool)
            or release.get("upload_url")
            != f"https://uploads.github.com/repos/{repo}/releases/{release_id}/assets{{?name,label}}"
        ):
            raise Refused("release upload destination unverifiable")
        data = self._asset_summary(
            self.client.upload_release_asset(repo, release_id, name, content, self.bearer)
        )
        if data["name"] != name or data["state"] != "uploaded" or data["size"] != len(content):
            raise RelayGitHubError(502, "release asset upload outcome unverifiable")
        return {"asset": data}

    def _text(self, args, key, limit=32768):
        value = args.get(key)
        if (
            not isinstance(value, str)
            or not value.strip()
            or len(value.encode("utf-8")) > limit
            or find_secret(value)
        ):
            raise Refused(f"invalid {key}")
        return value

    def _pull_at_commit(self, repo, number, commit):
        # Pre-read is not an atomic head lock; GitHub binds the review to commit_id.
        try:
            data = _obj(self._get(f"/repos/{repo}/pulls/{number}"))
            observed_head = _sub(data, "head").get("sha")
            if not isinstance(observed_head, str) or not re.fullmatch(
                r"[0-9a-f]{40}", observed_head
            ):
                raise RelayGitHubError(502, "unexpected upstream shape")
        except RelayGitHubError as exc:
            if exc.status in (0, 408) or exc.status >= 500:
                raise PreReadUnavailable(
                    "review pre-read unavailable; no write attempted"
                ) from None
            raise
        if observed_head != commit:
            raise Refused("pull request head changed")

    def wf_review_submit(self, repo, args):
        _only(args, ("repo", "number", "commit_id", "event", "body"))
        number, commit = _int(args, "number"), _sha(args, "commit_id")
        event = _enum(args, "event", ("APPROVE", "REQUEST_CHANGES", "COMMENT"))
        body = self._text(args, "body")
        self._pull_at_commit(repo, number, commit)
        result = self.client._call(
            "POST",
            f"/repos/{repo}/pulls/{number}/reviews",
            {"commit_id": commit, "event": event, "body": body},
            bearer=self.bearer,
        )
        if (
            not isinstance(result, dict)
            or isinstance(result.get("id"), bool)
            or not isinstance(result.get("id"), int)
            or result["id"] <= 0
            or result.get("commit_id") != commit
        ):
            raise RelayGitHubError(502, "review write outcome unverifiable")
        if (
            result.get("state")
            != {
                "APPROVE": "APPROVED",
                "REQUEST_CHANGES": "CHANGES_REQUESTED",
                "COMMENT": "COMMENTED",
            }[event]
        ):
            raise RelayGitHubError(502, "review state unverifiable")
        return {"review": {k: result.get(k) for k in ("id", "state", "commit_id", "html_url")}}

    def wf_review_inline(self, repo, args):
        _only(args, ("repo", "number", "commit_id", "path", "line", "side", "body"))
        number, commit = _int(args, "number"), _sha(args, "commit_id")
        path, line = _path(args), _int(args, "line", 1, 10000000)
        side, body = _enum(args, "side", ("LEFT", "RIGHT")), self._text(args, "body")
        self._pull_at_commit(repo, number, commit)
        result = self.client._call(
            "POST",
            f"/repos/{repo}/pulls/{number}/comments",
            {"commit_id": commit, "path": path, "line": line, "side": side, "body": body},
            bearer=self.bearer,
        )
        if (
            not isinstance(result, dict)
            or isinstance(result.get("id"), bool)
            or not isinstance(result.get("id"), int)
            or result["id"] <= 0
            or result.get("commit_id") != commit
        ):
            raise RelayGitHubError(502, "inline write outcome unverifiable")
        return {"comment": {k: result.get(k) for k in ("id", "commit_id", "html_url")}}

    @staticmethod
    def _deployment_summary(value, status=False):
        if (
            not isinstance(value, dict)
            or isinstance(value.get("id"), bool)
            or not isinstance(value.get("id"), int)
            or value["id"] < 1
        ):
            raise RelayGitHubError(502, "deployment receipt unverifiable")
        keys = (
            ("id", "state", "description", "created_at")
            if status
            else (
                "id",
                "sha",
                "ref",
                "environment",
                "task",
                "created_at",
                "transient_environment",
                "production_environment",
            )
        )
        if status:
            if value.get("state") not in (
                "error",
                "failure",
                "inactive",
                "in_progress",
                "queued",
                "pending",
                "success",
            ):
                raise RelayGitHubError(502, "deployment state unverifiable")
        elif not isinstance(value.get("sha"), str) or not _SHA.fullmatch(value["sha"]):
            raise RelayGitHubError(502, "deployment SHA unverifiable")
        out = {k: value.get(k) for k in keys}
        for v in out.values():
            if isinstance(v, str) and len(v.encode("utf-8")) > 1024:
                raise RelayGitHubError(502, "deployment field unbounded")
        return out

    def wf_deployments(self, repo, args):
        _only(args, ("repo", "per_page"))
        count = _page(args)
        rows = self._get(f"/repos/{repo}/deployments?per_page={count}")
        if not isinstance(rows, list) or len(rows) > count:
            raise RelayGitHubError(502, "deployment list unverifiable")
        return {
            "deployments": [self._deployment_summary(x) for x in rows],
            "complete": len(rows) < count,
        }

    def wf_deployment_get(self, repo, args):
        _only(args, ("repo", "deployment_id"))
        ident = _int(args, "deployment_id")
        row = self._deployment_summary(self._get(f"/repos/{repo}/deployments/{ident}"))
        if row["id"] != ident:
            raise RelayGitHubError(502, "deployment identity unverifiable")
        return {"deployment": row}

    def wf_deployment_statuses(self, repo, args):
        _only(args, ("repo", "deployment_id", "per_page"))
        ident, count = _int(args, "deployment_id"), _page(args)
        rows = self._get(f"/repos/{repo}/deployments/{ident}/statuses?per_page={count}")
        if not isinstance(rows, list) or len(rows) > count:
            raise RelayGitHubError(502, "deployment status list unverifiable")
        return {
            "statuses": [self._deployment_summary(x, True) for x in rows],
            "complete": len(rows) < count,
        }

    def wf_deployment_create(self, repo, args):
        _only(args, ("repo", "sha", "environment", "description", "transient", "production"))
        sha = _sha(args, "sha")
        environment = self._text(args, "environment", 100)
        description = self._text(args, "description", 140)
        transient, production = args.get("transient"), args.get("production")
        if not isinstance(transient, bool) or not isinstance(production, bool):
            raise Refused("invalid deployment flags")
        # Never auto-merge a branch or bypass required status contexts.
        payload = {
            "ref": sha,
            "environment": environment,
            "description": description,
            "auto_merge": False,
            "transient_environment": transient,
            "production_environment": production,
        }
        row = self._deployment_summary(
            self.client._call("POST", f"/repos/{repo}/deployments", payload, bearer=self.bearer)
        )
        if (
            row["sha"] != sha
            or row["environment"] != environment
            or row["transient_environment"] != transient
            or row["production_environment"] != production
        ):
            raise RelayGitHubError(502, "deployment creation unverifiable")
        return {"deployment": row}

    def wf_deployment_status_create(self, repo, args):
        _only(args, ("repo", "deployment_id", "state", "description"))
        ident = _int(args, "deployment_id")
        state = _enum(
            args,
            "state",
            ("error", "failure", "inactive", "in_progress", "queued", "pending", "success"),
        )
        description = self._text(args, "description", 140)
        # auto_inactive=False avoids changing other deployments as a side effect.
        row = self._deployment_summary(
            self.client._call(
                "POST",
                f"/repos/{repo}/deployments/{ident}/statuses",
                {"state": state, "description": description, "auto_inactive": False},
                bearer=self.bearer,
            ),
            True,
        )
        if row["state"] != state or row["description"] != description:
            raise RelayGitHubError(502, "deployment status write unverifiable")
        return {"status": row}

    def wf_deployment_delete(self, repo, args):
        _only(args, ("repo", "deployment_id"))
        ident = _int(args, "deployment_id")
        result = self.client._call(
            "DELETE",
            f"/repos/{repo}/deployments/{ident}",
            None,
            bearer=self.bearer,
            expected_status=204,
        )
        if result != {}:
            raise RelayGitHubError(502, "deployment deletion unverifiable")
        return {"deleted": True, "deployment_id": ident}

    def wf_branch_create(self, repo, args):
        _only(args, ("repo", "branch", "sha"))
        branch, sha = _ref(args, "branch"), _sha(args, "sha")
        # Creation only: GitHub refuses an existing ref; never force/update/delete.
        result = self.client._call(
            "POST",
            f"/repos/{repo}/git/refs",
            {"ref": "refs/heads/" + branch, "sha": sha},
            bearer=self.bearer,
        )
        if (
            not isinstance(result, dict)
            or result.get("ref") != "refs/heads/" + branch
            or not isinstance(result.get("object"), dict)
            or result["object"].get("sha") != sha
        ):
            raise RelayGitHubError(502, "branch write outcome unverifiable")
        return {"ref": result["ref"], "sha": sha}

    def wf_labels_set(self, repo, args):
        _only(
            args,
            (
                "repo",
                "number",
                "labels",
            ),
        )
        number = _int(args, "number")
        labels = args.get("labels")
        if (
            not isinstance(labels, list)
            or not 1 <= len(labels) <= 50
            or any(
                not isinstance(v, str) or not re.fullmatch(r"[A-Za-z0-9_. /-]{1,100}", v)
                for v in labels
            )
        ):
            raise Refused("invalid labels")
        data = self.client._call(
            "PUT", f"/repos/{repo}/issues/{number}/labels", {"labels": labels}, bearer=self.bearer
        )
        if not isinstance(data, list):
            raise RelayGitHubError(502, "label receipt unverifiable")
        result = data
        if any(
            not isinstance(x, dict)
            or isinstance(x.get("id"), bool)
            or not isinstance(x.get("id"), int)
            or x["id"] <= 0
            or not isinstance(x.get("name"), str)
            for x in result
        ):
            raise RelayGitHubError(502, "label receipt unverifiable")
        if {x["name"].casefold() for x in result} != {x.casefold() for x in labels}:
            raise RelayGitHubError(502, "label receipt does not match requested labels")
        return {"labels": [{k: x.get(k) for k in ("id", "name", "color")} for x in result]}

    def wf_reviewers_request(self, repo, args):
        _only(
            args,
            (
                "repo",
                "number",
                "reviewers",
            ),
        )
        number = _int(args, "number")
        reviewers = args.get("reviewers")
        if (
            not isinstance(reviewers, list)
            or not 1 <= len(reviewers) <= 50
            or any(
                not isinstance(v, str) or not re.fullmatch(r"[A-Za-z0-9_. /-]{1,100}", v)
                for v in reviewers
            )
        ):
            raise Refused("invalid reviewers")
        data = self.client._call(
            "POST",
            f"/repos/{repo}/pulls/{number}/requested_reviewers",
            {"reviewers": reviewers},
            bearer=self.bearer,
        )
        if not isinstance(data, dict):
            raise RelayGitHubError(502, "write receipt unverifiable")
        result = data
        if (
            not isinstance(result.get("requested_reviewers"), list)
            or isinstance(result.get("id"), bool)
            or not isinstance(result.get("id"), int)
            or result["id"] <= 0
        ):
            raise RelayGitHubError(502, "write outcome unverifiable")
        if result.get("number") != number or type(result.get("number")) is not int:
            raise RelayGitHubError(502, "reviewer receipt PR identity unverifiable")
        observed = set()
        for person in result["requested_reviewers"]:
            if not isinstance(person, dict) or not isinstance(person.get("login"), str):
                raise RelayGitHubError(502, "reviewer receipt unverifiable")
            observed.add(person["login"].casefold())
        requested = {name.casefold() for name in reviewers}
        if not requested <= observed:
            raise RelayGitHubError(502, "reviewer receipt membership unverifiable")
        return {"result": result}

    def wf_reviewers_remove(self, repo, args):
        _only(
            args,
            (
                "repo",
                "number",
                "reviewers",
            ),
        )
        number = _int(args, "number")
        reviewers = args.get("reviewers")
        if (
            not isinstance(reviewers, list)
            or not 1 <= len(reviewers) <= 50
            or any(
                not isinstance(v, str) or not re.fullmatch(r"[A-Za-z0-9_. /-]{1,100}", v)
                for v in reviewers
            )
        ):
            raise Refused("invalid reviewers")
        data = self.client._call(
            "DELETE",
            f"/repos/{repo}/pulls/{number}/requested_reviewers",
            {"reviewers": reviewers},
            bearer=self.bearer,
        )
        if not isinstance(data, dict):
            raise RelayGitHubError(502, "write receipt unverifiable")
        result = data
        if (
            not isinstance(result.get("requested_reviewers"), list)
            or isinstance(result.get("id"), bool)
            or not isinstance(result.get("id"), int)
            or result["id"] <= 0
        ):
            raise RelayGitHubError(502, "write outcome unverifiable")
        if result.get("number") != number or type(result.get("number")) is not int:
            raise RelayGitHubError(502, "reviewer receipt PR identity unverifiable")
        observed = set()
        for person in result["requested_reviewers"]:
            if not isinstance(person, dict) or not isinstance(person.get("login"), str):
                raise RelayGitHubError(502, "reviewer receipt unverifiable")
            observed.add(person["login"].casefold())
        requested = {name.casefold() for name in reviewers}
        if requested & observed:
            raise RelayGitHubError(502, "reviewer receipt membership unverifiable")
        return {"result": result}

    def wf_release_create(self, repo, args):
        _only(
            args,
            (
                "repo",
                "tag",
                "name",
                "body",
                "draft",
                "prerelease",
            ),
        )
        tag = self._text(args, "tag")
        name = self._text(args, "name")
        body = self._text(args, "body")
        draft = args.get("draft")
        if not isinstance(draft, bool):
            raise Refused("invalid draft")
        prerelease = args.get("prerelease")
        if not isinstance(prerelease, bool):
            raise Refused("invalid prerelease")
        tag = _ref(args, "tag")
        data = self.client._call(
            "POST",
            f"/repos/{repo}/releases",
            {"tag_name": tag, "name": name, "body": body, "draft": draft, "prerelease": prerelease},
            bearer=self.bearer,
        )
        if not isinstance(data, dict):
            raise RelayGitHubError(502, "write receipt unverifiable")
        result = data
        if (
            not isinstance(result.get("id"), int)
            or isinstance(result.get("id"), bool)
            or result["id"] <= 0
            or result.get("tag_name") != tag
        ):
            raise RelayGitHubError(502, "write outcome unverifiable")
        return {"result": result}

    def wf_release_update(self, repo, args):
        _only(
            args,
            (
                "repo",
                "release_id",
                "name",
                "body",
                "draft",
                "prerelease",
            ),
        )
        release_id = _int(args, "release_id")
        name = self._text(args, "name")
        body = self._text(args, "body")
        draft = args.get("draft")
        if not isinstance(draft, bool):
            raise Refused("invalid draft")
        prerelease = args.get("prerelease")
        if not isinstance(prerelease, bool):
            raise Refused("invalid prerelease")
        data = self.client._call(
            "PATCH",
            f"/repos/{repo}/releases/{release_id}",
            {"name": name, "body": body, "draft": draft, "prerelease": prerelease},
            bearer=self.bearer,
        )
        if not isinstance(data, dict):
            raise RelayGitHubError(502, "write receipt unverifiable")
        result = data
        if (
            not isinstance(result.get("id"), int)
            or isinstance(result.get("id"), bool)
            or result["id"] <= 0
            or result["id"] != release_id
        ):
            raise RelayGitHubError(502, "write outcome unverifiable")
        return {"result": result}

    def wf_issue_comment_update(self, repo, args):
        _only(
            args,
            (
                "repo",
                "comment_id",
                "body",
            ),
        )
        comment_id = _int(args, "comment_id")
        body = self._text(args, "body")
        data = self.client._call(
            "PATCH",
            f"/repos/{repo}/issues/comments/{comment_id}",
            {"body": body},
            bearer=self.bearer,
        )
        result = _obj(data)
        if (
            result.get("id") != comment_id
            or isinstance(result.get("id"), bool)
            or not isinstance(result.get("id"), int)
        ):
            raise RelayGitHubError(502, "response unverifiable")
        return {"result": result}

    def wf_issue_comment_delete(self, repo, args):
        _only(
            args,
            (
                "repo",
                "comment_id",
            ),
        )
        comment_id = _int(args, "comment_id")
        data = self.client._call(
            "DELETE", f"/repos/{repo}/issues/comments/{comment_id}", None, bearer=self.bearer
        )
        return {"deleted": True}

    def wf_review_comment_update(self, repo, args):
        _only(
            args,
            (
                "repo",
                "comment_id",
                "body",
            ),
        )
        comment_id = _int(args, "comment_id")
        body = self._text(args, "body")
        data = self.client._call(
            "PATCH",
            f"/repos/{repo}/pulls/comments/{comment_id}",
            {"body": body},
            bearer=self.bearer,
        )
        result = _obj(data)
        if (
            result.get("id") != comment_id
            or isinstance(result.get("id"), bool)
            or not isinstance(result.get("id"), int)
        ):
            raise RelayGitHubError(502, "response unverifiable")
        return {"result": result}

    def wf_review_comment_delete(self, repo, args):
        _only(
            args,
            (
                "repo",
                "comment_id",
            ),
        )
        comment_id = _int(args, "comment_id")
        data = self.client._call(
            "DELETE", f"/repos/{repo}/pulls/comments/{comment_id}", None, bearer=self.bearer
        )
        return {"deleted": True}

    @staticmethod
    def _release_summary(value):
        value = _obj(value)
        if (
            isinstance(value.get("id"), bool)
            or not isinstance(value.get("id"), int)
            or value["id"] <= 0
        ):
            raise RelayGitHubError(502, "release receipt unverifiable")
        result = {"id": value["id"]}
        for key in ("tag_name", "name", "html_url", "created_at", "published_at"):
            item = value.get(key)
            if item is not None and (not isinstance(item, str) or len(item.encode("utf-8")) > 2048):
                raise RelayGitHubError(502, "release receipt unverifiable")
            result[key] = item
        for key in ("draft", "prerelease"):
            if not isinstance(value.get(key), bool):
                raise RelayGitHubError(502, "release flags unverifiable")
            result[key] = value[key]
        return result

    def wf_release_list(self, repo, args):
        _only(args, ("repo",))
        data = self.client._call(
            "GET", f"/repos/{repo}/releases?per_page=50", None, bearer=self.bearer
        )
        return {
            "releases": [self._release_summary(x) for x in _arr(data)[:MAX_PAGE]],
            "complete": False,
        }

    def wf_release_get(self, repo, args):
        _only(
            args,
            (
                "repo",
                "release_id",
            ),
        )
        release_id = _int(args, "release_id")
        data = self.client._call(
            "GET", f"/repos/{repo}/releases/{release_id}", None, bearer=self.bearer
        )
        result = _obj(data)
        if (
            result.get("id") != release_id
            or isinstance(result.get("id"), bool)
            or not isinstance(result.get("id"), int)
        ):
            raise RelayGitHubError(502, "response unverifiable")
        return {"result": self._release_summary(result)}

    def wf_release_delete(self, repo, args):
        _only(
            args,
            (
                "repo",
                "release_id",
            ),
        )
        release_id = _int(args, "release_id")
        data = self.client._call(
            "DELETE", f"/repos/{repo}/releases/{release_id}", None, bearer=self.bearer
        )
        return {"deleted": True}

    def wf_branch_update(self, repo, args):
        _only(
            args,
            (
                "repo",
                "branch",
                "sha",
                "expected_sha",
            ),
        )
        branch = _ref(args, "branch")
        sha = _sha(args, "sha")
        expected_sha = _sha(args, "expected_sha")
        name = branch
        try:
            repository = _obj(self._get(f"/repos/{repo}"))
        except RelayGitHubError as exc:
            if exc.status in (0, 408) or exc.status >= 500:
                raise PreReadUnavailable(
                    "repository pre-read unavailable; no write attempted"
                ) from None
            raise
        default_branch = repository.get("default_branch")
        if not isinstance(default_branch, str) or not default_branch or name == default_branch:
            raise Refused("default branch mutation refused")
        path = f"/repos/{repo}/git/refs/heads/{quote(name, safe='/')}"
        try:
            prior = _obj(self._get(f"/repos/{repo}/git/ref/heads/{quote(name, safe='/')}"))
        except RelayGitHubError as exc:
            if exc.status in (0, 408) or exc.status >= 500:
                raise PreReadUnavailable("ref pre-read unavailable; no write attempted") from None
            raise
        if _sub(prior, "object").get("sha") != expected_sha:
            raise Refused("ref changed")
        data = self.client._call("PATCH", path, {"sha": sha, "force": False}, bearer=self.bearer)
        result = _obj(data)
        got_sha = _sub(result, "object").get("sha")
        if (
            result.get("ref") != "refs/heads/" + name
            or not isinstance(got_sha, str)
            or not _SHA.fullmatch(got_sha)
            or got_sha != sha
        ):
            raise RelayGitHubError(502, "ref outcome unverifiable")
        return {"ref": result["ref"], "sha": got_sha}

    def wf_branch_delete(self, repo, args):
        _only(
            args,
            (
                "repo",
                "branch",
                "expected_sha",
            ),
        )
        branch = _ref(args, "branch")
        expected_sha = _sha(args, "expected_sha")
        name = branch
        try:
            repository = _obj(self._get(f"/repos/{repo}"))
        except RelayGitHubError as exc:
            if exc.status in (0, 408) or exc.status >= 500:
                raise PreReadUnavailable(
                    "repository pre-read unavailable; no write attempted"
                ) from None
            raise
        default_branch = repository.get("default_branch")
        if not isinstance(default_branch, str) or not default_branch or name == default_branch:
            raise Refused("default branch mutation refused")
        path = f"/repos/{repo}/git/refs/heads/{quote(name, safe='/')}"
        try:
            prior = _obj(self._get(f"/repos/{repo}/git/ref/heads/{quote(name, safe='/')}"))
        except RelayGitHubError as exc:
            if exc.status in (0, 408) or exc.status >= 500:
                raise PreReadUnavailable("ref pre-read unavailable; no write attempted") from None
            raise
        if _sub(prior, "object").get("sha") != expected_sha:
            raise Refused("ref changed")
        data = self.client._call("DELETE", path, None, bearer=self.bearer)
        return {"deleted": True, "ref": "refs/heads/" + name}

    def wf_tag_create(self, repo, args):
        _only(
            args,
            (
                "repo",
                "tag",
                "sha",
            ),
        )
        tag = _ref(args, "tag")
        sha = _sha(args, "sha")
        name = tag
        path = f"/repos/{repo}/git/refs"
        data = self.client._call(
            "POST", path, {"ref": "refs/tags/" + name, "sha": sha}, bearer=self.bearer
        )
        result = _obj(data)
        got_sha = _sub(result, "object").get("sha")
        if (
            result.get("ref") != "refs/tags/" + name
            or not isinstance(got_sha, str)
            or not _SHA.fullmatch(got_sha)
            or got_sha != sha
        ):
            raise RelayGitHubError(502, "ref outcome unverifiable")
        return {"ref": result["ref"], "sha": got_sha}

    def wf_tag_get(self, repo, args):
        _only(
            args,
            (
                "repo",
                "tag",
            ),
        )
        tag = _ref(args, "tag")
        name = tag
        path = f"/repos/{repo}/git/ref/tags/{quote(name, safe='/')}"
        data = self.client._call("GET", path, None, bearer=self.bearer)
        result = _obj(data)
        got_sha = _sub(result, "object").get("sha")
        if (
            result.get("ref") != "refs/tags/" + name
            or not isinstance(got_sha, str)
            or not _SHA.fullmatch(got_sha)
        ):
            raise RelayGitHubError(502, "ref outcome unverifiable")
        return {"ref": result["ref"], "sha": got_sha}

    def wf_tag_delete(self, repo, args):
        _only(
            args,
            (
                "repo",
                "tag",
                "expected_sha",
            ),
        )
        tag = _ref(args, "tag")
        expected_sha = _sha(args, "expected_sha")
        name = tag
        path = f"/repos/{repo}/git/refs/tags/{quote(name, safe='/')}"
        try:
            prior = _obj(self._get(f"/repos/{repo}/git/ref/tags/{quote(name, safe='/')}"))
        except RelayGitHubError as exc:
            if exc.status in (0, 408) or exc.status >= 500:
                raise PreReadUnavailable("ref pre-read unavailable; no write attempted") from None
            raise
        if _sub(prior, "object").get("sha") != expected_sha:
            raise Refused("ref changed")
        data = self.client._call("DELETE", path, None, bearer=self.bearer)
        return {"deleted": True, "ref": "refs/tags/" + name}

    def _pr_node(self, repo, number):
        try:
            data = _obj(self._get(f"/repos/{repo}/pulls/{number}"))
        except RelayGitHubError as exc:
            if exc.status in (0, 408) or exc.status >= 500:
                raise PreReadUnavailable("PR pre-read unavailable; no write attempted") from None
            raise
        node = data.get("node_id")
        if not isinstance(node, str) or not re.fullmatch(r"[A-Za-z0-9_/=+-]{1,200}", node):
            raise Refused("PR node unavailable")
        return node

    def _fixed_graph(self, query, variables, field, write=True):
        data = self.client._call(
            "POST", "/graphql", {"query": query, "variables": variables}, bearer=self.bearer
        )
        # GraphQL HTTP200 errors may follow partial mutation application; never call them definitely unsent.
        if (
            not isinstance(data, dict)
            or data.get("errors")
            or not isinstance(data.get("data"), dict)
        ):
            raise RelayGitHubError(502, "GraphQL outcome unverifiable")
        result = data["data"].get(field)
        if not isinstance(result, dict):
            raise RelayGitHubError(502, "GraphQL result unverifiable")
        return result

    def wf_pr_draft(self, repo, args):
        _only(args, ("repo", "number"))
        node = self._pr_node(repo, _int(args, "number"))
        query = "mutation($id: ID!) { convertPullRequestToDraft(input:{pullRequestId:$id}) { pullRequest { id isDraft } } }"
        result = self._fixed_graph(query, {"id": node}, "convertPullRequestToDraft")
        pull = _sub(result, "pullRequest")
        if pull.get("id") != node or pull.get("isDraft") is not True:
            raise RelayGitHubError(502, "PR mutation outcome unverifiable")
        return {"pull": pull}

    def wf_pr_ready(self, repo, args):
        _only(args, ("repo", "number"))
        node = self._pr_node(repo, _int(args, "number"))
        query = "mutation($id: ID!) { markPullRequestReadyForReview(input:{pullRequestId:$id}) { pullRequest { id isDraft } } }"
        result = self._fixed_graph(query, {"id": node}, "markPullRequestReadyForReview")
        pull = _sub(result, "pullRequest")
        if pull.get("id") != node or pull.get("isDraft") is not False:
            raise RelayGitHubError(502, "PR mutation outcome unverifiable")
        return {"pull": pull}

    def wf_automerge_disable(self, repo, args):
        _only(args, ("repo", "number"))
        node = self._pr_node(repo, _int(args, "number"))
        query = "mutation($id: ID!) { disablePullRequestAutoMerge(input:{pullRequestId:$id}) { pullRequest { id autoMergeRequest { enabledAt } } } }"
        result = self._fixed_graph(query, {"id": node}, "disablePullRequestAutoMerge")
        pull = _sub(result, "pullRequest")
        if (
            pull.get("id") != node
            or "autoMergeRequest" not in pull
            or pull["autoMergeRequest"] is not None
        ):
            raise RelayGitHubError(502, "PR mutation outcome unverifiable")
        return {"pull": pull}

    def wf_automerge_enable(self, repo, args):
        _only(args, ("repo", "number", "method"))
        method = _enum(args, "method", ("SQUASH", "MERGE", "REBASE"))
        node = self._pr_node(repo, _int(args, "number"))
        query = "mutation($id: ID!, $method: PullRequestMergeMethod!) { enablePullRequestAutoMerge(input:{pullRequestId:$id,mergeMethod:$method}) { pullRequest { id autoMergeRequest { enabledAt } } } }"
        result = self._fixed_graph(
            query, {"id": node, "method": method}, "enablePullRequestAutoMerge"
        )
        pull = _sub(result, "pullRequest")
        if pull.get("id") != node or not isinstance(pull.get("autoMergeRequest"), dict):
            raise RelayGitHubError(502, "auto-merge outcome unverifiable")
        return {"pull": pull}

    def wf_review_threads(self, repo, args):
        _only(args, ("repo", "number"))
        number = _int(args, "number")
        owner, name = repo.split("/")
        query = "query($owner:String!, $name:String!, $number:Int!) { repository(owner:$owner,name:$name) { pullRequest(number:$number) { reviewThreads(first:50) { pageInfo { hasNextPage endCursor } nodes { id isResolved isOutdated path line comments(first:20) { nodes { id databaseId body author { login } } pageInfo { hasNextPage endCursor } } } } } } }"
        result = self._fixed_graph(
            query, {"owner": owner, "name": name, "number": number}, "repository", write=False
        )
        threads = _sub(_sub(result, "pullRequest"), "reviewThreads")
        if not isinstance(threads.get("nodes"), list):
            raise RelayGitHubError(502, "thread result unverifiable")
        return {"threads": threads}

    def wf_assignees_add(self, repo, args):
        _only(
            args,
            (
                "repo",
                "number",
                "assignees",
            ),
        )
        number = _int(args, "number")
        assignees = args.get("assignees")
        if (
            not isinstance(assignees, list)
            or not 1 <= len(assignees) <= 10
            or any(
                not isinstance(v, str)
                or not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?", v)
                for v in assignees
            )
        ):
            raise Refused("invalid assignees")
        path = f"/repos/{repo}/issues/{number}/assignees"
        data = self.client._call("POST", path, {"assignees": assignees}, bearer=self.bearer)
        result = _obj(data)
        if (
            not isinstance(result.get("id"), int)
            or isinstance(result.get("id"), bool)
            or result["id"] <= 0
            or result.get("number") != number
            or not isinstance(result.get("assignees"), list)
        ):
            raise RelayGitHubError(502, "assignee outcome unverifiable")
        logins = []
        for item in result["assignees"]:
            login = item.get("login") if isinstance(item, dict) else None
            if not isinstance(login, str) or not re.fullmatch(
                r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?", login
            ):
                raise RelayGitHubError(502, "assignee outcome unverifiable")
            logins.append(login)
        requested = {x.lower() for x in assignees}
        actual = {x.lower() for x in logins}
        if not requested <= actual:
            raise RelayGitHubError(502, "assignee state unverifiable")
        return {
            "result": {
                "id": result["id"],
                "number": number,
                "assignees": [{"login": x} for x in logins],
            }
        }

    def wf_assignees_remove(self, repo, args):
        _only(
            args,
            (
                "repo",
                "number",
                "assignees",
            ),
        )
        number = _int(args, "number")
        assignees = args.get("assignees")
        if (
            not isinstance(assignees, list)
            or not 1 <= len(assignees) <= 10
            or any(
                not isinstance(v, str)
                or not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?", v)
                for v in assignees
            )
        ):
            raise Refused("invalid assignees")
        path = f"/repos/{repo}/issues/{number}/assignees"
        data = self.client._call("DELETE", path, {"assignees": assignees}, bearer=self.bearer)
        result = _obj(data)
        if (
            not isinstance(result.get("id"), int)
            or isinstance(result.get("id"), bool)
            or result["id"] <= 0
            or result.get("number") != number
            or not isinstance(result.get("assignees"), list)
        ):
            raise RelayGitHubError(502, "assignee outcome unverifiable")
        logins = []
        for item in result["assignees"]:
            login = item.get("login") if isinstance(item, dict) else None
            if not isinstance(login, str) or not re.fullmatch(
                r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?", login
            ):
                raise RelayGitHubError(502, "assignee outcome unverifiable")
            logins.append(login)
        requested = {x.lower() for x in assignees}
        actual = {x.lower() for x in logins}
        if requested & actual:
            raise RelayGitHubError(502, "assignee state unverifiable")
        return {
            "result": {
                "id": result["id"],
                "number": number,
                "assignees": [{"login": x} for x in logins],
            }
        }

    def wf_issue_lock(self, repo, args):
        _only(
            args,
            (
                "repo",
                "number",
                "reason",
            ),
        )
        number = _int(args, "number")
        reason = _enum(args, "reason", ("off-topic", "too heated", "resolved", "spam"))
        path = f"/repos/{repo}/issues/{number}/lock"
        data = self.client._call("PUT", path, {"lock_reason": reason}, bearer=self.bearer)
        return {"completed": True}

    def wf_issue_unlock(self, repo, args):
        _only(
            args,
            (
                "repo",
                "number",
            ),
        )
        number = _int(args, "number")
        path = f"/repos/{repo}/issues/{number}/lock"
        data = self.client._call("DELETE", path, None, bearer=self.bearer)
        return {"completed": True}

    def wf_label_create(self, repo, args):
        _only(
            args,
            (
                "repo",
                "name",
                "color",
                "description",
            ),
        )
        name = self._text(args, "name", 100)
        color = self._text(args, "color", 1024)
        if not re.fullmatch(r"[0-9a-fA-F]{6}", color):
            raise Refused("invalid color")
        description = self._text(args, "description", 1024)
        path = f"/repos/{repo}/labels"
        data = self.client._call(
            "POST",
            path,
            {"name": name, "color": color, "description": description},
            bearer=self.bearer,
        )
        result = _obj(data)
        if (
            not isinstance(result.get("id"), int)
            or isinstance(result.get("id"), bool)
            or result["id"] <= 0
            or result.get("name") != name
            or not isinstance(result.get("color"), str)
            or result["color"].lower() != color.lower()
        ):
            raise RelayGitHubError(502, "label outcome unverifiable")
        return {"result": {k: result.get(k) for k in ("id", "name", "color", "description")}}

    def wf_label_delete(self, repo, args):
        _only(
            args,
            (
                "repo",
                "name",
            ),
        )
        name = self._text(args, "name", 100)
        path = "/repos/" + repo + "/labels/" + quote(name, safe="")
        data = self.client._call("DELETE", path, None, bearer=self.bearer)
        return {"completed": True}

    def wf_workflow_enable(self, repo, args):
        _only(args, ("repo", "workflow_id"))
        value = _int(args, "workflow_id")
        self.client._call(
            "PUT",
            f"/repos/{repo}/actions/workflows/{value}/enable",
            None,
            bearer=self.bearer,
            expected_status=204,
        )
        return {"completed": True, "workflow_id": value, "state": "enabled"}

    def wf_workflow_disable(self, repo, args):
        _only(args, ("repo", "workflow_id"))
        value = _int(args, "workflow_id")
        self.client._call(
            "PUT",
            f"/repos/{repo}/actions/workflows/{value}/disable",
            None,
            bearer=self.bearer,
            expected_status=204,
        )
        return {"completed": True, "workflow_id": value, "state": "disabled"}

    def wf_run_delete(self, repo, args):
        _only(args, ("repo", "run_id"))
        value = _int(args, "run_id")
        self.client._call(
            "DELETE",
            f"/repos/{repo}/actions/runs/{value}",
            None,
            bearer=self.bearer,
            expected_status=204,
        )
        return {"completed": True, "run_id": value}

    def wf_artifact_delete(self, repo, args):
        _only(args, ("repo", "artifact_id"))
        value = _int(args, "artifact_id")
        self.client._call(
            "DELETE",
            f"/repos/{repo}/actions/artifacts/{value}",
            None,
            bearer=self.bearer,
            expected_status=204,
        )
        return {"completed": True, "artifact_id": value}

    # ---- reads ----
    def wf_pr_get(self, repo, args):
        _only(
            args,
            (
                "repo",
                "number",
            ),
        )
        number = _int(args, "number")
        data = self._get(f"/repos/{repo}/pulls/{number}")
        return {"pull": _obj(data)}

    def wf_pr_reviews(self, repo, args):
        _only(
            args,
            (
                "repo",
                "number",
                "per_page",
            ),
        )
        number = _int(args, "number")
        per_page = _page(args)
        data = self._get(f"/repos/{repo}/pulls/{number}/reviews?per_page={per_page}")
        return {"reviews": _arr(data)[:MAX_PAGE], "complete": False}

    def wf_pr_comments(self, repo, args):
        _only(
            args,
            (
                "repo",
                "number",
                "per_page",
            ),
        )
        number = _int(args, "number")
        per_page = _page(args)
        data = self._get(f"/repos/{repo}/pulls/{number}/comments?per_page={per_page}")
        return {"comments": _arr(data)[:MAX_PAGE], "complete": False}

    def wf_issue_comments(self, repo, args):
        _only(
            args,
            (
                "repo",
                "number",
                "per_page",
            ),
        )
        number = _int(args, "number")
        per_page = _page(args)
        data = self._get(f"/repos/{repo}/issues/{number}/comments?per_page={per_page}")
        return {"comments": _arr(data)[:MAX_PAGE], "complete": False}

    def wf_commit_get(self, repo, args):
        _only(
            args,
            (
                "repo",
                "sha",
            ),
        )
        sha = _sha(args, "sha")
        data = self._get(f"/repos/{repo}/commits/{sha}")
        return {"commit": _obj(data)}

    def wf_compare(self, repo, args):
        _only(
            args,
            (
                "repo",
                "base",
                "head",
            ),
        )
        base = quote(_ref(args, "base"), safe="")
        head = quote(_ref(args, "head"), safe="")
        data = self._get(f"/repos/{repo}/compare/{base}...{head}")
        return {"comparison": _obj(data)}

    def wf_branch_get(self, repo, args):
        _only(
            args,
            (
                "repo",
                "branch",
            ),
        )
        branch = quote(_ref(args, "branch"), safe="")
        data = self._get(f"/repos/{repo}/branches/{branch}")
        return {"branch": _obj(data)}

    def wf_repo_get(self, repo, args):
        _only(args, ("repo",))
        data = self._get(f"/repos/{repo}")
        return {"repository": _obj(data)}

    def wf_labels(self, repo, args):
        _only(
            args,
            (
                "repo",
                "per_page",
            ),
        )
        per_page = _page(args)
        data = self._get(f"/repos/{repo}/labels?per_page={per_page}")
        return {"labels": _arr(data)[:MAX_PAGE], "complete": False}

    def wf_workflows(self, repo, args):
        _only(
            args,
            (
                "repo",
                "per_page",
            ),
        )
        per_page = _page(args)
        data = self._get(f"/repos/{repo}/actions/workflows?per_page={per_page}")
        return {"workflows": _obj(data)}

    def wf_run_get(self, repo, args):
        _only(
            args,
            (
                "repo",
                "run_id",
            ),
        )
        run_id = _int(args, "run_id")
        data = self._get(f"/repos/{repo}/actions/runs/{run_id}")
        return {"run": _obj(data)}

    def wf_run_artifacts(self, repo, args):
        _only(
            args,
            (
                "repo",
                "run_id",
                "per_page",
            ),
        )
        run_id = _int(args, "run_id")
        per_page = _page(args)
        data = self._get(f"/repos/{repo}/actions/runs/{run_id}/artifacts?per_page={per_page}")
        return {"artifacts": _obj(data)}

    def wf_pr_list(self, repo, args):
        _only(args, ("repo", "state", "head", "base", "per_page"))
        state = _enum(args, "state", ("open", "closed", "all"), "open")
        q = f"state={state}&per_page={_page(args)}"
        if "base" in args:
            q += "&base=" + quote(_ref(args, "base"), safe="/")
        if "head" in args:
            q += "&head=" + quote(repo.split("/")[0] + ":" + _ref(args, "head"), safe="")
        data = _arr(self._get(f"/repos/{repo}/pulls?{q}"))
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
                for p in data[:MAX_PROJECT]
            ],
            "complete": False,
        }

    def wf_pr_files(self, repo, args):
        _only(args, ("repo", "number", "per_page"))
        number = _int(args, "number")
        data = _arr(self._get(f"/repos/{repo}/pulls/{number}/files?per_page={_page(args)}"))
        return {
            "files": [
                {k: f.get(k) for k in ("filename", "status", "additions", "deletions", "sha")}
                for f in data[:MAX_PROJECT]
            ],
            "complete": False,
        }

    def wf_file_get(self, repo, args):
        _only(args, ("repo", "path", "ref"))
        path, ref = _path(args), _ref(args, "ref")
        where = quote(path, safe="/")
        data = self._get(f"/repos/{repo}/contents/{where}?ref={quote(ref, safe='/')}")
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
        data = _obj(self._get(f"/repos/{repo}/git/trees/{quote(ref, safe='')}?recursive=1"))
        tree = data.get("tree")
        if tree is not None and not isinstance(tree, list):
            raise RelayGitHubError(502, "unexpected upstream shape")
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
        data = _arr(self._get(f"/repos/{repo}/branches?per_page={_page(args)}"))
        return {
            "branches": [
                {"name": b.get("name"), "sha": (b.get("commit") or {}).get("sha")}
                for b in data[:MAX_PROJECT]
            ],
            "complete": False,
        }

    def wf_commits(self, repo, args):
        _only(args, ("repo", "ref", "path", "per_page"))
        q = f"sha={quote(_ref(args, 'ref'), safe='')}&per_page={_page(args)}"
        if "path" in args:
            q += "&path=" + quote(_path(args), safe="")
        data = _arr(self._get(f"/repos/{repo}/commits?{q}"))
        return {
            "commits": [
                {
                    "sha": c.get("sha"),
                    "message": ((c.get("commit") or {}).get("message") or "")[:200],
                    "author": (c.get("author") or {}).get("login"),
                    "date": ((c.get("commit") or {}).get("author") or {}).get("date"),
                }
                for c in data[:MAX_PROJECT]
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
        data = _obj(self._get(f"/search/code?q={q}&per_page={_page(args)}"))
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
            q += "&branch=" + quote(_ref(args, "branch"), safe="")
        if "head_sha" in args:
            q += "&head_sha=" + _sha(args, "head_sha")
        if "status" in args:
            q += "&status=" + _enum(
                args, "status", ("queued", "in_progress", "completed", "success", "failure")
            )
        data = _obj(self._get(f"/repos/{repo}/actions/runs?{q}"))
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
                for r in _dicts((data or {}).get("workflow_runs"))[:MAX_PROJECT]
            ],
            "complete": False,
        }

    def wf_run_jobs(self, repo, args):
        _only(args, ("repo", "run_id", "per_page"))
        run_id = _int(args, "run_id")
        data = _obj(self._get(f"/repos/{repo}/actions/runs/{run_id}/jobs?per_page={_page(args)}"))
        return {
            "jobs": [
                {
                    "id": j.get("id"),
                    "name": j.get("name"),
                    "status": j.get("status"),
                    "conclusion": j.get("conclusion"),
                    "steps": [
                        {k: s.get(k) for k in ("name", "status", "conclusion")}
                        for s in _dicts(j.get("steps"))[:100]
                    ],
                }
                for j in _dicts((data or {}).get("jobs"))[:MAX_PROJECT]
            ],
            "complete": False,
        }

    def wf_checks(self, repo, args):
        """Check runs for one commit (Kody, Seer, merge-gate and Actions jobs alike)."""
        _only(args, ("repo", "ref", "per_page"))
        ref = (
            _sha(args, "ref")
            if re.fullmatch(r"[0-9a-f]{40}", str(args.get("ref", "")))
            else _ref(args, "ref")
        )
        data = _obj(
            self._get(
                f"/repos/{repo}/commits/{quote(ref, safe='')}/check-runs?per_page={_page(args)}"
            )
        )
        return {
            "checks": [
                {k: c.get(k) for k in ("id", "name", "status", "conclusion", "completed_at")}
                for c in _dicts((data or {}).get("check_runs"))[:MAX_PROJECT]
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

    def wf_file_delete(self, repo, args):
        _only(args, ("repo", "path", "branch", "message", "prior_sha"))
        path, branch, sha = _path(args), _ref(args, "branch"), _sha(args, "prior_sha")
        if branch.startswith("refs/"):
            raise Refused("qualified branch refs are not accepted")
        if _workflow_path(path):
            raise Refused("workflow files are not writable here")
        message = args.get("message")
        if (
            not isinstance(message, str)
            or not message.strip()
            or len(message.encode("utf-8")) > 500
            or "\x00" in message
            or find_secret(message)
        ):
            raise Refused("invalid message")
        try:
            info = self._get(f"/repos/{repo}")
            if (
                not isinstance(info, dict)
                or not isinstance(info.get("default_branch"), str)
                or not info["default_branch"]
            ):
                raise RelayGitHubError(502, "repository pre-read unverifiable")
        except RelayGitHubError as exc:
            if exc.status in (0, 408) or exc.status >= 500:
                raise PreReadUnavailable(
                    "repository pre-read unavailable; no write attempted"
                ) from None
            raise
        if branch == info["default_branch"]:
            raise Refused("default branch is not writable")
        try:
            target = self._get(f"/repos/{repo}/branches/{quote(branch, safe='')}")
            if (
                not isinstance(target, dict)
                or target.get("name") != branch
                or not isinstance(target.get("commit"), dict)
                or not isinstance(target["commit"].get("sha"), str)
                or not _SHA.fullmatch(target["commit"]["sha"])
            ):
                raise RelayGitHubError(502, "branch pre-read unverifiable")
        except RelayGitHubError as exc:
            if exc.status in (0, 408) or exc.status >= 500:
                raise PreReadUnavailable(
                    "branch pre-read unavailable; no write attempted"
                ) from None
            raise
        result = self.client._call(
            "DELETE",
            f"/repos/{repo}/contents/{quote(path, safe='/')}",
            {"message": message, "sha": sha, "branch": branch},
            bearer=self.bearer,
        )
        if (
            not isinstance(result, dict)
            or "content" not in result
            or result["content"] is not None
            or not isinstance(result.get("commit"), dict)
            or not isinstance(result["commit"].get("sha"), str)
            or not _SHA.fullmatch(result["commit"]["sha"])
        ):
            raise RelayGitHubError(502, "file deletion receipt unverifiable")
        return {"deleted": True, "path": path, "branch": branch, "commit": result["commit"]["sha"]}

    def wf_file_put(self, repo, args):
        _only(args, ("repo", "path", "branch", "message", "content_b64", "prior_sha"))
        path, branch = _path(args), _ref(args, "branch")
        if _workflow_path(path):
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
        payload = {"message": message, "content": content, "branch": branch}
        if "prior_sha" in args:
            payload["sha"] = _sha(args, "prior_sha")
        repo_info = _obj(self._get(f"/repos/{repo}"))
        default = repo_info.get("default_branch")
        if not isinstance(default, str) or not default or branch == default:
            raise Refused("default branch is not writable")
        data = self.client._call(
            "PUT", f"/repos/{repo}/contents/{quote(path, safe='/')}", payload, bearer=self.bearer
        )
        result = _obj(data)
        commit = _sub(result, "commit").get("sha")
        content_receipt = _sub(result, "content")
        if (
            not isinstance(commit, str)
            or not _SHA.fullmatch(commit)
            or content_receipt.get("path") != path
        ):
            raise RelayGitHubError(502, "file write receipt unverifiable")
        return {"path": path, "branch": branch, "commit": commit}

    def _entry_mode(self, repo, tree_sha, path, cache):
        """Mode of an existing blob at path, so an update never resets an executable bit."""
        sha = tree_sha
        parts = path.split("/")
        for i, part in enumerate(parts):
            if sha not in cache:
                cache[sha] = _obj(self._get(f"/repos/{repo}/git/trees/{sha}"))
            entries = {e.get("path"): e for e in _dicts(cache[sha].get("tree"))}
            entry = entries.get(part)
            if entry is None:
                return None
            last = i == len(parts) - 1
            if last:
                return entry.get("mode") if entry.get("type") == "blob" else None
            if entry.get("type") != "tree" or not _SHA.fullmatch(str(entry.get("sha"))):
                return None
            sha = entry["sha"]
        return None

    def wf_files_put(self, repo, args):
        """One commit changing several files on a non-default branch, via git-data."""
        _only(args, ("repo", "branch", "message", "expected_head_sha", "files"))
        branch, head = _ref(args, "branch"), _sha(args, "expected_head_sha")
        message = args.get("message")
        if not isinstance(message, str) or not 1 <= len(message) <= 500 or "\x00" in message:
            raise Refused("invalid message")
        files = args.get("files")
        if not isinstance(files, list) or not 1 <= len(files) <= MAX_FILES:
            raise Refused("invalid files")
        seen, plan, total = set(), [], 0
        for f in files:
            if not isinstance(f, dict) or set(f) - {"path", "content_b64", "delete"}:
                raise Refused("invalid files")
            path = _path(f)
            if path in seen or _workflow_path(path):
                raise Refused("duplicate or workflow path refused")
            seen.add(path)
            if f.get("delete") is True:
                if "content_b64" in f:
                    raise Refused("invalid files")
                plan.append((path, None))
                continue
            content = f.get("content_b64")
            if not isinstance(content, str) or not content or "delete" in f:
                raise Refused("invalid files")
            total += len(content)
            if total > MAX_FILES_B64:
                raise Refused("files too large")
            try:
                raw = base64.b64decode(content, validate=True)
            except (binascii.Error, ValueError):
                raise Refused("invalid content") from None
            if find_secret(raw.decode("utf-8", "replace")):
                raise Refused("credential-shaped content refused")
            plan.append((path, content))
        if find_secret(message):
            raise Refused("credential-shaped content refused")
        default = _obj(self._get(f"/repos/{repo}")).get("default_branch")
        if not isinstance(default, str) or not default or branch == default:
            raise Refused("default branch is not writable")
        ref = _obj(self._get(f"/repos/{repo}/git/ref/heads/{quote(branch, safe='/')}"))
        if _sub(ref, "object").get("sha") != head:
            raise Refused("branch head moved")
        base_tree = _sub(_obj(self._get(f"/repos/{repo}/git/commits/{head}")), "tree").get("sha")
        if not isinstance(base_tree, str) or not _SHA.fullmatch(base_tree):
            raise RelayGitHubError(502, "unexpected upstream shape")
        cache, entries = {}, []
        for path, content in plan:
            if content is None:
                entries.append({"path": path, "mode": "100644", "type": "blob", "sha": None})
                continue
            blob = _obj(
                self.client._call(
                    "POST",
                    f"/repos/{repo}/git/blobs",
                    {"content": content, "encoding": "base64"},
                    bearer=self.bearer,
                )
            )
            blob_sha = blob.get("sha")
            if not isinstance(blob_sha, str) or not _SHA.fullmatch(blob_sha):
                raise RelayGitHubError(502, "unexpected upstream shape")
            existing = self._entry_mode(repo, base_tree, path, cache)
            if existing not in (None, "100644", "100755"):
                # A symlink or submodule is never silently replaced by file content.
                raise Refused("existing entry is not a regular file")
            mode = existing or "100644"
            entries.append({"path": path, "mode": mode, "type": "blob", "sha": blob_sha})
        tree = _obj(
            self.client._call(
                "POST",
                f"/repos/{repo}/git/trees",
                {"base_tree": base_tree, "tree": entries},
                bearer=self.bearer,
            )
        )
        tree_sha = tree.get("sha")
        if not isinstance(tree_sha, str) or not _SHA.fullmatch(tree_sha):
            raise RelayGitHubError(502, "unexpected upstream shape")
        commit = _obj(
            self.client._call(
                "POST",
                f"/repos/{repo}/git/commits",
                {"message": message, "tree": tree_sha, "parents": [head]},
                bearer=self.bearer,
            )
        )
        new_sha = commit.get("sha")
        if not isinstance(new_sha, str) or not _SHA.fullmatch(new_sha):
            raise RelayGitHubError(502, "unexpected upstream shape")
        # Fast-forward only: a branch that moved since the read is refused upstream.
        self.client._call(
            "PATCH",
            f"/repos/{repo}/git/refs/heads/{quote(branch, safe='/')}",
            {"sha": new_sha, "force": False},
            bearer=self.bearer,
        )
        return {"branch": branch, "commit": new_sha, "files": len(plan)}

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
