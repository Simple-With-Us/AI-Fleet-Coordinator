"""Own-seat, no-write runtime proof for this exact existing-service rollout.

The bearer is supplied from a private local file by the fixed root entry point;
never persisted in the manifest, printed, transmitted elsewhere or returned.
Default-branch refusal occurs before any provider mutation in wf_file_patch.
"""

import json
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class RuntimeProof:
    URL = "http://127.0.0.1:8471/v1/"
    REPO = "Simple-With-Us/AI-Fleet-Coordinator"

    def __init__(self, bearer, opener=None):
        self.bearer = bearer
        self.opener = opener or build_opener(NoRedirect())

    def call(self, action, args):
        raw = json.dumps(args, separators=(",", ":")).encode()
        request = Request(
            self.URL + action,
            data=raw,
            headers={"Authorization": "Bearer " + self.bearer, "Content-Type": "application/json"},
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

    def __call__(self, identity, hashes):
        # Real own-App authenticated read proves normal policy/minter path.
        status, repo = self.call("wf_repo_get", {"repo": self.REPO})
        if (
            status != 200
            or not isinstance(repo, dict)
            or repo.get("repository", {}).get("full_name") != self.REPO
            or repo.get("repository", {}).get("default_branch") != "main"
        ):
            return False
        status, result = self.call(
            "wf_file_patch",
            {
                "repo": self.REPO,
                "branch": "main",
                "path": "README.md",
                "message": "Bounded runtime negative probe: default branch must refuse",
                "expected_head_sha": "0" * 40,
                "base_blob_sha": "0" * 40,
                "edits": [{"start": 0, "end": 0, "text": ""}],
            },
        )
        if hashes.get("code/provenance_patch.py") is None:
            # Original image does not know the new action. Its refusal precedes
            # policy/outcome/provider effects and proves the old dispatch table.
            return status == 400 and result == {"error": "unknown action 'wf_file_patch'"}
        return status == 400 and result == {"error": "default branch is not writable"}
