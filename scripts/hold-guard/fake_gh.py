#!/usr/bin/env python3
"""Test double for the gh CLI.  Serves canned API responses from the scenario
file at $FAKE_GH_SCENARIO and appends every invocation (argv + stdin) as a
JSON line to $FAKE_GH_LOG.  Not shipped; used by test_guard_integration.py."""
import json, os, sys

scenario = json.load(open(os.environ['FAKE_GH_SCENARIO']))
log_path = os.environ['FAKE_GH_LOG']
argv = sys.argv[1:]

stdin_data = ''
if '--input' in argv:
    stdin_data = sys.stdin.read()

with open(log_path, 'a') as fh:
    fh.write(json.dumps({'argv': argv, 'stdin': stdin_data}) + '\n')

def out(obj):
    print(json.dumps(obj))
    sys.exit(0)

def fail(key):
    f = scenario.get('fail', {}).get(key)
    if f:
        sys.stderr.write(f.get('stderr', 'boom') + '\n')
        sys.exit(f.get('rc', 1))

if argv and argv[0] == 'api':
    path = next((a for a in argv[1:] if a.startswith('repos/')), '')
    # error injection keys: 'labels/<n>', 'events/<n>', 'check-runs', 'pulls/<sha>'
    parts = path.split('/')
    if path.endswith('/labels') and '-X' not in argv:
        n = parts[4]; fail(f'labels/{n}')
        out(scenario.get('labels', {}).get(n, []))
    if path.endswith('/labels') and '-X' in argv:
        out({})
    if path.endswith('/events'):
        n = parts[4]; fail(f'events/{n}')
        out(scenario.get('events', {}).get(n, [[]]))
    if '/commits/' in path and path.endswith('/pulls'):
        sha = parts[4]; fail(f'pulls/{sha}')
        pulls = scenario.get('pulls', {}).get(sha, [])
        if '--jq' in argv and argv[argv.index('--jq') + 1] == '.[].number':
            for p in pulls:
                print(p['number'])
            sys.exit(0)
        out(pulls)
    if '/commits/' in path and 'check-runs' in path and '-X' not in argv:
        sha = parts[4]; fail(f'sha-checks/{sha}')
        runs = scenario.get('check_runs', {}).get(sha, [])
        if '--jq' in argv and argv[argv.index('--jq') + 1] == '.check_runs[].id':
            for r in runs:
                print(r['id'])
            sys.exit(0)
        out({'check_runs': runs})
    if '/check-runs/' in path and '-X' in argv and 'PATCH' in argv:
        run_id = int(path.split('/check-runs/')[1])
        for runs in scenario.get('check_runs', {}).values():
            for r in runs:
                if r['id'] == run_id:
                    if r.get('app', 'github-actions') != 'github-actions':
                        sys.stderr.write('Resource not accessible by integration\n')
                        sys.exit(1)
                    out(r)
        sys.stderr.write('Not Found\n')
        sys.exit(1)
    if path.endswith('/check-runs'):
        fail('check-runs')
        out({'id': 1001})
    out({})
if argv and argv[0] == 'pr':
    # `gh pr merge --auto --disable URL` exits 1 when nothing armed; scenario
    # controls via fail.pr_merge
    if argv[1] == 'merge':
        fail('pr_merge')
        out({})
    out({})
out({})
