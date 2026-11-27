"""Webhook, deploy key and environment write verbs for the relay (wave 2).

Every answer is a small, shape-checked receipt.  A webhook secret travels to GitHub in the
request and is never echoed, logged or returned.  A hook URL that carries credential-shaped
text is refused before anything is sent.  Deploy keys take a PUBLIC ssh key only.
"""

import re
from urllib.parse import quote, urlsplit

from .github_client import RelayGitHubError
from .secret_scan import find_secret

_EVENT = re.compile(r"[a-z][a-z_]{0,59}")
_BRANCH_PATTERN = re.compile(r"[A-Za-z0-9._/*?\[\]!-]{1,255}")
_PUBKEY = re.compile(
    r"(ssh-rsa|ssh-ed25519|ecdsa-sha2-nistp(?:256|384|521)"
    r"|sk-ssh-ed25519@openssh\.com|sk-ecdsa-sha2-nistp256@openssh\.com)"
    r" [A-Za-z0-9+/=]{1,8000}( [\x20-\x7e]{1,100})?"
)
_VAR = re.compile(r"[A-Z_][A-Z0-9_]{0,99}")

REPO_HOOK = "repository_hooks"
ORG_HOOK = "organization_hooks"
ADMIN = "administration"
VARS = "actions_variables"
ENVS = "environments"

HOOK_SPECS = {
    "wf_hook_get": ({REPO_HOOK: "read"}, False),
    "wf_hook_create": ({REPO_HOOK: "write"}, True),
    "wf_hook_update": ({REPO_HOOK: "write"}, True),
    "wf_hook_delete": ({REPO_HOOK: "write"}, True),
    "wf_hook_ping": ({REPO_HOOK: "write"}, True),
    "wf_hook_test": ({REPO_HOOK: "write"}, True),
    "wf_hook_deliveries": ({REPO_HOOK: "read"}, False),
    "wf_hook_redeliver": ({REPO_HOOK: "write"}, True),
    "wf_org_hooks": ({ORG_HOOK: "read"}, False),
    "wf_org_hook_get": ({ORG_HOOK: "read"}, False),
    "wf_org_hook_create": ({ORG_HOOK: "write"}, True),
    "wf_org_hook_update": ({ORG_HOOK: "write"}, True),
    "wf_org_hook_delete": ({ORG_HOOK: "write"}, True),
    "wf_org_hook_ping": ({ORG_HOOK: "write"}, True),
    "wf_deploy_key_get": ({ADMIN: "read"}, False),
    "wf_deploy_key_add": ({ADMIN: "write"}, True),
    "wf_deploy_key_delete": ({ADMIN: "write"}, True),
    "wf_environment_put": ({ADMIN: "write"}, True),
    "wf_environment_delete": ({ADMIN: "write"}, True),
    "wf_env_branch_policies": ({ADMIN: "read"}, False),
    "wf_env_branch_policy_create": ({ADMIN: "write"}, True),
    "wf_env_branch_policy_delete": ({ADMIN: "write"}, True),
    "wf_env_variables": ({VARS: "read", ENVS: "read"}, False),
    "wf_env_variable_set": ({VARS: "write", ENVS: "write"}, True),
    "wf_env_variable_delete": ({VARS: "write", ENVS: "write"}, True),
}
HOOK_READS = tuple(a for a, (_, w) in HOOK_SPECS.items() if not w)
HOOK_WRITES = tuple(a for a, (_, w) in HOOK_SPECS.items() if w)


def _id(args, refused, key="id"):
    value = args.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value < 2**53:
        raise refused(f"invalid {key}")
    return value


