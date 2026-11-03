#!/usr/bin/env python3
"""The Operator's Zulip -> Linear living comments: the relay, hosted by the listener.

This is the standalone Zulip -> Linear relay (linear_relay.py) folded into the Operator, per
the owner's directive of 2026-11-03: the relay's own bot and process go away, and the job
becomes one of the Operator's. The behaviour contract is unchanged from the relay's owner
rulings (2026-10-21 and 2026-10-23), with the keying stated exactly as the owner put it in
2026-11-03:

  - Just one comment per Zulip topic.  The lifecycle is keyed by the topic (stream id +
    topic): for each Linear issue mentioned in that topic, exactly one living comment on
    that issue, created on the first mention in the topic and EDITED in place as the topic
    grows.  One topic naming two issues gets one comment on each issue; two topics naming
    the same issue get separate comments, one per topic.
  - The comment covers the WHOLE topic, not the thread from the first mention on (owner
    ruling, 2026-11-03): the poster often did not know an issue existed, did not find the
    related ones, or did not make one, so the issue's comment carries the full thread
    context.  On creation the worker is given a history reader (below) and pulls the
    topic's earlier messages into the entry before the create renders; on each edit the
    comment reflects the whole current thread.
  - Public streams only, as the relay ruled: never a direct message, never a private
    stream, and nothing at all until the public-stream set has loaded.
  - Update policy (inherited from the relay, unchanged): a create lands at once on the
    first mention; edits are batched to at most one per EDIT_INTERVAL (10 minutes) per
    comment; one poll cycle's writes share TICK_BUDGET so a slow Linear never stalls the
    listener; a failed write backs off exponentially and is retried on a later cycle; a
    create whose outcome is unknown is reconciled by its marker line before anything is
    created again, so a restart or a lost answer never stacks a second comment.  The
    (topic, issue) -> comment id mapping lives in the worker's ledger, a JSON file under
    the Operator's state root, written atomically under an flock.

What is different from the standalone relay:

  - Identity.  Writes go out under the Operator's own Linear credential (OPERATOR_LINEAR_TOKEN,
    below), never the relay app's token and never a seat's.  The comment footer names the
    Operator; the marker line is deliberately the relay's own (`<!-- zulip-relay -->`), so
    the hosted worker ADOPTS the comments the standalone relay already created instead of
    stacking new ones beside them.
  - Ingest.  No second Zulip bot and no second event queue: the daemon feeds the worker
    the stream messages it already routes (route_message) and the update_message events it
    already handles.  The worker deduplicates by message id, so a message routed for
    several seats counts once.  Nothing in this module ever posts to Zulip.
  - History.  The topic-history reader reads with the routing seat's existing daemon
    client (a read of a public stream); Linear writes are the only writes, and they use
    the Operator's token alone.

Credentials (environment, loaded by the container from Infisical, never logged):

    OPERATOR_LINEAR_TOKEN   a dedicated Linear credential for the Operator (see
                            docs/operator-linear-comments.md for the exact seam: it must
                            be created, it does not exist yet)
    OPERATOR_ZULIP_EMAIL    the Operator bot's own Zulip address; its posts are skipped,
                            as the relay skipped its own
    OPERATOR_LINEAR_STATE   optional ledger path override
    OPERATOR_LINEAR_DRY_RUN=1   log what would be written, write nothing to Linear, and
                            keep the ledger in a sibling dry-run file

Off unless [linear_comments] enabled is true in listener.toml AND OPERATOR_LINEAR_TOKEN
is set.  Python 3.11+, standard library only.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from . import linear_relay as lr
from .zulip import ZulipClient, realm_url

TOKEN_ENV = "OPERATOR_LINEAR_TOKEN"
STATE_ENV = "OPERATOR_LINEAR_STATE"
DRY_RUN_ENV = "OPERATOR_LINEAR_DRY_RUN"
OWN_EMAIL_ENV = "OPERATOR_ZULIP_EMAIL"
STATE_NAME = "linear-comments.json"
PAGE = 500  # messages per history page


def state_path(root: str | Path, env: Mapping[str, str], dry_run: bool) -> Path:
    """Where the hosted ledger lives: <state root>/operator/linear-comments.json, beside the
    session registry.  A dry run writes a sibling (`linear-comments.dry-run.json`), never
    the live ledger, because a dry run marks topics written that the live engine would
    then never create a comment for."""
    raw = (env.get(STATE_ENV) or "").strip()
    live = Path(raw).expanduser() if raw else Path(root) / "operator" / STATE_NAME
    if not live.name or live.name in (".", ".."):
        raise ValueError("%s must name a ledger file, not %s" % (STATE_ENV, live))
    if dry_run:
        return live.with_name(live.stem + ".dry-run" + (live.suffix or ".json"))
    return live


def fetch_topic_history(
    client: ZulipClient, stream_id: int, topic: str, *, max_messages: int = lr.MAX_BACKFILL
) -> list[dict[str, Any]]:
    """Every message in one public stream topic, oldest first, up to max_messages.

    Pages forward from the topic's start with the messages API, the same narrow shape the
    daemon's own backfill uses.  A page that is not a list of messages raises ValueError:
    the caller (the worker's backfill) treats any failure as "no history" and logs it, so
    a malformed answer can never half-fill a comment."""
    narrow = [
        {"operator": "channel", "operand": int(stream_id)},
        {"operator": "topic", "operand": topic},
    ]
    out: list[dict[str, Any]] = []
    anchor: Any = "oldest"
    while len(out) < max_messages:
        result = client.get(
            "messages",
            {
                "narrow": narrow,
                "anchor": anchor,
                "num_before": 0,
                "num_after": min(PAGE, max_messages - len(out)),
                "include_anchor": False,
                "apply_markdown": False,
            },
        )
        page = result.get("messages") if isinstance(result, Mapping) else None
        if not isinstance(page, list) or not all(isinstance(m, Mapping) for m in page):
            raise ValueError("a Zulip message page must be a list of messages")
        fresh = [m for m in page if isinstance(m.get("id"), int)]
        if not fresh:
            break
        out.extend(dict(m) for m in fresh)
        if len(page) < PAGE:
            break
        anchor = max(int(m["id"]) for m in fresh)
    return out[:max_messages]


class OperatorLinearComments:
    """The daemon's handle on the hosted worker: one lock, one ledger, one worker.

    The daemon calls feed_message for every stream message it routes, feed_event for every
    update_message event, reload_streams when the realm's streams change, and tick on its
    own cadence.  Every call is best-effort: an exception is logged by the daemon's caller
    and never reaches routing, and a refused Linear token stops the engine (the worker
    raises SystemExit for that, exactly as the standalone relay exits) instead of stopping
    a seat thread — after that, feeds are no-ops and the stop is in the log."""

    def __init__(
        self,
        *,
        realm: str,
        own_email: str,
        linear: lr.LinearClient,
        ledger: lr.Ledger,
        log: Callable[[str], None],
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.log = log
        self._lock = threading.Lock()
        self._client: ZulipClient | None = None
        self._reconciled = False
        self.stopped: str | None = None
        ledger.open()  # fails closed (SystemExit) on a held or unreadable ledger
        self.worker = lr.RelayWorker(
            realm,
            own_email,
            linear,
            ledger,
            lr.build_regex(lr.DEFAULT_TEAMS),
            clock=clock,
            monotonic=monotonic,
            log=log,
            history=self._history,
            byline="the Operator",
        )

    # -- plumbing ----------------------------------------------------------------------
    def _history(self, stream_id: int, topic: str) -> list[Mapping[str, Any]]:
        client = self._client
        if client is None:
            return []
        return fetch_topic_history(client, stream_id, topic)

    def _ensure_streams(self, client: ZulipClient | None) -> None:
        """Load the public-stream set on first use.  A load that fails leaves the worker
        knowing nothing (it writes and drops nothing), and the next feed tries again."""
        if client is not None:
            self._client = client
        if self.worker.public_streams is not None or self._client is None:
            return
        try:
            streams = lr.load_public_streams(self._client)
        except Exception as exc:  # noqa: BLE001 - a failed load is retried on the next feed
            self.log(
                "could not load the public-stream set (%s); commenting nothing until it loads"
                % type(exc).__name__
            )
            return
        self.worker.set_public_streams(streams)

    def reload_streams(self, client: ZulipClient | None) -> None:
        """Forget the public-stream set so the next feed reloads it (a stream was created,
        renamed or made private).  Mirrors the standalone relay's reload discipline."""
        with self._lock:
            if client is not None:
                self._client = client
            self.worker.set_public_streams(None)

    # -- daemon entry points -------------------------------------------------------------
    def feed_message(self, client: ZulipClient | None, message: Mapping[str, Any]) -> None:
        with self._lock:
            if self.stopped is not None:
                return
            self._ensure_streams(client)
            try:
                self.worker.start_cycle()
                self.worker.handle_event({"type": "message", "message": message})
                self.worker.tick()
            except SystemExit as exc:
                self._stop(exc)

    def feed_event(self, client: ZulipClient | None, event: Mapping[str, Any]) -> None:
        """An update_message event (a topic rename or a stream move): the worker carries the
        standing comment over a whole-topic move and never names what left for a private
        stream, exactly as the relay did."""
        with self._lock:
            if self.stopped is not None:
                return
            self._ensure_streams(client)
            try:
                self.worker.start_cycle()
                self.worker.handle_event(event)
                self.worker.tick()
            except SystemExit as exc:
                self._stop(exc)

    def tick(self, client: ZulipClient | None = None) -> None:
        """Flush what is dirty (edits whose batching interval has passed), and, the first
        time the public-stream set is loaded, adopt the comments of any creates whose
        answer was lost before a restart."""
        with self._lock:
            if self.stopped is not None:
                return
            self._ensure_streams(client)
            try:
                if not self._reconciled and self.worker.public_streams is not None:
                    self.worker.start_cycle()
                    self.worker.reconcile()
                    self._reconciled = True
                self.worker.tick()
            except SystemExit as exc:
                self._stop(exc)

    def _stop(self, exc: SystemExit) -> None:
        self.stopped = str(exc)
        self.log("the Operator's Linear comments are stopped: %s" % self.stopped)

    def close(self) -> None:
        """Release the ledger lock (a daemon rebuilding the engine on a credential change
        closes the old one first, so the new engine can open the same ledger file)."""
        with self._lock:
            self.worker.ledger.close()


def build(
    env: Mapping[str, str],
    root: str | Path,
    *,
    log: Callable[[str], None],
    clock: Callable[[], float] = time.time,
    monotonic: Callable[[], float] = time.monotonic,
) -> OperatorLinearComments | None:
    """The hosted engine, or None when OPERATOR_LINEAR_TOKEN is not set (the feature is
    off; the daemon logs the reason).  A ledger that refuses to open raises SystemExit,
    which the daemon logs as this feature failing closed — never as a daemon failure."""
    token = (env.get(TOKEN_ENV) or "").strip()
    if not token:
        return None
    dry = env.get(DRY_RUN_ENV) == "1"
    ledger = lr.Ledger(state_path(root, env, dry))
    linear = lr.LinearClient(token, dry_run=dry)
    own_email = (env.get(OWN_EMAIL_ENV) or "").strip().lower()
    return OperatorLinearComments(
        realm=realm_url(env),
        own_email=own_email,
        linear=linear,
        ledger=ledger,
        log=log,
        clock=clock,
        monotonic=monotonic,
    )
