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
  UNKNOWN-owner holds are clearable by no one automatically; they escalate.
  Logins are display only; all matching is by numeric actor.id (renames safe).

State comes from the timeline API at evaluation time, never from a webhook
payload.  Evaluation errors raise; main() reports state=error and exits 3
(fail closed for ERRORS only - see report for the async-placement window,
which no label/check design closes without a merge queue).

Usage: hold_guard.py OWNER/REPO PR_NUMBER
Stdout: {"state": "held"|"clear"|"error", "holds": [...]}
Exit: 0 evaluated (held or clear); 3 evaluation error.
"""
import json, re, subprocess, sys

# Grounded hold families: the fleet's documented do-not-automerge /
# needs-human-merge, plus the literal do-not-merge family Jay named.
# Per-setter variants carry a :<login> suffix (display hint only).
HOLD_RE = re.compile(
    r'^(do-not-merge(:.*)?|do-not-automerge(:.*)?|needs-human-merge)$')


def gh_json(args):
    p = subprocess.run(['gh'] + args, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args)} rc={p.returncode}: {p.stderr.strip()[:500]}")
    return json.loads(p.stdout)


def evaluate(repo, pr, runner=gh_json):
    labels = runner(['api', f'repos/{repo}/issues/{pr}/labels'])
    current = {l['name'] for l in labels if HOLD_RE.match(l.get('name', ''))}
    # --slurp flattens --paginate's per-page arrays into one.
    pages = runner(['api', '--paginate', '--slurp',
                    f'repos/{repo}/issues/{pr}/events'])
    evs = [e for page in pages for e in page
           if e.get('event') in ('labeled', 'unlabeled')
           and HOLD_RE.match((e.get('label') or {}).get('name', ''))]
    evs.sort(key=lambda e: (e.get('created_at') or '', e.get('id') or 0))
    by_label = {}
    for e in evs:
        by_label.setdefault(e['label']['name'], []).append(e)

    holds = []
    for name in sorted(current | set(by_label)):
        hist = by_label.get(name, [])
        if not hist:
            if name in current:
                holds.append({'label': name, 'owner_id': None,
                              'owner_login': None, 'unknown': True,
                              'illegitimate_removals': [],
                              'needs_reapply': False})
            continue
        open_hold = None
        illegit = []
        for e in hist:
            a = e.get('actor') or {}
            if e['event'] == 'labeled':
                if open_hold is None:
                    open_hold = {'owner_id': a.get('id'),
                                 'owner_login': a.get('login'),
                                 'unknown': a.get('id') is None}
                # labeled while open: repair/duplicate, ownership unchanged
            else:  # unlabeled
                if open_hold is None:
                    open_hold = {'owner_id': None, 'owner_login': None,
                                 'unknown': True}
                    illegit.append(a.get('login'))
                elif (open_hold['owner_id'] is not None
                      and a.get('id') == open_hold['owner_id']):
                    open_hold = None  # released by owner
                    illegit = []
                else:
                    illegit.append(a.get('login'))
        if open_hold is not None:
            holds.append({'label': name, **open_hold,
                          'illegitimate_removals': illegit,
                          'needs_reapply': name not in current})
    return {'state': 'held' if holds else 'clear', 'holds': holds}


def main():
    repo, pr = sys.argv[1], sys.argv[2]
    try:
        result = evaluate(repo, pr)
    except Exception as exc:  # fail closed on evaluation error only
        print(json.dumps({'state': 'error', 'error': str(exc)[:800]}))
        sys.exit(3)
    print(json.dumps(result))


if __name__ == '__main__':
    main()