def _hook_body(args, refused, create):
    """The validated GitHub hook body.  A secret is allowed in a request and never answered."""
    body, config = {}, {}
    if create or "url" in args:
        url = args.get("url")
        if not isinstance(url, str) or not 8 <= len(url) <= 2000:
            raise refused("invalid url")
        try:
            parts = urlsplit(url)
        except ValueError:
            raise refused("invalid url") from None
        if parts.scheme not in ("https", "http") or not parts.hostname:
            raise refused("invalid url")
        if "secret" in args and parts.scheme != "https":
            raise refused("a hook with a signing secret needs an https url")
        if parts.username or parts.password or "@" in parts.netloc:
            raise refused("the hook url must not carry credentials; use the hook secret field")
        if find_secret(url):
            raise refused("the hook url carries credential-shaped text")
        config["url"] = url
    if create or "content_type" in args:
        ctype = args.get("content_type", "json")
        if ctype not in ("json", "form"):
            raise refused("invalid content_type")
        config["content_type"] = ctype
    if "insecure_ssl" in args:
        if args["insecure_ssl"] not in ("0", "1"):
            raise refused("invalid insecure_ssl")
        config["insecure_ssl"] = args["insecure_ssl"]
    if "secret" in args:
        secret = args["secret"]
        if not isinstance(secret, str) or not 1 <= len(secret) <= 256 or "\x00" in secret:
            raise refused("invalid secret")
        config["secret"] = secret
    if config:
        body["config"] = config
    if "events" in args or create:
        events = args.get("events", ["push"])
        if (
            not isinstance(events, list)
            or not 1 <= len(events) <= 50
            or not all(isinstance(e, str) and (_EVENT.fullmatch(e) or e == "*") for e in events)
        ):
            raise refused("invalid events")
        body["events"] = events
    if "active" in args:
        if type(args["active"]) is not bool:
            raise refused("invalid active")
        body["active"] = args["active"]
    return body


def _hook_receipt(data, org=False):
    config = data.get("config") if isinstance(data, dict) else None
    url = config.get("url") if isinstance(config, dict) else None
    events = data.get("events") if isinstance(data, dict) else None
    if (
        not isinstance(data, dict)
        or type(data.get("id")) is not int
        or data["id"] <= 0
        or not isinstance(data.get("name"), str)
        or len(data["name"]) > 100
        or type(data.get("active")) is not bool
        or not isinstance(events, list)
        or len(events) > 50
        or not all(isinstance(e, str) and len(e) <= 100 for e in events)
        or not isinstance(url, str)
        or len(url) > 2000
    ):
        raise RelayGitHubError(502, "hook receipt unverifiable")
    from .workflow_verbs import _hook_url_carries_secret

    withheld = bool(find_secret(url) or _hook_url_carries_secret(url))
    content_type = config.get("content_type")
    return {
        "id": data["id"],
        "name": data["name"],
        "active": data["active"],
        "events": events,
        "url": "<withheld: credential-shaped text>" if withheld else url,
        "url_withheld": withheld,
        "content_type": content_type if content_type in ("json", "form") else None,
        "has_secret": bool(config.get("secret")),
    }


def _delivery_row(item):
    if not isinstance(item, dict) or type(item.get("id")) is not int or item["id"] <= 0:
        raise RelayGitHubError(502, "delivery metadata unverifiable")
    out = {"id": item["id"]}
    for key, kind in (
        ("guid", str),
        ("delivered_at", str),
        ("event", str),
        ("action", str),
        ("status", str),
        ("status_code", int),
        ("redelivery", bool),
    ):
        value = item.get(key)
        if value is None:
            out[key] = None
        elif type(value) is kind and (kind is not str or len(value) <= 100):
            out[key] = value
        else:
            raise RelayGitHubError(502, "delivery metadata unverifiable")
    return out


def _env_policy_row(item):
    if (
        not isinstance(item, dict)
        or type(item.get("id")) is not int
        or item["id"] <= 0
        or not isinstance(item.get("name"), str)
        or len(item["name"]) > 255
    ):
        raise RelayGitHubError(502, "branch policy unverifiable")
    kind = item.get("type")
    return {
        "id": item["id"],
        "name": item["name"],
        "type": kind if kind in ("branch", "tag") else None,
    }


def _name_arg(wf, args):
    return wf._environment_name(args)


