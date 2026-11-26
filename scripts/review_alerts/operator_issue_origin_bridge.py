"""Registry bridge for STANDALONE-ISSUE origin binding.

The ONLY ownership source for an issue is the registry's explicit
bound_issues association (the session's own seat-lane write, canonical
existence/type-checked at bind time upstream), read with the same
generation rule the router's _issue_bound applies:  the entry's generation
stamp is mandatory and must equal the session row's LIVE generation, and
bound_prs never satisfies an issue pin nor the reverse.  An assignee,
label, title or letter guess is never ownership.

Row contract (supplied by rows_provider, the daemon-side census wiring):
each row is one LIVE registry session (a row carrying closed_at is skipped):
session_id, letter, generation, seat, nonempty verified_host and
profile_scope, and bound_issues entries of {"repo", "number", "generation"}
with number an exact int in 1..99,999,999 (no string/bool coercion).
Exactly one matching live row binds;  zero or several hold.  Platform,
host and scope come from the configured maps, never from row metadata
alone:  same discipline as the PR bridge.
"""

from __future__ import annotations

from typing import Mapping

from .operator_pr_origin import Held

#: Canonical GitHub issue numbers are bounded by the consumer contract
#: (1..99,999,999), same as the hint intake boundary.
ISSUE_NUMBER_MAX = 99_999_999


class RegistryIssueOriginBinder:
    def __init__(self, rows_provider, seat_map, platform_map):
        self.rows_provider = rows_provider
        self.seat_map = dict(seat_map)
        self.platform_map = dict(platform_map)

    def bind(self, repo, number):
        if type(number) is not int or not 1 <= number <= ISSUE_NUMBER_MAX:
            raise Held("issue-number-out-of-range")
        matches = []
        for row in self.rows_provider() or []:
            if not isinstance(row, Mapping):
                continue
            # Only LIVE session rows bind:  a closed session's stale
            # association is not ownership.
            if row.get("closed_at"):
                continue
            generation = row.get("generation")
            if type(generation) is not int or generation < 1:
                continue
            for entry in row.get("bound_issues") or []:
                if not isinstance(entry, Mapping):
                    continue
                entry_repo = entry.get("repo")
                if not isinstance(entry_repo, str) or not entry_repo:
                    continue
                if entry_repo.casefold() != str(repo).casefold():
                    continue
                # The entry number is an exact canonical int:  a string,
                # bool or out-of-range value is a malformed census entry,
                # never a match by coercion.
                entry_number = entry.get("number")
                if type(entry_number) is not int or not 1 <= entry_number <= ISSUE_NUMBER_MAX:
                    continue
                if entry_number != number:
                    continue
                entry_generation = entry.get("generation")
                if type(entry_generation) is not int or entry_generation != generation:
                    continue
                matches.append(row)
                break
        if not matches:
            raise Held("issue-unbound")
        if len(matches) > 1:
            raise Held("issue-bound-ambiguous")
        row = matches[0]
        seat = self.seat_map.get(str(row.get("seat") or ""))
        if not seat:
            raise Held("seat-unmapped")
        platform = self.platform_map.get(seat)
        if not platform:
            raise Held("platform-not-configured")
        session = row.get("session_id")
        letter = row.get("letter")
        if not isinstance(session, str) or not session:
            raise Held("session-missing")
        if not isinstance(letter, str) or not letter:
            raise Held("letter-missing")
        verified_host = str(row.get("verified_host") or "")
        profile_scope = str(row.get("profile_scope") or "")
        if not verified_host:
            raise Held("host-missing")
        if not profile_scope:
            raise Held("scope-missing")
        return dict(
            seat=seat,
            platform=platform,
            verified_host=verified_host,
            profile_scope=profile_scope,
            session=session,
            letter=letter,
            generation=row["generation"],
        )
