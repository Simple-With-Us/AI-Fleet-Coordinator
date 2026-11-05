#!/usr/bin/env python3
"""Account usage watchdog for one Cloudflare account, run from a one-minute systemd timer.

One GraphQL query per run.  Rules live in config.json, per Worker or resource and per metric:
day-to-date over a threshold, and a 5-minute rate over a threshold.  Two named profiles:  paid
(runaway catchers) and free (80% of the Free caps).  Two modes:  observe (default, alerts only)
and enforce.  See README.md for the design, the stop action and the operating steps.

Standard library only, plus this repo's stdlib-only fleet_knobs for the mode and the profile.
Credentials come from fleet-secrets, live in memory only and never reach a log or a message.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import http.client
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.json"
REGISTRY_PATH = HERE.parent / "worker-registry.json"
API_BASE = "https://api.cloudflare.com/client/v4"
PUSHOVER_URL = "https://api.pushover.net/1/messages.json"
CHICAGO = ZoneInfo("America/Chicago")
GAP = "  "  # Between sentences in owner-facing text.
GUARD_SECRET = "GUARD_MODE"
TRIP_MARKER = "GUARD-TRIPPED"
UNOWNED = {"__unknown__", "(account)", ""}
EVENTS_MAX_BYTES = 5_000_000
ALERT_LINES_MAX = 20
INVENTORY_RETRY_SECONDS = 900
INVENTORY_WANTED_RETRY_SECONDS = 60
INVENTORY_MAX_AGE_SECONDS = 86400
ACCOUNT_ID_ENV = "CF_USAGE_WATCHDOG_ACCOUNT_ID"
ACCOUNT_ID_SHAPE = re.compile(r"[0-9a-f]{32}")
MINUTE_SHAPE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:00Z")
MAX_RESPONSE_BYTES = 32 * 1024 * 1024
MAX_ERROR_BYTES = 64 * 1024
ROW_LIMIT = 2000
STORAGE_ROW_LIMIT = 500


class ResponseTooLarge(http.client.HTTPException):
    """An answer past MAX_RESPONSE_BYTES, refused before it is parsed."""


NETWORK_ERRORS = (urllib.error.URLError, OSError, http.client.HTTPException)


class PollError(Exception):
    """The usage read failed.  Never leads to a trip."""


class StateCorrupt(Exception):
    """state.json exists and cannot be trusted.  Its text is never carried here."""


class ApiError(Exception):
    """A Cloudflare API call failed.  Carries a status and error codes, never a header or a body."""

    def __init__(self, status: int, codes: Iterable[Any] = ()) -> None:
        self.status = status
        self.codes = list(codes)
        super().__init__(f"HTTP {status} codes {self.codes}")


def error_codes(errors: Any) -> list:
    """Machine codes only:  an int or an identifier.  Anything else in an error body is dropped."""
    if not isinstance(errors, list):
        return []
    codes = [item.get("code") for item in errors if isinstance(item, dict)]
    return [
        code
        for code in codes
        if (isinstance(code, int) and not isinstance(code, bool))
        or (isinstance(code, str) and code.isidentifier())
    ]


def is_transient(status: int) -> bool:
    """A failure that may pass:  a network fault (0), a timeout, a rate limit or a server error."""
    return status == 0 or status in (408, 429) or status >= 500


class NoRedirects(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect.  urllib would otherwise copy the Authorization header to the new host.
    Cloudflare's API does not redirect, so a 3xx surfaces as an HTTPError and a failed call."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


NO_REDIRECT_OPENER = urllib.request.build_opener(NoRedirects)


# ---------------------------------------------------------------- time


def minute_key(moment: dt.datetime) -> str:
    return moment.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:00Z")


def floor_minute(moment: dt.datetime) -> dt.datetime:
    return moment.astimezone(dt.UTC).replace(second=0, microsecond=0)


def local_clock(moment: dt.datetime) -> str:
    """The time owners read:  America/Chicago, 12-hour, no zone label."""
    return moment.astimezone(CHICAGO).strftime("%-I:%M %p")


# ---------------------------------------------------------------- the one query

DATASETS = (
    ("wd", "workersInvocationsAdaptive", "day", "sum{requests cpuTimeUs}", "scriptName"),
    (
        "wm",
        "workersInvocationsAdaptive",
        "rate",
        "sum{requests cpuTimeUs}",
        "scriptName datetimeMinute",
    ),
    ("dd", "durableObjectsInvocationsAdaptiveGroups", "day", "sum{requests}", "scriptName"),
    (
        "dm",
        "durableObjectsInvocationsAdaptiveGroups",
        "rate",
        "sum{requests}",
        "scriptName datetimeMinute",
    ),
    (
        "pd",
        "durableObjectsPeriodicGroups",
        "day",
        "sum{activeTime rowsRead rowsWritten}",
        "namespaceId",
    ),
    (
        "pm",
        "durableObjectsPeriodicGroups",
        "rate",
        "sum{activeTime rowsRead rowsWritten}",
        "namespaceId datetimeMinute",
    ),
    ("d1d", "d1AnalyticsAdaptiveGroups", "day", "sum{rowsRead rowsWritten}", "databaseId"),
    (
        "d1m",
        "d1AnalyticsAdaptiveGroups",
        "rate",
        "sum{rowsRead rowsWritten}",
        "databaseId datetimeMinute",
    ),
    ("kvd", "kvOperationsAdaptiveGroups", "day", "sum{requests}", "namespaceId actionType"),
    (
        "kvm",
        "kvOperationsAdaptiveGroups",
        "rate",
        "sum{requests}",
        "namespaceId actionType datetimeMinute",
    ),
    ("r2d", "r2OperationsAdaptiveGroups", "day", "sum{requests}", "bucketName actionType"),
    (
        "r2m",
        "r2OperationsAdaptiveGroups",
        "rate",
        "sum{requests}",
        "bucketName actionType datetimeMinute",
    ),
    ("r2o", "r2OperationsAdaptiveGroups", "month", "sum{requests}", "bucketName actionType"),
)


def build_query() -> str:
    window_vars = {
        "day": ("$dayStart", "$now"),
        "month": ("$monthStart", "$now"),
        "rate": ("$rateStart", "$rateEnd"),
    }
    parts = []
    for alias, dataset, window, fields, dims in DATASETS:
        start, end = window_vars[window]
        parts.append(
            f"{alias}:{dataset}(limit:{ROW_LIMIT},filter:{{datetime_geq:{start},datetime_lt:{end}}})"
            f"{{{fields} dimensions{{{dims}}}}}"
        )
    parts.append(
        f"r2s:r2StorageAdaptiveGroups(limit:{STORAGE_ROW_LIMIT},filter:{{datetime_geq:$storageStart,datetime_lt:$now}},"
        "orderBy:[datetime_DESC]){max{payloadSize metadataSize} dimensions{bucketName datetime}}"
    )
    return (
        "query($account:String!,$now:Time!,$dayStart:Time!,$monthStart:Time!,$rateStart:Time!,"
        "$rateEnd:Time!,$storageStart:Time!){viewer{accounts(filter:{accountTag:$account}){"
        + " ".join(parts)
        + "}}}"
    )


def query_variables(cfg: dict, now: dt.datetime) -> dict:
    floor = floor_minute(now)
    lag = cfg["limits"]["lag_minutes"]
    window = cfg["limits"]["rate_window_minutes"]
    rate_end = floor - dt.timedelta(minutes=lag)
    iso = lambda moment: moment.strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "account": cfg["account_id"],
        "now": iso(now.astimezone(dt.UTC)),
        "dayStart": iso(floor.replace(hour=0, minute=0)),
        "monthStart": iso(floor.replace(day=1, hour=0, minute=0)),
        "rateStart": iso(rate_end - dt.timedelta(minutes=window)),
        "rateEnd": iso(rate_end),
        "storageStart": iso(floor - dt.timedelta(hours=6)),
    }


def r2_class(action: str) -> str | None:
    """Cloudflare's R2 pricing classes.  An action this does not know bills as Class A, the dear one."""
    if action.startswith(("Delete", "Abort")):
        return None
    if action.startswith(("Get", "Head")) or action in ("ReportUsageSummary", "UsageSummary"):
        return "r2.class_b"
    return "r2.class_a"


def empty_usage() -> dict:
    return {"day": {}, "month": {}, "level": {}, "minutes": {}}


def _bump(
    usage: dict, window: str, metric: str, resource: str, value: float, minute: str | None = None
) -> None:
    if not value:
        return
    if window == "rate":
        bucket = usage["minutes"].setdefault(metric, {}).setdefault(resource, {})
        bucket[minute] = bucket.get(minute, 0) + value
    else:
        bucket = usage[window].setdefault(metric, {})
        bucket[resource] = bucket.get(resource, 0) + value


def _is_count(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, bool) or not isinstance(value, int | float):
        return False
    return math.isfinite(value) and value >= 0


