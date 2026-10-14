#!/usr/bin/env python3
"""Operator-only, resumable outgoing-bot replacement (dry-run unless --apply).

Run stage, publish, then (after consumers and real delivery are verified) retire.
Stage releases the canonical email/name and creates its replacement; old history
stays on the renamed old account.  Publish updates Infisical prod /zulip FIRST,
readback-verifies every value, then installs the local rc atomically.  This is
intentionally not an atomic cross-service transaction: a failed publish MUST be
resumed before consumers restart.  Stage never claims Infisical write readiness.
The private journal retains both keys for recovery; never commit or print it.

Zulip source contract: zerver/views/users.py add_bot_backend/patch_bot_backend;
zerver/lib/events.py realm_bot registration; zerver/actions/users.py
get_owned_bot_dicts/get_service_dicts_for_bots.  The service token is NOT the API key.
"""
from __future__ import annotations

import argparse
import fcntl
import hmac
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request

from agent_sync import zulip as Z
from agent_sync.identity import seat_tag_for


def seat_for_email(email: str) -> str:
    return seat_tag_for({"is_bot": True, "email": email})

ROLES = ('COMPILER', 'DIRECTOR', 'TRADER', 'DEPLOYER', 'DESIGNER', 'ORACLE',
         'PUBLISHER', 'PLUMBER', 'FIXER', 'HOUSEKEEPER', 'MONITOR', 'ENGINEER')
BF_ROLES = ('BUILDER', 'COMPILER', 'DEPLOYER', 'DESIGNER', 'DIRECTOR', 'FIXER',
            'HOUSEKEEPER', 'MONITOR', 'ORACLE', 'PLUMBER', 'PUBLISHER')
ALLOWED = frozenset({'JET', *(f'GB-{role}' for role in ROLES),
                     *(f'BF-{role}' for role in BF_ROLES)})


class MigrationError(Exception):
    """Only fixed, secret-free messages may be passed here."""


def validate_target(seat: str, email: str, endpoint_url: str) -> None:
    """Both native BotFleet and the fleet receiver bind the exact uppercase seat path."""
    endpoint = urllib.parse.urlsplit(endpoint_url)
    if (seat not in ALLOWED or endpoint.scheme != 'https' or not endpoint.hostname
            or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment
            or endpoint.path != '/zulip/outgoing/' + seat or seat_for_email(email) != seat):
        raise MigrationError('Endpoint or canonical email does not match the selected seat')


def private_file(path: Path) -> None:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
        raise MigrationError('Private input must be a regular mode-600 file')


