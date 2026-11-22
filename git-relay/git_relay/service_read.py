"""Fixed read-only contract for the existing Operator review-alert client.

Authentication, repository allowlists and quotas remain the relay's normal
policy boundary. No GitHub credential or arbitrary API path crosses it.
"""

from __future__ import annotations

import hmac
import re

from .github_client import GitHubClient, RelayGitHubError

ACTION = "service_read"
PRINCIPAL = "swu-operator"
CLAIMS = ("origin_session_claim", "origin_letter_claim", "origin_generation_claim")
SHA = re.compile(r"[0-9a-f]{40}")
SESSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{3,127}")
LOGIN = re.compile(r"[A-Za-z0-9_-]{1,100}(?:\[bot\])?")


def origin_claims(args):
    present = set(args) & set(CLAIMS)
    if not present:
        return {}
    if present != set(CLAIMS):
        raise ValueError("native origin claims must be supplied together")
    session, letter, generation = (args[k] for k in CLAIMS)
    if (
        not isinstance(session, str)
        or not SESSION.fullmatch(session)
        or not isinstance(letter, str)
        or not re.fullmatch(r"[A-Z]{2}", letter)
        or type(generation) is not int
        or not 1 <= generation <= 2**53 - 1
    ):
        raise ValueError("invalid native origin claims")
    return {k: args[k] for k in CLAIMS}


def validate(args):
    verb = args.get("verb")
    specs = {
        "pull": {"repo", "verb", "number"},
        "reviews": {"repo", "verb", "number", "page"},
        "review_comments": {"repo", "verb", "number", "page"},
        "check_runs": {"repo", "verb", "sha", "page"},
        "creation_receipts": {"repo", "verb"},
    }
    if not isinstance(verb, str) or verb not in specs or set(args) != specs[verb]:
        raise ValueError("unsupported service read envelope")
    GitHubClient._check_repo(args.get("repo"))
    if "number" in args and (type(args["number"]) is not int or args["number"] < 1):
        raise ValueError("invalid service PR number")
    if "page" in args and (type(args["page"]) is not int or not 1 <= args["page"] <= 5):
        raise ValueError("invalid service page")
    if "sha" in args and (not isinstance(args["sha"], str) or not SHA.fullmatch(args["sha"])):
        raise ValueError("invalid service commit")
    return dict(args)


def _bad():
    raise RelayGitHubError(502, "review service source unverifiable")


def _positive(value):
    if type(value) is not int or value < 1:
        _bad()
    return value


def _sha(value):
    if not isinstance(value, str) or not SHA.fullmatch(value):
        _bad()
    return value


def _actor(row):
    user = row.get("user")
    login = user.get("login") if isinstance(user, dict) else None
    if not isinstance(login, str) or not LOGIN.fullmatch(login):
        _bad()
    return {"login": login}


def _stamp(value):
    # Preserve absence so first-sight seeding retains its conservative policy.
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 64 or not re.fullmatch(r"[0-9TZtz:+.\-]+", value):
        _bad()
    return value


def dispatch(relay, seat, args):
    # The caller already passed the normal action/repo policy and rate check.
    from .server import PolicyDenied, ServerUnavailable

    slug, ceiling = relay.policy.app_identity(seat)
    if seat != PRINCIPAL or slug != PRINCIPAL:
        raise PolicyDenied("review service requires its own Operator identity")
    repo, verb = args["repo"], args["verb"]
    if verb == "creation_receipts":
        store = relay.pr_owners
        if store is None:
            raise ServerUnavailable("protected creation receipts unavailable")
        try:
            rows = store.protected_for_repo(repo)
        except ValueError:
            raise ServerUnavailable("protected creation receipts unavailable") from None
        fields = ("ts", "seat", "repo", "number", "corr", "confirmed", "source", *CLAIMS)
        return {
            "body": [
                {k: row[k] for k in fields if k in row}
                for row in rows
                if row.get("confirmed") is True and row.get("source") == "open_pr"
            ]
        }
    required = {"checks" if verb == "check_runs" else "pull_requests": "read", "metadata": "read"}
    if any(ceiling.get(name) not in ("read", "write") for name in required):
        raise PolicyDenied("review service permission ceiling insufficient")
    if relay.app_minter is None:
        raise ServerUnavailable("review service own-App minter unavailable")
    token, _ = relay.app_minter.token_for(slug, repo, required)
    root = "/repos/" + repo
    if verb == "pull":
        value = relay.client._call("GET", root + "/pulls/%d" % args["number"], bearer=token)
        if (
            not isinstance(value, dict)
            or type(value.get("number")) is not int
            or value["number"] != args["number"]
            or value.get("state") not in ("open", "closed")
            or not isinstance(value.get("head"), dict)
        ):
            _bad()
        result = {
            "number": value["number"],
            "state": value["state"],
            "head": {"sha": _sha(value["head"].get("sha"))},
        }
        url = value.get("html_url")
        if url == "https://github.com/%s/pull/%d" % (repo, args["number"]):
            result["html_url"] = url
        return {"body": result}
    page = args["page"]
    if verb == "check_runs":
        path = root + "/commits/%s/check-runs?per_page=100&page=%d&filter=all" % (args["sha"], page)
    else:
        tail = "reviews" if verb == "reviews" else "comments"
        path = root + "/pulls/%d/%s?per_page=100&page=%d" % (args["number"], tail, page)
    value = relay.client._call("GET", path, bearer=token)
    rows = value.get("check_runs") if verb == "check_runs" and isinstance(value, dict) else value
    if not isinstance(rows, list) or len(rows) > 100:
        _bad()
    results = []
    for row in rows:
        if not isinstance(row, dict):
            _bad()
        item = {"id": _positive(row.get("id"))}
        if verb == "reviews":
            if row.get("state") not in (
                "APPROVED",
                "CHANGES_REQUESTED",
                "COMMENTED",
                "DISMISSED",
                "PENDING",
            ):
                _bad()
            item.update(
                state=row["state"],
                commit_id=(
                    None
                    if row["state"] == "PENDING" and row.get("commit_id") is None
                    else _sha(row.get("commit_id"))
                ),
                user=_actor(row),
                submitted_at=_stamp(row.get("submitted_at")),
            )
        elif verb == "review_comments":
            item.update(
                original_commit_id=_sha(row.get("original_commit_id")),
                user=_actor(row),
                created_at=_stamp(row.get("created_at")),
            )
        else:
            from .workflow_verbs import WorkflowVerbs

            metadata = WorkflowVerbs._check_run_metadata(row)
            if metadata["head_sha"] != args["sha"]:
                _bad()
            app = {"id": metadata["app_id"]}
            slug = row["app"].get("slug")
            if slug is not None:
                if not isinstance(slug, str) or not re.fullmatch(r"[a-z0-9-]{1,100}", slug):
                    _bad()
                app["slug"] = slug
            item.update(
                head_sha=args["sha"],
                name=metadata["name"],
                status=metadata["status"],
                conclusion=metadata["conclusion"],
                completed_at=_stamp(row.get("completed_at")),
                app=app,
            )
        results.append(item)
    return {"body": {"check_runs": results} if verb == "check_runs" else results}


