"""Bounded PR state operations. No merge, branch deletion, or token fallback.

The resource reservation spans callers and argument aliases. A pending/unknown
write cannot be re-armed by a nonce, a different title, or an alternate state.
The operation reservation additionally binds the observed immutable identity
and prior version, so a later close/reopen/close cycle is a distinct operation.
"""

from __future__ import annotations

import re
from urllib.parse import quote

from .github_client import RelayGitHubError
from .outcomes import OutcomeStoreError, key_for

SHA = re.compile(r"[0-9a-f]{40}\Z")
STAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z")
REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")


class Refused(Exception):
    pass


class Held(Refused):
    pass


def positive(value):
    return type(value) is int and 0 < value <= 9007199254740991


def repo_identity(row):
    if (
        not isinstance(row, dict)
        or not positive(row.get("id"))
        or not isinstance(row.get("full_name"), str)
        or not REPO.fullmatch(row["full_name"])
    ):
        raise Refused("PR repository identity unverifiable")
    return {"id": row["id"], "full_name": row["full_name"]}


def snapshot(row, repo, number):
    if (
        not isinstance(row, dict)
        or row.get("errors")
        or row.get("message")
        or type(row.get("number")) is not int
        or row["number"] != number
        or not positive(row.get("id"))
        or not isinstance(row.get("node_id"), str)
        or not re.fullmatch(r"[A-Za-z0-9_+/=-]{4,200}", row["node_id"])
        or row.get("state") not in ("open", "closed")
        or row.get("merged") is not False
        or type(row.get("draft")) is not bool
        or not isinstance(row.get("updated_at"), str)
        or not STAMP.fullmatch(row["updated_at"])
        or row.get("html_url") != f"https://github.com/{repo}/pull/{number}"
    ):
        raise Refused("PR identity, state or merged status unverifiable")
    out = {
        k: row[k] for k in ("id", "node_id", "number", "state", "draft", "updated_at", "html_url")
    }
    for side in ("head", "base"):
        part = row.get(side)
        if (
            not isinstance(part, dict)
            or not isinstance(part.get("sha"), str)
            or not SHA.fullmatch(part["sha"])
            or not isinstance(part.get("ref"), str)
            or not part["ref"]
            or len(part["ref"]) > 255
            or any(ord(c) < 33 or ord(c) == 127 for c in part["ref"])
        ):
            raise Refused("PR branch identity unverifiable")
        out[side] = {
            "sha": part["sha"],
            "ref": part["ref"],
            "repo": repo_identity(part.get("repo")),
        }
    if out["base"]["repo"]["full_name"] != repo:
        raise Refused("PR base repository mismatch")
    return out


def identity(row):
    return {k: row[k] for k in ("id", "node_id", "number", "html_url", "head", "base")}


