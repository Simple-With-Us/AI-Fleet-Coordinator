"""The test harness:  a temp world with a fake `board` and a fake `agent-sync` on PATH.

    world = World(self)
    code, out, err = world.run("file", "--by", "CLAUDE", ...)
    world.calls("board")            # every call a fake received, with argv, seat and file contents
    world.zulip()["messages"]       # what was posted

`run` calls `needs_owner.cli.main` in process with a synthetic environment (the test's own, never the
real one), so the children it starts are the fakes and nothing can reach Linear or Zulip.
"""
from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from typing import NamedTuple

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from needs_owner import cli  # noqa: E402

REALM_LINK = "https://simplewithus.zulipchat.com/#narrow/channel/12-agent-sync/topic/%s/near/%d"


class Run(NamedTuple):
    code: int
    out: str
    err: str

    def json(self):
        return json.loads(self.out)


class World:
    def __init__(self, test: unittest.TestCase) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="nowner-test-"))
        test.addCleanup(shutil.rmtree, self.root, True)
        self.bin = self.root / "bin"
        self.state = self.root / "state"
        self.home = self.root / "home"
        for d in (self.bin, self.state, self.home):
            d.mkdir()
        self._launcher("board", HERE / "fake_board.py")
        self._launcher("agent-sync", HERE / "fake_agent_sync.py")

    def _launcher(self, name: str, script: Path) -> None:
        path = self.bin / name
        path.write_text("#!%s\nimport runpy, sys\nsys.argv[0] = %r\nrunpy.run_path(%r, run_name='__main__')\n" % (
            sys.executable, str(script), str(script)))
        path.chmod(0o755)

    def remove_tool(self, name: str) -> None:
        (self.bin / name).unlink()

    # ---- running -----------------------------------------------------------------------------
    def env(self, **extra: str) -> dict:
        env = {"PATH": "%s:/usr/bin:/bin" % self.bin, "HOME": str(self.home), "FAKE_STATE_DIR": str(self.state),
               "FAKE_SCRIPTS_DIR": str(SCRIPTS), "AGENT_SEAT": "CLAUDE", "PYTHONDONTWRITEBYTECODE": "1"}
        env.update(extra)
        return {k: v for k, v in env.items() if v is not None}

    def run(self, *argv: str, env: dict | None = None) -> Run:
        out, err = io.StringIO(), io.StringIO()
        try:
            code = cli.main(list(argv), environ=env if env is not None else self.env(), out=out, err=err)
        except SystemExit as exc:  # argparse refusing a flag
            code = exc.code if isinstance(exc.code, int) else 2
        return Run(code, out.getvalue(), err.getvalue())

    def file(self, question: str, *options: str, seat: str = "CLAUDE", extra: tuple = (), env: dict | None = None,
             app: str = "AFC") -> Run:
        args = ["file", "--by", seat, "--app", app, "--question", question, "--json"]
        for option in options or ("Yes", "No"):
            args += ["--option", option]
        return self.run(*args, *extra, env=env)

    # ---- the fakes' records ------------------------------------------------------------------
    def calls(self, tool: str | None = None) -> list[dict]:
        path = self.state / "calls.jsonl"
        if not path.exists():
            return []
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        return [r for r in rows if tool is None or r["tool"] == tool]

    def verb_calls(self, tool: str, verb: str) -> list[dict]:
        return [c for c in self.calls(tool) if verb in c["argv"][:1] or (c["argv"] and c["argv"][0] == verb)]

    def board_state(self) -> dict:
        path = self.state / "board.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"next": 300, "issues": {}}

    def save_board(self, world: dict) -> None:
        (self.state / "board.json").write_text(json.dumps(world), encoding="utf-8")

    def zulip(self) -> dict:
        path = self.state / "zulip.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"next": 630000000, "messages": [], "post_mode": "ok",
                                                                                   "no_credential": []}

    def set_zulip(self, **changes) -> None:
        world = self.zulip()
        world.update(changes)
        (self.state / "zulip.json").write_text(json.dumps(world), encoding="utf-8")

    def issue(self, ident: str) -> dict:
        return self.board_state()["issues"][ident]

    def comments(self, ident: str) -> list[str]:
        return [c["body"] for c in self.issue(ident)["comments"]]

    # ---- seeding and editing the board -------------------------------------------------------
    def seed_issue(self, title: str, *, labels: tuple = ("Kind/agent-report", "needs-owner"), reporter: str | None = "CLAUDE",
                   body: str = "Tonight's decision, filed by hand.", state: str = "Todo", team: str = "AFC",
                   priority: int = 3) -> str:
        """An issue as an earlier `board file` left it (the day-0 items of live step L0b), or an imported one
        (reporter None:  no seat tag line).  Returns its identifier."""
        world = self.board_state()
        ident = "%s-%d" % (team, world["next"])
        world["next"] += 1
        world["tick"] = world.get("tick", 0) + 1
        text = body
        if reporter:
            text = ("[%s\u00b7seed0001] Filed with the board CLI.\n\n%s\n\n---\n\n**Filed with the board CLI**\n\n- Reported by %s\n"
                    "- Kind: agent-report\n\n<!-- board-cli: app=%s kind=agent-report -->" % (reporter, body, reporter, team))
        nodes = []
        for key in labels:
            parent, _, name = key.rpartition("/")
            nodes.append({"id": "lbl-" + key, "name": name, "parent": {"name": parent} if parent else None})
        world["issues"][ident] = {
            "id": str(uuid.uuid5(uuid.NAMESPACE_URL, "seed/" + ident)), "identifier": ident,
            "url": "https://linear.app/simple-with-us/issue/" + ident, "title": title, "description": text,
            "priority": priority, "createdAt": "2026-10-11T07:%02d:00.000Z" % (world["tick"] % 60),
            "updatedAt": "2026-10-11T07:00:00.000Z", "state": {"name": state}, "team": {"key": team},
            "labels": {"nodes": nodes}, "attachments": {"nodes": []}, "comments": []}
        self.save_board(world)
        return ident

    def add_comment(self, ident: str, body: str) -> None:
        world = self.board_state()
        node = world["issues"][ident]
        world["tick"] = world.get("tick", 0) + 1
        node["comments"].append({"id": "seed-%d" % world["tick"], "body": body, "user": {"name": "Fleet"},
                                 "createdAt": "2026-10-11T09:%02d:00.000Z" % (world["tick"] % 60)})
        self.save_board(world)

    def close(self, ident: str, state: str = "Done", keep_label: bool = False) -> None:
        world = self.board_state()
        node = world["issues"][ident]
        node["state"] = {"name": state}
        if not keep_label:
            node["labels"]["nodes"] = [x for x in node["labels"]["nodes"] if x["name"] != "needs-owner"]
        self.save_board(world)

    def add_label(self, ident: str, key: str) -> None:
        world = self.board_state()
        node = world["issues"][ident]
        parent, _, name = key.rpartition("/")
        node["labels"]["nodes"].append({"id": "lbl-" + key, "name": name or key, "parent": {"name": parent} if parent else None})
        self.save_board(world)

    def leftover_temp_files(self) -> list[str]:
        return sorted(p for p in os.listdir(tempfile.gettempdir()) if p.startswith(".needs-owner-"))
