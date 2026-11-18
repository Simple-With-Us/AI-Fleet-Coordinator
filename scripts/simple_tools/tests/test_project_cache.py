"""No subprocess or live credential access: project-scoped caches stay isolated."""
import json
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock
import fleet_infisical as FI
from simple_tools import zulip as Z

class Tests(unittest.TestCase):
    def setUp(self):
        for c in (Z._value_cache,Z._names_cache,Z._failures):c.clear();self.addCleanup(c.clear)
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.home=Path(self.tmp.name)
        self.a={Z.ENV_INFISICAL_PROJECT:str(uuid.uuid5(uuid.NAMESPACE_URL, 'fixture-project-a')),FI.ENV_TOKEN:'st.'+'synthetic-'*4}
        self.b={Z.ENV_INFISICAL_PROJECT:str(uuid.uuid5(uuid.NAMESPACE_URL, 'fixture-project-b')),FI.ENV_TOKEN:'st.'+'synthetic-'*4}
    def test_values_and_names_do_not_cross_projects(self):
        def run(project,path,env,*,names=None,**kwargs):
            value='A' if project==self.a[Z.ENV_INFISICAL_PROJECT] else 'B'
            return {value:'fixture'} if names is None else {'SAME':value}
        def names(project,path,env,**kwargs):
            return tuple(run(project,path,env))
        with mock.patch.object(FI,'fetch_project',side_effect=run) as call, mock.patch.object(FI,'list_project_names',side_effect=names,create=True) as listing:
            self.assertEqual(Z._fetch_from_fleet_secrets(('SAME',),self.a,self.home),{'SAME':'A'})
            self.assertEqual(Z._fetch_from_fleet_secrets(('SAME',),self.b,self.home),{'SAME':'B'})
            self.assertEqual(Z._list_from_fleet_secrets(self.a,self.home),frozenset({'A'}))
            self.assertEqual(Z._list_from_fleet_secrets(self.b,self.home),frozenset({'B'}))
            Z._fetch_from_fleet_secrets(('SAME',),self.a,self.home)
            self.assertEqual((call.call_count,listing.call_count),(2,2))
    def test_outage_does_not_cross_projects(self):
        def run(project,path,env,**kwargs):
            if project==self.a[Z.ENV_INFISICAL_PROJECT]:raise TimeoutError()
            return {'SAME':'B'}
        with mock.patch.object(FI,'fetch_project',side_effect=run) as call:
            with self.assertRaises(Z.CredentialError):Z._fetch_from_fleet_secrets(('SAME',),self.a,self.home)
            self.assertEqual(Z._fetch_from_fleet_secrets(('SAME',),self.b,self.home),{'SAME':'B'})
            with self.assertRaisesRegex(Z.CredentialError,'not retried'):Z._fetch_from_fleet_secrets(('OTHER',),self.a,self.home)
            self.assertEqual(call.call_count,2)
    def test_missing_project_no_fetch(self):
        with mock.patch.object(FI,'fetch_project') as run:
            with self.assertRaises(Z.CredentialError):Z._fetch_from_fleet_secrets(('SAME',),{},self.home)
            run.assert_not_called()

    def test_binding_rotation_cannot_reuse_another_tokens_cached_value(self):
        changed=dict(self.a,INFISICAL_TOKEN='st.'+'replacement-'*4)
        with mock.patch.object(FI,'fetch_project',side_effect=[{'SAME':'first'},{'SAME':'second'}]) as fetch:
            self.assertEqual(Z._fetch_from_fleet_secrets(('SAME',),self.a,self.home),{'SAME':'first'})
            self.assertEqual(Z._fetch_from_fleet_secrets(('SAME',),changed,self.home),{'SAME':'second'})
        self.assertEqual(fetch.call_count,2)

    def test_ambient_folder_grants_never_widen_the_named_read(self):
        env=dict(self.a,INFISICAL_READ_PATHS='/zulip,/unrelated')
        with mock.patch.object(FI,'fetch_project',return_value={'SAME':'fixture'}) as fetch:
            Z._fetch_from_fleet_secrets(('SAME',),env,self.home)
        self.assertEqual(fetch.call_args.args[1],'/zulip')
        self.assertEqual(fetch.call_args.args[2][FI.ENV_READ_PATHS],'/zulip')
        self.assertEqual(fetch.call_args.kwargs['names'],('SAME',))
        self.assertEqual(fetch.call_args.kwargs['token'],env[FI.ENV_TOKEN])

    def test_name_discovery_never_requests_credential_values(self):
        with mock.patch.object(FI,'fetch_project',side_effect=AssertionError('value read')), \
                mock.patch.object(FI,'list_project_names',return_value=('A_ZULIP_EMAIL','A_ZULIP_KEY','A_ZULIP_SITE'),create=True) as listing:
            self.assertEqual(Z._list_from_fleet_secrets(self.a,self.home),frozenset(('A_ZULIP_EMAIL','A_ZULIP_KEY','A_ZULIP_SITE')))
        self.assertEqual(listing.call_args.args[2][FI.ENV_READ_PATHS],'/zulip')
