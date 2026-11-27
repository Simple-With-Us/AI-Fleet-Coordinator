"""Repository settings verbs for the relay: read settings, update settings, set topics.

Only the fields a caller names are sent.  Every sent field is checked in GitHub's answer
before the verb reports success.  Strings are bounded and secret-scanned before any call.
Rename, privacy, archive and delete are not part of these verbs.
"""

import re

from .github_client import RelayGitHubError
from .secret_scan import find_secret

ADMIN = "administration"

REPO_ADMIN_SPECS = {
    "wf_repo_settings": ({ADMIN: "read"}, False),
    "wf_repo_update": ({ADMIN: "write"}, True),
    "wf_repo_topics_set": ({ADMIN: "write"}, True),
}
REPO_ADMIN_READS = tuple(a for a, (_, w) in REPO_ADMIN_SPECS.items() if not w)

_TOPIC = re.compile(r"[a-z0-9][a-z0-9-]{0,49}")
_BOOLS = (
    "has_issues",
    "has_wiki",
    "has_projects",
    "has_discussions",
    "allow_squash_merge",
    "allow_merge_commit",
    "allow_rebase_merge",
    "allow_auto_merge",
    "delete_branch_on_merge",
    "allow_update_branch",
    "web_commit_signoff_required",
)
_ENUMS = {
    "squash_merge_commit_title": ("PR_TITLE", "COMMIT_OR_PR_TITLE"),
    "squash_merge_commit_message": ("PR_BODY", "COMMIT_MESSAGES", "BLANK"),
    "merge_commit_title": ("PR_TITLE", "MERGE_MESSAGE"),
    "merge_commit_message": ("PR_BODY", "PR_TITLE", "BLANK"),
}
_SECURITY = ("secret_scanning", "secret_scanning_push_protection", "dependabot_security_updates")
_TEXT_LIMIT = 1024


def _text(args, key, refused):
    value = args[key]
    if not isinstance(value, str) or len(value.encode("utf-8")) > _TEXT_LIMIT or "\x00" in value:
        raise refused(f"invalid {key}")
    if find_secret(value):
        raise refused(f"{key} carries credential-shaped text")
    return value


def _homepage(value, refused):
    if value == "":
        return ""
    if not re.fullmatch(r"https?://[^\s/@]+(/[^\s]*)?", value):
        raise refused("invalid homepage: use an http(s) URL without credentials")
    return value


def _security_body(args, refused):
    sec = args["security_and_analysis"]
    if not isinstance(sec, dict) or not sec or set(sec) - set(_SECURITY):
        raise refused("invalid security_and_analysis")
    body = {}
    for key, value in sec.items():
        if value not in ("enabled", "disabled"):
            raise refused(f"invalid security_and_analysis.{key}")
        body[key] = {"status": value}
    return body


def _settings_view(data):
    if not isinstance(data, dict) or type(data.get("id")) is not int:
        raise RelayGitHubError(502, "repository settings unverifiable")
    out = {"full_name": data.get("full_name")}
    for key in ("description", "homepage", "default_branch", "visibility"):
        v = data.get(key)
        out[key] = v if isinstance(v, str) else None
    for key in ("archived", "private") + _BOOLS:
        v = data.get(key)
        out[key] = v if type(v) is bool else None
    for key in _ENUMS:
        v = data.get(key)
        out[key] = v if isinstance(v, str) else None
    topics = data.get("topics")
    out["topics"] = [t for t in topics if isinstance(t, str)] if isinstance(topics, list) else None
    sec = data.get("security_and_analysis")
    out["security_and_analysis"] = (
        {
            k: v.get("status")
            for k, v in sec.items()
            if k in _SECURITY and isinstance(v, dict) and isinstance(v.get("status"), str)
        }
        if isinstance(sec, dict)
        else None
    )
    return out


class RepoAdminVerbsMixin:
    """Mixed into WorkflowVerbs: self._get, self.client and self.bearer come from it."""

    def wf_repo_settings(self, repo, args):
        from . import workflow_verbs as wv

        wv._only(args, ("repo",))
        return {"settings": _settings_view(self._get(f"/repos/{repo}"))}

    def wf_repo_update(self, repo, args):
        from . import workflow_verbs as wv

        wv._only(
            args,
            (
                "repo",
                "description",
                "homepage",
                "security_and_analysis",
                "has_issues",
                "has_wiki",
                "has_projects",
                "has_discussions",
                "allow_squash_merge",
                "allow_merge_commit",
                "allow_rebase_merge",
                "allow_auto_merge",
                "delete_branch_on_merge",
                "allow_update_branch",
                "web_commit_signoff_required",
                "squash_merge_commit_title",
                "squash_merge_commit_message",
                "merge_commit_title",
                "merge_commit_message",
            ),
        )
        body = {}
        if "description" in args:
            body["description"] = _text(args, "description", wv.Refused)
        if "homepage" in args:
            body["homepage"] = _homepage(_text(args, "homepage", wv.Refused), wv.Refused)
        for key in _BOOLS:
            if key in args:
                if type(args[key]) is not bool:
                    raise wv.Refused(f"invalid {key}")
                body[key] = args[key]
        for key, choices in _ENUMS.items():
            if key in args:
                if args[key] not in choices:
                    raise wv.Refused(f"invalid {key}")
                body[key] = args[key]
        if "security_and_analysis" in args:
            body["security_and_analysis"] = _security_body(args, wv.Refused)
        if not body:
            raise wv.Refused("nothing to update")
        data = self.client._call("PATCH", f"/repos/{repo}", body, bearer=self.bearer)
        view = _settings_view(data)
        for key, want in body.items():
            if key == "security_and_analysis":
                got = view["security_and_analysis"] or {}
                for sk, sv in want.items():
                    if got.get(sk) != sv["status"]:
                        raise RelayGitHubError(502, f"update unverified: {sk}")
            elif key in ("description", "homepage"):
                if (view[key] or "") != want:
                    raise RelayGitHubError(502, f"update unverified: {key}")
            elif view[key] != want:
                raise RelayGitHubError(502, f"update unverified: {key}")
        return {"settings": view}

    def wf_repo_topics_set(self, repo, args):
        from . import workflow_verbs as wv

        wv._only(args, ("repo", "topics"))
        topics = args.get("topics")
        if (
            not isinstance(topics, list)
            or len(topics) > 20
            or len(set(topics)) != len(topics)
            or not all(isinstance(t, str) and _TOPIC.fullmatch(t) for t in topics)
        ):
            raise wv.Refused("invalid topics: up to 20 unique lowercase names")
        data = self.client._call(
            "PUT", f"/repos/{repo}/topics", {"names": topics}, bearer=self.bearer
        )
        names = data.get("names") if isinstance(data, dict) else None
        if not isinstance(names, list) or sorted(names) != sorted(topics):
            raise RelayGitHubError(502, "topics unverifiable")
        return {"topics": names}
