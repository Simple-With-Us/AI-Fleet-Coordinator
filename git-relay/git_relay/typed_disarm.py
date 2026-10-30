"""Inactive typed candidate. Not registered in broker or Worker dispatch.

Only an authenticated, policy-authorized own-App caller may construct this
service. Authorization is repeated here through the supplied policy boundary.
No caller token, URL, query, arbitrary path or hold-clear operation is accepted.
"""

import re
import time

from .policy import RATE_LIMIT_DENIALS
from .server import _perm_within_ceiling

STATE_QUERY = """query($owner:String!,$name:String!,$number:Int!){viewer{login}
repository(owner:$owner,name:$name){nameWithOwner pullRequest(number:$number){
id number state headRefOid autoMergeRequest{enabledAt}
labels(first:50){nodes{name} pageInfo{hasNextPage}}
timelineItems(first:50,itemTypes:[LABELED_EVENT,UNLABELED_EVENT]){
nodes{__typename ... on LabeledEvent{id createdAt actor{login} label{name}}
... on UnlabeledEvent{id createdAt actor{login} label{name}}}
pageInfo{hasNextPage}}}}}"""
DISABLE_MUTATION = (
    """mutation($id:ID!){disablePullRequestAutoMerge(input:{pullRequestId:$id}){pullRequest{id}}}"""
)


ACTIONS = ("pr_state", "pr_hold_state", "pr_automerge_disable")


def _required_perms(action):
    return {
        "pull_requests": "write" if action == "pr_automerge_disable" else "read",
        "issues": "read",
    }


class Refused(ValueError):
    pass


class RateLimited(Refused):
    """Policy quota exhausted before any upstream effect."""


class Unknown(RuntimeError):
    """Fixed public error, never wraps upstream text or credentials."""


class PreflightUnavailable(Unknown):
    """Read-only preflight failed before any mutation could be attempted."""


_FACTORY_BINDING = object()


