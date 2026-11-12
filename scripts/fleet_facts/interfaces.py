"""Tolerant readers for the other open-loops components' status output.

  C1  `fleet-autopush status --json`          part `autopush`
  C3  `simple-chat asks status --json`         part `asks`
  C4  `~/apps/claim-expiry/state/report-latest.json`   part `report`  (a file, schema 1)
  C6  `needs-owner list --all --json`         part `needs_owner`   (--all:  the default lists only the open
                                                                   rows, and the digest needs the answered ones)
  C7  `consult status --all --json`           part `consult`

Each of those components is built by a later unit, so each reader is tolerant:  a program that is not
installed (or a report file that does not exist) is "absent", never an error;  one that runs and fails, or
prints something that is not JSON, is "failed" with a short reason;  one that prints JSON has its document
carried through under `document`, size-capped, and cleaned later by the redactor like everything else.  This
module does not interpret the documents:  the component that consumes one knows its schema.

All five are local state of one Mac, so with `no_mac` each one is absent.
"""

from __future__ import annotations

import json
from pathlib import Path

from .runner import Runner, absent, failed, home_path, ok, which

TIMEOUT_S = 60
MAX_DOCUMENT_BYTES = 1_000_000

COMMANDS: dict[str, list[str]] = {
    "autopush": ["fleet-autopush", "status", "--json"],
    "asks": ["simple-chat", "asks", "status", "--json"],
    "needs_owner": ["needs-owner", "list", "--all", "--json"],
    "consult": ["consult", "status", "--all", "--json"],
}
REPORT_PARTS = ("report",)
PARTS = tuple(COMMANDS) + REPORT_PARTS


def report_path() -> Path:
    return home_path("apps", "claim-expiry", "state", "report-latest.json")


def _document(text: str) -> dict:
    if len(text.encode("utf-8", errors="replace")) > MAX_DOCUMENT_BYTES:
        return failed("the output is larger than %d bytes" % MAX_DOCUMENT_BYTES)
    try:
        return ok({"document": json.loads(text)})
    except ValueError:
        return failed("the output is not JSON")


def read_command(run: Runner, part: str, *, no_mac: bool = False) -> dict:
    if no_mac:
        return absent("--no-mac")
    spec = COMMANDS[part]
    exe = which(spec[0])
    if exe is None:
        return absent("%s is not installed" % spec[0])
    result = run([exe, *spec[1:]], timeout=TIMEOUT_S)
    if result.reason:
        return failed("%s %s" % (spec[0], result.reason))
    if result.rc != 0:
        return failed("%s exited %s" % (spec[0], result.rc))
    return _document(result.out)


def read_report(*, no_mac: bool = False) -> dict:
    """C4's latest claim-expiry report, a regular file the sweeper writes."""
    if no_mac:
        return absent("--no-mac")
    path = report_path()
    if not path.is_file():
        return absent("no claim-expiry report yet")
    try:
        if path.stat().st_size > MAX_DOCUMENT_BYTES:
            return failed("the report is larger than %d bytes" % MAX_DOCUMENT_BYTES)
        return _document(path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return failed("the report could not be read")


def gather(part: str, run: Runner, *, no_mac: bool = False) -> dict:
    if part in REPORT_PARTS:
        return read_report(no_mac=no_mac)
    return read_command(run, part, no_mac=no_mac)
