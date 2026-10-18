"""``python3 -m fleet_secrets`` (run from scripts/) is the same as the ``fleet-secrets`` command."""
import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
