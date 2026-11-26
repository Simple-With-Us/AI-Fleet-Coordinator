"""Read-only PR source reads for the alert service, through the relay's
service_read lane.

The service never holds a GitHub token:  the relay mints the narrowed
one-repo grant (swu-operator, pull_requests/checks/metadata read) and answers
with the body only.  This client carries the service's own dedicated bearer
for that lane (a credential file, never a seat token, never the relay's
owner credential).

Only the fixed verbs exist:  pull, reviews, review_comments, check_runs,
issue, issue_comments, issue_comment.
A 401/403 is fail-closed:  raised, recorded by the caller, retried next
cycle - never worked around.  A 5xx or transport failure is retryable the
same way:  no cursor advances on either.

Issue-surface contract (qualified relay lane):  the DEDICATED authenticated
service_read lane (this client's lane;  never the seat-dispatch projection,
which carries no body) returns the RAW canonical GitHub objects, comment
body included, and the answer envelope echoes the request's verb/repo/
number (and comment_id for the single-comment read).  Every verb
re-validates that envelope and the canonical body:  an envelope that does
not match the request, a mismatched id, a malformed pull_request marker or
a foreign URL refuses the read.  A failed upstream read arrives as a relay
502 whose body carries the integer github_status (301/404/410/...):  it is
raised as GithubReadError with github_status set, so the caller can hold
for uncertainty - a 404 alone never proves deletion or missing ownership.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from . import comment_evidence, stamps

MAX_PAGES = 5
TIMEOUT = 30

#: Bounds shared with the relay lane and every downstream consumer:
#: issue/PR numbers 1..99,999,999 (8 digits), comment ids the exact safe
#: integer range 1..2**53-1 (no truncation, no rounding).
ISSUE_NUMBER_MAX = comment_evidence.MAX_ISSUE_NUMBER
COMMENT_ID_MAX = comment_evidence.MAX_COMMENT_ID


class GithubReadError(Exception):
    def __init__(self, status: int, path: str, github_status: int | None = None):
        super().__init__("service read %s answered HTTP %s" % (path, status))
        self.status = status
        self.path = path
        # The upstream GitHub status when the relay reports one (502 lane);
        # None when the failure is local/transport/malformed.
        self.github_status = github_status


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
        return self._read_full(payload)["body"]

    def _read_full(self, payload: dict) -> dict:
        """The whole service_read answer:  body plus lane metadata such as the
        explicit coverage marker.  Consumers that only need the body use
        _read;  anyone reporting completeness must read the marker here."""
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
            github_status = None
            if exc.code == 502:
                try:
                    detail = json.loads(exc.read().decode("utf-8"))
                    status = detail.get("github_status") if isinstance(detail, dict) else None
                    github_status = status if type(status) is int else None
                except (ValueError, UnicodeDecodeError, OSError):
                    github_status = None
            raise GithubReadError(
                exc.code, "service_read:" + str(payload.get("verb")), github_status
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            raise GithubReadError(-2, "service_read:%s" % type(exc).__name__) from exc
        if not isinstance(answer, dict) or "body" not in answer:
            raise GithubReadError(-2, "service_read:malformed-answer")
        return answer

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

    def issue(self, repo: str, number: int) -> dict:
        """The canonical type/state of one issue-or-PR from the RAW canonical
        object (dedicated lane).  The kind comes ONLY from the canonical
        pull_request marker:  absent is an issue, an object is a pull
        request, anything else (null, string, list) is ambiguous and the
        read refuses.  Semantics byte-adopted from the qualified writer
        canonical_issue_proof:  the envelope and body must name exactly the
        requested repo+number, and the observed URL must be exactly the
        canonical object path (/issues/<n> or /pull/<n>)."""
        _check_issue_number(number)
        answer = self._read_full({"repo": repo, "number": number, "verb": "issue"})
        _check_envelope(answer, "issue", repo, number)
        body = answer["body"]
        if (
            not isinstance(body, dict)
            or type(body.get("number")) is not int
            or body["number"] != number
            or body.get("state") not in ("open", "closed")
        ):
            raise GithubReadError(-2, "service_read:issue-malformed")
        if "pull_request" in body:
            if not isinstance(body["pull_request"], dict):
                raise GithubReadError(-2, "service_read:issue-marker-malformed")
            kind = "pull_request"
        else:
            kind = "issue"
        url = body.get("html_url")
        leaf = "pull" if kind == "pull_request" else "issues"
        expected = "https://github.com/%s/%s/%d" % (repo, leaf, number)
        if not isinstance(url, str) or url.casefold() != expected.casefold():
            raise GithubReadError(-2, "service_read:issue-foreign-url")
        # The canonical created_at grounds the issue_opened baseline decision
        # (opening vs the census-rollout boundary).  It is validated with the
        # same strict stamp rule as comment evidence and REFUSED when absent
        # or malformed:  a read that cannot prove its opening instant is
        # never projected without it (silently dropping it would quietly
        # disable the opening check).
        created = body.get("created_at")
        if not comment_evidence._valid_stamp(created):
            raise GithubReadError(-2, "service_read:issue-created-at-malformed")
        # The canonical numeric issue id grounds the issue_opened source-event
        # identity (the qualified router's i<id> grammar):  whole, exact,
        # 1..2**53-1, never the number and never a node id.  Refused when
        # absent or malformed, same discipline as created_at.
        issue_id = body.get("id")
        if type(issue_id) is not int or not 1 <= issue_id <= 2**53 - 1:
            raise GithubReadError(-2, "service_read:issue-id-malformed")
        return {
            "id": issue_id,
            "number": number,
            "state": body["state"],
            "kind": kind,
            "html_url": url,
            "created_at": created,
        }

    def issue_comments(self, repo: str, number: int) -> list:
        """The full ascending-id comment list for one issue-or-PR, every page
        drained.  GitHub's issue-comment list is ascending, so the watermark
        filter is the caller's;  hitting the page cap with a full last page
        means the tail may be unread, which is the Uncertain case (no
        watermark advance).  Every row is validated:  safe-integer id,
        nonempty author login, exact valid timestamps."""
        _check_issue_number(number)
        out: list = []
        for page in range(1, MAX_PAGES + 1):
            answer = self._read_full(
                {"repo": repo, "number": number, "verb": "issue_comments", "page": page}
            )
            _check_envelope(answer, "issue_comments", repo, number, page=page)
            body = answer["body"]
            if not isinstance(body, list):
                raise GithubReadError(-2, "service_read:issue_comments-not-a-list")
            out.extend(_comment_row(body_row, repo, number) for body_row in body)
            if len(body) < 100:
                return out
        raise GithubReadUncertain("service_read:issue_comments")

    def issue_comment(self, repo: str, comment_id: int, expected_number: int | None = None) -> dict:
        """One canonical comment by id, validated to the qualified evidence
        contract (comment_evidence.canonical_comment_evidence):  exact
        envelope, exact id, well-formed login/updated_at/body, and an
        issue_url proving its exact target - numeric existence alone never
        proves membership in any issue.  Returns the canonical evidence
        (id, login, updated_at, exact body, target_number, revision digest).
        Raises GithubReadError on transport/HTTP failure and
        CommentUnverified on any evidence gap;  the caller HOLDS for both
        (a 404 alone never proves deletion)."""
        if type(comment_id) is not int or not 1 <= comment_id <= COMMENT_ID_MAX:
            raise GithubReadError(-2, "service_read:issue_comment-id-out-of-range")
        answer = self._read_full(
            {"repo": repo, "verb": "issue_comment", "comment_id": comment_id}
        )
        evidence = comment_evidence.canonical_comment_evidence(
            repo, comment_id, answer, expected_number
        )
        # The evidence contract carries updated_at only;  an added/edited
        # decision needs created_at from the same canonical body, validated
        # with the same strict stamp rule and the canonical invariant
        # created <= updated.  comment_evidence._valid_stamp is reused, not
        # reimplemented:  one qualified definition of a strict stamp.
        created = (answer.get("body") or {}).get("created_at")
        if not comment_evidence._valid_stamp(created) or stamps.stamp_key(
            created
        ) > stamps.stamp_key(evidence["updated_at"]):
            raise comment_evidence.CommentUnverified(
                "comment lacks canonical creation evidence"
            )
        evidence["created_at"] = created
        return evidence


def _check_issue_number(number: int) -> None:
    if type(number) is not int or not 1 <= number <= ISSUE_NUMBER_MAX:
        raise GithubReadError(-2, "service_read:issue-number-out-of-range")


def _check_envelope(answer: dict, verb: str, repo: str, number: int, page: int | None = None) -> None:
    """The relay's own envelope is kept and validated, never synthesized from
    the request:  a mismatched or missing verb, repo, number or page refuses
    the read, so no mismatched/malformed answer is laundered into matching
    evidence."""
    if (
        answer.get("verb") != verb
        or not isinstance(answer.get("repo"), str)
        or answer["repo"].casefold() != repo.casefold()
        or type(answer.get("number")) is not int
        or answer["number"] != number
        or (
            page is not None
            and (type(answer.get("page")) is not int or answer["page"] != page)
        )
    ):
        raise GithubReadError(-2, "service_read:%s-envelope-mismatch" % verb)


def _comment_row(row, repo: str, number: int) -> dict:
    """One validated RAW canonical comment row (dedicated lane), or the read
    refuses:  safe-integer id, well-formed author login, strict created/
    updated stamps, a well-formed body, and an exact canonical issue_url
    proving membership in THIS target - never inferred from list position."""
    if not isinstance(row, dict):
        raise GithubReadError(-2, "service_read:comment-row-malformed")
    cid = row.get("id")
    user = row.get("user")
    login = user.get("login") if isinstance(user, dict) else None
    body = row.get("body")
    if (
        type(cid) is not int
        or not 1 <= cid <= COMMENT_ID_MAX
        or not isinstance(login, str)
        or not comment_evidence._LOGIN_RE.fullmatch(login)
        or not comment_evidence._valid_stamp(row.get("created_at"))
        or not comment_evidence._valid_stamp(row.get("updated_at"))
        or not isinstance(body, str)
        or len(body) > comment_evidence.MAX_COMMENT_BODY
    ):
        raise GithubReadError(-2, "service_read:comment-row-malformed")
    if stamps.stamp_key(row["created_at"]) > stamps.stamp_key(row["updated_at"]):
        # Same canonical invariant as the single read:  created <= updated,
        # compared at exact supported precision.
        raise GithubReadError(-2, "service_read:comment-row-malformed")
    url = row.get("issue_url")
    prefix = "https://api.github.com/repos/%s/issues/" % repo
    tail = url[len(prefix):] if isinstance(url, str) and url.casefold().startswith(prefix.casefold()) else ""
    if not tail.isascii() or not tail.isdigit() or tail != str(int(tail)) or int(tail) != number:
        raise GithubReadError(-2, "service_read:comment-row-target-mismatch")
    out = {
        "id": cid,
        "user": {"login": login},
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "body": body,
        "issue_url": url,
    }
    if isinstance(row.get("html_url"), str):
        out["html_url"] = row["html_url"]
    return out
