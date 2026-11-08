"""Own-seat no-write proof for the bounded provider-filename matcher rollout.

All patch requests name the separately verified default branch. The workflow
rejects that branch before content reads or writes. No new branch, ref or object
is created. The ordinary message distinguishes the old and new matchers.
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

    def patch_refusal(self, message):
        return self.call(
            "wf_file_patch",
            {
                "repo": self.REPO,
                "branch": "main",
                "path": "README.md",
                "message": message,
                "expected_head_sha": "0" * 40,
                "base_blob_sha": "0" * 40,
                "edits": [{"start": 0, "end": 0, "text": ""}],
            },
        )

    def __call__(self, identity, hashes):
        status, repo = self.call("wf_repo_get", {"repo": self.REPO})
        if (
            status != 200
            or not isinstance(repo, dict)
            or not isinstance(repo.get("repository"), dict)
            or repo["repository"].get("full_name") != self.REPO
            or repo["repository"].get("default_branch") != "main"
        ):
            return False
        new = hashes.get("code/provider_scan.py") is not None
        expected = "default branch is not writable" if new else "invalid patch message"
        for ordinary in [
            "risk-ordinary-filename-guide-for-tomorrow",
            "disk-admin-guide-for-the-server",
        ]:
            if self.patch_refusal(ordinary) != (400, {"error": expected}):
                return False
        # Synthetic full-format negative control, assembled at runtime, never a
        # credential. Refusal precedes the repo read and every write in both images.
        synthetic = "sk-proj-" + "X" * 74 + "T3BlbkFJ" + "Y" * 74
        return self.patch_refusal(synthetic) == (400, {"error": "invalid patch message"})
