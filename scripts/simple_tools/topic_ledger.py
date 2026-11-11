"""The per-topic post ledger behind reply wakes (owner, Nov 10 2026):  who posted recently in each topic,
as which chat, and whom they tagged.

Owner rulings this exists for, in his words:  "if anything I'd want it to be more liberal with what it
considered a wake like a reply to a post that didn't have address to any chat thread", and "too many
wakes ais not my concern and I long for the day that I am annoyed by too many wakes".  The core case:
"if someone comments a reply to someone that tagged tehm".  Chat A (seat S, header letter AA) tags seat
T in a topic, T's chat answers in the topic without tagging anyone, and that answer wakes chat A.  The
router (`router.classify`, class `reply_to_chat`) asks this ledger whether seat S posted earlier in the
topic, and which chat it posted as.

No I/O and no clock:  the daemon feeds it every stream message (`record`) and the router reads it
(`reply_target`).  It is thread-safe (the router thread and the wake workers both read it) and bounded:
at most `max_topics` topics, least recently written first out, and at most `per_topic` posts in a topic,
oldest first out.  Direct messages never come here.  A restart empties it (it is memory only).

Order matters in one way only:  a message is judged against the posts strictly EARLIER than itself
(`before_id`).  Every seat's queue delivers the same message, and the queues are not in step, so by the
time one seat classifies message m the ledger may already hold m and even later ones.  Reading only ids
below m makes the answer the same whichever seat's queue got there first.
"""

from __future__ import annotations

import bisect
import collections
import threading
from collections.abc import Iterable
from typing import NamedTuple

#: How many topics the ledger keeps and how many recent posts it keeps in each.
MAX_TOPICS = 2000
POSTS_PER_TOPIC = 40


class TopicPost(NamedTuple):
    """One post as the ledger keeps it.  `seat` is the sender's seat tag when the sender is a fleet bot
    and "" for the owner or anyone else.  `letter` is the session letter of the post's own header
    (`[CLAUDE: AA.1.S 📁AFC]` gives `AA`), "" when the post has none.  `tagged` is the set of seats the
    post addressed:  the header's `→` list and the loud `@**Name**` mentions of fleet bots."""

    message_id: int
    sender_id: int | None
    seat: str
    letter: str
    tagged: frozenset[str]


class ReplyTarget(NamedTuple):
    """The chat an untagged post is answering:  seat S, the header `letter` of S's post it answers ("" when
    that post carried none) and that post's message id (for the wake record)."""

    seat: str
    letter: str
    message_id: int


class TopicLedger:
    def __init__(self, *, max_topics: int = MAX_TOPICS, per_topic: int = POSTS_PER_TOPIC) -> None:
        self.max_topics = max(1, int(max_topics))
        self.per_topic = max(1, int(per_topic))
        self._lock = threading.Lock()
        # topic key -> its recent posts, oldest first (ascending message id), least recently written first.
        self._topics: collections.OrderedDict[str, list[TopicPost]] = collections.OrderedDict()

    def record(self, key: str, post: TopicPost) -> bool:
        """Keep one post.  False when its message id is already there (a message reaches the daemon once
        per seat and again when an edit or a retry routes it, and it is kept once), or when it is older
        than every post of a full topic and so not worth keeping."""
        with self._lock:
            posts = self._topics.get(key)
            if posts is None:
                posts = self._topics[key] = []
            self._topics.move_to_end(key)
            at = bisect.bisect_left(posts, post.message_id, key=_message_id)
            if at < len(posts) and posts[at].message_id == post.message_id:
                return False
            posts.insert(at, post)
            kept = True
            if len(posts) > self.per_topic:
                kept = at >= len(posts) - self.per_topic
                del posts[: len(posts) - self.per_topic]
            while len(self._topics) > self.max_topics:
                self._topics.popitem(last=False)
            return kept

    def seen(self, key: str, message_id: int) -> bool:
        """Is this message already kept in the topic?  A cheap check, so the daemon builds a post only
        for the first copy of a message."""
        with self._lock:
            posts = self._topics.get(key)
            if not posts:
                return False
            at = bisect.bisect_left(posts, message_id, key=_message_id)
            return at < len(posts) and posts[at].message_id == message_id

    def posts(self, key: str, before_id: int | None = None) -> list[TopicPost]:
        """The topic's kept posts, oldest first, only those with an id below `before_id` when given."""
        with self._lock:
            found = self._topics.get(key) or []
            if before_id is None:
                return list(found)
            return found[: bisect.bisect_left(found, before_id, key=_message_id)]

    def reply_target(
        self,
        key: str,
        seat: str,
        user_id: int | None,
        before_id: int,
        sender_seat: str = "",
    ) -> ReplyTarget | None:
        """The chat of `seat` that a post with id `before_id`, by a sender of seat `sender_seat` ("" for
        the owner), is answering, or None when the seat has not posted in the topic before it.

        `seat`'s posts are those by its bot (`user_id`) and any other bot that signs as the seat.  The
        answer is the MOST RECENT such post, except that a post which tagged the sender's seat is
        preferred over a more recent one that did not:  the chat that called on the sender is the one the
        sender is most likely answering.  A post with no letter answers with "" (no letter)."""
        with self._lock:
            found = self._topics.get(key)
            if not found:
                return None
            earlier = found[: bisect.bisect_left(found, before_id, key=_message_id)]
        mine = [
            p
            for p in earlier
            if (user_id is not None and p.sender_id == user_id) or (seat and p.seat == seat)
        ]
        if not mine:
            return None
        pick = mine[-1]
        if sender_seat:
            for post in reversed(mine):
                if sender_seat in post.tagged:
                    pick = post
                    break
        return ReplyTarget(seat, pick.letter, pick.message_id)

    def rename(self, old_key: str, new_key: str) -> None:
        """A topic was renamed or moved:  its kept posts follow it.  Posts already under the new key are
        merged in (deduplicated by message id, bounded as ever)."""
        if old_key == new_key:
            return
        with self._lock:
            moved = self._topics.pop(old_key, None)
            if not moved:
                return
            merged = {p.message_id: p for p in self._topics.get(new_key) or []}
            merged.update((p.message_id, p) for p in moved)
            posts = sorted(merged.values(), key=_message_id)
            self._topics[new_key] = posts[-self.per_topic :]
            self._topics.move_to_end(new_key)
            while len(self._topics) > self.max_topics:
                self._topics.popitem(last=False)

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {
                "topics": len(self._topics),
                "posts": sum(len(v) for v in self._topics.values()),
            }

    def keys(self) -> Iterable[str]:
        with self._lock:
            return list(self._topics)


def _message_id(post: TopicPost) -> int:
    return post.message_id
