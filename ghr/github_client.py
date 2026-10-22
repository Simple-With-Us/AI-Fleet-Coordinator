"""Thin GitHub REST client for the PR relay.

Holds the scoped token; seats never see it.  Responses are bounded (1 MiB,
32 KiB error previews) and every call carries a timeout.  Errors surface as
RelayGitHubError with the HTTP status so the server can classify them without
leaking response bodies upstream.
"""

from __future__ import annotations

import http.client
import json
import urllib.error
import urllib.request

MAX_BODY = 1024 * 1024
MAX_ERROR_PREVIEW = 512
TIMEOUT_SECONDS = 30
MAX_DISPATCH_INPUTS = 32
MAX_DISPATCH_INPUT_VALUE = 1024


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A 3xx must never be followed:  redirects can carry the bearer to another origin."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class RelayGitHubError(Exception):
    """A GitHub call failed; carries the status code, never the token."""

    def __init__(self, status: int, message: str):
        super().__init__(f"github {status}: {message}")
        self.status = status
        self.message = message


class GitHubClient:
    def __init__(self, token: str, base_url: str = "https://api.github.com", opener=None):
        if not token:
            raise ValueError("GitHubClient requires a token")
        self._token = token
        self._base = base_url.rstrip("/")
        self._opener = opener or urllib.request.build_opener(_NoRedirect()).open

    def _call(self, method: str, path: str, payload: dict | None = None, bearer=None) -> dict:
        if bearer is not None and not bearer:
            # Fail closed:  an empty task credential must never fall back to the
            # owner token and run a privileged write under the wrong identity.
            raise ValueError("empty task credential; refusing to use the owner token")
        url = f"{self._base}{path}"
        req = urllib.request.Request(
            url,
            method=method,
            data=None if payload is None else json.dumps(payload).encode(),
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {bearer or self._token}",
                "Content-Type": "application/json",
                "User-Agent": "swu-pr-relay",
            },
        )
        try:
            with self._opener(req, timeout=TIMEOUT_SECONDS) as resp:
                raw = resp.read(MAX_BODY + 1)
        except urllib.error.HTTPError as exc:
            preview = exc.read(MAX_ERROR_PREVIEW).decode("utf-8", "replace")
            try:
                message = json.loads(preview).get("message") or preview
            except json.JSONDecodeError:
                message = preview
            raise RelayGitHubError(exc.code, message[:MAX_ERROR_PREVIEW]) from exc
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

    def edit_pr(self, repo: str, number: int, title: str | None, body: str | None) -> dict:
        self._check_repo(repo)
        payload: dict[str, str] = {}
        if title is not None:
            payload["title"] = title
        if body is not None:
            payload["body"] = body
        if not payload:
            raise ValueError("edit_pr needs a title or a body")
        data = self._call("PATCH", f"/repos/{repo}/pulls/{number}", payload)
        return {"number": data.get("number"), "url": data.get("html_url")}

    def reply_thread(self, repo: str, number: int, comment_id: int, body: str) -> dict:
        self._check_repo(repo)
        data = self._call(
            "POST",
            f"/repos/{repo}/pulls/{number}/comments",
            {"body": body, "in_reply_to": comment_id},
        )
        return {"id": data.get("id"), "url": data.get("html_url")}

    def comment_pr(self, repo: str, number: int, body: str) -> dict:
        self._check_repo(repo)
        data = self._call("POST", f"/repos/{repo}/issues/{number}/comments", {"body": body})
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
            raise RelayGitHubError(0, f"graphql resolveReviewThread failed{detail}")
        payload_data = data.get("data")
        thread = (
            (payload_data.get("resolveReviewThread") or {}).get("thread")
            if isinstance(payload_data, dict)
            else None
        )
        if not isinstance(thread, dict):
            raise RelayGitHubError(0, "graphql reply was malformed")
        return {"thread": thread_id, "resolved": bool(thread.get("isResolved"))}
