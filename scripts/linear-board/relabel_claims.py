#!/usr/bin/env python3
"""One-time cutover step:  give the import's live claims a claimed:<SEAT> label.

The import put seat:<TAG> labels on reporters as well as claimants, and wrote
the claimant only in each issue's provenance footer ("addressed by x").  The
board CLI therefore reads a claim from a claimed:<SEAT> label, and falls back
to the footer on `claim` and `show`.  `list` and `list --mine` see labels only,
so until this script runs they show no claimant for imported In Progress and
In Review issues.

It is purely additive:  it adds claimed:<SEAT> and removes nothing, so
attribution (seat: labels) stays as imported.  It skips issues that already
carry a claimed: label, so it is idempotent and safe to resume after an
interruption.  It uses the board CLI's own Fleet credentials, token cache,
rate-limit handling and loopback-only URL rule, never a personal or admin key.

Dry run by default.  --apply is a write, so it needs a bearer-verified seat, as
every write does.  Owner-approved cutover step (README, "Cutover relabel"); do
not run it before the owner says so.

  python3 scripts/linear-board/relabel_claims.py            # print the plan
  python3 scripts/linear-board/relabel_claims.py --apply
"""

from __future__ import annotations

import argparse
import importlib.machinery
import importlib.util
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
LIVE_STATES = ("In Progress", "In Review")


def load_board():
    """The board CLI module, loaded once per process and cached in sys.modules.

    The cache is what makes the board's own test seam reachable from here:  the board exposes
    BEARER_TRANSPORT for tests to patch after import, and a caller that loaded a fresh copy per
    call would drop that patch before the write.
    """
    mod = sys.modules.get("board_cli")
    if mod is not None:
        return mod
    loader = importlib.machinery.SourceFileLoader("board_cli", str(HERE / "board"))
    spec = importlib.util.spec_from_loader("board_cli", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    sys.modules["board_cli"] = mod
    return mod


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Add claimed:<SEAT> labels to imported live claims.")
    ap.add_argument(
        "--apply", action="store_true", help="write the labels (default: print the plan only)"
    )
    ap.add_argument(
        "--states", default=",".join(LIVE_STATES), help="comma-separated workflow states to cover"
    )
    ap.add_argument(
        "--limit", type=int, default=0, help="stop after this many writes (0 = no limit)"
    )
    a = ap.parse_args(argv)
    B = load_board()
    # A write needs a bearer-verified seat, as every write does:  the script never takes an identity from
    # the environment and never guesses one.  A dry run needs no seat, because it writes nothing, so it
    # must not verify the bearer at all:  that call is a network round trip and can refuse, and a plan
    # that dies before it prints is not a plan.
    seat = B.current_seat(required=True) if a.apply else ""
    states = B.split_csv(a.states)
    # The seat is the gate, not the identity:  the script acts as the shared Fleet app, or as the cloud
    # pair the host operator chose, and never as a seat's own app.  So the identity resolves the way a
    # read resolves it.
    ident = B.resolve_identity("") if seat else None
    if ident is not None and ident.kind == "env":
        # The cloud pair may hold the seat's app, which is that host's deliberate configuration:  accept
        # that name as well as Fleet, so the check below reads as the swap check it is and the pair the
        # README promises still works.  A pair holding another seat's app is still refused.
        ident.accept = tuple(dict.fromkeys((*ident.accept, *B.seat_app_names(seat))))
    auth = B.Auth(B.endpoint("LINEAR_BOARD_TOKEN_URL", B.TOKEN_URL), ident)
    L = B.Linear(auth, B.endpoint("LINEAR_BOARD_API_URL", B.GRAPHQL_URL))
    if a.apply:
        B.check_write_identity(L, auth)
    query = (
        """query BoardRelabel($filter: IssueFilter, $first: Int!, $after: String) {
      issues(filter: $filter, first: $first, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes { ...BoardIssue description }
      }
    }"""
        + B.F_ISSUE
    )
    todo, marked, unknown = [], 0, []
    try:
        for n in L.paginate(
            query, {"filter": {"state": {"name": {"in": states}}}}, ("issues",), 50
        ):
            if B.claim_markers(n):
                marked += 1
                continue
            seats = B.footer_claimants(n.get("description"))
            if seats:
                todo.append((n, seats))
            else:
                unknown.append(n)
        print(
            f"{len(todo)} issue(s) to mark, {marked} already marked, {len(unknown)} with no claimant "
            f"in their footer (states: {', '.join(states)})"
        )
        for n, seats in todo:
            print(
                f"  {n['identifier']:<9} {(n.get('state') or {}).get('name', ''):<12} -> "
                f"{', '.join(B.CLAIM_PREFIX + s for s in seats)}"
            )
        for n in unknown:
            print(
                f"  {n['identifier']:<9} {(n.get('state') or {}).get('name', ''):<12} no claimant "
                "(left as it is)"
            )
        if not a.apply:
            print("dry run: nothing written.  Rerun with --apply to add the labels.")
            return 0
        ws = B.Workspace(L)
        done = 0
        for n, seats in todo:
            if a.limit and done >= a.limit:
                print(
                    f"stopped at --limit {a.limit}; rerun to continue (marked issues are skipped)."
                )
                break
            ids = [ws.label_id(f"{B.CLAIM_PREFIX}{s}", create=True) for s in seats]
            current = {x["id"] for x in (n.get("labels") or {}).get("nodes") or []}
            add = [i for i in ids if i and i not in current]
            if add:
                L.call(B.M_UPDATE, {"id": n["id"], "input": {"addedLabelIds": add}})
            done += 1
            if done % 25 == 0:
                print(f"  marked {done} of {len(todo)}", file=sys.stderr, flush=True)
        print(f"marked {done} issue(s).")
    except B.GqlError as e:
        B.die(f"Linear error: {e}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