def atomic_private(path: Path, text: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink():
        raise MigrationError('Refusing symlink output')
    fd, tmp = tempfile.mkstemp(prefix='.outgoing-', dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w') as out:
            out.write(text)
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def bot_client(email: str, key: str):
    return Z.ZulipClient(Z.Credentials(email, key, Z.REALM, 'migration journal'), Z.REALM)


def realm_bots(client) -> dict[int, dict]:
    result = client.request('POST', 'register', {'event_types': ['realm_bot'],
                                               'fetch_event_types': ['realm_bot']})
    try:
        return {b['user_id']: b for b in result.get('realm_bots', [])}
    finally:
        if result.get('queue_id'):
            client.request('DELETE', 'events', {'queue_id': result['queue_id']})


def service_token(client, user_id: int, endpoint: str) -> str:
    services = realm_bots(client).get(user_id, {}).get('services', [])
    if len(services) != 1 or services[0].get('base_url') != endpoint or services[0].get('interface') != 1:
        raise MigrationError('Replacement service does not match the requested endpoint')
    token = services[0].get('token')
    if not isinstance(token, str) or not token:
        raise MigrationError('Replacement service token is unavailable')
    return token


def user(client, user_id: int) -> dict:
    return client.request('GET', f'users/{user_id}')['user']


def members(client) -> list[dict]:
    return client.request('GET', 'users')['members']


def own_key(client, email: str) -> str:
    found = [b for b in client.request('GET', 'bots')['bots'] if b['username'] == email]
    if len(found) != 1 or not found[0].get('api_key'):
        raise MigrationError('Expected exactly one owner-managed active bot')
    return found[0]['api_key']


def check_bot(bot: dict, owner_id: int, bot_type: int) -> None:
    if (not bot.get('is_bot') or not bot.get('is_active') or bot.get('bot_type') != bot_type
            or bot.get('bot_owner_id') != owner_id or bot.get('role') != 400):
        raise MigrationError('Bot must be active, member-role, and owned by the authenticated owner')


def save(path: Path, journal: dict) -> None:
    atomic_private(path, json.dumps(journal, sort_keys=True) + '\n')


def prepare(client, seat: str, email: str, endpoint: str, owner_id: int) -> dict:
    matches = [b for b in members(client) if b.get('email') == email]
    if len(matches) != 1:
        raise MigrationError('Canonical bot must exist exactly once')
    old = matches[0]
    check_bot(old, owner_id, 1)
    key = own_key(client, email)
    subscriptions = bot_client(email, key).request('GET', 'users/me/subscriptions')['subscriptions']
    defaults = realm_bots(client)[old['user_id']]
    short = email.split('@')[0]
    if not short.endswith('-bot') or seat_for_email(email) != seat:
        raise MigrationError('Canonical email does not match the allowed seat')
    return {'version': 1, 'seat': seat, 'email': email, 'endpoint': endpoint,
            'owner_id': owner_id, 'old_id': old['user_id'], 'old_key': key,
            'full_name': old['full_name'], 'short_name': short[:-4],
            'retired_short_name': f'retired-wake-{old["user_id"]}',
            'subscriptions': [{'name': s['name']} for s in subscriptions],
            'defaults': {k: defaults[k] for k in ('default_sending_stream',
                        'default_events_register_stream', 'default_all_public_streams')},
            'phase': 'prepared'}


def stage(client, journal: dict, path: Path) -> None:
    """Reconcile before each write; uncertain writes are never blindly repeated."""
    old = user(client, journal['old_id'])
    retired_email = journal['retired_short_name'] + '-bot@' + journal['email'].split('@')[1]
    if old['email'] == journal['email']:
        check_bot(old, journal['owner_id'], 1)
        client.request('PATCH', f'bots/{journal["old_id"]}', {
            'short_name': journal['retired_short_name'],
            'full_name': f'Retired Wake {journal["old_id"]}'})
        old = user(client, journal['old_id'])
    if old['email'] != retired_email:
        raise MigrationError('Old bot changed unexpectedly; manual reconciliation required')
    journal['phase'] = 'old-renamed'
    save(path, journal)
    matches = [b for b in members(client) if b.get('email') == journal['email']]
    if not matches:
        params = {'short_name': journal['short_name'], 'full_name': journal['full_name'],
                  'bot_type': 3, 'interface_type': 1, 'payload_url': json.dumps(journal['endpoint'])}
        params.update({k: v for k, v in journal['defaults'].items() if v is not None})
        # If this POST times out, the next invocation discovers the canonical bot.
        client.request('POST', 'bots', params)
        matches = [b for b in members(client) if b.get('email') == journal['email']]
    if len(matches) != 1:
        raise MigrationError('Replacement identity is ambiguous')
    new = matches[0]
    check_bot(new, journal['owner_id'], 3)
    if (new['full_name'] != journal['full_name'] or new['user_id'] == journal['old_id']
            or journal.get('new_id', new['user_id']) != new['user_id']):
        raise MigrationError('Replacement identity does not match the journal')
    journal['new_id'] = new['user_id']
    journal['new_key'] = own_key(client, journal['email'])
    journal['service_token'] = service_token(client, new['user_id'], journal['endpoint'])
    journal['phase'] = 'created'
    save(path, journal)
    new_client = bot_client(journal['email'], journal['new_key'])
    # Owner subscription administration supports private channels the old bot joined.
    if journal['subscriptions']:
        client.request('POST', 'users/me/subscriptions', {
            'subscriptions': journal['subscriptions'], 'principals': [new['user_id']]})
    actual = new_client.request('GET', 'users/me/subscriptions')['subscriptions']
    if not {s['name'] for s in journal['subscriptions']} <= {s['name'] for s in actual}:
        raise MigrationError('Replacement subscriptions did not verify')
    journal['phase'] = 'staged'
    save(path, journal)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Infisical:
    def __init__(self, env):
        self.project = env.get('INFISICAL_PROJECT_ID', '')
        self.base = env.get('INFISICAL_API_URL', 'https://app.infisical.com/api').rstrip('/')
        parsed = urllib.parse.urlsplit(self.base)
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment or not self.project
                or env.get('INFISICAL_ENVIRONMENT', 'prod') != 'prod'
                or env.get('INFISICAL_SECRET_PATH', '/zulip') != '/zulip'):
            raise MigrationError('Explicit Infisical project, HTTPS API, prod and /zulip are required')
        self.token = None
        self.opener = urllib.request.build_opener(NoRedirect)
        credentials = {'clientId': env.get('INFISICAL_CLIENT_ID', ''),
                       'clientSecret': env.get('INFISICAL_CLIENT_SECRET', '')}
        if not all(credentials.values()):
            raise MigrationError('Infisical machine identity is missing')
        self.token = self.request('POST', '/v1/auth/universal-auth/login', credentials)['accessToken']

    def request(self, method, path, body=None):
        headers = {'Content-Type': 'application/json'}
        if self.token:
            headers['Authorization'] = 'Bearer ' + self.token
        request = urllib.request.Request(self.base + path, method=method, headers=headers,
                                         data=json.dumps(body).encode() if body is not None else None)
        try:
            with self.opener.open(request, timeout=20) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as exc:
            status = exc.code
            exc.close()
            raise HttpStatus(status) from None
        except Exception:
            raise MigrationError('Infisical request failed; reconcile by rerunning publish') from None

    def values(self):
        query = urllib.parse.urlencode({'workspaceId': self.project, 'environment': 'prod',
                                        'secretPath': '/zulip', 'expandSecretReferences': 'true'})
        result = self.request('GET', '/v3/secrets/raw?' + query)
        return {s['secretKey']: s['secretValue'] for s in result['secrets']}

    def set_verified(self, key, value):
        existing = self.values()
        if existing.get(key) != value:
            self.request('PATCH' if key in existing else 'POST', '/v3/secrets/raw/' + key,
                         {'workspaceId': self.project, 'environment': 'prod', 'secretPath': '/zulip',
                          'type': 'shared', 'secretValue': value})
        actual = self.values().get(key, '')
        if not hmac.compare_digest(actual, value):
            raise MigrationError('Infisical readback did not match')


class HttpStatus(MigrationError):
    def __init__(self, status):
        super().__init__(f'Infisical HTTP {status}; no response body logged')


def publish(client, journal, path, rc_dir, store):
    if journal.get('phase') not in {'staged', 'published'}:
        raise MigrationError('Complete stage before publishing')
    current = user(client, journal['new_id'])
    check_bot(current, journal['owner_id'], 3)
    if current.get('email') != journal['email']:
        raise MigrationError('Replacement email changed; reconcile before publishing')
    authenticated = bot_client(journal['email'], journal['new_key']).request('GET', 'users/me')
    if authenticated.get('user_id') != journal['new_id']:
        raise MigrationError('Replacement API credential no longer matches')
    if service_token(client, journal['new_id'], journal['endpoint']) != journal['service_token']:
        raise MigrationError('Service token changed; reconcile before publishing')
    code = journal['seat'].replace('-', '_')
    values = {f'ZULIP_{code}_EMAIL': journal['email'], f'ZULIP_{code}_API_KEY': journal['new_key'],
              f'ZULIP_OUTGOING_{code}_TOKEN': journal['service_token']}
    for key, value in values.items():
        store.set_verified(key, value)
    atomic_private(rc_dir / Z.credential_file_name(journal['seat']),
                   f'[api]\nemail={journal["email"]}\nkey={journal["new_key"]}\nsite={Z.REALM}\n')
    journal['phase'] = 'published'
    save(path, journal)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase', choices=['stage', 'publish', 'retire'])
    parser.add_argument('--seat', required=True, choices=sorted(ALLOWED))
    parser.add_argument('--email', required=True)
    parser.add_argument('--endpoint', required=True)
    parser.add_argument('--owner-rc', required=True, type=Path)
    parser.add_argument('--journal-dir', required=True, type=Path)
    parser.add_argument('--rc-dir', required=True, type=Path)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--consumers-verified', action='store_true',
                        help='Required for retire, after deployment and real wake verification')
    args = parser.parse_args(argv)
    validate_target(args.seat, args.email, args.endpoint)
    private_file(args.owner_rc)
    client = Z.ZulipClient(Z.read_zuliprc(args.owner_rc), Z.REALM)
    owner = client.request('GET', 'users/me')
    if owner.get('is_bot') or owner.get('role') != 100:
        raise MigrationError('Owner credential required for authorized administrative replacement')
    if not args.apply:
        if args.phase == 'stage':
            # No account mutation, journal or rc write; inspect the actual source bot.
            prepare(client, args.seat, args.email, args.endpoint, owner['user_id'])
        print(f'{args.seat}: dry-run; phase={args.phase}; no bot, rc or Infisical writes')
        return 0
    args.journal_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    if args.journal_dir.is_symlink() or stat.S_IMODE(args.journal_dir.stat().st_mode) != 0o700:
        raise MigrationError('Journal directory must be private mode 700')
    lock_path = args.journal_dir / (args.seat + '.lock')
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        path = args.journal_dir / (args.seat + '.json')
        if path.exists():
            private_file(path)
            journal = json.loads(path.read_text())
            expected = {'seat': args.seat, 'email': args.email, 'endpoint': args.endpoint,
                        'owner_id': owner['user_id'], 'version': 1}
            if any(journal.get(k) != v for k, v in expected.items()):
                raise MigrationError('Journal does not match this migration')
        else:
            if args.phase != 'stage':
                raise MigrationError('Run stage first')
            journal = prepare(client, args.seat, args.email, args.endpoint, owner['user_id'])
            save(path, journal)
        if args.phase == 'stage':
            if journal['phase'] not in {'published', 'retired'}:
                stage(client, journal, path)
        elif args.phase == 'publish':
            publish(client, journal, path, args.rc_dir, Infisical(os.environ))
        else:
            if journal['phase'] not in {'published', 'retired'} or not args.consumers_verified:
                raise MigrationError('Publish and verify consumers before retiring the old bot')
            old = user(client, journal['old_id'])
            if old.get('is_active'):
                client.request('DELETE', f'bots/{journal["old_id"]}')
            if user(client, journal['old_id']).get('is_active'):
                raise MigrationError('Old bot deactivation did not verify')
            journal['phase'] = 'retired'
            save(path, journal)
        print(f'{args.seat}: {journal["phase"]}; secret values withheld')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as exc:
        # Even Zulip errors can contain server-echoed payloads: never emit repr/tracebacks.
        print(str(exc) if isinstance(exc, MigrationError) else
              f'Migration stopped ({type(exc).__name__}); rerun to reconcile', file=sys.stderr)
        sys.exit(1)
