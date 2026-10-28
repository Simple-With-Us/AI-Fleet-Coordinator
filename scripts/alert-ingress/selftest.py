#!/usr/bin/env python3
"""End-to-end self-test for the alert-ingress Worker.

Proves the thing the ingress exists to prove:  an alert updates a STANDING
status message in place, per alerting source, instead of posting a new message
per event.

The test posts synthetic vendor events at the live Worker and then reads the
Zulip topic back through the vendor bot's own credentials.  It asserts:

  A. a flapping monitor (trigger, trigger, recover, trigger) produces exactly
     ONE standing message from its vendor bot, and that message keeps the same
     id through every event;
  B. a second vendor sharing the same service name gets its OWN standing
     message, and neither vendor edits the other's;
  C. a second alert in the same topic joins the same standing block, as a
     second row, rather than a second message.

The first firing event is HELD for MIN_OPEN_MS before it is announced as an
alert line, so the alert line that proves the announcement path is posted by
the cron sweep, minutes later -- not by this script.  Run the script, then
check the topic again after the sweep has run.

Secrets are read through `fleet-secrets` and never printed:  names and lengths
only.  --cleanup deletes the messages this test created.
"""

import argparse
import json
import pathlib
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://alert-ingress.jays-services.workers.dev"
ZULIP_API = "https://simplewithus.zulipchat.com/api/v1"
CHANNEL = "alerts"
TEST_SERVICE = "alert-ingress-selftest"

AFC_SLUG = "ai-fleet-coordinator"
SECRET_PATH = "/zulip"


class Failed(Exception):
    """The self-test did not prove what it claims to prove."""


def fs_get(name, project, path):
    binary = shutil.which("fleet-secrets") or str(
        pathlib.Path.home() / ".local" / "bin" / "fleet-secrets"
    )
    proc = subprocess.run(
        [binary, "get", name, "--path", path, "--project", project],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=90,
        check=False,
    )
    if proc.returncode == 3:
        return None
    if proc.returncode != 0:
        raise Failed(f"{name} could not be read (exit {proc.returncode})")
    return proc.stdout.decode("utf-8")


# Cloudflare answers the default Python user agent with "error code: 1010"
# (the browser-fight rule), so every outbound call names itself.
USER_AGENT = "AI-Fleet-Coordinator alert-ingress selftest/1.0"


def post_json(url, body, headers=None, timeout=40):
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
            **(headers or {}),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode())
    except urllib.error.HTTPError as error:
        raw = error.read().decode()
        try:
            return error.code, json.loads(raw)
        except ValueError:
            return error.code, {"raw": raw[:400]}


