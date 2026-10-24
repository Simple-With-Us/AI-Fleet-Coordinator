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

The indicator is cosmetic, so it can never fail a run, and it can never slow one down
either.  Every post happens on the signal's own worker thread, so a slow, stalled or
rate-limited typing endpoint never blocks the wake worker thread, and every post carries
a short timeout of its own rather than the client's default thirty seconds.  A posting
error is handed to the caller's error hook and the seat still answers.  A transient
network error does not end the refresh:  the loop retries, and it gives up only after a
run of consecutive failures, so one blip does not silently take the indicator down for
the rest of the run.  A hard ceiling bounds how long one indicator can live, so a hung
adapter cannot leave "<seat> is typing" up forever.

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

# The timeout on one typing post.  It is cosmetic, so it gets a short one:  the client's
# default of thirty seconds would let a stalled endpoint hold the worker thread for half a
# minute over a decoration.
POST_TIMEOUT = 3.0

# How often the worker thread checks whether it has been asked to stop.  The stop is
# posted by the worker itself, so this bounds how long the indicator can outlive the
# answer by.
TICK_SECONDS = 0.5

# How many consecutive failed posts end the refresh.  One blip is not a dead endpoint, so
# the loop keeps trying through it; a run of them means the endpoint is gone.
MAX_CONSECUTIVE_FAILURES = 5

# How long stop() waits for the worker to post the closing "stop".  The wait is bounded
# and small:  the reply is posted next and must not wait on the decoration.
STOP_JOIN_SECONDS = 1.0


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
    so the worker thread and the daemon's event thread never share a connection).  Every
    post is made by the worker thread, with its own short timeout, so the wake worker
    thread is never held up by the indicator.  The error hook is called with every
    failure; it never raises, and a failure never stops the seat from answering.
    """

    def __init__(
        self,
        client: Any,
        params: Mapping[str, Any],
        *,
        refresh: float = REFRESH_SECONDS,
        max_seconds: float = MAX_SECONDS,
        post_timeout: float = POST_TIMEOUT,
        on_error: Callable[[Exception], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.client = client
        self.params = dict(params)
        self.refresh = max(0.5, float(refresh))
        self.max_seconds = max(self.refresh, float(max_seconds))
        self.post_timeout = max(0.5, float(post_timeout))
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
        try:
            self.client.post("typing", params, timeout=self.post_timeout)
        except TypeError as exc:
            # A client without a timeout argument still gets the post:  the timeout is a
            # safeguard, not a requirement.
            if "timeout" not in str(exc):
                raise
            self.client.post("typing", params)

    def _fail(self, exc: Exception) -> None:
        if self.on_error is None:
            return
        try:
            self.on_error(exc)
        except Exception:  # a broken error hook must not break the run
            pass

    def _post_ok(self, op: str) -> bool:
        """One post.  False when it failed; the failure is reported and never raised."""
        try:
            self._post(op)
        except Exception as exc:
            self._fail(exc)
            return False
        return True

    # ---- life cycle ----------------------------------------------------------------------
    def start(self) -> bool:
        """Start the indicator.  The first "start" is posted by the worker thread, so the
        caller returns at once and a slow endpoint cannot delay the seat's answer."""
        with self._lock:
            if self.live:
                return True
            self.live = True
            self._stop.clear()
        thread = threading.Thread(target=self._run, name="typing-signal", daemon=True)
        self._thread = thread
        thread.start()
        return True

    def _run(self) -> None:
        """The worker:  post "start", keep it alive, and post "stop" when it is told to.

        A failed post is logged and the loop carries on.  The refresh is given up on only
        after a run of consecutive failures, because one transient error is a blip and not
        a dead endpoint, and the indicator must survive it.
        """
        deadline = self.clock() + self.max_seconds
        now = self.clock()
        failures = 0 if self._post_ok("start") else 1
        next_refresh = now + self.refresh
        while True:
            if self._stop.wait(TICK_SECONDS):
                break
            now = self.clock()
            if now >= deadline:
                break
            if now < next_refresh:
                continue
            if self._post_ok("start"):
                failures = 0
            else:
                failures += 1
                if failures >= MAX_CONSECUTIVE_FAILURES:
                    break
            next_refresh = now + self.refresh
        self._post_ok("stop")

    def stop(self) -> None:
        """Stop the refresh and post the closing "stop".  Safe to call more than once.

        The worker posts the closing "stop" itself, so the caller waits only a short,
        bounded moment for it and the reply is never held up by the indicator.
        """
        with self._lock:
            if not self.live:
                return
            self.live = False
        self._stop.set()
        thread = self._thread
        self._thread = None
        if thread is not None:
            thread.join(timeout=STOP_JOIN_SECONDS)
