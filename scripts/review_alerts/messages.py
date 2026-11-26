"""Operator review notifications use the existing participant Side-Chats route."""

from __future__ import annotations

import re
from urllib.parse import urlsplit


class MessageRetryable(Exception):
    """The Operator refused before posting anything; retry is safe."""


class MessageDestinationUnresolved(MessageRetryable):
    """Current metadata cannot establish the approved stream and owning bot."""


def side_chats_destination(client, origin, seat_tag_for, stream_id, topic_for):
    """Read current audience metadata with the already-pinned Operator client.

    The stream ID and participant-topic function come from existing dm_policy,
    not a new review-alert setting or a display-name lookup. No subscriptions
    are created here, and the Operator is never replaced by a seat's client.
    """
    if (
        not isinstance(origin, dict)
        or any(
            not isinstance(origin.get(k), str) or not origin[k]
            for k in ("seat", "platform", "verified_host", "profile_scope", "session")
        )
        or not re.fullmatch(r"[A-Z][A-Z0-9-]*", origin["seat"])
        or origin["seat"] == "OPERATOR"
        or type(stream_id) is not int
        or stream_id <= 0
    ):
        raise MessageDestinationUnresolved()
    subscriptions = client.get("users/me/subscriptions", {"include_subscribers": "true"})
    users = client.get("users")
    streams = [
        row
        for row in subscriptions.get("subscriptions", [])
        if isinstance(row, dict)
        and type(row.get("stream_id")) is int
        and row["stream_id"] == stream_id
        and not row.get("is_archived", False)
    ]
    bots = [
        row
        for row in users.get("members", [])
        if isinstance(row, dict)
        and row.get("is_bot") is True
        and row.get("is_active") is True
        and type(row.get("user_id")) is int
        and row["user_id"] > 0
        and seat_tag_for(row) == origin["seat"]
    ]
    if len(streams) != 1 or len(bots) != 1:
        raise MessageDestinationUnresolved()
    subscribers = streams[0].get("subscribers")
    bot_id = bots[0]["user_id"]
    if not isinstance(subscribers, list) or bot_id not in [
        uid for uid in subscribers if type(uid) is int
    ]:
        raise MessageDestinationUnresolved()
    return {
        "stream_id": stream_id,
        "bot_id": bot_id,
        "seat": origin["seat"],
        "topic": topic_for(["OPERATOR", origin["seat"]]),
    }


class OperatorMessages:
    def __init__(self, send, destination_for_origin):
        if not callable(send) or not callable(destination_for_origin):
            raise ValueError("operator-message-destination-source-required")
        self.send = send
        self.destination_for_origin = destination_for_origin

    def post(self, owner: dict, event: dict, link: str) -> int:
        origin = owner.get("_review_origin")
        try:
            destination = self.destination_for_origin(origin)
        except Exception:
            # All destination work is GET-only and precedes the send. Failures
            # are retry-safe, unlike an error after a messages POST starts.
            raise MessageDestinationUnresolved() from None
        repo, number = event["repo"], event["number"]
        if (
            not isinstance(repo, str)
            or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo)
            or type(number) is not int
            or number <= 0
        ):
            raise MessageDestinationUnresolved()
        # Only bounded factual status, never review bodies, titles or other
        # source-controlled prose. The existing exact native wake carries ID.
        # A failed check's NAME is workflow prose, so the CI line stays generic here and the
        # wake's own topic is where the runs are named.
        summary = {
            "review_submitted": "review completed",
            "review_completed": "review check completed",
            "findings_added": "new review findings",
            "merge_conflict": "merge conflict with the base branch",
            "check_failure": "a check failed on the current head",
            "changes_requested": "changes were requested",
            "ci_failed": "CI checks failed on the current head",
            "merge_blocked": "the PR is blocked from merging",
            "pr_comment_added": "a new conversation comment",
            "pr_comment_edited": "a conversation comment was edited",
            "issue_opened": "a new issue",
            "issue_comment_added": "a new issue comment",
            "issue_comment_edited": "an issue comment was edited",
        }.get(event.get("kind"))
        if summary is None:
            raise MessageDestinationUnresolved()
        old = " (against an older PR head)" if not event.get("head_matches") else ""
        params = {
            "type": "stream",
            "to": str(destination["stream_id"]),
            "topic": destination["topic"],
            "content": "@**%s|%s** %s#%s: %s%s"
            % (destination["seat"], destination["bot_id"], repo, number, summary, old),
        }
        if isinstance(link, str):
            url = urlsplit(link)
            # The canonical source path for the event's OWN subject:  an
            # issue event links /issues/<n>, everything else /pull/<n>.  A
            # link naming the other subject's path is not attached.
            leaf = "issues" if event.get("subject") == "issue" else "pull"
            if (
                url.scheme == "https"
                and url.netloc == "github.com"
                and url.path == "/%s/%s/%s" % (repo, leaf, number)
                and not url.query
                and not url.fragment
            ):
                params["content"] += "\n" + link
        result = self.send(params)
        message_id = result.get("id") if isinstance(result, dict) else None
        if type(message_id) is not int or not 0 < message_id <= 2**63 - 1:
            raise ValueError("operator-message-outcome-unknown")
        return message_id
