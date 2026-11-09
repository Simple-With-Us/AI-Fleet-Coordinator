"""Unchanged WISE own-seat read proof. ECHO proof remains external."""

import json
import os
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class RuntimeProof:
    BASELINE = "640d8554e0098c374dd3f7f1952593c4ebfc65f36fce2c1666a9b67fed678cb2"
    CANDIDATE = "30aaa6b107e241499435bc797c9d0ed40fe6bba6973f56174514b9e2ee7248e4"
    URL = "http://127.0.0.1:8471/v1/"
    REPO = "Simple-With-Us/AI-Fleet-Coordinator"

    def __init__(self, bearer, opener=None):
        self.bearer = bearer
        self.opener = opener or build_opener(ProxyHandler({}), NoRedirect())

    def call(self, action, args):
        raw = json.dumps(args, separators=(",", ":")).encode()
        request = Request(
            self.URL + action,
            data=raw,
            headers={
                "Authorization": "Bearer " + self.bearer,
                "Content-Type": "application/json",
            },
        )
        try:
            response = self.opener.open(request, timeout=15)
        except HTTPError as response:
            status = response.code
            with response:
                raw = response.read(65537)
        else:
            with response:
                status, raw = response.status, response.read(65537)
        if len(raw) > 65536:
            return None, None
        try:
            return status, json.loads(raw)
        except (ValueError, UnicodeError):
            return None, None

    def selector(self, identity):
        try:
            env = (Path("/proc") / str(identity["pid"]) / "environ").read_bytes()
            selectors = [
                v
                for v in env.split(b"\0")
                if v.split(b"=", 1)[0]
                in {
                    b"GIT_RELAY_SEATS_FILE",
                    b"GIT_RELAY_SEATS_JSON",
                    b"GIT_RELAY_SEATS_CREDENTIAL",
                }
            ]
            return selectors == [
                b"GIT_RELAY_SEATS_FILE=/var/lib/git-relay/seats.json"
            ] and os.path.samefile(
                "/var/lib/git-relay/seats.json", "/var/lib/private/git-relay/seats.json"
            )
        except (OSError, KeyError, TypeError):
            return False

    def __call__(self, identity, hashes):
        # WISE proof is a narrow unchanged-seat dispatch/read gate. Exact disk,
        # service process identity and startup selector are separate host gates.
        # ECHO supplies its own successful thread read after activation; no ECHO
        # bearer is passed to this transaction or this seat.
        if hashes not in (
            {"state/seats.json": self.BASELINE},
            {"state/seats.json": self.CANDIDATE},
        ):
            return False
        if not self.selector(identity):
            return False
        status, repo = self.call("wf_repo_get", {"repo": self.REPO})
        return (
            status == 200
            and isinstance(repo, dict)
            and isinstance(repo.get("repository"), dict)
            and repo["repository"].get("full_name") == self.REPO
            and repo["repository"].get("default_branch") == "main"
        )
