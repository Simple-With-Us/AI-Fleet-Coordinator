"""No subprocess or live credential access: project-scoped caches stay isolated."""
import json
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock
from agent_sync import zulip as Z

class Tests(unittest.TestCase):
    def setUp(self):
        for c in (Z._value_cache,Z._names_cache,Z._failures):c.clear();self.addCleanup(c.clear)
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.home=Path(self.tmp.name)
        self.a={Z.ENV_INFISICAL_PROJECT:str(uuid.uuid5(uuid.NAMESPACE_URL, 'fixture-project-a'))}
        self.b={Z.ENV_INFISICAL_PROJECT:str(uuid.uuid5(uuid.NAMESPACE_URL, 'fixture-project-b'))}
        self.patch=mock.patch.object(Z,'fleet_secrets_bin',return_value='/fixture/fleet-secrets');self.patch.start();self.addCleanup(self.patch.stop)
    def test_values_and_names_do_not_cross_projects(self):
        def run(argv,env):
            value='A' if env==self.a else 'B'
            if argv[1]=='names':return (value+'\n').encode()
            return json.dumps({'SAME':value}).encode()
        with mock.patch.object(Z,'_run_fleet_secrets',side_effect=run) as call:
            self.assertEqual(Z._fetch_from_fleet_secrets(('SAME',),self.a,self.home),{'SAME':'A'})
            self.assertEqual(Z._fetch_from_fleet_secrets(('SAME',),self.b,self.home),{'SAME':'B'})
            self.assertEqual(Z._list_from_fleet_secrets(self.a,self.home),frozenset({'A'}))
            self.assertEqual(Z._list_from_fleet_secrets(self.b,self.home),frozenset({'B'}))
            Z._fetch_from_fleet_secrets(('SAME',),self.a,self.home)
            self.assertEqual(call.call_count,4)
    def test_outage_does_not_cross_projects(self):
        def run(argv,env):
            if env==self.a:raise Z.CredentialError('fleet-secrets timeout')
            return b'{"SAME":"B"}'
        with mock.patch.object(Z,'_run_fleet_secrets',side_effect=run) as call:
            with self.assertRaises(Z.CredentialError):Z._fetch_from_fleet_secrets(('SAME',),self.a,self.home)
            self.assertEqual(Z._fetch_from_fleet_secrets(('SAME',),self.b,self.home),{'SAME':'B'})
            with self.assertRaisesRegex(Z.CredentialError,'not retried'):Z._fetch_from_fleet_secrets(('OTHER',),self.a,self.home)
            self.assertEqual(call.call_count,2)
    def test_missing_project_no_fetch(self):
        with mock.patch.object(Z,'_run_fleet_secrets') as run:
            with self.assertRaises(Z.CredentialError):Z._fetch_from_fleet_secrets(('SAME',),{},self.home)
            run.assert_not_called()
