#!/usr/bin/env python3
"""Unit tests for hold_guard.evaluate against a canned-API runner (fake gh)."""
import os, sys, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hold_guard

_seq = [0]
def ev(kind, label, actor_id, login, ts):
    _seq[0] += 1
    return {'id': _seq[0], 'event': kind, 'label': {'name': label},
            'actor': {'id': actor_id, 'login': login}, 'created_at': ts}

def make_runner(labels, pages):
    def runner(args):
        if args[1].endswith('/labels'):
            return [{'name': n} for n in labels]
        return pages  # already slurped: list of pages
    return runner

ECHO = (41898282, 'swu-echo[bot]')
WISE = (41900001, 'swu-wise[bot]')
GUARD = (41900002, 'swu-guard[bot]')

class HoldGuardTests(unittest.TestCase):
    def test_no_labels_clear(self):
        r = hold_guard.evaluate('o/r', 1, make_runner([], [[]]))
        self.assertEqual(r['state'], 'clear')

    def test_active_hold_owner(self):
        r = hold_guard.evaluate('o/r', 1, make_runner(
            ['do-not-automerge'], [[ev('labeled', 'do-not-automerge', *ECHO, '2026-10-25T01:00:00Z')]]))
        self.assertEqual(r['state'], 'held')
        self.assertEqual(r['holds'][0]['owner_id'], ECHO[0])
        self.assertFalse(r['holds'][0]['unknown'])
        self.assertFalse(r['holds'][0]['needs_reapply'])

    def test_owner_release_clears(self):
        pages = [[ev('labeled', 'do-not-automerge', *ECHO, '2026-10-25T01:00:00Z'),
                  ev('unlabeled', 'do-not-automerge', *ECHO, '2026-10-25T02:00:00Z')]]
        self.assertEqual(hold_guard.evaluate('o/r', 1, make_runner([], pages))['state'], 'clear')

    def test_illegitimate_removal_keeps_original_owner_and_reapplies(self):
        pages = [[ev('labeled', 'do-not-automerge', *ECHO, '2026-10-25T01:00:00Z'),
                  ev('unlabeled', 'do-not-automerge', *WISE, '2026-10-25T02:00:00Z')]]
        r = hold_guard.evaluate('o/r', 1, make_runner([], pages))
        h = r['holds'][0]
        self.assertEqual(r['state'], 'held')
        self.assertEqual(h['owner_login'], 'swu-echo[bot]')
        self.assertEqual(h['illegitimate_removals'], ['swu-wise[bot]'])
        self.assertTrue(h['needs_reapply'])

    def test_guard_repair_reapply_does_not_steal_ownership(self):
        # the core regression: guard re-applies after unauthorized removal;
        # original owner must survive the repair event
        pages = [[ev('labeled', 'do-not-automerge', *ECHO, '2026-10-25T01:00:00Z'),
                  ev('unlabeled', 'do-not-automerge', *WISE, '2026-10-25T02:00:00Z'),
                  ev('labeled', 'do-not-automerge', *GUARD, '2026-10-25T02:00:05Z')]]
        r = hold_guard.evaluate('o/r', 1, make_runner(['do-not-automerge'], pages))
        h = r['holds'][0]
        self.assertEqual(h['owner_id'], ECHO[0])
        self.assertEqual(h['illegitimate_removals'], ['swu-wise[bot]'])
        self.assertFalse(h['needs_reapply'])  # label is back on
        # and the ORIGINAL owner can still release after the repair
        pages[0].append(ev('unlabeled', 'do-not-automerge', *ECHO, '2026-10-25T03:00:00Z'))
        self.assertEqual(hold_guard.evaluate('o/r', 1, make_runner([], pages))['state'], 'clear')

    def test_repeated_unauthorized_removal_and_repair(self):
        pages = [[ev('labeled', 'do-not-automerge', *ECHO, '2026-10-25T01:00:00Z'),
                  ev('unlabeled', 'do-not-automerge', *WISE, '2026-10-25T02:00:00Z'),
                  ev('labeled', 'do-not-automerge', *GUARD, '2026-10-25T02:00:05Z'),
                  ev('unlabeled', 'do-not-automerge', *WISE, '2026-10-25T04:00:00Z'),
                  ev('labeled', 'do-not-automerge', *GUARD, '2026-10-25T04:00:05Z')]]
        r = hold_guard.evaluate('o/r', 1, make_runner(['do-not-automerge'], pages))
        h = r['holds'][0]
        self.assertEqual(h['owner_id'], ECHO[0])
        self.assertEqual(h['illegitimate_removals'], ['swu-wise[bot]', 'swu-wise[bot]'])

    def test_other_actor_reapply_while_open_does_not_transfer(self):
        pages = [[ev('labeled', 'do-not-automerge', *ECHO, '2026-10-25T01:00:00Z'),
                  ev('unlabeled', 'do-not-automerge', *WISE, '2026-10-25T02:00:00Z'),
                  ev('labeled', 'do-not-automerge', *WISE, '2026-10-25T03:00:00Z')]]
        r = hold_guard.evaluate('o/r', 1, make_runner(['do-not-automerge'], pages))
        self.assertEqual(r['holds'][0]['owner_id'], ECHO[0])  # no ownership theft

    def test_per_setter_labels_independent(self):
        pages = [[ev('labeled', 'do-not-automerge:swu-echo[bot]', *ECHO, '2026-10-25T01:00:00Z'),
                  ev('labeled', 'do-not-automerge:swu-wise[bot]', *WISE, '2026-10-25T01:05:00Z'),
                  ev('unlabeled', 'do-not-automerge:swu-echo[bot]', *ECHO, '2026-10-25T02:00:00Z')]]
        r = hold_guard.evaluate('o/r', 1, make_runner(['do-not-automerge:swu-wise[bot]'], pages))
        self.assertEqual(len(r['holds']), 1)
        self.assertEqual(r['holds'][0]['label'], 'do-not-automerge:swu-wise[bot]')

    def test_renamed_login_same_id_releases(self):
        pages = [[ev('labeled', 'do-not-automerge', *ECHO, '2026-10-25T01:00:00Z'),
                  ev('unlabeled', 'do-not-automerge', ECHO[0], 'echo-renamed[bot]', '2026-10-25T02:00:00Z')]]
        self.assertEqual(hold_guard.evaluate('o/r', 1, make_runner([], pages))['state'], 'clear')

    def test_label_present_no_events_unknown_owner(self):
        r = hold_guard.evaluate('o/r', 1, make_runner(['do-not-automerge'], [[]]))
        self.assertEqual(r['state'], 'held')
        self.assertTrue(r['holds'][0]['unknown'])

    def test_unknown_owner_never_auto_releases(self):
        pages = [[ev('unlabeled', 'do-not-automerge', *WISE, '2026-10-25T02:00:00Z'),
                  ev('unlabeled', 'do-not-automerge', *ECHO, '2026-10-25T03:00:00Z'),
                  ev('unlabeled', 'do-not-automerge', 777, 'jaywedgeworth22', '2026-10-25T04:00:00Z')]]
        r = hold_guard.evaluate('o/r', 1, make_runner([], pages))
        self.assertEqual(r['state'], 'held')
        self.assertTrue(r['holds'][0]['unknown'])
        self.assertEqual(len(r['holds'][0]['illegitimate_removals']), 3)

    def test_deleted_label_events_retain_name_and_owner(self):
        pages = [[ev('labeled', 'do-not-automerge:old-name', *ECHO, '2026-10-25T01:00:00Z')]]
        r = hold_guard.evaluate('o/r', 1, make_runner([], pages))
        h = r['holds'][0]
        self.assertEqual(r['state'], 'held')
        self.assertEqual(h['owner_id'], ECHO[0])
        self.assertTrue(h['needs_reapply'])  # absent label must be restored

    def test_time_ties_broken_by_event_id(self):
        # same created_at: lower event id (the actually-earlier event) sorts first,
        # even when pages arrive out of order
        e1 = {'id': 100, 'event': 'labeled', 'label': {'name': 'do-not-automerge'},
              'actor': {'id': ECHO[0], 'login': ECHO[1]}, 'created_at': '2026-10-25T01:00:00Z'}
        e2 = {'id': 101, 'event': 'unlabeled', 'label': {'name': 'do-not-automerge'},
              'actor': {'id': ECHO[0], 'login': ECHO[1]}, 'created_at': '2026-10-25T01:00:00Z'}
        pages = [[e2, e1]]
        self.assertEqual(hold_guard.evaluate('o/r', 1, make_runner([], pages))['state'], 'clear')

    def test_multiple_holds_multiple_actors(self):
        pages = [[ev('labeled', 'do-not-automerge', *ECHO, '2026-10-25T01:00:00Z'),
                  ev('labeled', 'needs-human-merge', *WISE, '2026-10-25T01:30:00Z'),
                  ev('unlabeled', 'do-not-automerge', *WISE, '2026-10-25T02:00:00Z')]]
        r = hold_guard.evaluate('o/r', 1, make_runner(['needs-human-merge'], pages))
        self.assertEqual(len(r['holds']), 2)
        by = {h['label']: h for h in r['holds']}
        self.assertEqual(by['do-not-automerge']['owner_id'], ECHO[0])
        self.assertTrue(by['do-not-automerge']['needs_reapply'])
        self.assertEqual(by['needs-human-merge']['owner_id'], WISE[0])

    def test_unrelated_labels_ignored(self):
        pages = [[ev('labeled', 'bug', *ECHO, '2026-10-25T01:00:00Z')]]
        self.assertEqual(hold_guard.evaluate('o/r', 1, make_runner(['bug', 'priority'], pages))['state'], 'clear')

    def test_api_error_fails_closed(self):
        def bad(args): raise RuntimeError('boom')
        self.assertRaises(RuntimeError, hold_guard.evaluate, 'o/r', 1, bad)

if __name__ == '__main__':
    unittest.main()