# --------------------------------------------------------------------------
# The dedicated-bearer lane
#
# A relay that configures the lane answers the review-alert client with its
# own credential, its own repository allowlist and its own narrowed
# one-repository App grant.  The client presents that credential
# (scripts/review_alerts/service.py:  RA_SERVICE_TOKEN_FILE), so the lane
# owns the action on such a relay;  a relay with no lane configured leaves
# the action to the normal relay policy, where the seat's own grant,
# repository scope and quota apply.
# --------------------------------------------------------------------------

#: The App identity the lane mints for:  the operator's own App.
SERVICE_READ_APP_SLUG = "swu-operator"
#: The one grant set a service read mints:  the narrowest the read verbs need.
SERVICE_READ_GRANTS = {"pull_requests": "read", "checks": "read", "metadata": "read"}
#: owner/name, each part 1-100 of the characters GitHub allows;  never a URL fragment.
LANE_REPO = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}/[A-Za-z0-9][A-Za-z0-9._-]{0,99}")
#: A commit sha for the check_runs verb:  7-40 lowercase hex.
LANE_SHA = re.compile(r"[0-9a-f]{7,40}")
#: The fixed read verbs, each one GET path.  No caller-composed path ever leaves here.
LANE_VERBS = {
    "pull": "/repos/{repo}/pulls/{number}",
    "reviews": "/repos/{repo}/pulls/{number}/reviews?per_page=100&page={page}",
    "review_comments": "/repos/{repo}/pulls/{number}/comments?per_page=100&page={page}",
    "check_runs": "/repos/{repo}/commits/{sha}/check-runs?per_page=100&page={page}",
}
LANE_MAX_PAGE = 20
#: The creation-receipt census bound:  the most eligible rows one answer may
#: carry.  A repo with more fails closed instead of paging.
LANE_RECEIPT_LIMIT = 1000


