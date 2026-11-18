#!/usr/bin/env python3
"""Write Socratic Trade and Congress.Trade operational health for Muster (AFC-398).

This is the reserved ``api`` collector:  it polls the two apps' own health
routes and writes one state file Muster reads,
``~/.local/state/fleet-tools/api-health.json`` (mode 0600, written
atomically).  It is the adapter input only.  AFC-398 completes when
collectors push health to one authenticated endpoint or state store and
Muster reads the store;  this script is that store's writer, in the same
shape as ``fleet-mac-services.py``, ``fleet-server-status.py`` and
``fleet-worker-health.py``.

Read only, end to end.  The collector issues GET requests and writes its
own state file.  It never calls a mutating route, never restarts or
requeues anything, holds no credential, and changes nothing in Socratic
Trade or Congress.Trade.  Muster only displays the file;  the Operator
decides letters, and nothing here touches letter logic.

AFC-441 discipline, applied throughout:

  * Bounded destination.  Only the two pinned URLs below may be fetched.
    A URL with another scheme, host or path is refused before any request
    exists, and redirects are never followed.
  * Timeout and size bound.  Every fetch carries a timeout (default
    10 seconds, hard ceiling 30) and a response size cap.
  * Provenance.  Every app row names its source URL, the time it was
    fetched and the HTTP status it answered with.  No upstream free text
    is copied into the store;  only fixed codes, states and numbers ride.
  * Freshness.  The document carries ``generatedAt``.  A reader treats a
    file that is missing, malformed, of another ``schemaVersion`` or
    older than 45 minutes as unknown, exactly as Muster treats the other
    collectors' files.
  * Fail closed.  A fetch that fails, times out or answers unparseable
    output yields an ``unknown`` app row with a fixed error code, never a
    green one, and the run still writes the file and exits 1.

The mapping contract (Jet's sign-off for AFC-398, 2026-10-23) is pinned
in ``map_st_live`` and ``map_ct_health``:

  * ST ``/api/proxy-use`` reports the cached Mango reachability probe.  Green (``ok``) requires
    ``mango.reachable`` to be exactly true AND the last reachability probe to be
    fresh by ``mango.checkedAt`` (age at most ``ST_OBSERVATION_MAX_AGE_SECONDS``).  HTTP 200 and
    the body's ``ok`` flag alone never make ST green.
  * CT keeps its upstream aggregate and its components.  A direct
    ``senate_efd`` failure does not mean ingestion stopped:  while the
    all three chamber polls are observed alive, a failing non-ingestion
    component degrades CT to ``warn``, never to ``fail``.
  * A missing optional check is ``unknown``, never zero and never green:
    a component the upstream did not report keeps state ``unknown``, and
    an app with any expected component unreported cannot read ``ok``.

State vocabulary, shared by both apps and the document's ``worst``:
``ok``, ``warn``, ``fail``, ``unknown``.  ``unknown`` always means "not
observed", never "observed healthy".

  fleet-api-health.py [--write PATH] [--timeout SECONDS] [--dry-run]

--dry-run fetches, prints the document and writes nothing.  Exit 0 only
when both apps were observed and mapped to a known state (no row is
``unknown``, whether from a fetch error or a check the upstream did not
report);  1 otherwise, with the file still written.  Python 3.9,
standard library only.
"""

import argparse
import json
import os
import socket
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

TOOL = "fleet-api-health"
COLLECTOR = "api"
SCHEMA_VERSION = 1
DEFAULT_PATH = os.path.expanduser("~/.local/state/fleet-tools/api-health.json")
USER_AGENT = "fleet-api-health/1 (AI-Fleet-Coordinator; AFC-398)"

# The only destinations this collector may ever fetch.  The path is part
# of the pin:  another route on the same host is still another route.
ST_LIVE_URL = "https://socratictrade.com/api/proxy-use"
CT_HEALTH_URL = "https://congress.trade/api/health"
ALLOWED_URLS = frozenset({ST_LIVE_URL, CT_HEALTH_URL})

