"""Read-only own-seat proof for the exact preservation-pairs module images.

No provider write probe or synthetic surrogate is sent: only authenticated
own-App reads (wf_repo_get, wf_issue_list) against the loopback relay. The
proof bearer is staged by the qualified operator at execution time and is
never part of source, manifest, logs or command arguments.
"""

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
    BASELINE = "f84708ceb6def508441af18770da5a91547c724ffda11e5e9d18a16b5455f132"
    CANDIDATE = "3f72088feb8e69b29431fb8811a4a29d3c0e5c51cbed947de34cecf2783323a3"
    POLICY = "POLICY_SHA256_PLACEHOLDER"
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
            try:
                response = self.opener.open(request, timeout=15)
            except HTTPError as response:
                status = response.code
                with response:
                    raw = response.read(65537)
            else:
                with response:
                    status, raw = response.status, response.read(65537)
        except OSError:
            return None, None
        if len(raw) > 65536:
            return None, None
        try:
            return status, json.loads(raw)
        except (ValueError, UnicodeError):
            return None, None

    def selector(self, identity):
        try:
            pid = identity["pid"]
            if isinstance(pid, bool):
                return False
            if isinstance(pid, int):
                if pid <= 0:
                    return False
                pid = str(pid)
            if not isinstance(pid, str) or not pid.isdigit() or not pid.strip("0"):
                return False
            env = (Path("/proc") / pid / "environ").read_bytes()
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
        except (OSError, KeyError, TypeError, ValueError):
            return False

    def __call__(self, identity, hashes):
        # Narrow unchanged-seat dispatch/read gate. The engine pins the
        # unchanged provenance sentinel into both hash views
        # (deploy_runner._pin_image_generation), so discriminate on the one
        # row this transaction changes; the engine independently verifies
        # every hash against disk. Exact disk state, service process
        # identity and startup selector are separate host gates.
        if not isinstance(hashes, dict):
            return False
        image = hashes.get("code/reviewed_preservation.py")
        if image not in (self.BASELINE, self.CANDIDATE):
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
        if image == self.CANDIDATE:
            args["labels"] = ",".join(["a" * 100] * 10)
        status, data = self.call("wf_issue_list", args)
        return (
            status == 200
            and isinstance(data, dict)
            and isinstance(data.get("issues"), list)
            and data.get("complete") is False
        )
