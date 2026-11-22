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
  - The mapping outlives the ledger's bound (AFC-821).  The ledger keeps its 500 most
    recently touched entries.  A pruned entry that has a comment, or a create whose
    outcome is unknown, leaves its first message id and its comment id in the ledger's
    archive, under the same (issue, stream id, topic digest) key and with no text at all.
    The next message in that topic, whether or not it names the issue, restores the
    entry under its ORIGINAL marker, refills it from the topic's history, and EDITS the
    old comment instead of creating a second one.  When that history read fails, the
    record stays archived and the comment as it is, and a message a minute or more later
    tries again.  Archived records follow a whole-topic rename or move, and are dropped
    with a stream that is not public, as live entries are.  NOT guaranteed:  a lost
    ledger file still loses the whole mapping;  the archive is bounded too (20,000
    records and the ledger's 8 MB;  the oldest are dropped first, and the log says how
    many);  a rename or move made while the listener was down, or while its stream set
    was unloaded, is not followed, so a mention under the new name starts a second
    comment;  and a partial move of messages to a private stream leaves them listed in
    an archived comment until the topic's next message refills it.
  - A restart catches up (AFC-821).  The daemon's own backfill replays only direct
    messages, mentions, owner posts and leased or followed topics, so a plain message
    posted while the listener was down never reaches the worker, and the same holds for
    the gap while a seat's event queue registers again.  The worker is given a second
    reader (below) and closes that gap itself:  the first message that reaches a
    standing comment's topic after a restart, after the daemon reports a queue
    registered again (queue_registered), or after a message of the topic was dropped
    while the stream set was unloaded, reads the topic's messages newer than the newest
    one the comment has recorded, and records the missed ones before the live one.  Only
    a read that reaches the live message settles the topic:  a read cut short of it (at
    its 1000-message cap, or by a page Zulip repeated), or one that shows the live
    message was a replayed older one, is continued by the topic's next message, and
    three reads in a row that bring nothing new settle it, in the log.  NOT guaranteed:  a
    topic is caught up only when its next message arrives, so a topic that stays silent
    keeps its gap until then;  a read that fails is logged and tried again a minute or
    more later, from a point kept in memory only, so a restart before then loses that
    gap;  and a catch-up still open after 1000 further messages gives up, in the log.

What is different from the standalone relay:

  - Identity.  Writes go out under the owner's Linear developer token (a personal API
    key with Write permission) held in Infisical (below), never a seat's credential and
    never a baked token.  The comment footer names the
    Operator; the marker line is deliberately the relay's own (`<!-- zulip-relay -->`), so
    the hosted worker ADOPTS the comments the standalone relay already created instead of
    stacking new ones beside them.
  - Ingest.  No second Zulip bot and no second event queue: the daemon feeds the worker
    the stream messages it already routes (route_message) and the update_message events it
    already handles.  The worker deduplicates by message id, so a message routed for
    several seats counts once.  Nothing in this module ever posts to Zulip.
  - History.  The topic-history reader reads with the routing seat's existing daemon
    client (a read of a public stream); Linear writes are the only writes, and they use
    the Operator's token alone.  The catch-up reader is the same read started after a
    message id (fetch_topic_history with after_id), handed to the worker as
    history_since.  Each read, every page and retry included, has HISTORY_READ_BUDGET
    seconds, because it runs on the daemon's routing thread.

