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
alert line, so the alert line that proves the announcement path is posted by the
cron sweep, minutes later -- not by this script.  Run the script, then
check the topic again after the sweep has run.

The assertions are made against the messages this run posted, not against every
message the topic holds:  the ids present before the run are the baseline, so a
second run over a healthy ingress proves the same thing the first one did.

Secrets are read from the environment first and from `fleet-secrets` second,
and are never printed:  names and lengths only.  --cleanup deletes the one-line
alert messages this test created and leaves the standing status messages alone,
because the ingress still points at them.
"""

import argparse
import base64
import json
import os
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

# The first line of every rendered standing block, exactly as the Worker writes
# it (workers/alert-ingress/src/zulip.js, STANDING_BLOCK_HEADER).  A one-line
# alert message carries no such header, so this is what tells the two apart.
STANDING_BLOCK_HEADER = "**Status: "

# The only fields of a refusal that are ever read out of a response body.  The
# body itself is never interpolated into a message.
ALLOWED_REFUSAL_FIELDS = ("code", "request_id")

# Exit codes, as the shell reads them.
EXIT_FAILED = 1
EXIT_TIMEOUT = 124
EXIT_MISSING_EXECUTABLE = 127

# The shortest secret whose preview says anything without handing over most of
# the value.  Below this length no preview is shown at all.
MIN_PREVIEW_LENGTH = 16

# The actions the Worker answers a datadog event with.  A held incident is
# never announced by the event itself ("hold", "refresh", "discard"), and an
# incident that was already announced closes with "resolve".
ALLOWED_ACTIONS = ("hold", "refresh", "discard", "resolve")


class Failed(Exception):
    """The self-test did not prove what it claims to prove."""

    def __init__(self, message, code=EXIT_FAILED):
        super().__init__(message)
        self.code = code


def masked_preview(secret):
    """The shape the board's own preview has:  secret[:8] + "…" + secret[-4:].

    A secret too short for that shape gets no preview at all:  the shape would
    hand over most of the value.
    """
    if not isinstance(secret, str) or len(secret) < MIN_PREVIEW_LENGTH:
        return "***"
    return secret[:8] + "…" + secret[-4:]


def fs_get(name, project, path):
    """One value through `fleet-secrets get`, or None when it is absent.

    A missing binary, a timeout and a refused read are all this script's own
    failures, so they are raised as `Failed` and never as a raw traceback.
    """
    binary = shutil.which("fleet-secrets") or str(
        pathlib.Path.home() / ".local" / "bin" / "fleet-secrets"
    )
    try:
        proc = subprocess.run(
            [binary, "get", name, "--path", path, "--project", project],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=90,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise Failed(f"{name} could not be read:  fleet-secrets timed out", EXIT_TIMEOUT) from None
    except FileNotFoundError:
        raise Failed(
            f"{name} could not be read:  fleet-secrets is not installed",
            EXIT_MISSING_EXECUTABLE,
        ) from None
    except OSError as error:
        raise Failed(
            f"{name} could not be read:  fleet-secrets could not run ({type(error).__name__})"
        ) from None
    if proc.returncode == 3:
        return None
    if proc.returncode != 0:
        raise Failed(f"{name} could not be read (exit {proc.returncode})")
    try:
        return proc.stdout.decode("utf-8")
    except UnicodeDecodeError:
        raise Failed(f"{name} could not be read (unreadable output)")


def secret_get(name, project, path):
    """One credential:  the environment first, then fleet-secrets.

    The value is never printed.
    """
    value = os.environ.get(name, "").strip()
    if value:
        return value
    return fs_get(name, project, path)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """A 3xx is refused, not followed.

    `urlopen` follows a redirect by default and the `Authorization` header
    travels with it, so a redirect could carry the credential to another
    origin.  Every request that carries a credential goes through the opener
    below instead.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


OPENER = urllib.request.build_opener(NoRedirect)

# Cloudflare answers the default Python user agent with "error code: 1010"
# (the browser-fight rule), so every outbound call names itself.
USER_AGENT = "AI-Fleet-Coordinator alert-ingress selftest/1.0"


def _decode_json(raw):
    """The parsed body, or None when the body is not JSON."""
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None


def post_json(url, body, headers=None, timeout=40):
    """One POST.  Returns (status, the parsed body or None)."""
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
        with OPENER.open(request, timeout=timeout) as response:
            return response.status, _decode_json(response.read())
    except urllib.error.HTTPError as error:
        return error.code, _decode_json(error.read())
    except urllib.error.URLError as error:
        raise Failed(f"the Worker could not be reached:  {type(error.reason).__name__}") from None
    except TimeoutError:
        raise Failed("the Worker could not be reached:  the request timed out") from None