class Lifecycle:
    def __init__(self, relay, seat, action, args):
        self.relay, self.seat, self.action, self.args = relay, seat, action, args
        self.client, self.store = relay.client, relay.outcomes
        self.repo = args["repo"]
        self.keys = []
        self.sent = False
        self.received = False

    def finish(self, state, result=None):
        for key in self.keys:
            self.store.finish(key, state, result)

    def reserve(self, key, reapply=False):
        prior = self.store.begin(key, reapply_ok=reapply)
        if prior is None:
            self.keys.append(key)
            return None
        if prior["state"] in ("pending", "unknown"):
            raise Held("PR lifecycle outcome held; reconcile before another write")
        return prior.get("result")

    def call(self, method, path, payload=None, bearer=None):
        return self.client._call(method, path, payload, bearer=bearer)

    def branches(self, row, bearer):
        for side in ("head", "base"):
            branch = row[side]
            name = quote(branch["ref"], safe="")
            data = self.call(
                "GET", f"/repos/{branch['repo']['full_name']}/git/ref/heads/{name}", bearer=bearer
            )
            if (
                not isinstance(data, dict)
                or data.get("ref") != "refs/heads/" + branch["ref"]
                or not isinstance(data.get("object"), dict)
                or data["object"].get("type") != "commit"
                or data["object"].get("sha") != branch["sha"]
            ):
                raise Refused("PR branch absent or changed")

    def create(self):
        a, repo = self.args, self.repo
        # Creation aliases (different title/body/draft) share one held resource.
        resource = {"repo": repo.casefold(), "base": a["base"], "head": a["head"]}
        digest = key_for("pr-intent", "pr_create", a)
        prior = self.reserve(key_for("pr-resource", "pr_create", resource))
        if prior is not None:
            if prior.get("request_digest") != digest:
                raise Refused("creation resource bound to a different request; reconcile first")
            replay_raw = self.call("GET", f"/repos/{repo}/pulls/{prior['number']}")
            if (
                not isinstance(replay_raw, dict)
                or replay_raw.get("title") != a["title"]
                or replay_raw.get("body") != a["body"]
            ):
                raise Refused("creation text replay mismatch")
            row = snapshot(replay_raw, repo, prior["number"])
            if row["state"] != "open" or {
                k: v for k, v in prior.items() if k != "request_digest"
            } != self.result(row):
                raise Refused("creation receipt no longer matches current PR")
            return prior
        # Keep the established owner-authored creation lane, not a new grant.
        # Restrict refs to same-repository branch names; no implicit fork reads.
        if any(":" in a[k] for k in ("head", "base")):
            raise Refused("cross-repository creation source unverifiable")
        repository = repo_identity(self.call("GET", f"/repos/{repo}"))
        if repository["full_name"] != repo:
            raise Refused("creation repository mismatch")
        expected = {}
        for side in ("head", "base"):
            ref = a[side]
            if not isinstance(ref, str) or not ref or len(ref) > 255:
                raise Refused("creation branch invalid")
            branch = self.call("GET", f"/repos/{repo}/git/ref/heads/{quote(ref, safe='')}")
            obj = branch.get("object") if isinstance(branch, dict) else None
            if (
                not isinstance(obj, dict)
                or obj.get("type") != "commit"
                or not isinstance(obj.get("sha"), str)
                or not SHA.fullmatch(obj["sha"])
                or branch.get("ref") != "refs/heads/" + ref
            ):
                raise Refused("creation branch unverifiable")
            expected[side] = {"ref": ref, "sha": obj["sha"], "repo": repository}
        payload = {k: a[k] for k in ("head", "base", "title", "body")}
        if a.get("draft") is True:
            payload["draft"] = True
        self.sent = True
        raw = self.call("POST", f"/repos/{repo}/pulls", payload)
        self.received = True
        if not isinstance(raw, dict) or not positive(raw.get("number")):
            raise Refused("creation receipt unverifiable")
        if raw.get("title") != a["title"] or raw.get("body") != a["body"]:
            raise Refused("creation text receipt mismatch")
        row = snapshot(raw, repo, raw["number"])
        if (
            row["state"] != "open"
            or row["draft"] is not a.get("draft", False)
            or any(row[k] != expected[k] for k in ("head", "base"))
        ):
            raise Refused("creation receipt mismatch")
        fresh_raw = self.call("GET", f"/repos/{repo}/pulls/{row['number']}")
        if (
            not isinstance(fresh_raw, dict)
            or fresh_raw.get("title") != a["title"]
            or fresh_raw.get("body") != a["body"]
        ):
            raise Refused("creation text readback mismatch")
        fresh = snapshot(fresh_raw, repo, row["number"])
        if fresh != row:
            raise Refused("creation readback mismatch")
        self.branches(fresh, None)
        return {**self.result(fresh), "request_digest": digest}

    def state(self):
        a, repo = self.args, self.repo
        number = a["number"]
        # Stable resource hold is independent of actor, target, version and text.
        self.reserve(
            key_for("pr-resource", "pr_lifecycle", {"repo": repo.casefold(), "number": number}),
            True,
        )
        slug, ceiling = self.relay.policy.app_identity(self.seat)
        if slug != self.seat:
            raise Refused("own App required")
        required = {"pull_requests": "write", "contents": "read"}
        from .server import PolicyDenied, ServerUnavailable, _perm_within_ceiling

        if _perm_within_ceiling(required, ceiling):
            raise PolicyDenied("seat ceiling does not cover PR lifecycle permissions")
        if self.relay.app_minter is None:
            raise ServerUnavailable("own App minting unavailable")
        reader, _ = self.relay.app_minter.token_for(
            slug, repo, {"pull_requests": "read", "contents": "read"}
        )
        path = f"/repos/{repo}/pulls/{number}"
        canonical_repo = repo_identity(self.call("GET", f"/repos/{repo}", bearer=reader))
        if canonical_repo["full_name"] != repo:
            raise Refused("canonical repository mismatch")
        before = snapshot(self.call("GET", path, bearer=reader), repo, number)
        if before["base"]["repo"] != canonical_repo:
            raise Refused("canonical repository ID mismatch")
        if before["head"]["repo"]["full_name"] != repo:
            raise Refused("cross-repository branch source unverifiable")
        if before["head"]["repo"] != canonical_repo:
            raise Refused("canonical head repository ID mismatch")
        self.branches(before, reader)
        # Validate target no-op only after immutable identity and unmerged checks.
        if before["head"]["sha"] != a["expected_head_sha"]:
            raise Refused("PR head differs from expected source")
        if before["updated_at"] != a["expected_updated_at"]:
            raise Refused("PR prior version differs from expected version")
        op = key_for(
            self.seat,
            "pr_lifecycle",
            {
                "repo": repo,
                "identity": identity(before),
                "version": before["updated_at"],
                "prior_state": before["state"],
                "target": a["state"],
            },
        )
        prior = self.reserve(op, True)
        if prior is not None:
            # Never return a stale stored receipt without a fresh source read.
            if before["state"] != a["state"]:
                raise Refused("recorded operation no longer matches current state")
            return self.result(before, True)
        if before["state"] == a["state"]:
            return self.result(before, True)
        # Re-read after branch checks and before minting a mutation credential.
        if snapshot(self.call("GET", path, bearer=reader), repo, number) != before:
            raise Refused("PR changed during preflight")
        writer, _ = self.relay.app_minter.token_for(slug, repo, {"pull_requests": "write"})
        self.sent = True
        raw = self.call("PATCH", path, {"state": a["state"]}, writer)
        self.received = True
        receipt = snapshot(raw, repo, number)
        if (
            identity(receipt) != identity(before)
            or receipt["state"] != a["state"]
            or receipt["draft"] != before["draft"]
        ):
            raise Refused("PR write receipt mismatch")
        after = snapshot(self.call("GET", path, bearer=reader), repo, number)
        if after != receipt:
            raise Refused("PR state readback mismatch")
        self.branches(after, reader)
        return self.result(after, False)

    @staticmethod
    def result(row, noop=False):
        return {
            "number": row["number"],
            "url": row["html_url"],
            "state": row["state"],
            "draft": row["draft"],
            "head_sha": row["head"]["sha"],
            "updated_at": row["updated_at"],
            "noop": noop,
            "verified": True,
        }

    def execute(self):
        return self._execute()

    def _execute(self):
        from .app_tokens import AppTokenError
        from .server import PolicyDenied, ServerUnavailable

        try:
            if self.store.has_held_except():
                raise Held("an unreconciled relay write blocks PR lifecycle")
            result = self.create() if self.action == "open_pr" else self.state()
            try:
                self.finish("ok", result)
            except Exception:
                # Leave reservation held on persistence failure.
                return 503, {
                    "error": "PR lifecycle outcome persistence unavailable",
                    "outcome": "unknown",
                    "retryable": False,
                }
            return 200, result
        except (
            Refused,
            PolicyDenied,
            ServerUnavailable,
            AppTokenError,
            RelayGitHubError,
            OutcomeStoreError,
        ) as exc:
            if self.sent and not self.received and isinstance(exc, RelayGitHubError):
                # A definite mutation refusal releases this reservation. A
                # readback failure after any receipt never does.
                definite = exc.code == "invalid_grant" or (
                    400 <= exc.status < 500 and exc.status != 408
                )
                if definite:
                    self.finish("refused")
                    status = (
                        503 if exc.code == "invalid_grant" else 429 if exc.status == 429 else 400
                    )
                    payload = {
                        "error": "github refused PR lifecycle request",
                        "github_status": exc.status,
                        "retryable": exc.code == "invalid_grant" or exc.status == 429,
                        "github_diagnostics": dict(exc.diagnostics),
                    }
                    if exc.code:
                        payload["github_code"] = exc.code
                    if exc.retry_after is not None:
                        payload["retry_after_seconds"] = exc.retry_after
                    if exc.credential_preview is not None:
                        payload["credential_preview"] = exc.credential_preview
                    return status, payload
            if self.sent:
                # Even a definite refusal of a subsequent GET cannot release a
                # successful but unverifiable mutation. No raw provider prose.
                self.finish("unknown")
                payload = {
                    "error": "PR lifecycle outcome unknown; reconcile before retry",
                    "outcome": "unknown",
                    "retryable": False,
                }
                if isinstance(exc, RelayGitHubError):
                    payload.update(
                        github_status=exc.status, github_diagnostics=dict(exc.diagnostics)
                    )
                    if exc.code:
                        payload["github_code"] = exc.code
                return 502, payload
            self.finish("refused")
            if isinstance(exc, Held):
                return 409, {
                    "error": "PR lifecycle outcome held; reconcile before retry",
                    "outcome": "unknown",
                    "retryable": False,
                }
            if isinstance(exc, PolicyDenied):
                return 403, {"error": "PR lifecycle permission denied", "retryable": False}
            return 409 if isinstance(exc, Refused) else 503, {
                "error": "PR lifecycle preflight unavailable or refused; no write attempted",
                "retryable": False,
            }
        except Exception:
            self.finish("unknown" if self.sent else "refused")
            return 500, {
                "error": "PR lifecycle unavailable",
                "outcome": "unknown" if self.sent else "refused",
                "retryable": False,
            }