Credentials (never logged; the developer key lives in memory only):

    The writing credential is the owner's Linear developer token — a personal API key
    with Write permission — in Infisical (project AI Fleet Coordinator, prod, folder
    /linear) as LINEAR_OPERATOR_DEVELOPER_TOKEN.  The owner ruled this route on
    2026-11-03:  the lane originally minted tokens from the Operator app's OAuth client
    pair, but the app's registered scopes are create-only, so comment EDITS fail with
    "Invalid scope: write required";  a personal API key carries the owner's own write
    permission and needs no mint.  The daemon reaches the key through ONE scoped
    read-only Infisical service token (operator-linear-read;  registry:  SWU Fleet
    References, "Infisical Service Tokens"), never through a machine identity and
    never through a baked Linear token.  The service token itself arrives one of two
    ways:

    OPERATOR_LINEAR_INFISICAL_TOKEN   the service token, bound directly by the host as
                            a runtime-only variable;  wins when set
    [linear_comments] infisical_token_file   the path of a file holding the service
                            token (owner-only permissions, checked before use);
                            defaults to ~/workspace/.secrets/operator-linear-service-token

    At engine build (daemon start, and again whenever the service token changes) the
    key is fetched with the service token — after the token is verified against its
    own record:  read-only, one scope, exactly prod /linear in this project — and the
    key itself is what the relay's LinearClient writes with, sent as
    `Authorization: <key>` with no Bearer prefix (Linear's personal-key form;  there is
    no OAuth exchange).  A configured credential that is refused stops this feature
    (CredentialRefused, logged with its stage and status, never a value), never the
    daemon.

    OPERATOR_ZULIP_EMAIL    the Operator bot's own Zulip address; its posts are skipped,
                            as the relay skipped its own
    OPERATOR_LINEAR_STATE   optional ledger path override
    OPERATOR_LINEAR_DRY_RUN=1   log what would be written, write nothing to Linear, and
                            keep the ledger in a sibling dry-run file

