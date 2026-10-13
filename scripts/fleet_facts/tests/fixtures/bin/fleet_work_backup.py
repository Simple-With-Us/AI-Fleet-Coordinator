#!/usr/bin/env python3
"""A fake fleet_work_backup.py:  `health --json` prints $FF_SCENARIO's {"backup": <report>}."""
import json
import os
import sys

log = os.environ.get("FF_LOG")
if log:
    with open(log, "a") as fh:
        fh.write(json.dumps(["backup"] + sys.argv[1:]) + "\n")
print(json.dumps(json.load(open(os.environ["FF_SCENARIO"]))["backup"]))