def zulip(path, email, api_key, form=None, method="GET"):
    """One Zulip API call.  Returns (status, the parsed body or None)."""
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
        with OPENER.open(request, timeout=40) as response:
            return response.status, _decode_json(response.read())
    except urllib.error.HTTPError as error:
        return error.code, _decode_json(error.read())
    except urllib.error.URLError as error:
        raise Failed(f"Zulip could not be reached:  {type(error.reason).__name__}") from None
    except TimeoutError:
        raise Failed("Zulip could not be reached:  the request timed out") from None


def refusal_detail(answer):
    """The allow-listed fields of a refusal, and nothing else."""
    fields = []
    if isinstance(answer, dict):
        for field in ALLOWED_REFUSAL_FIELDS:
            value = answer.get(field)
            if isinstance(value, (str, int, float, bool)) and value != "":
                fields.append(f"{field}={value}")
    return "  ".join(fields)


def worker_refusal(status, answer, secret, what):
    """A Worker refusal, described without its body.

    Only the HTTP status, the allow-listed fields and, for an auth failure, a
    masked preview of the credential are reported.  The response body itself
    is never interpolated into the message.
    """
    text = f"the Worker refused the {what} event (HTTP {status})"
    detail = refusal_detail(answer)
    if detail:
        text += f"  [{detail}]"
    if status in (401, 403):
        text += f"  credential={masked_preview(secret)}"
    return text


def zulip_refusal(status, answer, secret, what):
    """A Zulip refusal, described without its body."""
    text = f"Zulip refused {what} (HTTP {status})"
    if 300 <= status < 400:
        text += "  (a redirect was refused, not followed)"
    detail = refusal_detail(answer)
    if detail:
        text += f"  [{detail}]"
    if status in (401, 403):
        text += f"  credential={masked_preview(secret)}"
    return text


def worker_event(path, payload, header, secret, what):
    """One synthetic vendor event, delivered to the live Worker.

    The answer is shape-checked before anything reads it, so a Worker that
    answered with something else fails here, with a precise diagnostic, and
    not later with an attribute error.
    """
    status, answer = post_json(f"{BASE}{path}", payload, {header: secret})
    if status != 200:
        raise Failed(worker_refusal(status, answer, secret, what))
    if not isinstance(answer, dict):
        raise Failed(
            f"the Worker's answer to the {what} event was not a JSON object "
            f"(got {type(answer).__name__})"
        )
    return answer


def topic_messages(email, api_key, topic):
    """Every message in the topic, oldest last."""
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
    if status in (401, 403):
        raise Failed(zulip_refusal(status, data, api_key, f"the read of #{CHANNEL} > {topic}"))
    if status != 200:
        raise Failed(f"the topic could not be read (HTTP {status})")
    if not isinstance(data, dict):
        raise Failed(f"the topic read answered with {type(data).__name__}, not a JSON object")
    messages = data.get("messages")
    if not isinstance(messages, list):
        raise Failed("the topic read carried no message list")
    return messages


def is_standing(message):
    """A standing status block, as the Worker renders it."""
    return str(message.get("content", "")).startswith(STANDING_BLOCK_HEADER)


def standing_ids(messages, sender_email):
    """The ids of the standing blocks one vendor owns in the topic."""
    return [m["id"] for m in messages if m.get("sender_email") == sender_email and is_standing(m)]


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


def check(label, condition, detail=""):
    mark = "PASS" if condition else "FAIL"
    print(f"  [{mark}] {label}{(' -- ' + detail) if detail else ''}")
    return bool(condition)


def status_first_line(answer):
    """The first line of the standing block the Worker rewrote, or ""."""
    status = answer.get("status")
    if not isinstance(status, str) or not status:
        return ""
    return status.splitlines()[0]


