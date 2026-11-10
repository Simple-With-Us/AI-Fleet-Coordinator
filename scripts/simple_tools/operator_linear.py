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

  - Identity.  Writes go out under the Operator's own Linear app (the renamed relay app),
    reached through its OAuth client pair in Infisical (below), never a seat's credential
    and never a baked token.  The comment footer names the
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

Credentials (never logged; the pair and the minted token live in memory only):

    The writing credential is the Operator's own Linear app (the renamed relay app), an
    OAuth client pair in Infisical (project AI Fleet Coordinator, prod, folder /linear):
    LINEAR_OPERATOR_CLIENT_ID and LINEAR_OPERATOR_CLIENT_SECRET.  The daemon reaches
    the pair through ONE scoped read-only Infisical service token (operator-linear-read;
    registry:  SWU Fleet References, "Infisical Service Tokens"), never through a
    machine identity and never through a baked Linear token.  The service token itself
    arrives one of two ways:

    OPERATOR_LINEAR_INFISICAL_TOKEN   the service token, bound directly by the host as
                            a runtime-only variable;  wins when set
    [linear_comments] infisical_token_file   the path of a file holding the service
                            token (owner-only permissions, checked before use);
                            defaults to ~/workspace/.secrets/operator-linear-service-token

    At engine build (daemon start, and again whenever the service token changes) the
    pair is fetched with the service token — after the token is verified against its
    own record:  read-only, one scope, exactly prod /linear in this project — then
    exchanged at Linear's OAuth token endpoint (client_credentials, the app's own
    registered scopes), and the minted access token is what the relay's LinearClient
    writes with.  A configured credential that is refused stops this feature
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

# The Operator's Linear credential seam (docs/operator-linear-comments.md):  constants,
# never environment overrides, as in linear_project_linkifiers.py.
INFISICAL_API = "https://app.infisical.com/api"
INFISICAL_PROJECT_ID = "9bf7417a-fbbb-42ca-870c-2b45207233f5"  # AI Fleet Coordinator
INFISICAL_ENVIRONMENT = "prod"
INFISICAL_SECRET_PATH = "/linear"
CLIENT_ID_NAME = "LINEAR_OPERATOR_CLIENT_ID"
CLIENT_SECRET_NAME = "LINEAR_OPERATOR_CLIENT_SECRET"
PAIR_NAMES = (CLIENT_ID_NAME, CLIENT_SECRET_NAME)
DEFAULT_SERVICE_TOKEN_FILE = "~/workspace/.secrets/operator-linear-service-token"
LINEAR_TOKEN_URL = "https://api.linear.app/oauth/token"
# The Operator app's own registered scopes.  Asking Linear for a different scope string
# revokes every token the app holds (the linear-board rule), so this is a constant.
LINEAR_MINT_SCOPE = "read,comments:create"


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


def service_token(env: Mapping[str, str], token_file: str | None = None) -> str:
    """The scoped Infisical service token for the pair fetch:  the host-bound variable
    wins;  else the configured file (default DEFAULT_SERVICE_TOKEN_FILE).  "" when
    neither source yields one — the feature is simply unconfigured.  A file that
    exists but is group- or other-readable raises CredentialRefused:  a service token
    must not sit in a file other accounts can read."""
    bound = _strip_quotes(env.get(SERVICE_TOKEN_ENV) or "")
    if bound:
        return bound
    path = Path(token_file or DEFAULT_SERVICE_TOKEN_FILE).expanduser()
    try:
        mode = stat.S_IMODE(os.stat(path).st_mode)
    except OSError:
        return ""
    if mode & 0o077:
        raise CredentialRefused(
            "token_file_permissions",
            "%s is group- or other-readable (mode %04o); the service token file "
            "must be owner-only" % (path, mode),
        )
    try:
        return _strip_quotes(path.read_text("utf-8"))
    except OSError:
        return ""


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


def fetch_client_pair(token: str, *, http: HttpFn | None = None) -> tuple[str, str]:
    """The Operator's Linear OAuth client pair from Infisical, the ONLY source.  The
    service token is first verified against its own record — read-only, scoped to
    exactly prod /linear in the AI Fleet Coordinator project — and anything wider is
    refused before a single secret is read."""
    request = http or _default_http
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
    query = urllib.parse.urlencode(
        {
            "workspaceId": INFISICAL_PROJECT_ID,
            "environment": INFISICAL_ENVIRONMENT,
            "secretPath": INFISICAL_SECRET_PATH,
        }
    )
    raw = _infisical_get(
        request, INFISICAL_API + "/v3/secrets/raw?" + query, token, "read the Linear client pair"
    )
    items: Any = raw.get("secrets", raw) if isinstance(raw, dict) else raw
    found: dict[str, str] = {}
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            key = item.get("secretKey", item.get("key", item.get("name")))
            value = item.get("secretValue", item.get("value"))
            if isinstance(key, str) and isinstance(value, str) and key in PAIR_NAMES:
                found[key] = value
    missing = [name for name in PAIR_NAMES if not found.get(name)]
    if missing:
        raise CredentialRefused(
            "pair_incomplete",
            "Infisical prod /linear is missing %s" % " and ".join(missing),
        )
    return found[CLIENT_ID_NAME], found[CLIENT_SECRET_NAME]


def mint_access_token(client_id: str, client_secret: str, *, http: HttpFn | None = None) -> str:
    """Exchange the pair at Linear's OAuth token endpoint (client_credentials, the app's
    own registered scopes — a different scope string would revoke the app's other
    tokens).  Only the access token comes back;  a refusal names the HTTP status and
    nothing of Linear's answer."""
    request = http or _default_http
    body = urllib.parse.urlencode(
        {
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
            "scope": LINEAR_MINT_SCOPE,
        }
    ).encode()
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": "operator-linear-comments",
    }
    try:
        status, payload = request("POST", LINEAR_TOKEN_URL, headers, body)
    except OSError as exc:
        raise CredentialRefused(
            "linear_mint_failed",
            "minting the Operator's Linear token: %s" % type(exc).__name__,
        ) from None
    if (
        status is None
        or not 200 <= int(status) < 300
        or not isinstance(payload, dict)
        or not payload.get("access_token")
    ):
        raise CredentialRefused(
            "linear_mint_failed", "minting the Operator's Linear token: HTTP %s" % status
        )
    return str(payload["access_token"])


def load_access_token(
    env: Mapping[str, str], token_file: str | None = None, *, http: HttpFn | None = None
) -> str | None:
    """The minted Linear access token for the hosted engine, or None when no service
    token is configured (the feature is off).  Raises CredentialRefused when a service
    token IS configured but the pair cannot be fetched or exchanged.  The pair and the
    token are held in memory only — nothing here writes either anywhere."""
    token = service_token(env, token_file)
    if not token:
        return None
    client_id, client_secret = fetch_client_pair(token, http=http)
    return mint_access_token(client_id, client_secret, http=http)


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
    service_token_file: str | None = None,
    http: HttpFn | None = None,
) -> OperatorLinearComments | None:
    """The hosted engine, or None when no Infisical service token is configured (the
    feature is off; the daemon logs the reason).  The Operator's Linear pair is fetched
    and exchanged here, at build, so a build is the one moment credentials move.  A
    configured credential that is refused raises CredentialRefused, and a ledger that
    refuses to open raises SystemExit:  the daemon logs both as this feature failing
    closed — never as a daemon failure."""
    token = load_access_token(env, service_token_file, http=http)
    if token is None:
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