DEFAULT_TIMEOUT_SECONDS = 10
MAX_TIMEOUT_SECONDS = 30
MAX_RESPONSE_BYTES = 256 * 1024
# A read that times out raises `socket.timeout`, which is `TimeoutError` only from Python 3.10;  the
# Mac's /usr/bin/python3 is 3.9, where it is a plain OSError and would read as `unreachable`.
TIMEOUTS = (TimeoutError, socket.timeout)

# ST green needs an observation at most this old.  Two minutes covers a
# live loop that observes about once a minute while still refusing to
# call a wedged loop current.
ST_OBSERVATION_MAX_AGE_SECONDS = 120

# The window Muster grants every collector's file (three 15-minute runs).
STORE_MAX_AGE_SECONDS = 45 * 60

STATES = ("ok", "warn", "fail", "unknown")
_STATE_RANK = {"ok": 0, "warn": 1, "unknown": 2, "fail": 3}

# CT's expected components, in display order.  ``ingestion`` is the one
# component whose failure means filing stopped;  the rest are upstreams.
CT_COMPONENTS = ("house", "senate_efd", "oge", "ingestion")
CT_INGESTION_COMPONENT = "ingestion"

# Fixed fetch error codes.  An upstream's own error text never rides.
FETCH_ERROR_CODES = ("timeout", "unreachable", "unparseable", "refused_url")


