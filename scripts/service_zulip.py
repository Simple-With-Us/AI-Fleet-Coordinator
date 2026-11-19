#!/usr/bin/env python3
"""Service-only Zulip transport. No seat wrappers, credential fallback or audience override."""

import argparse
import base64
import importlib.util
import json
import os
import stat
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ORIGIN = "https://simplewithus.zulipchat.com"
# Existing dedicated Watchdog bot, verified /users/me in the restoration investigation.
SERVICE_EMAIL = "watchdog-bot@simplewithus.zulipchat.com"
SERVICE_USER_ID = 1222575
SERVICES = {
    "doctor": ("FLEET_DOCTOR", "Work-In-Progress", "fleet tools doctor"),
    "testflight": ("IOS_TF_FRESHNESS", "Work-In-Progress", "AFC testflight-freshness"),
    "sim-reaper": ("SIM_REAPER", "Alerts", "sim-idle-reaper"),
}


class Refused(Exception):
    pass


class Unknown(Exception):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise Unknown("redirect outcome uncertain")


def agent_emails():
    registry = Path(__file__).resolve().with_name("fleet-apps.json")
    if not registry.exists():
        registry = Path(__file__).resolve().parent.parent / "fleet-apps.json"
    try:
        data = json.loads(registry.read_text())
    except Exception:
        raise Refused("agent registry unavailable") from None
    found = set()

    def walk(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key == "zulipBotEmail" and isinstance(item, str):
                    found.add(item.casefold())
                elif isinstance(item, (dict, list)):
                    walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(data)
    if not found:
        raise Refused("agent registry empty")
    return found


def protected_binding(service, env):
    """Runtime-only Mac/user binding. Fixed path, no executable config or secret discovery."""
    prefix = SERVICES[service][0]
    names = [prefix + suffix for suffix in ("_ZULIP_EMAIL", "_ZULIP_API_KEY", "_ZULIP_USER_ID")]
    if any(env.get(name) for name in names):
        return env  # A partial injected binding fails; never mix custody sources.
    path = Path.home() / ".config" / "fleet-services" / (service + ".json")
    try:
        for directory in (path.parent, path.parent.parent):
            info = directory.lstat()
            if (
                not stat.S_ISDIR(info.st_mode)
                or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) & 0o022
            ):
                raise Refused("unsafe binding directory")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
        with os.fdopen(fd, "r") as stream:
            info = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_size > 16384
            ):
                raise Refused("unsafe service binding")
            data = json.load(stream)
        if not isinstance(data, dict) or set(data) != {"email", "api_key", "user_id"}:
            raise Refused("invalid service binding")
        if (
            not isinstance(data["email"], str)
            or not isinstance(data["api_key"], str)
            or type(data["user_id"]) is not int
        ):
            raise Refused("invalid service binding")
        return dict(env, **dict(zip(names, [data["email"], data["api_key"], str(data["user_id"])])))
    except Exception:
        raise Refused("service binding unavailable; staged only") from None


def binding(service, env):
    env = protected_binding(service, env)
    prefix, channel, topic = SERVICES[service]
    email = env.get(prefix + "_ZULIP_EMAIL", "")
    key = env.get(prefix + "_ZULIP_API_KEY", "")
    raw_id = env.get(prefix + "_ZULIP_USER_ID", "")
    if (
        not email.endswith("@simplewithus.zulipchat.com")
        or "\n" in email
        or ":" in email
        or not key
        or not raw_id.isdecimal()
        or int(raw_id) != SERVICE_USER_ID
        or email != SERVICE_EMAIL
    ):
        raise Refused("dedicated service binding missing")
    if email.casefold() in agent_emails():
        raise Refused("agent identity forbidden")
    return email, key, int(raw_id), channel, topic


def send(service, content, message_id=None, env=None, request=None):
    email, key, user_id, channel, topic = binding(service, os.environ if env is None else env)
    # Preserve the existing CLI's outbound secret check without changing its patterns.
    try:
        scanner_path = Path(__file__).with_name("simple_tools") / "secretscan.py"
        spec = importlib.util.spec_from_file_location("service_secret_check", scanner_path)
        scanner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(scanner)
        if scanner.scan(content, known=[key]):
            raise Refused("message content refused")
    except Exception:
        raise Refused("message content check refused") from None
    opener = urllib.request.build_opener(NoRedirect())

    def http(method, path, fields=None):
        headers = {
            "Authorization": "Basic " + base64.b64encode((email + ":" + key).encode()).decode(),
            "User-Agent": "fleet-service-zulip/1",
        }
        body = None
        if fields is not None:
            body = urllib.parse.urlencode(fields).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        req = urllib.request.Request(ORIGIN + path, data=body, headers=headers, method=method)
        try:
            with opener.open(req, timeout=20) as response:
                data = json.loads(response.read(1024 * 1024))
        except Refused:
            raise
        except urllib.error.HTTPError as exc:
            if 400 <= exc.code < 500:
                raise Refused("service request refused") from None
            raise Unknown("service request uncertain") from None
        except Exception:
            raise Unknown("service request uncertain") from None
        if not isinstance(data, dict) or data.get("result") != "success":
            raise Refused("service response refused")
        return data

    request = request or http
    # Failed reads are known unsent. Never expose raw provider bodies or exceptions.
    try:
        identity = request("GET", "/api/v1/users/me")
        if (
            identity.get("result") != "success"
            or identity.get("user_id") != user_id
            or identity.get("email") != email
            or identity.get("is_bot") is not True
        ):
            raise Refused("dedicated service identity mismatch")
        if message_id is not None:
            previous = request("GET", "/api/v1/messages/" + str(message_id)).get("message", {})
            if (
                previous.get("sender_id") != user_id
                or previous.get("type") != "stream"
                or previous.get("display_recipient", "").casefold() != channel.casefold()
                or previous.get("subject") != topic
            ):
                raise Refused("edit ownership or audience mismatch")
    except Exception:
        raise Refused("service preflight refused") from None
    if message_id is not None:
        result = request("PATCH", "/api/v1/messages/" + str(message_id), {"content": content})
        return {"id": message_id, "edited": True}
    result = request(
        "POST",
        "/api/v1/messages",
        {"type": "stream", "to": channel, "topic": topic, "content": content},
    )
    if type(result.get("id")) is not int or result["id"] <= 0:
        raise Unknown("send receipt missing")
    return {"id": result["id"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--service", required=True, choices=SERVICES)
    parser.add_argument("action", choices=["post", "edit", "status"])
    parser.add_argument("--id", type=int)
    parser.add_argument("--body-file", type=Path)
    parser.add_argument("body", nargs="?")
    args = parser.parse_args()
    if args.action == "status":
        try:
            binding(args.service, os.environ)
            print(
                json.dumps(
                    {
                        "service": args.service,
                        "state": "bound_unverified",
                        "delivery_verified": False,
                    }
                )
            )
            return 0
        except Refused:
            print(
                json.dumps(
                    {"service": args.service, "state": "staged_unbound", "delivery_verified": False}
                )
            )
            return 3
    if (args.action == "edit") != (args.id is not None) or (args.id is not None and args.id <= 0):
        parser.error("edit needs a positive message id; post takes no id")
    try:
        content = args.body_file.read_text() if args.body_file else args.body
        if not content or len(content) > 10000:
            raise Refused("message size refused")
        print(json.dumps(send(args.service, content, args.id)))
        return 0
    except Refused:
        print("service sender refused", file=sys.stderr)
        return 3
    except Exception:
        print("service sender outcome unknown", file=sys.stderr)
        return 6


if __name__ == "__main__":
    sys.exit(main())
