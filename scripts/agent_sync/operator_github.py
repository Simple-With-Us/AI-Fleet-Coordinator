"""Disabled-by-default Operator GitHub broker, hosted by the existing listener.

No server starts on import. A reviewed caller must explicitly construct/start
BrokerHttp with its own seat policy, key loader and independent mint-auth keys.
Read-API/routine keys are NOT accepted automatically. Never log request bodies,
signatures, private keys, or token response bodies.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Mapping

from .operator_github_state import LedgerError
from .github_token_router import RoutingError, permission_set, target_parts

CONTRACT = 'operator-github-mint/1'
PATH = re.compile(r'^/operator/v1/github/([A-Z][A-Z0-9_-]{0,31})/token$')
NONCE = re.compile(r'^[a-f0-9]{64}$')
MAX_BODY = 4096
MAX_SKEW = 60
MAX_NONCES = 10000
MAX_REQUESTS_PER_MINUTE = 60

class Refused(Exception):
    def __init__(self, status: int, reason: str):
        self.status, self.reason = status, reason
        super().__init__(reason)

@dataclass(frozen=True)
class CallerPolicy:
    app_slug: str
    # Exact owner/repository names only, no wildcard expansion.
    repositories: Mapping[str, Mapping[str, str]]


def signature(key: str, path: str, stamp: str, nonce: str, body: bytes) -> str:
    message = '\n'.join((CONTRACT, 'POST', path, stamp, nonce,
                         hashlib.sha256(body).hexdigest())).encode()
    return hmac.new(key.encode(), message, hashlib.sha256).hexdigest()

class Broker:
    """Every request authenticates and applies current policy BEFORE cache access.

    `key_for` and `policy_for` are trusted server-side lookups, not client input.
    The runtime must reload revocation/policy changes on these lookups. `router`
    loads only the bound App's key through Infisical. No seat/env fallback.
    """
    def __init__(self, key_for: Callable, policy_for: Callable, router,
                 bot_identity: Callable, *, clock=time.time, audit=None,
                 enabled=False, nonce_ledger=None):
        self.enabled = enabled
        self.key_for, self.policy_for = key_for, policy_for
        self.router, self.bot_identity = router, bot_identity
        self.clock, self.audit = clock, audit or (lambda **kw: None)
        self.lock = threading.Lock()
        self.nonce_ledger = nonce_ledger
        self.rates = {}

    def issue(self, path: str, stamp: str, nonce: str, sig: str, body: bytes):
        if not self.enabled:
            raise Refused(404, 'disabled')
        match = PATH.fullmatch(path)
        if not match or len(body) > MAX_BODY:
            raise Refused(400, 'invalid request')
        seat = match[1]
        if not isinstance(stamp, str) or not re.fullmatch(r'[0-9]{1,12}', stamp):
            raise Refused(401, 'authentication failed')
        now = self.clock()
        if abs(now-int(stamp)) > MAX_SKEW or not isinstance(nonce, str) or not NONCE.fullmatch(nonce):
            raise Refused(401, 'authentication failed')
        key = self.key_for(seat)
        if not isinstance(key, str) or len(key) < 32:
            raise Refused(401, 'authentication failed')
        if not isinstance(sig, str) or not re.fullmatch(r'[a-f0-9]{64}', sig) or not hmac.compare_digest(signature(key,path,stamp,nonce,body),sig):
            raise Refused(401, 'authentication failed')
        with self.lock:
            bucket = [t for t in self.rates.get(seat, []) if t > now-60]
            if len(bucket) >= MAX_REQUESTS_PER_MINUTE:
                raise Refused(429, 'rate limited')
            self.rates[seat] = bucket+[now]
            # Retain until even the future edge of this timestamp is stale.
            if self.nonce_ledger is None:
                raise Refused(503, 'replay storage unavailable')
            try:
                outcome = self.nonce_ledger.consume(seat, nonce, int(stamp)+MAX_SKEW+1, now, MAX_NONCES)
            except LedgerError:
                raise Refused(503, 'replay storage unavailable') from None
            if outcome != 'ok':
                raise Refused(409 if outcome == 'replay' else 503,
                              'replay refused' if outcome == 'replay' else 'request capacity exceeded')
        try:
            data = json.loads(body, object_pairs_hook=_unique)
            if not isinstance(data, dict) or set(data) != {'repo','permissions'}:
                raise ValueError()
            target_parts(data['repo'])
            requested = permission_set(data['permissions'])
        except (ValueError, TypeError, KeyError, RoutingError):
            raise Refused(400, 'invalid request') from None
        policy = self.policy_for(seat)
        if not isinstance(policy, CallerPolicy):
            raise Refused(403, 'caller not authorized')
        allowed = policy.repositories.get(data['repo'].casefold())
        if not isinstance(allowed, Mapping):
            raise Refused(403, 'repository not authorized')
        ranks = {'read':1,'write':2}
        if any(ranks.get(allowed.get(k),0) < ranks[v] for k,v in requested.items()):
            raise Refused(403, 'permission not authorized')
        try:
            identity = self.bot_identity(policy.app_slug)
            if identity['login'] != policy.app_slug+'[bot]' or not isinstance(identity['id'],int) or identity['id'] <= 0:
                raise ValueError()
            token = self.router.token_for(policy.app_slug, data['repo'], requested)
        except (RoutingError, ValueError, TypeError, KeyError):
            self.audit(seat=seat, repo=data['repo'], outcome='failed', at=now)
            raise Refused(502, 'token issuance failed') from None
        self.audit(seat=seat, repo=data['repo'], permissions=requested, app=policy.app_slug,
                   outcome='issued', at=now)
        return {'token':token, 'repo':data['repo'], 'permissions':requested,
                'app_slug':policy.app_slug, 'git_author_name':identity['login'],
                'git_author_email':str(identity['id'])+'+'+identity['login']+'@users.noreply.github.com'}


def _unique(pairs):
    out = {}
    for k,v in pairs:
        if k in out:
            raise ValueError('duplicate field')
        out[k]=v
    return out

class BrokerHttp:
    """HTTP transport for same-daemon lifecycle, not a new process/service.

    Bind loopback by default. Remote production needs a separately verified TLS
    reverse proxy routing ONLY this endpoint. Never deploy plain public HTTP.
    """
    def __init__(self, broker: Broker, bind='127.0.0.1', port=0):
        class Server(ThreadingHTTPServer):
            daemon_threads = True
            allow_reuse_address = True
            slots = threading.BoundedSemaphore(8)
            def get_request(self):
                s,a=super().get_request();s.settimeout(10);return s,a
            def process_request(self,s,a):
                if not self.slots.acquire(blocking=False):s.close();return
                try:super().process_request(s,a)
                except BaseException:self.slots.release();raise
            def process_request_thread(self,s,a):
                try:super().process_request_thread(s,a)
                finally:self.slots.release()
            def handle_error(self,*args):return
        class Handler(BaseHTTPRequestHandler):
            protocol_version='HTTP/1.1'
            def log_message(self,*args):return
            def respond(self,status,data):
                payload=json.dumps(data,separators=(',',':')).encode()
                self.send_response(status)
                for k,v in [('Content-Type','application/json'),('Content-Length',str(len(payload))),('Cache-Control','no-store'),('Connection','close')]:self.send_header(k,v)
                self.end_headers();self.close_connection=True
                try:self.wfile.write(payload)
                except OSError:pass
            def do_POST(self):
                try:
                    # Reject ambiguous framing and duplicate security headers.
                    for name in ['Content-Length','X-Operator-Timestamp','X-Operator-Nonce','X-Operator-Signature']:
                        if len(self.headers.get_all(name,[])) != 1:raise Refused(400,'invalid framing')
                    if self.headers.get('Transfer-Encoding') or self.headers.get('Content-Type') != 'application/json':raise Refused(400,'invalid framing')
                    length=self.headers['Content-Length']
                    if not re.fullmatch(r'[0-9]{1,5}',length) or not 1 <= int(length) <= MAX_BODY:raise Refused(400,'invalid length')
                    body=self.rfile.read(int(length))
                    if len(body)!=int(length):raise Refused(400,'incomplete request')
                    result=broker.issue(self.path,self.headers['X-Operator-Timestamp'],self.headers['X-Operator-Nonce'],self.headers['X-Operator-Signature'],body)
                    self.respond(200,result)
                except Refused as e:self.respond(e.status,{'error':e.reason})
                except Exception:self.respond(503,{'error':'broker unavailable'})
            def do_GET(self):self.respond(405,{'error':'method not allowed'})
            do_PUT=do_DELETE=do_PATCH=do_GET
        self.server=Server((bind,port),Handler)
        self.thread=None
    @property
    def address(self):return self.server.server_address
    def start(self):
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()
    def close(self):
        if self.thread:self.server.shutdown();self.thread.join(timeout=5)
        self.server.server_close()
