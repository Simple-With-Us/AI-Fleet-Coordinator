#!/usr/bin/env python3
"""Hold evaluation for fleet PRs: is this PR merge-held, and who owns each hold?

Ownership model (epoch pairing per label name):
  Scan the PR timeline chronologically - fully paginated, sorted by
  (created_at, id) because API order is not trusted and time ties happen.
    `labeled` with no open hold        -> opens a hold; owner = actor.id
    `labeled` while a hold is open     -> repair/duplicate (e.g. the guard
        re-applying after an unauthorized removal); ownership UNCHANGED.
        A repair event never transfers or resets ownership.
    `unlabeled` by the open hold's owner (actor.id match) -> hold closed
    `unlabeled` by anyone else         -> illegitimate removal: hold stays
        open under the ORIGINAL owner; remover recorded
    `unlabeled` with no visible open hold -> application predates the
        recoverable window: hold opens as owner UNKNOWN
    label present with zero timeline events -> hold ACTIVE, owner UNKNOWN
  UNKNOWN-owner holds are clearable by no one automatically; they escalate
  (red gate + notice) and are never re-armed - re-applying one would make
  it releaseable by nobody (Kody r4373587152).
  Logins are display only; all matching is by numeric actor.id (renames safe).

State comes from the timeline API at evaluation time, never from a webhook
payload.  Evaluation errors raise; main() reports state=error and exits 3
(fail closed for ERRORS only - see report for the async-placement window,
which no label/check design closes without a merge queue).

Usage: hold_guard.py OWNER/REPO PR_NUMBER
Stdout: {"state": "held"|"clear"|"error", "holds": [...]}
Exit: 0 evaluated (held or clear); 3 evaluation error.
"""

import json
import re
import subprocess
import sys

# Grounded hold families: the fleet's documented do-not-automerge /
# needs-human-merge, plus the literal do-not-merge and do-not-auto-merge (owner, Oct 27
# 2026) families Jay named.
# Per-setter variants carry a :<login> suffix (display hint only).
HOLD_RE = re.compile(r"^(do-not-merge(:.*)?|do-not-auto-?merge(:.*)?|needs-human-merge)$")


def gh_json(args):
    # Kody r4373592227/r4373586898: bound every gh call; a hung child maps to
    # 124 and a missing executable to 127 (both nonzero -> callers fail
    # closed).  Kody r4373586704/r4373592031: error text carries normalized
    # metadata only - never the argv or the API's stderr, which can echo
    # response bodies, request details or tokens into logs and check output.
    try:
        p = subprocess.run(["gh"] + args, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        print(json.dumps({"state": "error", "error": "gh call exceeded 30s timeout"}))
        sys.exit(124)
    except FileNotFoundError:
        print(json.dumps({"state": "error", "error": "gh executable not found"}))
        sys.exit(127)
    if p.returncode != 0:
        raise RuntimeError(f"gh call failed rc={p.returncode}")
    try:
        value = json.loads(p.stdout)
    except json.JSONDecodeError:
        # precise failure instead of a misleading partial consume
        raise RuntimeError(f"gh rc=0 but stdout is not JSON ({len(p.stdout)} bytes)")
    if not isinstance(value, (list, dict)):
        raise RuntimeError("gh returned an unexpected JSON shape")
    return value


def evaluate(repo, pr, runner=gh_json):
    labels = runner(["api", f"repos/{repo}/issues/{pr}/labels"])
    current = {l["name"] for l in labels if HOLD_RE.match(l.get("name", ""))}
    # --slurp flattens --paginate's per-page arrays into one.
    pages = runner(["api", "--paginate", "--slurp", f"repos/{repo}/issues/{pr}/events"])
    evs = [
        e
        for page in pages
        for e in page
        if e.get("event") in ("labeled", "unlabeled")
        and HOLD_RE.match((e.get("label") or {}).get("name", ""))
    ]
    evs.sort(key=lambda e: (e.get("created_at") or "", e.get("id") or 0))
    by_label = {}
    for e in evs:
        by_label.setdefault(e["label"]["name"], []).append(e)

    holds = []
    for name in sorted(current | set(by_label)):
        hist = by_label.get(name, [])
        if not hist:
            if name in current:
                holds.append(
                    {
                        "label": name,
                        "owner_id": None,
                        "owner_login": None,
                        "unknown": True,
                        "illegitimate_removals": [],
                        "needs_reapply": False,
                    }
                )
            continue
        open_hold = None
        illegit = []
        for e in hist:
            a = e.get("actor") or {}
            if e["event"] == "labeled":
                if open_hold is None:
                    open_hold = {
                        "owner_id": a.get("id"),
                        "owner_login": a.get("login"),
                        "unknown": a.get("id") is None,
                    }
                # labeled while open: repair/duplicate, ownership unchanged
            else:  # unlabeled
                if open_hold is None:
                    open_hold = {"owner_id": None, "owner_login": None, "unknown": True}
                    illegit.append(a.get("login"))
                elif open_hold["owner_id"] is not None and a.get("id") == open_hold["owner_id"]:
                    open_hold = None  # released by owner
                    illegit = []
                else:
                    illegit.append(a.get("login"))
        if open_hold is not None:
            holds.append(
                {
                    "label": name,
                    **open_hold,
                    "illegitimate_removals": illegit,
                    # Kody r4373587152: UNKNOWN-owner holds are NEVER re-armed -
                    # a re-applied unknown-owner hold is releaseable by no one
                    # (owner_id is None) and becomes a permanent block.  They
                    # stay open in evaluation (gate red) and escalate via the
                    # check summary; only a human resolves them.
                    "needs_reapply": (name not in current) and not open_hold["unknown"],
                }
            )
    return {"state": "held" if holds else "clear", "holds": holds}


def main():
    repo, pr = sys.argv[1], sys.argv[2]
    try:
        if len(sys.argv) == 4 and sys.argv[3] == "--hold-signal":
            labels = gh_json(["api", f"repos/{repo}/issues/{pr}/labels"])
            if not isinstance(labels, list) or any(
                not isinstance(label, dict) or not isinstance(label.get("name"), str)
                for label in labels
            ):
                raise RuntimeError("Invalid current label evidence")
            print(json.dumps({"held": any(HOLD_RE.match(label["name"]) for label in labels)}))
            return
        result = evaluate(repo, pr)
    except Exception as exc:  # fail closed on evaluation error only
        print(json.dumps({"state": "error", "error": str(exc)[:800]}))
        sys.exit(3)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