def zulip(path, email, api_key, form=None, method="GET"):
    import base64

    token = base64.b64encode(f"{email}:{api_key}".encode()).decode()
    body = urllib.parse.urlencode(form).encode() if form is not None else None
    request = urllib.request.Request(
        f"{ZULIP_API}{path}",
        data=body,
        headers={
            "Authorization": f"Basic {token}",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": USER_AGENT,
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=40) as response:
            return response.status, json.loads(response.read().decode())
    except urllib.error.HTTPError as error:
        raw = error.read().decode()
        try:
            return error.code, json.loads(raw)
        except ValueError:
            return error.code, {"raw": raw[:400]}


def topic_messages(email, api_key, topic):
    narrow = json.dumps(
        [
            {"operator": "stream", "operand": CHANNEL},
            {"operator": "topic", "operand": topic},
        ]
    )
    # apply_markdown=false:  the standing block is Markdown, and the default
    # answer is the rendered HTML, which does not start with "**Status: ".
    query = urllib.parse.urlencode(
        {
            "anchor": "newest",
            "num_before": 100,
            "num_after": 0,
            "narrow": narrow,
            "apply_markdown": "false",
        }
    )
    status, data = zulip(f"/messages?{query}", email, api_key)
    if status != 200:
        raise Failed(f"the topic could not be read (HTTP {status})")
    return data.get("messages", [])


def datadog_payload(monitor_id, scope, transition, title, severity="SEVERE"):
    return {
        "monitor_id": monitor_id,
        "alert_id": monitor_id,
        "alert_type": "recovered" if transition == "Recovered" else "triggered",
        "alert_transition_type": transition,
        "alert_title": title,
        "alert_transition": f"{title} {transition.lower()}",
        "priority": severity,
        "host": TEST_SERVICE,
        "scope": scope,
        "event_url": "https://app.datadoghq.com/event/jump_to?event_id=0",
    }


def betterstack_payload(name, status):
    return {
        "data": {
            "id": "bs-selftest-1",
            "type": "incident",
            "attributes": {
                "status": status,
                "check_name": name,
                "url": "https://uptime.betterstack.com/monitors/bs-selftest-1",
            },
            "relationships": {"monitor": {"data": {"id": "bs-selftest-1", "type": "monitor"}}},
        }
    }


def standing_ids(messages, sender_email):
    """The ids of the standing blocks one vendor owns in the topic."""
    return [
        m["id"]
        for m in messages
        if m.get("sender_email") == sender_email
        and str(m.get("content", "")).startswith("**Status: ")
    ]


def check(label, condition, detail=""):
    mark = "PASS" if condition else "FAIL"
    print(f"  [{mark}] {label}{(' -- ' + detail) if detail else ''}")
    return bool(condition)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cleanup", action="store_true", help="delete the test messages")
    args = parser.parse_args()

    datadog_secret = fs_get("DATADOG_WEBHOOK_SECRET", AFC_SLUG, SECRET_PATH)
    betterstack_secret = fs_get("BETTERSTACK_WEBHOOK_SECRET", AFC_SLUG, SECRET_PATH)
    dd_email = fs_get("ZULIP_DATADOG_BOT_EMAIL", AFC_SLUG, SECRET_PATH)
    dd_key = fs_get("ZULIP_DATADOG_BOT_API_KEY", AFC_SLUG, SECRET_PATH)
    bs_email = fs_get("ZULIP_BETTERSTACK_BOT_EMAIL", AFC_SLUG, SECRET_PATH)
    bs_key = fs_get("ZULIP_BETTERSTACK_BOT_API_KEY", AFC_SLUG, SECRET_PATH)
    for name, value in (
        ("DATADOG_WEBHOOK_SECRET", datadog_secret),
        ("BETTERSTACK_WEBHOOK_SECRET", betterstack_secret),
        ("ZULIP_DATADOG_BOT_EMAIL", dd_email),
        ("ZULIP_DATADOG_BOT_API_KEY", dd_key),
        ("ZULIP_BETTERSTACK_BOT_EMAIL", bs_email),
        ("ZULIP_BETTERSTACK_BOT_API_KEY", bs_key),
    ):
        if not value:
            raise Failed(f"{name} is missing from {AFC_SLUG}{SECRET_PATH}")
        print(f"  secret {name} present (len {len(value)})")

    if args.cleanup:
        deleted = 0
        for email, key in ((dd_email, dd_key), (bs_email, bs_key)):
            for message in topic_messages(email, key, TEST_SERVICE):
                status, _ = zulip(f"/messages/{message['id']}", email, key, method="DELETE")
                if status == 200:
                    deleted += 1
        print(f"Cleanup: {deleted} message(s) deleted from #{CHANNEL} > {TEST_SERVICE}.")
        return 0

    print("\nA. a flapping monitor produces ONE standing message")
    events = [
        ("Triggered", 999001, "flap", "Selftest flapping monitor"),
        ("Triggered", 999001, "flap", "Selftest flapping monitor"),
        ("Recovered", 999001, "flap", "Selftest flapping monitor"),
        ("Triggered", 999001, "flap", "Selftest flapping monitor"),
    ]
    seen = []
    for transition, monitor, scope, title in events:
        status, answer = post_json(
            f"{BASE}/webhook/datadog",
            datadog_payload(monitor, scope, transition, title),
            {"X-Datadog-Token": datadog_secret},
        )
        if status != 200:
            raise Failed(f"the Worker refused the event (HTTP {status}): {answer}")
        print(
            f"    {transition:9s} -> action={answer.get('action')} "
            f"posted={answer.get('posted')} status={answer.get('status')}"
        )
        seen.append(answer.get("action"))

    messages = topic_messages(dd_email, dd_key, TEST_SERVICE)
    dd_standing = standing_ids(messages, dd_email)
    ok = check(
        "exactly one standing message from the datadog bot",
        len(dd_standing) == 1,
        f"ids={dd_standing}",
    )
    ok &= check(
        "no alert line was posted while the incident was held",
        all(a in ("hold", "refresh", "discard") for a in seen),
        f"actions={seen}",
    )
    dd_messages = [m for m in messages if m.get("sender_email") == dd_email]
    ok &= check(
        "the datadog bot has posted exactly one message: the standing block",
        len(dd_messages) == 1,
        f"{len(dd_messages)} message(s) from the datadog bot",
    )
    ok &= check(
        "no alert line was posted for any of the four events",
        not [m for m in dd_messages if not str(m.get("content", "")).startswith("**Status: ")],
        f"{len(dd_messages)} message(s) from the datadog bot",
    )
    if dd_standing:
        content = next((m.get("content", "") for m in messages if m["id"] == dd_standing[0]), "")
        print("    standing block:")
        for line in content.splitlines():
            print(f"      | {line}")

    print("\nB. a second vendor gets its own standing message in the same topic")
    status, answer = post_json(
        f"{BASE}/webhook/betterstack",
        betterstack_payload(TEST_SERVICE, "down"),
        {"X-BetterStack-Token": betterstack_secret},
    )
    if status != 200:
        raise Failed(f"the Worker refused the betterstack event (HTTP {status}): {answer}")
    print(
        f"    down      -> action={answer.get('action')} "
        f"posted={answer.get('posted')} status={answer.get('status')}"
    )
    messages = topic_messages(dd_email, dd_key, TEST_SERVICE)
    dd_standing = standing_ids(messages, dd_email)
    bs_standing = standing_ids(messages, bs_email)
    ok &= check(
        "the datadog standing message is still exactly one",
        len(dd_standing) == 1,
        f"ids={dd_standing}",
    )
    ok &= check(
        "the betterstack bot owns a separate standing message",
        len(bs_standing) == 1,
        f"ids={bs_standing}",
    )
    ok &= check(
        "each vendor owns exactly one standing message",
        len(dd_standing) == 1 and len(bs_standing) == 1,
        f"datadog={dd_standing} betterstack={bs_standing}",
    )
    ok &= check(
        "the two vendors' standing messages are different messages",
        not (set(dd_standing) & set(bs_standing)),
        f"datadog={dd_standing} betterstack={bs_standing}",
    )

    print("\nC. a second alert joins the same standing block as a second row")
    status, answer = post_json(
        f"{BASE}/webhook/datadog",
        datadog_payload(999002, "second", "Triggered", "Selftest second monitor"),
        {"X-Datadog-Token": datadog_secret},
    )
    if status != 200:
        raise Failed(f"the Worker refused the event (HTTP {status}): {answer}")
    print(
        f"    Triggered -> action={answer.get('action')} "
        f"posted={answer.get('posted')} status={answer.get('status')}"
    )
    messages = topic_messages(dd_email, dd_key, TEST_SERVICE)
    dd_standing = standing_ids(messages, dd_email)
    ok &= check(
        "still exactly one datadog standing message",
        len(dd_standing) == 1,
        f"ids={dd_standing}",
    )
    if dd_standing:
        content = next((m.get("content", "") for m in messages if m["id"] == dd_standing[0]), "")
        rows = [
            line for line in content.splitlines() if line.startswith("●") or line.startswith("○")
        ]
        ok &= check("the standing block lists both alerts", len(rows) == 2, f"rows={len(rows)}")
        print("    standing block:")
        for line in content.splitlines():
            print(f"      | {line}")

    print("\n" + ("SELFTEST PASSED" if ok else "SELFTEST FAILED"))
    print(
        "Note: the alert LINE for a held incident is posted by the cron sweep once the\n"
        "incident ages past MIN_OPEN_MS, not by this script.  Check the topic again in\n"
        "about ten minutes: one line per announced incident, never one per event."
    )
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Failed as error:
        print(f"STOP: {error}")
        sys.exit(1)
