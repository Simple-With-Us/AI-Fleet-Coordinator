"""The private fleet-loops-data repository:  the gate in front of every write, and the sha-aware writer.

Nothing is written unless `guard()` has just said the repository is private and nothing but this job (and the
apps the owner allowed by name) can read what lands in it:

  * `gh repo view` says visibility PRIVATE, not archived, not a fork;
  * the repository has no webhook (a deploy webhook included) and no deploy key;
  * no GitHub App installed on the organization can read it:  an installation with access to all repositories
    always can, and one limited to selected repositories is listed through `user/installations/<id>/repositories`
    to see whether this repository is among them.  Any read that fails is a refusal, because "could not look"
    is not "nothing there".  `allowed_apps` (the K1 knob FLEET_LOOPS_DATA_ALLOWED_APPS, empty by default) names the
    app slugs the owner accepted.

Not checked, because the token that reads the rest cannot:  organization-level webhooks (they need the
admin:org_hook scope).

Writes go through the git-relay when `ContentsRelay` is configured (`wf_file_get`, `wf_file_put`, `wf_file_delete` on the repository's default branch).  `gh api -X PUT repos/<repo>/contents/<path> --input <0600 file>` (and, for `mac-facts.json` alone, `-X DELETE`) is the fallback only when no relay is configured:  the body (message, base64
content and the blob sha of the file being replaced) is in a private file, never on a command line;  there is no
clone and no force.  A configured relay that refuses is not retried through `gh`.  A path must be `mac-facts.json` or a file under `gb/`, never a dotfile, never `..`.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Iterator
from typing import Any

from .runner import Result, Runner

__all__ = [
    "REPO",
    "ContentsRelay",
    "Refused",
    "WriteFailed",
    "Writer",
    "check_path",
    "git_blob_sha",
    "guard",
    "json_stream",
]

REPO = "Simple-With-Us/fleet-loops-data"
_PATH_OK = re.compile(r"^(?:mac-facts\.json|gb(?:/[A-Za-z0-9][A-Za-z0-9._-]{0,99}){1,4})\Z")
_REPO_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}/[A-Za-z0-9._-]{1,100}\Z")
API_TIMEOUT_S = 60
SHOWN_APPS = 8


class Refused(Exception):
    """The repository (or the path) is not one this job may write to.  `reason` has no secret in it."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class WriteFailed(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def check_path(rel: str) -> str:
    """`rel` if this job may write it, else Refused.  `mac-facts.json` or a file under `gb/`;  no `..`, no dotfile."""
    if (
        not isinstance(rel, str)
        or not _PATH_OK.match(rel)
        or ".." in rel
        or rel.startswith("/")
        or "/." in rel
    ):
        raise Refused(
            "the path %r is not one this job writes (mac-facts.json or a file under gb/)"
            % (rel if isinstance(rel, str) else "?")
        )
    return rel


