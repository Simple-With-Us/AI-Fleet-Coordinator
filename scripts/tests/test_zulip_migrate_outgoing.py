"""Safety and uncertain-write recovery for the private operator migration helper."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('migration', Path(__file__).parents[1] / 'zulip_migrate_outgoing.py')
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)


class FakeZulip:
    def __init__(self):
        self.users = {1: {'user_id': 1, 'email': 'compiler-grok-bot@example.com', 'full_name': 'GB-Compiler',
                         'is_bot': True, 'is_active': True, 'bot_owner_id': 5, 'bot_type': 1, 'role': 400}}
        self.created = 0
        self.fail_create = False
        self.subscriptions = []
        self.endpoint = 'https://example.com/zulip/outgoing/GB-COMPILER'

    def request(self, method, path, params=None):
        if path == 'users/me':
            return self.users[2]
        if path == 'users':
            return {'members': list(self.users.values())}
        if path.startswith('users/') and path.split('/')[-1].isdigit():
            return {'user': self.users[int(path.split('/')[-1])]}
        if path == 'bots' and method == 'GET':
            return {'bots': [{'username': b['email'], 'api_key': 'api-key'} for b in self.users.values()]}
        if path == 'register':
            return {'queue_id': 'queue', 'realm_bots': [{'user_id': 2, 'services': [
                {'base_url': self.endpoint, 'interface': 1, 'token': 'separate-token'}]}]}
        if path == 'events':
            return {}
        if path == 'bots/1':
            self.users[1]['email'] = params['short_name'] + '-bot@example.com'
            self.users[1]['full_name'] = params['full_name']
            return {}
        if path == 'bots' and method == 'POST':
            self.created += 1
            self.users[2] = {**self.users[1], 'user_id': 2, 'email': 'compiler-grok-bot@example.com',
                             'full_name': 'GB-Compiler', 'bot_type': 3}
            if self.fail_create:
                self.fail_create = False
                raise TimeoutError('possibly sent')
            return {}
        if path == 'users/me/subscriptions':
            if method == 'POST':
                self.subscriptions = params['subscriptions']
            return {'subscriptions': self.subscriptions}
        raise AssertionError((method, path))


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'journal.json'
        self.client = FakeZulip()
        self.journal = {'version': 1, 'seat': 'GB-COMPILER', 'old_id': 1, 'owner_id': 5,
                        'email': 'compiler-grok-bot@example.com', 'full_name': 'GB-Compiler',
                        'short_name': 'compiler-grok', 'retired_short_name': 'retired-wake-1',
                        'endpoint': self.client.endpoint, 'defaults': {},
                        'subscriptions': [{'name': 'agent-sync'}], 'phase': 'prepared'}

    def test_uncertain_create_reconciles_without_duplicate(self):
        self.client.fail_create = True
        with self.assertRaises(TimeoutError):
            M.stage(self.client, self.journal, self.path)
        with patch.object(M, 'bot_client', return_value=self.client):
            M.stage(self.client, self.journal, self.path)
        self.assertEqual(self.client.created, 1)
        self.assertEqual(self.journal['phase'], 'staged')
        self.assertEqual(self.journal['service_token'], 'separate-token')
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_publish_failure_does_not_install_rc(self):
        class Store:
            def set_verified(self, key, value):
                raise M.MigrationError('write denied')
        with patch.object(M, 'bot_client', return_value=self.client):
            M.stage(self.client, self.journal, self.path)
        rc_dir = Path(self.tmp.name) / 'rc'
        with patch.object(M, 'bot_client', return_value=self.client), self.assertRaises(M.MigrationError):
            M.publish(self.client, self.journal, self.path, rc_dir, Store())
        self.assertFalse(rc_dir.exists())
        self.assertEqual(self.journal['phase'], 'staged')

    def test_publish_exact_names_and_permissions(self):
        values = {}
        class Store:
            def set_verified(self, key, value):
                values[key] = value
        with patch.object(M, 'bot_client', return_value=self.client):
            M.stage(self.client, self.journal, self.path)
        rc_dir = Path(self.tmp.name) / 'rc'
        with patch.object(M, 'bot_client', return_value=self.client):
            M.publish(self.client, self.journal, self.path, rc_dir, Store())
        self.assertEqual(set(values), {'ZULIP_GB_COMPILER_EMAIL', 'ZULIP_GB_COMPILER_API_KEY',
                                      'ZULIP_OUTGOING_GB_COMPILER_TOKEN'})
        rc = rc_dir / 'GB-Compiler-zuliprc'
        self.assertEqual(rc.stat().st_mode & 0o777, 0o600)
        self.assertNotIn('separate-token', rc.read_text())
        self.assertEqual(self.journal['phase'], 'published')

    def test_wrong_service_rejected(self):
        self.client.endpoint += '/wrong'
        with self.assertRaises(M.MigrationError):
            M.stage(self.client, self.journal, self.path)

    def test_privileged_bot_rejected(self):
        self.client.users[1]['role'] = 100
        with self.assertRaises(M.MigrationError):
            M.stage(self.client, self.journal, self.path)
        self.assertEqual(self.client.created, 0)

    def test_symlink_output_rejected(self):
        target = Path(self.tmp.name) / 'target'
        target.write_text('keep')
        link = Path(self.tmp.name) / 'link'
        link.symlink_to(target)
        with self.assertRaises(M.MigrationError):
            M.atomic_private(link, 'secret')
        self.assertEqual(target.read_text(), 'keep')

    def test_nonprod_infisical_rejected_before_network(self):
        with self.assertRaises(M.MigrationError):
            M.Infisical({'INFISICAL_PROJECT_ID': 'project', 'INFISICAL_ENVIRONMENT': 'dev'})


if __name__ == '__main__':
    unittest.main()
