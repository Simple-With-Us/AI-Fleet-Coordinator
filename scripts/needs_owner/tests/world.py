"""The test harness:  a temp world with a fake `simple-board` and a fake `simple-chat` on PATH.

    world = World(self)
    code, out, err = world.run("file", "--by", "CLAUDE", ...)
    world.calls("simple-board")            # every call a fake received, with argv, seat and file contents
    world.zulip()["messages"]       # what was posted

`run` calls `needs_owner.cli.main` in process with a synthetic environment (the test's own, never the
real one), so the children it starts are the fakes and nothing can reach Linear or Zulip.
"""
from __future__ import annotations

import datetime as dt
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

REALM_LINK = "https://simplewithus.zulipchat.com/#narrow/channel/12-work-in-progress/topic/%s/near/%d"
OWNER_ID = 7  # Jay, the pinned owner in the fake realm (fake_agent_sync.OWNER_ID)
KEYCAP = {n: "003%d-20e3" % n for n in range(1, 6)}  # Zulip's emoji_code for 1️⃣ to 5️⃣


class FakeKnobs:
    """The K1 knobs as the notifier reads them (`get` and `stale`);  a test sets `values`, `stale_copy` or `fail`."""

    def __init__(self) -> None:
        self.values: dict = {"NEEDS_OWNER_NOTIFY_PAUSED": False,
                             "NEEDS_OWNER_CAPS": {"urgent_per_hour": 3, "per_hour": 6, "per_day": 30},
                             "OWNER_LINEAR_USER_ID": ""}
        self.stale_copy = False
        self.fail: Exception | None = None
        self.reads: list = []

    def get(self, name: str):
        self.reads.append(name)
        if self.fail is not None:
            raise self.fail
        return self.values[name]

    def stale(self) -> bool:
        return self.stale_copy


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
        self.tmp = self.root / "tmp"
        for d in (self.bin, self.state, self.home, self.tmp):
            d.mkdir()
        # The CLI runs in process, so its private files land wherever `tempfile` points.  Point it at this
        # world's own directory for the life of the world, so another run on the same machine cannot fail
        # this run's leak check (and this run cannot fail theirs).
        self._saved_tempdir = tempfile.tempdir
        tempfile.tempdir = str(self.tmp)
        test.addCleanup(setattr, tempfile, "tempdir", self._saved_tempdir)
        self.knobs = FakeKnobs()
        self.now = dt.datetime(2026, 10, 11, 18, 0, 0, tzinfo=dt.timezone.utc)  # well after every issue the fake board stamps
        from needs_owner import seat as _seat

        self._seat_module, self._saved_bin = _seat, _seat.MANAGED_BIN_DIR
        _seat.MANAGED_BIN_DIR = str(self.bin)  # the import-only seam:  the fake tools are the "managed" install
        test.addCleanup(setattr, _seat, "MANAGED_BIN_DIR", self._saved_bin)
        self._launcher("simple-board", HERE / "fake_board.py")
        self._launcher("simple-chat", HERE / "fake_agent_sync.py")

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
        # The harness plays the trusted launcher:  a seat binds the run to a bearer locator and the fake board
        # proves that seat (FAKE_BEARER_SEAT).  AGENT_SEAT itself is never an identity any more, so it is not
        # passed on;  no seat means no locator and the run refuses.
        seat = env.pop("AGENT_SEAT", None)
        if seat:
            env["SIMPLE_BOARD_BEARER_LOCATOR"] = "/fleet-seats:%s_SEAT_BEARER" % seat.replace("-", "_")
            env["FAKE_BEARER_SEAT"] = seat
        return {k: v for k, v in env.items() if v is not None}

    def run(self, *argv: str, env: dict | None = None) -> Run:
        out, err = io.StringIO(), io.StringIO()
        try:
            code = cli.main(list(argv), environ=env if env is not None else self.env(), out=out, err=err,
                            knobs=self.knobs, clock=lambda: self.now)
        except SystemExit as exc:  # argparse refusing a flag
            code = exc.code if isinstance(exc.code, int) else 2
        return Run(code, out.getvalue(), err.getvalue())

    def notify(self, *extra: str, env: dict | None = None) -> Run:
        """One run of the fallback notifier, as CLAUDE, with the fake knobs and the fake clock."""
        return self.run("notify", "--json", *extra, env=env)

    @property
    def notifier_dir(self) -> Path:
        return self.home / "apps" / "needs-owner" / "state"

    def notifier_state(self) -> dict:
        path = self.notifier_dir / "notify-state.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

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

    def add_message(self, topic: str, content: str, *, seat: str | None = None, channel: str = "work-in-progress",
                    sender_id: int | None = None, client: str | None = None, email: str | None = None,
                    reactions: list | None = None) -> int:
        """A message someone else put in a topic, after the fact.  `seat` is a bot's seat tag;  with none it
        is a person's message (`is_bot` false, no seat, as the real `read --json` prints a person), by default
        Jay (id 7, the pinned owner) from the website.  `sender_id` and `client` make another person or an API
        client.  Returns its id."""
        world = self.zulip()
        mid = world["next"]
        world["next"] += 1
        row = {"id": mid, "channel": channel, "topic": topic, "content": content, "is_bot": seat is not None}
        if seat is not None:
            row["seat"] = seat
        else:
            row["sender_id"] = OWNER_ID if sender_id is None else sender_id
        if sender_id is not None and seat is not None:
            row["sender_id"] = sender_id
        if client is not None:
            row["client"] = client
        if email is not None:
            row["email"] = email
        if reactions:
            row["reactions"] = list(reactions)
        world["messages"].append(row)
        (self.state / "zulip.json").write_text(json.dumps(world), encoding="utf-8")
        return mid

    def react(self, message_id: int, user_id: int, emoji_code: str, *, emoji_name: str = "one",
              reaction_type: str = "unicode_emoji") -> None:
        """One more reaction on a message."""
        world = self.zulip()
        row = next(m for m in world["messages"] if m["id"] == message_id)
        row.setdefault("reactions", []).append({"user_id": user_id, "emoji_code": emoji_code, "emoji_name": emoji_name,
                                                "reaction_type": reaction_type})
        (self.state / "zulip.json").write_text(json.dumps(world), encoding="utf-8")

    def unreact(self, message_id: int, user_id: int) -> None:
        world = self.zulip()
        row = next(m for m in world["messages"] if m["id"] == message_id)
        row["reactions"] = [r for r in row.get("reactions", []) if r["user_id"] != user_id]
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
            "url": "https://linear.app/test-fleet/issue/" + ident, "title": title, "description": text,
            "priority": priority, "createdAt": "2026-10-11T07:%02d:00.000Z" % (world["tick"] % 60),
            "updatedAt": "2026-10-11T07:00:00.000Z", "state": {"name": state}, "team": {"key": team},
            "labels": {"nodes": nodes}, "attachments": {"nodes": []}, "comments": []}
        self.save_board(world)
        return ident

    def add_comment(self, ident: str, body: str, *, user_name: str = "Fleet", user_id: str | None = None,
                    app: bool | None = None, at: str | None = None) -> str:
        """A comment on an issue.  By default it is the Fleet app's (no account id, so no reply can come from it);
        `user_id` and `app` make a person's or another app's.  Returns the comment id."""
        world = self.board_state()
        node = world["issues"][ident]
        world["tick"] = world.get("tick", 0) + 1
        who = {"name": user_name}
        if user_id is not None:
            who["id"] = user_id
        if app is not None:
            who["app"] = app
        cid = "seed-%d" % world["tick"]
        node["comments"].append({"id": cid, "body": body, "user": who,
                                 "createdAt": at or "2026-10-11T09:%02d:00.000Z" % (world["tick"] % 60)})
        self.save_board(world)
        return cid

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
        return sorted(p for p in os.listdir(self.tmp) if p.startswith(".needs-owner-"))