def lane(relay, token, args) -> tuple[int, dict]:
    """The alert service's fixed-shape read lane (no seat, no token export)."""
    expected = relay.service_read_token
    args = args if isinstance(args, dict) else {}
    if expected is None:
        # Every exit from this lane is audited, so an unconfigured-lane
        # probe stays visible in review instead of a silent 503.
        relay.audit.record("service-read", "service_read", "", "error", {"cause": "not_configured"})
        return 503, {"error": "service reads are not configured"}
    if not isinstance(token, str) or not hmac.compare_digest(
        # A crafted bearer, including one carrying a lone surrogate, is
        # an unauthenticated request:  it must not escape as an
        # exception and skip the audit row.
        token.encode("utf-8", "surrogatepass"),
        expected.encode("utf-8", "surrogatepass"),
    ):
        relay._unauth_auditor.record(relay.audit, "service_read")
        return 401, {"error": "unauthenticated"}
    repo = args.get("repo")
    if not isinstance(repo, str) or not LANE_REPO.fullmatch(repo):
        relay.audit.record("service-read", "service_read", "", "bad_request", {"field": "repo"})
        return 400, {"error": "repo must be owner/name"}
    if repo.casefold() not in {allowed.casefold() for allowed in relay.service_read_repos}:
        # GitHub repository names are case-insensitive, and the
        # ownership index and the mint answer both fold case:  the
        # allowlist must not make availability depend on the config's
        # spelling.  The validated spelling is kept for the upstream path.
        relay.audit.record(
            "service-read", "service_read", repo, "denied", {"cause": "repo_not_allowed"}
        )
        return 403, {"error": "repo is not on the service-read allowlist"}
    # Protected same-service creation receipts. No filesystem permission
    # change, arbitrary path, caller envelope, App mint or token export.
    if args.get("verb") == "creation_receipts":
        if set(args) != {"repo", "verb"}:
            relay.audit.record(
                "service-read",
                "service_read",
                repo,
                "bad_request",
                {"field": "verb", "cause": "creation_receipt_args"},
            )
            return 400, {"error": "creation receipt query accepts repo and verb only"}
        if relay.pr_owners is None:
            relay.audit.record(
                "service-read",
                "service_read",
                repo,
                "error",
                {"cause": "owners_unavailable"},
            )
            return 503, {"error": "creation receipts unavailable"}
        # The store answers this query under the bound:  it stops at the
        # limit and reports the overflow, so the per-repo history is never
        # materialized in full and the refusal is decided first.
        rows, overflow = relay.pr_owners.confirmed_for_repo(repo, LANE_RECEIPT_LIMIT)
        if overflow:
            relay.audit.record(
                "service-read",
                "service_read",
                repo,
                "error",
                {"cause": "census_exceeded", "limit": LANE_RECEIPT_LIMIT},
            )
            return 503, {"error": "creation receipt census exceeds bounded query"}
        relay.audit.record(
            "service-read",
            "service_read",
            repo,
            "ok",
            {"verb": "creation_receipts", "count": len(rows)},
        )
        return 200, {"body": rows}
    verb = args.get("verb")
    if verb not in LANE_VERBS:
        relay.audit.record("service-read", "service_read", repo, "bad_request", {"field": "verb"})
        return 400, {"error": "unknown read verb"}
    number = args.get("number")
    if verb == "check_runs":
        sha = args.get("sha")
        if not isinstance(sha, str) or not LANE_SHA.fullmatch(sha):
            relay.audit.record(
                "service-read", "service_read", repo, "bad_request", {"field": "sha"}
            )
            return 400, {"error": "sha must be 7-40 lowercase hex"}
        number = 0
    elif type(number) is not int or not 1 <= number <= 99999999:
        relay.audit.record("service-read", "service_read", repo, "bad_request", {"field": "number"})
        return 400, {"error": "number must be a positive integer"}
    page = args.get("page", 1)
    if type(page) is not int or not 1 <= page <= LANE_MAX_PAGE:
        relay.audit.record("service-read", "service_read", repo, "bad_request", {"field": "page"})
        return 400, {"error": "page out of range"}
    if relay.app_minter is None:
        relay.audit.record(
            "service-read", "service_read", repo, "error", {"cause": "minter_absent"}
        )
        return 503, {"error": "service reads are not wired"}
    detail = {"verb": verb, "number": number, "page": page}
    try:
        # The strict lane only:  the minted token's grants and repository
        # set are validated against the request before use, and a failed
        # validation revokes it.  Legacy seat mints are untouched.
        bearer, _expiry = relay.app_minter.token_for_service(
            SERVICE_READ_APP_SLUG, repo, dict(SERVICE_READ_GRANTS)
        )
    except Exception:
        relay.audit.record("service-read", "service_read", repo, "error", {**detail, "at": "mint"})
        return 502, {"error": "read grant unavailable"}
    path = LANE_VERBS[verb].format(repo=repo, number=number, page=page, sha=args.get("sha"))
    try:
        try:
            body = relay.client._call("GET", path, bearer=bearer, timeout=30)
        except RelayGitHubError as exc:
            relay.audit.record(
                "service-read",
                "service_read",
                repo,
                "error",
                {**detail, "at": "github", "github_code": exc.code},
            )
            return 502, {"error": "upstream read failed", "github_code": exc.code}
        except Exception as exc:
            # Every exit from this lane writes exactly one row, so an
            # unexpected upstream failure is recorded before it escapes
            # instead of hiding from review.  The exception still
            # reaches the caller;  only the row is added here.
            relay.audit.record(
                "service-read",
                "service_read",
                repo,
                "error",
                {**detail, "at": "github", "exception": type(exc).__name__},
            )
            raise
        relay.audit.record("service-read", "service_read", repo, "ok", detail)
        return 200, {"verb": verb, "repo": repo, "number": number, "page": page, "body": body}
    finally:
        # The fresh mint dies with the call:  success or failure, the
        # token is revoked once the read is over.  A revocation GitHub
        # does not accept leaves it live for up to an hour, so that
        # failure is recorded as secret-free operational evidence.
        if not relay.app_minter.revoke_installation_token(bearer):
            relay.audit.record(
                "service-read",
                "service_read",
                repo,
                "error",
                {**detail, "at": "revoke"},
            )
