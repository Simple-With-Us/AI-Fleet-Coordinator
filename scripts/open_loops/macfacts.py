"""The signed Mac facts file (`gb/mac-facts.json` in the private fleet-loops-data repo), for a host with no Mac.

    load(spec)   spec is a file path or an https URL;  the result is {"status", "reason", "document", "built_at"}

A file is used only if ALL of these hold, and otherwise it is ignored (status "absent", with the reason, and
the sources it would have supplied are reported as Missing):

  * it can be read (a regular file under 8 MB, or an https response from api.github.com or
    raw.githubusercontent.com with no redirect);
  * FLEET_FACTS_SIGNING_KEY is set in the environment;
  * `fleet-facts verify FILE --path gb/mac-facts.json` exits 0 (the contract is in the fleet_facts README):  the signature is good and
    the file was signed for that path;
  * the bytes that were verified parse as a JSON object, and that object says when it was built and was built
    less than 9 hours ago (F7 publishes every 3 hours).

This module does not check a signature itself and never reads, prints or passes the key anywhere but the
environment of the `fleet-facts verify` child.  The GitHub token for a private-repo URL comes from
FLEET_LOOPS_DATA_TOKEN, GH_TOKEN or GITHUB_TOKEN, is sent only to the two hosts above, and is never printed.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Callable, Mapping

from fleet_facts import safety
from fleet_facts.activity import parse_time
from fleet_facts.runner import Runner, which

from . import central

__all__ = ["KEY_ENV", "EXPECTED_PATH", "MAX_BYTES", "MAX_AGE_HOURS", "ALLOWED_HOSTS", "FetchError", "fetch_https", "load", "part"]

KEY_ENV = "FLEET_FACTS_SIGNING_KEY"
EXPECTED_PATH = "gb/mac-facts.json"      # the repository path the file must have been signed for
TOKEN_ENVS = ("FLEET_LOOPS_DATA_TOKEN", "GH_TOKEN", "GITHUB_TOKEN")
MAX_BYTES = 8 * 1024 * 1024
MAX_AGE_HOURS = 9.0
ALLOWED_HOSTS = ("api.github.com", "raw.githubusercontent.com")
FETCH_TIMEOUT_S = 30
VERIFY_TIMEOUT_S = 30
TIME_KEYS = ("generated_at", "published_at", "built_at")

Fetcher = Callable[[str, Mapping[str, str]], bytes]


class FetchError(Exception):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # a token must not follow a redirect
        return None


def fetch_https(url: str, headers: Mapping[str, str]) -> bytes:
    """GET `url` (https, no redirect) and return at most MAX_BYTES of the body."""
    request = urllib.request.Request(url, headers=dict(headers), method="GET")
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=FETCH_TIMEOUT_S) as response:
            body = response.read(MAX_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise FetchError("HTTP %d" % exc.code) from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise FetchError("could not connect (%s)" % type(exc).__name__) from None
    if len(body) > MAX_BYTES:
        raise FetchError("larger than %d MB" % (MAX_BYTES // (1024 * 1024)))
    return body


def _ignored(reason: str) -> dict:
    return {"status": "absent", "reason": "ignored:  " + reason, "document": None, "built_at": None}


def _read_url(spec: str, fetch: Fetcher, environ: Mapping[str, str]) -> bytes:
    parsed = urllib.parse.urlparse(spec)
    if parsed.scheme != "https" or (parsed.hostname or "").lower() not in ALLOWED_HOSTS or parsed.username or parsed.password:
        raise FetchError("the URL is not an https address on %s" % " or ".join(ALLOWED_HOSTS))
    headers = {"User-Agent": "open-loops", "Accept": "application/vnd.github.raw, application/json"}
    for name in TOKEN_ENVS:
        token = (environ.get(name) or "").strip()
        if token:
            headers["Authorization"] = "Bearer " + token
            break
    return fetch(spec, headers)


def _read_path(spec: str) -> bytes:
    path = Path(os.path.expanduser(spec))
    try:
        info = path.stat()
    except OSError:
        raise FetchError("the file cannot be read") from None
    if not stat.S_ISREG(info.st_mode):
        raise FetchError("not a regular file")
    if info.st_size > MAX_BYTES:
        raise FetchError("larger than %d MB" % (MAX_BYTES // (1024 * 1024)))
    try:
        return path.read_bytes()
    except OSError:
        raise FetchError("the file cannot be read") from None


def _verify(run: Runner, data: bytes, environ: Mapping[str, str]) -> str | None:
    """None when `fleet-facts verify` accepts these bytes, else why not."""
    key = environ.get(KEY_ENV) or ""
    if not key.strip():
        return "%s is not set, so the signature cannot be checked" % KEY_ENV
    exe = which("fleet-facts")
    if exe is None:
        return "fleet-facts is not installed, so the signature cannot be checked"
    folder = tempfile.mkdtemp(prefix="open-loops-verify-")
    try:
        os.chmod(folder, 0o700)
        target = os.path.join(folder, "mac-facts.json")
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        # `--path` pins what the file was signed for:  the full mac-facts.json (lane paths) is a different signed document and
        # must not be accepted as the Grok Bot copy if it ever reaches this reader by mistake.
        result = run([exe, "verify", target, "--path", EXPECTED_PATH], timeout=VERIFY_TIMEOUT_S, env={KEY_ENV: key})
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    if result.reason:
        return "fleet-facts verify %s" % result.reason
    if result.rc == 0:
        return None
    if result.rc == 2:
        return "this fleet-facts has no verify command yet"
    return "the signature did not verify (fleet-facts verify exited %s)" % result.rc


def load(spec: str | None, *, run: Runner, now: datetime, fetch: Fetcher = fetch_https,
         environ: Mapping[str, str] | None = None) -> dict:
    env = os.environ if environ is None else environ
    if not spec:
        return {"status": "absent", "reason": "not requested", "document": None, "built_at": None}
    try:
        raw = _read_url(spec, fetch, env) if "://" in spec else _read_path(spec)
    except FetchError as exc:
        return _ignored(safety.clean(str(exc), 120))
    if len(raw) > MAX_BYTES:
        return _ignored("larger than %d MB" % (MAX_BYTES // (1024 * 1024)))
    problem = _verify(run, raw, env)
    if problem:
        return _ignored("unverified:  " + problem)
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return _ignored("the verified file is not JSON")
    if not isinstance(document, dict):
        return _ignored("the verified file is not a JSON object")
    built = None
    for key in TIME_KEYS:
        built = parse_time(document.get(key))
        if built is not None:
            break
    if built is None:
        return _ignored("it does not say when it was built")
    age = (now - built).total_seconds() / 3600.0
    if age < -0.1:
        return _ignored("it is dated in the future")
    if age > MAX_AGE_HOURS:
        return _ignored("stale, built %s" % central.stamp(built))
    return {"status": "ok", "reason": None, "document": document, "built_at": built}


def part(document: Mapping, name: str) -> dict:
    """One part of a Mac facts document in the shape the F6 gatherers answer with:  {status, reason, data}."""
    sources = document.get("sources") if isinstance(document.get("sources"), dict) else {}
    entry = sources.get(name)
    if not isinstance(entry, dict):
        return {"status": "absent", "reason": "not in the Mac facts file", "data": None}
    status = entry.get("status")
    reason = safety.clean(entry.get("reason"), 160) or None
    data = document.get(name) if isinstance(document.get(name), dict) else None
    if status == "ok" and data is None:
        return {"status": "failed", "reason": "the Mac facts file has no data for %s" % name, "data": None}
    if status not in ("ok", "absent", "failed"):
        return {"status": "failed", "reason": "the Mac facts file has an unreadable status for %s" % name, "data": None}
    return {"status": status, "reason": reason, "data": data}
