"""Read Apple's notarization answers for release_build.sh, keeping only allow-listed fields.

    notary_report.py submission   stdin: `notarytool submit --output-format json`
                                  prints "<id> <status>", or exits 1 when the shape is wrong
    notary_report.py log          stdin: `notarytool log` JSON
                                  prints the status, its summary and each issue's severity,
                                  path and message, and nothing else

The submission id goes back to notarytool as an argument, so it must be a UUID:  a value
that is not one, or one that starts with `-`, never reaches a child process.
"""

import json
import re
import sys

SUBMISSION_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
STATUS = re.compile(r"^[A-Za-z][A-Za-z ]{0,39}$")
FIELD_LIMIT = 300


def parse_submission(text):
    """The submission id and status, or None when the answer is not that shape."""
    try:
        answer = json.loads(text)
    except ValueError:
        return None
    if not isinstance(answer, dict):
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


def summarize_log(text):
    """The allow-listed lines of a notarization log, or one line saying it could not be read."""
    try:
        log = json.loads(text)
    except ValueError:
        return ["The notarization log could not be read."]
    if not isinstance(log, dict):
        return ["The notarization log could not be read."]
    lines = ["status: %s" % clip(log.get("status", "unknown"))]
    if log.get("statusSummary"):
        lines.append("summary: %s" % clip(log["statusSummary"]))
    issues = log.get("issues")
    for issue in issues if isinstance(issues, list) else []:
        if isinstance(issue, dict):
            severity = clip(issue.get("severity", ""))
            path = clip(issue.get("path", ""))
            message = clip(issue.get("message", ""))
            lines.append("issue: %s %s: %s" % (severity, path, message))
    return lines


def main(argv, stdin):
    if len(argv) != 2 or argv[1] not in ("submission", "log"):
        print("Usage: notary_report.py [submission|log]", file=sys.stderr)
        return 2
    text = stdin.read()
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
