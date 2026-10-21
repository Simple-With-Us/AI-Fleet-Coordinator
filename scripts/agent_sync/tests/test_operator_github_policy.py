import unittest

from agent_sync.operator_github import Broker, CallerPolicy, Refused
from agent_sync.operator_github_policy import (
    APPS, POLICY, SEATS, _validate, app_credentials_loader, caller_key_loader, load_policy,
)
from agent_sync.operator_github_state import NonceLedger
from agent_sync.operator_github import signature
from agent_sync.github_token_router import RoutingError

import json, tempfile


class FakeSecrets:
    def __init__(self):
        self.calls = []
    def caller_key(self, seat):
        self.calls.append(('caller', seat))
        return 'k' * 64
    def app_credentials(self, slug):
        self.calls.append(('app', slug))
        return ('123', 'pem')


class PolicyTests(unittest.TestCase):
    def test_pilot_seats_and_apps(self):
        self.assertEqual(SEATS, frozenset({'CODEX', 'ECHO'}))
        self.assertEqual(APPS, frozenset({'swu-codex', 'swu-echo'}))

    def test_load_policy_binds_seat_to_own_app(self):
        policy = load_policy('CODEX')
        self.assertIsInstance(policy, CallerPolicy)
        self.assertEqual(policy.app_slug, 'swu-codex')
        self.assertIn('simple-with-us/ai-fleet-coordinator', policy.repositories)

    def test_unknown_seat_gets_no_policy(self):
        self.assertIsNone(load_policy('WISE'))
        self.assertIsNone(load_policy('codex'))  # case-sensitive: seats are uppercase

    def test_validation_rejects_bad_shapes(self):
        for bad in [
            {'lowercase': ('swu-x', {'a/b': {'contents': 'read'}})},
            {'OK': ('not-swu', {'a/b': {'contents': 'read'}})},
            {'OK': ('swu-x', {'a/b': {'contents': 'admin'}})},
            {'OK': ('swu-x', {'a/b': {'actions': 'read'}})},
            {'OK': ('swu-x', {'a/b': {'contents': 'read'}})},  # missing metadata: read
            {'OK': ('swu-x', {})},
            {'OK': ('swu-x', {'bad repo!': {'contents': 'read'}})},
        ]:
            with self.assertRaises(RoutingError, msg=str(bad)):
                _validate(bad)

    def test_caller_key_loader_refuses_unlisted_seat(self):
        secrets = FakeSecrets()
        key_for = caller_key_loader(secrets)
        self.assertIsNone(key_for('WISE'))
        self.assertEqual(secrets.calls, [])
        self.assertEqual(key_for('ECHO'), 'k' * 64)
        self.assertEqual(secrets.calls, [('caller', 'ECHO')])

    def test_app_loader_refuses_unlisted_app(self):
        secrets = FakeSecrets()
        creds_for = app_credentials_loader(secrets)
        with self.assertRaises(RoutingError):
            creds_for('swu-wise')
        self.assertEqual(secrets.calls, [])
        self.assertEqual(creds_for('swu-echo'), ('123', 'pem'))

    def test_broker_end_to_end_with_allowlist(self):
        # A wired broker: allowlist policy + bound loaders + ledger.  An
        # allowlisted seat mints within its ceiling;  everything else refuses.
        secrets = FakeSecrets()
        issued = []

        class Router:
            def token_for(self, slug, repo, perms):
                issued.append((slug, repo, perms))
                return 'tok'

        broker = Broker(
            key_for=caller_key_loader(secrets),
            policy_for=load_policy,
            router=Router(),
            bot_identity=lambda slug: {'login': slug + '[bot]', 'id': 7},
            enabled=True,
            nonce_ledger=NonceLedger(tempfile.mkdtemp()),
        )

        nonces = iter('%.64x' % i for i in range(1, 20))

        def request(seat, repo, perms):
            nonce = next(nonces)
            path = '/operator/v1/github/%s/token' % seat
            body = json.dumps({'repo': repo, 'permissions': perms}, sort_keys=True).encode()
            sig = signature('k' * 64, path, '1000', nonce, body)
            broker.clock = lambda: 1000
            return broker.issue(path, '1000', nonce, sig, body)

        result = request('CODEX', 'Simple-With-Us/AI-Fleet-Coordinator', {'contents': 'read'})
        self.assertEqual(result['token'], 'tok')
        self.assertEqual(issued, [('swu-codex', 'Simple-With-Us/AI-Fleet-Coordinator', {'contents': 'read', 'metadata': 'read'})])

        # repository outside the seat's grant
        with self.assertRaises(Refused) as caught:
            request('CODEX', 'Simple-With-Us/BotFleet', {'contents': 'read'})
        self.assertEqual(caught.exception.status, 403)

        # above the ceiling
        with self.assertRaises(Refused) as caught:
            request('ECHO', 'Simple-With-Us/AI-Fleet-Coordinator', {'contents': 'write'})
        self.assertEqual(caught.exception.status, 403)

        # seat not in the allowlist: no caller key, which surfaces as a
        # provisioning failure naming OPERATOR_GITHUB_CALLER_KEY (never the value)
        with self.assertRaises(Refused) as caught:
            request('WISE', 'Simple-With-Us/AI-Fleet-Coordinator', {'contents': 'read'})
        self.assertEqual(caught.exception.status, 503)
        self.assertIn('OPERATOR_GITHUB_CALLER_KEY', caught.exception.reason)


if __name__ == '__main__':
    unittest.main()
