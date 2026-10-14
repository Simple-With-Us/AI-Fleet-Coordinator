from __future__ import annotations
from types import SimpleNamespace
from unittest import mock
from agent_sync import wakes as W
from agent_sync.tests.test_daemon import DaemonHarness

class ChatRoutingTests(DaemonHarness):
    def setup_route(self):
        d = self.daemon()
        r = d.seats['CLAUDE']
        r.cfg.wake = 'muse'
        target = {'id': 'a' * 64, 'seat': 'GROK', 'platform': 'grok-leader', 'host': 'host',
                  'session_id': 'existing', 'cwd': '/repo', 'title': 'Work', 'status': 'idle',
                  'stale': False, 'capabilities': {'read': True, 'wake': True}}
        d.seats['GROK'] = SimpleNamespace(seat='GROK', cfg=SimpleNamespace(enabled=True))
        self.addCleanup(lambda: d.seats.pop('GROK', None))
        d._chat_catalog = SimpleNamespace(discover=mock.Mock(return_value=[target]),
                                         resolve=mock.Mock(return_value=target),
                                         excerpt=mock.Mock(return_value=[{'role': 'user', 'text': 'Recent work'}]))
        p = W.Pending('wake', 'CLAUDE', {'channel': 'agent-sync', 'topic': 'work'}, owner=True,
                      now=self.clock.time(), due=self.clock.time())
        p.add({'id': 42}, True)
        return d, r, p, target

    def test_owner_catalog_excerpt_and_exact_selection_queue_once(self):
        d, r, p, target = self.setup_route()
        context = d.chat_context(r, p)
        self.assertNotIn('recent_messages', context[0])
        d._chat_catalog.excerpt.assert_not_called()
        obj = {'chat_target': target['id'], 'risk': None, 'route': None}
        self.assertEqual(d.select_chat(r, p, obj, context)['status'], 'queued')
        self.assertEqual(d.select_chat(r, p, obj, context)['status'], 'already_queued')
        queue = d.chat_queue('GROK')
        self.assertEqual(len(queue.snapshot()['items']), 1)
        self.assertEqual(queue.snapshot()['bindings'], {})
        transport = SimpleNamespace(preflight=lambda b: 'ready', send=lambda b,t: 'accepted')
        self.assertEqual(queue.dispatch_one(transport, self.clock.time())['status'], 'accepted')

    def test_peer_cannot_read_catalog_or_select_chat(self):
        d, r, p, target = self.setup_route()
        p.owner_ids = []
        self.assertEqual(d.chat_context(r, p), [])
        self.assertEqual(d.select_chat(r, p, {'chat_target': target['id']}, [target])['status'], 'held')
        d._chat_catalog.discover.assert_not_called()
        self.assertEqual(d.chat_queue('GROK').snapshot()['items'], [])

    def test_unknown_readonly_stale_and_changed_targets_hold(self):
        for change in ('unknown', 'readonly', 'stale', 'changed', 'unsafe', 'double-route'):
            with self.subTest(change=change):
                d, r, p, target = self.setup_route()
                offered = dict(target)
                obj = {'chat_target': target['id']}
                if change == 'unknown': obj['chat_target'] = 'b' * 64
                if change == 'readonly': offered['capabilities'] = {'read': True, 'wake': False}
                if change == 'stale': offered['stale'] = True
                if change == 'changed': target['cwd'] = '/different'
                if change == 'unsafe': obj['risk'] = 'high'
                if change == 'double-route': obj['route'] = {'seat': 'CURSOR', 'reason': 'work'}
                self.assertEqual(d.select_chat(r, p, obj, [offered])['status'], 'held')
                self.assertEqual(d.chat_queue('GROK').snapshot()['items'], [])

    def test_output_schema_accepts_only_opaque_target_shape(self):
        obj = {'action': 'none', 'reply': None, 'board': None, 'owner_note': None, 'risk': None,
               'chat_target': 'a' * 64}
        self.assertIsNone(W.validate(obj)[1])
        self.assertIsNotNone(W.validate(dict(obj, chat_target='../../new-chat'))[1])
        self.assertIsNotNone(W.validate(dict(obj, chat_target={'id': 'a'*64}))[1])

    def test_selection_cannot_discard_a_reply_or_escalation(self):
        obj = {'action': 'none', 'reply': None, 'board': None, 'owner_note': None,
               'risk': None, 'route': None, 'chat_target': 'a' * 64}
        for key, value in [('action', 'reply'), ('reply', 'answer'), ('owner_note', 'escalate'),
                           ('risk', 'high'), ('board', {}), ('route', {})]:
            with self.subTest(key=key):
                self.assertIsNotNone(W.validate(dict(obj, **{key: value}))[1])

    def test_inspection_shape_and_exclusivity(self):
        obj = {'action': 'none', 'reply': None, 'board': None, 'owner_note': None,
               'risk': None, 'route': None, 'chat_target': None, 'chat_inspect': 'a' * 64}
        self.assertIsNone(W.validate(obj)[1])
        for key, value in [('chat_inspect', '../bad'), ('chat_target', 'b' * 64),
                           ('action', 'reply'), ('reply', 'answer'), ('risk', 'high')]:
            with self.subTest(key=key):
                self.assertIsNotNone(W.validate(dict(obj, **{key: value}))[1])
