"""Thin GitHub REST client for the PR relay.

Holds the scoped token; seats never see it.  Responses are bounded (1 MiB,
32 KiB error previews) and every call carries a timeout.  Errors surface as
RelayGitHubError with the HTTP status so the server can classify them without
leaking response bodies upstream.
"""

from __future__ import annotations

import http.client
import json
import re
import urllib.error
import urllib.request

from .secret_scan import mask_credential

MAX_BODY = 1024 * 1024
MAX_ERROR_PREVIEW = 512
TIMEOUT_SECONDS = 30
MAX_DISPATCH_INPUTS = 32
MAX_DISPATCH_INPUT_VALUE = 1024


# Never retain arbitrary upstream text.  Request IDs are GitHub's bounded
# hexadecimal correlation IDs; permission metadata is parsed, not copied.
_DIAGNOSTIC_PERMISSIONS = frozenset(
    {
        "actions",
        "administration",
        "checks",
        "contents",
        "issues",
        "metadata",
        "pull_requests",
        "statuses",
        "workflows",
    }
)
_ERROR_CATEGORIES = {
    "Resource not accessible by integration": "resource_not_accessible",
    "Resource not accessible by personal access token": "resource_not_accessible",
    "API rate limit exceeded": "rate_limit",
    "Bad credentials": "bad_credentials",
    "Not Found": "not_found",
}


def safe_error_diagnostics(headers=None, message=None):
    """Capture only bounded, validated metadata and fixed categories.

    Unknown/hostile bodies cannot become a diagnostic string.  This does not
    change retry classification, request permissions, or request behavior.
    """
    category = "unknown"
    if isinstance(message, str) and len(message) <= MAX_ERROR_PREVIEW:
        category = _ERROR_CATEGORIES.get(message, "unknown")
        if re.fullmatch(
            r"API rate limit exceeded for (?:user|installation) ID [0-9]{1,20}\.", message
        ):
            category = "rate_limit"
    result = {"category": category}
    request_id = headers.get("X-GitHub-Request-Id") if headers is not None else None
    if isinstance(request_id, str) and re.fullmatch(
        r"[0-9A-Fa-f]{4,8}(?::[0-9A-Fa-f]{4,8}){3}", request_id
    ):
        result["request_id"] = request_id
    raw = headers.get("X-Accepted-GitHub-Permissions") if headers is not None else None
    if isinstance(raw, str) and 0 < len(raw) <= 512 and all(32 <= ord(char) < 127 for char in raw):
        groups = raw.split(";")
        sets = []
        # Reject the whole header on an anomaly.  Partial alternatives could
        # misrepresent upstream requirements; unknown metadata is safer.
        if len(groups) <= 4:
            for group in groups:
                permissions = {}
                for item in group.split(","):
                    pair = item.strip().split("=")
                    if (
                        len(pair) != 2
                        or pair[0] not in _DIAGNOSTIC_PERMISSIONS
                        or pair[1] not in ("read", "write")
                        or pair[0] in permissions
                    ):
                        break
                    permissions[pair[0]] = pair[1]
                else:
                    if permissions:
                        sets.append(permissions)
                        continue
                break
            else:
                if len(sets) == 1:
                    result["accepted_permissions"] = sets[0]
                elif sets:
                    result["accepted_permission_sets"] = sets
    return result


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A 3xx must never be followed:  redirects can carry the bearer to another origin."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class RelayGitHubError(Exception):
    """A GitHub call failed; carries the status code, never the token.

    code preserves a machine-readable upstream error code (a REST ``code`` or a
    GraphQL error ``type``) so the server can classify definite auth failures
    such as ``invalid_grant`` without parsing prose.  retry_after carries the
    integer Retry-After seconds a 429/403 response asked for, when present.
    """

    def __init__(
        self,
        status: int,
        message: str,
        code: str | None = None,
        retry_after=None,
        credential_preview: str | None = None,
        diagnostics: dict | None = None,
    ):
        super().__init__(f"github {status}: {message}")
        self.status = status
        self.message = message
        self.code = code
        self.retry_after = retry_after
        # First-eight/last-four of the bearer GitHub refused, when the failure
        # is about the credential itself;  identifies the dead key without
        # exposing it.
        self.credential_preview = credential_preview
        # Revalidate at the public exception boundary, including stub clients.
        source = diagnostics if isinstance(diagnostics, dict) else {}
        hdrs = {"X-GitHub-Request-Id": source.get("request_id")}
        perms = source.get("accepted_permissions")
        alternatives = source.get("accepted_permission_sets")
        groups = [perms] if perms is not None and alternatives is None else alternatives
        if isinstance(groups, list) and 1 <= len(groups) <= 4:
            if all(
                isinstance(group, dict)
                and 1 <= len(group) <= len(_DIAGNOSTIC_PERMISSIONS)
                and all(isinstance(k, str) and isinstance(v, str) for k, v in group.items())
                for group in groups
            ):
                hdrs["X-Accepted-GitHub-Permissions"] = ";".join(
                    ",".join(k + "=" + v for k, v in group.items()) for group in groups
                )
        self.diagnostics = safe_error_diagnostics(hdrs)
        if isinstance(source.get("category"), str) and source["category"] in set(
            _ERROR_CATEGORIES.values()
        ) | {"unknown"}:
            self.diagnostics["category"] = source["category"]


