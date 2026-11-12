"""python3 -m seat_mcp"""

from __future__ import annotations

import sys

from .auth import load_token
from .jobs import ensure_jobs_dir
from .server import serve


def main() -> int | None:
    try:
        load_token()  # fail fast, naming the Infisical key, when the token is missing
    except RuntimeError as exc:
        sys.stderr.write("seat-mcp: %s\n" % exc)
        return 2
    ensure_jobs_dir()
    serve()
    return None


if __name__ == "__main__":
    sys.exit(main())
