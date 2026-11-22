"""Durable supervisor identity/start-guard journal, fixed private directory."""

import json
import os
from pathlib import Path

from .deploy_guard import start_decision
from .deploy_transaction import TransactionError, atomic, read_regular


class Journal:
    def __init__(self, directory, inventory, current_hashes, observe_executor, boot_id):
        self.path = Path(directory) / "supervisor.json"
        self.inventory = inventory
        self.current_hashes = current_hashes
        self.observe_executor = observe_executor
        self.boot_id = boot_id

    def write(self, doc):
        atomic(
            self.path,
            json.dumps(doc, sort_keys=True).encode(),
            {"uid": os.getuid(), "gid": os.getgid(), "mode": 0o600},
        )

    def read(self):
        doc = json.loads(read_regular(self.path)[0])
        if (
            not isinstance(doc, dict)
            or set(doc) != {"phase", "rows", "executor", "deadline", "guard"}
            or doc["rows"] != self.inventory
        ):
            raise TransactionError("supervisor journal differs from pinned inventory")
        return doc

    def arm(self, rows, executor, deadline):
        if rows != self.inventory or self.path.exists() or self.path.is_symlink():
            raise TransactionError("journal exists or inventory changed")
        self.write(
            {
                "phase": "prepared",
                "rows": rows,
                "executor": executor,
                "deadline": deadline,
                "guard": False,
            }
        )

    def guard_installed(self):
        doc = self.read()
        if doc["phase"] != "prepared":
            raise TransactionError("guard phase mismatch")
        doc["guard"] = True
        self.write(doc)

    def allow_new(self, rows, executor):
        doc = self.read()
        if rows != self.inventory or executor != doc["executor"] or not doc["guard"]:
            raise TransactionError("new start identity or inventory mismatch")
        doc["phase"] = "applied"
        self.write(doc)

    def allow_old(self, rows):
        doc = self.read()
        if rows != self.inventory:
            raise TransactionError("old start inventory mismatch")
        doc["phase"] = "restored"
        self.write(doc)

    def qualified(self):
        doc = self.read()
        if doc["phase"] != "applied":
            raise TransactionError("not applied")
        doc["phase"] = "committed"
        self.write(doc)

    def aborted_without_writes(self):
        doc = self.read()
        if doc["phase"] != "prepared":
            raise TransactionError("cannot abort changed image")
        doc["phase"] = "restored"
        self.write(doc)

    def decision(self, old_hashes, new_hashes, now):
        doc = self.read()
        observed = self.observe_executor(doc["executor"])
        if doc["phase"] == "applied" and now >= doc["deadline"]:
            observed = None
        return start_decision(
            doc["phase"],
            doc["executor"],
            observed,
            self.boot_id(),
            self.current_hashes(),
            old_hashes,
            new_hashes,
        )

    def safe_to_finish(self):
        return self.read()["phase"] in {"committed", "restored"}

    def finished(self):
        # Preserve audit journal rather than removing crash evidence. A later
        # transaction uses a new reviewed directory, never overwrites this one.
        if not self.safe_to_finish():
            raise TransactionError("unqualified finish")
