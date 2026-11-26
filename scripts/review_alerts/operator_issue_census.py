"""The daemon-side protected issue census, projected for the standalone-issue lane.

One in-process read (the Operator's own RegistryService.issue_census, never an HTTP
route and never caller JSON) feeds three consumers from the SAME snapshot logic:

* sessions():  LIVE session rows for RegistryIssueOriginBinder (the binder re-proves
  ownership per issue at execution);
* owners():  one flattened row per current-generation binding for the poll census, whose
  origin claims are the census row's own exact identity (the same identity the binder
  returns);
* probe_target():  the diagnostics-only require_issue capability context, derived FRESH
  from the gate seat's own live row, with a fixed high issue number verified UNBOUND on
  that row.  Nothing is synthesized:  with no live gate-seat row the lane holds.

Every projection is fail closed.  An unreadable census raises Held (never an empty
census), a row whose seat or platform does not match the configured maps is dropped, and
a malformed entry is dropped:  a dropped row can only make the binder hold, never bind.
"""

from __future__ import annotations

from .operator_pr_origin import Held

ISSUE_NUMBER_MAX = 99_999_999
PROBE_ATTEMPTS = 8


def _is_int(value) -> bool:
    return type(value) is int


class IssueCensus:
    def __init__(self, census_fn, seat_map, platform_map, *, stop=None, repos=()):
        if not callable(census_fn):
            raise Held("issue-census-unavailable")
        self._census = census_fn
        self.seat_map = {str(k): str(v) for k, v in dict(seat_map).items()}
        self.platform_map = {str(k): str(v) for k, v in dict(platform_map).items()}
        self.stop = stop
        self.repos = tuple(sorted(repos))
        inverse: dict[str, list[str]] = {}
        for slug, tag in self.seat_map.items():
            inverse.setdefault(tag, []).append(slug)
        # A tag two slugs share is ambiguous:  those rows are dropped, never guessed.
        self._slug_for_tag = {tag: s[0] for tag, s in inverse.items() if len(s) == 1}

    def _read(self):
        if self.stop is not None and self.stop():
            raise Held("operator-stopping")
        try:
            rows = self._census()
        except Held:
            raise
        except Exception as exc:  # noqa: BLE001 - unreadable is never empty
            raise Held("issue-census-unreadable:" + type(exc).__name__) from exc
        if not isinstance(rows, list):
            raise Held("issue-census-invalid")
        return rows

    def sessions(self) -> list[dict]:
        out = []
        for row in self._read():
            if not isinstance(row, dict) or row.get("closed_at"):
                continue
            tag = str(row.get("seat") or "")
            slug = self._slug_for_tag.get(tag)
            platform = self.platform_map.get(tag)
            if slug is None or not platform:
                continue
            if str(row.get("platform") or "").casefold() != platform.casefold():
                continue
            generation = row.get("generation")
            session, letter = row.get("session_id"), row.get("letter")
            host, scope = row.get("verified_host"), row.get("profile_scope")
            if (
                not _is_int(generation)
                or generation < 1
                or not all(isinstance(v, str) and v for v in (session, letter, host, scope))
            ):
                continue
            entries = []
            for entry in row.get("bound_issues") or []:
                if (
                    isinstance(entry, dict)
                    and isinstance(entry.get("repo"), str)
                    and entry["repo"]
                    and _is_int(entry.get("number"))
                    and 1 <= entry["number"] <= ISSUE_NUMBER_MAX
                    and _is_int(entry.get("generation"))
                    and entry["generation"] == generation
                ):
                    entries.append(
                        {
                            "repo": entry["repo"],
                            "number": entry["number"],
                            "generation": generation,
                            "ts": entry["ts"] if isinstance(entry.get("ts"), str) else "",
                        }
                    )
            if entries:
                out.append(
                    {
                        "session_id": session,
                        "letter": letter,
                        "generation": generation,
                        "seat": slug,
                        "tag": tag,
                        "verified_host": host,
                        "profile_scope": scope,
                        "bound_issues": entries,
                    }
                )
        return out

    def owners(self) -> list[dict]:
        out = []
        for row in self.sessions():
            for entry in row["bound_issues"]:
                out.append(
                    {
                        "repo": entry["repo"],
                        "number": entry["number"],
                        "ts": entry["ts"],
                        "seat": row["seat"],
                        "origin_session_claim": row["session_id"],
                        "origin_letter_claim": row["letter"],
                        "origin_generation_claim": row["generation"],
                    }
                )
        return out

    def probe_target(self, gate_tag: str) -> dict:
        """A fresh exact probe context for the gate seat, or Held (the lane holds)."""
        if not self.repos:
            raise Held("issue-probe-repo-unconfigured")
        live = sorted(
            (r for r in self.sessions() if r["tag"] == gate_tag),
            key=lambda r: (r["letter"], r["session_id"]),
        )
        if not live:
            raise Held("issue-probe-no-live-gate-session")
        row = live[0]
        repo = self.repos[0]
        bound = {
            (e["repo"].casefold(), e["number"]) for e in row["bound_issues"]
        }
        for offset in range(PROBE_ATTEMPTS):
            number = ISSUE_NUMBER_MAX - offset
            if (repo.casefold(), number) not in bound:
                return {
                    "seat": gate_tag,
                    "session": row["session_id"],
                    "letter": row["letter"],
                    "generation": row["generation"],
                    "require_issue": "%s#%d" % (repo, number),
                }
        raise Held("issue-probe-number-bound")
