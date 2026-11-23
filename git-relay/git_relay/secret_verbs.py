"""Secret and organization-variable verbs for the relay.

Values never reach the relay in the clear.  The caller fetches the scope's public key,
seals the value on its own machine (libsodium crypto_box_seal) and sends only the base64
ciphertext and key_id.  Results carry names, visibility, timestamps and counts, never a
value.  Nothing here logs or returns a request value.
"""

import base64
import binascii
import re

from .github_client import RelayGitHubError

_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,99}")
_ENV = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,99}")
_KEY_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
_B64 = re.compile(r"[A-Za-z0-9+/]+={0,2}")
MAX_CIPHERTEXT_B64 = 70000  # 48 KiB secret + 48 byte sealed-box overhead, base64

# scope -> (app permission, path template, org level, supports selected repositories)
SCOPES = {
    "org_actions": ("organization_secrets", "/orgs/{org}/actions/secrets", True),
    "org_dependabot": (
        "organization_dependabot_secrets",
        "/orgs/{org}/dependabot/secrets",
        True,
    ),
    "org_codespaces": (
        "organization_codespaces_secrets",
        "/orgs/{org}/codespaces/secrets",
        True,
    ),
    "repo_actions": ("secrets", "/repos/{repo}/actions/secrets", False),
    "repo_dependabot": ("dependabot_secrets", "/repos/{repo}/dependabot/secrets", False),
    "repo_codespaces": ("codespaces_secrets", "/repos/{repo}/codespaces/secrets", False),
    "env_actions": ("secrets", "/repositories/{rid}/environments/{env}/secrets", False),
}

SECRET_SPECS = {}
for _scope, (_perm, _tpl, _org) in SCOPES.items():
    SECRET_SPECS[f"wf_secrets_{_scope}"] = ({_perm: "read"}, False)
    SECRET_SPECS[f"wf_secret_key_{_scope}"] = ({_perm: "read"}, False)
    SECRET_SPECS[f"wf_secret_put_{_scope}"] = ({_perm: "write"}, True)
    SECRET_SPECS[f"wf_secret_delete_{_scope}"] = ({_perm: "write"}, True)
    if _org:
        SECRET_SPECS[f"wf_secret_repos_{_scope}"] = ({_perm: "read"}, False)
        SECRET_SPECS[f"wf_secret_repos_set_{_scope}"] = ({_perm: "write"}, True)

# Organization Actions variables (the repository-level verbs already exist).
VARIABLE_PERM = "organization_actions_variables"
SECRET_SPECS["wf_org_variables"] = ({VARIABLE_PERM: "read"}, False)
SECRET_SPECS["wf_org_variable_set"] = ({VARIABLE_PERM: "write"}, True)
SECRET_SPECS["wf_org_variable_delete"] = ({VARIABLE_PERM: "write"}, True)
SECRET_SPECS["wf_org_variable_repos"] = ({VARIABLE_PERM: "read"}, False)
SECRET_SPECS["wf_org_variable_repos_set"] = ({VARIABLE_PERM: "write"}, True)

SECRET_WRITES_REAPPLY = tuple(a for a, (_, w) in SECRET_SPECS.items() if w and "put" in a) + tuple(
    a for a, (_, w) in SECRET_SPECS.items() if w and ("delete" in a or "_set" in a)
)


def _refuse(refused, msg):
    raise refused(msg)


def _name(args, refused):
    name = args.get("name")
    if not isinstance(name, str) or not _NAME.fullmatch(name) or name.upper().startswith("GITHUB_"):
        raise refused("invalid secret name")
    return name


def _ids(args, refused, key="selected_repository_ids"):
    ids = args.get(key)
    if (
        not isinstance(ids, list)
        or len(ids) > 1000
        or any(isinstance(i, bool) or not isinstance(i, int) or not 0 < i < 2**53 for i in ids)
    ):
        raise refused(f"invalid {key}")
    return ids


def _ciphertext(args, refused):
    value = args.get("encrypted_value")
    key_id = args.get("key_id")
    if (
        not isinstance(value, str)
        or not 60 <= len(value) <= MAX_CIPHERTEXT_B64
        or not _B64.fullmatch(value)
        or not isinstance(key_id, str)
        or not _KEY_ID.fullmatch(key_id)
    ):
        raise refused("invalid encrypted_value or key_id; send base64 sealed-box ciphertext")
    try:
        raw = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        raise refused("invalid encrypted_value; send base64 sealed-box ciphertext") from None
    if len(raw) < 49:
        raise refused("encrypted_value is too short to be a sealed box")
    return value, key_id


def _receipt_rows(rows, key, want, refused_status=RelayGitHubError):
    if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
        raise refused_status(502, f"{key} page unverifiable")
    out = []
    for row in rows:
        name = row.get("name")
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            raise refused_status(502, f"{key} name unverifiable")
        out.append({k: row.get(k) for k in want if k in row})
    return out


