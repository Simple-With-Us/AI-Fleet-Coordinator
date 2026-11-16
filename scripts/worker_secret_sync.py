#!/usr/bin/env python3
"""Dry-run, then optionally put, one Worker's Infisical folder onto Cloudflare.

Cloudflare's secret list returns names and types only.  A metadata secret
written with ``secret put`` cannot be read back, so the next check could not
tell a match from a change.  This module stores hex digests in the plain_text
binding ``FLEET_SECRET_DIGESTS``.  The script settings API returns that
binding's text and still omits every secret_text value.  Each digest is
HMAC-SHA256 over ``name + NUL + value``, keyed by ``FLEET_SECRET_DIGEST_KEY``
from the same Infisical folder.  The key is never put on the Worker and never
printed.  Until the binding exists, a name present on both sides is ``differ``:
nothing has proved the values match.

Apply stays off unless the process is started with ``--apply`` and
``WORKER_SECRET_SYNC_APPLY`` is exactly ``true``.  Apply puts only names listed
in the Worker's committed allowlist.  It does not delete Worker-only secrets.
A partial Infisical or Cloudflare read raises before a diff is reported and
before any put.  Universal-auth client ids and client secrets are not read.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import pathlib
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime

ROOT = pathlib.Path(__file__).resolve().parent.parent
FOLDERS_PATH = ROOT / "scripts" / "worker-secret-folders.json"
ALLOWLIST_DIR = ROOT / "scripts" / "worker-secret-allowlists"
INFISICAL_SITE = "https://app.infisical.com"
CLOUDFLARE_BASE = "https://api.cloudflare.com/client/v4"
USER_AGENT = "AI-Fleet-Coordinator-worker-secret-sync/1.0"
DIGEST_BINDING = "FLEET_SECRET_DIGESTS"
DIGEST_KEY_NAME = "FLEET_SECRET_DIGEST_KEY"
APPLY_ENV = "WORKER_SECRET_SYNC_APPLY"
NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
WORKER_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
ACCOUNT_RE = re.compile(r"^[a-f0-9]{32}$")
TOKEN_ENV_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,80}$")
SERVICE_TOKEN_RE = re.compile(r"^st\.\S{17,2045}$")
PROJECT_RE = re.compile(r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$", re.IGNORECASE)
UNRESOLVED_REFERENCE = re.compile(r"\$\{[^}]+\}")
HEX_DIGEST = re.compile(r"^[0-9a-f]{64}$")
SECRET_TYPES = frozenset({"secret_text", "secret_key"})
METADATA_LIMIT = 128 * 1024
BODY_LIMIT = 2 * 1024 * 1024
DIGEST_TEXT_LIMIT = 24 * 1024


class SyncError(Exception):
    """Fail closed.  The message is a fixed code plus an optional secret name."""


def apply_enabled(env, apply_flag):
    """True only for an explicit flag and the exact variable value ``true``."""
    return bool(apply_flag) and env.get(APPLY_ENV) == "true"


def per_key_digest(key, name, value):
    """HMAC of one secret.  The name is part of the message so equal values differ."""
    mac = hmac.new(key.encode("utf-8"), (name + "\0" + value).encode("utf-8"), hashlib.sha256)
    return mac.hexdigest()


def mask_text(text, values):
    """Literal replacement, longest first.  Short tokens are left alone.

    A 1-3 character value would redact ordinary report words.  Real credentials
    are longer than that.  The match is ``str.replace``, never a regular expression,
    so a value that contains ``/`` or ``*`` cannot widen the mask.
    """
    redacted = str(text)
    secrets = sorted(
        {value for value in values if isinstance(value, str) and len(value) >= 4},
        key=len,
        reverse=True,
    )
    for value in secrets:
        redacted = redacted.replace(value, "[REDACTED]")
    return redacted


def classify(desired_digests, worker_names, stored, allowlist):
    """Names only.  ``stored`` is the digest map, or None when the binding is absent."""
    desired = set(desired_digests)
    worker = set(worker_names) - {DIGEST_BINDING}
    missing = sorted(desired - worker)
    only = sorted(worker - desired)
    differ = []
    for name in sorted(desired & worker):
        current = desired_digests[name]
        recorded = None if stored is None else stored.get(name)
        same = (
            isinstance(recorded, str)
            and len(recorded) == len(current)
            and hmac.compare_digest(recorded, current)
        )
        if not same:
            differ.append(name)
    allowed = set(allowlist)
    withheld = sorted(name for name in (*missing, *differ) if name not in allowed)
    return {
        "missing_on_worker": tuple(missing),
        "only_on_worker": tuple(only),
        "differ": tuple(differ),
        "not_allowlisted": tuple(withheld),
    }


def puts_for_apply(diff, allowlist):
    """Allowlisted missing or differing names.  Worker-only names are never deleted."""
    allowed = set(allowlist) - {DIGEST_KEY_NAME, DIGEST_BINDING}
    chosen = set(diff["missing_on_worker"]) | set(diff["differ"])
    return tuple(sorted(name for name in chosen if name in allowed))


def render_report(diff):
    """One name per line.  Raises if a name is not a token, so a value cannot hide in it."""
    lines = []
    buckets = (
        ("missing-on-worker", diff["missing_on_worker"]),
        ("only-on-worker", diff["only_on_worker"]),
        ("differ", diff["differ"]),
        ("not-allowlisted", diff["not_allowlisted"]),
    )
    for label, names in buckets:
        if not names:
            lines.append(label + ": (none)")
            continue
        for name in names:
            if not isinstance(name, str) or not NAME_RE.fullmatch(name):
                raise SyncError("invalid-secret-name")
            lines.append(label + ": " + name)
    return "\n".join(lines) + "\n"


def load_folder_map(path=FOLDERS_PATH):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SyncError("folder-map-unreadable") from exc
    if not isinstance(data, dict) or not data:
        raise SyncError("folder-map-unreadable")
    parsed = {}
    for worker, row in data.items():
        if not WORKER_RE.fullmatch(worker) or not isinstance(row, dict):
            raise SyncError("folder-map-unreadable")
        project = row.get("project_id")
        environment = row.get("environment")
        folder = row.get("folder")
        token_env = row.get("token_env")
        if (
            not isinstance(project, str)
            or not PROJECT_RE.fullmatch(project)
            or environment != "prod"
            or not _folder_ok(folder)
            or not isinstance(token_env, str)
            or not TOKEN_ENV_RE.fullmatch(token_env)
        ):
            raise SyncError("folder-map-unreadable")
        parsed[worker] = {
            "project_id": project,
            "environment": "prod",
            "folder": folder,
            "token_env": token_env,
        }
    return parsed


def load_allowlist(worker, directory=ALLOWLIST_DIR):
    path = directory / (worker + ".json")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SyncError("allowlist-unreadable") from exc
    names = data.get("names") if isinstance(data, dict) else None
    if not isinstance(names, list):
        raise SyncError("allowlist-unreadable")
    if len(names) != len(set(names)):
        raise SyncError("allowlist-unreadable")
    for name in names:
        if not isinstance(name, str) or not NAME_RE.fullmatch(name):
            raise SyncError("allowlist-unreadable")
        if name in (DIGEST_KEY_NAME, DIGEST_BINDING):
            raise SyncError("allowlist-unreadable")
    return tuple(names)


def _folder_ok(folder):
    if not isinstance(folder, str) or not folder.startswith("/") or folder.endswith("/"):
        return False
    parts = folder.split("/")
    return ".." not in parts and "." not in parts and all(parts[1:])


def require_service_token(token):
    if not isinstance(token, str) or not SERVICE_TOKEN_RE.fullmatch(token):
        raise SyncError("service-token-required")
    return token


def validate_service_metadata(metadata, project_id, folder, now):
    """The same single-folder read-only grant ``scripts/infisical-fetch.mjs`` requires."""
    if (
        not isinstance(metadata, dict)
        or not isinstance(metadata.get("id"), str)
        or not metadata["id"]
        or not isinstance(metadata.get("name"), str)
        or not metadata["name"]
        or metadata.get("workspace") != project_id
        or ("projectId" in metadata and metadata.get("projectId") != project_id)
    ):
        raise SyncError("token-scope-mismatch")
    scopes = metadata.get("scopes")
    permissions = metadata.get("permissions")
    if (
        not isinstance(scopes, list)
        or len(scopes) != 1
        or not isinstance(scopes[0], dict)
        or sorted(scopes[0]) != ["environment", "secretPath"]
        or scopes[0].get("environment") != "prod"
        or scopes[0].get("secretPath") != folder
        or permissions != ["read"]
    ):
        raise SyncError("token-scope-mismatch")
    expiry = metadata.get("expiresAt")
    if expiry is None:
        return
    if not isinstance(expiry, str) or not re.search(r"(?:Z|[+-]\d{2}:\d{2})$", expiry):
        raise SyncError("token-scope-mismatch")
    try:
        parsed = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SyncError("token-scope-mismatch") from exc
    if parsed.tzinfo is None or parsed <= now:
        raise SyncError("token-scope-mismatch")


def parse_infisical_folder(body, project_id, folder):
    """The whole folder, or an error.  A short or mixed page is never a success."""
    if not isinstance(body, dict) or not isinstance(body.get("secrets"), list):
        raise SyncError("infisical-incomplete")
    secrets = body["secrets"]
    if "totalCount" in body and body["totalCount"] != len(secrets):
        raise SyncError("infisical-incomplete")
    for key in ("pagination", "nextPage", "nextCursor"):
        if body.get(key):
            raise SyncError("infisical-incomplete")
    if body.get("imports"):
        raise SyncError("infisical-incomplete")
    found = {}
    for record in secrets:
        key, value = _infisical_record(record, project_id, folder)
        if key in found:
            raise SyncError("infisical-incomplete")
        if UNRESOLVED_REFERENCE.search(value):
            raise SyncError("unresolved-reference")
        found[key] = value
    return found


def _infisical_record(record, project_id, folder):
    if (
        not isinstance(record, dict)
        or record.get("type") != "shared"
        or record.get("secretValueHidden") is True
    ):
        raise SyncError("infisical-incomplete")
    for field, expected in (
        ("workspace", project_id),
        ("environment", "prod"),
        ("secretPath", folder),
    ):
        if field in record and record.get(field) != expected:
            raise SyncError("infisical-incomplete")
    key = record.get("secretKey")
    value = record.get("secretValue")
    if not isinstance(key, str) or not NAME_RE.fullmatch(key) or not isinstance(value, str):
        raise SyncError("infisical-incomplete")
    return key, value


def parse_worker_secret_names(body):
    if (
        not isinstance(body, dict)
        or body.get("success") is not True
        or not isinstance(body.get("result"), list)
    ):
        raise SyncError("cloudflare-incomplete")
    result = body["result"]
    info = body.get("result_info")
    if isinstance(info, dict) and (
        ("total_count" in info and info["total_count"] != len(result))
        or info.get("cursor")
        or info.get("next_cursor")
    ):
        raise SyncError("cloudflare-incomplete")
    names = []
    for item in result:
        if not isinstance(item, dict) or item.get("type") not in SECRET_TYPES:
            raise SyncError("cloudflare-incomplete")
        name = item.get("name")
        if not isinstance(name, str) or not NAME_RE.fullmatch(name):
            raise SyncError("cloudflare-incomplete")
        names.append(name)
    if len(names) != len(set(names)):
        raise SyncError("cloudflare-incomplete")
    return names


def parse_stored_digests(settings_body):
    """None when the binding is absent.  A present but unusable record fails closed."""
    bindings = _settings_bindings(settings_body)
    found = [item for item in bindings if item.get("name") == DIGEST_BINDING]
    if not found:
        return None
    if len(found) != 1 or found[0].get("type") != "plain_text":
        raise SyncError("digest-record-unreadable")
    text = found[0].get("text")
    if not isinstance(text, str) or len(text.encode("utf-8")) > DIGEST_TEXT_LIMIT:
        raise SyncError("digest-record-unreadable")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SyncError("digest-record-unreadable") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("v") != 1
        or payload.get("algo") != "hmac-sha256"
        or not isinstance(payload.get("keys"), dict)
    ):
        raise SyncError("digest-record-unreadable")
    stored = {}
    for name, digest in payload["keys"].items():
        if not isinstance(name, str) or not NAME_RE.fullmatch(name):
            raise SyncError("digest-record-unreadable")
        if not isinstance(digest, str) or not HEX_DIGEST.fullmatch(digest):
            raise SyncError("digest-record-unreadable")
        stored[name] = digest
    return stored


def _settings_bindings(body):
    if not isinstance(body, dict) or body.get("success") is not True:
        raise SyncError("cloudflare-incomplete")
    result = body.get("result")
    if not isinstance(result, dict) or not isinstance(result.get("bindings"), list):
        raise SyncError("cloudflare-incomplete")
    return result["bindings"]


def bindings_for_digest_update(bindings, digest_text):
    """Full binding list for one settings patch.

    ``secret_text`` and ``secret_key`` are sent as name and type only, which is
    how ``wrangler secret bulk`` tells Cloudflare to keep the current value.
    Every other binding is copied.  A type whose name contains ``secret`` and
    is not one of those two is refused, because copying it might drop or echo
    a value this process has not classified.
    """
    if not isinstance(bindings, list) or len(digest_text.encode("utf-8")) > DIGEST_TEXT_LIMIT:
        raise SyncError("settings-incomplete")
    outgoing = []
    seen = set()
    replaced = False
    for binding in bindings:
        if not isinstance(binding, dict):
            raise SyncError("settings-incomplete")
        name = binding.get("name")
        kind = binding.get("type")
        if not isinstance(name, str) or not NAME_RE.fullmatch(name) or not isinstance(kind, str):
            raise SyncError("settings-incomplete")
        if name in seen:
            raise SyncError("settings-incomplete")
        seen.add(name)
        if name == DIGEST_BINDING:
            if kind != "plain_text":
                raise SyncError("digest-binding-conflict")
            outgoing.append({"name": name, "type": "plain_text", "text": digest_text})
            replaced = True
            continue
        if kind in SECRET_TYPES:
            outgoing.append({"name": name, "type": kind})
            continue
        if "secret" in kind:
            raise SyncError("settings-incomplete")
        outgoing.append(dict(binding))
    if not replaced:
        outgoing.append({"name": DIGEST_BINDING, "type": "plain_text", "text": digest_text})
    return outgoing


def digest_document(digests):
    payload = {"v": 1, "algo": "hmac-sha256", "keys": dict(sorted(digests.items()))}
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def desired_digests(values, digest_key):
    """Digests for Worker secrets.  The HMAC key itself is not one of them."""
    if not isinstance(digest_key, str) or len(digest_key.encode("utf-8")) < 16:
        raise SyncError("digest-key-missing")
    wanted = {}
    for name, value in values.items():
        if name == DIGEST_KEY_NAME:
            continue
        wanted[name] = per_key_digest(digest_key, name, value)
    return wanted


class Transport:
    """HTTP that returns ``(status, body)`` and never raises a body into an error string."""

    def __init__(self, opener):
        self.opener = opener

    def __call__(self, method, url, token, payload, accept_status):
        headers = {
            "Authorization": "Bearer " + token,
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        }
        data = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=30) as response:
                status = response.status
                raw = response.read(BODY_LIMIT + 1)
        except urllib.error.HTTPError as error:
            raise SyncError("http-" + str(error.code)) from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise SyncError("network") from exc
        if status not in accept_status:
            raise SyncError("http-" + str(status))
        if len(raw) > BODY_LIMIT:
            raise SyncError("response-too-large")
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise SyncError("not-json") from exc


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise SyncError("redirect")


def default_opener():
    return urllib.request.build_opener(_NoRedirect)


def _query(project_id, folder):
    return urllib.parse.urlencode(
        {
            "workspaceId": project_id,
            "environment": "prod",
            "secretPath": folder,
            "viewSecretValue": "true",
            "expandSecretReferences": "false",
            "recursive": "false",
            "include_imports": "false",
        }
    )


def read_infisical(transport, token, project_id, folder, site, now):
    token = require_service_token(token)
    metadata = transport(
        "GET",
        site + "/api/v2/service-token",
        token,
        None,
        {200},
    )
    validate_service_metadata(metadata, project_id, folder, now)
    body = transport(
        "GET",
        site + "/api/v3/secrets/raw?" + _query(project_id, folder),
        token,
        None,
        {200},
    )
    return parse_infisical_folder(body, project_id, folder)


def _account(account):
    if not isinstance(account, str) or not ACCOUNT_RE.fullmatch(account):
        raise SyncError("account-invalid")
    return account


def _worker_base(account, worker):
    if not WORKER_RE.fullmatch(worker):
        raise SyncError("unknown-worker")
    return (
        CLOUDFLARE_BASE
        + "/accounts/"
        + _account(account)
        + "/workers/scripts/"
        + urllib.parse.quote(worker, safe="")
    )


def read_worker(transport, token, account, worker):
    token = _cloudflare_token(token)
    base = _worker_base(account, worker)
    names = parse_worker_secret_names(transport("GET", base + "/secrets", token, None, {200}))
    settings = transport("GET", base + "/settings", token, None, {200})
    stored = parse_stored_digests(settings)
    return names, stored, settings


def _cloudflare_token(token):
    if not isinstance(token, str) or len(token) < 20 or any(ch.isspace() for ch in token):
        raise SyncError("cloudflare-token-required")
    return token


def put_secret(transport, token, account, worker, name, value):
    if not NAME_RE.fullmatch(name):
        raise SyncError("invalid-secret-name")
    base = _worker_base(account, worker) + "/secrets"
    body = transport(
        "PUT",
        base,
        token,
        {"name": name, "text": value, "type": "secret_text"},
        {200, 201},
    )
    if not isinstance(body, dict) or body.get("success") is not True:
        raise SyncError("put-failed " + name)


def write_digest_binding(transport, token, account, worker, settings, digest_text):
    bindings = bindings_for_digest_update(_settings_bindings(settings), digest_text)
    base = _worker_base(account, worker) + "/settings"
    body = transport("PATCH", base, token, {"settings": {"bindings": bindings}}, {200})
    if not isinstance(body, dict) or body.get("success") is not True:
        raise SyncError("digest-not-confirmed")
    confirmed = parse_stored_digests(transport("GET", base, token, None, {200}))
    expected = json.loads(digest_text)["keys"]
    if confirmed != expected:
        raise SyncError("digest-not-confirmed")


def drift(diff):
    return bool(diff["missing_on_worker"] or diff["only_on_worker"] or diff["differ"])


def sync_worker(
    worker,
    *,
    env,
    account,
    apply_flag,
    strict,
    transport,
    guard,
    now,
    site=INFISICAL_SITE,
):
    """Returns ``(report, exit_code)``.  Values stay in this process."""
    folders = load_folder_map()
    if worker not in folders:
        raise SyncError("unknown-worker")
    spec = folders[worker]
    allowlist = load_allowlist(worker)
    token = require_service_token(env.get(spec["token_env"]))
    values = read_infisical(transport, token, spec["project_id"], spec["folder"], site, now)
    if DIGEST_KEY_NAME not in values:
        raise SyncError("digest-key-missing")
    digest_key = values[DIGEST_KEY_NAME]
    computed = desired_digests(values, digest_key)
    cf = _cloudflare_token(env.get("CLOUDFLARE_API_TOKEN"))
    names, stored, _settings = read_worker(transport, cf, account, worker)
    diff = classify(computed, names, stored, allowlist)
    report = mask_text(render_report(diff), [*values.values(), token, cf])
    if not apply_enabled(env, apply_flag):
        code = 3 if strict and drift(diff) else 0
        return report + "apply: off\n", code
    if guard(worker):
        raise SyncError("guard-tripped")
    chosen = puts_for_apply(diff, allowlist)
    for name in chosen:
        put_secret(transport, cf, account, worker, name, values[name])
    if chosen:
        # Re-read after the puts.  A settings patch built from the pre-put
        # binding list would omit a secret that was missing and then drop it.
        names_after, _stored_after, fresh = read_worker(transport, cf, account, worker)
        if any(name not in names_after for name in chosen):
            raise SyncError("put-unconfirmed")
        recorded = {}
        for name in allowlist:
            previous = None if stored is None else stored.get(name)
            if name in computed and (name in chosen or previous == computed[name]):
                recorded[name] = computed[name]
        write_digest_binding(transport, cf, account, worker, fresh, digest_document(recorded))
    lines = report + "apply: wrote " + str(len(chosen)) + "\n"
    for name in chosen:
        lines += "apply: wrote " + name + "\n"
    return mask_text(lines, [*values.values(), token, cf]), 0


def cli(argv, *, account, guard, env=None, transport=None, now=None, stdout=None, stderr=None):
    env = os.environ if env is None else env
    stdout = sys.stdout if stdout is None else stdout
    stderr = sys.stderr if stderr is None else stderr
    now = now or datetime.now(UTC)
    apply_flag = False
    strict = False
    worker = None
    for arg in argv:
        if arg == "--apply":
            apply_flag = True
            continue
        if arg == "--strict":
            strict = True
            continue
        if arg.startswith("-") or worker is not None:
            stderr.write("secret-sync: usage\n")
            return 2
        worker = arg
    if worker is None:
        stderr.write("secret-sync: usage\n")
        return 2
    if transport is None:
        transport = Transport(default_opener())
    try:
        report, code = sync_worker(
            worker,
            env=env,
            account=account,
            apply_flag=apply_flag,
            strict=strict,
            transport=transport,
            guard=guard,
            now=now,
        )
    except SyncError as error:
        # No value is on the exception.  Mask anyway, in case a future code path
        # interpolates one, using whatever credentials this process was given.
        secret_values = [env.get("CLOUDFLARE_API_TOKEN") or ""]
        try:
            token_env = load_folder_map()[worker]["token_env"]
            secret_values.append(env.get(token_env) or "")
        except (SyncError, KeyError):
            pass
        stderr.write("secret-sync: " + mask_text(str(error), secret_values) + "\n")
        return 1
    stdout.write(report)
    return code
