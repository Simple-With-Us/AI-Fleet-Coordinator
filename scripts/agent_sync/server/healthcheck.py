#!/usr/bin/env python3
"""Container health check for the agent-sync server instance.

Healthy (exit 0) while the listener keeps rewriting <state>/listener/status.json, which it does
every 2 seconds from its main loop.  Unhealthy (exit 1) when the file is older than 60 seconds
(AGENT_SYNC_HEALTH_MAX_AGE overrides) or missing:  a stuck main loop, a daemon that has not
started, or one that refused to start over the seat partition (that path never writes it).  It
reads only the file's modification time, never its contents.

Python 3.9+, standard library only.
"""
import os
import sys
import time

DEFAULT_MAX_AGE = 60.0


def main(env=None, now=None, out=None):
    env = os.environ if env is None else env
    out = out or sys.stdout
    root = env.get("AGENT_SYNC_STATE_DIR") or os.path.join(env.get("HOME") or os.path.expanduser("~"), ".agent-sync")
    try:
        max_age = float(env.get("AGENT_SYNC_HEALTH_MAX_AGE") or DEFAULT_MAX_AGE)
    except ValueError:
        max_age = DEFAULT_MAX_AGE
    path = os.path.join(root, "listener", "status.json")
    try:
        age = (time.time() if now is None else now) - os.stat(path).st_mtime
    except OSError:
        out.write("unhealthy: %s does not exist yet\n" % path)
        return 1
    if age > max_age:
        out.write("unhealthy: status.json is %d seconds old (limit %d)\n" % (age, max_age))
        return 1
    out.write("healthy: status.json is %d seconds old\n" % max(age, 0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