Off unless [linear_comments] enabled is true in listener.toml AND a service token
resolves.  Python 3.11+, standard library only.
"""

from __future__ import annotations

import json
import os
import stat
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from . import linear_relay as lr
from .zulip import ZulipClient, _strip_quotes, realm_url

SERVICE_TOKEN_ENV = "OPERATOR_LINEAR_INFISICAL_TOKEN"
STATE_ENV = "OPERATOR_LINEAR_STATE"
DRY_RUN_ENV = "OPERATOR_LINEAR_DRY_RUN"
OWN_EMAIL_ENV = "OPERATOR_ZULIP_EMAIL"
STATE_NAME = "linear-comments.json"
PAGE = 500  # messages per history page
HISTORY_READ_BUDGET = 30.0  # seconds for one topic-history read, every page and retry included

# The Operator's Linear credential seam (docs/operator-linear-comments.md):  constants,
# never environment overrides, as in linear_project_linkifiers.py.
INFISICAL_API = "https://app.infisical.com/api"
INFISICAL_PROJECT_ID = "9bf7417a-fbbb-42ca-870c-2b45207233f5"  # AI Fleet Coordinator
INFISICAL_ENVIRONMENT = "prod"
INFISICAL_SECRET_PATH = "/linear"
API_KEY_NAME = "LINEAR_OPERATOR_DEVELOPER_TOKEN"
DEFAULT_SERVICE_TOKEN_FILE = "~/workspace/.secrets/operator-linear-service-token"


class CredentialRefused(Exception):
    """The Operator's Linear credential could not be loaded.  The message names only the
    stage and a status — never a value, a response body or a URL query."""

    def __init__(self, reason: str, message: str | None = None) -> None:
        self.reason = reason
        super().__init__(message or reason)


HttpFn = Callable[[str, str, dict[str, str] | None, bytes | None], tuple[int, Any]]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # never re-send a credential to another host
        return None


def _default_http(
    method: str, url: str, headers: dict[str, str] | None = None, body: bytes | None = None
) -> tuple[int, Any]:
    """(status, parsed JSON) for one request; an unparseable body reads as {}.  Mirrors
    linear_project_linkifiers._default_http, plus the no-redirect opener the rest of
    this package uses around credentials:  an HTTP error is a result, not a raise, and
    its body is parsed only as JSON, never logged."""
    req = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
    opener = urllib.request.build_opener(_NoRedirect())
    try:
        with opener.open(req, timeout=30) as resp:
            status = getattr(resp, "status", 200)
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read()
        except OSError:
            raw = b""
        try:
            return exc.code, json.loads(raw or b"{}")
        except ValueError:
            return exc.code, {}
    try:
        return status, json.loads(raw.decode("utf-8") if raw else "{}")
    except ValueError:
        return status, {}


def service_token(
    env: Mapping[str, str], token_file: str | None = None, *, store_first: bool = False
) -> str:
    """The scoped Infisical service token for the key fetch.

    The consumer's own store is the configured file (default DEFAULT_SERVICE_TOKEN_FILE).
    With ``store_first`` the store is the authority and the host-bound variable is only
    a copy of that same grant, used when the store has none;  without it the historical
    order holds and the host-bound variable wins.  "" when neither source yields one —
    the feature is simply unconfigured.  A file that exists but is group- or
    other-readable raises CredentialRefused:  a service token must not sit in a file
    other accounts can read."""
    bound = _strip_quotes(env.get(SERVICE_TOKEN_ENV) or "")
    if bound and not store_first:
        return bound
    path = Path(token_file or DEFAULT_SERVICE_TOKEN_FILE).expanduser()
    try:
        mode = stat.S_IMODE(os.stat(path).st_mode)
    except OSError:
        return bound
    if mode & 0o077:
        raise CredentialRefused(
            "token_file_permissions",
            "%s is group- or other-readable (mode %04o); the service token file "
            "must be owner-only" % (path, mode),
        )
    try:
        stored = _strip_quotes(path.read_text("utf-8"))
    except OSError:
        return bound
    return stored or bound


def _infisical_get(http: HttpFn, url: str, token: str, stage: str) -> Any:
    headers = {
        "Authorization": "Bearer " + token,
        "Accept": "application/json",
        "User-Agent": "operator-linear-comments",
    }
    try:
        status, payload = http("GET", url, headers, None)
    except OSError as exc:
        raise CredentialRefused("infisical_error", "%s: %s" % (stage, type(exc).__name__)) from None
    if status is None or not 200 <= int(status) < 300:
        raise CredentialRefused("infisical_error", "%s: HTTP %s" % (stage, status))
    return payload


def _verify_service_token(request: HttpFn, token: str) -> None:
    """Verify the service token against its own record — read-only, scoped to exactly
    prod /linear in the AI Fleet Coordinator project — and refuse anything wider
    before a single secret is read."""
    record = _infisical_get(
        request, INFISICAL_API + "/v2/service-token", token, "read the service-token record"
    )
    if not isinstance(record, dict):
        raise CredentialRefused("token_invalid", "the service-token record is not an object")
    permissions = record.get("permissions", [])
    if isinstance(permissions, dict):
        permissions = list(permissions)
    if not isinstance(permissions, list):
        permissions = []
    lowered = [str(p).lower() for p in permissions]
    if "write" in lowered or "read" not in lowered:
        raise CredentialRefused("token_not_read_only", "the service token is not read-only")
    scopes = record.get("scopes", record.get("scope", []))
    if isinstance(scopes, dict):
        scopes = [scopes]
    if not isinstance(scopes, list) or len(scopes) != 1:
        raise CredentialRefused(
            "token_scope_mismatch", "the service token must be scoped to exactly prod /linear"
        )
    scope = scopes[0]
    if (
        not isinstance(scope, dict)
        or scope.get("environment") != INFISICAL_ENVIRONMENT
        or scope.get("secretPath") != INFISICAL_SECRET_PATH
    ):
        raise CredentialRefused(
            "token_scope_mismatch", "the service token must be scoped to exactly prod /linear"
        )
    if record.get("workspace") != INFISICAL_PROJECT_ID:
        raise CredentialRefused(
            "token_scope_mismatch", "the service token belongs to another project"
        )


def fetch_api_key(token: str, *, http: HttpFn | None = None) -> str:
    """The owner's Linear developer token from Infisical, the ONLY source.  The service
    token is first verified against its own record — read-only, scoped to exactly
    prod /linear in the AI Fleet Coordinator project — and anything wider is refused
    before a single secret is read."""
    request = http or _default_http
    _verify_service_token(request, token)
    query = urllib.parse.urlencode(
        {
            "workspaceId": INFISICAL_PROJECT_ID,
            "environment": INFISICAL_ENVIRONMENT,
            "secretPath": INFISICAL_SECRET_PATH,
        }
    )
    raw = _infisical_get(
        request,
        INFISICAL_API + "/v3/secrets/raw?" + query,
        token,
        "read the Linear developer token",
    )
    items: Any = raw.get("secrets", raw) if isinstance(raw, dict) else raw
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            key = item.get("secretKey", item.get("key", item.get("name")))
            value = item.get("secretValue", item.get("value"))
            if key == API_KEY_NAME and isinstance(value, str) and value:
                return value
    raise CredentialRefused(
        "key_missing",
        "Infisical prod /linear is missing %s" % API_KEY_NAME,
    )


def load_api_key(
    env: Mapping[str, str],
    token_file: str | None = None,
    *,
    http: HttpFn | None = None,
    hide: Callable[[list[str]], None] | None = None,
) -> str | None:
    """The Linear developer token for the hosted engine, or None when no service token
    is configured (the feature is off).  Raises CredentialRefused when a service token
    IS configured but the key cannot be fetched.  The key is held in memory only —
    nothing here writes it anywhere;  `hide` (the daemon's log scrubber registry) is
    told the value the moment it is fetched, so no later log line can carry it."""
    # The store is the authority:  the host-bound variable is only a copy of that same
    # grant,  used when the store has none.  A hand-set value never replaces it.
    token = service_token(env, token_file, store_first=True)
    if not token:
        return None
    key = fetch_api_key(token, http=http)
    if hide is not None:
        hide([key])
    return key


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


def _page_message_id(item: Any) -> int | None:
    """The id one item of a Zulip message page is placed in its topic by, or None when it has
    none:  not a mapping, or an id that is not an integer (a bool is not one)."""
    if not isinstance(item, Mapping):
        return None
    ident = item.get("id")
    return ident if isinstance(ident, int) and not isinstance(ident, bool) else None


def fetch_topic_history(
    client: ZulipClient,
    stream_id: int,
    topic: str,
    *,
    max_messages: int = lr.MAX_BACKFILL,
    after_id: int | None = None,
    deadline: float | None = None,
) -> list[dict[str, Any]]:
    """Every message in one public stream topic, oldest first, up to max_messages.  With
    after_id, only the messages after that message id (the worker's catch-up after a
    restart reads from the newest message it has recorded); an after_id below 1 reads from
    the topic's start, as no after_id does.  With deadline (a time.monotonic() value), every
    page and every retry share it, and a read that runs past it raises.

    Pages forward from the topic's start, or from after_id, with the messages API, the
    same narrow shape the daemon's own backfill uses.  An answer that is not a list of
    messages raises ValueError, and so does a full page with no message the read can page
    on from:  the caller (the worker's backfill or catch-up) treats any failure as "no
    history" and logs it, so a malformed answer can never half-fill a comment.

    One bad item does not fail the read (Kody, PR 2298):  a failed read is tried again
    from the same point, so an item that never parses would hold the topic's catch-up, or
    an archived comment's restore, open for good.  A mapping with an integer id is passed
    on as it came, for the worker to check (linear_relay._by_id skips and counts one that
    is malformed);  an item without one (not a mapping, or no integer id) has no place in
    the topic, and is left out here.  Only the messages passed on count toward
    max_messages, so a read stopped at its cap still answers max_messages of them."""
    narrow = [
        {"operator": "channel", "operand": int(stream_id)},
        {"operator": "topic", "operand": topic},
    ]
    out: list[dict[str, Any]] = []
    seen: set[int] = set()
    # An id anchors the first page at that message's place, and include_anchor is false for it,
    # so the page starts with the message after it.  No id, or one below 1 (no message has
    # one: a catch-up whose first read failed starts after 0), asks for the topic's start by
    # the API's own name for it, never for a message 0, and with include_anchor true.  Zulip
    # places "oldest" before every message id, where including the anchor changes nothing; a
    # server that placed it at the first message instead still answers that message (Kody, PR
    # 2298).  The pages after the first are anchored at a message, and leave it out.
    anchor: Any = "oldest" if after_id is None or int(after_id) < 1 else int(after_id)
    limits: dict[str, Any] = {} if deadline is None else {"deadline": deadline}
    while len(out) < max_messages:
        wanted = min(PAGE, max_messages - len(out))
        result = client.get(
            "messages",
            {
                "narrow": narrow,
                "anchor": anchor,
                "num_before": 0,
                "num_after": wanted,
                "include_anchor": anchor == "oldest",
                "apply_markdown": False,
            },
            **limits,
        )
        page = result.get("messages") if isinstance(result, Mapping) else None
        if not isinstance(page, list):
            raise ValueError("a Zulip message page must be a list of messages")
        placed = [m for m in page if _page_message_id(m) is not None]
        if len(page) >= wanted and not placed:
            # A full page gives no id to anchor the next one on, and ending the read here
            # would answer the topic as ending here too.
            raise ValueError("a full Zulip message page carried no message id to page on from")
        # Only ids never seen count:  a page the API repeats (an anchor it did not
        # honour) must neither duplicate messages into the comment nor page forever.
        fresh = [m for m in placed if int(m["id"]) not in seen]
        if not fresh:
            break
        seen.update(int(m["id"]) for m in fresh)
        out.extend(dict(m) for m in fresh)
        if len(page) < wanted:
            break  # fewer than asked for:  the topic ends here
        new_anchor = max(int(m["id"]) for m in fresh)
        if new_anchor == anchor:
            break  # the anchor failed to advance:  the next page would repeat this one
        anchor = new_anchor
    return out[:max_messages]


class OperatorLinearComments:
    """The daemon's handle on the hosted worker: one lock, one ledger, one worker.

    The daemon calls feed_message for every stream message it routes, feed_event for every
    update_message event, reload_streams when the realm's streams change, queue_registered
    when a seat's event queue registers again, and tick on its own cadence.  Every call is
    best-effort: an exception is logged by the daemon's caller and never reaches routing,
    and a refused Linear token stops the engine (the worker
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
            history_since=self._history_since,
            byline="the Operator",
        )

    # -- plumbing ----------------------------------------------------------------------
    def _history(
        self, stream_id: int, topic: str, deadline: float | None = None
    ) -> list[Mapping[str, Any]]:
        """The whole topic, oldest first:  the worker's backfill reader, for an entry a
        message makes or restores.  With no client there is nothing to read with, and that
        is a failed read, not an empty topic:  an empty answer would let a restored entry
        rewrite its standing comment down to the live message.

        The deadline is the one the caller's read shares, and the Operator's own budget
        bounds the read as well, so a walk never gets more time out of a read than a read
        on its own gets (Kody, PR 2340)."""
        client = self._client
        if client is None:
            raise RuntimeError("no Zulip client to read the topic with")
        budget = time.monotonic() + HISTORY_READ_BUDGET
        return fetch_topic_history(
            client,
            stream_id,
            topic,
            deadline=budget if deadline is None else min(deadline, budget),
        )

    def _history_since(
        self,
        stream_id: int,
        topic: str,
        after_id: int,
        deadline: float | None = None,
    ) -> list[Mapping[str, Any]]:
        """The topic's messages after one message id, oldest first:  the worker's catch-up
        reader.  With no client there is nothing to read with, and that is a failed read,
        not an empty topic:  the worker logs it and a later message tries again, where an
        empty answer would mark the topic caught up.

        The deadline is the one the caller's walk shares, so a walk of several pages is
        bounded in time as a whole and not page by page (Kody, PR 2340).  The Operator's
        own budget bounds the read as well, so a walk never gets more time out of a read
        than a read on its own gets (Kody, PR 2340)."""
        client = self._client
        if client is None:
            raise RuntimeError("no Zulip client to read the topic with")
        budget = time.monotonic() + HISTORY_READ_BUDGET
        return fetch_topic_history(
            client,
            stream_id,
            topic,
            after_id=after_id,
            deadline=budget if deadline is None else min(deadline, budget),
        )

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

    def queue_registered(self) -> None:
        """A seat's event queue registered again (the daemon's connect, after a restart, an
        expiry, a dead queue or a clock jump).  What the realm got while the old queue was
        gone reaches the daemon only in part, so every standing comment's topic reads for
        it at its next message (the worker's reopen_catch_up, AFC-821).  Memory only:
        nothing is read, written or posted here."""
        with self._lock:
            if self.stopped is not None:
                return
            self.worker.reopen_catch_up()

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
    service_token_file: str | None = None,
    http: HttpFn | None = None,
    hide: Callable[[list[str]], None] | None = None,
) -> OperatorLinearComments | None:
    """The hosted engine, or None when no Infisical service token is configured (the
    feature is off; the daemon logs the reason).  The owner's Linear developer token is
    fetched here, at build, so a build is the one moment credentials move.  A
    configured credential that is refused raises CredentialRefused, and a ledger that
    refuses to open raises SystemExit:  the daemon logs both as this feature failing
    closed — never as a daemon failure."""
    token = load_api_key(env, service_token_file, http=http, hide=hide)
    if token is None:
        return None
    dry = env.get(DRY_RUN_ENV) == "1"
    ledger = lr.Ledger(state_path(root, env, dry))
    linear = lr.LinearClient(token, dry_run=dry, auth_scheme="api_key")
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