def _check_rows(alias: str, rows: Any, limit: int, block: str, fields: list, dims: list) -> None:
    def bad() -> PollError:
        return PollError(f"graphql-malformed-{alias}")

    if not isinstance(rows, list) or len(rows) > limit:
        raise bad()
    for row in rows:
        if not isinstance(row, dict):
            raise bad()
        values, dimensions = row.get(block), row.get("dimensions")
        if not isinstance(values, dict) or not isinstance(dimensions, dict):
            raise bad()
        # GraphQL returns every field it was asked for, so a missing one is not an answer to this
        # query.  A null is legal in its schema and counts as nothing.
        if any(field not in values or not _is_count(values[field]) for field in fields):
            raise bad()
        for dim in dims:
            value = dimensions.get(dim, 0)
            if value is not None and not isinstance(value, str):
                raise bad()
        if "datetimeMinute" in dims and not MINUTE_SHAPE.fullmatch(
            str(dimensions.get("datetimeMinute"))
        ):
            raise bad()


def validate_account(account: Any) -> None:
    """Refuse an answer that is not the shape this query asked for, before anything reads it.  A
    refusal is a failed poll:  it alerts and never trips."""
    if not isinstance(account, dict):
        raise PollError("graphql-malformed-account")
    for alias, _, _, fields, dims in DATASETS:
        block, inner = fields.rstrip("}").split("{")
        _check_rows(alias, account.get(alias), ROW_LIMIT, block, inner.split(), dims.split())
    _check_rows(
        "r2s",
        account.get("r2s"),
        STORAGE_ROW_LIMIT,
        "max",
        ["payloadSize", "metadataSize"],
        ["bucketName", "datetime"],
    )


def capped_datasets(account: dict) -> list:
    """Datasets that came back full, so they may have lost rows and their totals and top contributor
    cannot be trusted.  r2s is left out:  it is newest first, a lost row is an old reading, and it
    feeds only the alert-only storage metric."""
    return sorted(alias for alias, *_ in DATASETS if len(account.get(alias) or []) >= ROW_LIMIT)


def parse_usage(account: dict, ns_to_script: dict) -> dict:
    usage = empty_usage()
    window_of = {alias: window for alias, _, window, _, _ in DATASETS}
    for alias, window in window_of.items():
        for row in account.get(alias) or []:
            total = {key: value or 0 for key, value in (row.get("sum") or {}).items()}
            dims = {key: value or "" for key, value in (row.get("dimensions") or {}).items()}
            minute = dims.get("datetimeMinute")
            add = lambda metric, resource, value: _bump(
                usage, window, metric, resource, value, minute
            )
            if alias.startswith("w"):
                add("workers.requests", dims.get("scriptName", ""), total.get("requests", 0))
                add("workers.cpu_ms", dims.get("scriptName", ""), total.get("cpuTimeUs", 0) / 1000)
            elif alias in ("dd", "dm"):
                add("do.requests", dims.get("scriptName", ""), total.get("requests", 0))
            elif alias in ("pd", "pm"):
                owner = ns_to_script.get(dims.get("namespaceId", ""), dims.get("namespaceId", ""))
                # activeTime is in microseconds;  Durable Objects bill 128 MB per active second.
                add("do.gb_s", owner, total.get("activeTime", 0) / 1_000_000 * 0.128)
                add("do.rows_written", owner, total.get("rowsWritten", 0))
                add("do.rows_read", owner, total.get("rowsRead", 0))
            elif alias.startswith("d1"):
                add("d1.rows_written", dims.get("databaseId", ""), total.get("rowsWritten", 0))
                add("d1.rows_read", dims.get("databaseId", ""), total.get("rowsRead", 0))
            elif alias.startswith("kv"):
                action = dims.get("actionType", "")
                if action in ("read", "write", "delete", "list"):
                    add(f"kv.{action}", dims.get("namespaceId", ""), total.get("requests", 0))
            else:
                metric = r2_class(dims.get("actionType", ""))
                if metric:
                    add(metric, dims.get("bucketName") or "(account)", total.get("requests", 0))
    seen = set()
    for row in (
        account.get("r2s") or []
    ):  # Newest first:  the first row for a bucket is its current size.
        bucket = (row.get("dimensions") or {}).get("bucketName") or ""
        if bucket in seen:
            continue
        seen.add(bucket)
        maximum = row.get("max") or {}
        size = ((maximum.get("payloadSize") or 0) + (maximum.get("metadataSize") or 0)) / 1e9
        _bump(usage, "level", "r2.storage_gb", bucket, size)
    return usage


# ---------------------------------------------------------------- rules


def usd_estimate(cfg: dict, metric: str, amount: float) -> float:
    spec = cfg["metrics"][metric]
    if "usd_per_gb_month" in spec:
        return amount * spec["usd_per_gb_month"]
    return amount * spec["usd_per_million"] / 1_000_000


def rate_total(usage: dict, metric: str, resource: str) -> float:
    return sum(usage["minutes"].get(metric, {}).get(resource, {}).values())


def _breach(
    cfg: dict,
    metric: str,
    resource: str,
    window: str,
    value: float,
    threshold: float,
    scope: str,
    top: str,
) -> dict:
    return {
        "key": f"{metric}|{resource}|{window}",
        "metric": metric,
        "resource": resource,
        "window": window,
        "value": value,
        "threshold": threshold,
        "scope": scope,
        "top": top,
        "usd": round(usd_estimate(cfg, metric, value), 2),
    }


def evaluate(cfg: dict, profile_name: str, usage: dict) -> list:
    profile = cfg["profiles"][profile_name]
    breaches = []
    for metric, rule in profile.get("per_resource", {}).items():
        resources = set(usage["day"].get(metric, {})) | set(usage["minutes"].get(metric, {}))
        resources |= set(usage["month"].get(metric, {})) | set(usage["level"].get(metric, {}))
        for resource in sorted(resources):
            limits = {**rule, **profile.get("overrides", {}).get(resource, {}).get(metric, {})}
            for window in ("day", "month", "level"):
                value = usage[window].get(metric, {}).get(resource, 0)
                if window in limits and value >= limits[window]:
                    breaches.append(
                        _breach(
                            cfg,
                            metric,
                            resource,
                            window,
                            value,
                            limits[window],
                            "resource",
                            resource,
                        )
                    )
            value = rate_total(usage, metric, resource)
            if "rate_5m" in limits and value >= limits["rate_5m"]:
                breaches.append(
                    _breach(
                        cfg,
                        metric,
                        resource,
                        "rate_5m",
                        value,
                        limits["rate_5m"],
                        "resource",
                        resource,
                    )
                )
    for metric, limits in profile.get("account", {}).items():
        for window in ("day", "month", "level"):
            per = usage[window].get(metric, {})
            value = sum(per.values())
            if window in limits and value >= limits[window]:
                top = max(per, key=per.get) if per else ""
                breaches.append(
                    _breach(cfg, metric, "(account)", window, value, limits[window], "account", top)
                )
        per_rate = {
            res: sum(mins.values()) for res, mins in usage["minutes"].get(metric, {}).items()
        }
        value = sum(per_rate.values())
        if "rate_5m" in limits and value >= limits["rate_5m"]:
            top = max(per_rate, key=per_rate.get) if per_rate else ""
            breaches.append(
                _breach(
                    cfg, metric, "(account)", "rate_5m", value, limits["rate_5m"], "account", top
                )
            )
    return breaches


def names_its_script(metric: str) -> bool:
    """These metrics name a script in the data.  Every other one needs the inventory to find an owner."""
    return metric.startswith("workers.") or metric == "do.requests"


def owners_for(metric: str, resource: str, inventory: dict) -> list:
    """The Workers a breach can be stopped through.  None for a resource no Worker binds (an S3 client)."""
    workers = inventory.get("workers", [])
    if metric.startswith(("workers.", "do.")):
        if resource in UNOWNED:
            return []
        # So a Worker newer than the inventory can still stop.
        if names_its_script(metric):
            return [resource]
        # The others hold a namespace id until the inventory maps it to a script.
        return [resource] if resource in workers else []
    family = metric.split(".", 1)[0]
    return sorted(inventory.get("owners", {}).get(family, {}).get(resource, []))


def trip_targets(cfg: dict, profile_name: str, breach: dict, inventory: dict) -> list:
    if cfg["metrics"][breach["metric"]].get("alert_only"):
        return []
    if breach["scope"] == "account" and not cfg["profiles"][profile_name].get(
        "trip_top_contributor"
    ):
        return []
    return owners_for(breach["metric"], breach["top"], inventory)


# ---------------------------------------------------------------- persistence and log