def _make(scope, kind):
    perm, tpl, org = SCOPES[scope]
    env = scope == "env_actions"
    allowed_base = ("repo", "environment") if env else ("repo",)

    def base(self, repo, args, refused, extra=()):
        extra_ok = tuple(extra)
        extras = set(args) - set(allowed_base) - set(extra_ok)
        if extras:
            raise refused("unexpected argument")
        environment = None
        if env:
            environment = args.get("environment")
            if not isinstance(environment, str) or not _ENV.fullmatch(environment):
                raise refused("invalid environment")
        return environment

    def path(self, repo, environment):
        if org:
            return tpl.format(org=repo.split("/")[0])
        if env:
            data = self._get(f"/repos/{repo}")
            rid = data.get("id") if isinstance(data, dict) else None
            if type(rid) is not int or rid <= 0:
                raise RelayGitHubError(502, "repository id unverifiable")
            from urllib.parse import quote

            return tpl.format(rid=rid, env=quote(environment, safe=""))
        return tpl.format(repo=repo)

    def lst(self, repo, args):
        from . import workflow_verbs as wv

        environment = base(self, repo, args, wv.Refused, ("per_page", "page"))
        limit = wv._page(args)
        page = wv._int(args, "page", 1, 1000) if "page" in args else 1
        data = self._get(f"{path(self, repo, environment)}?per_page={limit}&page={page}")
        if not isinstance(data, dict) or type(data.get("total_count")) is not int:
            raise RelayGitHubError(502, "secrets page unverifiable")
        rows = _receipt_rows(
            data.get("secrets"), "secrets", ("name", "created_at", "updated_at", "visibility")
        )
        if len(rows) > limit:
            raise RelayGitHubError(502, "secrets page unverifiable")
        return {"secrets": rows, "total_count": data["total_count"], "page": page}

    def key(self, repo, args):
        from . import workflow_verbs as wv

        environment = base(self, repo, args, wv.Refused)
        data = self._get(f"{path(self, repo, environment)}/public-key")
        key_id, public = (data.get("key_id"), data.get("key")) if isinstance(data, dict) else (None, None)
        if (
            not isinstance(key_id, str)
            or not _KEY_ID.fullmatch(key_id)
            or not isinstance(public, str)
            or not _B64.fullmatch(public)
            or len(public) > 200
        ):
            raise RelayGitHubError(502, "public key unverifiable")
        return {"key_id": key_id, "key": public}

    def put(self, repo, args):
        from . import workflow_verbs as wv

        opt = ("name", "encrypted_value", "key_id")
        if org:
            opt += ("visibility", "selected_repository_ids")
        environment = base(self, repo, args, wv.Refused, opt)
        name = _name(args, wv.Refused)
        value, key_id = _ciphertext(args, wv.Refused)
        body = {"encrypted_value": value, "key_id": key_id}
        if org:
            visibility = args.get("visibility", "private")
            if visibility not in ("all", "private", "selected"):
                raise wv.Refused("invalid visibility")
            body["visibility"] = visibility
            if visibility == "selected":
                body["selected_repository_ids"] = _ids(args, wv.Refused)
            elif "selected_repository_ids" in args:
                raise wv.Refused("selected_repository_ids needs visibility selected")
        self.client._call(
            "PUT", f"{path(self, repo, environment)}/{name}", body, bearer=self.bearer
        )
        return {"completed": True, "name": name}

    def delete(self, repo, args):
        from . import workflow_verbs as wv

        environment = base(self, repo, args, wv.Refused, ("name",))
        name = _name(args, wv.Refused)
        try:
            self.client._call(
                "DELETE",
                f"{path(self, repo, environment)}/{name}",
                None,
                bearer=self.bearer,
                expected_status=204,
            )
        except RelayGitHubError as exc:
            if exc.status != 404:
                raise
            # A hidden or missing scope also answers 404: confirm the repository first.
            self._get(f"/repos/{repo}")
            return {"completed": True, "name": name, "already_absent": True}
        return {"completed": True, "name": name}

    def repos(self, repo, args):
        from . import workflow_verbs as wv

        environment = base(self, repo, args, wv.Refused, ("name", "per_page", "page"))
        name = _name(args, wv.Refused)
        limit = wv._page(args)
        page = wv._int(args, "page", 1, 1000) if "page" in args else 1
        data = self._get(
            f"{path(self, repo, environment)}/{name}/repositories?per_page={limit}&page={page}"
        )
        rows = data.get("repositories") if isinstance(data, dict) else None
        if not isinstance(rows, list) or type(data.get("total_count")) is not int:
            raise RelayGitHubError(502, "secret repositories unverifiable")
        out = []
        for r in rows[:limit]:
            if not isinstance(r, dict) or type(r.get("id")) is not int:
                raise RelayGitHubError(502, "secret repositories unverifiable")
            out.append({"id": r["id"], "full_name": r.get("full_name")})
        return {"repositories": out, "total_count": data["total_count"], "page": page}

    def repos_set(self, repo, args):
        from . import workflow_verbs as wv

        environment = base(self, repo, args, wv.Refused, ("name", "selected_repository_ids"))
        name = _name(args, wv.Refused)
        ids = _ids(args, wv.Refused)
        self.client._call(
            "PUT",
            f"{path(self, repo, environment)}/{name}/repositories",
            {"selected_repository_ids": ids},
            bearer=self.bearer,
            expected_status=204,
        )
        return {"completed": True, "name": name, "count": len(ids)}

    return {"list": lst, "key": key, "put": put, "delete": delete, "repos": repos, "repos_set": repos_set}[kind]


