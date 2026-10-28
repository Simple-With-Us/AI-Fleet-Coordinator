"""Announcing an item:  the filing seat's bot opens the OWNER topic that mentions Jay.

`notify` runs right after an item is filed (or adopted), as the filing seat:

    simple-chat post --as SEAT --session needsown --json --topic "OWNER <ID> <subject>" --to-owner -- -

with the message on standard input.  The mention comes from the owner id pinned in listener.toml
(`--to-owner`), never from a name.  On success a comment `[needs-owner] notified msg <id>` records the
message.  When the post fails or the seat has no bot credential the item gets `[needs-owner]
notify_pending` and CLAUDE's fallback notifier (C6-2) sends it.  A mirrored item (`--mirror-of`) has no
post of its own:  the Grok Bot persona's topic is the notice, so its message id is recorded and the
Linear link is replied in that topic.

A post that may or may not have gone through (simple-chat exit 5 or 6, or a timeout) is never retried
blind:  the message carries a reference line (the item's uid), and the topic is read for it first.  Only
`find_notice` reads it, and the C6-2 notifier imports that one function:  it takes a message only from the
bot of the filing seat (or CLAUDE, whose fallback notifier posts for any seat), only when the reference is
a line of its own that is not quoted, and then the oldest such message, so a person's quote-and-reply or
a peer who copies the (public) uid can never become the notice.

A board write is classified before anything is done with it.  `ok` is the comment on the issue.  `retryable`
means the board decided before it wrote anything and a later run may write the record again (exit 3, the
claim is held by another seat, and exit 5, the rate limit).  `terminal` means it decided before it wrote
anything and no retry will help until an operator acts (exit 2 usage, 4 no credentials, and 6 no
bearer-verified seat or a refused authentication), and it raises BoardFailed, so the CLI exits with
the board's own code.  `unknown` means the
write may have landed (a timeout, an exit-1 GraphQL
refusal, a transport failure, or anything we cannot prove was never sent):  the row is kept as unknown,
so the record is never written twice.

What the operator is told about a failure is built from allow-listed fields only.  The board's own prose
is third-party text, and a GraphQL error body or a transport error can carry a credential or PII, so it
is never copied into the detail that gets persisted in a Linear comment.  What survives is the name of a
credential variable, a rejected credential's masked preview, a status code, a variable name, a scrubbed
request id, and a reason token this module chose.  The board is the layer that owns the credential, so it
is the board that hands back the masked preview.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from . import comment_keys, f3, records
from . import links as linkmod
from . import text as T
from .errors import BoardFailed, Refusal
from .items import Item
from .proc import Result, Runner

SESSION = "needsown"  # simple-chat cuts a session label to 8 characters
# The realm renamed #agent-sync, and the env var overrides it.  The name is read
# through the links module, which casefolds it, so a mixed-case override still
# compares with the casefolded channel names everywhere else.
CHANNEL = linkmod.resolve_channel()
READ_BACK = 30  # messages read when looking for a post that may have landed
MAYBE_SENT_CODES = (5, 6)
FALLBACK_SEAT = "CLAUDE"  # CLAUDE's fallback notifier (C6-2) posts a pending notice for any seat
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")

# The board CLI's exit codes, and what each one means for a write.  2 usage,
# 4 no credentials, and 6 no bearer-verified seat or a refused authentication
# are all decided before anything is written, and a retry will not help until
# an operator acts:  they are terminal.  3 claim held and 5 rate limit are
# decided before anything is written too, but a later run may write the
# record, so they are retryable.
# Exit 1 is the generic failure:  it carries both a GraphQL refusal and a
# transport failure, and the mutation may have landed, so it is unknown.
# Exit 7 is retired:  it was the writes-off switch, and the bearer-verified
# seat is the only write gate now.
BOARD_RETRYABLE_CODES = (3, 5)
BOARD_TERMINAL_CODES = (2, 4, 6)
UNKNOWN_MARKERS = ("no usable response", "not confirmed", "timed out", "timeout")

# The four outcomes of a board write.
OK = "ok"
RETRYABLE = "retryable"
TERMINAL = "terminal"
UNKNOWN = "unknown"

# The reason of a pending record goes into a Linear comment as `reason: <word>`
# and is read back with a `^[a-z_]{1,30}$` pattern, so it stays lowercase word
# characters.  What the board said about the failure goes on a line of its own,
# never on that one.
REASON_CLAIM_HELD = "claim_held"
REASON_RATE_LIMITED = "rate_limited"
REASON_OUTCOME_UNKNOWN = "outcome_unknown"

# A name of an environment variable, as the board CLI writes it.  Only the name
# is ever reported:  a value never comes out of here.
VAR_NAME = re.compile(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b")
# The board's error prose is third-party text:  a GraphQL error body or a transport
# error can carry a credential or PII, and the detail is persisted in a Linear
# comment, so the prose is never copied into it.  What survives is built from the
# board's output by patterns that cannot carry an arbitrary value, and each part is
# a field this module names.
#
# The reasons are tokens this module chose, matched by a phrase the board is known
# to use.  An unmatched failure is reported by its code alone, which is honest:  a
# better clue would have to be an allow-listed field of its own.
REASON_TOKENS = (
    ("claimed by", "claim_held"),
    ("claim held", "claim_held"),
    ("rate limit", "rate_limited"),
    ("too many requests", "rate_limited"),
    ("no usable response", "no_usable_response"),
    ("not confirmed", "not_confirmed"),
    ("unauthorized", "unauthorized"),
    ("invalid api key", "unauthorized"),
    ("forbidden", "forbidden"),
    ("entity not found", "entity_not_found"),
    ("not found", "not_found"),
    ("timed out", "timed_out"),
    ("timeout", "timed_out"),
)
# The status code Linear answered with, and a request id to quote back to Linear.
# The board hands the status back as a field of its own (`http=401`), so the field
# form is read as well as the sentence form.
HTTP_STATUS = re.compile(r"\bHTTP[ =](\d{3})\b", re.IGNORECASE)
REQUEST_ID = re.compile(r"\brequest[-_ ]?id[:= ]+([A-Za-z0-9_-]{4,64})\b", re.IGNORECASE)
# The name of the error the board hit, as the board names it:  a class name, never
# a message.
ERROR_TYPE = re.compile(r"\berror_type=([A-Za-z0-9_]{1,40})\b")
# The board's masked credential preview.  It is the board that owns the credential,
# so it is the board that can hand back a safe preview of it, and only a value
# shaped exactly like a preview is taken.
#
# The shape is the shape the board's own `credential_preview` produces:  the first
# eight characters of the credential, the ellipsis, and the last four.  A Linear
# credential starts with `lin_api_`, `lin_oauth_` or `lin_wh_`, so the preview
# carries an underscore, and a credential under twenty-four characters shows one
# leading character and the ellipsis alone.  The character set is an allow-list of
# the characters a credential preview can hold: a value carrying punctuation or
# symbols is not a preview, and taking it would put arbitrary text into a Linear
# comment and a job log.
CREDENTIAL_FIELD = re.compile(r"\bcredential=(\S+)")
MASKED_SHAPE = re.compile(r"^[A-Za-z0-9_.-]{1,8}…[A-Za-z0-9_.-]{0,4}$")
# The names of the credential variables the operator has to set, as the board hands
# them back.  The board owns the credential, so the board is the layer that can say
# which variable is missing.
CREDENTIAL_FIELD_VAR = re.compile(r"\bcredential_field=(\S+)")
DETAIL_MAX = 240


@dataclass
class Notice:
    state: str  # notified | notify_pending
    msg_id: int | None = None
    topic: str | None = None
    reason: str | None = None
    recorded: bool = False  # the Linear comment that records it
    already: bool = False  # it was already announced
    linkback: str | None = None  # mirror items:  ok | failed
    detail: str | None = None  # what the board said about a failure

    def to_json(self) -> dict:
        return {
            "state": self.state,
            "notified_msg_id": self.msg_id,
            "topic": self.topic,
            "reason": self.reason,
            "recorded": self.recorded,
            "already_notified": self.already,
            "linkback": self.linkback,
            "detail": self.detail,
        }


def _quote(text: str, cap: int) -> str:
    return f3.textsafe.render_untrusted(text, cap)


def _again_phrase(again_of: str) -> str:
    """'AFC-12 msg 456' as 'AFC-12 (message 456)'."""
    ident, _, message = again_of.partition(" msg ")
    return "%s (message %s)" % (ident, message) if message else ident


def compose_body(item: Item, header: str | None = None) -> str:
    """The OWNER topic's first message.  Every word that came from the filer goes through `render_untrusted`.
    `header` is a line of the sender's own words that goes first (the fallback notifier's "for SEAT ...")."""
    b = item.block
    if b is None:
        raise Refusal("%s has no readable needs-owner block" % item.identifier)
    urgent = item.severity == "P1"
    paragraphs = [header] if header else []
    paragraphs.append(
        "**%s** from %s:  %s"
        % (
            "Urgent decision needed" if urgent else "Decision needed",
            b.filed_by,
            _quote(b.question, 260),
        )
    )
    if b.again_of:
        paragraphs.append("Asking again.  The earlier question was %s." % _again_phrase(b.again_of))
    lines = []
    for n, option in enumerate(b.options, 1):
        if n in b.low_stakes:
            lines.append(
                "%s Option %d:  %s  (Tap to answer)" % (T.option_emoji(n), n, _quote(option, 220))
            )
        else:
            lines.append("Option %d:  %s  (reply here to choose)" % (n, _quote(option, 220)))
    paragraphs.append("\n".join(lines))
    if b.recommend:
        paragraphs.append("Recommendation:  option %d." % b.recommend)
    paragraphs.append("Linear:  %s" % item.url)
    paragraphs.append("Answer here or as a comment on the Linear issue.")
    paragraphs.append("Ref:  `%s`" % b.uid)
    return "\n\n".join(paragraphs) + "\n"


def _message_id(result: Result) -> int | None:
    for line in reversed(result.out.strip().splitlines()):
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict) and isinstance(value.get("id"), int):
            return value["id"]
    return None


def own_lines(content: str) -> list[str]:
    """The lines of a message's raw Markdown that are its author's own words:  not a line that begins with `>`,
    and not a line inside a closed code fence (Zulip's quote-and-reply wraps the original in a ```quote fence
    with no `>` on its lines).  A fence that is never closed hides nothing."""
    lines = (content or "").splitlines()
    kept = []
    n = 0
    while n < len(lines):
        opening = _FENCE.match(lines[n])
        if opening:
            fence = opening.group(1)
            closing = re.compile(r"^ {0,3}%s{%d,}[ \t]*$" % (re.escape(fence[0]), len(fence)))
            end = next((k for k in range(n + 1, len(lines)) if closing.match(lines[k])), None)
            if end is not None:
                n = end + 1
                continue
        if not lines[n].lstrip().startswith(">"):
            kept.append(lines[n])
        n += 1
    return kept


def carries_ref(content: str, uid: str) -> bool:
    """True when `content` has the reference line `Ref:  `<uid>`` as a line of its own, not quoted."""
    want = re.compile(r"^ {0,3}Ref:\s+`%s`\s*$" % re.escape(uid))
    return any(want.match(line) for line in own_lines(content))


def notice_seats(seat: str) -> frozenset[str]:
    """The seats whose bot may have posted the notice of an item that `seat` filed."""
    return frozenset({f3.identity.canonical_seat(seat), FALLBACK_SEAT})


def _env_of(runner: Runner, environ: dict | None = None) -> dict:
    """The environment a call reads:  the caller's own, and the runner's own when
    there is none.

    Never the process environment:  an ambient variable must not decide where a
    notice goes, or how a board write is read."""
    if environ is not None:
        return environ
    return dict(getattr(runner, "env", None) or {})


def find_notice(
    runner: Runner,
    topic: str,
    uid: str,
    *,
    filer: str | None = None,
    channel: str | None = None,
    environ: dict | None = None,
) -> int | None:
    """The id of the message that announced the item whose uid is `uid`, else None.  `filer` is the seat that
    filed the item when the caller is not that seat (the fallback notifier runs as CLAUDE, so the filer's own bot
    must be allowed too, or a post the filer's bot landed but did not record would be posted a second time).

    It reads the newest `READ_BACK` messages of `topic` (`simple-chat read --json`, whose fields are the real
    `message_json`) and keeps a message only when all of this holds:  `is_bot` is true (never a person,
    whatever name they sign), its `seat` is the filer's or CLAUDE (a bot of another seat can copy the public
    uid), and `carries_ref` finds the reference as an unquoted line of its own (a quote-and-reply, from
    anyone, does not count).  The result is the lowest matching id, because the first post is the notice.
    A sender whose kind simple-chat could not look up has `is_bot` null and is left out:  the worst result
    is a repeated post, never an answer or a quote taken for the notice.

    The channel comes from the caller's own `environ`, and from the runner's own
    environment when there is none:  never from the process environment, so an
    ambient `AGENT_SYNC_CHANNEL` cannot decide where the notice is read."""
    result = runner.agent_sync(
        "read",
        "--as",
        runner.seat,
        "--session",
        SESSION,
        "--json",
        "--channel",
        channel if channel is not None else linkmod.resolve_channel(_env_of(runner, environ)),
        "--topic",
        topic,
        "--include-self",
        "--limit",
        str(READ_BACK),
    )
    if not result.ok:
        return None
    allowed = notice_seats(runner.seat) | (notice_seats(filer) if filer else frozenset())
    found = []
    for line in result.out.splitlines():
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if (
            not isinstance(message, dict)
            or not isinstance(message.get("id"), int)
            or isinstance(message.get("id"), bool)
        ):
            continue
        if (
            message.get("is_bot") is not True
            or f3.identity.canonical_seat(message.get("seat")) not in allowed
        ):
            continue
        if carries_ref(str(message.get("content") or ""), uid):
            found.append(message["id"])
    return min(found) if found else None


def _board_text(result: Result) -> str:
    """Everything the board said, as one line:  its stderr first, then its stdout."""
    return " ".join(part for part in ((result.err or ""), (result.out or "")) if part.strip())


def _not_confirmed(result: Result) -> bool:
    """True when the board said the outcome of the write was not confirmed."""
    text = ("%s %s" % (result.err or "", result.out or "")).lower()
    return any(marker in text for marker in UNKNOWN_MARKERS)


def _missing_names(text: str) -> list[str]:
    """The names of the environment variables the board says are missing.

    Only names come out.  A name the board printed with a value is left out:
    that value is set, not missing, and a value must never reach the operator.
    When that leaves nothing, every name is reported:  a name the operator needs
    beats a tidy list."""
    every = list(dict.fromkeys(VAR_NAME.findall(text)))
    unset = [name for name in every if not re.search(re.escape(name) + r"=\s*\S", text)]
    return unset or every


def _field_names(text: str) -> list[str]:
    """The names of the credential variables the board handed back as structured data.

    The board owns the credential, so the board is the layer that can say which
    variable an operator has to set, and it says it in a field of its own.  Only
    a name that is shaped like a name comes out:  the field is read, never the
    sentence around it."""
    match = CREDENTIAL_FIELD_VAR.search(text)
    if not match:
        return []
    names = []
    for part in match.group(1).split(","):
        name = part.strip()
        if VAR_NAME.fullmatch(name) and name not in names:
            names.append(name)
    return names


def _reason_token(text: str) -> str:
    """The reason this module gives the failure, from a phrase the board is known to use.

    The board's own words are not copied:  they are third-party text, and this detail
    is persisted in a Linear comment.  A phrase this module does not know is left out,
    and the failure is reported by its code alone."""
    lowered = text.lower()
    for phrase, token in REASON_TOKENS:
        if phrase in lowered:
            return token
    return ""


def _credential_preview(text: str) -> str:
    """The masked credential preview the board printed, when it printed one.

    The board owns the credential, so the board is the layer that can hand back a
    safe preview of it.  Only a value shaped exactly like a preview is taken."""
    match = CREDENTIAL_FIELD.search(text)
    if not match:
        return ""
    value = match.group(1)
    return value if MASKED_SHAPE.match(value) else ""


def _allow_listed_extras(text: str) -> list[str]:
    """The board's allow-listed fields about the failure, as detail parts.

    A stable error name, the status code and a scrubbed request id:  the three
    things a caller may quote back without repeating anything the provider said."""
    parts = []
    error_type = ERROR_TYPE.search(text)
    if error_type:
        parts.append("error_type=%s" % error_type.group(1))
    status = HTTP_STATUS.search(text)
    if status:
        parts.append("http=%s" % status.group(1))
    request = REQUEST_ID.search(text)
    if request:
        parts.append("request_id=%s" % request.group(1))
    return parts


def _detail(result: Result) -> str:
    """What the board said about a failed write, as allow-listed fields only.

    The board's own prose is third-party text:  a GraphQL error body or a transport
    error can carry a credential or PII, and this string is persisted in a Linear
    comment, so the prose is never copied into it.  What survives is built here from
    the board's output by patterns that cannot carry an arbitrary value:  the name of
    a credential variable, a rejected credential's masked preview, a status code, a
    variable name, a scrubbed request id, and a reason token this module chose."""
    text = _board_text(result)
    if result.code == 4:
        # The board owns the credential, so the board is the layer that names the
        # variable the operator has to set, and it names it in its own structured
        # field.  Only that field counts: a name read out of the board's prose is
        # not a contract, and a name this module guessed would send the operator
        # after the wrong secret.  Every code-4 exit of the board carries the
        # field, so a board that names no variable is a board that broke its
        # contract, and this module says that instead of guessing.
        names = _field_names(text)
        if names:
            return "credential variable: %s" % ", ".join(names)
        # No field: fail closed.  The outcome stays terminal, so the run stops,
        # and the detail names the board's broken contract and no credential.
        return "the board named no credential variable"
    if result.code == 6:
        preview = _credential_preview(text)
        parts = [
            "credential rejected: %s" % preview if preview else "credential rejected: (no preview)"
        ]
        parts += _allow_listed_extras(text)
        return " ".join(parts)[:DETAIL_MAX]
    parts = ["board write failed (code=%d)" % result.code]
    reason = _reason_token(text)
    if reason:
        parts.append("reason=%s" % reason)
    parts += _allow_listed_extras(text)
    for name in _missing_names(text)[:4]:
        parts.append("name=%s" % name)
    return " ".join(parts)[:DETAIL_MAX]


def _board_failure(result: Result) -> BoardFailed:
    """The board's failure as an error the CLI can exit with.

    The message names what an operator has to fix and never a value.  The
    allow-listed detail rides on the exception as well, so a caller that catches
    it can report the diagnosis without taking the message apart."""
    detail = _detail(result)
    failure = BoardFailed(
        "board comment failed (exit %d):  %s" % (result.code, detail or "the board said nothing"),
        result.code,
    )
    failure.detail = detail
    return failure


def _classify(result: Result) -> str:
    """What one board write did:  `ok`, `retryable`, `terminal` or `unknown`.

    `ok` is the comment on the issue.  `retryable` means the board decided
    before it wrote anything and a later run may write the record again.
    `terminal` means it decided before it wrote anything and no retry will help
    until an operator acts.  `unknown` means the write may have landed, so
    nothing more is written on this key.

    The board's own words can only make the answer more careful:  a failure it
    says was not confirmed is unknown, whatever the code said."""
    if result.ok:
        return OK
    if result.timed_out:
        return UNKNOWN  # the request may have landed
    if result.missing:
        return TERMINAL  # the board CLI is not installed, so nothing was written
    if result.code in BOARD_RETRYABLE_CODES:
        return UNKNOWN if _not_confirmed(result) else RETRYABLE
    if result.code in BOARD_TERMINAL_CODES:
        return UNKNOWN if _not_confirmed(result) else TERMINAL
    # Exit 1 is the generic failure:  it carries both a GraphQL refusal and a
    # transport failure, and the mutation may have landed.  An unclassified
    # failure is unknown and never a definite refusal:  a replay of a write
    # that may have landed would put the record on the issue twice.
    return UNKNOWN


@dataclass
class Write:
    """What one board write did, and what the board said about it."""

    outcome: str
    detail: str = ""
    code: int = 0

    def __bool__(self) -> bool:
        """True only when the record was written.

        A caller that only wants to know whether the record landed keeps working
        with `if not record(...)`, and one that wants the reason reads `.outcome`
        and `.detail`:  a terminal failure and a retryable one are never the same
        thing."""
        return self.outcome == OK


def _write_record(runner: Runner, item: Item, text: str, *, environ: dict | None = None) -> Write:
    """One comment on the item, written once.

    The board CLI derives a comment's own id from the seat and the session, so the
    same record written by the filing seat and again by CLAUDE's fallback notifier
    is two comments on one issue, and a retry after a lost response can post the
    record a second time.  The key store holds one outcome per item and record
    text, so the second writer returns the first one's result instead of posting
    again.

    A retryable failure is tried twice, because nothing was written either time and
    the claim may be released in between.  A terminal failure is tried once:  it is the
    same on a second try, so a second board call buys nothing.  An unknown outcome is
    not tried again:  the request may have landed, and a second write could post the
    record twice.  A terminal failure raises BoardFailed, so the CLI exits with the
    board's own code and the operator sees the board's reason;  `record` catches that
    raise and keeps the reason in the outcome it returns."""
    env = environ if environ is not None else runner.env
    key = comment_keys.key_for(item.identifier, text)
    held = comment_keys.get(key, env)
    if held == comment_keys.OK:
        return Write(OK)
    if held == comment_keys.UNKNOWN:
        return Write(UNKNOWN, "the record may already be on the issue;  it is not written again")
    outcome = UNKNOWN
    for _ in range(2):
        with runner.private_file(text) as path:
            result = runner.board(
                "comment", item.identifier, "--by", runner.seat, "--text-file", path
            )
        outcome = _classify(result)
        if outcome == OK:
            comment_keys.put(key, comment_keys.OK, env)
            return Write(OK)
        if outcome == UNKNOWN:
            # The write may have landed:  the row is kept as unknown, so the
            # record is never written twice.
            comment_keys.put(key, comment_keys.UNKNOWN, env)
            return Write(UNKNOWN, _detail(result), result.code)
        if outcome == TERMINAL:
            # A terminal failure is the same on a second try:  the claim is not held, the
            # rate limit is not in play, and the write is not going to be allowed.  Retrying
            # it costs a second board call and changes nothing, so it is tried once.
            break
    # Nothing was written, so the row may go:  a later run may write the record.
    comment_keys.release(key, env)
    if outcome == TERMINAL:
        raise _board_failure(result)
    return Write(outcome, _detail(result), result.code)


def record(runner: Runner, item: Item, text: str) -> Write:
    """One comment on the item, written as the runner's seat.  The notifier's reply records use it.

    The outcome is returned, not raised:  the caller may already have filed the
    issue, and an exception there would abandon the rest of the flow with the item
    half done.  A terminal failure keeps its own outcome and its own detail, so
    the caller can tell it apart from a retryable one and report why it failed:
    the detail is the board's masked preview of a refused credential, or the name
    of the variable that is missing, and never the board's prose."""
    try:
        return _write_record(runner, item, text)
    except BoardFailed as failure:
        return Write(TERMINAL, getattr(failure, "detail", ""), failure.exit_code)


