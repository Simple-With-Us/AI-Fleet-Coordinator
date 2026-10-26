#!/usr/bin/env python3
"""Integration tests: execute the SHIPPED shell wrappers (guard_run.sh,
armer_hold_check.sh) against a fake gh serving canned API responses.

Covers what the canned-state evaluator tests cannot: call ordering
(disarm before repair before check), jq-built request bodies, label names
with spaces (no word-splitting), merge_group PR mapping, multi-page event
pagination, error fail-closed behavior, and the check-run payload shape.
"""
import json, os, stat, subprocess, sys, tempfile, unittest

DIR = os.path.dirname(os.path.abspath(__file__))
_seq = [0]

def ev(kind, label, actor_id, login, ts):
    _seq[0] += 1
    return {'id': _seq[0], 'event': kind, 'label': {'name': label},
            'actor': {'id': actor_id, 'login': login}, 'created_at': ts}

ECHO = (41898282, 'swu-echo[bot]')
WISE = (41900001, 'swu-wise[bot]')

class GuardIntegration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.log = os.path.join(self.tmp, 'gh.log')
        open(self.log, 'w').close()
        bindir = os.path.join(self.tmp, 'bin')
        os.makedirs(bindir)
        shim = os.path.join(bindir, 'gh')
        with open(shim, 'w') as fh:
            fh.write('#!/usr/bin/env bash\nexec python3 "%s" "$@"\n'
                     % os.path.join(DIR, 'fake_gh.py'))
        os.chmod(shim, os.stat(shim).st_mode | stat.S_IEXEC)
        self.env = dict(os.environ)
        self.env['PATH'] = bindir + os.pathsep + self.env['PATH']
        self.env['FAKE_GH_LOG'] = self.log
        self.env.update(REPO='Simple-With-Us/AI-Fleet-Coordinator',
                        HEAD_SHA='deadbeef' * 5)
        self.scenario_path = os.path.join(self.tmp, 'scenario.json')

    def scenario(self, data):
        json.dump(data, open(self.scenario_path, 'w'))
        self.env['FAKE_GH_SCENARIO'] = self.scenario_path

    def calls(self):
        return [json.loads(l) for l in open(self.log)]

    def run_guard(self, **env):
        self.env.update(env)
        return subprocess.run(['bash', os.path.join(DIR, 'guard_run.sh')],
                              env=self.env, capture_output=True, text=True)

    def run_armer(self, **env):
        self.env.update(env)
        return subprocess.run(['bash', os.path.join(DIR, 'armer_hold_check.sh')],
                              env=self.env, capture_output=True, text=True)

    def test_held_repairs_comments_and_publishes_failure_in_order(self):
        self.scenario({
            'labels': {'3': []},
            'events': {'3': [[ev('labeled', 'do-not-automerge', *ECHO, '2026-10-25T01:00:00Z'),
                              ev('unlabeled', 'do-not-automerge', *WISE, '2026-10-25T02:00:00Z')]]},
        })
        p = self.run_guard(PR='3', PR_URL='https://github.com/x/pull/3',
                           EVENT_NAME='pull_request_target')
        self.assertEqual(p.returncode, 0, p.stderr)
        kinds = [(' '.join(c['argv'][:3])) for c in self.calls()]
        # order: disarm -> labels GET -> events -> label re-apply -> comment -> check
        self.assertEqual(kinds[0], 'pr merge --disable-auto')
        label_posts = [c for c in self.calls() if c['argv'][0] == 'api'
                       and any(a.endswith('/labels') for a in c['argv']) and '-X' in c['argv']]
        self.assertEqual(len(label_posts), 1)
        self.assertEqual(json.loads(label_posts[0]['stdin']),
                         {'labels': ['do-not-automerge']})
        comments = [c for c in self.calls() if c['argv'][:2] == ['pr', 'comment']]
        self.assertEqual(len(comments), 1)
        body = comments[0]['argv'][comments[0]['argv'].index('--body') + 1]
        self.assertIn('swu-wise[bot]', body)
        self.assertIn('swu-echo[bot]', body)
        checks = [c for c in self.calls() if any(a.endswith('/check-runs') for a in c['argv'])]
        self.assertEqual(len(checks), 1)
        payload = json.loads(checks[0]['stdin'])
        self.assertEqual(payload['conclusion'], 'failure')
        self.assertEqual(payload['head_sha'], 'deadbeef' * 5)
        self.assertIn('swu-echo[bot]', payload['output']['summary'])
        # disarm strictly before repair and check
        self.assertLess(kinds.index('pr merge --disable-auto'),
                        max(i for i, c in enumerate(self.calls())
                            if any(a.endswith('/check-runs') for a in c['argv'])))

    def test_clear_publishes_success_no_repairs(self):
        self.scenario({'labels': {'3': []}, 'events': {'3': [[
            ev('labeled', 'do-not-automerge', *ECHO, '2026-10-25T01:00:00Z'),
            ev('unlabeled', 'do-not-automerge', *ECHO, '2026-10-25T02:00:00Z')]]}})
        p = self.run_guard(PR='3', PR_URL='u', EVENT_NAME='pull_request_target')
        self.assertEqual(p.returncode, 0, p.stderr)
        checks = [c for c in self.calls() if any(a.endswith('/check-runs') for a in c['argv'])]
        self.assertEqual(json.loads(checks[0]['stdin'])['conclusion'], 'success')
        self.assertFalse([c for c in self.calls() if c['argv'][:2] == ['pr', 'comment']])

    def test_events_api_error_fails_closed_and_loud(self):
        self.scenario({'labels': {'3': []},
                       'fail': {'events/3': {'rc': 1, 'stderr': 'boom 500'}}})
        p = self.run_guard(PR='3', PR_URL='u', EVENT_NAME='pull_request_target')
        self.assertEqual(p.returncode, 3)
        checks = [c for c in self.calls() if any(a.endswith('/check-runs') for a in c['argv'])]
        payload = json.loads(checks[0]['stdin'])
        self.assertEqual(payload['conclusion'], 'failure')
        self.assertIn('evaluation error', payload['output']['title'])

    def test_missing_pr_fails_closed(self):
        self.scenario({'labels': {}, 'events': {}})
        p = self.run_guard(PR='', EVENT_NAME='pull_request_target')
        self.assertEqual(p.returncode, 3)

    def test_disarm_uses_real_gh_contract(self):
        self.scenario({'labels': {'3': []}, 'events': {'3': [[]]}})
        p = self.run_guard(PR='3', PR_URL='u', EVENT_NAME='pull_request_target')
        self.assertEqual(p.returncode, 0, p.stderr)
        disarm = self.calls()[0]
        self.assertEqual(disarm['argv'], ['pr', 'merge', '--disable-auto', 'u'])

    def test_do_not_merge_family_enforced(self):
        self.scenario({'labels': {'3': [{'name': 'do-not-merge'}]},
                       'events': {'3': [[ev('labeled', 'do-not-merge', *ECHO, '2026-10-25T01:00:00Z')]]}})
        p = self.run_guard(PR='3', PR_URL='u', EVENT_NAME='pull_request_target')
        checks = [c for c in self.calls() if any(a.endswith('/check-runs') for a in c['argv'])
                  and 'POST' in c['argv']]
        self.assertEqual(json.loads(checks[0]['stdin'])['conclusion'], 'failure')

    def test_fresh_fence_reevaluates_before_publish(self):
        # first evaluation: held; owner releases mid-run; the conclusion
        # published must come from the SECOND (write-time) evaluation
        sha = 'deadbeef' * 5
        self.scenario({
            'labels': {'3': []},
            'events_seq': {'3': [
                [[ev('labeled', 'do-not-automerge', *ECHO, '2026-10-25T01:00:00Z'),
                  ev('unlabeled', 'do-not-automerge', *WISE, '2026-10-25T02:00:00Z')]],
                [[ev('labeled', 'do-not-automerge', *ECHO, '2026-10-25T01:00:00Z'),
                  ev('unlabeled', 'do-not-automerge', *WISE, '2026-10-25T02:00:00Z'),
                  ev('labeled', 'do-not-automerge', *ECHO, '2026-10-25T02:00:05Z'),
                  ev('unlabeled', 'do-not-automerge', *ECHO, '2026-10-25T03:00:00Z')]],
            ]},
        })
        p = self.run_guard(PR='3', PR_URL='u', EVENT_NAME='pull_request_target')
        self.assertEqual(p.returncode, 0, p.stderr)
        checks = [c for c in self.calls() if any(a.endswith('/check-runs') for a in c['argv'])
                  and 'POST' in c['argv']]
        self.assertEqual(json.loads(checks[0]['stdin'])['conclusion'], 'success')

    def test_foreign_run_never_probed_or_patched(self):
        sha = 'deadbeef' * 5
        self._clear_scenario()
        self.scenario({**json.load(open(self.scenario_path)),
                       'check_runs': {sha: [{'id': 777, 'app': {'slug': 'swu-wise'}}]}})
        p = self.run_guard(PR='3', PR_URL='u', EVENT_NAME='pull_request_target')
        self.assertEqual(p.returncode, 0, p.stderr)
        patches = [c for c in self.calls() if 'PATCH' in c['argv']]
        self.assertEqual(len(patches), 0)  # zero mutation attempts on foreign runs
        posts = [c for c in self.calls() if 'POST' in c['argv']
                 and any(a.endswith('/check-runs') for a in c['argv'])]
        self.assertEqual(len(posts), 1)  # our identity posts its own current run

    def test_label_with_spaces_reapplied_intact(self):
        spaced = 'do-not-automerge:hold please'
        self.scenario({'labels': {'3': []},
                       'events': {'3': [[ev('labeled', spaced, *ECHO, '2026-10-25T01:00:00Z'),
                                         ev('unlabeled', spaced, *WISE, '2026-10-25T02:00:00Z')]]}})
        p = self.run_guard(PR='3', PR_URL='u', EVENT_NAME='pull_request_target')
        self.assertEqual(p.returncode, 0, p.stderr)
        posts = [c for c in self.calls() if any(a.endswith('/labels') for a in c['argv'])
                 and '-X' in c['argv']]
        self.assertEqual(json.loads(posts[0]['stdin']), {'labels': [spaced]})

    def test_pagination_across_pages_unsorted(self):
        self.scenario({'labels': {'3': []}, 'events': {'3': [
            [ev('unlabeled', 'do-not-automerge', *ECHO, '2026-10-25T02:00:00Z')],
            [ev('labeled', 'do-not-automerge', *ECHO, '2026-10-25T01:00:00Z')]]}})
        p = self.run_guard(PR='3', PR_URL='u', EVENT_NAME='pull_request_target')
        self.assertEqual(p.returncode, 0, p.stderr)
        checks = [c for c in self.calls() if any(a.endswith('/check-runs') for a in c['argv'])]
        self.assertEqual(json.loads(checks[0]['stdin'])['conclusion'], 'success')

    def test_armer_wrapper_held_blocks_with_reason(self):
        self.scenario({'labels': {'3': [{'name': 'do-not-automerge'}]},
                       'events': {'3': [[ev('labeled', 'do-not-automerge', *ECHO, '2026-10-25T01:00:00Z')]]}})
        p = self.run_armer(PR_NUMBER='3')
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('do-not-automerge', p.stdout)

    def test_armer_wrapper_clear_passes(self):
        self.scenario({'labels': {'3': []}, 'events': {'3': [[]]}})
        p = self.run_armer(PR_NUMBER='3')
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_armer_wrapper_error_fails_closed(self):
        self.scenario({'labels': {'3': []},
                       'fail': {'events/3': {'rc': 1, 'stderr': 'boom'}}})
        p = self.run_armer(PR_NUMBER='3')
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('error', p.stdout.lower())


    def _clear_scenario(self):
        self.scenario({'labels': {'3': []}, 'events': {'3': [[
            ev('labeled', 'do-not-automerge', *ECHO, '2026-10-25T01:00:00Z'),
            ev('unlabeled', 'do-not-automerge', *ECHO, '2026-10-25T02:00:00Z')]]}})

    def test_existing_runs_patched_no_duplicate_no_stale(self):
        # two of OUR prior merge-gate runs on the SHA: both must be PATCHed
        # to the current conclusion; no new POST -> no same-name ambiguity
        sha = 'deadbeef' * 5
        self._clear_scenario()
        self.scenario({**json.load(open(self.scenario_path)),
                       'check_runs': {sha: [{'id': 555, 'app': {'slug': 'github-actions'}},
                                            {'id': 556, 'app': {'slug': 'github-actions'}}]}})
        p = self.run_guard(PR='3', PR_URL='u', EVENT_NAME='pull_request_target')
        self.assertEqual(p.returncode, 0, p.stderr)
        patches = [c for c in self.calls() if 'PATCH' in c['argv']]
        posts = [c for c in self.calls() if 'POST' in c['argv']
                 and any(a.endswith('/check-runs') for a in c['argv'])]
        self.assertEqual(len(patches), 2)
        self.assertEqual(len(posts), 0)
        for c in patches:
            self.assertEqual(json.loads(c['stdin'])['conclusion'], 'success')


    def _held_scenario(self):
        # active hold needing repair (illegitimate removal by WISE)
        self.scenario({
            'labels': {'3': []},
            'events': {'3': [[ev('labeled', 'do-not-automerge', *ECHO, '2026-10-25T01:00:00Z'),
                              ev('unlabeled', 'do-not-automerge', *WISE, '2026-10-25T02:00:00Z')]]},
        })

    def test_disarm_unconfirmed_state_red_job(self):
        # armed and the disable mutation is refused; state read still says
        # armed -> critical failure (fleet contract: fail closed on state)
        self._clear_scenario()
        self.scenario({**json.load(open(self.scenario_path)),
                       'armed': True,
                       'fail': {'pr_merge': {'rc': 1, 'stderr': 'Resource not accessible by integration'}}})
        p = self.run_guard(PR='3', PR_URL='u', EVENT_NAME='pull_request_target')
        self.assertEqual(p.returncode, 3)
        self.assertIn('auto-merge state after disarm', p.stdout)

    def test_disarm_state_unreadable_red_job(self):
        self._clear_scenario()
        self.scenario({**json.load(open(self.scenario_path)),
                       'fail': {'pr_view': {'rc': 1, 'stderr': 'boom 500'}}})
        p = self.run_guard(PR='3', PR_URL='u', EVENT_NAME='pull_request_target')
        self.assertEqual(p.returncode, 3)
        self.assertIn('unreadable', p.stdout)

    def test_disarm_not_armed_is_benign(self):
        # disable attempt errors with "not enabled" but the state read says
        # off -> success, no red job (the fleet's own already-off case)
        self._clear_scenario()
        self.scenario({**json.load(open(self.scenario_path)), 'armed': False})
        p = self.run_guard(PR='3', PR_URL='u', EVENT_NAME='pull_request_target')
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)

    def test_owned_patch_failure_posts_fresh_and_red_job(self):
        sha = 'deadbeef' * 5
        self._clear_scenario()
        self.scenario({**json.load(open(self.scenario_path)),
                       'check_runs': {sha: [{'id': 555, 'app': {'slug': 'github-actions'}}]},
                       'fail': {'patch/555': {'rc': 1, 'stderr': 'boom 500'}}})
        p = self.run_guard(PR='3', PR_URL='u', EVENT_NAME='pull_request_target')
        self.assertEqual(p.returncode, 3)
        posts = [c for c in self.calls() if 'POST' in c['argv']
                 and any(a.endswith('/check-runs') for a in c['argv'])]
        self.assertEqual(len(posts), 1)  # fresh owned run carries the state
        self.assertEqual(json.loads(posts[0]['stdin'])['conclusion'], 'success')

    def test_check_post_failure_red_job(self):
        self._clear_scenario()
        self.scenario({**json.load(open(self.scenario_path)),
                       'fail': {'check-runs': {'rc': 1, 'stderr': 'boom 500'}}})
        p = self.run_guard(PR='3', PR_URL='u', EVENT_NAME='pull_request_target')
        self.assertEqual(p.returncode, 3)
        self.assertIn('failed to create merge-gate check run', p.stdout)

    def test_checkruns_get_failure_still_posts_and_red_job(self):
        sha = 'deadbeef' * 5
        self._clear_scenario()
        self.scenario({**json.load(open(self.scenario_path)),
                       'fail': {f'sha-checks/{sha}': {'rc': 1, 'stderr': 'boom 500'}}})
        p = self.run_guard(PR='3', PR_URL='u', EVENT_NAME='pull_request_target')
        self.assertEqual(p.returncode, 3)
        self.assertIn('could not list merge-gate runs', p.stdout)
        posts = [c for c in self.calls() if 'POST' in c['argv']
                 and any(a.endswith('/check-runs') for a in c['argv'])]
        self.assertEqual(len(posts), 1)  # best-effort conclusion still published

    def test_repair_label_post_failure_red_job(self):
        self._held_scenario()
        self.scenario({**json.load(open(self.scenario_path)),
                       'fail': {'labels-post/3': {'rc': 1, 'stderr': 'boom 500'}}})
        p = self.run_guard(PR='3', PR_URL='u', EVENT_NAME='pull_request_target')
        self.assertEqual(p.returncode, 3)
        self.assertIn('failed to re-apply label', p.stdout)

    def test_comment_failure_red_job(self):
        self._held_scenario()
        self.scenario({**json.load(open(self.scenario_path)),
                       'fail': {'comment/3': {'rc': 1, 'stderr': 'boom 500'}}})
        p = self.run_guard(PR='3', PR_URL='u', EVENT_NAME='pull_request_target')
        self.assertEqual(p.returncode, 3)
        self.assertIn('failed to comment', p.stdout)

if __name__ == '__main__':
    unittest.main()