def git_blob_sha(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\x00" % len(data) + data).hexdigest()


def json_stream(text: str) -> Iterator[Any]:
    """The JSON values in `text`, one after another (what `gh api --paginate` prints:  one document per page)."""
    decoder = json.JSONDecoder()
    index, size = 0, len(text)
    while index < size:
        while index < size and text[index].isspace():
            index += 1
        if index >= size:
            break
        value, index = decoder.raw_decode(text, index)
        yield value


def _http(result: Result) -> str:
    match = re.search(r"HTTP (\d{3})", result.err or "")
    return match.group(1) if match else ""


def _gh(run: Runner, gh: str, *args: str, timeout: float = API_TIMEOUT_S) -> Result:
    return run([gh, *args], timeout=timeout)


def _documents(result: Result, what: str) -> list[Any]:
    try:
        return list(json_stream(result.out))
    except ValueError:
        raise Refused("%s did not answer in JSON" % what) from None


def _items(result: Result, what: str, key: str | None = None) -> list[Any]:
    """The list items over every page of an answer that is a list, or an object holding the list under `key`."""
    items: list[Any] = []
    for doc in _documents(result, what):
        if key is not None and isinstance(doc, dict) and isinstance(doc.get(key), list):
            items.extend(doc[key])
        elif key is None and isinstance(doc, list):
            items.extend(doc)
        else:
            raise Refused("%s answered in a shape this job does not know" % what)
    return items


def guard(run: Runner, gh: str, repo: str = REPO, *, allowed_apps: Iterable[str] = ()) -> list[str]:
    """Raise Refused unless `repo` is private and unreadable by anything else.  Returns the notes to report (the
    app slugs that were allowed by name)."""
    if not _REPO_OK.match(repo):
        raise Refused("the repository name is not owner/name")
    owner = repo.split("/", 1)[0]
    allowed = {str(a).strip().lower() for a in allowed_apps if str(a).strip()}

    view = _gh(run, gh, "repo", "view", repo, "--json", "visibility,isArchived,isFork")
    if view.reason or view.rc != 0:
        raise Refused(
            "cannot read the repository (gh repo view %s)" % (view.reason or "exited %s" % view.rc)
        )
    try:
        info = json.loads(view.out)
    except ValueError:
        raise Refused("gh repo view did not answer in JSON") from None
    if not isinstance(info, dict) or str(info.get("visibility", "")).upper() != "PRIVATE":
        raise Refused(
            "the repository is not private (visibility %s)"
            % str(info.get("visibility") if isinstance(info, dict) else "unknown")[:20]
        )
    if info.get("isArchived") is not False or info.get("isFork") is not False:
        raise Refused("the repository is archived or a fork")

    for label, endpoint in (
        ("webhook", "repos/%s/hooks" % repo),
        ("deploy key", "repos/%s/keys" % repo),
    ):
        listed = _gh(run, gh, "api", "--paginate", endpoint)
        if listed.reason or listed.rc != 0:
            raise Refused(
                "cannot list the repository's %ss, so none can be ruled out (needs admin on the repository)"
                % label
            )
        found = _items(listed, "the %s list" % label)
        if found:
            raise Refused(
                "the repository has %d %s%s" % (len(found), label, "" if len(found) == 1 else "s")
            )

    # `--method GET`:  gh sends a POST as soon as a field (-f) is added, and these endpoints answer a POST with 404.
    installs = _gh(
        run,
        gh,
        "api",
        "--paginate",
        "--method",
        "GET",
        "-f",
        "per_page=100",
        "orgs/%s/installations" % owner,
    )
    if installs.reason or installs.rc != 0:
        raise Refused(
            "cannot list the apps installed on %s, so none can be ruled out (needs organization admin)"
            % owner
        )
    covering: list[str] = []
    notes: list[str] = []
    for item in _items(installs, "the app installation list", "installations"):
        if not isinstance(item, dict):
            raise Refused("the app installation list has an entry this job does not know")
        slug = str(item.get("app_slug") or "").strip() or "unknown-app"
        selection = item.get("repository_selection")
        reaches = selection == "all"
        if selection == "selected":
            install_id = item.get("id")
            if not isinstance(install_id, int) or isinstance(install_id, bool):
                reaches = True
            else:
                repos = _gh(
                    run,
                    gh,
                    "api",
                    "--paginate",
                    "--method",
                    "GET",
                    "-f",
                    "per_page=100",
                    "user/installations/%d/repositories" % install_id,
                )
                if repos.reason or repos.rc != 0:
                    reaches = True  # could not look:  assume it can read
                else:
                    reaches = repo.lower() in {
                        str(r.get("full_name", "")).lower()
                        for r in _items(repos, "the app's repository list", "repositories")
                        if isinstance(r, dict)
                    }
        elif selection != "all":
            reaches = True
        if not reaches:
            continue
        if slug.lower() in allowed:
            notes.append(slug)
        else:
            covering.append(slug)
    if covering:
        shown = ", ".join(sorted(set(covering))[:SHOWN_APPS])
        more = len(set(covering)) - SHOWN_APPS
        raise Refused(
            "%d installed app%s can read the repository (%s%s);  narrow them to selected repositories without it, or name them in "
            "FLEET_LOOPS_DATA_ALLOWED_APPS"
            % (
                len(set(covering)),
                "" if len(set(covering)) == 1 else "s",
                shown,
                ", and %d more" % more if more > 0 else "",
            )
        )
    return sorted(set(notes))


_RELAY_URL = re.compile(r"https://[A-Za-z0-9.-]+(?::[0-9]{1,5})?(?:/[A-Za-z0-9._~/-]{0,200})?\Z")
_RELAY_BRANCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,120}\Z")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class ContentsRelay:
    """The seat's git-relay for the three contents calls this job makes.

    `post`, when given, replaces the HTTP call: `(action, body) -> (status, object)`.
    The token is sent only as a header and is never put in an error.
    """

    def __init__(
        self,
        url: str,
        token: str,
        branch: str,
        *,
        post: Callable[[str, dict], tuple[int, dict]] | None = None,
    ) -> None:
        if not isinstance(url, str) or not _RELAY_URL.fullmatch(url.rstrip("/")):
            raise Refused("the facts relay URL is not an https origin")
        if not isinstance(token, str) or len(token) < 16 or any(c.isspace() for c in token):
            raise Refused("the facts relay token is not usable")
        if (
            not isinstance(branch, str)
            or not _RELAY_BRANCH.fullmatch(branch)
            or ".." in branch.split("/")
        ):
            raise Refused("the facts relay branch is not a branch name")
        self.url, self.token, self.branch = url.rstrip("/"), token, branch
        self._post = post
        self._default_checked = False

    def call(self, action: str, body: dict[str, Any]) -> tuple[int, dict]:
        status, doc = (
            self._post(action, body) if self._post is not None else self._http(action, body)
        )
        if not isinstance(status, int) or not isinstance(doc, dict):
            raise WriteFailed("the relay answer was not a JSON object")
        return status, doc

    def ensure_default(self, repo: str) -> None:
        """Refuse before a write when the configured branch is not the repository default."""
        if self._default_checked:
            return
        status, doc = self.call("wf_repo_get", {"repo": repo})
        repository = doc.get("repository") if isinstance(doc.get("repository"), dict) else {}
        if status != 200 or repository.get("default_branch") != self.branch:
            raise Refused("the relay branch is not the repository default branch")
        self._default_checked = True

    def _http(self, action: str, body: dict[str, Any]) -> tuple[int, dict]:
        raw = json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            self.url + "/v1/" + action,
            data=raw,
            method="POST",
            headers={
                "Authorization": "Bearer " + self.token,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        opener = urllib.request.build_opener(_NoRedirect)
        try:
            with opener.open(request, timeout=120) as response:
                payload = response.read(1024 * 1024)
                status = response.status
        except urllib.error.HTTPError as exc:
            payload = exc.read(8192)
            status = exc.code
        except (urllib.error.URLError, TimeoutError, OSError):
            raise WriteFailed("the relay could not be reached") from None
        try:
            doc = json.loads(payload.decode("utf-8"))
        except (UnicodeError, ValueError):
            raise WriteFailed("the relay answer was not JSON") from None
        if not isinstance(doc, dict):
            raise WriteFailed("the relay answer was not a JSON object")
        return status, doc


class Writer:
    """Create or update files in the repository through the contents API.  One file at a time, sha-aware, never forced."""

    def __init__(
        self,
        run: Runner,
        gh: str,
        repo: str = REPO,
        *,
        sleep: Callable[[float], None] = time.sleep,
        relay: ContentsRelay | None = None,
    ) -> None:
        if not _REPO_OK.match(repo):
            raise Refused("the repository name is not owner/name")
        self.run, self.gh, self.repo, self.sleep, self.relay = run, gh, repo, sleep, relay

    def _relay_result(self, action: str, body: dict[str, Any]) -> Result:
        assert self.relay is not None
        status, doc = self.relay.call(action, body)
        if status == 200:
            return Result(0, json.dumps(doc), "")
        # The relay refuses a stale blob sha before GitHub writes.  The publisher's one retry
        # already treats HTTP 409 as "read the sha again", which is the right recovery.
        if doc.get("error") == "default-branch blob sha does not match":
            return Result(1, "", "HTTP 409")
        code = doc.get("github_status") if isinstance(doc.get("github_status"), int) else status
        return Result(1, "", "HTTP %s" % code)

    def _remote_sha(self, rel: str) -> str | None:
        if self.relay is not None:
            self.relay.ensure_default(self.repo)
            for attempt in (0, 1):
                got = self._relay_result(
                    "wf_file_get", {"repo": self.repo, "path": rel, "ref": self.relay.branch}
                )
                if got.rc == 0:
                    try:
                        doc = json.loads(got.out)
                    except ValueError:
                        raise WriteFailed("the contents answer for %s was not JSON" % rel) from None
                    if isinstance(doc, dict) and isinstance(doc.get("sha"), str):
                        return doc["sha"]
                    raise WriteFailed("%s is not a file in the repository" % rel)
                status = _http(got)
                if status == "404":
                    return None
                if attempt == 0 and status.startswith("5"):
                    self.sleep(5)
                    continue
                raise WriteFailed(
                    "reading %s failed (%s)" % (rel, got.reason or "HTTP %s" % (status or "?"))
                )
            raise WriteFailed("reading %s failed" % rel)
        for attempt in (0, 1):
            got = _gh(self.run, self.gh, "api", "repos/%s/contents/%s" % (self.repo, rel))
            if got.reason is None and got.rc == 0:
                try:
                    doc = json.loads(got.out)
                except ValueError:
                    raise WriteFailed("the contents answer for %s was not JSON" % rel) from None
                if (
                    isinstance(doc, dict)
                    and isinstance(doc.get("sha"), str)
                    and doc.get("type", "file") == "file"
                ):
                    return doc["sha"]
                raise WriteFailed("%s is not a file in the repository" % rel)
            status = _http(got)
            if status == "404":
                return None
            if attempt == 0 and (got.reason or status.startswith("5")):
                self.sleep(5)
                continue
            raise WriteFailed(
                "reading %s failed (%s)" % (rel, got.reason or "HTTP %s" % (status or "?"))
            )
        raise WriteFailed(
            "reading %s failed" % rel
        )  # pragma: no cover - the loop always returns or raises

    def _send(self, method: str, rel: str, body: dict[str, Any]) -> Result:
        """Relay when one is configured, otherwise `gh api` with the body in a 0600 file, never on the command line."""
        if self.relay is not None:
            if method == "PUT":
                sent = {
                    "repo": self.repo,
                    "path": rel,
                    "branch": self.relay.branch,
                    "message": body["message"],
                    "content_b64": body["content"],
                }
                if body.get("sha"):
                    sent["prior_sha"] = body["sha"]
                return self._relay_result("wf_file_put", sent)
            if method == "DELETE":
                return self._relay_result(
                    "wf_file_delete",
                    {
                        "repo": self.repo,
                        "path": rel,
                        "branch": self.relay.branch,
                        "message": body["message"],
                        "prior_sha": body["sha"],
                    },
                )
            raise WriteFailed("the relay has no contents method %s" % method)
        folder = tempfile.mkdtemp(prefix="fleet-facts-put-")
        try:
            os.chmod(folder, 0o700)
            target = os.path.join(folder, "body.json")
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(body, fh)
            return _gh(
                self.run,
                self.gh,
                "api",
                "-X",
                method,
                "repos/%s/contents/%s" % (self.repo, rel),
                "--input",
                target,
                timeout=120,
            )
        finally:
            shutil.rmtree(folder, ignore_errors=True)

    def _put(self, rel: str, data: bytes, message: str, sha: str | None) -> Result:
        body: dict[str, Any] = {
            "message": message,
            "content": base64.b64encode(data).decode("ascii"),
        }
        if sha:
            body["sha"] = sha
        return self._send("PUT", rel, body)

    def _look(self, rel: str) -> tuple[str | None, bool]:
        """(the file's blob sha or None, True) or (None, False) when it could not be read:  a look after a write that did not answer."""
        try:
            return self._remote_sha(rel), True
        except WriteFailed:
            return None, False

    def put(self, rel: str, data: bytes, message: str) -> str:
        """ "created", "updated" or "unchanged".  Raises Refused for a bad path and WriteFailed for anything else.

        A write that fails is looked at before anything else is done:  a PUT that timed out may have landed, and then the retry would
        only be refused (the sha moved), so the file is read again and a blob that matches what was sent counts as written."""
        check_path(rel)
        sha = self._remote_sha(rel)
        want = git_blob_sha(data)
        if sha == want:
            return "unchanged"
        done = "created" if sha is None else "updated"
        result = self._put(rel, data, message, sha)
        if result.reason is None and result.rc == 0:
            return done
        status = _http(result)
        fresh, readable = self._look(rel)
        if readable and fresh == want:
            return done  # it landed after all
        if status in ("409", "422"):
            # Somebody (or a race with a second run) changed the file since we read it:  use the fresh sha once, never force.
            if not readable or fresh == sha:
                raise WriteFailed(
                    "writing %s was refused (HTTP %s) and the file did not change" % (rel, status)
                )
        elif result.reason or status.startswith("5"):
            self.sleep(5)
        else:
            raise WriteFailed("writing %s failed (HTTP %s)" % (rel, status or "?"))
        retry = self._put(rel, data, message, fresh if readable else sha)
        if retry.reason is None and retry.rc == 0:
            return "updated" if (fresh if readable else sha) else "created"
        again, readable = self._look(rel)
        if readable and again == want:
            return "updated" if again is not None and (fresh if readable else sha) else "created"
        raise WriteFailed(
            "writing %s failed (%s)" % (rel, retry.reason or "HTTP %s" % (_http(retry) or "?"))
        )

    def remove(self, rel: str, message: str) -> str:
        """Delete `mac-facts.json` (the path-bearing copy a knob governs) from the branch:  "removed", or "absent" when it was not there.
        Sha-aware, never forced, and no other path can be removed by this job.  Git keeps the old commits, so this stops the file
        being read at the tip;  it does not unpublish what was already there."""
        if rel != "mac-facts.json":
            raise Refused("this job removes only mac-facts.json")
        sha = self._remote_sha(rel)
        if sha is None:
            return "absent"
        result = self._send("DELETE", rel, {"message": message, "sha": sha})
        if result.reason is None and result.rc == 0:
            return "removed"
        status = _http(result)
        fresh, readable = self._look(rel)
        if readable and fresh is None:
            return "removed"  # it went after all (or someone else removed it)
        if status in ("409", "422"):
            if not readable or fresh == sha:
                raise WriteFailed(
                    "removing %s was refused (HTTP %s) and the file did not change" % (rel, status)
                )
        elif result.reason or status.startswith("5"):
            self.sleep(5)
        else:
            raise WriteFailed("removing %s failed (HTTP %s)" % (rel, status or "?"))
        retry = self._send("DELETE", rel, {"message": message, "sha": fresh if readable else sha})
        if retry.reason is None and retry.rc == 0:
            return "removed"
        again, readable = self._look(rel)
        if readable and again is None:
            return "removed"
        raise WriteFailed(
            "removing %s failed (%s)" % (rel, retry.reason or "HTTP %s" % (_http(retry) or "?"))
        )
