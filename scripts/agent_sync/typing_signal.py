"""The seat's typing indicator:  Zulip shows "<seat> is typing" while the seat thinks.

A wake run is silent for its whole length.  The daemon starts the adapter, the adapter
takes 30 to 50 seconds to answer, and the reply is posted only after the model has
finished, so the person who woke the seat watches an empty topic for the entire run
(owner ask, Oct 23, 2026).  The fleet already ships a typing op (POST /typing, reached
through the simple-chat CLI and the zulip_typing MCP tool), but nothing called it.

One notification is not enough.  Zulip's protocol expires a typing indicator when no new
"start" arrives, and the period is a SERVER setting, not a client one, so the indicator
would be gone long before the reply.  The signal therefore re-posts "start" on a short
interval until the run ends, and always posts "stop" when it does.

The indicator is cosmetic, so it can never fail a run.  A posting error is handed to the
caller's error hook and the seat still answers.  The same is true of the refresh thread:
it dies on the first error instead of retrying forever, and a hard ceiling bounds how long
one indicator can live, so a hung adapter cannot leave "<seat> is typing" up forever.

The target is the conversation the wake answers in:  the channel topic for a stream wake,
the direct-message partners for a private one.  A wake with no single conversation to type
in (a group direct message with more partners than Zulip accepts on one notification, a
stream wake with no channel id) has no target and simply does not show an indicator.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from typing import Any

# How often the indicator is refreshed while the seat thinks.  Zulip's own client uses the
# server's server_typing_started_wait_period_milliseconds (10 seconds upstream) as its
# refresh interval, and the indicator expires at server_typing_started_expiry_period_
# milliseconds (15 seconds upstream).  A refresh well inside the shortest plausible expiry
# keeps the indicator up for the whole run.  The server value is used instead when the
# register response carries one.
REFRESH_SECONDS = 5.0

# The ceiling on one indicator's life.  A run that never stops (a hung adapter, a seat that
# was reloaded mid-run) must not leave the indicator up forever.
MAX_SECONDS = 900.0

# Zulip accepts at most this many recipients on one direct typing notification.
DIRECT_LIMIT = 8


def target(pending: Any, me_user_id: int | None) -> dict[str, Any] | None:
    """The typing parameters for the conversation a wake answers in.

    None when the wake has no single conversation to type in:  a private wake with no
    partner besides the seat itself, a private wake with more partners than Zulip accepts
    on one notification, or a stream wake with no channel id or no topic.
    """
    kind = str(getattr(pending, "type", "") or "")
    if kind == "private":
        others = [
            int(uid)
            for uid in (getattr(pending, "recipients", None) or [])
            if isinstance(uid, int) and not isinstance(uid, bool) and uid != me_user_id
        ]
        if not others or len(others) > DIRECT_LIMIT:
            return None
        return {"type": "direct", "to": others}
    stream_id = getattr(pending, "stream_id", None)
    topic = str(getattr(pending, "topic", "") or "")
    if not isinstance(stream_id, int) or isinstance(stream_id, bool) or not topic:
        return None
    return {"type": "stream", "stream_id": stream_id, "topic": topic}


class TypingSignal:
    """One seat's typing indicator for one conversation.

    The client is the seat's own Zulip client (each request builds its own urllib objects,
    so the refresh thread and the daemon's event thread never share a connection).  The
    error hook is called with every failure; it never raises, and a failure never stops
    the seat from answering.
    """

    def __init__(
        self,
        client: Any,
        params: Mapping[str, Any],
        *,
        refresh: float = REFRESH_SECONDS,
        max_seconds: float = MAX_SECONDS,
        on_error: Callable[[Exception], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.client = client
        self.params = dict(params)
        self.refresh = max(0.5, float(refresh))
        self.max_seconds = max(self.refresh, float(max_seconds))
        self.on_error = on_error
        self.clock = clock
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self.live = False

    # ---- posting -------------------------------------------------------------------------
    def _post(self, op: str) -> None:
        params = dict(self.params)
        params["op"] = op
        self.client.post("typing", params)

    def _fail(self, exc: Exception) -> None:
        if self.on_error is None:
            return
        try:
            self.on_error(exc)
        except Exception:  # a broken error hook must not break the run
            pass

    # ---- life cycle ----------------------------------------------------------------------
    def start(self) -> bool:
        """Post the first "start" and start the refresh.  False when the first post failed."""
        with self._lock:
            if self.live:
                return True
            self.live = True
        try:
            self._post("start")
        except Exception as exc:
            with self._lock:
                self.live = False
            self._fail(exc)
            return False
        thread = threading.Thread(target=self._refresh, name="typing-signal", daemon=True)
        self._thread = thread
        thread.start()
        return True

    def _refresh(self) -> None:
        deadline = self.clock() + self.max_seconds
        while not self._stop.wait(self.refresh):
            if self.clock() >= deadline:
                break
            try:
                self._post("start")
            except Exception as exc:
                self._fail(exc)
                break

    def stop(self) -> None:
        """Stop the refresh and post the closing "stop".  Safe to call more than once."""
        with self._lock:
            if not self.live:
                return
            self.live = False
        self._stop.set()
        thread = self._thread
        self._thread = None
        if thread is not None:
            thread.join(timeout=2.0)
        try:
            self._post("stop")
        except Exception as exc:
            self._fail(exc)