def _org_variable_name(args, refused):
    name = args.get("name")
    if not isinstance(name, str) or not re.fullmatch(r"[A-Z_][A-Z0-9_]{0,99}", name) or name.startswith("GITHUB_"):
        raise refused("invalid variable name")
    return name


class SecretVerbsMixin:
    """Table-generated secret verbs plus organization variables."""

    def wf_org_variables(self, repo, args):
        from . import workflow_verbs as wv

        wv._only(args, ("repo", "per_page", "page"))
        limit = wv._page(args)
        page = wv._int(args, "page", 1, 1000) if "page" in args else 1
        org = repo.split("/")[0]
        data = self._get(f"/orgs/{org}/actions/variables?per_page={limit}&page={page}")
        rows = data.get("variables") if isinstance(data, dict) else None
        if not isinstance(rows, list) or type(data.get("total_count")) is not int or len(rows) > limit:
            raise RelayGitHubError(502, "organization variables unverifiable")
        return {
            "variables": [self._variable_receipt(r) | {"visibility": r.get("visibility")} for r in rows],
            "total_count": data["total_count"],
            "page": page,
        }

    def wf_org_variable_set(self, repo, args):
        from . import workflow_verbs as wv

        wv._only(args, ("repo", "name", "value", "visibility", "selected_repository_ids"))
        name = _org_variable_name(args, wv.Refused)
        value = self._variable_value(args)
        visibility = args.get("visibility", "private")
        if visibility not in ("all", "private", "selected"):
            raise wv.Refused("invalid visibility")
        body = {"value": value, "visibility": visibility}
        if visibility == "selected":
            body["selected_repository_ids"] = _ids(args, wv.Refused)
        elif "selected_repository_ids" in args:
            raise wv.Refused("selected_repository_ids needs visibility selected")
        org = repo.split("/")[0]
        try:
            self.client._call(
                "PATCH", f"/orgs/{org}/actions/variables/{name}", body, bearer=self.bearer,
                expected_status=204,
            )
        except RelayGitHubError as exc:
            if exc.status != 404:
                raise
            self.client._call(
                "POST", f"/orgs/{org}/actions/variables", {"name": name, **body},
                bearer=self.bearer, expected_status=201, allow_empty_object=True,
            )
        return {"completed": True, "name": name}

    def wf_org_variable_delete(self, repo, args):
        from . import workflow_verbs as wv

        wv._only(args, ("repo", "name"))
        name = _org_variable_name(args, wv.Refused)
        org = repo.split("/")[0]
        try:
            self.client._call(
                "DELETE", f"/orgs/{org}/actions/variables/{name}", None,
                bearer=self.bearer, expected_status=204,
            )
        except RelayGitHubError as exc:
            if exc.status != 404:
                raise
            self._get(f"/repos/{repo}")
            return {"completed": True, "name": name, "already_absent": True}
        return {"completed": True, "name": name}

    def wf_org_variable_repos(self, repo, args):
        from . import workflow_verbs as wv

        wv._only(args, ("repo", "name", "per_page", "page"))
        name = _org_variable_name(args, wv.Refused)
        limit = wv._page(args)
        page = wv._int(args, "page", 1, 1000) if "page" in args else 1
        org = repo.split("/")[0]
        data = self._get(f"/orgs/{org}/actions/variables/{name}/repositories?per_page={limit}&page={page}")
        rows = data.get("repositories") if isinstance(data, dict) else None
        if not isinstance(rows, list) or type(data.get("total_count")) is not int:
            raise RelayGitHubError(502, "variable repositories unverifiable")
        return {
            "repositories": [{"id": r["id"], "full_name": r.get("full_name")} for r in rows[:limit] if isinstance(r, dict) and type(r.get("id")) is int],
            "total_count": data["total_count"],
            "page": page,
        }

    def wf_org_variable_repos_set(self, repo, args):
        from . import workflow_verbs as wv

        wv._only(args, ("repo", "name", "selected_repository_ids"))
        name = _org_variable_name(args, wv.Refused)
        ids = _ids(args, wv.Refused)
        org = repo.split("/")[0]
        self.client._call(
            "PUT", f"/orgs/{org}/actions/variables/{name}/repositories",
            {"selected_repository_ids": ids}, bearer=self.bearer, expected_status=204,
        )
        return {"completed": True, "name": name, "count": len(ids)}


for _scope in SCOPES:
    for _verb, _kind in (
        ("wf_secrets_", "list"),
        ("wf_secret_key_", "key"),
        ("wf_secret_put_", "put"),
        ("wf_secret_delete_", "delete"),
        ("wf_secret_repos_", "repos"),
        ("wf_secret_repos_set_", "repos_set"),
    ):
        if _kind in ("repos", "repos_set") and not SCOPES[_scope][2]:
            continue
        setattr(SecretVerbsMixin, _verb + _scope, _make(_scope, _kind))