class Store:
    """state.json and events.jsonl in one private directory."""

    def __init__(self, directory: Path) -> None:
        if directory.is_symlink():
            raise ValueError("the watchdog's state directory must not be a symlink")
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.state_path = directory / "state.json"
        self.events_path = directory / "events.jsonl"

    @contextlib.contextmanager
    def lock(self):
        """One writer of the state file at a time:  the timer's run or a manual re-arm."""
        with open(self.directory / ".lock", "w", encoding="utf-8") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            yield

    def load(self) -> dict:
        """The state, or StateCorrupt.  No file is a first run.  A file that cannot be trusted must
        not read as empty:  that would forget trips, the data re-arm restores from, and the caps."""
        state: Any = {}
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            pass
        except OSError:
            raise StateCorrupt("unreadable") from None
        except (ValueError, RecursionError):
            raise StateCorrupt("not-json") from None
        if not isinstance(state, dict):
            raise StateCorrupt("not-an-object")
        for key in ("alerted", "trips", "inventory", "trips_by_day"):
            if not isinstance(state.setdefault(key, {}), dict):
                raise StateCorrupt(f"bad-{key}")
        return state

    def quarantine(self, reason: str, epoch: float) -> dict:
        """Call under the lock.  Keeps the bad file and starts a state that remembers the loss, so
        the next run cannot read a missing file as a first run.  Enforcement stays off until
        `watchdog.py state-ack`."""
        kept = f"state.corrupt-{int(epoch)}.json"
        try:
            os.replace(self.state_path, self.directory / kept)
        except OSError:
            kept = "no copy, it could not be moved"
        self.event("state_quarantined", reason=reason, kept=kept)
        state = {
            "alerted": {},
            "trips": {},
            "inventory": {},
            "trips_by_day": {},
            "state_lost_at": epoch,
            "state_lost_kept": kept,
        }
        self.save(state)
        return state

    def save(self, state: dict) -> None:
        fd, tmp = tempfile.mkstemp(dir=self.directory, prefix=".state-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(state, handle, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.state_path)
            directory = os.open(self.directory, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    def event(self, name: str, **fields: Any) -> None:
        line = json.dumps(
            {"at": dt.datetime.now(CHICAGO).isoformat(timespec="seconds"), "event": name, **fields},
            sort_keys=True,
        )
        print(line, flush=True)
        try:
            if self.events_path.exists() and self.events_path.stat().st_size > EVENTS_MAX_BYTES:
                os.replace(self.events_path, self.events_path.with_suffix(".jsonl.1"))
            fd = os.open(self.events_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError as exc:
            print(
                json.dumps({"event": "event_log_failed", "error": type(exc).__name__}), flush=True
            )


# ---------------------------------------------------------------- real connections


class Connections:
    """Everything that leaves this process.  Tests replace the whole object."""

    def __init__(
        self,
        cfg: dict,
        runner: Callable = subprocess.run,
        opener: Callable = NO_REDIRECT_OPENER.open,
        env: dict | None = None,
    ) -> None:
        self.cfg = cfg
        self.runner = runner
        self.opener = opener
        self.env = dict(os.environ if env is None else env)
        self.timeout = cfg["limits"]["http_timeout_seconds"]
        self._token: str | None = None
        self._heartbeat_url: str | None = None

    def account_id(self) -> str:
        """Not a credential, so unlike the secrets below the environment may name it, which is how
        a manual run or a second host would.  Otherwise it comes from Infisical."""
        value = self.env.get(ACCOUNT_ID_ENV) or self._secret(
            self.cfg["infisical"]["account_id_key"]
        )
        if not value:
            raise PollError(f"account-id-absent-{ACCOUNT_ID_ENV}")
        if not ACCOUNT_ID_SHAPE.fullmatch(value):
            raise PollError(f"account-id-malformed-{ACCOUNT_ID_ENV}")
        return value

    def _secret(self, name: str) -> str | None:
        """One value from fleet-secrets, or None when it is absent.  Raises PollError on any other failure."""
        conf = self.cfg["infisical"]
        command = [
            "fleet-secrets",
            "get",
            name,
            "--path",
            conf["path"],
            "--project",
            conf["project"],
        ]
        try:
            done = self.runner(
                command, capture_output=True, text=True, timeout=30, check=False, env=self.env
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise PollError(f"credential-fetch-{type(exc).__name__}") from None
        if done.returncode == 3:
            return None
        if done.returncode != 0:
            raise PollError(f"credential-fetch-exit-{done.returncode}")
        return done.stdout.strip() or None

    def token(self) -> str:
        if self._token is None:
            self._token = self._secret(self.cfg["infisical"]["api_token_key"])
            if not self._token:
                raise PollError("credential-absent")
        return self._token

    def _request(
        self, method: str, url: str, body: bytes | None = None, headers: dict | None = None
    ) -> tuple:
        request = urllib.request.Request(url, data=body, method=method, headers=headers or {})
        with self.opener(request, timeout=self.timeout) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise ResponseTooLarge("response-too-large")
            return response.status, raw

    def _cloudflare(self, method: str, path: str, body: Any = None) -> dict:
        headers = {
            "Authorization": f"Bearer {self.token()}",
            "Content-Type": "application/json",
            "User-Agent": "cf-usage-watchdog",
        }
        data = None if body is None else json.dumps(body).encode("utf-8")
        try:
            _, raw = self._request(method, API_BASE + path, data, headers)
        except urllib.error.HTTPError as exc:
            codes = []
            try:
                body = json.loads(exc.read(MAX_ERROR_BYTES))
                codes = error_codes(body.get("errors"))
            except (ValueError, AttributeError, OSError, RecursionError):
                pass
            finally:
                exc.close()
            raise ApiError(exc.code, codes) from None
        except NETWORK_ERRORS as exc:
            raise ApiError(0, [type(exc).__name__]) from None
        if not raw:
            return {}
        try:
            answer = json.loads(raw)
        except (ValueError, RecursionError):
            raise ApiError(0, ["not-json"]) from None
        if not isinstance(answer, dict):
            raise ApiError(0, ["not-json"])
        return answer

    def graphql(self, query: str, variables: dict) -> dict:
        try:
            answer = self._cloudflare("POST", "/graphql", {"query": query, "variables": variables})
        except ApiError as exc:
            raise PollError(f"graphql-http-{exc.status}") from None
        if answer.get("errors"):
            # Cloudflare's own code, such as budget, says whether it is a quota or a real fault.
            codes = {
                str((item.get("extensions") or {}).get("code", ""))
                for item in answer["errors"]
                if isinstance(item, dict)
            }
            named = "-".join(sorted(code for code in codes if code.isidentifier()))
            raise PollError(f"graphql-errors-{len(answer['errors'])}-{named or 'uncoded'}")
        accounts = ((answer.get("data") or {}).get("viewer") or {}).get("accounts") or []
        if len(accounts) != 1:
            raise PollError("graphql-no-account")
        return accounts[0]

    def api(self, method: str, path: str, body: Any = None) -> Any:
        answer = self._cloudflare(method, path, body)
        if answer.get("success") is False:
            raise ApiError(200, error_codes(answer.get("errors")))
        return answer.get("result")

    def zulip(self, text: str) -> bool:
        conf = self.cfg["zulip"]
        env = {key: value for key, value in self.env.items() if not key.startswith("INFISICAL_")}
        env["AGENT_SEAT"] = conf["seat"]
        # The seat wrapper turns on hosted reads only;  without this a post is refused with exit 2.
        env["SIMPLE_CHAT_REMOTE_WRITES"] = "1"
        command = [
            "simple-chat",
            "post",
            "--channel",
            conf["channel"],
            "--topic",
            conf["topic"],
            "-",
        ]
        try:
            done = self.runner(
                command,
                input=text,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
                env=env,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            print(json.dumps({"event": "zulip_failed", "error": type(exc).__name__}), flush=True)
            return False
        if done.returncode != 0:
            print(json.dumps({"event": "zulip_failed", "exit": done.returncode}), flush=True)
        return done.returncode == 0

    def page(self, title: str, text: str) -> bool | None:
        """Pushover directly, the one path that does not go through a Worker on this account.
        None means it is not configured."""
        conf = self.cfg["infisical"]
        try:
            token = self._secret(conf["pushover_token_key"])
            user = self._secret(conf["pushover_user_key"])
        except PollError as exc:
            # A PollError carries a type name, an exit code or a key name, never a value.
            print(json.dumps({"event": "page_failed", "error": str(exc)}), flush=True)
            return False
        if not token or not user:
            return None
        form = urllib.parse.urlencode(
            {"token": token, "user": user, "title": title, "message": text[:1000], "priority": 1}
        )
        try:
            status, _ = self._request("POST", PUSHOVER_URL, form.encode("utf-8"))
        except NETWORK_ERRORS as exc:
            print(json.dumps({"event": "page_failed", "error": type(exc).__name__}), flush=True)
            return False
        return status == 200

    def page_configured(self) -> bool | None:
        """Whether a page could go out, without sending one.  None when the read itself failed."""
        conf = self.cfg["infisical"]
        try:
            return bool(
                self._secret(conf["pushover_token_key"]) and self._secret(conf["pushover_user_key"])
            )
        except PollError:
            return None

    def heartbeat(self) -> bool | None:
        """Tell the dead-man monitor this poll worked.  None means it is not configured."""
        if self._heartbeat_url is None:
            try:
                self._heartbeat_url = self._secret(self.cfg["infisical"]["heartbeat_url_key"]) or ""
            except PollError:
                return False
        if not self._heartbeat_url.startswith("https://"):
            return None
        try:
            status, _ = self._request("GET", self._heartbeat_url)
        except NETWORK_ERRORS:
            return False
        return status < 300


# ---------------------------------------------------------------- the actions


def label_live_deployment(conn: Any, path: str, message: str) -> Any:
    """A new deployment of the live split as it stands, carrying `message`.  The deployers read only
    the newest deployment's message.  Cloudflare's create call takes no precondition, so a deploy that
    lands between the read and the post is replaced by the older split;  the README says more."""
    result = conn.api("GET", f"{path}/deployments")
    try:
        versions = [
            {"version_id": item["version_id"], "percentage": item["percentage"]}
            for item in result["deployments"][0]["versions"]
        ]
    except (KeyError, IndexError, TypeError):
        raise ApiError(0, ["no-live-deployment"]) from None
    return conn.api(
        "POST",
        f"{path}/deployments",
        {
            "strategy": "percentage",
            "versions": versions,
            "annotations": {"workers/message": message[:1000]},
        },
    )


class Watchdog:
    def __init__(
        self,
        cfg: dict,
        conn: Any,
        store: Store,
        state: dict,
        now: dt.datetime,
        mode: str,
        profile: str,
    ) -> None:
        self.cfg = cfg
        self.conn = conn
        self.store = store
        self.state = state
        self.now = now
        self.epoch = now.timestamp()
        # A lost state file holds the trip records, so nothing acts until an owner has looked.
        self.mode = "observe" if state.get("state_lost_at") else mode
        self.profile = profile
        self.limits = cfg["limits"]
        self.undelivered = False
        self.worker_failed = False
        self.capped: list = []
        self.last_failure_status = 0
        self.beat: bool | None = None
        self._usage = empty_usage()

    @property
    def base(self) -> str:
        return f"/accounts/{self.cfg['account_id']}"

    # ---- messages

    def notify(self, title: str, lines: list, page: bool = True) -> bool:
        shown = lines[:ALERT_LINES_MAX]
        if len(lines) > ALERT_LINES_MAX:
            shown.append(f"And {len(lines) - ALERT_LINES_MAX} more in the watchdog log.")
        text = "\n".join(
            [
                f"**{title}** ({local_clock(self.now)}, {self.mode} mode, {self.profile} profile)",
                *shown,
            ]
        )
        zulip_ok = self.conn.zulip(text)
        paged = self.conn.page(title, "\n".join(shown)) if page else None
        self.store.event("notify", title=title, zulip=zulip_ok, page=paged)
        delivered = bool(zulip_ok or paged)
        if not delivered:
            self.undelivered = True
        return delivered

    def describe(self, breach: dict, previous: dict) -> str:
        spec = self.cfg["metrics"][breach["metric"]]
        window = {"day": "today", "month": "this month", "level": "now", "rate_5m": "in 5 minutes"}[
            breach["window"]
        ]
        parts = [
            f"{spec['label']} on {breach['resource']}:  {breach['value']:,.0f} {window}, over {breach['threshold']:,.0f}, about ${breach['usd']:,.2f}."
        ]
        if breach["top"] != breach["resource"]:
            parts.append(f"Largest contributor:  {breach['top']}.")
        for worker, version in previous.items():
            parts.append(f"{worker} is on version {version}.")
        return GAP.join(parts)

    # ---- reading the live deployment

    def current_version(self, worker: str) -> str:
        try:
            result = self.conn.api("GET", f"{self.base}/workers/scripts/{worker}/deployments")
            versions = result["deployments"][0]["versions"]
            return ",".join(item["version_id"] for item in versions)
        except (ApiError, PollError, KeyError, IndexError, TypeError):
            return "unknown"

    # ---- stage 2:  the Worker's own off switch

    def _step(self, worker: str, action: str, previous_version: str, call: Callable) -> Any:
        """One logged action and exactly one row, whatever happens.  Returns the call's result, or
        False when it failed.  An unexpected error is a failed step, so the next poll retries it."""
        name = "action_failed"
        outcome: dict = {"status": 0, "codes": []}
        try:
            result = call()
            name, outcome = "action", {}
            return True if result is None else result
        except ApiError as exc:
            self.last_failure_status = exc.status
            outcome = {"status": exc.status, "codes": exc.codes}
            return False
        except Exception as exc:  # noqa: BLE001 - The type only:  its text could carry a response body.
            self.last_failure_status = 0
            outcome = {"status": 0, "codes": [type(exc).__name__]}
            return False
        finally:
            self.store.event(
                name, worker=worker, action=action, previous_version=previous_version, **outcome
            )

    def _worker_failed(self, worker: str, stage: str, exc: Exception) -> None:
        """One Worker's surprise must not stop the others'.  The type only, and one alert an hour."""
        self.worker_failed = True
        kind = type(exc).__name__
        self.store.event("worker_failed", worker=worker, stage=stage, error=kind)
        key = f"worker_failed|{worker}"
        if self.epoch - self.state["alerted"].get(key, 0) >= self.limits["realert_minutes"] * 60:
            if self.notify(
                f"Watchdog error on {worker}",
                [f"{kind} during {stage}.{GAP}It tries again each minute.{GAP}See the log."],
            ):
                self.state["alerted"][key] = self.epoch

    def trip(self, worker: str, breach: dict, previous_version: str) -> None:
        minute = floor_minute(self.now)
        pre_rate = (
            rate_total(self._usage, breach["metric"], breach["top"])
            / self.limits["rate_window_minutes"]
        )
        if breach["window"] == "rate_5m" and breach["scope"] == "account":
            # The threshold is the whole account's, and only this Worker's series is measured below,
            # so it could sit under that for good while running.  Judge it against its own rate.
            target = 0.5 * pre_rate
        elif breach["window"] == "rate_5m":
            target = breach["threshold"] / self.limits["rate_window_minutes"]
        else:
            target = max(0.5 * pre_rate, breach["threshold"] / 1440)
        trip = {
            "stage": 2,
            "at": self.epoch,
            "trip_minute": minute_key(minute),
            "metric": breach["metric"],
            "resource": breach["top"],
            "window": breach["window"],
            "target_per_minute": target,
            "polls": 0,
            "previous_version": previous_version,
            "guard_set": False,
            "marked": False,
        }
        self.state["trips"][worker] = trip
        day = minute.strftime("%Y-%m-%d")
        self.state["trips_by_day"][day] = self.state["trips_by_day"].get(day, 0) + 1
        self.store.save(self.state)
        if not self._set_guard_off(worker, trip):
            final = self._guard_failure_is_final(trip)
            next_step = (
                "Moving straight to the fallback stop."
                if final
                else "That may pass, so the fallback stop waits for the next tries."
            )
            self.notify(
                f"Could not set {GUARD_SECRET} on {worker}",
                [
                    f"{next_step}{GAP}It keeps trying the secret each minute.{GAP}Previous version:  {previous_version}."
                ],
            )
            self._mark_tripped(worker, trip)
            if final:
                self.harden(worker)
            self.store.save(self.state)
            return
        self._mark_tripped(worker, trip)
        self.notify(
            f"Tripped {worker}",
            [
                f"{GUARD_SECRET}=off is set.{GAP}Previous version:  {previous_version}.{GAP}A Worker that has not bundled the budget guard ignores it, and the fallback stop follows if the rate does not fall."
            ],
        )
        self.store.save(self.state)

    def _set_guard_off(self, worker: str, trip: dict) -> bool:
        done = self._step(
            worker,
            "guard_mode_off",
            trip.get("previous_version", "unknown"),
            lambda: self.conn.api(
                "PUT",
                f"{self.base}/workers/scripts/{worker}/secrets",
                {"name": GUARD_SECRET, "type": "secret_text", "text": "off"},
            ),
        )
        trip["guard_set"] = done is not False
        if trip["guard_set"]:
            trip["marked"] = False  # The secret's own new deployment replaced any earlier label.
            trip["guard_failures"] = 0
        else:
            trip["guard_failures"] = trip.get("guard_failures", 0) + 1
            trip["guard_failure_transient"] = is_transient(self.last_failure_status)
        return trip["guard_set"]

    def _guard_failure_is_final(self, trip: dict) -> bool:
        """A refusal will not change, so the fallback stop follows at once.  A network fault or a
        Cloudflare 5xx, 408 or 429 gets the same number of polls as any escalation, because the fallback
        takes hostnames down and one lost call should not do that."""
        return (
            not trip.get("guard_failure_transient")
            or trip.get("guard_failures", 0) > self.limits["escalate_after_polls"]
        )

    def _mark_tripped(self, worker: str, trip: dict) -> None:
        """Label the newest deployment so the deployers refuse to undo the trip.  They read only the
        newest one, so this repeats the live split as it stands and runs again until it lands."""
        previous_version = trip.get("previous_version", "unknown")
        message = f"{TRIP_MARKER} cf-usage-watchdog {trip['metric']} {trip['window']} previous {previous_version}"
        path = f"{self.base}/workers/scripts/{worker}"
        done = self._step(
            worker,
            "mark_tripped",
            previous_version,
            lambda: label_live_deployment(self.conn, path, message),
        )
        trip["marked"] = done is not False

    # ---- stage 3:  the fallback stop

    def _detach_domain(self, domain_id: str) -> None:
        """A 404 means a run that died after this call already removed it."""
        try:
            self.conn.api("DELETE", f"{self.base}/workers/domains/{domain_id}")
        except ApiError as exc:
            if exc.status != 404:
                raise

    def _run_pending(self, worker: str, trip: dict) -> None:
        """The stop steps not yet done.  `pending` is the record of what is left and is saved after
        every step, so a crash part way repeats none that landed."""
        path = f"{self.base}/workers/scripts/{worker}"
        hostnames = {d.get("id"): d.get("hostname") for d in trip["previous"]["domains"]}
        for step in list(trip["pending"]):
            if step == "empty_schedules":
                name, call = step, lambda: self.conn.api("PUT", f"{path}/schedules", [])
            elif step == "disable_subdomain":
                name, call = (
                    step,
                    lambda: self.conn.api(
                        "POST", f"{path}/subdomain", {"enabled": False, "previews_enabled": False}
                    ),
                )
            else:
                domain_id = step.split(":", 1)[1]
                name = f"detach_domain {hostnames.get(domain_id)}"
                call = lambda d=domain_id: self._detach_domain(d)
            if self._step(worker, name, trip.get("previous_version", "unknown"), call) is not False:
                trip["pending"].remove(step)
                self.store.save(self.state)

    def retry_unfinished(self, worker: str, trip: dict) -> None:
        """A failed action must not stay failed:  a lost API call otherwise leaves a runaway running
        behind a recorded trip.  Runs each poll until everything has landed."""
        if not trip.get("guard_set", True):
            self._set_guard_off(worker, trip)
        if not trip.get("marked", True):
            self._mark_tripped(worker, trip)
        if trip.get("stage") == 2 and not trip.get("guard_set", True):
            if self._guard_failure_is_final(trip):
                self.harden(worker)
        elif trip.get("stage") == 3 and trip.get("pending"):
            self._run_pending(worker, trip)
            if (
                trip["pending"]
                and self.epoch - trip.get("last_stuck_alert", 0)
                >= self.limits["realert_minutes"] * 60
            ):
                if self.notify(
                    f"Fallback stop is incomplete on {worker}",
                    [
                        f"Still failing:  {', '.join(trip['pending'])}.{GAP}It tries again each minute.{GAP}Stop it by hand if this does not clear."
                    ],
                ):
                    trip["last_stuck_alert"] = self.epoch

    def _stuck_alert(self, worker: str, trip: dict, title: str, line: str) -> None:
        """One message an hour, not one a minute."""
        if self.epoch - trip.get("last_stuck_alert", 0) >= self.limits["realert_minutes"] * 60:
            if self.notify(title, [line]):
                trip["last_stuck_alert"] = self.epoch

    def harden(self, worker: str) -> None:
        trip = self.state["trips"][worker]
        previous_version = trip.get("previous_version", "unknown")
        # The stop is held until the label is on:  without it a deployer would see an ordinary live
        # version and a redeploy would quietly restore the cron triggers this removes.
        if not trip.get("marked", True):
            self.store.event(
                "fallback_held",
                worker=worker,
                reason="no-trip-label",
                previous_version=previous_version,
            )
            self._stuck_alert(
                worker,
                trip,
                f"Fallback stop is held on {worker}",
                f"The {TRIP_MARKER} label would not land, so the stop waits for it.{GAP}It tries again each minute.{GAP}Stop it by hand if this does not clear.",
            )
            self.store.save(self.state)
            return
        previous: dict = {}
        path = f"{self.base}/workers/scripts/{worker}"
        try:
            previous["schedules"] = (self.conn.api("GET", f"{path}/schedules") or {}).get(
                "schedules", []
            )
            previous["subdomain"] = self.conn.api("GET", f"{path}/subdomain")
            domains = self.conn.api("GET", f"{self.base}/workers/domains") or []
            previous["domains"] = [d for d in domains if d.get("service") == worker]
            pending = [
                "empty_schedules",
                "disable_subdomain",
                *(f"detach:{d['id']}" for d in previous["domains"]),
            ]
        except Exception as exc:  # noqa: BLE001 - Nothing has changed yet, so any failure can wait a minute.
            status = exc.status if isinstance(exc, ApiError) else 0
            self.store.event(
                "action_failed",
                worker=worker,
                action="read_previous",
                previous_version=previous_version,
                status=status,
                codes=exc.codes if isinstance(exc, ApiError) else [type(exc).__name__],
            )
            # The trip stays at stage 2, so the next poll tries again.
            self._stuck_alert(
                worker,
                trip,
                f"Fallback stop failed on {worker}",
                f"Could not read what it would change (HTTP {status}).{GAP}It tries again each minute.{GAP}Stop it by hand if this does not clear.",
            )
            self.store.save(self.state)
            return
        trip["stage"] = 3
        trip["previous"] = previous
        trip["pending"] = pending
        self.store.save(self.state)  # What re-arm needs, before anything is changed.
        self._run_pending(worker, trip)
        failed = len(trip["pending"])
        hosts = ", ".join(d.get("hostname", "?") for d in previous["domains"]) or "none"
        self.notify(
            f"Fallback stop on {worker}",
            [
                f"Emptied its cron triggers, turned off workers.dev and preview URLs, and detached custom domains ({hosts}).{GAP}"
                f"{failed} step(s) failed, and those try again each minute.{GAP}"
                f"Durable Object alarms, email routes, service bindings and zone routes can still run a Worker without the budget guard, so check that one by hand.{GAP}"
                f"Previous version:  {previous_version}.{GAP}Re-arm:  watchdog.py rearm {worker} --apply."
            ],
        )
        self.store.save(self.state)

    def check_trips(self, usage: dict) -> None:
        """A tripped Worker whose rate has not fallen moves to the fallback stop.  Only minutes after the
        trip count:  the analytics lag means earlier polls still read minutes from before it."""
        floor = floor_minute(self.now)
        last = floor - dt.timedelta(minutes=self.limits["lag_minutes"] + 1)
        first = floor - dt.timedelta(
            minutes=self.limits["lag_minutes"] + self.limits["rate_window_minutes"]
        )
        for worker, trip in list(self.state["trips"].items()):
            try:
                self.retry_unfinished(worker, trip)
                # A capped read can miss a Worker's minutes, so it says nothing about its rate.
                if not self.capped:
                    self._escalate_if_over(worker, trip, usage, first, last)
            except Exception as exc:  # noqa: BLE001 - One Worker's surprise must not stop the rest.
                self._worker_failed(worker, "trip check", exc)

    def _escalate_if_over(
        self, worker: str, trip: dict, usage: dict, first: dt.datetime, last: dt.datetime
    ) -> None:
        after = dt.datetime.strptime(trip["trip_minute"], "%Y-%m-%dT%H:%M:00Z").replace(
            tzinfo=dt.UTC
        ) + dt.timedelta(minutes=1)
        start = max(after, first)
        if start > last:
            return
        # Only a poll that holds minutes from after the trip counts toward escalating.
        trip["polls"] = trip.get("polls", 0) + 1
        count = int((last - start).total_seconds() // 60) + 1
        series = usage["minutes"].get(trip["metric"], {}).get(trip["resource"], {})
        total = sum(
            value
            for minute, value in series.items()
            if start
            <= dt.datetime.strptime(minute, "%Y-%m-%dT%H:%M:00Z").replace(tzinfo=dt.UTC)
            <= last
        )
        average = total / count
        over = average > trip["target_per_minute"]
        self.store.event(
            "trip_check",
            worker=worker,
            stage=trip.get("stage"),
            polls=trip["polls"],
            per_minute=round(average, 2),
            target=round(trip["target_per_minute"], 2),
        )
        if not over:
            return
        if trip.get("stage") == 2 and trip["polls"] >= self.limits["escalate_after_polls"]:
            self.harden(worker)
        elif (
            trip.get("stage") == 3
            and self.epoch - trip.get("last_stuck_alert", 0) >= self.limits["realert_minutes"] * 60
        ):
            if self.notify(
                f"{worker} is still over after the fallback stop",
                [
                    f"{average:,.0f} per minute of {trip['metric']}, target {trip['target_per_minute']:,.0f}.{GAP}Stop it by hand."
                ],
            ):
                trip["last_stuck_alert"] = self.epoch

    # ---- the run

    def inventory_usable(self, inventory: dict) -> bool:
        """The stored ownership may authorize a trip only while its last read worked and it is
        under a day old.  It still names owners in alerts either way."""
        return (
            not self.state.get("inventory_failed")
            and self.epoch - inventory.get("at", 0) < INVENTORY_MAX_AGE_SECONDS
        )

    def _withhold_reason(self, breach: dict, inventory: dict) -> str | None:
        if self.capped:
            return "capped-read"
        if not names_its_script(breach["metric"]) and not self.inventory_usable(inventory):
            return "stale-inventory"
        return None

    def _report_withheld(self, withheld: dict, versions: dict) -> None:
        """The usage alert has already gone.  This one says the stop did not happen."""
        for worker, (breach, reason) in withheld.items():
            self.store.event(
                "trip_withheld",
                worker=worker,
                key=breach["key"],
                reason=reason,
                previous_version=versions[worker],
            )
            key = f"withheld|{reason}|{worker}"
            if (
                self.mode == "enforce"
                and self.epoch - self.state["alerted"].get(key, 0)
                >= self.limits["realert_minutes"] * 60
                and self.notify(
                    f"Not stopping {worker}",
                    [
                        f"It is over on {breach['metric']}, but the data that would justify a stop is not good enough ({reason}).{GAP}It tries again each minute.{GAP}Stop it by hand if it is running away."
                    ],
                )
            ):
                self.state["alerted"][key] = self.epoch

    def _alert_state_lost(self) -> None:
        if not self.state.get("state_lost_at"):
            return
        if self.epoch - self.state["alerted"].get("state_lost", 0) < (
            self.limits["realert_minutes"] * 60
        ):
            return
        if self.notify(
            "Cloudflare usage watchdog lost its state",
            [
                f"Its state file could not be trusted, so it no longer knows what it tripped.{GAP}It only alerts until an owner checks the Workers for {GUARD_SECRET}, emptied crons and detached domains, using the copy kept as {self.state.get('state_lost_kept')}.{GAP}Then run watchdog.py state-ack."
            ],
        ):
            self.state["alerted"]["state_lost"] = self.epoch
            self.store.save(self.state)

    def _alert_capped(self) -> None:
        self.store.event("read_capped", datasets=self.capped)
        if self.epoch - self.state["alerted"].get("read_capped", 0) < (
            self.limits["realert_minutes"] * 60
        ):
            return
        if self.notify(
            "Cloudflare usage read is incomplete",
            [
                f"These came back at the row limit and may be missing rows:  {', '.join(self.capped)}.{GAP}Nothing trips or escalates on a read like this, and the totals may be low."
            ],
        ):
            self.state["alerted"]["read_capped"] = self.epoch

    def _rearm_holds(self, worker: str, breach: dict) -> bool:
        """After a re-arm the day-to-date total stays over until 6 PM, and the month's until the 1st.
        Those two must not trip the same Worker straight back;  a 5-minute rate still does."""
        held = self.state.get("rearmed", {}).get(worker) or {}
        floor = floor_minute(self.now)
        return (breach["window"] == "day" and held.get("day") == floor.strftime("%Y-%m-%d")) or (
            breach["window"] == "month" and held.get("month") == floor.strftime("%Y-%m")
        )

    def apply_breaches(self, breaches: list, inventory: dict) -> None:
        due = [
            b
            for b in breaches
            if self.epoch - self.state["alerted"].get(b["key"], 0)
            >= self.limits["realert_minutes"] * 60
        ]
        wanted: dict = {}
        withheld: dict = {}
        # Observe mode only reports, so it reads each Worker's version when it has something new to say.
        for breach in breaches if self.mode == "enforce" else due:
            for worker in trip_targets(self.cfg, self.profile, breach, inventory):
                if (
                    worker not in self.state["trips"]
                    and worker not in wanted
                    and not self._rearm_holds(worker, breach)
                ):
                    reason = self._withhold_reason(breach, inventory)
                    if reason:
                        withheld.setdefault(worker, (breach, reason))
                    else:
                        wanted[worker] = breach
        withheld = {worker: item for worker, item in withheld.items() if worker not in wanted}
        versions: dict = {}
        for worker in (
            {w for b in due for w in trip_targets(self.cfg, self.profile, b, inventory)}
            | set(wanted)
            | set(withheld)
        ):
            versions[worker] = self.current_version(worker)
        if due:
            lines = []
            for breach in due:
                targets = trip_targets(self.cfg, self.profile, breach, inventory)
                previous = {worker: versions[worker] for worker in targets}
                lines.append(self.describe(breach, previous))
                self.store.event(
                    "breach",
                    key=breach["key"],
                    value=breach["value"],
                    threshold=breach["threshold"],
                    usd=breach["usd"],
                    mode=self.mode,
                    targets=targets,
                    previous_versions=previous,
                )
            if self.notify("Cloudflare usage over a limit", lines):
                for breach in due:
                    self.state["alerted"][breach["key"]] = self.epoch
                self.store.save(self.state)
        self._report_withheld(withheld, versions)
        if self.mode != "enforce":
            for worker, breach in wanted.items():
                self.store.event(
                    "would_trip",
                    worker=worker,
                    key=breach["key"],
                    previous_version=versions[worker],
                )
            return
        day = floor_minute(self.now).strftime("%Y-%m-%d")
        tripped_this_run = 0
        for worker, breach in wanted.items():
            over_day = (
                self.state["trips_by_day"].get(day, 0) >= self.limits["max_trips_per_utc_day"]
            )
            if tripped_this_run >= self.limits["max_trips_per_run"] or over_day:
                self.store.event(
                    "trip_suppressed",
                    worker=worker,
                    key=breach["key"],
                    reason="daily cap" if over_day else "run cap",
                    previous_version=versions[worker],
                )
                if over_day and f"capped|{day}" not in self.state["alerted"]:
                    if self.notify(
                        "Trip cap reached",
                        [
                            f"{worker} would trip on {breach['metric']}, but {self.limits['max_trips_per_utc_day']} Workers have already tripped today.{GAP}Look at it by hand."
                        ],
                    ):
                        self.state["alerted"][f"capped|{day}"] = self.epoch
                continue
            tripped_this_run += 1
            try:
                self.trip(worker, breach, versions[worker])
            except Exception as exc:  # noqa: BLE001 - One Worker's surprise must not stop the rest.
                self._worker_failed(worker, "trip", exc)

    def handle_poll_failure(self, reason: str) -> None:
        since = self.state.setdefault("poll_failed_since", self.epoch)
        self.store.event("poll_failed", reason=reason, minutes=round((self.epoch - since) / 60, 1))
        if self.epoch - since < self.limits["poll_fail_alert_minutes"] * 60:
            return
        if (
            self.epoch - self.state.get("poll_fail_alerted", 0)
            < self.limits["realert_minutes"] * 60
        ):
            return
        minutes = round((self.epoch - since) / 60)
        if self.notify(
            "Cloudflare usage watchdog is blind",
            [
                f"No usage read for {minutes} minutes ({reason}).{GAP}Nothing trips while it is blind."
            ],
        ):
            self.state["poll_fail_alerted"] = self.epoch

    def run(self) -> int:
        try:
            return self._run()
        except Exception as exc:  # noqa: BLE001 - A surprise must reach an owner, not only the journal.
            trace = exc.__traceback__
            while trace is not None and trace.tb_next is not None:
                trace = trace.tb_next
            # The type and line only:  an exception's text could carry a response body.
            where = trace.tb_lineno if trace is not None else 0
            self.handle_poll_failure(f"internal-{type(exc).__name__}-line-{where}")
            self.store.save(self.state)
            return 1

    def _unmapped_breach(self, breaches: list, inventory: dict) -> bool:
        """A breach that could trip but has no owner, which a Worker or binding newer than the
        inventory would look like."""
        return any(
            not trip_targets(self.cfg, self.profile, breach, inventory)
            and breach["top"] not in UNOWNED
            and not self.cfg["metrics"][breach["metric"]].get("alert_only")
            and (
                breach["scope"] != "account"
                or self.cfg["profiles"][self.profile].get("trip_top_contributor")
            )
            for breach in breaches
        )

    def _needs_a_fresh_inventory(self, breaches: list, inventory: dict) -> bool:
        """A breach that could trip but has no owner, or a stop waiting on ownership it cannot trust,
        which is worth a read each minute rather than each 15."""
        tried = max(inventory.get("at", 0), self.state.get("inventory_tried", 0))
        waited = self.epoch - tried
        if self._unmapped_breach(breaches, inventory) and waited >= INVENTORY_RETRY_SECONDS:
            return True
        return (
            self.mode == "enforce"
            and waited >= INVENTORY_WANTED_RETRY_SECONDS
            and not self.inventory_usable(inventory)
            and any(
                not names_its_script(breach["metric"])
                and trip_targets(self.cfg, self.profile, breach, inventory)
                for breach in breaches
            )
        )

    def _run(self) -> int:
        self._alert_state_lost()
        try:
            if "account_id" not in self.cfg:
                self.cfg["account_id"] = self.conn.account_id()
            account = self.conn.graphql(build_query(), query_variables(self.cfg, self.now))
            validate_account(account)
            # A cron fires inside every 5-minute window, so an empty one is an analytics fault that
            # would otherwise read as all clear.
            if not account.get("wm"):
                raise PollError("analytics-empty")
        except PollError as exc:
            self.handle_poll_failure(str(exc))
            self.store.save(self.state)
            return 1
        self.capped = capped_datasets(account)
        inventory = self.refresh_inventory_if_stale()
        usage = parse_usage(account, inventory.get("ns_to_script", {}))
        self._usage = usage
        self.beat = self.conn.heartbeat()
        self.store.event("poll_ok", heartbeat=self.beat, trips=sorted(self.state["trips"]))
        if self.capped:
            self._alert_capped()
        if self.mode == "enforce":
            self.check_trips(usage)
        breaches = evaluate(self.cfg, self.profile, usage)
        if self._needs_a_fresh_inventory(breaches, inventory):
            inventory = self.refresh_inventory()
            self._usage = usage = parse_usage(account, inventory.get("ns_to_script", {}))
            breaches = evaluate(self.cfg, self.profile, usage)
        self.apply_breaches(breaches, inventory)
        self.daily_check_if_due(inventory)
        # Last, so a crash above still counts toward the blind alert.
        if (
            self.state.pop("poll_failed_since", None) is not None
            and self.state.pop("poll_fail_alerted", None) is not None
        ):
            self.notify(
                "Cloudflare usage watchdog can read again", ["Usage reads are working."], page=False
            )
        self.state.pop("poll_fail_alerted", None)
        self.prune()
        self.store.save(self.state)
        failed = self.undelivered or self.worker_failed or self.state.get("inventory_failed")
        return 1 if failed else 0

    def prune(self) -> None:
        cutoff = self.epoch - 2 * 86400
        self.state["alerted"] = {
            key: at for key, at in self.state["alerted"].items() if at >= cutoff
        }
        today = floor_minute(self.now).strftime("%Y-%m-%d")
        self.state["trips_by_day"] = {
            day: count for day, count in self.state["trips_by_day"].items() if day == today
        }
        month = today[:7]
        self.state["rearmed"] = {
            worker: held
            for worker, held in self.state.get("rearmed", {}).items()
            if held.get("month") == month
        }

    # ---- the daily read:  who owns what, and does the account still match git

    def refresh_inventory_if_stale(self) -> dict:
        inventory = self.state.get("inventory") or {}
        today = floor_minute(self.now).strftime("%Y-%m-%d")
        # The daily check compares the live account, so it gets a fresh read at the turn of the day.
        if (
            self.epoch - inventory.get("at", 0) < 86400
            and self.state.get("daily_check_day") == today
        ):
            return inventory
        # A read that keeps failing, or a daily check that cannot post, must not repeat it each minute.
        if (
            inventory
            and self.epoch - self.state.get("inventory_tried", 0) < INVENTORY_RETRY_SECONDS
        ):
            return inventory
        return self.refresh_inventory()

    def refresh_inventory(self) -> dict:
        inventory = self.state.get("inventory") or {}
        self.state["inventory_tried"] = self.epoch
        try:
            inventory = self.read_inventory()
        except (ApiError, PollError, KeyError, TypeError, AttributeError) as exc:
            # The kept copy still names owners in alerts, but `inventory_failed` stops it
            # authorizing a trip until a read works, and the run exits nonzero.
            self.state["inventory_failed"] = self.epoch
            self.store.event(
                "inventory_failed", source="cloudflare-workers-api", error=type(exc).__name__
            )
            return self.state.get("inventory") or {}
        self.state.pop("inventory_failed", None)
        self.state["inventory"] = inventory
        return inventory

    def read_inventory(self) -> dict:
        scripts = self.conn.api("GET", f"{self.base}/workers/scripts") or []
        owners: dict = {"d1": {}, "kv": {}, "r2": {}}
        details = {}
        for script in scripts:
            name = script["id"]
            settings = self.conn.api("GET", f"{self.base}/workers/scripts/{name}/settings") or {}
            subdomain = self.conn.api("GET", f"{self.base}/workers/scripts/{name}/subdomain") or {}
            details[name] = {
                "limits": settings.get("limits") or script.get("limits") or {},
                "workers_dev": subdomain.get("enabled"),
                "previews_enabled": subdomain.get("previews_enabled"),
            }
            for binding in settings.get("bindings") or []:
                kind = binding.get("type")
                if kind == "d1":
                    resource, family = binding.get("id") or binding.get("database_id"), "d1"
                elif kind == "kv_namespace":
                    resource, family = binding.get("namespace_id"), "kv"
                elif kind == "r2_bucket":
                    resource, family = binding.get("bucket_name"), "r2"
                else:
                    continue
                if resource:
                    owners[family].setdefault(resource, [])
                    if name not in owners[family][resource]:
                        owners[family][resource].append(name)
        namespaces = self.conn.api("GET", f"{self.base}/workers/durable_objects/namespaces") or []
        return {
            "at": self.epoch,
            "workers": sorted(details),
            "details": details,
            "owners": owners,
            "ns_to_script": {ns["id"]: ns["script"] for ns in namespaces if ns.get("script")},
        }

    def daily_check_if_due(self, inventory: dict, registry_path: Path | None = None) -> None:
        today = floor_minute(self.now).strftime("%Y-%m-%d")
        if (
            self.state.get("daily_check_day") == today
            or not inventory.get("workers")
            or self.state.get("inventory_failed")  # It compares the live account, so it waits.
        ):
            return
        drift = compare_registry(inventory, load_registry(registry_path or REGISTRY_PATH))
        self.store.event("daily_check", drift=len(drift) if drift is not None else "no-registry")
        lines = list(drift or [])
        if self.beat is None:
            lines.append("No dead-man heartbeat is configured.")
        if self.conn.page_configured() is False:
            lines.append(
                "No direct page path is configured, so an alert sent while simple-tools-mcp is tripped or capped reaches nobody."
            )
        if lines and lines != self.state.get("last_drift"):
            if not self.notify(
                "Cloudflare account does not match what the watchdog expects", lines, page=False
            ):
                return
        self.state["last_drift"] = lines
        self.state["daily_check_day"] = today


def load_registry(path: Path) -> dict | None:
    """scripts/worker-registry.json comes from a sibling PR:  absent is fine, and any of its likely
    shapes reads as {worker: entry}."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    workers = raw.get("workers", raw) if isinstance(raw, dict) else raw
    if isinstance(workers, list):
        return {item["name"]: item for item in workers if isinstance(item, dict) and "name" in item}
    if isinstance(workers, dict):
        return {name: entry for name, entry in workers.items() if isinstance(entry, dict)}
    return None


def compare_registry(inventory: dict, registry: dict | None) -> list | None:
    if registry is None:
        return None
    drift = []
    details = inventory.get("details", {})
    for name in sorted(set(details) - set(registry)):
        drift.append(f"Worker {name} is live but not in the registry.")
    for name in sorted(set(registry) - set(details)):
        drift.append(f"Worker {name} is in the registry but not live.")
    for name in sorted(set(details) & set(registry)):
        entry, live = registry[name], details[name]
        for key, wanted in (entry.get("limits") or {}).items():
            if (live.get("limits") or {}).get(key) != wanted:
                drift.append(
                    f"{name} limits.{key} is {(live.get('limits') or {}).get(key)}, the registry says {wanted}."
                )
        # Preview URLs off is the fleet's rule for every Worker, so a registry row need not repeat it.
        if not entry.get("preview_urls", False) and live.get("previews_enabled"):
            drift.append(f"{name} has preview URLs on, the registry says off.")
        # workers.dev has no such rule:  some Workers are served from it, so only a row that says off
        # can disagree.
        if "workers_dev" in entry and not entry["workers_dev"] and live.get("workers_dev"):
            drift.append(f"{name} has workers.dev on, the registry says off.")
    return drift


# ---------------------------------------------------------------- re-arm


def rearm(
    cfg: dict,
    conn: Any,
    store: Store,
    state: dict,
    worker: str,
    apply: bool,
    now: dt.datetime | None = None,
) -> int:
    """Undo a trip from what it recorded.  Manual on purpose.  Without --apply it only prints the plan."""
    trip = state["trips"].get(worker)
    if not trip:
        print(f"{worker} is not tripped")
        if state.get("state_lost_at"):
            print(
                f"The state file was lost, so this cannot be sure.  Check by hand against {state.get('state_lost_kept')}."
            )
        return 2
    try:
        account_id = cfg.get("account_id") or conn.account_id()
    except PollError as exc:
        print(f"Cannot re-arm:  {exc}")
        return 2
    base = f"/accounts/{account_id}"
    path = f"{base}/workers/scripts/{worker}"
    previous = trip.get("previous") or {}
    plan: list = [("DELETE", f"{path}/secrets/{GUARD_SECRET}", None)]
    if "schedules" in previous:
        plan.append(
            ("PUT", f"{path}/schedules", [{"cron": item["cron"]} for item in previous["schedules"]])
        )
    if "subdomain" in previous and previous["subdomain"] is not None:
        plan.append(
            (
                "POST",
                f"{path}/subdomain",
                {
                    "enabled": bool(previous["subdomain"].get("enabled")),
                    "previews_enabled": bool(previous["subdomain"].get("previews_enabled")),
                },
            )
        )
    for domain in previous.get("domains", []):
        plan.append(
            (
                "PUT",
                f"{base}/workers/domains",
                {
                    key: domain[key]
                    for key in ("hostname", "service", "zone_id", "environment")
                    if key in domain
                },
            )
        )
    for method, url, body in plan:
        print(method, url, json.dumps(body) if body is not None else "")
    if not apply:
        print("Dry run.  Add --apply to do this.")
        return 0
    failed = 0

    def failure(exc: Exception) -> dict:
        if isinstance(exc, ApiError):
            return {"status": exc.status, "codes": exc.codes}
        return {"status": 0, "codes": [type(exc).__name__]}

    def attempt(method: str, url: str, call: Callable) -> None:
        """One step and exactly one row, whatever happens."""
        nonlocal failed
        name = "rearm_failed"
        outcome: dict = {"status": 0, "codes": []}
        try:
            call()
            name, outcome = "rearm_step", {}
        except Exception as exc:  # noqa: BLE001 - A failed step keeps the trip recorded.
            failed += 1
            outcome = failure(exc)
        finally:
            store.event(name, worker=worker, method=method, path=url.replace(base, ""), **outcome)

    # The secret may never have landed, or an earlier partial re-arm removed it, and deleting what
    # is not there would fail forever.  Deleting it makes a new deployment, which clears the
    # deployers' label;  with nothing to delete that has to be done on purpose.
    names: list | None = None
    try:
        names = [item.get("name") for item in conn.api("GET", f"{path}/secrets") or []]
    except Exception as exc:  # noqa: BLE001 - Same:  the trip stays until this can be read.
        failed += 1
        store.event(
            "rearm_failed",
            worker=worker,
            method="GET",
            path=f"{path}/secrets".replace(base, ""),
            **failure(exc),
        )
    if names is not None:
        if GUARD_SECRET in names:
            attempt("DELETE", plan[0][1], lambda: conn.api("DELETE", plan[0][1]))
        else:
            attempt(
                "POST",
                f"{path}/deployments",
                lambda: label_live_deployment(conn, path, "cf-usage-watchdog re-armed"),
            )
    for method, url, body in plan[1:]:
        attempt(method, url, lambda m=method, u=url, b=body: conn.api(m, u, b))
    if failed:
        print(f"{failed} step(s) failed;  the trip stays recorded.  Fix them and run this again.")
        return 1
    moment = now or dt.datetime.fromtimestamp(time.time(), dt.UTC)
    state.setdefault("rearmed", {})[worker] = {
        "day": moment.strftime("%Y-%m-%d"),
        "month": moment.strftime("%Y-%m"),
    }
    del state["trips"][worker]
    store.save(state)
    print(
        f"{worker} re-armed.  It stays on the version the trip left live;  previous version {trip.get('previous_version')}."
        "  The day-to-date and month-to-date rules will not trip it again until they reset;  the 5-minute rate still can."
    )
    return 0


# ---------------------------------------------------------------- config and entry point


def load_config(path: Path = CONFIG_PATH) -> dict:
    cfg = json.loads(path.read_text(encoding="utf-8"))
    problems = validate_config(cfg)
    if problems:
        raise ValueError("; ".join(problems))
    return cfg


def validate_config(cfg: dict) -> list:
    problems = []
    for name, profile in cfg.get("profiles", {}).items():
        for section in ("per_resource", "account"):
            for metric, rule in profile.get(section, {}).items():
                if metric not in cfg["metrics"]:
                    problems.append(f"{name}.{section} names unknown metric {metric}")
                if not set(rule) & {"day", "month", "level", "rate_5m"}:
                    problems.append(f"{name}.{section}.{metric} has no threshold")
    return problems


def read_knobs(stop_file: Path, store: Store | None = None) -> tuple:
    """Mode and profile through fleet_knobs:  Infisical, then its last-known-good copy (marked stale),
    then the defaults.  A stale enforce stays enforce, because failing open on a cost guard is the
    worse failure, and the log says when that is what is happening.  A broken read of the knobs
    themselves reads as observe."""
    try:
        sys.path.insert(0, str(HERE.parent))
        from fleet_knobs import knobs

        answer = (
            knobs.get("CF_USAGE_WATCHDOG_MODE", stop=stop_file.exists()),
            knobs.get("CF_USAGE_WATCHDOG_PROFILE"),
        )
    except Exception as exc:  # noqa: BLE001 - A broken knob read must never turn into enforcement or a crash.
        print(json.dumps({"event": "knobs_failed", "error": type(exc).__name__}), flush=True)
        return "observe", "paid"
    if store is not None:
        try:
            store.event("knobs", report=knobs.report())  # Names, sources, stale;  never a value.
        except Exception as exc:  # noqa: BLE001 - A failed log line must not change the mode.
            print(
                json.dumps({"event": "knobs_report_failed", "error": type(exc).__name__}),
                flush=True,
            )
    return answer


def print_thresholds(cfg: dict) -> None:
    for name, profile in cfg["profiles"].items():
        print(f"{name}:  {profile['description']}")
        for section in ("per_resource", "account"):
            for metric, rule in profile.get(section, {}).items():
                spec = cfg["metrics"][metric]
                bits = [
                    f"{key} {rule[key]:,}"
                    for key in ("day", "month", "level", "rate_5m")
                    if key in rule
                ]
                dollars = " ".join(
                    f"{key} ${rule[key]:,.2f}"
                    for key in ("usd_day", "usd_rate_hour", "usd_month")
                    if key in rule
                )
                print(f"  {section:<12} {spec['label']:<28} {'; '.join(bits):<36} {dollars}")


def default_state_dir(env: dict) -> Path:
    return Path(
        env.get("CF_USAGE_WATCHDOG_STATE_DIR")
        or Path(env.get("HOME", "~")).expanduser() / ".local/state/cf-usage-watchdog"
    )


def load_or_quarantine(store: Store, now: dt.datetime) -> dict:
    """Call under the lock."""
    try:
        return store.load()
    except StateCorrupt as exc:
        return store.quarantine(str(exc), now.timestamp())


def state_ack(store: Store, state: dict) -> int:
    """An owner has checked the Workers by hand, so enforcement may resume on the fresh state."""
    if not state.get("state_lost_at"):
        print("Nothing to acknowledge.")
        return 0
    state.pop("state_lost_at")
    state.pop("state_lost_kept", None)
    state["alerted"].pop("state_lost", None)
    store.event("state_acknowledged")
    store.save(state)
    print("Acknowledged.  The watchdog follows its mode knob again.")
    return 0


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "command",
        nargs="?",
        default="run",
        choices=("run", "thresholds", "status", "rearm", "state-ack"),
    )
    parser.add_argument("worker", nargs="?")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    cfg = load_config()
    if args.command == "thresholds":
        print_thresholds(cfg)
        return 0
    try:
        store = Store(default_state_dir(dict(os.environ)))
    except ValueError as exc:
        print(json.dumps({"event": "state_dir_refused", "error": str(exc)}), flush=True)
        return 1
    if args.command == "status":
        try:
            state = store.load()
        except StateCorrupt as exc:
            print(f"The state file cannot be trusted ({exc}).  The next run keeps a copy of it.")
            return 1
        print(
            json.dumps(
                {
                    "trips": state["trips"],
                    "poll_failed_since": state.get("poll_failed_since"),
                    "state_lost_kept": state.get("state_lost_kept"),
                },
                indent=1,
                sort_keys=True,
            )
        )
        return 0
    conn = Connections(cfg)
    if args.command == "rearm" and not args.worker:
        parser.error("rearm needs a Worker name")
    now = dt.datetime.fromtimestamp(time.time(), dt.UTC)
    with store.lock():
        state = load_or_quarantine(store, now)
        if args.command == "state-ack":
            return state_ack(store, state)
        if args.command == "rearm":
            return rearm(cfg, conn, store, state, args.worker, args.apply)
        mode, profile = read_knobs(store.directory / "STOP", store)
        if profile not in cfg["profiles"]:
            store.event("profile_unknown")
            profile = "paid"
        return Watchdog(cfg, conn, store, state, now, mode, profile).run()


if __name__ == "__main__":
    sys.exit(main())
