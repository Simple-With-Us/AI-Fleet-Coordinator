"""Named-secret adapter. Runtime must provide an independently scoped identity.

No universal-auth bootstrap or folder export is supported here. Deployment must
verify that the supplied identity is denied outside the approved secret paths.
"""
import json
import re
import urllib.parse
import urllib.request
from .github_token_router import secret_names, RoutingError, NoRedirect

PROJECT = '8dae6d03-8f12-48b2-adf3-e5c1b881c8c5'

class NamedSecrets:
    def __init__(self, bearer_for, seats, apps, *, transport=None):
        self.bearer_for = bearer_for
        self.seats = frozenset(seats)
        self.apps = frozenset(apps)
        self.transport = transport or self._read

    def _read(self, path, name):
        token = self.bearer_for()
        if not isinstance(token,str) or not token:
            raise RoutingError('Scoped secret identity unavailable')
        query = urllib.parse.urlencode({'projectId':PROJECT,'environment':'prod',
            'secretPath':path,'expandSecretReferences':'false','includeImports':'false'})
        req = urllib.request.Request('https://app.infisical.com/api/v4/secrets/'+name+'?'+query,
                                     headers={'Authorization':'Bearer '+token})
        try:
            with urllib.request.build_opener(NoRedirect()).open(req,timeout=10) as response:
                raw=response.read(32769)
                if len(raw)>32768:raise ValueError()
                value=json.loads(raw)['secret']['secretValue']
            if not isinstance(value,str) or not value or '${' in value:raise ValueError()
            return value
        except Exception:
            raise RoutingError('Named secret unavailable') from None

    def caller_key(self, seat):
        if seat not in self.seats or not re.fullmatch(r'[A-Z][A-Z0-9_]{0,31}',seat):
            return None
        return self.transport('/operator-github/callers','OPERATOR_GITHUB_CALLER_'+seat+'_HMAC')

    def app_credentials(self, slug):
        if slug not in self.apps:raise RoutingError('App not authorized')
        return tuple(self.transport('/github-apps',name) for name in secret_names(slug))
