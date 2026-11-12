#!/usr/bin/env python3
"""Read-only GitHub REST client for the Actions spend report.

The token stays in memory. Errors name the status and the path, never the
token and never the response body.
"""

from __future__ import annotations

import http.client
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request


class ApiError(Exception):
    def __init__(self, status, message):
        self.status = status
        super().__init__(message)


class RateLimited(ApiError):
    def __init__(self, message):
        super().__init__(429, message)


API_HOST = "api.github.com"
# The largest response this report reads (100 runs or jobs per page) is well
# under a megabyte.  Anything past this budget is refused before parsing.
MAX_RESPONSE_BYTES = 16 * 1024 * 1024


class _SameHostRedirects(urllib.request.HTTPRedirectHandler):
    """Follow redirects only to https://api.github.com.

    urllib copies every request header, including Authorization, onto the
    redirected request.  GitHub's own 301/307 for a renamed repository stays on
    api.github.com, so that case keeps working; any other target is refused
    before a second request (and the bearer token) leaves the process.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urllib.parse.urlsplit(newurl)
        if target.scheme != "https" or target.hostname != API_HOST:
            raise urllib.error.HTTPError(
                req.full_url, code, "refused cross-origin redirect", headers, fp
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(_SameHostRedirects)


def _read_json(response):
    body = response.read(MAX_RESPONSE_BYTES + 1)
    if len(body) > MAX_RESPONSE_BYTES:
        raise ValueError("response exceeds the size budget")
    return json.loads(body.decode())


class Client:
    def __init__(
        self,
        token,
        sleep=time.sleep,
        max_wait_s=45 * 60,
        clock=time.time,
        max_long_waits=1,
        refresh=None,
    ):
        self._token = token
        self._sleep = sleep
        self._max_wait_s = max_wait_s
        self._clock = clock
        self._max_long_waits = max_long_waits
        self._refresh = refresh
        self._refreshed_at = 0.0
        self._lock = threading.Lock()
        self.remaining = None
        self.reset_at = None
        self.calls = 0
        self._long_waits = 0
        self._waiting = False
        self.stop = False

    def __repr__(self):
        return "Client(token=[redacted])"

    def redact(self, text):
        if self._token and text and self._token in text:
            return text.replace(self._token, "[redacted]")
        return text

    def _note_headers(self, headers):
        if headers is None:
            return
        remaining = headers.get("X-RateLimit-Remaining")
        reset = headers.get("X-RateLimit-Reset")
        with self._lock:
            if remaining is not None:
                try:
                    self.remaining = int(remaining)
                except ValueError:
                    pass
            if reset is not None:
                try:
                    self.reset_at = float(reset)
                except ValueError:
                    pass

    def _begin_long_wait(self, wait):
        """One thread sleeps through a reset. The others wait without spending another."""
        with self._lock:
            if self.stop:
                raise RateLimited("rate limit stop")
            if self._waiting:
                return False
            if wait > self._max_wait_s or self._long_waits >= self._max_long_waits:
                self.stop = True
                raise RateLimited("rate limit budget exhausted")
            self._long_waits += 1
            self._waiting = True
            return True

    def _end_long_wait(self):
        with self._lock:
            self._waiting = False
            self.remaining = None

    def _refresh_token(self):
        """Ask for a new token after HTTP 401. Never logs the token."""
        if self._refresh is None:
            return False
        now = self._clock()
        with self._lock:
            if now - self._refreshed_at < 30:
                return False
            self._refreshed_at = now
        try:
            token = self._refresh()
        except Exception:
            return False
        if not token or token == self._token:
            return False
        self._token = token
        return True

    def _respect_budget(self):
        while True:
            with self._lock:
                if self.stop:
                    raise RateLimited("rate limit stop")
                remaining = self.remaining
                reset_at = self.reset_at
                waiting = self._waiting
            if not waiting and (remaining is None or remaining > 8):
                return
            wait = 1.0
            if not waiting and reset_at:
                wait = max(1.0, reset_at - self._clock() + 2)
            if waiting or not self._begin_long_wait(wait):
                self._sleep(1.0)
                continue
            self._sleep(wait)
            self._end_long_wait()
            return

    def get(self, path, params=None):
        self._respect_budget()
        query = ""
        if params:
            query = "?" + urllib.parse.urlencode(params)
        url = "https://api.github.com/" + path.lstrip("/") + query
        headers = {
            "Authorization": "Bearer " + self._token,
            "Accept": "application/vnd.github+json",
            "User-Agent": "actions-spend",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        for attempt in range(4):
            headers["Authorization"] = "Bearer " + self._token
            request = urllib.request.Request(url, headers=headers)
            try:
                with _OPENER.open(request, timeout=60) as response:
                    payload = _read_json(response)
                    self._note_headers(response.headers)
                    with self._lock:
                        self.calls += 1
                    return payload
            except urllib.error.HTTPError as exc:
                self._note_headers(exc.headers)
                exc.read()
                if exc.code == 401 and self._refresh_token():
                    continue
                if exc.code in (403, 429) and attempt < 3:
                    self._backoff(exc.headers)
                    continue
                if exc.code >= 500 and attempt < 3:
                    self._sleep(min(2**attempt, 8))
                    continue
                raise ApiError(exc.code, f"HTTP {exc.code} on {path}") from None
            except (OSError, http.client.HTTPException):
                # URLError, TimeoutError, ConnectionResetError and an
                # IncompleteRead are all transport faults worth a retry.
                if attempt < 3:
                    self._sleep(min(2**attempt, 8))
                    continue
                raise ApiError(0, f"network error on {path}") from None
            except (ValueError, RecursionError):
                # Not JSON, not UTF-8, too large, or nested past the parser's
                # recursion limit.  The body is never echoed.
                raise ApiError(0, f"unreadable response on {path}") from None
        raise RateLimited(f"rate limited on {path}")

    def _backoff(self, headers):
        wait = 5.0
        if headers is not None and headers.get("Retry-After"):
            try:
                wait = float(headers.get("Retry-After"))
            except ValueError:
                wait = 5.0
        elif self.remaining == 0 and self.reset_at:
            wait = max(1.0, self.reset_at - self._clock() + 2)
        if wait > self._max_wait_s:
            self.stop = True
            raise RateLimited(f"rate limit reset is {round(wait)} seconds away")
        if wait > 30:
            if not self._begin_long_wait(wait):
                self._sleep(1.0)
                return
            self._sleep(wait)
            self._end_long_wait()
            return
        self._sleep(wait)
