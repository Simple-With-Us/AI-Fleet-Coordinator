"""The one place a URL is made.

Only the gatherer makes URLs, so the DM lint can check every link in a DM against the list the gatherer
wrote into the facts (`facts["urls"]`).  Nothing here takes a URL from outside text:

  * Linear issue links are kept only if they match the Linear issue shape, and are cut to the short form
    (workspace and identifier, no title slug);
  * pull request links are built from the repository name and number;
  * Zulip links are built from the realm, the channel id and a topic or message id;
  * the full-list link is a constant.
"""
from __future__ import annotations

import re
import urllib.parse

__all__ = ["REALM", "CHANNEL", "STREAM_ID", "FULL_LIST_URL", "MAC_FACTS_API_URL", "MAC_FACTS_RAW_URL", "UrlBook",
           "hash_part", "topic_url", "message_url", "pr_url", "linear_url"]

REALM = "https://simplewithus.zulipchat.com"
CHANNEL = "agent-sync"
STREAM_ID = 642232
FULL_LIST_URL = "https://github.com/Simple-With-Us/fleet-loops-data/blob/main/gb/full-list.md"
MAC_FACTS_API_URL = "https://api.github.com/repos/Simple-With-Us/fleet-loops-data/contents/gb/mac-facts.json"
MAC_FACTS_RAW_URL = "https://raw.githubusercontent.com/Simple-With-Us/fleet-loops-data/main/gb/mac-facts.json"

_LINEAR = re.compile(r"^https://linear\.app/([A-Za-z0-9._-]+)/issue/([A-Za-z][A-Za-z0-9]*-\d{1,7})(?:/[A-Za-z0-9._/-]*)?$")
_REPO = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")


def hash_part(text: str) -> str:
    """Zulip's URL-hash encoding:  percent-encode, then `.` for `%` (and `.2E` for a dot)."""
    return urllib.parse.quote(text, safe="").replace(".", "%2E").replace("%", ".")


def topic_url(topic: str) -> str:
    return "%s/#narrow/channel/%d-%s/topic/%s" % (REALM, STREAM_ID, CHANNEL, hash_part(topic))


def message_url(topic: str, message_id: int) -> str:
    return "%s/near/%d" % (topic_url(topic), int(message_id))


def pr_url(repo: str, number: int) -> str | None:
    if isinstance(repo, str) and _REPO.match(repo) and isinstance(number, int) and not isinstance(number, bool) and number > 0:
        return "https://github.com/%s/pull/%d" % (repo, number)
    return None


def linear_url(value: object) -> str | None:
    """The short canonical form `https://linear.app/<workspace>/issue/<ID>` (Linear fills the slug in itself):
    Linear's own links carry the whole title, and the digest has under 4,000 characters."""
    match = _LINEAR.match(value) if isinstance(value, str) else None
    return "https://linear.app/%s/issue/%s" % (match.group(1), match.group(2)) if match else None


class UrlBook:
    """Every URL the gatherer put into the facts, for the lint."""

    def __init__(self) -> None:
        self._seen: set[str] = set()

    def add(self, url: str | None) -> str | None:
        if url:
            self._seen.add(url)
        return url

    def sorted(self) -> list[str]:
        return sorted(self._seen)
