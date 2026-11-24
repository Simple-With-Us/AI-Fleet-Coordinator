"""Source-isolated actual router commit -> platform builder -> custody integration.

Only external registry/store/transport helpers are fixtures. Router _deliver_turn
and the candidate make_deliver_turn/deliver_turn_local/_deliver_prompt are real source.
"""

import ast
import dataclasses
import errno
import fcntl
import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from native_custody import fresh

ROOT = Path(__file__).parent


class Delivery:
    def __init__(self, outcome, *a, code=None, **kw):
        self.outcome, self.code = outcome, code


class Turn:
    def __init__(self, delivery, *a, **kw):
        self.delivery = delivery


@dataclasses.dataclass
class Wake:
    wake_id: str = "ra-new"
    message_id: int = 123
    credential_scope: str = "review-alerts"
    required_session: str = "sess-fixture"
    require_generation: int = 1
    require_letter: str = "II"
    picked: str = ""
    reason: str = ""
    link_note: str = ""


class Tests(unittest.TestCase):
    def test_actual_head_only_commit_reaches_driver_once(self):
        with tempfile.TemporaryDirectory(dir="/dev/shm") as tmp:
            root = Path(tmp)
            (root / "pending").mkdir()
            (root / "done").mkdir()
            target = SimpleNamespace(
                session_id="sess-fixture", host="server", letter="II", generation=1
            )
            wr = SimpleNamespace(
                Target=object,
                Wake=Wake,
                Turn=Turn,
                Delivery=Delivery,
                workspace_allowed=lambda a, b: a == b,
                turn_text=lambda *a: "fixture",
            )
            calls = []

            def driver(*a, **kw):
                self.assertTrue(kw["fresh"])
                self.assertTrue(fresh(root, "ra-new", target, 99))
                calls.append(a)
                return {"outcome": "unknown"}

            g = {
                "Mapping": dict,
                "Any": object,
                "Callable": object,
                "WR": wr,
                "Runner": object,
                "subprocess": subprocess,
                "SESSION_RE": re.compile("sess-fixture"),
                "state_dir": lambda env: root,
                "_private_dir": lambda p: p.mkdir(exist_ok=True),
                "os": os,
                "Path": Path,
                "fcntl": fcntl,
                "errno": errno,
                "_tasks_row": lambda *a: ("ok", {"task_status": "running"}),
                "_session_binding": lambda *a: (
                    "ok",
                    {"id": "sess-fixture", "directory": tmp},
                ),
                "_subagent_ids": lambda *a: ("ok", set()),
                "_recheck": lambda *a: None,
                "TURN_PREFIX": "fixture",
            }
            tree = ast.parse((ROOT / "candidate-zcode_wake.py").read_text())
            names = {"_deliver_prompt", "deliver_turn_local", "make_deliver_turn"}
            nodes = [
                n
                for n in tree.body
                if isinstance(n, ast.FunctionDef) and n.name in names
            ]
            for n in nodes:
                n.returns = None
                for arg in n.args.args + n.args.kwonlyargs:
                    arg.annotation = None
            exec(
                compile(
                    ast.Module(body=nodes, type_ignores=[]), "<actual-builder>", "exec"
                ),
                g,
            )
            platform = SimpleNamespace(
                deliver_turn=g["make_deliver_turn"](
                    {"HOME": tmp},
                    {"workspace": tmp, "native_delivery_review_only": True},
                    native_driver=driver,
                    native_is_fresh=lambda t, k: fresh(root, k, t, 99),
                )
            )
            rtree = ast.parse((ROOT / "wake_router_base.py").read_text())
            router = next(
                n
                for n in rtree.body
                if isinstance(n, ast.ClassDef) and n.name == "Router"
            )
            method = next(
                n
                for n in router.body
                if isinstance(n, ast.FunctionDef) and n.name == "_deliver_turn"
            )
            method.returns = None
            for arg in method.args.args:
                arg.annotation = None
            rg = {
                "replace": dataclasses.replace,
                "Turn": Turn,
                "Delivery": Delivery,
                "TURN_BAD_ANSWER": "bad",
            }
            exec(
                compile(
                    ast.Module(body=[method], type_ignores=[]),
                    "<actual-router>",
                    "exec",
                ),
                rg,
            )
            wake = Wake()
            entry = {
                "wake": dataclasses.asdict(wake),
                "attempts": 0,
                "accepted_at": 100,
                "pick": {"target": vars(target)},
            }

            def save(e):
                p = root / "pending/ra-new.json"
                p.write_text(json.dumps(e))
                p.chmod(0o600)
                return True

            owner = SimpleNamespace(
                platform=platform,
                _save=save,
                _give_up_slot=lambda: None,
                _settle_turn=lambda *a: None,
                _abandon=lambda *a: None,
                _told=lambda r: r,
            )
            rg["_deliver_turn"](
                owner,
                entry,
                SimpleNamespace(target=target, picked="", reason="", link_note=""),
                wake,
                [],
                "t-current",
            )
            self.assertEqual(len(calls), 1)
            self.assertEqual(
                entry["turn"], {"id": "t-current", "riders": [], "message_ids": [123]}
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
