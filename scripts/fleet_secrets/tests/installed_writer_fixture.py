"""Exercise an installed shared CLI and its real ledger with only synthetic provider responses."""

import os
import subprocess
import sys


def assert_installed_writer(test, installed, root):
    code = r'''
import io, json, secrets, sys, uuid
from pathlib import Path
from urllib.parse import urlsplit
sys.path.insert(0, sys.argv[1])
import fleet_infisical as core
import fleet_infisical_cli as cli
assert '_fleet_infisical_write_ledger' not in sys.modules
value = secrets.token_urlsafe(40)
project = str(uuid.uuid4())
env = {'HOME':sys.argv[2], core.ENV_PROJECT:project,
       core.ENV_WRITE_TOKEN:'st.' + secrets.token_urlsafe(32)}
calls = []
def api(method, endpoint, token, body, environ, **kwargs):
    assert token == env[core.ENV_WRITE_TOKEN]
    calls.append((method, endpoint))
    if endpoint == '/v2/service-token':
        return 200, dict(workspace=project, permissions=['read','write'],
            scopes=[dict(environment='prod',secretPath='/linear')],
            expiresAt='2099-01-01T00:00:00Z')
    if method == 'PATCH':
        assert body['secretValue'] == value
        kwargs['on_status'](200)
        return 200, {}
    assert method == 'GET'
    if urlsplit(endpoint).path == '/v3/secrets/raw':
        return 200, {'secrets':[]}
    return 200, {'secret':dict(secretKey='CACHE_FIXTURE',secretValue=value,
        workspace=project,environment='prod',secretPath='/linear',type='shared')}
core._api_json = api
intent = cli.write_intent_for_cli(env, 'installed-writer')
args = ['CACHE_FIXTURE','--path','/linear','--purpose',intent.purpose,
        '--request-id',intent.request_id,'--request-created-at',str(intent.created_at_ms)]
out = io.StringIO()
assert cli.cmd_set(args, env, out, io.StringIO(value + '\n')) == 0
receipt = json.loads(out.getvalue())
assert receipt == dict(request_id=intent.request_id,created_at_ms=intent.created_at_ms,
    purpose=intent.purpose,name='CACHE_FIXTURE',path='/linear',outcome='ok',result='updated',replayed=False)
before = list(calls)
out = io.StringIO()
assert cli.cmd_set(args, env, out, io.StringIO(value)) == 0
assert json.loads(out.getvalue())['replayed'] is True
assert calls == before
assert [method for method, _ in calls].count('PATCH') == 1
assert Path(core._write_ledger(intent.state_dir).path).is_file()
print('installed-writer-ok')
'''
    result = subprocess.run(
        [sys.executable, "-I", "-c", code, str(installed), str(root)],
        cwd=root,
        env={"HOME": str(root), "PATH": os.defpath},
        capture_output=True,
        text=True,
        timeout=10,
    )
    test.assertEqual(
        (result.returncode, result.stdout.strip()), (0, "installed-writer-ok"), result.stderr
    )
    ledger = installed / "simple_tools/operator_github_state.py"
    saved = ledger.with_suffix(".held")
    ledger.rename(saved)
    try:
        code = r'''
import io, secrets, sys, uuid
sys.path.insert(0, sys.argv[1])
import fleet_infisical as core
import fleet_infisical_cli as cli
def forbidden(*args, **kwargs):
    raise AssertionError('provider called without the required ledger')
core._api_json = forbidden
env = {'HOME':sys.argv[2], core.ENV_PROJECT:str(uuid.uuid4()),
       core.ENV_WRITE_TOKEN:'st.' + secrets.token_urlsafe(32)}
cli.sys.stdin = io.StringIO(secrets.token_urlsafe(40))
out, err = io.StringIO(), io.StringIO()
assert cli.main(['set','CACHE_FIXTURE','--path','/linear'],env,out,err) == 4
assert not out.getvalue()
assert err.getvalue().strip() == 'fleet-secrets: write-state-unavailable'
print('missing-ledger-refused')
'''
        result = subprocess.run(
            [sys.executable, "-I", "-c", code, str(installed), str(root)],
            cwd=root,
            env={"HOME": str(root), "PATH": os.defpath},
            capture_output=True,
            text=True,
            timeout=10,
        )
        test.assertEqual(
            (result.returncode, result.stdout.strip()), (0, "missing-ledger-refused"), result.stderr
        )
    finally:
        saved.rename(ledger)
