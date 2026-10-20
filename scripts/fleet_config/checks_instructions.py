"""``instructions`` checks:  the marked block hash and the whole file hash.

Two check types live here, both L0:

  * ``MARKED_BLOCK_HASH``  hashes the body of one named marker block inside a rules
    file, and compares it with ``expected.sha256``.
  * ``FILE_HASH``  hashes the whole target file and compares it with ``expected.sha256``.

The marker grammar is re-implemented here rather than imported.  ``fleet_lanes`` must
not be imported at runtime:  a stable copy of this package runs under ``python3 -I``
with only this package present (design doc, section 12).  A parity test in
``tests/test_checks.py`` pins this module's extraction to
``fleet_lanes.marked_block.block_body`` on a shared fixture.

Two marker families are read, in this order:

  * the hash-comment family, ``# fleet:begin <name>`` ... ``# fleet:end <name>``, which
    is the grammar ``fleet_lanes/marked_block.py`` writes and reads;
  * the HTML-comment family, ``<!-- <name>:begin v<N> -->`` ... ``<!-- <name>:end -->``,
    which is the grammar ``fleet_lanes/install_rules.py`` writes into the platform
    rules files.

The block body is hashed as its lines joined with ``\\n`` and one trailing newline,
encoded as UTF-8, which is what the design doc pins in section 5.

A rule with no ``expected.sha256`` is a presence check.  The block (or the file) must
be there and well formed, the check reports ``PASS`` with ``proof_level_achieved`` at
``L0``, and the observed hash is reported so a manifest can be seeded from it.  A
rule with ``expected.present`` set to false inverts the sense:  the target must be
absent, and its absence is the pass.

Stdlib only, and Python 3.9 safe, matching ``fleet_knobs``.
"""
from __future__ import annotations

import hashlib
import os
import re
from typing import List, Optional, Tuple

from .checks import CheckContext, CheckError, build_result, expand_target, sha256_text
from .types import (
    CheckResult,
    CheckState,
    CheckType,
    ObservedState,
    ProofLevel,
    ReasonCode,
    Rule,
)

__all__ = ["CHECKS"]

# The hash-comment family, the grammar fleet_lanes/marked_block.py writes.
_BEGIN_MARKER = re.compile(r"^[ \t]*# fleet:begin (?P<name>[A-Za-z0-9._-]+)(?: .*)?$")
_END_MARKER = re.compile(r"^[ \t]*# fleet:end (?P<name>[A-Za-z0-9._-]+)[ \t]*$")

# The HTML-comment family, the grammar fleet_lanes/install_rules.py writes.
_HTML_BEGIN_MARKER = re.compile(
    r"^[ \t]*<!-- (?P<name>[A-Za-z0-9._-]+):begin v(?P<version>\d+) -->[ \t]*$"
)
_HTML_END_MARKER = re.compile(r"^[ \t]*<!-- (?P<name>[A-Za-z0-9._-]+):end -->[ \t]*$")


class MarkerError(ValueError):
    """The markers in a file cannot be trusted, so the caller refuses instead of guessing."""


def _locate(
    lines: List[str],
    name: str,
    begin_re: "re.Pattern[str]",
    end_re: "re.Pattern[str]",
) -> Optional[Tuple[int, int]]:
    """The (begin index, end index) of the named block, or None when there is none.

    Raises MarkerError for a begin with no end, an end with no begin, an end before its
    begin, or the same name twice.  Blocks of other names are ignored, exactly as
    ``fleet_lanes.marked_block.find_block`` ignores them.
    """
    begins: List[int] = []
    ends: List[int] = []
    for index, raw in enumerate(lines):
        line = raw.rstrip("\r")
        match = begin_re.match(line)
        if match and match.group("name") == name:
            begins.append(index)
            continue
        match = end_re.match(line)
        if match and match.group("name") == name:
            ends.append(index)
    if not begins and not ends:
        return None
    if len(begins) > 1 or len(ends) > 1:
        raise MarkerError(
            "the file holds %d begin and %d end markers for %r;  expected one block"
            % (len(begins), len(ends), name)
        )
    if not begins:
        raise MarkerError("the end marker for %r has no begin marker" % name)
    if not ends:
        raise MarkerError("the begin marker for %r has no end marker" % name)
    if ends[0] < begins[0]:
        raise MarkerError(
            "the end marker for %r comes before its begin marker" % name
        )
    return begins[0], ends[0]


