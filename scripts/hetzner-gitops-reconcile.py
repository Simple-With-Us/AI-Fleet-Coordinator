#!/usr/bin/env python3
"""CLI entry for Hetzner GitOps G1 (AFC-369). Run with python3 -I from any cwd."""

from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from hetzner_gitops.reconcile import main

if __name__ == "__main__":
    raise SystemExit(main())
