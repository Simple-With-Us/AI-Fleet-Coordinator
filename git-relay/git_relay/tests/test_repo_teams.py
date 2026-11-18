"""Offline typed repository-team read, least permission and bounded receipt tests."""
import copy
import unittest
from .test_workflow_verbs import CEILING, REPO, Client, Minter, Policy
from git_relay.workflow_verbs import WorkflowVerbs, Refused, RelayGitHubError, SPECS

TEAM = dict(id=1, slug='fixture-team', name='Fixture team', permission='push', privacy='closed',
            description='unreturned fixture', url='https://api.github.com/teams/1')

class RepositoryTeamsTests(unittest.TestCase):
    def run_action(self, data, args=None):
        client, minter = Client(data), Minter()
        handle = WorkflowVerbs.for_action(client, Policy(), minter, 'swu-wise', 'wf_repo_teams', REPO)
        return handle.run('wf_repo_teams', dict(repo=REPO, **(args or {}))), client, minter

    def test_bounded_projection_exact_scope_and_path(self):
        out, client, minter = self.run_action([TEAM], dict(page=2, per_page=1))
        self.assertEqual(client.calls[0][0:2], ('GET', '/repos/'+REPO+'/teams?per_page=1&page=2'))
        self.assertEqual(minter.calls[0][2], {'administration':'read', 'metadata':'read'})
        self.assertEqual(set(out['teams'][0]), {'id','slug','name','permission','privacy'})
        self.assertTrue(out['has_more'])
        self.assertEqual(out['page'], 2)
        self.assertFalse(SPECS['wf_repo_teams'][1])

    def test_empty_page_not_full_inventory_claim(self):
        out, _, _ = self.run_action([])
        self.assertEqual(out, dict(teams=[], page=1, has_more=False))

    def test_no_scope_no_mint(self):
        for ceiling in ({'metadata':'read'}, {'administration':'none','metadata':'read'}):
            minter = Minter()
            with self.assertRaises(Refused):
                WorkflowVerbs.for_action(Client(), Policy(ceiling), minter, 'swu-wise', 'wf_repo_teams', REPO)
            self.assertEqual(minter.calls, [])

    def test_invalid_args_before_upstream(self):
        for args in ({'page':False}, {'page':0}, {'page':1001}, {'per_page':51},
                     {'per_page':True}, {'url':'https://elsewhere.invalid'}, {'team':'other'}):
            with self.assertRaises(Refused):
                WorkflowVerbs.check_shape('wf_repo_teams',dict(repo=REPO,**args),REPO)

    def test_malformed_receipt(self):
        bad = []
        for key, values in [('id',[True,0,-1,'1']), ('slug',['','bad/slug','a'*101]),
                            ('name',['', 'a'*257, 'bad\nname']),
                            ('privacy',['public', [], True]), ('permission',['owner', [], True])]:
            for value in values:
                item=copy.deepcopy(TEAM);item[key]=value;bad.append([item])
        bad += [{}, [TEAM,TEAM], [TEAM]*51, [None]]
        for data in bad:
            with self.subTest(data=str(data)[:80]):
                with self.assertRaises(RelayGitHubError):self.run_action(data)

    def test_credential_shaped_names_refused(self):
        for key in ('name','slug'):
            item=copy.deepcopy(TEAM);item[key]='sk-proj-'+'A'*40
            with self.assertRaises(Refused):self.run_action([item])

    def test_nullable_privacy(self):
        item=dict(TEAM,privacy=None)
        self.assertIsNone(self.run_action([item])[0]['teams'][0]['privacy'])