def cleanup(dd_email, dd_key, bs_email, bs_key):
    """Delete the one-line alert messages, and only those.

    A standing block is the message the ingress edits in place;  deleting it
    would leave the Worker's stored message id pointing at a message that is
    gone, so every later refresh would fail.  It is left alone and reported.
    """
    messages = topic_messages(dd_email, dd_key, TEST_SERVICE)
    owners = {dd_email: dd_key, bs_email: bs_key}
    deleted = 0
    kept = []
    for message in messages:
        sender = message.get("sender_email")
        if sender not in owners:
            continue
        key = owners[sender]
        if is_standing(message):
            kept.append((sender, message["id"]))
            continue
        status, _ = zulip(f"/messages/{message['id']}", sender, key, method="DELETE")
        if status == 200:
            deleted += 1
            continue
        if status in (408, 429) or status >= 500:
            raise Failed(
                f"the delete of message {message['id']} did not answer cleanly "
                f"(HTTP {status}, retryable)  credential={masked_preview(key)}"
            )
        raise Failed(
            f"the delete of message {message['id']} was refused "
            f"(HTTP {status}, permanent)  credential={masked_preview(key)}"
        )
    print(f"Cleanup: {deleted} one-line alert message(s) deleted from #{CHANNEL} > {TEST_SERVICE}.")
    if kept:
        print("Left in place (the standing status message the ingress still points at):")
        for sender, message_id in kept:
            print(f"  id={message_id}  bot={sender}")
    else:
        print("No standing status message was left behind.")
        print(
            "  If the ingress has already fired for this topic, it still pins a "
            "message id that no longer exists:  a stale id is refused, so no fresh "
            "standing block is created.  See the cleanup runbook in "
            "workers/alert-ingress/README.md."
        )
    return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cleanup", action="store_true", help="delete the test messages")
    args = parser.parse_args()

    datadog_secret = secret_get("DATADOG_WEBHOOK_SECRET", AFC_SLUG, SECRET_PATH)
    betterstack_secret = secret_get("BETTERSTACK_WEBHOOK_SECRET", AFC_SLUG, SECRET_PATH)
    dd_email = secret_get("ZULIP_DATADOG_BOT_EMAIL", AFC_SLUG, SECRET_PATH)
    dd_key = secret_get("ZULIP_DATADOG_BOT_API_KEY", AFC_SLUG, SECRET_PATH)
    bs_email = secret_get("ZULIP_BETTERSTACK_BOT_EMAIL", AFC_SLUG, SECRET_PATH)
    bs_key = secret_get("ZULIP_BETTERSTACK_BOT_API_KEY", AFC_SLUG, SECRET_PATH)
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
        return cleanup(dd_email, dd_key, bs_email, bs_key)

    # The baseline:  every message the topic holds before this run's own
    # events.  A healthy ingress edits its standing message in place, so a
    # second run over the same topic has to find the same standing message
    # and must not count what an earlier run posted.
    baseline = topic_messages(dd_email, dd_key, TEST_SERVICE)
    baseline_ids = {message["id"] for message in baseline}

    print("\nA. a flapping monitor produces ONE standing message")
    events = [
        ("Triggered", 999001, "flap", "Selftest flapping monitor"),
        ("Triggered", 999001, "flap", "Selftest flapping monitor"),
        ("Recovered", 999001, "flap", "Selftest flapping monitor"),
        ("Triggered", 999001, "flap", "Selftest flapping monitor"),
    ]
    seen = []
    standing_after_first = []
    for index, (transition, monitor, scope, title) in enumerate(events):
        answer = worker_event(
            "/webhook/datadog",
            datadog_payload(monitor, scope, transition, title),
            "X-Datadog-Token",
            datadog_secret,
            "datadog",
        )
        action = answer.get("action")
        seen.append(action)
        print(
            f"    {transition:9s} -> action={action} posted={answer.get('posted')} "
            f"standing={status_first_line(answer)}"
        )
        if index == 0:
            standing_after_first = standing_ids(
                topic_messages(dd_email, dd_key, TEST_SERVICE), dd_email
            )

    messages = topic_messages(dd_email, dd_key, TEST_SERVICE)
    new_messages = [m for m in messages if m["id"] not in baseline_ids]
    dd_standing = standing_ids(messages, dd_email)
    dd_new = [m for m in new_messages if m.get("sender_email") == dd_email]
    ok = check(
        "exactly one standing message from the datadog bot",
        len(dd_standing) == 1,
        f"ids={dd_standing}",
    )
    ok &= check(
        "the standing message kept its id through every event",
        bool(standing_after_first) and standing_after_first == dd_standing,
        f"after the first event={standing_after_first}  at the end={dd_standing}",
    )
    ok &= check(
        "the four events did not post one message each",
        len(dd_new) < len(events),
        f"{len(dd_new)} new message(s) from the datadog bot:  {[m['id'] for m in dd_new]}",
    )
    ok &= check(
        "no alert line was posted while the incident was held",
        all(action in ALLOWED_ACTIONS for action in seen),
        f"actions={seen}",
    )
    if dd_standing:
        content = next((m.get("content", "") for m in messages if m["id"] == dd_standing[0]), "")
        print("    standing block:")
        for line in content.splitlines():
            print(f"      | {line}")

    print("\nB. a second vendor gets its own standing message in the same topic")
    answer = worker_event(
        "/webhook/betterstack",
        betterstack_payload(TEST_SERVICE, "down"),
        "X-BetterStack-Token",
        betterstack_secret,
        "betterstack",
    )
    print(
        f"    down      -> action={answer.get('action')} "
        f"posted={answer.get('posted')} standing={status_first_line(answer)}"
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
    answer = worker_event(
        "/webhook/datadog",
        datadog_payload(999002, "second", "Triggered", "Selftest second monitor"),
        "X-Datadog-Token",
        datadog_secret,
        "datadog",
    )
    print(
        f"    Triggered -> action={answer.get('action')} "
        f"posted={answer.get('posted')} standing={status_first_line(answer)}"
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
        sys.exit(error.code)