def _reason(wrote: Write) -> str:
    """The reason word for a pending record:  lowercase word characters only."""
    if wrote.outcome == RETRYABLE:
        return REASON_CLAIM_HELD if wrote.code == 3 else REASON_RATE_LIMITED
    return REASON_OUTCOME_UNKNOWN


def _pending_text(reason: str, detail: str = "") -> str:
    """The pending record:  the reason word, and the board's words on a line of their own.

    The reason is read back with a `^[a-z_]{1,30}$` pattern, so the detail never
    rides on that line."""
    text = records.pending_text(reason)
    return "%s\ndetail: %s" % (text, detail) if detail else text


def _announce(runner: Runner, item: Item, msg_id: int, topic: str) -> Notice:
    """The notice went out:  its record goes on the issue.

    A record that could not be written leaves the item pending:  a definite
    failure may be written by a later run, and an unknown one must never be
    written twice.  A terminal failure never comes back:  it raises, so the CLI
    exits with the board's own code."""
    wrote = _write_record(runner, item, records.notified_text(msg_id, topic))
    if wrote.outcome == OK:
        return Notice("notified", msg_id, topic, None, True)
    notice = _pending(runner, item, topic, _reason(wrote), wrote.detail)
    notice.msg_id = msg_id  # the notice went out;  only its record is missing
    return notice