def marked_block_body(text: str, name: str) -> Optional[Tuple[str, ...]]:
    """The lines of the named marker block, CR stripped and markers excluded, or None.

    The hash-comment family is read first and the HTML-comment family second.  A
    broken marker pair raises MarkerError rather than falling through to the other
    family, because a file with two begin markers is a file nobody can trust.
    """
    lines = text.split("\n")
    loc = _locate(lines, name, _BEGIN_MARKER, _END_MARKER)
    if loc is None:
        loc = _locate(lines, name, _HTML_BEGIN_MARKER, _HTML_END_MARKER)
    if loc is None:
        return None
    return tuple(line.rstrip("\r") for line in lines[loc[0] + 1:loc[1]])


def block_body_hash(body: Tuple[str, ...]) -> str:
    """The SHA-256 of a marker block body.

    The lines are joined with a newline and one trailing newline is added, then the
    text is encoded as UTF-8, which is the digest the design doc pins in section 5.
    """
    return sha256_text("\n".join(body) + "\n")


def _resolve(rule: Rule, ctx: CheckContext) -> str:
    """The expanded target path, under the home directory this check runs against.

    ``expand_target`` never consults the process environment for ``HOME``, so one run
    cannot see two homes.
    """
    return expand_target(rule.check.target, ctx.home)


def _read_bytes(path: str) -> bytes:
    """The raw bytes of a file, or an OSError the caller turns into an error result."""
    with open(path, "rb") as handle:
        return handle.read()


def _absent_result(rule: Rule, platform: str, ctx: CheckContext, reason: str) -> CheckResult:
    """A MISSING result for a target that is not there."""
    return build_result(
        rule,
        platform,
        CheckState.MISSING,
        reason,
        ReasonCode.MISSING,
        ObservedState(present=False),
        ctx,
    )


def _unreadable_result(
    rule: Rule, platform: str, ctx: CheckContext, reason: str, code: ReasonCode = ReasonCode.ERROR
) -> CheckResult:
    """An UNKNOWN result for a check that could not run."""
    return build_result(
        rule,
        platform,
        CheckState.UNKNOWN,
        reason,
        code,
        ObservedState(present=None),
        ctx,
    )


def check_marked_block_hash(rule: Rule, platform: str, ctx: CheckContext) -> CheckResult:
    """Check that a named marker block is present and matches the expected hash."""
    target = rule.check.target
    try:
        path = _resolve(rule, ctx)
    except CheckError as exc:
        return _absent_result(rule, platform, ctx, str(exc))

    if not os.path.exists(path):
        if rule.expected.present is False:
            return build_result(
                rule,
                platform,
                CheckState.PASS,
                "The file %s is absent, as the rule expects." % target,
                ReasonCode.OK,
                ObservedState(present=False),
                ctx,
                proof_level_achieved=ProofLevel.L0,
            )
        return _absent_result(rule, platform, ctx, "The file %s is missing." % target)

    if not os.path.isfile(path):
        return _unreadable_result(
            rule,
            platform,
            ctx,
            "The target %s is not a regular file, so its marked block cannot be read." % target,
        )

    try:
        raw = _read_bytes(path)
    except OSError as exc:
        return _unreadable_result(
            rule, platform, ctx, "The file %s cannot be read:  %s" % (target, exc.strerror or "unknown error")
        )

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return _unreadable_result(
            rule, platform, ctx, "The file %s is not valid UTF-8." % target
        )

    name = rule.check.detail
    if not name:
        return _unreadable_result(
            rule,
            platform,
            ctx,
            "The rule names no marked block, so there is nothing to hash.",
        )

    try:
        body = marked_block_body(text, name)
    except MarkerError as exc:
        return _unreadable_result(
            rule,
            platform,
            ctx,
            "The markers in %s cannot be trusted:  %s" % (target, exc),
            ReasonCode.UNPARSEABLE,
        )

    if body is None:
        if rule.expected.present is False:
            return build_result(
                rule,
                platform,
                CheckState.PASS,
                "The file %s holds no marked block named %r, as the rule expects." % (target, name),
                ReasonCode.OK,
                ObservedState(present=False),
                ctx,
                proof_level_achieved=ProofLevel.L0,
            )
        return _absent_result(
            rule,
            platform,
            ctx,
            "The file %s holds no marked block named %r." % (target, name),
        )

    digest = block_body_hash(body)
    observed = ObservedState(present=True, sha256=digest)
    expected_hash = (rule.expected.sha256 or "").strip().lower()

    if rule.expected.present is False:
        return build_result(
            rule,
            platform,
            CheckState.DRIFT,
            "The file %s holds a marked block named %r, which the rule says must be absent."
            % (target, name),
            ReasonCode.DRIFT,
            observed,
            ctx,
        )

    if not expected_hash:
        return build_result(
            rule,
            platform,
            CheckState.PASS,
            "The marked block %r is present and well formed in %s." % (name, target),
            ReasonCode.OK,
            observed,
            ctx,
            proof_level_achieved=ProofLevel.L0,
        )

    if digest != expected_hash:
        return build_result(
            rule,
            platform,
            CheckState.DRIFT,
            "The marked block %r in %s differs from the expected hash." % (name, target),
            ReasonCode.DRIFT,
            observed,
            ctx,
        )

    return build_result(
        rule,
        platform,
        CheckState.PASS,
        "The marked block %r in %s matches the expected hash." % (name, target),
        ReasonCode.OK,
        observed,
        ctx,
        proof_level_achieved=ProofLevel.L0,
    )


