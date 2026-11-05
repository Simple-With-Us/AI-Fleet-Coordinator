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
import datetime as dt
import json
import os
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


class PollError(Exception):
    """The usage read failed.  Never leads to a trip."""


class ApiError(Exception):
    """A Cloudflare API call failed.  Carries a status and error codes, never a header or a body."""

    def __init__(self, status: int, codes: Iterable[Any] = ()) -> None:
        self.status = status
        self.codes = list(codes)
        super().__init__(f"HTTP {status} codes {self.codes}")


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
            f"{alias}:{dataset}(limit:2000,filter:{{datetime_geq:{start},datetime_lt:{end}}})"
            f"{{{fields} dimensions{{{dims}}}}}"
        )
    parts.append(
        "r2s:r2StorageAdaptiveGroups(limit:500,filter:{datetime_geq:$storageStart,datetime_lt:$now},"
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


def parse_usage(account: dict, ns_to_script: dict) -> dict:
    usage = empty_usage()
    window_of = {alias: window for alias, _, window, _, _ in DATASETS}
    for alias, window in window_of.items():
        for row in account.get(alias) or []:
            total, dims = row.get("sum") or {}, row.get("dimensions") or {}
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
        bucket = (row.get("dimensions") or {}).get("bucketName", "")
        if bucket in seen:
            continue
        seen.add(bucket)
        maximum = row.get("max") or {}
        size = (maximum.get("payloadSize", 0) + maximum.get("metadataSize", 0)) / 1e9
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


def owners_for(metric: str, resource: str, inventory: dict) -> list:
    """The Workers a breach can be stopped through.  None for a resource no Worker binds (an S3 client)."""
    workers = inventory.get("workers", [])
    if metric.startswith(("workers.", "do.")):
        if resource in UNOWNED:
            return []
        if workers:
            return [resource] if resource in workers else []
        # No inventory yet:  these two name a script in the data, the others an unmapped namespace id.
        return [resource] if metric.startswith("workers.") or metric == "do.requests" else []
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
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.state_path = directory / "state.json"
        self.events_path = directory / "events.jsonl"

    def load(self) -> dict:
        state: dict = {}
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            self.event("state_unreadable")
        for key, default in (
            ("alerted", {}),
            ("trips", {}),
            ("inventory", {}),
            ("trips_by_day", {}),
        ):
            state.setdefault(key, default)
        return state

    def save(self, state: dict) -> None:
        fd, tmp = tempfile.mkstemp(dir=self.directory, prefix=".state-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(state, handle, sort_keys=True)
            os.replace(tmp, self.state_path)
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
        opener: Callable = urllib.request.urlopen,
        env: dict | None = None,
    ) -> None:
        self.cfg = cfg
        self.runner = runner
        self.opener = opener
        self.env = dict(os.environ if env is None else env)
        self.timeout = cfg["limits"]["http_timeout_seconds"]
        self._token: str | None = None
        self._heartbeat_url: str | None = None

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
            return response.status, response.read()

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
                codes = [item.get("code") for item in json.loads(exc.read()).get("errors", [])]
            except (ValueError, AttributeError, OSError):
                pass
            finally:
                exc.close()
            raise ApiError(exc.code, codes) from None
        except (urllib.error.URLError, OSError) as exc:
            raise ApiError(0, [type(exc).__name__]) from None
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except ValueError:
            raise ApiError(0, ["not-json"]) from None

    def graphql(self, query: str, variables: dict) -> dict:
        try:
            answer = self._cloudflare("POST", "/graphql", {"query": query, "variables": variables})
        except ApiError as exc:
            raise PollError(f"graphql-http-{exc.status}") from None
        if answer.get("errors"):
            raise PollError(f"graphql-errors-{len(answer['errors'])}")
        accounts = ((answer.get("data") or {}).get("viewer") or {}).get("accounts") or []
        if len(accounts) != 1:
            raise PollError("graphql-no-account")
        return accounts[0]

    def api(self, method: str, path: str, body: Any = None) -> Any:
        answer = self._cloudflare(method, path, body)
        if answer.get("success") is False:
            raise ApiError(200, [item.get("code") for item in answer.get("errors", [])])
        return answer.get("result")

    def zulip(self, text: str) -> bool:
        conf = self.cfg["zulip"]
        env = {key: value for key, value in self.env.items() if not key.startswith("INFISICAL_")}
        env["AGENT_SEAT"] = conf["seat"]
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
        except (OSError, subprocess.SubprocessError):
            return False
        return done.returncode == 0

    def page(self, title: str, text: str) -> bool | None:
        """Pushover directly, the one path that does not go through a Worker on this account.
        None means it is not configured."""
        conf = self.cfg["infisical"]
        try:
            token = self._secret(conf["pushover_token_key"])
            user = self._secret(conf["pushover_user_key"])
        except PollError:
            return False
        if not token or not user:
            return None
        form = urllib.parse.urlencode(
            {"token": token, "user": user, "title": title, "message": text[:1000], "priority": 1}
        )
        try:
            status, _ = self._request("POST", PUSHOVER_URL, form.encode("utf-8"))
        except (urllib.error.URLError, OSError):
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
        except (urllib.error.URLError, OSError):
            return False
        return status < 300


# ---------------------------------------------------------------- the actions


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
        self.mode = mode
        self.profile = profile
        self.limits = cfg["limits"]
        self.base = f"/accounts/{cfg['account_id']}"
        self.undelivered = False
        self.beat: bool | None = None
        self._usage = empty_usage()

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
        except (ApiError, KeyError, IndexError, TypeError):
            return "unknown"

    # ---- stage 2:  the Worker's own off switch

    def _step(self, worker: str, action: str, previous_version: str, call: Callable) -> Any:
        """One logged action.  Returns the call's result, or False when it failed."""
        try:
            result = call()
        except ApiError as exc:
            self.store.event(
                "action_failed",
                worker=worker,
                action=action,
                previous_version=previous_version,
                status=exc.status,
                codes=exc.codes,
            )
            return False
        self.store.event("action", worker=worker, action=action, previous_version=previous_version)
        return True if result is None else result

    def trip(self, worker: str, breach: dict, previous_version: str) -> None:
        minute = floor_minute(self.now)
        pre_rate = (
            rate_total(self._usage, breach["metric"], breach["top"])
            / self.limits["rate_window_minutes"]
        )
        if breach["window"] == "rate_5m":
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
        }
        self.state["trips"][worker] = trip
        day = minute.strftime("%Y-%m-%d")
        self.state["trips_by_day"][day] = self.state["trips_by_day"].get(day, 0) + 1
        self.store.save(self.state)
        set_off = self._step(
            worker,
            "guard_mode_off",
            previous_version,
            lambda: self.conn.api(
                "PUT",
                f"{self.base}/workers/scripts/{worker}/secrets",
                {"name": GUARD_SECRET, "type": "secret_text", "text": "off"},
            ),
        )
        if set_off is False:
            self.notify(
                f"Could not set {GUARD_SECRET} on {worker}",
                [
                    f"Moving straight to the fallback stop.{GAP}Previous version:  {previous_version}."
                ],
            )
            self.harden(worker)
            return
        self._mark_tripped(worker, breach, previous_version)
        self.notify(
            f"Tripped {worker}",
            [
                f"{GUARD_SECRET}=off is set.{GAP}Previous version:  {previous_version}.{GAP}A Worker that has not bundled the budget guard ignores it, and the fallback stop follows if the rate does not fall."
            ],
        )
        self.store.save(self.state)

    def _mark_tripped(self, worker: str, breach: dict, previous_version: str) -> None:
        """Label the live version so the deployers can refuse to undo the trip.  Best effort."""
        version = self.current_version(worker)
        if "," in version or version == "unknown":
            return
        message = f"{TRIP_MARKER} cf-usage-watchdog {breach['metric']} {breach['window']} previous {previous_version}"
        self._step(
            worker,
            "mark_tripped",
            previous_version,
            lambda: self.conn.api(
                "POST",
                f"{self.base}/workers/scripts/{worker}/deployments",
                {
                    "strategy": "percentage",
                    "versions": [{"version_id": version, "percentage": 100}],
                    "annotations": {"workers/message": message[:1000]},
                },
            ),
        )

    # ---- stage 3:  the fallback stop

    def harden(self, worker: str) -> None:
        trip = self.state["trips"][worker]
        previous_version = trip.get("previous_version", "unknown")
        previous: dict = {}
        path = f"{self.base}/workers/scripts/{worker}"
        try:
            previous["schedules"] = (self.conn.api("GET", f"{path}/schedules") or {}).get(
                "schedules", []
            )
            previous["subdomain"] = self.conn.api("GET", f"{path}/subdomain")
            domains = self.conn.api("GET", f"{self.base}/workers/domains") or []
            previous["domains"] = [d for d in domains if d.get("service") == worker]
        except ApiError as exc:
            self.store.event(
                "action_failed",
                worker=worker,
                action="read_previous",
                previous_version=previous_version,
                status=exc.status,
                codes=exc.codes,
            )
            # The trip stays at stage 2, so the next poll tries again.  One message an hour, not one a minute.
            if self.epoch - trip.get("last_stuck_alert", 0) >= self.limits["realert_minutes"] * 60:
                if self.notify(
                    f"Fallback stop failed on {worker}",
                    [
                        f"Could not read what it would change (HTTP {exc.status}).{GAP}It tries again each minute.{GAP}Stop it by hand if this does not clear."
                    ],
                ):
                    trip["last_stuck_alert"] = self.epoch
            self.store.save(self.state)
            return
        trip["stage"] = 3
        trip["previous"] = previous
        self.store.save(self.state)  # What re-arm needs, before anything is changed.
        results = [
            self._step(
                worker,
                "empty_schedules",
                previous_version,
                lambda: self.conn.api("PUT", f"{path}/schedules", []),
            ),
            self._step(
                worker,
                "disable_subdomain",
                previous_version,
                lambda: self.conn.api(
                    "POST", f"{path}/subdomain", {"enabled": False, "previews_enabled": False}
                ),
            ),
        ]
        for domain in previous["domains"]:
            results.append(
                self._step(
                    worker,
                    f"detach_domain {domain.get('hostname')}",
                    previous_version,
                    lambda d=domain: self.conn.api(
                        "DELETE", f"{self.base}/workers/domains/{d['id']}"
                    ),
                )
            )
        failed = sum(1 for result in results if result is False)
        hosts = ", ".join(d.get("hostname", "?") for d in previous["domains"]) or "none"
        self.notify(
            f"Fallback stop on {worker}",
            [
                f"Emptied its cron triggers, turned off workers.dev and preview URLs, and detached custom domains ({hosts}).{GAP}"
                f"{failed} step(s) failed.{GAP}Durable Object alarms keep running on a Worker without the budget guard, so check that one by hand.{GAP}"
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
            trip["polls"] = trip.get("polls", 0) + 1
            after = dt.datetime.strptime(trip["trip_minute"], "%Y-%m-%dT%H:%M:00Z").replace(
                tzinfo=dt.UTC
            ) + dt.timedelta(minutes=1)
            start = max(after, first)
            if start > last:
                continue
            count = int((last - start).total_seconds() // 60) + 1
            series = usage["minutes"].get(trip["metric"], {}).get(trip["resource"], {})
            total = sum(
                value
                for minute, value in series.items()
                if start
                <= dt.datetime.strptime(minute, "%Y-%m-%dT%H:%M:00Z").replace(
                    tzinfo=dt.UTC
                )
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
                continue
            if trip.get("stage") == 2 and trip["polls"] >= self.limits["escalate_after_polls"]:
                self.harden(worker)
            elif (
                trip.get("stage") == 3
                and self.epoch - trip.get("last_stuck_alert", 0)
                >= self.limits["realert_minutes"] * 60
            ):
                if self.notify(
                    f"{worker} is still over after the fallback stop",
                    [
                        f"{average:,.0f} per minute of {trip['metric']}, target {trip['target_per_minute']:,.0f}.{GAP}Stop it by hand."
                    ],
                ):
                    trip["last_stuck_alert"] = self.epoch

    # ---- the run

    def apply_breaches(self, breaches: list, inventory: dict) -> None:
        due = [
            b
            for b in breaches
            if self.epoch - self.state["alerted"].get(b["key"], 0)
            >= self.limits["realert_minutes"] * 60
        ]
        wanted: dict = {}
        # Observe mode only reports, so it reads each Worker's version when it has something new to say.
        for breach in breaches if self.mode == "enforce" else due:
            for worker in trip_targets(self.cfg, self.profile, breach, inventory):
                if worker not in self.state["trips"] and worker not in wanted:
                    wanted[worker] = breach
        versions: dict = {}
        for worker in {
            w for b in due for w in trip_targets(self.cfg, self.profile, b, inventory)
        } | set(wanted):
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
            if self.notify(f"Cloudflare usage over a limit on {self.cfg['account_name']}", lines):
                for breach in due:
                    self.state["alerted"][breach["key"]] = self.epoch
                self.store.save(self.state)
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
            self.trip(worker, breach, versions[worker])
            tripped_this_run += 1

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
            account = self.conn.graphql(build_query(), query_variables(self.cfg, self.now))
        except PollError as exc:
            self.handle_poll_failure(str(exc))
            self.store.save(self.state)
            return 1
        if (
            self.state.pop("poll_failed_since", None) is not None
            and self.state.pop("poll_fail_alerted", None) is not None
        ):
            self.notify(
                "Cloudflare usage watchdog can read again", ["Usage reads are working."], page=False
            )
        self.state.pop("poll_fail_alerted", None)
        inventory = self.refresh_inventory_if_stale()
        usage = parse_usage(account, inventory.get("ns_to_script", {}))
        self._usage = usage
        self.beat = self.conn.heartbeat()
        self.store.event("poll_ok", heartbeat=self.beat, trips=sorted(self.state["trips"]))
        if self.mode == "enforce":
            self.check_trips(usage)
        self.apply_breaches(evaluate(self.cfg, self.profile, usage), inventory)
        self.daily_check_if_due(inventory)
        self.prune()
        self.store.save(self.state)
        return 1 if self.undelivered else 0

    def prune(self) -> None:
        cutoff = self.epoch - 2 * 86400
        self.state["alerted"] = {
            key: at for key, at in self.state["alerted"].items() if at >= cutoff
        }
        today = floor_minute(self.now).strftime("%Y-%m-%d")
        self.state["trips_by_day"] = {
            day: count for day, count in self.state["trips_by_day"].items() if day == today
        }

    # ---- the daily read:  who owns what, and does the account still match git

    def refresh_inventory_if_stale(self) -> dict:
        inventory = self.state.get("inventory") or {}
        if self.epoch - inventory.get("at", 0) < 86400:
            return inventory
        try:
            inventory = self.read_inventory()
        except (ApiError, KeyError, TypeError) as exc:
            self.store.event("inventory_failed", error=str(exc)[:100])
            return self.state.get("inventory") or {}
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
        if self.state.get("daily_check_day") == today or not inventory.get("workers"):
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
        if entry.get("preview_urls") is False and live.get("previews_enabled"):
            drift.append(f"{name} has preview URLs on, the registry says off.")
        if entry.get("workers_dev") is False and live.get("workers_dev"):
            drift.append(f"{name} has workers.dev on, the registry says off.")
    return drift


# ---------------------------------------------------------------- re-arm


def rearm(cfg: dict, conn: Any, store: Store, state: dict, worker: str, apply: bool) -> int:
    """Undo a trip from what it recorded.  Manual on purpose.  Without --apply it only prints the plan."""
    trip = state["trips"].get(worker)
    if not trip:
        print(f"{worker} is not tripped")
        return 2
    base = f"/accounts/{cfg['account_id']}"
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
    for method, url, body in plan:
        try:
            conn.api(method, url, body)
            store.event("rearm_step", worker=worker, method=method, path=url.replace(base, ""))
        except ApiError as exc:
            failed += 1
            store.event(
                "rearm_failed",
                worker=worker,
                method=method,
                path=url.replace(base, ""),
                status=exc.status,
                codes=exc.codes,
            )
    if failed:
        print(f"{failed} step(s) failed;  the trip stays recorded.  Fix them and run this again.")
        return 1
    del state["trips"][worker]
    store.save(state)
    print(
        f"{worker} re-armed.  It stays on the version the trip left live;  previous version {trip.get('previous_version')}."
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


def read_knobs(stop_file: Path) -> tuple:
    """Mode and profile from Infisical through fleet_knobs.  Anything wrong reads as observe."""
    try:
        sys.path.insert(0, str(HERE.parent))
        from fleet_knobs import knobs

        return (
            knobs.get("CF_USAGE_WATCHDOG_MODE", stop=stop_file.exists()),
            knobs.get("CF_USAGE_WATCHDOG_PROFILE"),
        )
    except Exception as exc:  # noqa: BLE001 - A broken knob read must never turn into enforcement or a crash.
        print(json.dumps({"event": "knobs_failed", "error": type(exc).__name__}), flush=True)
        return "observe", "paid"


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


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "command", nargs="?", default="run", choices=("run", "thresholds", "status", "rearm")
    )
    parser.add_argument("worker", nargs="?")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    cfg = load_config()
    if args.command == "thresholds":
        print_thresholds(cfg)
        return 0
    store = Store(default_state_dir(dict(os.environ)))
    state = store.load()
    if args.command == "status":
        print(
            json.dumps(
                {"trips": state["trips"], "poll_failed_since": state.get("poll_failed_since")},
                indent=1,
                sort_keys=True,
            )
        )
        return 0
    conn = Connections(cfg)
    if args.command == "rearm":
        if not args.worker:
            parser.error("rearm needs a Worker name")
        return rearm(cfg, conn, store, state, args.worker, args.apply)
    mode, profile = read_knobs(store.directory / "STOP")
    if profile not in cfg["profiles"]:
        store.event("profile_unknown")
        profile = "paid"
    now = dt.datetime.fromtimestamp(time.time(), dt.UTC)
    return Watchdog(cfg, conn, store, state, now, mode, profile).run()


if __name__ == "__main__":
    sys.exit(main())
