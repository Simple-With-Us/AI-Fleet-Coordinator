#!/usr/bin/env python3
"""Read-only GitHub REST client for the Actions spend report.

The token stays in memory. Errors name the status and the path, never the
token and never the response body.
"""

from __future__ import annotations

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
                with urllib.request.urlopen(request, timeout=60) as response:
                    payload = json.loads(response.read().decode())
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
            except (urllib.error.URLError, TimeoutError):
                if attempt < 3:
                    self._sleep(min(2**attempt, 8))
                    continue
                raise ApiError(0, f"network error on {path}") from None
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
