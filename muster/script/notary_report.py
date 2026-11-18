"""Read Apple's notarization answers for release_build.sh, keeping only allow-listed fields.

    notary_report.py submission   stdin: `notarytool submit --output-format json`
                                  prints "<id> <status>", or exits 1 when the shape is wrong
    notary_report.py log          stdin: `notarytool log` JSON
                                  prints the status, its summary and each issue's severity,
                                  path and message, and nothing else
    notary_report.py failure      stdin: whatever a failed `notarytool submit` wrote
                                  prints one line naming the kind of failure, and the HTTP
                                  status as digits when Apple gave one, and nothing else

The submission id goes back to notarytool as an argument, so it must be a UUID:  a value
that is not one, or one that starts with `-`, never reaches a child process.

A failed submission's own text is never printed:  only a fixed sentence chosen from the
list in FAILURE_KINDS, so a key id, an issuer or a path can never reach the release log.
"""

import json
import re
import sys

SUBMISSION_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
STATUS = re.compile(r"^[A-Za-z][A-Za-z ]{0,39}$")
FIELD_LIMIT = 300
# Apple's answers are a few kilobytes.  Anything larger is not read at all, and a nesting
# deep enough to exhaust the parser's stack is refused like any other unreadable answer.
MAX_BYTES = 1024 * 1024


def load_bounded(text):
    """The parsed JSON object, or None when it is too large, unreadable or not an object."""
    if len(text.encode("utf-8", "replace")) > MAX_BYTES:
        return None
    try:
        value = json.loads(text)
    except (ValueError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def parse_submission(text):
    """The submission id and status, or None when the answer is not that shape."""
    answer = load_bounded(text)
    if answer is None:
        return None
    submission, status = answer.get("id"), answer.get("status")
    if not isinstance(submission, str) or not SUBMISSION_ID.match(submission.lower()):
        return None
    if not isinstance(status, str) or not STATUS.match(status):
        return None
    return submission.lower(), status


def clip(value):
    text = " ".join(str(value).split())
    return text[:FIELD_LIMIT]


def text_field(container, name):
    """A string field, clipped, or None when it is absent or not a string."""
    value = container.get(name)
    return clip(value) if isinstance(value, str) else None


def summarize_log(text):
    """The allow-listed lines of a notarization log, or one line saying it could not be read.

    Only string fields are shown.  A field that is present with another type is named as
    not readable rather than printed, so a malformed log never reads as a clean one.
    """
    log = load_bounded(text)
    if log is None:
        return ["The notarization log could not be read."]
    lines = ["status: %s" % (text_field(log, "status") or "not readable")]
    if "statusSummary" in log:
        lines.append("summary: %s" % (text_field(log, "statusSummary") or "not readable"))
    issues = log.get("issues", [])
    if not isinstance(issues, list):
        lines.append("issues: not readable")
        issues = []
    for issue in issues:
        if not isinstance(issue, dict):
            lines.append("issue: not readable")
            continue
        severity = text_field(issue, "severity") or "unknown"
        path = text_field(issue, "path") or "no path"
        message = text_field(issue, "message") or "no message"
        lines.append("issue: %s %s: %s" % (severity, path, message))
    return lines


# Each kind is (name, the words that mark it, the sentence printed).  The first match wins,
# so the specific answers come before the general ones.  Only the sentence is printed.
FAILURE_KINDS = (
    ("agreement",
     ("required agreement", "agreement is missing", "agreements_missing", "agreement has expired",
      "in-effect agreement"),
     "Apple refused the key because a required Apple Developer agreement is missing or expired.  "
     "The account holder accepts it at developer.apple.com/account."),
    ("not_authorized",
     ("http status code: 401", "status code 401", "unable to authenticate", "not_authorized",
      "invalid credentials", "authentication credentials are missing or invalid"),
     "Apple did not accept the API key (401):  the key is revoked, or the key id, issuer and key "
     "file do not belong together."),
    ("forbidden",
     ("http status code: 403", "status code 403", "forbidden"),
     "Apple accepted the key but refused the request (403):  a missing or expired agreement, "
     "or a key role that may not notarize."),
    ("key_unreadable",
     ("no such file", "invalid private key", "could not read the private key", "failed to load",
      "unable to read", "no keychain password item"),
     "notarytool could not read the API key file on the runner."),
    ("apple_unavailable",
     ("http status code: 500", "http status code: 502", "http status code: 503",
      "http status code: 504", "service unavailable", "internal server error"),
     "Apple's notary service answered with a server error."),
    ("network",
     ("nsurlerrordomain", "timed out", "network connection was lost", "could not connect",
      "internet connection appears to be offline"),
     "The runner could not reach Apple's notary service."),
)
HTTP_STATUS = re.compile(r"(?:http status code|status code)[:\s]+([1-5][0-9]{2})\b", re.IGNORECASE)


def classify_failure(text):
    """The kind name, its sentence and Apple's HTTP status (digits or None) for a failed submit."""
    if len(text.encode("utf-8", "replace")) > MAX_BYTES:
        text = text[:MAX_BYTES]
    lowered = text.lower()
    match = HTTP_STATUS.search(text)
    status = match.group(1) if match else None
    for name, marks, sentence in FAILURE_KINDS:
        if any(mark in lowered for mark in marks):
            return name, sentence, status
    return "unknown", "notarytool gave no answer this script recognizes.", status


def main(argv, stdin):
    if len(argv) != 2 or argv[1] not in ("submission", "log", "failure"):
        print("Usage: notary_report.py [submission|log|failure]", file=sys.stderr)
        return 2
    text = stdin.read()
    if argv[1] == "failure":
        name, sentence, status = classify_failure(text)
        print("notarytool failure: %s%s.  %s" % (name, " (HTTP %s)" % status if status else "", sentence))
        return 0
    if argv[1] == "submission":
        parsed = parse_submission(text)
        if parsed is None:
            return 1
        print("%s %s" % parsed)
        return 0
    print("\n".join(summarize_log(text)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv, sys.stdin))