class TypedDisarm:
    def __init__(
        self,
        client,
        policy,
        seat,
        bearer,
        app_slug,
        action_scope,
        clock=time.monotonic,
        *,
        _binding=None,
    ):
        # Scope and ceiling bind on every construction; for_action is the
        # normal path, a direct caller must still name one action and repo.
        if _binding is not _FACTORY_BINDING:
            raise Refused("use authorized factory")
        self.client, self.policy, self.seat = client, policy, seat
        try:
            expected, ceiling = policy.app_identity(seat)
        except KeyError:
            raise Refused("unknown seat") from None
        if not bearer or app_slug != expected or app_slug != seat:
            raise Refused("own App required")
        if (
            not isinstance(action_scope, tuple)
            or len(action_scope) != 2
            or action_scope[0] not in ACTIONS
            or not isinstance(action_scope[1], str)
        ):
            raise Refused("action scope required")
        if _perm_within_ceiling(_required_perms(action_scope[0]), ceiling):
            raise Refused("seat permission ceiling insufficient")
        self._action_scope = action_scope
        self.bearer, self.app_slug, self.clock = bearer, app_slug, clock

    @classmethod
    def for_action(cls, client, policy, minter, seat, action, repo):
        # Seat must already be resolved by the authenticated broker boundary.
        if action not in ACTIONS:
            raise Refused("unsupported typed action")
        try:
            client._check_repo(repo)
        except ValueError:
            raise Refused("invalid repository") from None
        denial = policy.authorize(seat, action, repo)
        if denial in RATE_LIMIT_DENIALS:
            raise RateLimited("quota exhausted")
        if denial:
            raise Refused("action or repository denied")
        try:
            app_slug, ceiling = policy.app_identity(seat)
        except KeyError:
            raise Refused("unknown seat") from None
        required = _required_perms(action)
        if _perm_within_ceiling(required, ceiling):
            raise Refused("seat permission ceiling insufficient")
        try:
            token, _ = minter.token_for(app_slug, repo, required)
        except Exception:
            raise Unknown("own App credential unavailable") from None
        return cls(client, policy, seat, token, app_slug, (action, repo), _binding=_FACTORY_BINDING)

    def _query(self, query, variables):
        try:
            reply = self.client._call(
                "POST", "/graphql", {"query": query, "variables": variables}, bearer=self.bearer
            )
            if (
                not isinstance(reply, dict)
                or reply.get("errors")
                or not isinstance(reply.get("data"), dict)
            ):
                raise Unknown("upstream result unavailable")
            return reply["data"]
        except Exception:
            raise Unknown("upstream result unavailable") from None

    def _validate(self, action, args):
        if not isinstance(args, dict) or set(args) != {"repo", "number", "expected_head"}:
            raise Refused("exact repo, number, expected_head required")
        repo, number, head = args["repo"], args["number"], args["expected_head"]
        if not isinstance(repo, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
            raise Refused("invalid repository")
        if type(number) is not int or not 0 < number < 2**31:
            raise Refused("invalid PR number")
        if not isinstance(head, str) or not re.fullmatch(r"[0-9a-f]{40}", head):
            raise Refused("exact head SHA required")
        if getattr(self, "_action_scope", None) is not None and self._action_scope != (
            action,
            repo,
        ):
            raise Refused("credential action scope mismatch")
        if not hasattr(self, "_action_scope") and self.policy.authorize(self.seat, action, repo):
            raise Refused("action or repository denied")
        return repo, number, head

    def _state(self, repo, number, head):
        owner, name = repo.split("/")
        start = self.clock()
        data = self._query(STATE_QUERY, {"owner": owner, "name": name, "number": number})
        try:
            if self.clock() - start > 35:
                raise ValueError()
            login = data["viewer"]["login"]
            if login != self.app_slug + "[bot]":
                raise ValueError()
            repository = data["repository"]
            pr = repository["pullRequest"]
            if repository["nameWithOwner"] != repo or pr["number"] != number:
                raise ValueError()
            if pr["headRefOid"] != head:
                raise Refused("head changed")
            if not re.fullmatch(r"PR_[A-Za-z0-9_-]{1,160}", pr["id"]):
                raise ValueError()
            if pr["state"] not in ("OPEN", "CLOSED", "MERGED"):
                raise ValueError()
            # Deliberately no permission-to-arm inference from incomplete history.
            connections = [pr["labels"], pr["timelineItems"]]
            if any(
                type(c["pageInfo"]["hasNextPage"]) is not bool
                or not isinstance(c["nodes"], list)
                or len(c["nodes"]) > 50
                for c in connections
            ):
                raise ValueError()
            labels = []
            for node in pr["labels"]["nodes"]:
                label = node["name"]
                if not isinstance(label, str) or len(label.encode()) > 100:
                    raise ValueError()
                labels.append(label)
            events = []
            for node in pr["timelineItems"]["nodes"]:
                if node["__typename"] not in ("LabeledEvent", "UnlabeledEvent"):
                    raise ValueError()
                event = {k: node[k] for k in ("id", "createdAt", "__typename")}
                event["actor"] = (node.get("actor") or {}).get("login")
                event["label"] = node["label"]["name"]
                if any(
                    v is not None and (not isinstance(v, str) or len(v.encode()) > 160)
                    for v in event.values()
                ):
                    raise ValueError()
                events.append(event)
            auto = pr["autoMergeRequest"]
            if auto is not None and (
                not isinstance(auto, dict) or not isinstance(auto.get("enabledAt"), str)
            ):
                raise ValueError()
            return {
                "repo": repo,
                "number": number,
                "head": head,
                "pr_id": pr["id"],
                "state": pr["state"],
                "auto_merge_enabled": auto is not None,
                "labels": labels,
                "label_events": events,
                "complete": not any(c["pageInfo"]["hasNextPage"] for c in connections),
                "hold_decision": "not_evaluated",
                "principal": login,
            }
        except Refused:
            raise
        except Exception:
            raise Unknown("state or principal unavailable") from None

    def state(self, args):
        return self._state(*self._validate("pr_state", args))

    def hold_state(self, args):
        # Epoch records only. Existing setter/epoch policy is not replaced.
        return self._state(*self._validate("pr_hold_state", args))

    def disable(self, args):
        repo, number, head = self._validate("pr_automerge_disable", args)
        try:
            before = self._state(repo, number, head)
        except Unknown:
            raise PreflightUnavailable("preflight unavailable") from None
        if before["state"] != "OPEN":
            raise Refused("PR is not open")
        # Disable is safety-only and never edits/removes hold labels or comments.
        # GitHub has no atomic expectedHeadOid on disable; a concurrent head
        # update is detected on readback, not falsely described as fenced.
        attempted = before["auto_merge_enabled"]
        if attempted:
            try:
                self._query(DISABLE_MUTATION, {"id": before["pr_id"]})
            except Unknown:
                pass  # One reconciliation read, never a blind write retry.
        try:
            after = self._state(repo, number, head)
        except (Unknown, Refused):
            raise Unknown("disable outcome unknown") from None
        if (
            after["state"] != "OPEN"
            or after["auto_merge_enabled"]
            or after["pr_id"] != before["pr_id"]
        ):
            raise Unknown("disable outcome unknown")
        return {
            **after,
            "outcome": "confirmed_disabled",
            "write_attempted": attempted,
            "atomic_head_guard": False,
        }
