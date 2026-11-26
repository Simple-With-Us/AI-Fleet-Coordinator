"""The wake-router client:  the exact simple-tools-wake/1 contract.

POST /wake/<SEAT> with the service's own bearer (NEVER a seat's credential,
never the listener's key:  deployment supplies this service's own accepted
credential, constrained to exact-session+generation wakes and receipts).
`require_session` carries the origin session as claimed at open time, with
the claimed letter and generation as constraints:  the router holds the wake
unless exactly that live registry row picks it up (no fall-through to another
chat, and no following a re-lettered or re-generated chat).  Delivery is
acknowledged by reading the router's durable receipt;  a receipt's
`delivered` is transport acknowledgement only.

wake_id is deterministic from the event key and epoch, so a resubmission
after a lost answer is idempotent:  the router answers the existing record.
A NEW wake id (next epoch) is minted only when the prior wake record is
certain-undelivered - never while its delivery remains uncertain.
"""

from __future__ import annotations

import hashlib
import json
import re
import urllib.error
import urllib.request

CONTRACT = "simple-tools-wake/1"
WAKE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{3,127}$")
LETTER_RE = re.compile(r"^[A-Z]{2}$")
PR_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#[0-9]{1,10}")
#: The require_issue pin shape, byte-identical to the router's own
#: REQUIRE_ISSUE_RE grammar (8-digit issue numbers, owner/name#number):
#: a separate pin from require_pr - an issue number is never a PR binding,
#: and the two fields are mutually exclusive on any wake.
REQUIRE_ISSUE_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}/[A-Za-z0-9][A-Za-z0-9._-]{0,99}#[1-9][0-9]{0,7}$"
)
MAX_BODY = 64 * 1024
TIMEOUT = 15

# Reasons the router answers when a required-session wake cannot pick its
# chat.  Any other pick reason for such a wake means the router does not
# implement require_session and MUST NOT be sent real wakes.
REQUIRED_SESSION_REASONS = (
    "required-session-not-live",
    "required-session-ambiguous",
    "required-letter-mismatch",
    "required-generation-mismatch",
)

#: Holds the origin chat may recover from:  the wake record is certainly
#: undelivered, so a fresh wake id (next epoch) may be minted for a retry.
REDRIVE_REASONS = (
    "required-session-not-live",
    "required-session-ambiguous",
    # The origin row did not bind this PR at delivery:  nothing was
    # delivered, so a bounded redrive (fresh epoch) covers the race where
    # the alert fired before the post-open binding landed.  Same
    # certain-undelivered reconciliation as the session holds;  uncertain
    # (delivered=null) stays unknown and is never redriven.
    "required-pr-unbound",
)


class WakeError(Exception):
    """A permanent refusal or contract problem:  retrying unchanged will not
    help (400/401/403, a malformed answer, a local validation failure)."""


class WakeRetryable(WakeError):
    """A transport failure or 5xx:  the wake may or may not have landed.
    The caller marks the event submit-pending and reconciles by receipt
    (same wake id), never by minting a new one."""


def wake_id_for(repo: str, number: int, kind: str, event_id: str, epoch: int = 0) -> str:
    digest = hashlib.sha256(
        ("%s#%d:%s:%s:e%d" % (repo, number, kind, event_id, epoch)).encode()
    ).hexdigest()
    return "ra-" + digest[:24]


def wake_id_for_issue(repo: str, number: int, kind: str, event_id: str, epoch: int = 0) -> str:
    """The wake id for an ISSUE-subject source event:  the qualified router's
    semantic hash carries an "issue:" prefix so an issue#N wake id can never
    collide with a PR#N one.  PR digests (wake_id_for) are byte-unchanged."""
    digest = hashlib.sha256(
        ("issue:" + "%s#%d:%s:%s:e%d" % (repo, number, kind, event_id, epoch)).encode()
    ).hexdigest()
    return "ra-" + digest[:24]


