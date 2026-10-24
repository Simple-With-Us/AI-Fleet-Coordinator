"""A Zulip topic link, taken apart.

`needs-owner file --mirror-of LINK` names the OWNER topic a Grok Bot persona already opened.  Only a
link on this realm, in #Work-In-Progress, to a topic that starts `OWNER `, and that names its message
(`/near/<id>`), is accepted:  the id is what the link-back reply and the notice record use.
"""

from __future__ import annotations

import os
import re
import urllib.parse
from typing import NamedTuple

from .errors import Refusal

DEFAULT_REALM = "https://simplewithus.zulipchat.com"
DEFAULT_CHANNEL = "work-in-progress"  # the realm renamed #agent-sync;  AGENT_SYNC_CHANNEL overrides


def resolve_channel(environ: dict | None = None) -> str:
    """The service channel, casefolded so it compares with every casefolded name here.

    Read from the caller's own `environ` when one is supplied, so a caller (and its tests) controls
    the channel instead of the module's import-time snapshot of `os.environ`."""
    env = os.environ if environ is None else environ
    return (env.get("AGENT_SYNC_CHANNEL") or "").strip().casefold() or DEFAULT_CHANNEL


CHANNEL = resolve_channel()


class TopicLink(NamedTuple):
    url: str
    channel: str
    topic: str
    message_id: int


def realm_host(environ: dict | None = None) -> str:
    env = os.environ if environ is None else environ
    site = (env.get("AGENT_SYNC_REALM") or DEFAULT_REALM).strip().rstrip("/")
    if "://" not in site:
        site = "https://" + site
    return urllib.parse.urlsplit(site).netloc.lower()


def _decode(part: str) -> str:
    """Zulip's narrow encoding:  `.` where a URL has `%` (so `.20` is a space), `.2E` for a real dot."""
    return urllib.parse.unquote(re.sub(r"\.([0-9A-Fa-f]{2})", r"%\1", part.replace("%", "%25")))


def parse_topic_link(raw: str, environ: dict | None = None) -> TopicLink:
    link = str(raw or "").strip()
    parts = urllib.parse.urlsplit(link)
    if parts.scheme != "https" or parts.netloc.lower() != realm_host(environ):
        raise Refusal(
            "--mirror-of must be an https link on the fleet's Zulip realm (%s)"
            % realm_host(environ)
        )
    match = re.fullmatch(
        r"narrow/(?:channel|stream)/(?P<chan>[^/]+)/topic/(?P<topic>[^/]+)/near/(?P<id>[0-9]{1,12})",
        parts.fragment.strip("/"),
    )
    if not match:
        raise Refusal(
            "--mirror-of must look like .../#narrow/channel/<id>-work-in-progress/topic/<topic>/near/<message id>"
        )
    chan = _decode(match.group("chan"))
    chan = re.sub(r"^[0-9]+-", "", chan)
    topic = _decode(match.group("topic"))
    channel = resolve_channel(environ)
    if chan.casefold() != channel:
        raise Refusal("--mirror-of must be in #%s" % channel)
    if not topic.startswith("OWNER "):
        raise Refusal("--mirror-of must be an OWNER topic (its name starts with 'OWNER ')")
    return TopicLink(link, channel, topic, int(match.group("id")))


def _encode(part: str) -> str:
    """Zulip's narrow encoding:  the URL-encoded text with `.` for `%` (and `.2E` for a real dot), the inverse of `_decode`."""
    return urllib.parse.quote(part, safe="").replace(".", "%2E").replace("%", ".")


def make_topic_link(
    topic: str,
    message_id: int,
    *,
    stream_id: int | None = None,
    channel: str | None = None,
    environ: dict | None = None,
) -> str:
    """The link to message `message_id` in `topic` of the service channel on this realm, in the form
    `parse_topic_link` reads back (`.../#narrow/channel/<stream id>-work-in-progress/topic/<topic>/near/<id>`).
    Without the stream id the channel part is its name alone, which Zulip also resolves."""
    host = realm_host(environ)
    chan = channel if channel is not None else resolve_channel(environ)
    chan = "%d-%s" % (stream_id, chan) if isinstance(stream_id, int) and stream_id > 0 else chan
    return "https://%s/#narrow/channel/%s/topic/%s/near/%d" % (
        host,
        _encode(chan),
        _encode(topic),
        int(message_id),
    )
