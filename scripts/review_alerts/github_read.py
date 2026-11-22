"""Read-only PR source reads for the alert service, through the relay's
service_read lane.

The service never holds a GitHub token:  the relay mints the narrowed
one-repo grant (swu-operator, pull_requests/checks/metadata read) and answers
with the body only.  This client carries the service's own dedicated bearer
for that lane (a credential file, never a seat token, never the relay's
owner credential).

Only the fixed verbs exist:  pull, reviews, review_comments, check_runs.
A 401/403 is fail-closed:  raised, recorded by the caller, retried next
cycle - never worked around.  A 5xx or transport failure is retryable the
same way:  no cursor advances on either.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

MAX_PAGES = 5
TIMEOUT = 30


class GithubReadError(Exception):
    def __init__(self, status: int, path: str):
        super().__init__("service read %s answered HTTP %s" % (path, status))
        self.status = status
        self.path = path


class GithubReadUncertain(GithubReadError):
    """The read did not provably reach the watermark:  paging hit its cap with
    the newest items still unread.  The caller must NOT advance any cursor
    from this cycle - the missed tail could hold the very events it would
    mark as seen."""

    def __init__(self, path: str):
        super().__init__(-3, path)


class ServiceReadClient:
    """POSTs the fixed read verbs to the relay's /v1/service_read endpoint."""

    def __init__(self, base_url: str, token_provider, *, opener=urllib.request.urlopen):
        self._base = base_url.rstrip("/")
        self._token = token_provider
        self._opener = opener

    def _read(self, payload: dict):
        token = self._token()
        if not token:
            raise GithubReadError(-1, "service_read:no-credential")
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(self._base + "/v1/service_read", data=body, method="POST")
        req.add_header("Authorization", "Bearer " + token)
        req.add_header("Content-Type", "application/json")
        try:
            with self._opener(req, timeout=TIMEOUT) as resp:
                answer = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise GithubReadError(exc.code, "service_read:" + str(payload.get("verb"))) from exc
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            raise GithubReadError(-2, "service_read:%s" % type(exc).__name__) from exc
        if not isinstance(answer, dict) or "body" not in answer:
            raise GithubReadError(-2, "service_read:malformed-answer")
        return answer["body"]

    def _paged(self, verb: str, repo: str, number: int, watermark: int = 0) -> list:
        """Newest-first pages until the watermark id (exclusive) is reached.

        GitHub's review and comment lists are oldest-first, so a watermark
        cannot stop the paging early:  every page up to the cap is read and
        the watermark only filters.  Hitting the cap with a full last page
        means older pages may hide UNREAD newer-than-watermark items only
        when the cap cut the list;  that is the Uncertain case.
        """
        out: list = []
        for page in range(1, MAX_PAGES + 1):
            body = self._read({"repo": repo, "number": number, "verb": verb, "page": page})
            if not isinstance(body, list):
                raise GithubReadError(-2, "service_read:%s-not-a-list" % verb)
            out.extend(item for item in body if isinstance(item, dict))
            if len(body) < 100:
                return out
        raise GithubReadUncertain("service_read:%s" % verb)

    def pull(self, repo: str, number: int) -> dict:
        body = self._read({"repo": repo, "number": number, "verb": "pull"})
        if not isinstance(body, dict):
            raise GithubReadError(-2, "service_read:pull-not-an-object")
        return body

    def reviews(self, repo: str, number: int, after_id: int = 0) -> list:
        items = self._paged("reviews", repo, number, after_id)
        return [r for r in items if isinstance(r.get("id"), int) and r["id"] > after_id]

    def review_comments(self, repo: str, number: int, after_id: int = 0) -> list:
        items = self._paged("review_comments", repo, number, after_id)
        return [c for c in items if isinstance(c.get("id"), int) and c["id"] > after_id]

    def check_runs(self, repo: str, sha: str) -> list:
        """The check runs on one exact commit.  The GitHub check-runs list is
        capped at 1000 and a single page is 100;  a PR head never nears that,
        but a full last page still raises Uncertain rather than truncating
        silently."""
        out: list = []
        for page in range(1, MAX_PAGES + 1):
            body = self._read({"repo": repo, "verb": "check_runs", "sha": sha, "page": page})
            runs = body.get("check_runs") if isinstance(body, dict) else None
            if not isinstance(runs, list):
                raise GithubReadError(-2, "service_read:check_runs-malformed")
            out.extend(r for r in runs if isinstance(r, dict))
            if len(runs) < 100:
                return out
        raise GithubReadUncertain("service_read:check_runs")