class WakeClient:
    def __init__(self, base_url: str, key_provider, *, opener=urllib.request.urlopen):
        self._base = base_url.rstrip("/")
        self._key_provider = key_provider
        self._opener = opener

    def _call(self, method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
        key = self._key_provider()
        if not key:
            raise WakeError("no wake-router key is configured for this service")
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        if body is not None and len(body) > MAX_BODY:
            raise WakeError("wake body too large")
        req = urllib.request.Request(self._base + path, data=body, method=method)
        req.add_header("Authorization", "Bearer " + key)
        if body is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with self._opener(req, timeout=TIMEOUT) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raw = exc.read() or b""
            try:
                return exc.code, json.loads(raw.decode("utf-8"))
            except (UnicodeError, ValueError):
                raise WakeRetryable("router answered HTTP %s unreadably" % exc.code) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise WakeRetryable("router unreachable: %s" % type(exc).__name__) from exc

    def submit(
        self,
        seat: str,
        wake_id: str,
        require_session: str,
        require_letter: str,
        require_generation: int,
        require_pr: str,
        topic: str,
        link: str,
        *,
        message_id: int = 0,
        source_event=None,
    ) -> dict:
        """One alert wake for exactly the claimed origin session.  The excerpt
        deliberately carries no session-shaped token:  fragment picking stays
        out of this path.  Session id, letter AND generation are all required
        (the host router auth overlay rejects the reserved ra- namespace
        without a positive generation), and require_pr pins the wake to the
        PR:  the router checks all of them against the live registry row,
        whose own bound_prs entry must contain this repo#number."""
        if not WAKE_ID_RE.fullmatch(wake_id):
            raise WakeError("bad wake_id")
        if not SESSION_ID_RE.fullmatch(require_session or ""):
            raise WakeError("require_session is not a session id shape")
        if not LETTER_RE.fullmatch(require_letter or ""):
            raise WakeError("require_letter is not a session letter (two capitals)")
        if type(require_generation) is not int or require_generation < 1:
            raise WakeError("require_generation must be a positive integer")
        if not PR_RE.fullmatch(require_pr or ""):
            raise WakeError("require_pr is not a repo#number shape")
        if type(message_id) is not int or not 0 <= message_id <= 2**63 - 1:
            raise WakeError("bad message_id")
        payload = {
            "contract": CONTRACT,
            "message_id": message_id,
            "seat": seat,
            "wake_id": wake_id,
            "require_session": require_session,
            "require_letter": require_letter,
            "require_generation": require_generation,
            "require_pr": require_pr,
            "excerpt": "review alert: %s" % topic,
            "channel": "review-alerts",
            "topic": topic,
            "zulip_link": link,
            "sender_full_name": "review-alerts",
            "dm": False,
        }
        if source_event is not None:
            payload["source_event"] = source_event
        status, answer = self._call("POST", "/wake/%s" % seat, payload)
        if status in (200, 202):
            return answer
        if status >= 500:
            raise WakeRetryable("wake submit: HTTP %s" % status)
        raise WakeError("wake refused: HTTP %s %s" % (status, answer.get("error") or ""))

    def submit_issue(
        self,
        seat: str,
        wake_id: str,
        require_session: str,
        require_letter: str,
        require_generation: int,
        require_issue: str,
        topic: str,
        link: str,
        *,
        message_id: int = 0,
        source_event=None,
    ) -> dict:
        """One alert wake for an ISSUE, pinned by require_issue instead of
        require_pr (the two are mutually exclusive and the payload carries
        no require_pr key at all):  the router checks the pin against the
        live registry row's bound_issues, never bound_prs.  Every other
        requirement (session, letter, positive generation, exact wake id)
        is identical to submit."""
        if not WAKE_ID_RE.fullmatch(wake_id):
            raise WakeError("bad wake_id")
        if not SESSION_ID_RE.fullmatch(require_session or ""):
            raise WakeError("require_session is not a session id shape")
        if not LETTER_RE.fullmatch(require_letter or ""):
            raise WakeError("require_letter is not a session letter (two capitals)")
        if type(require_generation) is not int or require_generation < 1:
            raise WakeError("require_generation must be a positive integer")
        if not REQUIRE_ISSUE_RE.fullmatch(require_issue or ""):
            raise WakeError("require_issue is not a repo#number shape")
        if type(message_id) is not int or not 0 <= message_id <= 2**63 - 1:
            raise WakeError("bad message_id")
        payload = {
            "contract": CONTRACT,
            "message_id": message_id,
            "seat": seat,
            "wake_id": wake_id,
            "require_session": require_session,
            "require_letter": require_letter,
            "require_generation": require_generation,
            "require_issue": require_issue,
            "excerpt": "review alert: %s" % topic,
            "channel": "review-alerts",
            "topic": topic,
            "zulip_link": link,
            "sender_full_name": "review-alerts",
            "dm": False,
        }
        if source_event is not None:
            payload["source_event"] = source_event
        status, answer = self._call("POST", "/wake/%s" % seat, payload)
        if status in (200, 202):
            return answer
        if status >= 500:
            raise WakeRetryable("wake submit: HTTP %s" % status)
        raise WakeError("wake refused: HTTP %s %s" % (status, answer.get("error") or ""))

    def test_submit_issue(
        self,
        seat: str,
        wake_id: str,
        require_session: str,
        require_letter: str = "ZZ",
        require_issue: str = "zz0probe/zz0probe#1",
        require_generation: int = 1,
    ) -> dict:
        """The require_issue `test` probe:  same diagnostics-only semantics
        as test_submit (no enqueue, no dedupe claim, zero effects).  Unlike
        the PR probe, the session named here is a REAL registered session
        (operator-supplied):  a session-first router must find it live
        before its issue binding is ever checked."""
        payload = {
            "contract": CONTRACT,
            "seat": seat,
            "wake_id": wake_id,
            "require_session": require_session,
            "require_letter": require_letter,
            "require_generation": require_generation,
            "require_issue": require_issue,
            "excerpt": "review-alerts router self-check",
            "channel": "review-alerts",
            "sender_full_name": "review-alerts",
            "dm": False,
            "test": True,
        }
        status, answer = self._call("POST", "/wake/%s" % seat, payload)
        if status != 200:
            raise WakeError("test issue wake refused: HTTP %s" % status)
        return answer

    def check_required_issue_support(
        self,
        seat: str,
        probe_id: str,
        *,
        session: str,
        letter: str,
        generation: int,
        require_issue: str,
    ) -> None:
        """Fail-closed rollout gate for ISSUE wakes:  prove the live router
        implements require_issue binding before any real issue wake is
        sent.  The probe names an EXACT LIVE registered session (operator-
        supplied:  session, letter and current generation) and an UNBOUND
        issue under test:true.  A capable session-first router passes the
        session/letter/generation checks and holds at the issue pin with
        the TYPED required-issue-unbound reason, an empty pick and
        delivered False;  an old router ignores the field and answers a
        legacy pick reason, which fails the gate.  A probe naming a
        nonexistent session can never work (a session-first router answers
        required-session-not-live before any issue check), so the target
        context is mandatory, never defaulted.  Zero drive calls either
        way."""
        if not SESSION_ID_RE.fullmatch(session or ""):
            raise WakeError("issue probe session is not a session id shape")
        if not LETTER_RE.fullmatch(letter or ""):
            raise WakeError("issue probe letter is not a session letter (two capitals)")
        if type(generation) is not int or generation < 1:
            raise WakeError("issue probe generation must be a positive integer")
        if not REQUIRE_ISSUE_RE.fullmatch(require_issue or ""):
            raise WakeError("issue probe target is not a repo#number shape")
        answer = self.test_submit_issue(
            seat,
            wake_id_for("probe", 0, "issuecheck", probe_id),
            session,
            letter,
            require_issue,
            generation,
        )
        if not isinstance(answer, dict) or not (
            answer.get("test") is True
            and answer.get("reason") == "required-issue-unbound"
            and not answer.get("picked")
            and answer.get("delivered") is False
        ):
            raise WakeError(
                "router lacks require_issue support (answered %r)" % (answer,)
            )

    def test_submit(
        self,
        seat: str,
        wake_id: str,
        require_session: str,
        require_letter: str = "ZZ",
        require_pr: str = "zz0probe/zz0probe#1",
    ) -> dict:
        """A `test` wake:  runs the pick, delivers nothing, reserves nothing
        (host-confirmed:  diagnostics only, no enqueue, no dedupe claim).
        Same scoped shape as a real alert wake:  the host overlay rejects the
        reserved review-alerts namespace without channel, ra- id, exact
        session/letter and a positive generation, so the probe carries all
        of them (generation 1 never matches a live row;  the probe session
        never exists, so the hold reason still proves the semantics)."""
        payload = {
            "contract": CONTRACT,
            "seat": seat,
            "wake_id": wake_id,
            "require_session": require_session,
            "require_letter": require_letter,
            "require_generation": 1,
            "require_pr": require_pr,
            "excerpt": "review-alerts router self-check",
            "channel": "review-alerts",
            "sender_full_name": "review-alerts",
            "dm": False,
            "test": True,
        }
        status, answer = self._call("POST", "/wake/%s" % seat, payload)
        if status != 200:
            raise WakeError("test wake refused: HTTP %s" % status)
        return answer

    def check_required_session_support(self, seat: str, probe_id: str) -> None:
        """Fail-closed rollout gate:  prove the live router implements
        require-session hold semantics AND require_pr validation before any
        real wake is sent.  A router without the session change ignores the
        field and answers with a legacy pick reason;  a router without the
        require_pr change accepts a malformed require_pr probe instead of
        refusing it.  This service refuses to run against either."""
        answer = self.test_submit(
            seat,
            wake_id_for("probe", 0, "selfcheck", probe_id),
            "zz0reviewalerts0probe0noresuchsession",
        )
        reason = str(answer.get("reason") or "")
        if not reason.startswith("required-session-"):
            raise WakeError(
                "router lacks require-session hold semantics (answered reason %r)" % reason
            )
        self.check_require_pr_support(seat, probe_id)

    def check_require_pr_support(self, seat: str, probe_id: str) -> None:
        """The require_pr half of the gate, on its own:  cheap enough to run
        every poll cycle, so a router rollback between cycles is detected
        before the cycle's wakes go out, not only at startup."""
        # The malformed shape must draw the SPECIFIC 400 "require_pr
        # malformed" refusal:  any other answer (a 200 from a router that
        # ignores the field, or a 400 from an unrelated validation) proves
        # nothing about require_pr enforcement.
        payload = {
            "contract": CONTRACT,
            "seat": seat,
            "wake_id": wake_id_for("probe", 0, "prcheck", probe_id),
            "require_session": "zz0reviewalerts0probe0noresuchsession",
            "require_letter": "ZZ",
            "require_generation": 1,
            "require_pr": "!!!",
            "excerpt": "review-alerts router self-check",
            "channel": "review-alerts",
            "sender_full_name": "review-alerts",
            "dm": False,
            "test": True,
        }
        status, answer = self._call("POST", "/wake/%s" % seat, payload)
        error = str(answer.get("error") or "") if isinstance(answer, dict) else ""
        if status != 400 or error != "require_pr malformed":
            raise WakeError(
                "router lacks require_pr validation (answered HTTP %s %r)" % (status, error or None)
            )

    def receipt(self, seat: str, wake_id: str) -> dict | None:
        status, answer = self._call("GET", "/receipt/%s/%s" % (seat, wake_id))
        if status == 404:
            return None
        if status >= 500:
            raise WakeRetryable("receipt read: HTTP %s" % status)
        if status != 200:
            raise WakeError("receipt read refused: HTTP %s" % status)
        return answer

    @staticmethod
    def receipt_terminal(receipt: dict) -> tuple[bool, str]:
        """(is_terminal, outcome):  outcome is delivered | unknown | held:<reason>.
        `delivered` True is transport acknowledgement.  `delivered` absent or
        None stays UNKNOWN regardless of any status wording:  the router's own
        record cannot prove the wake reached the chat, and no status label
        upgrades that."""
        if receipt.get("delivered") is True:
            return True, "delivered"
        if receipt.get("delivered") is False:
            # The failure identity rides in `code`:  a wake that failed at
            # delivery keeps its (successful) pick reason in `reason`, so
            # reading `reason` first would hide a delivery-time hold such as
            # required-pr-unbound and disable the redrive.  Pick-time holds
            # carry an empty code and still resolve through `reason`.
            reason = receipt.get("code") or receipt.get("reason") or "not-delivered"
            return True, "held:%s" % reason
        if receipt.get("status") == "accepted":
            return False, ""
        return True, "unknown"
