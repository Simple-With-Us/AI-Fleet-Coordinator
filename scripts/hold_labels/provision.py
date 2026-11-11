#!/usr/bin/env python3
"""Create the fleet hold labels when a repository does not already have them.

The auto-merge reusable workflow runs this on the caller's repository.  A label
that is already present is left alone, including its color and description, so
a second run changes nothing.  Creating one needs issues:write on the caller
token.  A 403 is a denied result, not an exception: the workflow warns and
auto-merge still runs.  Response bodies are never printed.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from urllib.parse import quote

# The names hold_guard.py and auto-merge-reusable.yml already treat as holds.
# do-not-automerge is the workflow default; do-not-auto-merge is the spelling
# several repos already created.  Both have to exist.  needs-human-merge is the
# one seat bots apply when a person has to merge.
STANDARD_LABELS = (
    {
        "name": "do-not-automerge",
        "color": "B60205",
        "description": "Hold: auto-merge skips this pull request",
    },
    {
        "name": "do-not-auto-merge",
        "color": "D93F0B",
        "description": "Hold: no automatic or bot merge",
    },
    {
        "name": "do-not-merge",
        "color": "B60205",
        "description": "Hold: no one merges this pull request",
    },
    {
        "name": "needs-human-merge",
        "color": "FBCA04",
        "description": "Hold: a person merges this pull request",
    },
)

_REPO = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
_API = "https://api.github.com"


class ProvisionError(Exception):
    """A fixed code, never an upstream body."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "redirect refused", headers, fp)


def ensure_hold_labels(repo: str, transport, labels=STANDARD_LABELS) -> dict:
    """Return {"labels": [{"name", "result"}], "denied": bool}.

    result is "present" or "created".  transport(method, path, payload) returns
    (status, body).  body is read only for the label name.  An unexpected
    status raises ProvisionError.  A 403 stops the rest and sets denied.
    """
    if not isinstance(repo, str) or not _REPO.fullmatch(repo) or ".." in repo.split("/"):
        raise ProvisionError("invalid_repo")
    rows = []
    for spec in labels:
        name = spec["name"]
        path = "/repos/" + repo + "/labels/" + quote(name, safe="")
        status, body = transport("GET", path, None)
        if (
            status == 200
            and isinstance(body, dict)
            and body.get("name", "").casefold() == name.casefold()
        ):
            rows.append({"name": name, "result": "present"})
            continue
        if status == 403:
            return {"labels": rows, "denied": True}
        if status != 404:
            raise ProvisionError("label_read_failed")
        created, payload = transport("POST", "/repos/" + repo + "/labels", dict(spec))
        if (
            created == 201
            and isinstance(payload, dict)
            and payload.get("name", "").casefold() == name.casefold()
        ):
            rows.append({"name": name, "result": "created"})
            continue
        if created in (200, 422):
            # 422 is GitHub's "already exists" answer for a create that lost the race.
            rows.append({"name": name, "result": "present"})
            continue
        if created == 403:
            return {"labels": rows, "denied": True}
        raise ProvisionError("label_create_failed")
    return {"labels": rows, "denied": False}


def _github(token: str, method: str, path: str, payload):
    if not token or not path.startswith("/repos/"):
        raise ProvisionError("label_request_refused")
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(
        _API + path,
        data=data,
        method=method,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
            "User-Agent": "swu-hold-labels",
        },
    )
    opener = urllib.request.build_opener(_NoRedirect())
    try:
        with opener.open(req, timeout=20) as resp:
            raw = resp.read(65536)
            status = resp.status
    except urllib.error.HTTPError as exc:
        status = exc.code
        raw = exc.read(65536)
    except (urllib.error.URLError, TimeoutError, OSError):
        raise ProvisionError("label_request_failed") from None
    if not raw:
        return status, None
    try:
        body = json.loads(raw)
    except json.JSONDecodeError:
        return status, None
    return status, body if isinstance(body, dict) else None


def main() -> int:
    repo = os.environ.get("REPO", "")
    token = os.environ.get("GH_TOKEN", "")
    if not repo or not token:
        print("hold label provisioning is missing REPO or GH_TOKEN", file=sys.stderr)
        return 2
    try:
        result = ensure_hold_labels(
            repo, lambda method, path, payload: _github(token, method, path, payload)
        )
    except ProvisionError as exc:
        print("hold label provisioning failed: " + exc.code, file=sys.stderr)
        return 1
    if result["denied"]:
        print("::warning::hold label provisioning needs issues:write on the caller token")
        return 0
    for row in result["labels"]:
        print("hold label " + row["name"] + " " + row["result"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
