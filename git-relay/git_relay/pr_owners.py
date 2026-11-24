"""Durable, queryable record of which authenticated seat created each pull request.

Why this exists:  every relay-created pull request is authored under the
owner's GitHub account (that is the relay's purpose), so the GitHub author
alone cannot say which seat asked for the PR.  The relay's audit log already
binds every open_pr attempt to its authenticated seat, the returned number
and the outcome correlation id.  This store is the queryable index over
those bindings:  appended at creation time, and rebuildable from the audit
log at any time (`python3 -m git_relay.pr_owners rebuild`).

Rows are one JSON object per line, fsynced on append (the directory is
fsynced on first create), exactly like the audit log.  Titles and bodies
are never stored here.  A row carries:

    ts          creation time (UTC, audit-row format)
    seat        the authenticated seat that asked for the PR
    repo        "owner/name", creation-time spelling (keys compare casefolded)
    number      the pull request number
    head        the head branch the seat asked for
    base        the base branch the seat asked for
    corr        the outcome correlation id of the creating attempt, when held
    confirmed   True once the create's read-back verified the PR;  False for
                a create whose outcome stayed unknown (unconfirmed number)
    source      "open_pr" (recorded live) or "audit_backfill" (rebuilt)

A pull request created outside the relay has no row and stays UNKNOWN:  the
index never guesses an owner from a title, a branch name or a label.  A
renamed repo is keyed under its creation-time spelling;  lookups under the
new name answer unknown rather than wrong.

The index is a derived view, not the ledger of record:  a load failure
(unreadable file, bad bytes, corrupt lines) never stops the relay - the
store starts empty or partial, records load_error, and a rebuild from the
audit log repairs it.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import threading
import time

CONFIRMED = "open_pr"
BACKFILL = "audit_backfill"

ROW_FIELDS = ("ts", "seat", "repo", "number", "head", "base", "corr", "confirmed", "source")


def _row(
    *,
    seat: str,
    repo: str,
    number: int,
    head: str,
    base: str,
    corr: str | None,
    confirmed: bool,
    source: str,
    ts: str | None = None,
    origin: dict | None = None,
    origin_session_claim: str | None = None,
    origin_letter_claim: str | None = None,
    origin_generation_claim: int | None = None,
) -> dict:
    from .service_read import origin_claims

    claims = dict(origin_claims(origin or {}))
    # The three creation-time native claims are the same values the shared
    # validator produces.  A caller that already validated them (the
    # creation path) passes them directly;  an absent claim stays absent,
    # so a row never grows an empty claim column.
    for name, value in (
        ("origin_session_claim", origin_session_claim),
        ("origin_letter_claim", origin_letter_claim),
        ("origin_generation_claim", origin_generation_claim),
    ):
        if value is not None:
            claims[name] = value
    # The merged set goes through the same shape checks as the shared
    # origin dict:  a row can never hold a claim the protected census
    # would reject later, which would fail-close every read for the repo.
    claims = dict(origin_claims(claims))
    return {
        **claims,
        "ts": ts or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "seat": seat,
        "repo": repo,
        "number": number,
        "head": head,
        "base": base,
        "corr": corr,
        "confirmed": bool(confirmed),
        "source": source,
    }


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("short write to pr-owners log")
        view = view[written:]


class PROwnerStore:
    """Append-only JSONL index with an in-memory (casefolded repo, number) map."""

    def __init__(self, path: str):
        self._path = path
        self._by_pr: dict[tuple[str, int], dict] = {}
        # A per-repo key list, in first-seen order, so a per-repo query never
        # scans every row in the store.
        self._by_repo: dict[str, list[tuple[str, int]]] = {}
        self._lock = threading.Lock()
        self.skipped_lines = 0
        self.load_error: str | None = None
        self._torn_tail = False
        self._pending_creations: set[str] = set()
        try:
            with open(path, "r", encoding="utf-8") as handle:
                for raw in handle:
                    if not raw.endswith("\n"):
                        # A crash can leave a partial final line:  the parse
                        # below skips it, and the next append first terminates
                        # it so the new row is not glued onto the torn tail.
                        self._torn_tail = True
                    line = raw.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                        if isinstance(row, dict) and "owner_index_event" in row:
                            self._fold_health(row)
                            continue
                        key = self._key_of(row)
                    except (ValueError, TypeError):
                        self.skipped_lines += 1
                        continue
                    if key is None:
                        self.skipped_lines += 1
                        continue
                    self._merge(key, row)
        except (OSError, UnicodeDecodeError) as exc:
            # Missing (FileNotFoundError), unreadable (PermissionError), a
            # directory, or invalid bytes:  never fatal to the relay.  The
            # audit log is the source of truth and rebuild repairs the index.
            self.load_error = type(exc).__name__

    @staticmethod
    def _key_of(row) -> tuple[str, int] | None:
        if not isinstance(row, dict):
            return None
        repo = row.get("repo")
        number = row.get("number")
        if not isinstance(repo, str) or not repo:
            return None
        if isinstance(number, bool) or not isinstance(number, int) or number < 1:
            return None
        # GitHub repo names are case-insensitive;  the key folds case while
        # the row keeps the creation-time spelling.
        return (repo.casefold(), number)

    def _merge(self, key: tuple[str, int], row: dict) -> bool:
        """Index one row:  confirmed beats unconfirmed, first confirmed wins.

        Returns True when the index changed.  A later unconfirmed row never
        downgrades a confirmed one;  a second confirmed row for the same PR
        (only possible after operator repair) keeps the first binding.
        """
        if row.get("confirmed") is True and row.get("source") == CONFIRMED:
            self._pending_creations.discard(row.get("corr"))
        existing = self._by_pr.get(key)
        if existing is None:
            self._by_pr[key] = row
            self._by_repo.setdefault(key[0], []).append(key)
            return True
        if existing.get("confirmed"):
            return False
        if row.get("confirmed"):
            self._by_pr[key] = row
            return True
        return False

    def append_row(self, row: dict) -> dict:
        """Append a validated row durably, then index it.  Raises OSError on write failure."""
        key = self._key_of(row)
        if key is None:
            raise ValueError("row needs a repo string and a positive integer number")
        return self._append_entry(row, key)

    def _fold_health(self, row):
        if (
            set(row) != {"owner_index_event", "corr"}
            or row["owner_index_event"] not in ("creation_pending", "creation_refused")
            or not isinstance(row["corr"], str)
            or not re.fullmatch(r"[0-9a-f]{32}", row["corr"])
        ):
            raise ValueError("invalid owner index health event")
        if row["owner_index_event"] == "creation_pending":
            self._pending_creations.add(row["corr"])
        else:
            self._pending_creations.discard(row["corr"])

    def begin_creation(self, corr):
        # Fence before GitHub, not after an append failure when disk may be down.
        row = {"owner_index_event": "creation_pending", "corr": corr}
        if not isinstance(corr, str) or not re.fullmatch(r"[0-9a-f]{32}", corr):
            raise ValueError("creation fence needs durable outcome correlation")
        self._append_entry(row)

    def refuse_creation(self, corr):
        # Only a definite no-create refusal may resolve without an owner row.
        self._append_entry({"owner_index_event": "creation_refused", "corr": corr})

    def _append_entry(self, row, key=None):
        line = json.dumps(row, sort_keys=True) + "\n"
        with self._lock:
            try:
                existed = os.path.exists(self._path)
                fd = os.open(self._path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                try:
                    if self._torn_tail:
                        _write_all(fd, b"\n")
                        self._torn_tail = False
                    _write_all(fd, line.encode())
                    os.fsync(fd)
                finally:
                    os.close(fd)
                if not existed:
                    # First create:  fsync the directory so the entry itself
                    # survives a crash, not only the file's bytes.
                    dir_fd = os.open(os.path.dirname(self._path) or ".", os.O_RDONLY)
                    try:
                        os.fsync(dir_fd)
                    finally:
                        os.close(dir_fd)
                if key is None:
                    self._fold_health(row)
                else:
                    self._merge(key, row)
                if self.load_error == "FileNotFoundError":
                    self.load_error = None
            except OSError:
                # A partial append/fsync failure also invalidates this live
                # snapshot. Restart revalidates the file and pending fences.
                self.load_error = "IndexWriteError"
                raise
        return row

    def record(
        self,
        *,
        seat: str,
        repo: str,
        number: int,
        head: str,
        base: str,
        corr: str | None = None,
        confirmed: bool = True,
        source: str = CONFIRMED,
        ts: str | None = None,
        origin: dict | None = None,
        origin_session_claim: str | None = None,
        origin_letter_claim: str | None = None,
        origin_generation_claim: int | None = None,
    ) -> dict:
        """Append one creation row durably, then index it.  Raises OSError on write failure."""
        return self.append_row(
            _row(
                seat=seat,
                repo=repo,
                number=number,
                head=head,
                base=base,
                corr=corr,
                confirmed=confirmed,
                source=source,
                ts=ts,
                origin=origin,
                origin_session_claim=origin_session_claim,
                origin_letter_claim=origin_letter_claim,
                origin_generation_claim=origin_generation_claim,
            )
        )

    def lookup(self, repo: str, number: int) -> dict | None:
        """The stored binding for one PR, or None when the relay never recorded it."""
        with self._lock:
            row = self._by_pr.get((repo.casefold(), number))
            return dict(row) if row is not None else None

    def for_seat(self, seat: str) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._by_pr.values() if r.get("seat") == seat]

    def for_repo(self, repo: str) -> list[dict]:
        folded = repo.casefold()
        with self._lock:
            return [dict(r) for r in self._by_pr.values() if r.get("repo", "").casefold() == folded]

    def protected_for_repo(self, repo: str) -> list[dict]:
        """Fail closed on incomplete census; never infer native claims."""
        from .service_read import origin_claims

        with self._lock:
            if self.load_error or self.skipped_lines or self._torn_tail or self._pending_creations:
                raise ValueError("protected creation index incomplete")
            rows = [
                dict(row)
                for row in self._by_pr.values()
                if row.get("repo", "").casefold() == repo.casefold()
            ]
            if len(rows) > 1000:
                raise ValueError("protected creation census exceeds bound")
            for row in rows:
                origin_claims(row)
            return rows

    def confirmed_for_repo(self, repo: str, limit: int) -> tuple[list[dict], bool]:
        """Bounded query: the confirmed open_pr rows for one repo, at most `limit`.

        Returns (rows, overflow).  `overflow` is True when more eligible rows
        exist than the bound allows, so the caller can fail closed.  The scan
        stops at the bound:  the per-repo history is never materialized in full
        and the refusal is decided before the rows are built.
        """
        if limit < 0:
            raise ValueError("limit must not be negative")
        folded = repo.casefold()
        rows: list[dict] = []
        overflow = False
        with self._lock:
            for key in self._by_repo.get(folded, ()):
                row = self._by_pr.get(key)
                if row is None:
                    continue
                if row.get("confirmed") is not True or row.get("source") != CONFIRMED:
                    continue
                if len(rows) >= limit:
                    overflow = True
                    break
                rows.append(dict(row))
        return rows, overflow

    def __len__(self) -> int:
        with self._lock:
            return len(self._by_pr)


def rebuild(audit_path: str, store: PROwnerStore) -> dict:
    """Fold every open_pr audit row into the store;  returns merge counts.

    Successful creations (result "ok" with a number) become confirmed rows;
    unknown outcomes carrying an unconfirmed_number become unconfirmed rows.
    Existing confirmed rows are never replaced;  a confirmed audit row that
    disagrees with an existing confirmed row's seat is counted under
    "conflicts" and left alone - attribution conflicts are surfaced, never
    silently resolved.  Existing store rows are otherwise preserved:  rebuild
    only adds or upgrades.
    """
    counts = {"audit_rows": 0, "added": 0, "skipped": 0, "conflicts": 0}
    with open(audit_path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if not isinstance(row, dict) or row.get("action") != "open_pr":
                continue
            seat = row.get("seat")
            repo = row.get("repo")
            if not all(isinstance(v, str) and v for v in (seat, repo)):
                continue
            # Unknown-outcome audit rows carry no head/base (their detail is
            # the failure classification);  an unconfirmed backfill row
            # records the branches as empty rather than guessing.
            head = row.get("head") if isinstance(row.get("head"), str) else ""
            base = row.get("base") if isinstance(row.get("base"), str) else ""
            counts["audit_rows"] += 1
            result = row.get("result")
            number = row.get("number")
            confirmed = result == "ok"
            if not confirmed:
                number = row.get("unconfirmed_number")
            if isinstance(number, bool) or not isinstance(number, int) or number < 1:
                counts["skipped"] += 1
                continue
            existing = store.lookup(repo, number)
            if existing is not None and (existing.get("confirmed") or not confirmed):
                if confirmed and existing.get("confirmed") and existing.get("seat") != seat:
                    counts["conflicts"] += 1
                counts["skipped"] += 1
                continue
            # The audit row's ts is the true creation time;  record() would
            # stamp a fresh one, so pass it through.  Claims and attestation
            # survive a rebuild only when the existing store row already holds
            # them (an unconfirmed -> confirmed upgrade):  the audit row keeps
            # only hashes, so a row re-added from audit alone is unbound.
            keep = existing if existing is not None else {}
            store.record(
                seat=seat,
                repo=repo,
                number=number,
                head=head,
                base=base,
                corr=row.get("outcome_corr"),
                confirmed=confirmed,
                source=BACKFILL,
                ts=row.get("ts") if isinstance(row.get("ts"), str) else None,
                origin_session_claim=keep.get("origin_session_claim"),
                origin_letter_claim=keep.get("origin_letter_claim"),
                origin_generation_claim=keep.get("origin_generation_claim"),
            )
            counts["added"] += 1
    return counts


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="git_relay.pr_owners",
        description="Query or rebuild the relay's PR-ownership index.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    p_rebuild = sub.add_parser("rebuild", help="fold an audit log into the index")
    p_rebuild.add_argument("--audit", required=True)
    p_rebuild.add_argument("--store", required=True)
    p_lookup = sub.add_parser("lookup", help="who created one PR")
    p_lookup.add_argument("--store", required=True)
    p_lookup.add_argument("repo")
    p_lookup.add_argument("number", type=int)
    p_list = sub.add_parser("list", help="rows for one seat or repo")
    p_list.add_argument("--store", required=True)
    group = p_list.add_mutually_exclusive_group(required=True)
    group.add_argument("--seat")
    group.add_argument("--repo")
    args = parser.parse_args(argv)
    if args.command == "rebuild":
        store = PROwnerStore(args.store)
        counts = rebuild(args.audit, store)
        # A nonzero conflicts count means an existing confirmed binding
        # disagreed with the audit log:  investigate, never auto-resolve.
        print(json.dumps(counts, sort_keys=True))
        return 1 if counts["conflicts"] else 0
    store = PROwnerStore(args.store)
    if args.command == "lookup":
        row = store.lookup(args.repo, args.number)
        # "owner" names a seat only for a CONFIRMED creation.  An unconfirmed
        # row means "this seat may have created it";  consumers must check
        # "confirmed", never the presence of a record.  No row is the honest
        # answer when the relay has no record of creating the PR.
        print(
            json.dumps(
                {
                    "owner": row["seat"] if row and row.get("confirmed") else None,
                    "confirmed": bool(row and row.get("confirmed")),
                    "record": row,
                },
                sort_keys=True,
            )
        )
        return 0
    rows = store.for_seat(args.seat) if args.seat else store.for_repo(args.repo)
    rows.sort(key=lambda r: (r.get("repo", ""), r.get("number", 0)))
    print(json.dumps(rows, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
