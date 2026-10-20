#!/usr/bin/env -S python3 -I
"""On-demand Linux bootstrap for fleet-secrets.  No credentials enter argv or output."""
import json
import os
from pathlib import Path
import stat
import subprocess
import sys


def load_bootstrap(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise ValueError('bootstrap must be an owned regular file with mode 0600')
        data = json.load(source)
    names = ('INFISICAL_AUTOMATION_CLIENT_ID', 'INFISICAL_AUTOMATION_CLIENT_SECRET')
    if not all(isinstance(data.get(k), str) and data[k].strip() for k in names):
        raise ValueError('bootstrap requires both machine credentials')
    return [data[k].strip() for k in names]


def main():
    try:
        env = dict(os.environ)
        if not env.get('INFISICAL_TOKEN'):
            client_id, client_secret = load_bootstrap(Path.home() / '.config/fleet-secrets/bootstrap.json')
            child = dict(env)
            child['INFISICAL_UNIVERSAL_AUTH_CLIENT_ID'] = client_id
            child['INFISICAL_UNIVERSAL_AUTH_CLIENT_SECRET'] = client_secret
            result = subprocess.run(
                ['infisical', 'login', '--method=universal-auth', '--plain', '--silent'],
                env=child, capture_output=True, text=True, timeout=25,
                stdin=subprocess.DEVNULL,
            )
            token = result.stdout.strip()
            if result.returncode or not token or any(c.isspace() for c in token):
                raise ValueError('machine authentication failed')
            env['INFISICAL_TOKEN'] = token
        target = Path.home() / '.local/share/fleet-secrets/fleet-secrets'
        os.execve(str(target), [str(target), *sys.argv[1:]], env)
    except (OSError, ValueError, subprocess.SubprocessError):
        print('fleet-secrets: Linux machine bootstrap failed; check provisioning and permissions', file=sys.stderr)
        return 4


if __name__ == '__main__':
    sys.exit(main())
