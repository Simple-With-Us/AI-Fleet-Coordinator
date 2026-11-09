"""Read-only own-seat proof for the exact isolated 1614 workflow images."""

import hashlib
import json
import os
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class RuntimeProof:
    BASELINE = "33c9c8c35c2eed3af01a43b810e3672ea85f3418d1ba45f7eabc182040a2fc1b"
    CANDIDATE = "88c1b914e3fcf4a6ad691e4076d1b508b483588c627c4916c4747a54f5b91604"
    POLICY = "30aaa6b107e241499435bc797c9d0ed40fe6bba6973f56174514b9e2ee7248e4"
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
            return (
                selectors == [b"GIT_RELAY_SEATS_FILE=/var/lib/git-relay/seats.json"]
                and os.path.samefile(
                    "/var/lib/git-relay/seats.json",
                    "/var/lib/private/git-relay/seats.json",
                )
                and hashlib.sha256(
                    Path("/var/lib/private/git-relay/seats.json").read_bytes()
                ).hexdigest()
                == self.POLICY
            )
        except (OSError, KeyError, TypeError):
            return False

    def __call__(self, identity, hashes):
        # WISE proof is a narrow unchanged-seat dispatch/read gate. Exact disk,
        # service process identity and startup selector are separate host gates.
        # ECHO supplies its own successful thread read after activation; no ECHO
        # bearer is passed to this transaction or this seat.
        if hashes not in (
            {"code/workflow_verbs.py": self.BASELINE},
            {"code/workflow_verbs.py": self.CANDIDATE},
        ):
            return False
        if not self.selector(identity):
            return False
        status, repo = self.call("wf_repo_get", {"repo": self.REPO})
        if not (
            status == 200
            and isinstance(repo, dict)
            and isinstance(repo.get("repository"), dict)
            and repo["repository"].get("full_name") == self.REPO
            and repo["repository"].get("default_branch") == "main"
        ):
            return False
        args = {"repo": self.REPO, "per_page": 1}
        if hashes == {"code/workflow_verbs.py": self.CANDIDATE}:
            args["labels"] = ",".join(["a" * 100] * 10)
        status, data = self.call("wf_issue_list", args)
        return (
            status == 200
            and isinstance(data, dict)
            and isinstance(data.get("issues"), list)
            and data.get("complete") is False
        )
