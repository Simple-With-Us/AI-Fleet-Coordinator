"""Native Codex thread queue transport for a host that runs the Codex app-server daemon.

The server has no Codex Desktop app.  Its live chats live in the long-lived app-server
daemon, and the daemon is the only process on this host that knows which thread is loaded
right now:  a one-shot app-server of our own reports every thread as `notLoaded`, the live
one included, so its own status proves nothing.  The queue is a shared store, so the queue
calls still go through a one-shot app-server, exactly as they do on the Mac.

The two never mix:  the daemon says which thread has a live owner, and the one-shot
app-server carries the queue.  The daemon refuses the queue calls, and the one-shot
app-server cannot say which thread is loaded, so each is asked only what it knows.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Mapping
from typing import Any

from . import codex_chat_transport as CT
from . import platform_letter_codex as PLC

#: How long one daemon answer stands for the next caller.  A fresh probe skips the cache.
STATUS_TTL = 5.0
#: How many thread answers the cache holds at once, so a long-lived router does not grow
#: one permanent entry per thread it has ever been asked about.
STATUS_MAX_ENTRIES = 512
#: The whole daemon conversation, connect and answer together.
DEADLINE = 5.0
#: The status the daemon reports for a thread no live owner holds.
NOT_LOADED = "notLoaded"


def daemon_status(
    env: Mapping[str, str],
    thread_id: str,
    *,
    deadline: float = DEADLINE,
    opener: Callable[..., Any] | None = None,
) -> str:
    """The daemon's own status for one thread (`idle`, `notLoaded`, ...), or "" for no answer.

    A host with no daemon, a daemon that cannot be reached, and an answer that names no
    status all leave "" here:  the proof is absent, never a guess.
    """
    if not isinstance(thread_id, str) or not thread_id:
        return ""
    if not PLC.daemon_socket(env):
        # No daemon on this host, so there is no live owner to know about:  ask nothing.
        return ""
    open_rpc = opener or PLC.open_rpc
    try:
        with open_rpc(env, time.monotonic() + deadline, daemon=True) as rpc:
            result = rpc.call("thread/read", {"threadId": thread_id, "includeTurns": False})
    except Exception:  # noqa: BLE001 - an unreadable daemon is no proof either way
        return ""
    thread = result.get("thread") if isinstance(result, Mapping) else None
    status = thread.get("status") if isinstance(thread, Mapping) else None
    kind = status.get("type") if isinstance(status, Mapping) else None
    return kind if isinstance(kind, str) else ""


class DaemonStatus:
    """The daemon's answer about which thread is loaded right now, kept for a few seconds.

    One answer serves every caller for `ttl` seconds, so a delivery attempt does not spend
    a daemon conversation per check.  A failed probe is cached as no answer for the same
    time, so a broken daemon is asked once and not once per caller, and it never
    authorizes a delivery.  Expired answers are dropped when the next one is stored, and
    the cache holds at most `max_entries` threads.
    """

    def __init__(
        self,
        env: Mapping[str, str],
        *,
        ttl: float = STATUS_TTL,
        opener: Callable[..., Any] | None = None,
        max_entries: int = STATUS_MAX_ENTRIES,
    ):
        self.env = env
        self.ttl = ttl
        self.opener = opener
        self._max_entries = max(1, max_entries)
        self._cache: dict[str, tuple[float, str]] = {}

    def status(self, thread_id: str, *, fresh: bool = False) -> str:
        """The daemon's status for one thread, or "" when it cannot say."""
        now = time.monotonic()
        if not fresh:
            cached = self._cache.get(thread_id)
            if cached is not None and now < cached[0]:
                return cached[1]
        answer = daemon_status(self.env, thread_id, opener=self.opener)
        now = time.monotonic()
        # An expired answer proves nothing, so it goes first.  What is left is bounded, so a
        # long-lived router holds the newest answers and not one entry per thread ever asked.
        for key in [key for key, kept in self._cache.items() if now >= kept[0]]:
            del self._cache[key]
        self._cache.pop(thread_id, None)
        while len(self._cache) >= self._max_entries:
            self._cache.pop(next(iter(self._cache)))
        self._cache[thread_id] = (now + self.ttl, answer)
        return answer

    def loaded(self, thread_id: str, *, fresh: bool = False) -> bool:
        """Whether the daemon says this thread has a live owner right now."""
        answer = self.status(thread_id, fresh=fresh)
        return bool(answer) and answer != NOT_LOADED


class CodexServerChatTransport(CT.CodexChatTransport):
    """The queue transport for a host whose live chats live in the app-server daemon.

    The queue calls are the ones the Mac makes, against the same shared store.  Only the
    proof that a thread has a live owner is different, because the daemon is what knows.
    """

    def __init__(
        self,
        *,
        env: Mapping[str, str] | None = None,
        home: str | None = None,
        rpc_factory: Callable[[], Any] | None = None,
        status: DaemonStatus | None = None,
    ):
        self.env = dict(env if env is not None else os.environ)
        super().__init__(
            home=home or self.env.get("HOME") or None,
            binary=PLC.codex_binary(self.env),
            rpc_factory=rpc_factory,
        )
        if rpc_factory is None:
            # The one-shot app-server of our own, with the profile this seat reads.
            child_env = {**self.env}
            home_path = self.home

            def factory() -> Any:
                return CT.RPC(
                    self.binary, env={**child_env, "CODEX_HOME": str(home_path / ".codex")}
                )

            self.rpc_factory = factory
        self.status = status or DaemonStatus(self.env)

    def _is_loaded(self, thread: Any, *, fresh: bool = False) -> bool:
        thread_id = thread.get("id") if isinstance(thread, Mapping) else None
        if not isinstance(thread_id, str) or not thread_id:
            return False
        return self.status.loaded(thread_id, fresh=fresh)
