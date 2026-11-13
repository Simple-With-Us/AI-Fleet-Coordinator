"""Run the shared post-sweep pipeline or its passive SSH receiver."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

sys.dont_write_bytecode = True
base = str(Path(__file__).resolve().parents[1])
if base not in sys.path:
    sys.path.insert(0, base)

from fleet_docs import pipeline, snapshot


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status", help="Recover a known interrupted promotion and report validated metadata only.")
    commands.add_parser("receive", help="Validate and atomically promote one private snapshot from stdin.")
    sweep = commands.add_parser("sweep", help="Preserve the authorized Git bootstrap, then refresh and replicate Docs.")
    sweep.add_argument("--mirror-command", nargs=argparse.REMAINDER, required=True)
    args = parser.parse_args(argv)
    if args.command == "sweep":
        command = args.mirror_command
        if not command or not os.path.isabs(command[0]) or not os.path.isfile(command[0]) or not os.access(command[0], os.X_OK):
            parser.error("--mirror-command requires an absolute executable")
        return pipeline.run(command)
    code_root = Path(os.environ.get("FLEET_CODE_ROOT") or Path.home() / "Code").resolve()
    root = code_root / "Docs/Generated"
    try:
        with snapshot.receiver_lock(root):
            snapshot.recover(root)
            if args.command == "status":
                value = snapshot.validate_generated(root) if root.exists() else None
                receipt = {"state": "ready", "sha256": value["sha256"], "files": len(value["files"])} if value else {"state": "empty", "sha256": None}
            else:
                receipt = snapshot.receive(root, sys.stdin.buffer)
        print(json.dumps(receipt, sort_keys=True))
        return 0
    except Exception as error:
        reason = str(error) if isinstance(error, snapshot.Refused) else "docs-receiver-refused"
        print(json.dumps({"state": "refused", "reason": reason}, sort_keys=True))
        return 1


if __name__ == "__main__":
    sys.exit(main())
