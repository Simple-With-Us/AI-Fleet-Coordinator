#!/usr/bin/env python3
"""Audit one existing alert reader binding without reading secret values."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import socket
import ssl
import sys
import urllib.error
import urllib.request

ENDPOINT = "https://app.infisical.com/api/v2/service-token"
MAX_BYTES = 128 * 1024
TIMEOUT_SECONDS = 10
TOKEN_KEY = "AFC_ALERT_WORKERS_INFISICAL_TOKEN"
PROJECT_KEY = "INFISICAL_FLEET_OPS_PROJECT_ID"
UUID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", re.ASCII)
SHA = re.compile(r"[0-9a-f]{40}", re.ASCII)
TOKEN = re.compile(r"st\.[A-Za-z0-9._~-]{17,2045}", re.ASCII)
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", re.ASCII)
EXPIRY = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})", re.ASCII
)
REFUSALS = frozenset(
    {
        "source_unverified",
        "missing_delivery",
        "invalid_binding",
        "redirect_refused",
        "metadata_http_error",
        "invalid_metadata",
        "response_too_large",
        "request_timeout",
        "metadata_unavailable",
        "invalid_json",
        "grant_mismatch",
        "invalid_expiry",
        "audit_failed",
    }
)


class Refusal(Exception):
    """Carry only a fixed local enum, never provider text."""

    def __init__(self, code):
        self.code = code if isinstance(code, str) and code in REFUSALS else "audit_failed"
        super().__init__(self.code)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise Refusal("redirect_refused")


def source_verified(env):
    expected = env.get("AUDIT_EXPECTED_SOURCE_SHA", "")
    return (
        isinstance(expected, str)
        and SHA.fullmatch(expected) is not None
        and env.get("GITHUB_SHA") == expected
        and env.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
        and env.get("GITHUB_REPOSITORY") == "Simple-With-Us/AI-Fleet-Coordinator"
        and env.get("GITHUB_REF") == "refs/heads/main"
    )


def receipt(env):
    def delivered(key):
        value = env.get(key)
        return isinstance(value, str) and bool(value.strip())

    return {
        "schema": "afc-alert-binding-audit-v1",
        "status": "audit_failed",
        "source_verified": source_verified(env),
        "token_delivered": delivered(TOKEN_KEY),
        "project_delivered": delivered(PROJECT_KEY),
        "grant_verified": False,
        "purpose_verified": False,
        "expiry_verified": False,
        "nonexpiring": False,
        "token_id_sha256": "",
        "token_name_sha256": "",
        "project_id_sha256": "",
    }


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise Refusal("invalid_json")
        result[key] = value
    return result


def reject_constant(_value):
    raise Refusal("invalid_json")


def self_metadata(token, opener):
    request = urllib.request.Request(
        ENDPOINT,
        headers={
            "Authorization": "Bearer " + token,
            "Accept": "application/json",
            "Accept-Encoding": "identity",
        },
        method="GET",
    )
    try:
        with opener.open(request, timeout=TIMEOUT_SECONDS) as response:
            if response.status != 200:
                raise Refusal("metadata_http_error")
            if response.headers.get_content_type() != "application/json":
                raise Refusal("invalid_metadata")
            length = response.headers.get("Content-Length")
            if length is not None:
                if not re.fullmatch(r"[0-9]{1,12}", length):
                    raise Refusal("invalid_metadata")
                if int(length) > MAX_BYTES:
                    raise Refusal("response_too_large")
            body = response.read(MAX_BYTES + 1)
            if len(body) > MAX_BYTES:
                raise Refusal("response_too_large")
    except urllib.error.HTTPError as error:
        code = error.code
        error.close()
        raise Refusal("redirect_refused" if 300 <= code < 400 else "metadata_http_error") from None
    except urllib.error.URLError as error:
        code = (
            "request_timeout"
            if isinstance(error.reason, (TimeoutError, socket.timeout))
            else "metadata_unavailable"
        )
        raise Refusal(code) from None
    except TimeoutError:
        raise Refusal("request_timeout") from None
    try:
        return json.loads(
            body.decode("utf-8"), object_pairs_hook=unique_object, parse_constant=reject_constant
        )
    except (UnicodeError, ValueError, RecursionError):
        raise Refusal("invalid_json") from None


def validate_metadata(metadata, project, token, now):
    if not isinstance(metadata, dict):
        raise Refusal("invalid_metadata")
    identity = metadata.get("id")
    name = metadata.get("name")
    workspace = metadata.get("workspace")
    if (
        not isinstance(identity, str)
        or UUID.fullmatch(identity) is None
        or ("_id" in metadata and metadata["_id"] != identity)
        or not isinstance(name, str)
        or NAME.fullmatch(name) is None
        or name.startswith("st.")
        or token in name
        or not isinstance(workspace, str)
        or UUID.fullmatch(workspace) is None
    ):
        raise Refusal("invalid_metadata")
    if workspace != project or ("projectId" in metadata and metadata["projectId"] != project):
        raise Refusal("grant_mismatch")
    if metadata.get("scopes") != [{"environment": "prod", "secretPath": "/"}]:
        raise Refusal("grant_mismatch")
    if metadata.get("permissions") != ["read"]:
        raise Refusal("grant_mismatch")
    expiry = metadata.get("expiresAt")
    if expiry is None:
        return identity, name, workspace, True
    if not isinstance(expiry, str) or EXPIRY.fullmatch(expiry) is None:
        raise Refusal("invalid_expiry")
    if not expiry.endswith("Z"):
        offset_hour, offset_minute = int(expiry[-5:-3]), int(expiry[-2:])
        if offset_hour > 23 or offset_minute > 59:
            raise Refusal("invalid_expiry")
    try:
        expiry_time = dt.datetime.fromisoformat(expiry.replace("Z", "+00:00"))
        if expiry_time <= now:
            raise Refusal("invalid_expiry")
    except ValueError:
        raise Refusal("invalid_expiry") from None
    return identity, name, workspace, False


def audit(env, *, opener=None, now=None):
    result = receipt(env)
    try:
        if not result["source_verified"]:
            raise Refusal("source_unverified")
        if not result["token_delivered"] or not result["project_delivered"]:
            raise Refusal("missing_delivery")
        token, project = env[TOKEN_KEY].strip(), env[PROJECT_KEY].strip()
        if TOKEN.fullmatch(token) is None or UUID.fullmatch(project) is None:
            raise Refusal("invalid_binding")
        if opener is None:
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({}),
                NoRedirect(),
                urllib.request.HTTPSHandler(context=ssl.create_default_context()),
            )
        metadata = self_metadata(token, opener)
        identity, name, workspace, nonexpiring = validate_metadata(
            metadata,
            project,
            token,
            now or dt.datetime.now(dt.UTC),
        )
        result.update(
            status="verified",
            grant_verified=True,
            expiry_verified=True,
            nonexpiring=nonexpiring,
            token_id_sha256=hashlib.sha256(identity.encode("utf-8")).hexdigest(),
            token_name_sha256=hashlib.sha256(name.encode("utf-8")).hexdigest(),
            project_id_sha256=hashlib.sha256(workspace.encode("utf-8")).hexdigest(),
        )
    except Refusal as error:
        result["status"] = error.code
    except Exception:
        result["status"] = "audit_failed"
    return result


def main(argv=None, *, env=None, opener=None, now=None, output=None):
    argv = sys.argv[1:] if argv is None else argv
    env = os.environ if env is None else env
    result = audit(env, opener=opener, now=now) if argv == ["--audit"] else receipt(env)
    if argv != ["--audit"]:
        result["status"] = "invalid_invocation"
    print(json.dumps(result, sort_keys=True, separators=(",", ":")), file=output or sys.stdout)
    return 0 if result["status"] == "verified" else 2


if __name__ == "__main__":
    raise SystemExit(main())
