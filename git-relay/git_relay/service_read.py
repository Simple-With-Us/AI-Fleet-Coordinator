"""Fixed read-only contract for the existing Operator review-alert client.

Authentication, repository allowlists and quotas remain the relay's normal
policy boundary. No GitHub credential or arbitrary API path crosses it.
"""

from __future__ import annotations

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