def _pending(runner: Runner, item: Item, topic: str, reason: str, detail: str = "") -> Notice:
    recorded = True
    if item.pending in (
        None,
        "held",
    ):  # one record is enough (a rerun does not pile more on);  a "held" mark is replaced
        recorded = _write_record(runner, item, _pending_text(reason, detail)).outcome == OK
    return Notice("notify_pending", None, topic, reason, recorded, detail=detail or None)


def notify(
    runner: Runner,
    item: Item,
    *,
    rescan: bool = False,
    environ: dict | None = None,
    header: str | None = None,
) -> Notice:
    """Announce `item` unless it already is.  `rescan` looks in the topic for an earlier post of this item
    first (a rerun after a crash between the post and its record).  `header` goes first in the message."""
    b = item.block
    if b is None:
        raise Refusal("%s has no readable needs-owner block;  adopt it first" % item.identifier)
    # one channel for the whole call, from the caller's own environ (or the runner's), so the post
    # and the read-back that confirms it can never look in two different channels, and neither of
    # them looks in an ambient channel
    channel = linkmod.resolve_channel(_env_of(runner, environ))
    if item.notified:
        return Notice(
            "notified", item.notified[0], item.notified[1] or item.topic, None, True, already=True
        )
    if b.mirror_of:
        return _mirror(runner, item, linkmod.parse_topic_link(b.mirror_of, environ))
    topic = T.topic_for(item.identifier, b.question)
    if rescan:
        earlier = find_notice(runner, topic, b.uid, filer=b.filed_by, channel=channel)
        if earlier is not None:
            return _announce(runner, item, earlier, topic)
    result = runner.agent_sync(
        "post",
        "--as",
        runner.seat,
        "--session",
        SESSION,
        "--json",
        "--channel",
        channel,
        "--topic",
        topic,
        "--to-owner",
        "--",
        "-",
        stdin=compose_body(item, header),
    )
    if result.ok:
        msg_id = _message_id(result)
        if msg_id is not None:
            return _announce(runner, item, msg_id, topic)
        landed = find_notice(
            runner, topic, b.uid, filer=b.filed_by, channel=channel
        )  # posted, but the answer carried no id
        return (
            _announce(runner, item, landed, topic)
            if landed is not None
            else _pending(runner, item, topic, "send_failed")
        )
    if result.missing:
        return _pending(runner, item, topic, "no_agent_sync")
    if result.timed_out or result.code in MAYBE_SENT_CODES:
        landed = find_notice(runner, topic, b.uid, filer=b.filed_by, channel=channel)
        if landed is not None:
            return _announce(runner, item, landed, topic)
        return _pending(runner, item, topic, "send_failed")
    if result.code == 3:
        return _pending(runner, item, topic, "no_credential")
    return _pending(runner, item, topic, "send_failed")


def _mirror(runner: Runner, item: Item, link: linkmod.TopicLink) -> Notice:
    """A Grok Bot persona already opened the topic:  record its message as the notice, then reply there
    with the Linear link (the reply is a courtesy;  its failure is reported and does not undo the record)."""
    notice = _announce(runner, item, link.message_id, link.topic)
    reply = "Filed in Linear as %s:  %s\n\nAnswer here or as a comment on the Linear issue.\n" % (
        item.identifier,
        item.url,
    )
    result = runner.agent_sync(
        "reply",
        "--as",
        runner.seat,
        "--session",
        SESSION,
        "--json",
        "--id",
        str(link.message_id),
        "--",
        "-",
        stdin=reply,
    )
    notice.linkback = "ok" if result.ok else "failed"
    return notice
