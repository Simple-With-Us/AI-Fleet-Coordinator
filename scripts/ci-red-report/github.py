"""GitHub REST client for the CI red report.  Stdlib only; injectable for tests."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator, Mapping, MutableMapping
from typing import Any

API_ROOT = "https://api.github.com"
DEFAULT_API_VERSION = "2022-11-28"
USER_AGENT = "AI-Fleet-Coordinator-ci-red-report/1"

SleepFn = Callable[[float], None]


class GitHubError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class GitHubClient:
    """Minimal read-only GitHub REST helper with pagination and rate-limit backoff."""

    def __init__(
        self,
        token: str,
        *,
        sleep: SleepFn = time.sleep,
        max_retries: int = 5,
        request_fn: Callable[..., Any] | None = None,
    ) -> None:
        if not token.strip():
            raise ValueError("GITHUB_TOKEN is required")
        self._token = token.strip()
        self._sleep = sleep
        self._max_retries = max(1, max_retries)
        self._request_fn = request_fn or self._default_request
        self._secondary_backoff = 0.0

    def get_json(self, path: str, *, params: Mapping[str, str] | None = None) -> Any:
        """GET one path under api.github.com (path must start with /)."""
        if not path.startswith("/"):
            raise ValueError("path must start with /")
        query = ""
        if params:
            query = "?" + urllib.parse.urlencode(params)
        url = API_ROOT + path + query
        return self._request_json("GET", url)

    def paginate(
        self,
        path: str,
        *,
        params: MutableMapping[str, str] | None = None,
        per_page: int = 100,
    ) -> Iterator[Any]:
        """Yield each item across Link-paginated list responses."""
        base_params: dict[str, str] = dict(params or {})
        base_params["per_page"] = str(per_page)
        page = 1
        while True:
            base_params["page"] = str(page)
            data, headers = self._request_json_with_headers("GET", API_ROOT + path, base_params)
            if not isinstance(data, list):
                raise GitHubError("expected a JSON array from " + path)
            if not data:
                return
            yield from data
            link = headers.get("Link", "")
            if 'rel="next"' not in link:
                return
            page += 1

    def _default_request(
        self, method: str, url: str, headers: dict[str, str]
    ) -> tuple[bytes, dict[str, str]]:
        req = urllib.request.Request(url, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=60) as response:
                body = response.read(2_000_001)
                if len(body) > 2_000_000:
                    raise GitHubError("response too large")
                hdrs = {k: v for k, v in response.headers.items()}
                return body, hdrs
        except urllib.error.HTTPError as exc:
            retry_after = exc.headers.get("Retry-After") if exc.headers else None
            err = GitHubError(
                f"GitHub HTTP {exc.code} for {method} {url}",
                status=exc.code,
            )
            if retry_after is not None:
                try:
                    err.retry_after = float(retry_after)  # type: ignore[attr-defined]
                except ValueError:
                    pass
            raise err from None

    def _request_json(self, method: str, url: str) -> Any:
        data, _headers = self._request_json_with_headers(method, url, None)
        return data

    def _request_json_with_headers(
        self,
        method: str,
        url: str,
        params: Mapping[str, str] | None,
    ) -> tuple[Any, dict[str, str]]:
        if params:
            url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": "Bearer " + self._token,
            "User-Agent": USER_AGENT,
            "X-GitHub-Api-Version": DEFAULT_API_VERSION,
        }
        attempt = 0
        while True:
            if self._secondary_backoff > 0:
                self._sleep(self._secondary_backoff)
                self._secondary_backoff = 0.0
            attempt += 1
            try:
                raw, hdrs = self._request_fn(method, url, headers)
            except GitHubError as exc:
                if exc.status in (403, 429) or (exc.status is not None and exc.status >= 500):
                    retry_after = getattr(exc, "retry_after", None)
                    wait = (
                        float(retry_after) if retry_after is not None else min(60.0, 2.0**attempt)
                    )
                    if exc.status == 403:
                        self._secondary_backoff = max(wait, 2.0)
                    if attempt >= self._max_retries:
                        raise
                    self._sleep(wait)
                    continue
                raise
            try:
                data = json.loads(raw)
            except ValueError as exc:
                raise GitHubError("invalid JSON from GitHub") from exc
            remaining = hdrs.get("X-RateLimit-Remaining")
            if remaining == "0":
                reset = hdrs.get("X-RateLimit-Reset")
                if reset and reset.isdigit():
                    wait = max(0.0, int(reset) - time.time()) + 1.0
                    self._sleep(min(wait, 120.0))
            return data, hdrs


FAIL_CONCLUSIONS = frozenset({"failure", "timed_out", "startup_failure", "action_required"})
IGNORE_CONCLUSIONS = frozenset({"skipped", "cancelled", "neutral"})
SUCCESS_CONCLUSIONS = frozenset({"success"})


def classify_run_conclusion(conclusion: str | None) -> str:
    """Return 'fail', 'success', 'ignore', or 'other'."""
    if not conclusion:
        return "other"
    if conclusion in IGNORE_CONCLUSIONS:
        return "ignore"
    if conclusion in SUCCESS_CONCLUSIONS:
        return "success"
    if conclusion in FAIL_CONCLUSIONS:
        return "fail"
    return "other"


def pick_failing_job_step(jobs_payload: Mapping[str, Any]) -> tuple[str, str]:
    """First failed job and step on a workflow run."""
    jobs = jobs_payload.get("jobs")
    if not isinstance(jobs, list):
        return "", ""
    for job in jobs:
        if not isinstance(job, dict):
            continue
        if job.get("conclusion") not in FAIL_CONCLUSIONS:
            continue
        job_name = str(job.get("name") or "")
        steps = job.get("steps")
        if isinstance(steps, list):
            for step in steps:
                if isinstance(step, dict) and step.get("conclusion") in FAIL_CONCLUSIONS:
                    return job_name, str(step.get("name") or "")
        return job_name, ""
    return "", ""
