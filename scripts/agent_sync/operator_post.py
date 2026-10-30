"""The Operator's voice for the daemon's own posts (owner ruling, Fri, Oct 30 2026).  Python 3.11+.

The rule, repeated for weeks and ruled again on Oct 30 2026: "anytime an agent's Zulip identity is
used it has to be something they actually thought about and said."  The daemon composes text on
a seat's behalf (a headless wake's reply, an escalation note, a quota notice, the held-wake digest,
a typing indicator) and no seat thought or said any of it, so none of it goes out under a seat's
bot.  It goes out as the pinned Operator bot, the deterministic service identity, with the seat's
tag in the text saying whose wake it reports.  On Oct 28 the owner had already ruled that the
MiniMax identity stops being used even where a headless wake used it, and this is the rest of that
ruling.

This module is the one place the daemon gets that client:

  - it is built the same way `operator_registration_ask.send_location_question` builds it: the
    credential is read from Fleet-OPS `/zulip_operator` through `service_identity.operator_credentials`
    (never from a seat's section, never from a file), and `users/me` has to be the pinned Operator
    bot, owned by the pinned owner (`service_identity.operator_bot`), before anything is sent;
  - it is built once and kept, because every post would otherwise cost a `fleet-secrets` call and a
    `users/me` read; a rejected credential (401 or 403) drops it, and so does a changed owner pin;
  - a build that fails is remembered for `RETRY_SECONDS`, so a vault outage costs one attempt per
    window and not one per post; every post in that window is refused at once with the reason;
  - there is NO fallback.  When the Operator cannot be reached `OperatorUnavailable` is raised and
    the caller logs a refusal and posts nothing.  It never reaches for a seat's client, which is
    the whole point.

The client is built WITHOUT `mark_operator_verified`: the owner-in-the-conversation guard (AFC-469)
applies to it exactly as it does to any bot, so a direct message must carry the owner, and the narrow
one-to-one status exemption (`ZulipClient.operator_status_dm`) is not opened for anything here.

Standard library only.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from . import service_identity as SI
from . import zulip as Z

#: What the Operator's posts and edits are called in logs, ledger rows and the standing-digest record.
VOICE = SI.OPERATOR

#: How long a failed build is remembered, in seconds of the monotonic clock.  The reads behind a
#: build can each wait on `fleet-secrets` (15 seconds), so retrying on every post would stall the
#: threads that post; two minutes keeps an outage to one attempt per window.
RETRY_SECONDS = 120.0

CODE_UNAVAILABLE = "operator_unavailable"
CODE_OWNER = "operator_owner_not_pinned"
CODE_SECRET = "secret_scanner"
#: Not a refusal: the client is being built in the background and the caller (which must not wait) asks again.
CODE_BUILDING = "operator_building"

Factory = Callable[[int], Any]


class OperatorUnavailable(Z.AgentSyncError):
    """The Operator client could not be built or verified, so nothing is posted.  `reason` carries a
    reason token or a short sentence, never a credential."""

    exit_code = 3

    def __init__(self, reason: str, *, code: str = CODE_UNAVAILABLE) -> None:
        super().__init__(reason)
        self.reason = reason
        self.code = code


class OperatorRefused(Z.AgentSyncError):
    """The text itself was refused before it left (the secret scanner)."""

    exit_code = 3

    def __init__(self, reason: str, *, code: str = CODE_SECRET) -> None:
        super().__init__(reason)
        self.reason = reason
        self.code = code


def pinned_user_id(env: Mapping[str, str]) -> int:
    """The Operator's pinned Zulip user id from the host's environment, or 0 when none is pinned.

    The same two settings `service_identity.operator_identity` reads (id and address); an unset or
    malformed pin is 0 and the caller treats it as "no Operator known", never as a guess."""
    try:
        return SI.operator_identity(env)[0]
    except Z.CredentialError:
        return 0


def default_factory(
    env: Mapping[str, str],
    home: str | Path,
    realm: str,
    *,
    timeout: float,
    sleep: Callable[[float], None],
) -> Factory:
    """The production factory: the Operator's credential from Fleet-OPS, checked against the realm.

    `ZULIP_SITE` defaults to the daemon's own realm, because `operator_credentials` takes the site
    from the environment and the Operator is never on a different server than the seats it speaks
    for; a credential for another host is refused by `verify_realm`."""

    def build(owner_user_id: int) -> Any:
        environment = dict(env)
        if not str(environment.get("ZULIP_SITE") or "").strip():
            environment["ZULIP_SITE"] = realm
        creds = SI.operator_credentials(environment, home=Path(home))
        Z.verify_realm(creds, realm)
        return Z.ZulipClient(
            creds, realm, timeout=timeout, sleep=sleep, owner_user_id=owner_user_id
        )

    return build


class _Flight:
    """One build of the Operator client in progress, joined by everyone who needs it.

    The thread that starts it runs it (or a background thread does, for a caller that must not
    wait); the others wait on `done` and read the outcome.  Nothing here is touched without
    `OperatorVoice._lock` except `done`, which is set last."""

    __slots__ = ("owner", "done", "client", "user_id", "error")

    def __init__(self, owner: int) -> None:
        self.owner = owner
        self.done = threading.Event()
        self.client: Any = None
        self.user_id = 0
        self.error: OperatorUnavailable | None = None


class OperatorVoice:
    """A lazily built, identity-verified, cached Operator client, with no fallback.

    `owner_user_id` is read on every call, because a reload can change the pin and the client's
    owner-in-the-conversation guard is built from it.  `factory` is injectable so a test hands it
    a client for the fake server; whatever it returns is still checked against the pinned Operator
    (`users/me`) before it is used.

    A build reads the vault (`fleet-secrets`, up to 15 seconds, longer in the daemon) and asks Zulip
    `users/me` (up to 30 seconds), so it NEVER runs under the lock: the lock only guards the cached
    state, and a build is single flight (the first caller starts it, the others join it).  A caller
    that must not wait, the router thread above all, passes `wait=False`: it gets the cached client,
    or `OperatorUnavailable` with `CODE_BUILDING` while a build runs in the background, and never
    stands in a build's way."""

    def __init__(
        self,
        *,
        env: Mapping[str, str],
        owner_user_id: Callable[[], int],
        factory: Factory,
        clock: Callable[[], float] = time.monotonic,
        retry_seconds: float = RETRY_SECONDS,
        log: Callable[..., None] | None = None,
        hide: Callable[[str, str], None] | None = None,
    ) -> None:
        self._env = dict(env)
        self._owner_user_id = owner_user_id
        self._factory = factory
        self._hide = hide
        self._clock = clock
        self._retry = float(retry_seconds)
        self._log = log
        self._lock = threading.Lock()
        self._client: Any = None
        self._user_id = 0
        self._owner = 0
        self._flight: _Flight | None = None
        self._failed_until = -1e18
        self._failure = ""

    # ---- the client ----------------------------------------------------------------------
    def client(self, *, wait: bool = True) -> Any:
        """The verified Operator client, or OperatorUnavailable.  Never another bot's client.

        With `wait=False` this never blocks on a build: a cached client is returned, a known
        failure is raised, and otherwise a build is started in the background (once) and
        OperatorUnavailable with CODE_BUILDING is raised, so the caller skips this time and asks
        again."""
        owner = self._owner_now()
        started_here = False
        with self._lock:
            if self._client is not None and self._owner == owner:
                return self._client
            self._client = None  # no client, or one built for another owner pin
            self._user_id = 0
            flight = self._flight
            if flight is not None and flight.owner != owner:
                flight = None  # the pin changed under a build: that build is not wanted any more
            if flight is None:
                if self._clock() < self._failed_until:
                    raise OperatorUnavailable(self._failure)
                flight = self._flight = _Flight(owner)
                started_here = True
        if not wait:
            if started_here:
                threading.Thread(
                    target=self._run_flight, args=(flight,), name="operator-voice-build", daemon=True
                ).start()
            raise OperatorUnavailable(
                "the Operator client is being built", code=CODE_BUILDING
            )
        if started_here:
            self._run_flight(flight)
        else:
            flight.done.wait()
        if flight.error is not None:
            raise OperatorUnavailable(flight.error.reason, code=flight.error.code)
        return flight.client

    def user_id(self, *, wait: bool = True) -> int:
        """The Operator's own Zulip user id (the client is built first, or `wait=False` is as
        for `client`)."""
        self.client(wait=wait)
        with self._lock:
            return self._user_id

    def warm(self) -> None:
        """Start building the client in the background if nothing has it and no failure is being
        waited out.  Never blocks, never raises: the daemon calls it at start, so the first digest
        on the router thread finds the client ready."""
        try:
            self.client(wait=False)
        except OperatorUnavailable:
            pass

    def known_unavailable(self) -> str | None:
        """The reason the Operator is known to be unreachable right now, or None.

        Pure state, no network and no thread: a cached client, or a build in progress, or nothing
        known yet, all answer None; only a recent failed build (inside its remembered window)
        answers with its reason.  It is what a check on the router thread may call."""
        owner = self._owner_now()
        with self._lock:
            if self._client is not None and self._owner == owner:
                return None
            if self._flight is not None:
                return None
            if self._clock() < self._failed_until:
                return self._failure
        return None

    def forget(self, client: Any) -> None:
        """Drop the cached client if it is this one (its credential was rejected)."""
        with self._lock:
            if self._client is client:
                self._client = None
                self._user_id = 0

    def _owner_now(self) -> int:
        try:
            owner = int(self._owner_user_id() or 0)
        except (TypeError, ValueError):
            owner = 0
        return max(0, owner)

    def _run_flight(self, flight: _Flight) -> None:
        """Do the build, outside the lock, then publish the client or record the failure under it."""
        client: Any = None
        user_id = 0
        error: OperatorUnavailable | None = None
        try:
            client, user_id = self._connect(flight.owner)
        except OperatorUnavailable as exc:
            error = exc
        except Exception as exc:  # noqa: BLE001 - whatever happens, the waiters are released
            error = OperatorUnavailable(
                "the Operator client could not be built: %s" % type(exc).__name__
            )
        failed_build = error is not None  # the build itself failed (not just a pin that moved)
        event = ""
        fields: dict[str, Any] = {}
        with self._lock:
            current = self._flight is flight
            if current:
                self._flight = None
            if error is None and not (current and self._owner_now() == flight.owner):
                # The owner pin changed (or a newer build replaced this one) while it ran: the client
                # was checked against a pin that is no longer the pin, so it is not published.
                error = OperatorUnavailable(
                    "the owner pin changed while the Operator client was being built",
                    code=CODE_OWNER,
                )
                client = None
            elif error is None:
                self._client, self._user_id, self._owner = client, user_id, flight.owner
                self._failure = ""
                self._failed_until = -1e18
                event, fields = "operator-voice-connected", {"operator_id": user_id}
            if failed_build and error is not None and current:
                self._failure = error.reason
                self._failed_until = self._clock() + self._retry
                event, fields = "operator-voice-unavailable", {
                    "code": error.code,
                    "reason": error.reason,
                }
            flight.client, flight.user_id, flight.error = client, user_id, error
        if event:
            self._write(event, **fields)
        flight.done.set()

    def _connect(self, owner: int) -> tuple[Any, int]:
        if owner <= 0:
            raise OperatorUnavailable(
                "the owner is not pinned, so the Operator bot cannot be checked",
                code=CODE_OWNER,
            )
        try:
            client = self._factory(owner)
        except Z.CredentialError as exc:
            raise OperatorUnavailable("the Operator credential is unavailable: %s" % exc) from None
        except Exception as exc:  # noqa: BLE001 - a build failure is a refusal, never a crash
            raise OperatorUnavailable(
                "the Operator client could not be built: %s" % type(exc).__name__
            ) from None
        creds = getattr(client, "creds", None)
        if self._hide is not None and creds is not None:
            # The key joins the values the daemon scrubs from its log and scans every post for.
            self._hide(str(creds.email), str(creds.key))
        try:
            me = client.get("users/me")
        except Z.AgentSyncError as exc:
            status = getattr(exc, "status", getattr(exc, "status_code", None))
            if status in (401, 403):
                raise OperatorUnavailable(
                    "the Operator credential (%s) was rejected (%s)"
                    % (getattr(getattr(client, "creds", None), "email", "?"), status)
                ) from None
            raise OperatorUnavailable(
                "the Operator identity could not be confirmed: %s" % type(exc).__name__
            ) from None
        except Exception as exc:  # noqa: BLE001
            raise OperatorUnavailable(
                "the Operator identity could not be confirmed: %s" % type(exc).__name__
            ) from None
        try:
            is_operator = isinstance(me, Mapping) and SI.operator_bot(me, self._env, owner)
        except Exception as exc:  # noqa: BLE001 - an unset pin is a refusal too
            raise OperatorUnavailable(
                "the Operator identity is not configured: %s" % type(exc).__name__
            ) from None
        if not is_operator:
            raise OperatorUnavailable("the authenticated bot is not the pinned Operator")
        return client, int(me["user_id"])

    def _write(self, event: str, **fields: Any) -> None:
        if self._log is None:
            return
        try:
            self._log(event, **fields)
        except Exception:  # noqa: BLE001 - a logging failure never changes a refusal
            pass

    # ---- the writes ----------------------------------------------------------------------
    def post(
        self, params: Mapping[str, Any], *, timeout: float | None = None, wait: bool = True
    ) -> dict[str, Any]:
        """POST messages as the Operator.  Raises OperatorUnavailable when there is no Operator
        (or, with `wait=False`, none ready yet) and whatever the client raises (DmGuardRefused,
        ApiError, NetworkError) otherwise."""
        client = self.client(wait=wait)
        try:
            if timeout is None:
                return client.post("messages", dict(params))
            return client.post("messages", dict(params), timeout=timeout)
        except Z.ApiError as exc:
            if exc.status in (401, 403):
                self.forget(client)
            raise

    def edit(self, message_id: int, content: str, *, wait: bool = True) -> dict[str, Any]:
        """PATCH one of the Operator's own messages (the standing held-wake digest).  A rejected
        credential (401 or 403) drops the cached client exactly as it does for a post."""
        client = self.client(wait=wait)
        try:
            return client.patch("messages/%d" % int(message_id), {"content": content})
        except Z.ApiError as exc:
            if exc.status in (401, 403):
                self.forget(client)
            raise
