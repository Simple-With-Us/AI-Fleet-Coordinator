#!/usr/bin/env python3
"""Release only reviewed alert Workers after successful exact-main CI."""
import importlib.util
import json
import os
import subprocess
import urllib.request

spec = importlib.util.spec_from_file_location('mapped', 'scripts/deploy-mapped-worker.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
TARGETS = {
    'alert-ingress': {'root': 'workers/alert-ingress/', 'config': 'workers/alert-ingress/wrangler.toml', 'health': 'https://alert-ingress.jays-services.workers.dev/health', 'ok': True},
    'pr-conflict-watch': {'root': 'workers/pr-conflict-watch/zulip-cutover/', 'config': 'workers/pr-conflict-watch/zulip-cutover/wrangler.toml', 'health': 'https://pr-conflict-watch.jays-services.workers.dev/health', 'text': 'ok'},
}

def github(path):
    req = urllib.request.Request('https://api.github.com/repos/' + os.environ['GITHUB_REPOSITORY'] + path,
        headers={'Authorization': 'Bearer ' + os.environ['GH_TOKEN'], 'Accept': 'application/vnd.github+json', 'User-Agent': m.USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.load(response)

def reviewed(sha):
    # A target-changing commit must be the merge of a real, non-draft PR.
    prs = github('/commits/' + sha + '/pulls')
    for pr in prs:
        if not pr.get('merged_at') or pr.get('draft') or pr.get('merge_commit_sha') != sha:
            continue
        number = str(pr['number'])
        reviews = github('/pulls/' + number + '/reviews?per_page=100')
        if len(reviews) >= 100:
            raise RuntimeError('Review pagination needs explicit resolution')
        if not any(r.get('user', {}).get('login') == 'kody-ai[bot]' and r.get('state') in ('APPROVED', 'COMMENTED') for r in reviews):
            continue
        # GraphQL includes actual thread resolution, unlike review state.
        owner, repo = os.environ['GITHUB_REPOSITORY'].split('/')
        query = 'query($owner:String!,$repo:String!,$number:Int!){repository(owner:$owner,name:$repo){pullRequest(number:$number){reviewThreads(first:100){pageInfo{hasNextPage} nodes{isResolved}}}}}'
        req = urllib.request.Request('https://api.github.com/graphql', data=json.dumps({'query':query,'variables':{'owner':owner,'repo':repo,'number':int(number)}}).encode(), headers={'Authorization':'Bearer '+os.environ['GH_TOKEN'],'Content-Type':'application/json','User-Agent':m.USER_AGENT})
        with urllib.request.urlopen(req, timeout=30) as response:
            body = json.load(response)
        threads = body['data']['repository']['pullRequest']['reviewThreads']
        if threads['pageInfo']['hasNextPage'] or any(not t['isResolved'] for t in threads['nodes']):
            raise RuntimeError('Unresolved or incomplete review thread evidence')
        return
    raise RuntimeError('Target commit lacks qualified merged Kody-reviewed PR')

def probe(target):
    req = urllib.request.Request(target['health'], headers={'User-Agent':m.USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as response:
        raw = response.read(65536)
        if response.status != 200:
            raise RuntimeError('Health failed')
    if target.get('ok') and json.loads(raw).get('ok') is not True:
        raise RuntimeError('Health not ready')
    if target.get('text') and raw.decode().strip() != target['text']:
        raise RuntimeError('Invalid health response')

def main():
    sha = subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip()
    if m.current_main() != sha:
        print('Superseded'); return
    runs = github('/actions/workflows/ci.yml/runs?branch=main&event=push&head_sha='+sha+'&per_page=100')['workflow_runs']
    if not any(r['head_sha']==sha and r['status']=='completed' and r['conclusion']=='success' for r in runs):
        raise RuntimeError('Exact-main CI has not passed')
    for name, target in TARGETS.items():
        live = m.deployed_tag(name)
        if not live:
            raise RuntimeError('No exact release baseline for '+name)
        subprocess.run(['git','cat-file','-e',live+'^{commit}'],check=True)
        changed = subprocess.check_output(['git','diff','--name-only',live,sha,'--',target['root']],text=True).splitlines()
        if not changed:
            probe(target); continue
        commits = subprocess.check_output(['git','rev-list','--first-parent',live+'..'+sha,'--',target['root']],text=True).splitlines()
        for commit in commits:
            reviewed(commit)
        # Initial cutover is manual and verified first. Automated code releases
        # cannot provision delivery credentials or reintroduce Slack.
        settings = m.api('/workers/scripts/'+name+'/settings')
        if name == 'pr-conflict-watch':
            names = {b['name'] for b in settings.get('bindings',[])}
            required = {'GITHUB_WEBHOOK_SECRET','GITHUB_TOKEN','PR_STATE','ZULIP_SITE','ZULIP_BOT_EMAIL','ZULIP_BOT_API_KEY'}
            if not required <= names or 'SLACK_BOT_TOKEN' in names:
                raise RuntimeError('Verified Zulip cutover prerequisite not met')
            subprocess.run(['npm','ci','--ignore-scripts','--prefix',target['root']],check=True)
            subprocess.run(['node','--test',target['root']+'body.test.js',target['root']+'index.test.js'],check=True)
        else:
            subprocess.run(['node','--test',*__import__('glob').glob(target['root']+'src/*.test.js')],check=True)
        probe(target)
        if m.current_main() != sha:
            print('Superseded before release'); return
        subprocess.run(['scripts/agent-sync-mcp/node_modules/.bin/wrangler','deploy','--config',target['config'],'--keep-vars','--strict','--tag',sha,'--message','reviewed main '+sha],check=True,env={**os.environ,'CLOUDFLARE_ACCOUNT_ID':m.ACCOUNT,'WRANGLER_SEND_METRICS':'false'})
        after = m.api('/workers/scripts/'+name+'/settings')
        for key in set(settings)|set(after):
            if key != 'annotations' and settings.get(key) != after.get(key):
                raise RuntimeError('Unexpected settings change: '+key)
        if m.deployed_tag(name) != sha:
            raise RuntimeError('Wrong version receipt')
        probe(target)
        print('Verified exact release: '+name+' '+sha)

if __name__ == '__main__':
    main()