def utc_stamp(epoch=None):
    """An ISO 8601 UTC stamp in the spelling every collector uses."""
    moment = datetime.fromtimestamp(time.time() if epoch is None else epoch, timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(raw):
    """Parse an ISO 8601 stamp into epoch seconds, or None.

    Accepts a ``Z`` suffix or an explicit offset.  A stamp with no offset
    is read as UTC.  Anything unparseable is None, never a guess.
    """
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def allowed_url(url):
    """True only for the two pinned destinations, byte for byte.

    The comparison is on the parsed scheme, host and path with no query
    and no fragment, so a lookalike host, an added query or a redirect
    target can never widen the bound.
    """
    if url not in ALLOWED_URLS:
        return False
    parts = urllib.parse.urlsplit(url)
    return parts.scheme == "https" and not parts.query and not parts.fragment


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a 3xx:  the answer must come from the pinned origin."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch_json(url, timeout=DEFAULT_TIMEOUT_SECONDS, opener=None):
    """GET one pinned URL.  Returns (status, payload, error_code).

    Exactly one of ``payload`` and ``error_code`` is set on success of the
    other:  a 200 with a JSON object gives (200, dict, None);  every other
    outcome gives (status_or_0, None, fixed_code).  A refused URL raises
    ValueError before any request exists.
    """
    if not allowed_url(url):
        raise ValueError("refused url: %r" % (url,))
    timeout = max(1, min(int(timeout), MAX_TIMEOUT_SECONDS))
    request = urllib.request.Request(
        url, method="GET", headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
    )
    build = opener if opener is not None else urllib.request.build_opener(_NoRedirect)
    try:
        with build.open(request, timeout=timeout) as response:
            status = response.status
            body = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as err:
        return err.code, None, "http_%d" % err.code
    except TIMEOUTS:
        return 0, None, "timeout"
    except urllib.error.URLError as err:
        reason = getattr(err, "reason", None)
        if isinstance(reason, TIMEOUTS) or "timed out" in str(reason).lower():
            return 0, None, "timeout"
        return 0, None, "unreachable"
    except OSError:
        return 0, None, "unreachable"
    if len(body) > MAX_RESPONSE_BYTES:
        return status, None, "unparseable"
    try:
        payload = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return status, None, "unparseable"
    if not isinstance(payload, dict):
        return status, None, "unparseable"
    return status, payload, None


def _observation_age(payload, now):
    """Age of the source-native Mango probe; missing is never guessed.

    The public proxy-use route emits mango.checkedAt. Other observation
    timestamps describe a different contract and cannot make it green.
    """
    mango = payload.get("mango") if isinstance(payload.get("mango"), dict) else {}
    raw = mango.get("checkedAt")
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return None
        return int(now - parsed.timestamp())
    except (ValueError, OverflowError, OSError):
        return None


def map_st_live(payload, http_status, now):
    """Map one ST /api/proxy-use answer to its store row (minus provenance).

    Green is the conjunction, and only the conjunction:  the route
    answered 200, Mango is explicitly reachable, and the observation is
    proved fresh.  Every missing piece is named in ``reason`` and keeps
    the row out of ``ok``.
    """
    row = {
        "app": "ST",
        "displayName": "Socratic Trade",
        "state": "unknown",
        "reason": "not_observed",
        "mangoReachable": None,
        "observationAgeSeconds": None,
        "upstreamOk": None,
    }
    if http_status != 200 or not isinstance(payload, dict):
        row["reason"] = "http_%d" % http_status if http_status else "no_answer"
        return row
    if isinstance(payload.get("ok"), bool):
        row["upstreamOk"] = payload["ok"]
    mango = payload.get("mango")
    if not isinstance(mango, dict) or not isinstance(mango.get("reachable"), bool):
        # Mango unreported is unobserved, however green the rest looks.
        row["reason"] = "mango_unreported"
        return row
    row["mangoReachable"] = mango["reachable"]
    age = _observation_age(payload, now)
    row["observationAgeSeconds"] = age
    if mango["reachable"] is not True:
        row["state"] = "fail"
        row["reason"] = "mango_unreachable"
        return row
    if age is None:
        row["reason"] = "observation_unreported"
        return row
    if age < 0:
        row["reason"] = "observation_in_future"
        return row
    if age > ST_OBSERVATION_MAX_AGE_SECONDS:
        # A stale observation proves the loop once ran, not that it runs.
        row["reason"] = "observation_stale"
        return row
    row["state"] = "ok"
    row["reason"] = "mango_reachable_and_observation_fresh"
    return row


def _component_state(value):
    """Normalize one CT component value to the shared vocabulary.

    Accepts a bare state string or an object carrying ``state`` /
    ``status`` / ``ok``.  Anything unrecognized is ``unknown``:  a word
    this adapter does not know is never read as healthy.
    """
    if isinstance(value, dict):
        for key in ("state", "status"):
            if isinstance(value.get(key), str):
                return _component_state(value[key])
        if isinstance(value.get("ok"), bool):
            return "ok" if value["ok"] else "fail"
        if isinstance(value.get("reachable"), bool):
            return "ok" if value["reachable"] else "fail"
        return "unknown"
    if isinstance(value, bool):
        return "ok" if value else "fail"
    if isinstance(value, str):
        word = value.strip().lower()
        if word in ("ok", "healthy", "up", "pass", "passed", "operational"):
            return "ok"
        if word in ("warn", "warning", "degraded"):
            return "warn"
        if word in ("fail", "failed", "down", "error", "unhealthy", "critical", "stalled"):
            return "fail"
    return "unknown"


def _ct_pipeline_components(payload):
    """Source-native CT checks, without conflating backlog and liveness.

    All three chamber polls must explicitly be current to prove ingestion
    alive.  A failed poll proves that lane stopped.  An empty outbox does
    not prove any poll ran.  The separate senate_efd canary is not Senate
    polling: a canary failure alone never declares ingestion stopped.
    Duplicate IDs are ambiguous and therefore unknown, never first-wins.
    """
    pipeline = payload.get("pipeline")
    if not isinstance(pipeline, dict) or "checks" not in pipeline:
        return None
    checks = pipeline.get("checks")
    if not isinstance(checks, list):
        return {}
    by_id = {}
    for entry in checks:
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
            continue
        name = entry["id"]
        by_id[name] = "unknown" if name in by_id else _component_state(entry.get("status"))
    found = {}
    for target, source in (("house", "polling_house"), ("oge", "polling_executive"),
                           ("senate_efd", "senate_efd")):
        if source in by_id:
            found[target] = by_id[source]
    polls = [by_id.get(name, "unknown") for name in
             ("polling_house", "polling_senate", "polling_executive")]
    if "fail" in polls:
        found["ingestion"] = "fail"
    elif all(state == "ok" for state in polls):
        found["ingestion"] = "ok"
    elif all(state in ("ok", "warn") for state in polls):
        found["ingestion"] = "warn"
    else:
        found["ingestion"] = "unknown"
    return found


def _ct_components(payload):
    """CT's components as {name: raw value}, from wherever upstream puts them."""
    native = _ct_pipeline_components(payload)
    if native is not None:
        return native
    found = {}
    for container_key in ("components", "checks", "upstreams"):
        container = payload.get(container_key)
        if isinstance(container, dict):
            for name, value in container.items():
                if isinstance(name, str):
                    found.setdefault(name.strip().lower(), value)
    for name in CT_COMPONENTS:
        if name in payload:
            found.setdefault(name, payload[name])
    return found


def _ct_aggregate(payload):
    """CT's upstream aggregate word, verbatim, or None when unreported."""
    for key in ("status", "state", "health"):
        if isinstance(payload.get(key), str):
            return payload[key]
    if isinstance(payload.get("ok"), bool):
        return "ok" if payload["ok"] else "fail"
    return None


def map_ct_health(payload, http_status, now):
    """Map one CT /api/health answer to its store row (minus provenance).

    The upstream aggregate rides verbatim as ``upstreamStatus``;  the
    mapped ``state`` is this adapter's verdict under the pinned rules,
    and the two are never conflated.  Ingestion is the load-bearing
    component:  its observed failure is CT's failure, its observed life
    caps every other component's failure at ``warn``, and its absence is
    ``unknown``.
    """
    row = {
        "app": "CT",
        "displayName": "Congress.Trade",
        "state": "unknown",
        "reason": "not_observed",
        "upstreamStatus": None,
        "ingestionAlive": None,
        "components": [
            {"name": name, "state": "unknown", "reported": False} for name in CT_COMPONENTS
        ],
    }
    if http_status != 200 or not isinstance(payload, dict):
        row["reason"] = "http_%d" % http_status if http_status else "no_answer"
        return row
    row["upstreamStatus"] = _ct_aggregate(payload)
    found = _ct_components(payload)
    components = []
    for name in CT_COMPONENTS:
        if name in found:
            components.append(
                {"name": name, "state": _component_state(found[name]), "reported": True}
            )
        else:
            components.append({"name": name, "state": "unknown", "reported": False})
    row["components"] = components
    by_name = {entry["name"]: entry for entry in components}
    ingestion = by_name[CT_INGESTION_COMPONENT]
    # Alive or stopped only when ingestion was reported in a word this
    # adapter knows;  an unreadable value proves neither.
    row["ingestionAlive"] = (
        ingestion["state"] in ("ok", "warn")
        if ingestion["reported"] and ingestion["state"] != "unknown"
        else None
    )
    # An observed ingestion failure is CT's failure even when some other
    # check went unreported:  the one outage seen must not read as unknown.
    if ingestion["state"] == "fail":
        row["state"] = "fail"
        row["reason"] = "ingestion_failed"
        return row
    unreported = [entry["name"] for entry in components if not entry["reported"]]
    if unreported:
        # A missing optional check is unknown, never zero and never green.
        row["reason"] = "components_unreported:%s" % ",".join(unreported)
        return row
    if ingestion["state"] == "unknown":
        row["reason"] = "ingestion_unproved"
        return row
    failing = [
        entry["name"]
        for entry in components
        if entry["name"] != CT_INGESTION_COMPONENT and entry["state"] == "fail"
    ]
    warning = [entry["name"] for entry in components if entry["state"] in ("warn", "unknown")]
    if failing:
        # senate_efd down is an upstream outage, not stopped ingestion.
        row["state"] = "warn"
        row["reason"] = "upstream_failed_ingestion_alive:%s" % ",".join(failing)
        return row
    if warning:
        row["state"] = "warn"
        row["reason"] = "components_degraded:%s" % ",".join(warning)
        return row
    aggregate = _component_state(row["upstreamStatus"])
    if aggregate != "ok":
        # Other source checks may degrade the aggregate even when the
        # displayed subset is current. Never hide an upstream warning.
        row["state"] = "warn" if aggregate in ("warn", "fail") else "unknown"
        row["reason"] = "upstream_aggregate_not_ok"
        return row
    row["state"] = "ok"
    row["reason"] = "all_components_ok"
    return row


def _app_row(mapper, url, timeout, now, fetcher):
    """Fetch one app and finish its row with provenance and freshness."""
    fetched_at = utc_stamp(now)
    try:
        status, payload, error = fetcher(url, timeout)
    except ValueError:
        status, payload, error = 0, None, "refused_url"
    except TIMEOUTS:
        status, payload, error = 0, None, "timeout"
    if error is not None or payload is None:
        row = mapper(None, status or 0, now)
        row["state"] = "unknown"
        row["reason"] = error if error in FETCH_ERROR_CODES else (error or "no_answer")
        row["error"] = row["reason"]
    else:
        row = mapper(payload, status, now)
    row["provenance"] = {"source": url, "fetchedAt": fetched_at, "httpStatus": status or 0}
    return row


def worst_of(rows):
    """The document's worst app state, by the shared ranking."""
    worst = "ok"
    for row in rows:
        state = row.get("state") if row.get("state") in STATES else "unknown"
        if _STATE_RANK[state] > _STATE_RANK[worst]:
            worst = state
    return worst


def collect(now=None, timeout=DEFAULT_TIMEOUT_SECONDS, fetcher=None):
    """Fetch both apps and build the store document.

    ``fetcher`` is injectable so tests drive every mapping with fixtures
    and no network:  it takes (url, timeout) and returns the
    ``fetch_json`` triple.
    """
    now = time.time() if now is None else now
    fetcher = fetch_json if fetcher is None else fetcher
    rows = [
        _app_row(map_st_live, ST_LIVE_URL, timeout, now, fetcher),
        _app_row(map_ct_health, CT_HEALTH_URL, timeout, now, fetcher),
    ]
    return {
        "schemaVersion": SCHEMA_VERSION,
        "tool": TOOL,
        "collector": COLLECTOR,
        "generatedAt": utc_stamp(now),
        "worst": worst_of(rows),
        "apps": rows,
    }


def store_reading(doc, now=None):
    """Judge a store document the way Muster does.

    Returns "unknown" for a missing, malformed, wrong-version,
    wrong-tool or stale document, and the document's ``worst`` only when
    the file is current and well formed.  This is the reference reader
    for the contract;  Muster's own reader is
    ``muster/Muster/Model/ApiHealthReport.swift``.
    """
    if not isinstance(doc, dict):
        return "unknown"
    if doc.get("schemaVersion") != SCHEMA_VERSION or doc.get("tool") != TOOL:
        return "unknown"
    generated = parse_iso(doc.get("generatedAt"))
    if generated is None:
        return "unknown"
    now = time.time() if now is None else now
    age = now - generated
    if age < -60 or age > STORE_MAX_AGE_SECONDS:
        return "unknown"
    worst = doc.get("worst")
    return worst if worst in STATES else "unknown"


def write_atomic(path, doc):
    """Write the document atomically at 0600, refusing a symlinked directory."""
    directory = os.path.dirname(os.path.abspath(path))
    if os.path.islink(directory):
        raise OSError("state directory is a symlink; refusing to write")
    os.makedirs(directory, mode=0o700, exist_ok=True)
    if os.path.islink(directory):
        raise OSError("state directory is a symlink; refusing to write")
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".api-health-")
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(doc, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        dir_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--write", default=DEFAULT_PATH)
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    doc = collect(timeout=args.timeout)
    if args.dry_run:
        print(json.dumps(doc, indent=2, sort_keys=True))
    else:
        write_atomic(args.write, doc)
    # Exit 0 only when both apps were actually observed:  a row carrying a
    # fetch error, or still unknown after mapping, means this run proved
    # less than the whole document, and the caller should hear that.
    observed = all("error" not in row and row["state"] != "unknown" for row in doc["apps"])
    return 0 if observed else 1


if __name__ == "__main__":
    sys.exit(main())