class HookVerbsMixin:
    """Mixed into WorkflowVerbs: self._get, self.client and self.bearer come from it."""

    # ---- repository webhooks -------------------------------------------------
    def wf_hook_get(self, repo, args):
        from . import workflow_verbs as wv

        wv._only(args, ("repo", "id"))
        hook = _id(args, wv.Refused)
        return {"hook": _hook_receipt(self._get(f"/repos/{repo}/hooks/{hook}"))}

    def wf_hook_create(self, repo, args):
        from . import workflow_verbs as wv

        wv._only(
            args, ("repo", "url", "content_type", "insecure_ssl", "secret", "events", "active")
        )
        body = _hook_body(args, wv.Refused, True)
        body["name"] = "web"
        body.setdefault("active", True)
        data = self.client._call("POST", f"/repos/{repo}/hooks", body, bearer=self.bearer)
        return {"hook": _hook_receipt(data)}

    def wf_hook_update(self, repo, args):
        from . import workflow_verbs as wv

        wv._only(
            args,
            ("repo", "id", "url", "content_type", "insecure_ssl", "secret", "events", "active"),
        )
        hook = _id(args, wv.Refused)
        body = _hook_body(args, wv.Refused, False)
        if not body:
            raise wv.Refused("nothing to update")
        data = self.client._call("PATCH", f"/repos/{repo}/hooks/{hook}", body, bearer=self.bearer)
        return {"hook": _hook_receipt(data)}

    def _delete_receipt(self, repo, path, key, value):
        try:
            self.client._call("DELETE", path, None, bearer=self.bearer, expected_status=204)
        except RelayGitHubError as exc:
            if exc.status != 404:
                raise
            self._get(f"/repos/{repo}")  # a hidden scope also answers 404: confirm the repository
            try:
                self._get(path)  # the resource itself must also read as missing
            except RelayGitHubError as probe:
                if probe.status != 404:
                    raise
            else:
                raise RelayGitHubError(
                    502, "delete answered missing but the resource still reads"
                ) from exc
            return {"completed": True, key: value, "already_absent": True}
        return {"completed": True, key: value}

    def wf_hook_delete(self, repo, args):
        from . import workflow_verbs as wv

        wv._only(args, ("repo", "id"))
        hook = _id(args, wv.Refused)
        return self._delete_receipt(repo, f"/repos/{repo}/hooks/{hook}", "id", hook)

    def wf_hook_ping(self, repo, args):
        from . import workflow_verbs as wv

        wv._only(args, ("repo", "id"))
        hook = _id(args, wv.Refused)
        self.client._call(
            "POST",
            f"/repos/{repo}/hooks/{hook}/pings",
            None,
            bearer=self.bearer,
            expected_status=204,
        )
        return {"completed": True, "id": hook}

    def wf_hook_test(self, repo, args):
        from . import workflow_verbs as wv

        wv._only(args, ("repo", "id"))
        hook = _id(args, wv.Refused)
        self.client._call(
            "POST",
            f"/repos/{repo}/hooks/{hook}/tests",
            None,
            bearer=self.bearer,
            expected_status=204,
        )
        return {"completed": True, "id": hook}

    def wf_hook_deliveries(self, repo, args):
        from . import workflow_verbs as wv

        wv._only(args, ("repo", "id", "per_page"))
        hook = _id(args, wv.Refused)
        limit = wv._page(args)
        data = self._get(f"/repos/{repo}/hooks/{hook}/deliveries?per_page={limit}")
        if not isinstance(data, list) or len(data) > limit:
            raise RelayGitHubError(502, "delivery page unverifiable")
        return {"deliveries": [_delivery_row(x) for x in data]}

    def wf_hook_redeliver(self, repo, args):
        from . import workflow_verbs as wv

        wv._only(args, ("repo", "id", "delivery_id"))
        hook = _id(args, wv.Refused)
        delivery = _id(args, wv.Refused, "delivery_id")
        self.client._call(
            "POST",
            f"/repos/{repo}/hooks/{hook}/deliveries/{delivery}/attempts",
            None,
            bearer=self.bearer,
            expected_status=202,
            allow_empty_object=True,
        )
        return {"completed": True, "id": hook, "delivery_id": delivery}

    # ---- organization webhooks ----------------------------------------------
    def wf_org_hooks(self, repo, args):
        from . import workflow_verbs as wv

        wv._only(args, ("repo", "per_page", "page"))
        limit = wv._page(args)
        page = wv._int(args, "page", 1, 1000) if "page" in args else 1
        org = repo.split("/")[0]
        data = self._get(f"/orgs/{org}/hooks?per_page={limit}&page={page}")
        if not isinstance(data, list) or len(data) > limit:
            raise RelayGitHubError(502, "hook page unverifiable")
        return {
            "hooks": [_hook_receipt(x) for x in data],
            "page": page,
            "has_more": len(data) == limit,
        }

    def wf_org_hook_get(self, repo, args):
        from . import workflow_verbs as wv

        wv._only(args, ("repo", "id"))
        hook = _id(args, wv.Refused)
        return {"hook": _hook_receipt(self._get(f"/orgs/{repo.split('/')[0]}/hooks/{hook}"))}

    def wf_org_hook_create(self, repo, args):
        from . import workflow_verbs as wv

        wv._only(
            args, ("repo", "url", "content_type", "insecure_ssl", "secret", "events", "active")
        )
        body = _hook_body(args, wv.Refused, True)
        body["name"] = "web"
        body.setdefault("active", True)
        data = self.client._call(
            "POST",
            f"/orgs/{repo.split('/')[0]}/hooks",
            body,
            bearer=self.bearer,
        )
        return {"hook": _hook_receipt(data)}

    def wf_org_hook_update(self, repo, args):
        from . import workflow_verbs as wv

        wv._only(
            args,
            ("repo", "id", "url", "content_type", "insecure_ssl", "secret", "events", "active"),
        )
        hook = _id(args, wv.Refused)
        body = _hook_body(args, wv.Refused, False)
        if not body:
            raise wv.Refused("nothing to update")
        data = self.client._call(
            "PATCH", f"/orgs/{repo.split('/')[0]}/hooks/{hook}", body, bearer=self.bearer
        )
        return {"hook": _hook_receipt(data)}

    def wf_org_hook_delete(self, repo, args):
        from . import workflow_verbs as wv

        wv._only(args, ("repo", "id"))
        hook = _id(args, wv.Refused)
        return self._delete_receipt(repo, f"/orgs/{repo.split('/')[0]}/hooks/{hook}", "id", hook)

    def wf_org_hook_ping(self, repo, args):
        from . import workflow_verbs as wv

        wv._only(args, ("repo", "id"))
        hook = _id(args, wv.Refused)
        self.client._call(
            "POST",
            f"/orgs/{repo.split('/')[0]}/hooks/{hook}/pings",
            None,
            bearer=self.bearer,
            expected_status=204,
        )
        return {"completed": True, "id": hook}

    # ---- deploy keys ----------------------------------------------------------
    @staticmethod
    def _deploy_key_receipt(item):
        if (
            not isinstance(item, dict)
            or type(item.get("id")) is not int
            or item["id"] <= 0
            or not isinstance(item.get("title"), str)
            or len(item["title"]) > 200
            or not isinstance(item.get("key"), str)
            or not _PUBKEY.fullmatch(item["key"])
            or type(item.get("read_only")) is not bool
        ):
            raise RelayGitHubError(502, "deploy key receipt unverifiable")
        return {
            "id": item["id"],
            "title": item["title"],
            "read_only": item["read_only"],
            "verified": item.get("verified") if type(item.get("verified")) is bool else None,
            "key": item["key"],
        }

    def wf_deploy_key_get(self, repo, args):
        from . import workflow_verbs as wv

        wv._only(args, ("repo", "key_id"))
        key = _id(args, wv.Refused, "key_id")
        return {"deploy_key": self._deploy_key_receipt(self._get(f"/repos/{repo}/keys/{key}"))}

    def wf_deploy_key_add(self, repo, args):
        from . import workflow_verbs as wv

        wv._only(args, ("repo", "title", "key", "read_only"))
        title, key = args.get("title"), args.get("key")
        if (
            not isinstance(title, str)
            or not 1 <= len(title) <= 200
            or any(ord(c) < 32 for c in title)
        ):
            raise wv.Refused("invalid title")
        if not isinstance(key, str) or not _PUBKEY.fullmatch(key.strip()):
            raise wv.Refused("invalid key; send a PUBLIC ssh key")
        read_only = args.get("read_only", True)
        if type(read_only) is not bool:
            raise wv.Refused("invalid read_only")
        data = self.client._call(
            "POST",
            f"/repos/{repo}/keys",
            {"title": title, "key": key.strip(), "read_only": read_only},
            bearer=self.bearer,
        )
        return {"deploy_key": self._deploy_key_receipt(data)}

    def wf_deploy_key_delete(self, repo, args):
        from . import workflow_verbs as wv

        wv._only(args, ("repo", "key_id"))
        key = _id(args, wv.Refused, "key_id")
        return self._delete_receipt(repo, f"/repos/{repo}/keys/{key}", "key_id", key)

    # ---- environments -----------------------------------------------------------
    def wf_environment_put(self, repo, args):
        from . import workflow_verbs as wv

        wv._only(
            args,
            (
                "repo",
                "name",
                "wait_timer",
                "prevent_self_review",
                "reviewers",
                "deployment_branch_policy",
            ),
        )
        name = self._environment_name(args)
        body = {}
        if "wait_timer" in args:
            body["wait_timer"] = wv._int(args, "wait_timer", 0, 43200)
        if "prevent_self_review" in args:
            if type(args["prevent_self_review"]) is not bool:
                raise wv.Refused("invalid prevent_self_review")
            body["prevent_self_review"] = args["prevent_self_review"]
        if "reviewers" in args:
            rows = args["reviewers"]
            if not isinstance(rows, list) or len(rows) > 6:
                raise wv.Refused("invalid reviewers")
            reviewers = []
            for row in rows:
                if (
                    not isinstance(row, dict)
                    or set(row) != {"type", "id"}
                    or row["type"] not in ("User", "Team")
                ):
                    raise wv.Refused("invalid reviewers")
                reviewers.append({"type": row["type"], "id": _id(row, wv.Refused)})
            body["reviewers"] = reviewers
        if "deployment_branch_policy" in args:
            policy = args["deployment_branch_policy"]
            if policy is None:
                body["deployment_branch_policy"] = None
            elif (
                isinstance(policy, dict)
                and set(policy) == {"protected_branches", "custom_branch_policies"}
                and all(type(v) is bool for v in policy.values())
                and policy["protected_branches"] != policy["custom_branch_policies"]
            ):
                body["deployment_branch_policy"] = policy
            else:
                raise wv.Refused("invalid deployment_branch_policy")
        data = self.client._call(
            "PUT", f"/repos/{repo}/environments/{quote(name, safe='')}", body, bearer=self.bearer
        )
        return {"environment": self._environment_summary(data, name)}

    def wf_environment_delete(self, repo, args):
        from . import workflow_verbs as wv

        wv._only(args, ("repo", "name"))
        name = self._environment_name(args)
        return self._delete_receipt(
            repo, f"/repos/{repo}/environments/{quote(name, safe='')}", "name", name
        )

    def wf_env_branch_policies(self, repo, args):
        from . import workflow_verbs as wv

        wv._only(args, ("repo", "name", "per_page", "page"))
        name = self._environment_name(args)
        limit = wv._page(args)
        page = wv._int(args, "page", 1, 1000) if "page" in args else 1
        data = self._get(
            f"/repos/{repo}/environments/{quote(name, safe='')}/deployment-branch-policies?per_page={limit}&page={page}"
        )
        rows = data.get("branch_policies") if isinstance(data, dict) else None
        if (
            not isinstance(rows, list)
            or len(rows) > limit
            or type(data.get("total_count")) is not int
        ):
            raise RelayGitHubError(502, "branch policies unverifiable")
        return {
            "policies": [_env_policy_row(x) for x in rows],
            "total_count": data["total_count"],
            "page": page,
        }

    def wf_env_branch_policy_create(self, repo, args):
        from . import workflow_verbs as wv

        wv._only(args, ("repo", "name", "policy_name", "type"))
        name = self._environment_name(args)
        pattern = args.get("policy_name")
        kind = args.get("type", "branch")
        if (
            not isinstance(pattern, str)
            or not _BRANCH_PATTERN.fullmatch(pattern)
            or kind not in ("branch", "tag")
        ):
            raise wv.Refused("invalid branch policy")
        data = self.client._call(
            "POST",
            f"/repos/{repo}/environments/{quote(name, safe='')}/deployment-branch-policies",
            {"name": pattern, "type": kind},
            bearer=self.bearer,
        )
        return {"policy": _env_policy_row(data)}

    def wf_env_branch_policy_delete(self, repo, args):
        from . import workflow_verbs as wv

        wv._only(args, ("repo", "name", "policy_id"))
        name = self._environment_name(args)
        pid = _id(args, wv.Refused, "policy_id")
        return self._delete_receipt(
            repo,
            f"/repos/{repo}/environments/{quote(name, safe='')}/deployment-branch-policies/{pid}",
            "policy_id",
            pid,
        )

    def _env_var_name(self, args, refused):
        name = args.get("variable")
        if not isinstance(name, str) or not _VAR.fullmatch(name) or name.startswith("GITHUB_"):
            raise refused("invalid variable name")
        return name

    def wf_env_variables(self, repo, args):
        from . import workflow_verbs as wv

        wv._only(args, ("repo", "name", "per_page", "page"))
        name = self._environment_name(args)
        limit = wv._page(args)
        page = wv._int(args, "page", 1, 1000) if "page" in args else 1
        data = self._get(
            f"/repos/{repo}/environments/{quote(name, safe='')}/variables?per_page={limit}&page={page}"
        )
        rows = data.get("variables") if isinstance(data, dict) else None
        if (
            not isinstance(rows, list)
            or len(rows) > limit
            or type(data.get("total_count")) is not int
        ):
            raise RelayGitHubError(502, "environment variables unverifiable")
        out = []
        for row in rows:
            if (
                not isinstance(row, dict)
                or not isinstance(row.get("name"), str)
                or not _VAR.fullmatch(row["name"])
            ):
                raise RelayGitHubError(502, "environment variables unverifiable")
            value = row.get("value")
            if not isinstance(value, str) or len(value) > 8192:
                raise RelayGitHubError(502, "environment variables unverifiable")
            out.append(
                {
                    "name": row["name"],
                    "value": value,
                    "created_at": row.get("created_at")
                    if isinstance(row.get("created_at"), str)
                    else None,
                    "updated_at": row.get("updated_at")
                    if isinstance(row.get("updated_at"), str)
                    else None,
                }
            )
        return {"variables": out, "total_count": data["total_count"], "page": page}

    def wf_env_variable_set(self, repo, args):
        from . import workflow_verbs as wv

        wv._only(args, ("repo", "name", "variable", "value"))
        name = self._environment_name(args)
        var = self._env_var_name(args, wv.Refused)
        value = self._variable_value(args)
        base = f"/repos/{repo}/environments/{quote(name, safe='')}/variables"
        try:
            self.client._call(
                "PATCH",
                f"{base}/{var}",
                {"name": var, "value": value},
                bearer=self.bearer,
                expected_status=204,
            )
        except RelayGitHubError as exc:
            if exc.status != 404:
                raise
            try:
                self.client._call(
                    "POST",
                    base,
                    {"name": var, "value": value},
                    bearer=self.bearer,
                    expected_status=201,
                    allow_empty_object=True,
                )
            except RelayGitHubError as race:
                if race.status != 409:
                    raise
                self.client._call(
                    "PATCH",
                    f"{base}/{var}",
                    {"name": var, "value": value},
                    bearer=self.bearer,
                    expected_status=204,
                )
        return {"completed": True, "name": name, "variable": var}

    def wf_env_variable_delete(self, repo, args):
        from . import workflow_verbs as wv

        wv._only(args, ("repo", "name", "variable"))
        name = self._environment_name(args)
        var = self._env_var_name(args, wv.Refused)
        out = self._delete_receipt(
            repo,
            f"/repos/{repo}/environments/{quote(name, safe='')}/variables/{var}",
            "variable",
            var,
        )
        out["name"] = name
        return out