def _retry_after_seconds(headers) -> int | None:
    raw = headers.get("Retry-After") if headers is not None else None
    # int() accepts "+5" and unicode digits isdigit() would pass;  accept only
    # a plain ASCII decimal so a malformed header can never crash the handler.
    if raw and all("0" <= ch <= "9" for ch in raw):
        try:
            value = int(raw)
        except ValueError:
            return None
        return value if value >= 0 else None
    return None


class GitHubClient:
    def __init__(self, token: str, base_url: str = "https://api.github.com", opener=None):
        if not token:
            raise ValueError("GitHubClient requires a token")
        self._token = token
        self._base = base_url.rstrip("/")
        self._opener = opener or urllib.request.build_opener(_NoRedirect()).open

    def _call(
        self, method: str, path: str, payload: dict | None = None, bearer=None, expected_status=None
    ) -> dict:
        if bearer is not None and not bearer:
            # Fail closed:  an empty task credential must never fall back to the
            # owner token and run a privileged write under the wrong identity.
            raise ValueError("empty task credential; refusing to use the owner token")
        url = f"{self._base}{path}"
        req = urllib.request.Request(
            url,
            method=method,
            data=None
            if payload is None
            else payload
            if isinstance(payload, bytes)
            else json.dumps(payload).encode(),
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {bearer or self._token}",
                "Content-Type": "application/octet-stream"
                if isinstance(payload, bytes)
                else "application/json",
                "User-Agent": "swu-git-relay",
            },
        )
        try:
            with self._opener(req, timeout=TIMEOUT_SECONDS) as resp:
                raw = resp.read(MAX_BODY + 1)
                if expected_status is not None and (
                    resp.status != expected_status or (expected_status == 204 and raw)
                ):
                    raise RelayGitHubError(502, "unexpected upstream completion status or body")
        except urllib.error.HTTPError as exc:
            preview = exc.read(MAX_ERROR_PREVIEW).decode("utf-8", "replace")
            code = None
            try:
                body = json.loads(preview)
            except json.JSONDecodeError:
                body = None
            if isinstance(body, dict):
                message = body.get("message") or preview
                if isinstance(body.get("code"), str):
                    code = body["code"]
                else:
                    errs = body.get("errors")
                    if isinstance(errs, list) and errs and isinstance(errs[0], dict):
                        first = errs[0].get("code") or errs[0].get("type")
                        if isinstance(first, str):
                            code = first
            else:
                message = preview
            raise RelayGitHubError(
                exc.code,
                message[:MAX_ERROR_PREVIEW],
                code=code,
                retry_after=_retry_after_seconds(exc.headers),
                diagnostics=safe_error_diagnostics(exc.headers, message),
                credential_preview=(
                    mask_credential(bearer) if bearer and exc.code in (401, 403) else None
                ),
            ) from exc
        except urllib.error.URLError as exc:
            raise RelayGitHubError(0, f"connection failed: {exc.reason}") from exc
        except (OSError, http.client.HTTPException) as exc:
            raise RelayGitHubError(0, f"connection failed: {exc}") from exc
        if len(raw) > MAX_BODY:
            raise RelayGitHubError(0, "github response exceeded 1 MiB bound")
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RelayGitHubError(0, "github response was not valid JSON") from exc

    def upload_release_asset(self, repo, release_id, name, content, bearer):
        """Fixed GitHub upload host, own bearer, no redirects or arbitrary URL."""
        from urllib.parse import quote

        self._check_repo(repo)
        if (
            not bearer
            or isinstance(release_id, bool)
            or not isinstance(release_id, int)
            or release_id < 1
            or not isinstance(name, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}", name)
            or name.endswith(".")
            or not isinstance(content, bytes)
            or not 1 <= len(content) <= 32768
        ):
            raise ValueError("bounded own-App asset upload required")
        client = GitHubClient(bearer, "https://uploads.github.com", opener=self._opener)
        return client._call(
            "POST",
            f"/repos/{repo}/releases/{release_id}/assets?name={quote(name, safe='')}",
            content,
            bearer=bearer,
        )

    @staticmethod
    def _check_repo(repo: str) -> None:
        parts = repo.split("/")
        if len(parts) != 2 or not all(parts) or ".." in repo:
            raise ValueError(f"repo must look like owner/name, got {repo!r}")

    def open_pr(self, repo: str, base: str, head: str, title: str, body: str) -> dict:
        self._check_repo(repo)
        data = self._call(
            "POST",
            f"/repos/{repo}/pulls",
            {"title": title, "head": head, "base": base, "body": body},
        )
        return {"number": data.get("number"), "url": data.get("html_url")}

    def edit_pr(
        self, repo: str, number: int, title: str | None, body: str | None, bearer=None
    ) -> dict:
        self._check_repo(repo)
        payload: dict[str, str] = {}
        if title is not None:
            payload["title"] = title
        if body is not None:
            payload["body"] = body
        if not payload:
            raise ValueError("edit_pr needs a title or a body")
        data = self._call("PATCH", f"/repos/{repo}/pulls/{number}", payload, bearer=bearer)
        return {"number": data.get("number"), "url": data.get("html_url")}

    def reply_thread(self, repo: str, number: int, comment_id: int, body: str, bearer=None) -> dict:
        self._check_repo(repo)
        data = self._call(
            "POST",
            f"/repos/{repo}/pulls/{number}/comments",
            {"body": body, "in_reply_to": comment_id},
            bearer=bearer,
        )
        return {"id": data.get("id"), "url": data.get("html_url")}

    def comment_pr(self, repo: str, number: int, body: str, bearer=None) -> dict:
        self._check_repo(repo)
        data = self._call(
            "POST", f"/repos/{repo}/issues/{number}/comments", {"body": body}, bearer=bearer
        )
        return {"id": data.get("id"), "url": data.get("html_url")}

    def merge_pr(self, repo: str, number: int, method: str, bearer: str) -> dict:
        self._check_repo(repo)
        if method not in ("squash", "merge", "rebase"):
            raise ValueError("method must be squash, merge or rebase")
        data = self._call(
            "PUT", f"/repos/{repo}/pulls/{number}/merge", {"merge_method": method}, bearer=bearer
        )
        return {"merged": data.get("merged"), "sha": data.get("sha")}

    def workflow_dispatch(
        self, repo: str, workflow: str, ref: str, inputs: dict, bearer: str
    ) -> dict:
        self._check_repo(repo)
        if not isinstance(workflow, str) or "/" in workflow or ".." in workflow:
            raise ValueError("workflow must be a file name or numeric id")
        if not isinstance(ref, str) or not ref:
            raise ValueError("ref is required")
        if inputs is not None:
            if not isinstance(inputs, dict) or len(inputs) > MAX_DISPATCH_INPUTS:
                raise ValueError(f"inputs must be an object of at most {MAX_DISPATCH_INPUTS} keys")
            for key, value in inputs.items():
                if not isinstance(key, str) or value is None or isinstance(value, (dict, list)):
                    raise ValueError("inputs must be a flat object of scalar values")
                if isinstance(value, str) and len(value) > MAX_DISPATCH_INPUT_VALUE:
                    raise ValueError(
                        f"inputs string values are capped at {MAX_DISPATCH_INPUT_VALUE} characters"
                    )
        self._call(
            "POST",
            f"/repos/{repo}/actions/workflows/{workflow}/dispatches",
            {"ref": ref, "inputs": inputs or {}},
            bearer=bearer,
        )
        return {"dispatched": True, "workflow": workflow, "ref": ref}

    def resolve_thread(self, thread_id: str, bearer: str) -> dict:
        if not isinstance(thread_id, str) or not thread_id.startswith("PRRT_"):
            raise ValueError("thread_id must be a PRRT_ review-thread id")
        query = (
            "mutation($id: ID!) { resolveReviewThread(input: {threadId: $id})"
            " { thread { isResolved } } }"
        )
        data = self._call(
            "POST",
            "/graphql",
            {"query": query, "variables": {"id": thread_id}},
            bearer=bearer,
        )
        if not isinstance(data, dict):
            raise RelayGitHubError(0, "graphql reply was not an object")
        errors = data.get("errors")
        if errors:
            first = errors[0] if isinstance(errors, list) and errors else {}
            kind = first.get("type") if isinstance(first, dict) else None
            detail = f" ({kind})" if isinstance(kind, str) and kind else ""
            # Only a structured authorization denial at this sole mutation,
            # with no mutation result, proves that no write took place.  Partial
            # results, mixed errors and unscoped errors remain unknown.
            payload = data.get("data")
            no_result = payload is None or (
                isinstance(payload, dict) and payload.get("resolveReviewThread") is None
            )
            definite_refusal = (
                isinstance(errors, list)
                and no_result
                and all(
                    isinstance(error, dict)
                    and error.get("type") == "FORBIDDEN"
                    and error.get("path") == ["resolveReviewThread"]
                    for error in errors
                )
            )
            raise RelayGitHubError(
                403 if definite_refusal else 0,
                f"graphql resolveReviewThread failed{detail}",
                code=kind if isinstance(kind, str) and kind else None,
                credential_preview=mask_credential(bearer),
            )
        payload_data = data.get("data")
        thread = (
            (payload_data.get("resolveReviewThread") or {}).get("thread")
            if isinstance(payload_data, dict)
            else None
        )
        if not isinstance(thread, dict):
            raise RelayGitHubError(0, "graphql reply was malformed")
        return {"thread": thread_id, "resolved": bool(thread.get("isResolved"))}