def check_file_hash(rule: Rule, platform: str, ctx: CheckContext) -> CheckResult:
    """Check that a whole file matches the expected SHA-256 of its bytes."""
    target = rule.check.target
    try:
        path = _resolve(rule, ctx)
    except CheckError as exc:
        return _absent_result(rule, platform, ctx, str(exc))

    if not os.path.exists(path):
        if rule.expected.present is False:
            return build_result(
                rule,
                platform,
                CheckState.PASS,
                "The file %s is absent, as the rule expects." % target,
                ReasonCode.OK,
                ObservedState(present=False),
                ctx,
                proof_level_achieved=ProofLevel.L0,
            )
        return _absent_result(rule, platform, ctx, "The file %s is missing." % target)

    if not os.path.isfile(path):
        return _unreadable_result(
            rule,
            platform,
            ctx,
            "The target %s is not a regular file, so its hash cannot be read." % target,
        )

    try:
        raw = _read_bytes(path)
    except OSError as exc:
        return _unreadable_result(
            rule, platform, ctx, "The file %s cannot be read:  %s" % (target, exc.strerror or "unknown error")
        )

    # The hash is over the raw bytes, as the design doc pins it.  For a UTF-8 file
    # this is the same digest checks.sha256_text gives for the decoded text.
    digest = hashlib.sha256(raw).hexdigest()
    observed = ObservedState(present=True, sha256=digest)
    expected_hash = (rule.expected.sha256 or "").strip().lower()

    if rule.expected.present is False:
        return build_result(
            rule,
            platform,
            CheckState.DRIFT,
            "The file %s exists, which the rule says must be absent." % target,
            ReasonCode.DRIFT,
            observed,
            ctx,
        )

    if not expected_hash:
        return build_result(
            rule,
            platform,
            CheckState.PASS,
            "The file %s is present." % target,
            ReasonCode.OK,
            observed,
            ctx,
            proof_level_achieved=ProofLevel.L0,
        )

    if digest != expected_hash:
        return build_result(
            rule,
            platform,
            CheckState.DRIFT,
            "The file %s differs from the expected hash." % target,
            ReasonCode.DRIFT,
            observed,
            ctx,
        )

    return build_result(
        rule,
        platform,
        CheckState.PASS,
        "The file %s matches the expected hash." % target,
        ReasonCode.OK,
        observed,
        ctx,
        proof_level_achieved=ProofLevel.L0,
    )


CHECKS = {
    CheckType.MARKED_BLOCK_HASH: check_marked_block_hash,
    CheckType.FILE_HASH: check_file_hash,
}
