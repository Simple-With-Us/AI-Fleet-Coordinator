#!/usr/bin/env python3
"""Resolve Muster packages with bounded retries for explicit transient HTTP failures.

Only dependency resolution is retried. Compilation, signing and publishing never
run here. Unknown errors and checksum failures stop immediately.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import re
import signal
import subprocess
import sys
import tempfile
import time

TRANSIENT = re.compile(r"badResponseStatusCode\((?:429|500|502|503|504)\)")
INTEGRITY = re.compile(
    r"checksum|signature|certificate|authentication|unauthorized|forbidden", re.IGNORECASE
)


def resolve(command, *, attempts=3, delay=15, timeout=300, sleep=time.sleep):
    for attempt in range(1, attempts + 1):
        with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as log:
            process = subprocess.Popen(
                command, stdout=log, stderr=subprocess.STDOUT, start_new_session=True
            )
            try:
                status = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait()
                log.seek(0)
                sys.stdout.write(log.read())
                print("error: package resolution exceeded its time bound", file=sys.stderr)
                return 124
            except BaseException:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait()
                raise
            log.seek(0)
            text = log.read()
            sys.stdout.write(text)
        if status == 0:
            return 0
        if attempt == attempts or not TRANSIENT.search(text) or any(
            INTEGRITY.search(line) and (
                re.search(r"\b(?:error|fail\w*|invalid|mismatch|not|unable|denied)\b", line, re.I)
                or not re.search(r"\b(?:verified|valid|ok|succeeded|successful)\b", line, re.I)
            )
            for line in text.splitlines()
        ):
            return status if status > 0 else 1
        print(
            "warning: transient package download failure; retrying resolution (%d/%d)"
            % (attempt, attempts),
            file=sys.stderr,
        )
        sleep(delay)
    return 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--derived-data", required=True)
    args = parser.parse_args()
    for value in (args.project, args.derived_data):
        if not value or value.startswith("-"):
            parser.error("paths must be nonempty and must not start with a dash")
    previous = signal.getsignal(signal.SIGTERM)
    def terminate(signum, frame):
        raise SystemExit(128 + signum)
    signal.signal(signal.SIGTERM, terminate)
    try:
        return resolve(
            [
                "/usr/bin/xcodebuild",
                "-resolvePackageDependencies",
                "-project",
                args.project,
                "-scheme",
                "Muster",
                "-derivedDataPath",
                args.derived_data,
            ]
        )
    finally:
        signal.signal(signal.SIGTERM, previous)


if __name__ == "__main__":
    raise SystemExit(main())
