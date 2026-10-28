#!/usr/bin/env python3
"""Tests for scripts/linear-board/board against a local mock of Linear.

Run:  python3 scripts/linear-board/test_board.py   (stdlib only, about a minute)

Every CLI run is a subprocess with HOME, the credentials file, the token cache
and the id caches in a temporary directory, and the GraphQL and OAuth URLs on
127.0.0.1, so no test can reach Linear or read a real credential.  The mock
dispatches on the GraphQL operation name, serves pages of at most two nodes
(so every list must follow the cursor), and records a violation when a query
names a connection without an explicit first:, when issueUpdate sends
labelIds (which would replace every label), when issueUpdate removes a seat:
attribution label, when a label is created inside a team or a group (a flat
label is workspace level), or when anything writes an issue straight into the
Duplicate state.  It can also rate-limit single operations (limit_ops), to
stop a verb part way between two of its requests.  It knows several apps by
client id (the Fleet app plus any seat app a test adds), answers the viewer
query with the app the bearer token was minted for, and records which app made
each request, so a test can tell one seat's token from another's.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import inspect
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
CLI = HERE / "board"
RELABEL = HERE / "relabel_claims.py"
PY = sys.executable
CLIENT_ID = "mock-client-id"
CLIENT_SECRET = "mock-client-secret-value"
EXPECTED_SCOPE = "read,write,app:assignable,app:mentionable"
CONN_FIELDS = (
    "issues",
    "comments",
    "labels",
    "issueLabels",
    "teams",
    "states",
    "attachments",
    "relations",
    "inverseRelations",
    "attachmentsForURL",
)
CONN_RE = re.compile(r"\b(" + "|".join(CONN_FIELDS) + r")\b(\s*\(([^)]*)\))?\s*\{")
STATES = [
    ("Backlog", "backlog"),
    ("Todo", "unstarted"),
    ("In Progress", "started"),
    ("In Review", "started"),
    ("Done", "completed"),
    ("Deployed", "completed"),
    ("Canceled", "canceled"),
    ("Won't Fix", "canceled"),
    ("Duplicate", "duplicate"),
    ("Parked", "backlog"),
]


def linear_markdown(text: str) -> str:
    """What Linear hands back for a description it stored (observed on
    imported issues, Sat, Oct 10, 2026):  [ ] and ~ escaped outside code
    spans, and - bullets turned into * bullets.  Comment bodies come back as
    sent, so the mock stores those unchanged."""

    def esc(seg):
        return re.sub(r"([\[\]~])", r"\\\1", seg)

    out, pos = [], 0
    for m in re.finditer(r"(`+)(.+?)\1", text, re.DOTALL):
        out.append(esc(text[pos : m.start()]))
        out.append(m.group(0))
        pos = m.end()
    out.append(esc(text[pos:]))
    return re.sub(r"(?m)^(\s*)- ", r"\1* ", "".join(out))


def hy(h: str) -> str:
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"


# Board ids used by the seed data (32 hex, as on the board).
BASE_SESSION = "5e55105a-aaaa-4000-8000-00000000b00d"
SEAT_SESSION_VARS = {
    "CLAUDE": "CLAUDE_CODE_SESSION_ID",
    "CODEX": "CODEX_THREAD_ID",
    "GROK": "GROK_SESSION_ID",
    "OPENCODE": "OPENCODE_SESSION_ID",
    "CURSOR": "CURSOR_CONVERSATION_ID",
    "MM": "__MAVIS_RUNTIME_SESSION_ID",
}
TAIL = f"  (sessions: {BASE_SESSION})"
B_A = "236a2fa7e2ab4d24be03b0414750e0d8"  # OPS-10, Todo, P1, three comments
B_B = "9f3afe1a801747d6933486954c70347d"  # OPS-11, In Progress, claimed by CODEX
B_C = "4ca246e5aaaa4bbb8ccc0ddd1eee2fff"  # ST-5, Todo, P2
B_D = "51ab845251124a499cf232f6572dadf8"  # merged into CT-7 (another uuid)
D_LINEAR = "8b181573-f795-435a-98bb-bdb795903f04"
B_F = "abcdef12000040008000000000000001"  # ambiguous prefix pair
B_G = "abcdef12000040008000000000000002"
B_H = "c0ffee00111142228333444455556666"  # CT-8, imported with an external uid footer


class MockLinear:
    def __init__(self):
        self.lock = threading.Lock()
        self.page_cap = 2
        self.ops: list[str] = []
        self.violations: list[str] = []
        self.mints = 0
        self.mint_down = False
        self.request_id = ""  # a request id the token endpoint's answer carries, if any
        self.scopes: list[str] = []
        self.tokens: set[str] = set()
        self.ratelimit400 = 0
        self.limit_ops: dict = {}  # operation name -> requests to rate-limit (400 RATELIMITED)
        self.http429 = 0
        self.reset_in_ms = 50
        self.revoke_all_tokens = False
        self.viewer = {
            "id": "app-user-1",
            "name": "Fleet",
            "displayName": "Fleet",
            "email": "fleet@oauthapp.linear.app",
            "app": True,
        }
        # Seat apps a test adds:  client id -> {"secret", "viewer"}.  The Fleet app is CLIENT_ID with
        # the module's CLIENT_SECRET (read at request time, so a rotation test can change it) and
        # self.viewer.
        self.clients: dict = {}
        self.token_client: dict = {}  # token -> the client id it was minted for
        self.mints_by: dict = {}  # client id -> mints
        self.calls: list = []  # (operation, client id of the bearer token), in order
        self.current_token = None
        self.org = {"name": "Simple With Us", "urlKey": "simple-with-us"}
        self.teams: dict = {}
        for i, key in enumerate(("OPS", "ST", "CT", "AFC", "FL", "CC", "SWU")):
            self.teams[key] = {
                "id": f"team-{key}",
                "name": key + " team",
                "n": 0,
                "states": {
                    n: {"id": f"st-{key}-{j}", "name": n, "type": t}
                    for j, (n, t) in enumerate(STATES)
                },
            }
        self.label_creates: list = []  # every issueLabelCreate input, in order
        self.updates: list = []  # (identifier, issueUpdate input), in order
        self.labels: dict = {}
        for g, kids in (
            ("Severity", ["P0", "P1", "P2", "P3", "P4"]),
            ("Env", ["Mac", "Cloud"]),
            ("Kind", ["agent-report", "review-finding", "effort-row", "github-issue", "sentry"]),
        ):
            self._label(g, None)
            for k in kids:
                self._label(k, g)
        for name in (
            "seat:CLAUDE",
            "seat:CODEX",
            "claimed:CODEX",
            "stale-claim",
            "imported",
            "app:agent-bar",
        ):
            self._label(name, None)
        # Workspace users, mirroring the fleet's Linear bot seats.  Names differ
        # in case from the claim labels on purpose:  a seat resolves to its user
        # by name, and the deactivated one must never be assigned to anything.
        self.users: dict = {}
        for n in ("Codex", "Claude", "Cursor", "GB-Plumber", "GB-Deployer"):
            self.users[n] = {"id": f"user-{n.lower()}", "name": n, "displayName": n, "active": True}
        self.users["GB-Retired"] = {
            "id": "user-gb-retired",
            "name": "GB-Retired",
            "displayName": "GB-Retired",
            "active": False,
        }
        # When True the BoardUsers listing is refused, so ownership must degrade
        # instead of aborting the verb.
        self.fail_users = False
        # When True, updates without an assignee are refused, so a test can
        # fail the claim retry after its assignee was refused.
        self.fail_ownerless_updates = False
        # When True, assignee clears (assigneeId None) are refused, so a test
        # can fail a steal's ownership write while the retry drops the field.
        self.fail_assignee_clear = False
        self.issues: dict = {}
        self.comments: dict = {}
        self.relations: list = []
        self.attachments: list = []
        self._seed()

    def add_client(self, cid, secret, name, app=True):
        """A seat app:  its client credentials and the viewer Linear names for its tokens."""
        slug = re.sub(r"[^a-z0-9]+", "-", name.lower())
        self.clients[cid] = {
            "secret": secret,
            "viewer": {
                "id": f"app-user-{slug}",
                "name": name,
                "displayName": name,
                "email": f"{slug}@oauthapp.linear.app",
                "app": app,
            },
        }

    def viewer_for(self, token):
        cid = self.token_client.get(token)
        return self.clients[cid]["viewer"] if cid in self.clients else self.viewer

    def secrets(self):
        return [CLIENT_SECRET] + [c["secret"] for c in self.clients.values()]

    # ----------------------------------------------------------- seed
    def _label(self, name, parent, lid=None, team=None):
        lid = lid or f"lbl-{(parent + '-') if parent else ''}{name}"
        self.labels[lid] = {
            "id": lid,
            "name": name,
            "parent": parent,
            "team": team,
            "isGroup": parent is None and name in ("Severity", "Env", "Kind"),
        }
        return lid

    def lid(self, key):
        for lb in self.labels.values():
            k = f"{lb['parent']}/{lb['name']}" if lb["parent"] else lb["name"]
            if k == key:
                return lb["id"]
        raise KeyError(key)

    def add_issue(
        self,
        uid,
        team,
        state,
        title,
        priority=0,
        labels=(),
        created="2026-10-01T12:00:00Z",
        description="*No description on the board.*",
    ):
        t = self.teams[team]
        t["n"] += 1
        ident = f"{team}-{t['n']}"
        self.issues[uid] = {
            "id": uid,
            "identifier": ident,
            "team": team,
            "state": state,
            "title": title,
            "priority": priority,
            "labels": [self.lid(k) for k in labels],
            "createdAt": created,
            "updatedAt": created,
            "description": description,
        }
        return self.issues[uid]

    def add_comment(self, issue_id, body, created, user="Jay", user_id=None, app=None):
        cid = str(uuid.uuid4())
        who = {"name": user}
        if user_id is not None:
            who["id"] = user_id
        if app is not None:
            who["app"] = app
        self.comments[cid] = {
            "id": cid,
            "issue": issue_id,
            "body": body,
            "createdAt": created,
            "user": who,
        }
        return cid

    def _seed(self):
        for _ in range(9):  # OPS-1..9 are filler so OPS-10 matches the real identifier
            self.teams["OPS"]["n"] += 1
        a = self.add_issue(
            hy(B_A),
            "OPS",
            "Todo",
            "Fleet daily digest",
            2,
            ["Severity/P1", "Kind/agent-report", "imported"],
            "2026-10-02T12:00:00Z",
            "Digest body text.\n\n## Recommended fix\n\nShip it.\n\n---\n\n**Provenance (migrated)**\n\n"
            "* Board row: `x` (kind agent-report, app fleet-infra)\n  * Reported by grok, addressed by claude\n"
            "  * Env mac, location \\~/apps/old @ main\n\n<!-- migrated-from: board:x -->",
        )
        self.add_comment(
            a["id"],
            "**claude** · Thu, Oct 2, 2026, 7:00am (mac, ~/apps/x)\n\nFirst look.\n\n"
            "<!-- migrated-comment: k author:claude at:z -->",
            "2026-10-02T13:00:00Z",
        )
        self.add_comment(
            a["id"], "**codex** · Thu, Oct 2, 2026, 8:00am\n\nSecond look.", "2026-10-02T14:00:00Z"
        )
        self.add_comment(
            a["id"],
            "**Resolution, by CLAUDE** · Thu, Oct 2, 2026, 9:00am\n\nOld resolution.",
            "2026-10-02T15:00:00Z",
        )
        self.add_issue(
            hy(B_B),
            "OPS",
            "In Progress",
            "Listener work",
            1,
            ["Severity/P0", "Kind/agent-report", "seat:CODEX", "claimed:CODEX", "Env/Mac"],
            "2026-10-03T12:00:00Z",
        )
        self.add_issue(
            hy(B_C),
            "ST",
            "Todo",
            "Socratic chart bug",
            3,
            ["Severity/P2", "Kind/review-finding"],
            "2026-10-04T12:00:00Z",
        )
        self.teams["CT"]["n"] = 6
        self.add_issue(
            D_LINEAR,
            "CT",
            "Done",
            "Merged GitHub issue",
            0,
            ["Kind/github-issue"],
            "2026-09-01T12:00:00Z",
        )
        self.add_issue(
            hy(B_H),
            "CT",
            "Todo",
            "Legal compliance five",
            4,
            ["Severity/P3", "Kind/review-finding"],
            "2026-09-02T12:00:00Z",
            "Text.\n\n---\n\n* External uid: `legal/LEGAL-05`\n",
        )
        self.add_issue(
            hy(B_F),
            "AFC",
            "Todo",
            "Ambiguous one",
            0,
            ["Kind/agent-report"],
            "2026-09-05T12:00:00Z",
        )
        self.add_issue(
            hy(B_G),
            "AFC",
            "Todo",
            "Ambiguous two",
            0,
            ["Kind/agent-report", "app:agent-bar"],
            "2026-09-06T12:00:00Z",
        )
        self.add_issue(
            str(uuid.uuid4()), "OPS", "Todo", "Low one", 4, ["Severity/P3"], "2026-09-07T12:00:00Z"
        )
        self.add_issue(
            str(uuid.uuid4()),
            "OPS",
            "Todo",
            "Lowest one",
            4,
            ["Severity/P4"],
            "2026-09-08T12:00:00Z",
        )

    # ----------------------------------------------------------- rendering
    def label_node(self, lid):
        lb = self.labels[lid]
        return {
            "id": lid,
            "name": lb["name"],
            "parent": {"name": lb["parent"]} if lb["parent"] else None,
        }

    def node(self, iss):
        t = self.teams[iss["team"]]
        st = t["states"][iss["state"]]
        return {
            "id": iss["id"],
            "identifier": iss["identifier"],
            "title": iss["title"],
            "url": f"https://linear.app/simple-with-us/issue/{iss['identifier']}",
            "priority": iss["priority"],
            "createdAt": iss["createdAt"],
            "updatedAt": iss["updatedAt"],
            "description": iss["description"],
            "state": dict(st),
            "team": {"id": t["id"], "key": iss["team"], "name": t["name"]},
            "labels": {"nodes": [self.label_node(x) for x in iss["labels"]]},
            "attachments": {
                "nodes": [
                    {"url": a["url"], "title": a["title"]}
                    for a in self.attachments
                    if a["issue"] == iss["id"]
                ]
            },
            "relations": {
                "nodes": [
                    {"type": r["type"], "relatedIssue": self.brief(r["related"])}
                    for r in self.relations
                    if r["issue"] == iss["id"]
                ]
            },
            "inverseRelations": {
                "nodes": [
                    {"type": r["type"], "issue": self.brief(r["issue"])}
                    for r in self.relations
                    if r["related"] == iss["id"]
                ]
            },
        }

    def brief(self, iid):
        i = self.issues[iid]
        return {"id": iid, "identifier": i["identifier"], "team": {"key": i["team"]}}

    def find(self, key):
        if key in self.issues:
            return self.issues[key]
        for i in self.issues.values():
            if i["identifier"] == key:
                return i
        return None

    # ----------------------------------------------------------- filters
    @staticmethod
    def cmp(val, c):
        for op, x in c.items():
            if op == "eq" and val != x:
                return False
            if op == "neq" and val == x:
                return False
            if op == "in" and val not in x:
                return False
            if op == "nin" and val in x:
                return False
            if op == "contains" and (val is None or x not in val):
                return False
            if op == "containsIgnoreCase" and (val is None or x.lower() not in val.lower()):
                return False
            if op == "startsWith" and (val is None or not val.startswith(x)):
                return False
        return True

    def label_match(self, lid, f):
        lb = self.labels[lid]
        for k, v in f.items():
            if k == "name" and not self.cmp(lb["name"], v):
                return False
            if k == "parent":
                if lb["parent"] is None or not self.cmp(lb["parent"], v.get("name", {})):
                    return False
        return True

    def match(self, iss, f):
        for k, v in (f or {}).items():
            if k == "and":
                if not all(self.match(iss, x) for x in v):
                    return False
            elif k == "or":
                if not any(self.match(iss, x) for x in v):
                    return False
            elif k == "team":
                if not self.cmp(iss["team"], v.get("key", {})):
                    return False
            elif k == "state":
                if not self.cmp(iss["state"], v.get("name", {})):
                    return False
            elif k in ("priority", "title", "description", "id"):
                if not self.cmp(iss[k], v):
                    return False
            elif k == "labels":
                if "some" in v and not any(self.label_match(x, v["some"]) for x in iss["labels"]):
                    return False
            else:
                self.violations.append(f"unknown filter key {k}")
                return False
        return True

    def page(self, items, first, after):
        if not isinstance(first, int) or first < 1:
            self.violations.append(f"connection without a usable first ({first!r})")
            first = 50
        start = int(after) if after else 0
        chunk = items[start : start + min(first, self.page_cap)]
        end = start + len(chunk)
        return {
            "pageInfo": {"hasNextPage": end < len(items), "endCursor": str(end)},
            "nodes": chunk,
        }

    # ----------------------------------------------------------- operations
    def handle(self, op, q, v):
        for m in CONN_RE.finditer(q):
            if "first:" not in (m.group(3) or ""):
                self.violations.append(f"{op}: {m.group(1)} without first:")
        if op == "BoardWhoami":
            return {"viewer": self.viewer_for(self.current_token), "organization": self.org}, None
        if op == "BoardTeams":
            nodes = [
                {
                    "id": t["id"],
                    "key": k,
                    "name": t["name"],
                    "issueCount": sum(1 for i in self.issues.values() if i["team"] == k),
                    "states": {"nodes": list(t["states"].values())},
                }
                for k, t in self.teams.items()
            ]
            return {"teams": self.page(nodes, v.get("first"), v.get("after"))}, None
        if op == "BoardLabels":
            nodes = [
                {
                    "id": lb["id"],
                    "name": lb["name"],
                    "isGroup": lb["isGroup"],
                    "parent": {"name": lb["parent"]} if lb["parent"] else None,
                    "team": {"key": lb["team"]} if lb["team"] else None,
                }
                for lb in self.labels.values()
            ]
            return {"issueLabels": self.page(nodes, v.get("first"), v.get("after"))}, None
        if op == "BoardUsers":
            if self.fail_users:
                return None, [{"message": "users listing refused"}]
            nodes = [
                {
                    "id": u.get("id"),
                    "name": u.get("name"),
                    "displayName": u.get("displayName"),
                    "active": u.get("active"),
                }
                for u in self.users.values()
            ]
            return {"users": self.page(nodes, v.get("first"), v.get("after"))}, None
        if op == "BoardIssue":
            iss = self.find(v["id"])
            if not iss:
                return None, [
                    {"message": "Entity not found: Issue", "extensions": {"code": "INVALID_INPUT"}}
                ]
            return {"issue": self.node(iss)}, None
        if op == "BoardComments":
            iss = self.find(v["id"])
            cs = sorted(
                (c for c in self.comments.values() if c["issue"] == iss["id"]),
                key=lambda c: c["createdAt"],
                reverse=True,
            )
            nodes = [
                {"id": c["id"], "body": c["body"], "createdAt": c["createdAt"], "user": c["user"]}
                for c in cs
            ]
            return {"issue": {"comments": self.page(nodes, v.get("first"), v.get("after"))}}, None
        if op in ("BoardList", "BoardCount", "BoardRelabel"):
            hits = [i for i in self.issues.values() if self.match(i, v.get("filter"))]
            hits.sort(key=lambda i: i["createdAt"], reverse=True)
            nodes = [self.node(i) for i in hits]
            return {"issues": self.page(nodes, v.get("first"), v.get("after"))}, None
        if op == "BoardUidAttachment":
            nodes = [
                {"issue": self.brief(a["issue"])} for a in self.attachments if a["url"] == v["url"]
            ]
            return {"attachmentsForURL": self.page(nodes, v.get("first"), v.get("after"))}, None
        if op == "BoardCommentOne":
            c = self.comments.get(v["id"])
            return (
                ({"comment": {"id": c["id"]}}, None)
                if c
                else (None, [{"message": "Entity not found: Comment"}])
            )
        if op in ("BoardFile", "BoardUpdate", "BoardComment"):
            for key in ("createAsUser", "displayIconUrl", "delegateId"):
                if key in (v.get("input") or {}):
                    self.violations.append(f"{op} sent {key} (no delegation or createAsUser yet)")
        if op == "BoardFile":
            inp = v["input"]
            if inp.get("id") in self.issues:
                return {"issueCreate": None}, [{"message": "Issue with this id already exists"}]
            team = next(k for k, t in self.teams.items() if t["id"] == inp["teamId"])
            st = next(
                s for s in self.teams[team]["states"].values() if s["id"] == inp.get("stateId")
            )
            if st["type"] == "duplicate":
                self.violations.append("issueCreate straight into Duplicate")
                return {"issueCreate": None}, [{"message": "invalid state"}]
            t = self.teams[team]
            t["n"] += 1
            now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            iid = inp.get("id") or str(uuid.uuid4())
            self.issues[iid] = {
                "id": iid,
                "identifier": f"{team}-{t['n']}",
                "team": team,
                "state": st["name"],
                "title": inp["title"],
                "priority": inp.get("priority", 0),
                "labels": list(inp.get("labelIds") or []),
                "createdAt": now,
                "updatedAt": now,
                "description": linear_markdown(inp.get("description") or ""),
            }
            for lid in self.issues[iid]["labels"]:
                if lid not in self.labels:
                    self.violations.append(f"unknown label id {lid}")
            return {"issueCreate": {"success": True, "issue": self.node(self.issues[iid])}}, None
        if op == "BoardUpdate":
            iss = self.find(v["id"])
            inp = v["input"]
            self.updates.append((iss["identifier"], json.loads(json.dumps(inp))))
            if "labelIds" in inp:
                self.violations.append("issueUpdate with labelIds (replaces every label)")
            if "assigneeId" in inp:
                uid = inp["assigneeId"]
                if uid is None:
                    if self.fail_assignee_clear:
                        return {"issueUpdate": None}, [{"message": "assignee clear refused"}]
                else:
                    # Linear only accepts an existing, active user here, and a
                    # refused update applies nothing: validate before writing.
                    m = next((u for u in self.users.values() if u["id"] == uid), None)
                    if not m or not m["active"]:
                        return {"issueUpdate": None}, [
                            {"message": f"assignee {uid} not found or deactivated"}
                        ]
            if self.fail_ownerless_updates and "assigneeId" not in inp:
                return {"issueUpdate": None}, [{"message": "update refused"}]
            for lid in inp.get("addedLabelIds") or []:
                if lid not in self.labels:
                    return {"issueUpdate": None}, [{"message": f"label {lid} not found"}]
            if "stateId" in inp:
                st = next(
                    s
                    for s in self.teams[iss["team"]]["states"].values()
                    if s["id"] == inp["stateId"]
                )
                if st["type"] == "duplicate":
                    self.violations.append("issueUpdate straight into Duplicate")
                    return {"issueUpdate": None}, [{"message": "invalid state"}]
                iss["state"] = st["name"]
            for lid in inp.get("removedLabelIds") or []:
                if self.labels[lid]["name"].startswith("seat:"):
                    self.violations.append("issueUpdate removed a seat: attribution label")
                if lid in iss["labels"]:
                    iss["labels"].remove(lid)
            for lid in inp.get("addedLabelIds") or []:
                if lid not in iss["labels"]:
                    iss["labels"].append(lid)
            if "assigneeId" in inp:
                iss["assignee"] = inp["assigneeId"]
            groups = [self.labels[x]["parent"] for x in iss["labels"] if self.labels[x]["parent"]]
            if len(groups) != len(set(groups)):
                self.violations.append("two labels from one exclusive group")
            return {"issueUpdate": {"success": True, "issue": self.node(iss)}}, None
        if op == "BoardComment":
            inp = v["input"]
            if inp.get("id") in self.comments:
                return {"commentCreate": None}, [{"message": "Comment already exists"}]
            iss = self.find(inp["issueId"])
            cid = inp.get("id") or str(uuid.uuid4())
            self.comments[cid] = {
                "id": cid,
                "issue": iss["id"],
                "body": inp["body"],
                "createdAt": f"2026-10-10T{20 + len(self.comments) // 3600:02d}:{len(self.comments) // 60 % 60:02d}:{len(self.comments) % 60:02d}.000Z",
                "user": {"name": "Fleet"},
            }
            return {"commentCreate": {"success": True, "comment": {"id": cid}}}, None
        if op == "BoardRelate":
            inp = v["input"]
            self.relations.append(
                {
                    "id": inp.get("id"),
                    "type": inp["type"],
                    "issue": inp["issueId"],
                    "related": inp["relatedIssueId"],
                }
            )
            if inp["type"] == "duplicate":
                self.issues[inp["issueId"]]["state"] = "Duplicate"  # Linear's own move
            return {
                "issueRelationCreate": {"success": True, "issueRelation": {"id": inp.get("id")}}
            }, None
        if op == "BoardAttach":
            inp = v["input"]
            self.attachments = [
                a
                for a in self.attachments
                if not (a["issue"] == inp["issueId"] and a["url"] == inp["url"])
            ]
            self.attachments.append(
                {"issue": inp["issueId"], "url": inp["url"], "title": inp.get("title")}
            )
            return {"attachmentCreate": {"success": True, "attachment": {"id": "att"}}}, None
        if op == "BoardLabelCreate":
            inp = v["input"]
            self.label_creates.append(dict(inp))
            if "parentId" in inp or "teamId" in inp:
                self.violations.append(f"label {inp['name']!r} created inside a team or a group")
            self._label(inp["name"], None, inp.get("id"))
            return {
                "issueLabelCreate": {
                    "success": True,
                    "issueLabel": {"id": inp.get("id"), "name": inp["name"]},
                }
            }, None
        self.violations.append(f"unknown operation {op}")
        return None, [{"message": f"unknown operation {op}"}]


def make_handler(mock: MockLinear):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, obj, headers=None):
            raw = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            for k, val in (headers or {}).items():
                self.send_header(k, val)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            with mock.lock:
                if self.path == "/oauth/token":
                    form = dict(urllib.parse.parse_qsl(body.decode()))
                    mock.scopes.append(form.get("scope"))
                    cid = form.get("client_id")
                    if mock.mint_down:
                        return self._send(503, {"error": "temporarily_unavailable"})
                    want = (
                        CLIENT_SECRET
                        if cid == CLIENT_ID
                        else (mock.clients.get(cid) or {}).get("secret")
                    )
                    if want is None or form.get("client_secret") != want:
                        return self._send(
                            401,
                            {"error": "invalid_client", "error_description": "bad client"},
                            {"x-request-id": mock.request_id} if mock.request_id else None,
                        )
                    mock.mints += 1
                    mock.mints_by[cid] = mock.mints_by.get(cid, 0) + 1
                    tok = f"lin_oauth_mocktoken_{mock.mints}_{uuid.uuid4().hex}"
                    mock.tokens.add(tok)
                    mock.token_client[tok] = cid
                    return self._send(
                        200,
                        {
                            "access_token": tok,
                            "token_type": "Bearer",
                            "expires_in": 2592000,
                            "scope": form.get("scope"),
                        },
                    )
                if self.path != "/graphql":
                    return self._send(404, {})
                auth = self.headers.get("Authorization") or ""
                if (
                    not auth.startswith("Bearer ")
                    or auth[7:] not in mock.tokens
                    or mock.revoke_all_tokens
                ):
                    return self._send(
                        401,
                        {
                            "errors": [
                                {
                                    "message": "Authentication required",
                                    "extensions": {"code": "AUTHENTICATION_ERROR"},
                                }
                            ]
                        },
                    )
                req = json.loads(body or b"{}")
                q = req.get("query") or ""
                m = re.search(r"\b(query|mutation)\s+(\w+)", q)
                op = m.group(2) if m else "?"
                mock.ops.append(op)
                mock.current_token = auth[7:]
                mock.calls.append((op, mock.token_client.get(auth[7:])))
                reset = str(int(time.time() * 1000) + mock.reset_in_ms)
                if mock.ratelimit400 > 0:
                    mock.ratelimit400 -= 1
                    return self._send(
                        400,
                        {
                            "errors": [
                                {
                                    "message": "Rate limit exceeded",
                                    "extensions": {"code": "RATELIMITED"},
                                }
                            ]
                        },
                        {"X-RateLimit-Requests-Reset": reset},
                    )
                if mock.limit_ops.get(op, 0) > 0:
                    mock.limit_ops[op] -= 1
                    return self._send(
                        400,
                        {
                            "errors": [
                                {
                                    "message": "Rate limit exceeded",
                                    "extensions": {"code": "RATELIMITED"},
                                }
                            ]
                        },
                        {"X-RateLimit-Requests-Reset": reset},
                    )
                if mock.http429 > 0:
                    mock.http429 -= 1
                    return self._send(
                        429,
                        {"errors": [{"message": "Too many requests"}]},
                        {"X-RateLimit-Requests-Reset": reset},
                    )
                data, errs = mock.handle(op, q, req.get("variables") or {})
                out = {"data": data}
                if errs:
                    out["errors"] = errs
                return self._send(200, out)

    return H


FAKE_FLEET_SECRETS = r"""#!@PY@
import os, sys
V = "@VAULT@"
a = sys.argv[1:]
with open("@CALLS@", "a") as log:
    log.write(" ".join(a[:2]) + "\n")
if len(a) < 2:
    sys.exit(2)
verb = a[0]
if verb == "get":
    if a[2:] == ["--path", "/linear"]:
        f = os.path.join(V, a[1])
    elif len(a[2:]) == 2 and a[2] == "--path" and a[3].startswith("/") and ".." not in a[3]:
        f = os.path.join(V, a[3].strip("/"), a[1])
    else:
        sys.exit(3)
    if not os.path.isfile(f):
        sys.exit(3)
    sys.stdout.write(open(f).read())
elif verb == "set":
    if a[2:] != ["--path", "/linear"]:
        sys.exit(2)            # a value in an argument, or any other shape, is refused
    if os.environ.get("FS_FAIL_SET") == "1":
        sys.exit(1)
    value = sys.stdin.read()
    if not value:
        sys.exit(2)
    tmp = os.path.join(V, "." + a[1] + ".tmp%d" % os.getpid())
    with open(tmp, "w") as fh:
        fh.write(value)
    os.replace(tmp, os.path.join(V, a[1]))
    print("updated", a[1], "at /linear")
elif verb == "run":
    # The board reads one named key at a time with `get`.  `run` would dump the store's environment, so it is
    # refused here and recorded, and a test fails if the CLI ever calls it (AFC-361 review).
    open(os.path.join(V, ".run-called"), "w").write("run")
    sys.exit(9)
else:
    sys.exit(2)
"""


class BoardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="board-test-"))
        self.mock = MockLinear()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.mock))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        port = self.server.server_address[1]
        (self.tmp / "home").mkdir()
        # Credentials come from Infisical Fleet-OPS /linear (AFC-359), through a fake `fleet-secrets` that serves
        # one file per key from this directory.  Nothing under the test's HOME or the real secrets folder is read.
        self.ops_vault = self.tmp / "fleet-ops-linear"
        self.ops_vault.mkdir()
        self.creds = (
            self.ops_vault / "FLEET_LINEAR_CLIENT_ID"
        )  # unlink() it for "no Fleet credentials"
        self.creds.write_text(CLIENT_ID)
        (self.ops_vault / "FLEET_LINEAR_CLIENT_SECRET").write_text(CLIENT_SECRET)
        # A fake fleet-secrets:  get / set / run over one file per key in ops_vault.  `set` takes its value on
        # standard input only (anything else is exit 2), FS_FAIL_SET=1 makes it fail, and every call's verb is
        # appended to fs_calls (verbs and key names only, never a value).
        self.fs_calls = self.tmp / "fs-calls.log"
        self.fleet_secrets_stub = self.tmp / "fleet-secrets"
        self.fleet_secrets_stub.write_text(
            FAKE_FLEET_SECRETS.replace("@VAULT@", str(self.ops_vault))
            .replace("@CALLS@", str(self.fs_calls))
            .replace("@PY@", PY)
        )
        self.fleet_secrets_stub.chmod(0o700)
        # The stored token blobs are keys of the same fake vault:  CACHE_FLEET, CACHE_<SEAT>.
        self.token_cache = self.ops_vault / "CACHE_FLEET"
        # The harness plays the trusted launcher and the ST-MCP verify route:  a mock
        # gr-bearer-verify/1 server, its key in the fake /tools folder, and one
        # admin-minted bearer per seat in /fleet-seats, bound to each run by board().
        self.verify_store = {"key": BEARER_VERIFY_KEY, "mode": "ok", "queries": []}
        self.vserver = ThreadingHTTPServer(("127.0.0.1", 0), make_verify_handler(self.verify_store))
        threading.Thread(target=self.vserver.serve_forever, daemon=True).start()
        self.verify_url = (
            f"http://127.0.0.1:{self.vserver.server_address[1]}/internal/gr-bearer-verify"
        )
        (self.ops_vault / "tools").mkdir()
        (self.ops_vault / "tools" / "GR_BEARER_VERIFY_KEY").write_text(BEARER_VERIFY_KEY)
        (self.ops_vault / "fleet-seats").mkdir()
        ids = self.tmp / "board-ids.txt"
        ids.write_text(
            "# test ids\n" + "\n".join([B_A, B_B, B_C, f"{B_D}\t{D_LINEAR}", B_F, B_G, B_H]) + "\n"
        )
        self.env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(self.tmp / "home"),
            "LANG": "en_US.UTF-8",
            "AGENT_SEAT": "CLAUDE",
            "CLAUDE_CODE_SESSION_ID": BASE_SESSION,
            "LINEAR_BOARD_FLEET_SECRETS": str(self.fleet_secrets_stub),
            "LINEAR_BOARD_API_URL": f"http://127.0.0.1:{port}/graphql",
            "LINEAR_BOARD_TOKEN_URL": f"http://127.0.0.1:{port}/oauth/token",
            "LINEAR_BOARD_CACHE_DIR": str(self.tmp / "cache"),
            "LINEAR_BOARD_IDS": str(ids),
            "LINEAR_BOARD_BACKOFF_BASE": "0.01",
            "LINEAR_BOARD_DUP_WAIT": "0",
        }

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.vserver.shutdown()
        self.vserver.server_close()
        self.assertEqual(self.mock.violations, [], "mock recorded contract violations")

    def bearer_locator_for(self, seat):
        """One deterministic admin-minted bearer per seat in the fake /fleet-seats folder."""
        part = seat.lower()
        if not re.fullmatch(r"[a-z][a-z0-9-]{1,19}", part):
            self.fail(f"test harness cannot mint a bearer for seat {seat!r}")
        token = f"sbt_{part}_" + (part * 43)[:43]
        key = seat.replace("-", "_") + "_SEAT_BEARER"  # key names allow no hyphen
        (self.ops_vault / "fleet-seats" / key).write_text(token)
        return f"/fleet-seats:{key}"

    def seated_env(self, seat="CLAUDE"):
        """self.env plus the launcher binding, for tests that spawn the CLI directly."""
        return dict(
            self.env,
            SIMPLE_BOARD_BEARER_LOCATOR=self.bearer_locator_for(seat),
            SIMPLE_BOARD_BEARER_VERIFY_URL=self.verify_url,
        )

    def ops_put(self, name, value):
        """Put one key in the fake Fleet-OPS /linear folder."""
        (self.ops_vault / name).write_text(value)

    def fs_verbs(self):
        """The fake fleet-secrets calls so far, as 'verb NAME' lines (never a value)."""
        return self.fs_calls.read_text().splitlines() if self.fs_calls.exists() else []

    def ops_drop_seat(self, seat):
        k = seat.replace("-", "_")
        for suffix in ("ID", "SECRET"):
            (self.ops_vault / f"{k}_LINEAR_CLIENT_{suffix}").unlink(missing_ok=True)

    def private(self, name, content, mode=0o600):
        """A file in the test's temp directory with exactly `mode` (bytes or text content)."""
        path = self.tmp / name
        path.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
        os.chmod(path, mode)
        return str(path)

    def board(self, *args, extra=None, drop=(), code=0, stdin=None):
        e = dict(self.env, **(extra or {}))
        # The seat brings its own platform's session variable (AFC-361).  A test that wants no session passes
        # drop=(...) for it, or names the seat's variable itself in `extra`.
        seat = re.sub(r"[^A-Za-z0-9]+", "-", e.get("AGENT_SEAT", "")).strip("-").upper()
        seat = "GROK" if re.fullmatch(r"GROK-?BUILD", seat) else seat
        own = SEAT_SESSION_VARS.get(seat, "AGENT_SESSION")
        if seat != "CLAUDE" and "CLAUDE_CODE_SESSION_ID" not in (extra or {}):
            e.pop("CLAUDE_CODE_SESSION_ID", None)
        e.setdefault(own, BASE_SESSION)
        for k in drop:
            e.pop(k, None)
        # The harness plays the trusted launcher:  a run whose environment names a seat is
        # bound to that seat's bearer locator (the CLI itself never reads AGENT_SEAT).  An
        # explicit locator in `extra` - including an emptied one - is left alone.
        fab = _cli().norm_seat(e.get("AGENT_SEAT")) if e.get("AGENT_SEAT") else ""
        if fab and e.get("SIMPLE_BOARD_BEARER_LOCATOR") is None:
            e["SIMPLE_BOARD_BEARER_LOCATOR"] = self.bearer_locator_for(fab)
        # SIMPLE_BOARD_BEARER_VERIFY_URL is consumed by CLI_BOOTSTRAP (test code) only.
        if e.get("SIMPLE_BOARD_BEARER_LOCATOR") and e.get("SIMPLE_BOARD_BEARER_VERIFY_URL") is None:
            e["SIMPLE_BOARD_BEARER_VERIFY_URL"] = self.verify_url
        kw = {} if stdin is None else {"input": stdin}
        p = subprocess.run(
            [PY, "-c", CLI_BOOTSTRAP, str(CLI), *args],
            env=e,
            capture_output=True,
            text=True,
            timeout=60,
            **kw,
        )
        for tok in self.mock.tokens:
            self.assertNotIn(tok, p.stdout + p.stderr, "a token reached the output")
        for secret in self.mock.secrets():
            self.assertNotIn(secret, p.stdout + p.stderr, "a client secret reached the output")
        if code is not None:
            self.assertEqual(
                p.returncode, code, f"exit {p.returncode}\nstdout:\n{p.stdout}\nstderr:\n{p.stderr}"
            )
        return p

    def issue_by_ident(self, ident):
        return next(i for i in self.mock.issues.values() if i["identifier"] == ident)

    def label_names(self, iss):
        return sorted(
            (self.mock.labels[x]["parent"] + "/" if self.mock.labels[x]["parent"] else "")
            + self.mock.labels[x]["name"]
            for x in iss["labels"]
        )

    def comments_of(self, iss):
        return [
            c["body"]
            for c in sorted(self.mock.comments.values(), key=lambda c: c["createdAt"])
            if c["issue"] == iss["id"]
        ]


GROK_ALIAS_SPELLINGS = (
    "GROK-BUILD",
    "Grok-Build",
    "grok-build",
    "grok_build",
    "Grok Build",
    "GROK.BUILD",
    "Grok (Build)",
    "grok/build/",
    "GrokBuild",
    "GROK--BUILD!!",
    "  grok-build  ",
    "@Grok-Build",
    "[GROK-BUILD]",
    "`grok-build`",
)
GROK_NOT_ALIASES = (
    "GROKBUILDER",
    "grok-build-bot",
    "grok2build",
    "GROK-WEB",
    "GROK-BOT",
    "GB-COMPILER",
    "BF-BUILDER",
    "Grok (Web/iOS)",
    "GROK",
    "",
    "MONET",
    "RENOIR",
    "HARNESS",
    "MINIMAX",
    "DEEPSEEK",
    "KIMI",
    "build",
    "grok",
)


# The test-only in-process launcher:  imports the CLI as a module, injects the fake verify
# transport for the mock ST-MCP route, and runs main().  The CLI itself has NO environment
# override for the verify endpoint or transport;  SIMPLE_BOARD_BEARER_VERIFY_URL is read by
# THIS bootstrap (test code), never by the CLI, mirroring how production launchers bind the
# locator while the endpoint stays fixed.
CLI_BOOTSTRAP = r"""
import importlib.machinery, importlib.util, os, sys, urllib.error, urllib.request
cli_path = sys.argv[1]
sys.argv = [cli_path] + sys.argv[2:]
loader = importlib.machinery.SourceFileLoader("board_cli_under_test", cli_path)
spec = importlib.util.spec_from_loader("board_cli_under_test", loader)
mod = importlib.util.module_from_spec(spec)
loader.exec_module(mod)
mock_url = os.environ.get("SIMPLE_BOARD_BEARER_VERIFY_URL", "")
if mock_url:
    def fake_transport(url, headers, body, timeout):
        req = urllib.request.Request(mock_url, data=body, headers=headers, method="POST")
        with urllib.request.build_opener().open(req, timeout=timeout) as resp:
            return resp.read(mod.BEARER_MAX_RESPONSE + 1)
    mod.BEARER_TRANSPORT = fake_transport
sys.exit(mod.main())
"""

# The relabel script loads its own copy of the board module, so it needs the same seam the CLI
# bootstrap gives the CLI:  the fake bearer transport, pointed at the mock verify route.
RELABEL_BOOTSTRAP = r"""
import importlib.machinery, importlib.util, os, sys, urllib.request
relabel_path = sys.argv[1]
sys.argv = [relabel_path] + sys.argv[2:]
loader = importlib.machinery.SourceFileLoader("relabel_under_test", relabel_path)
spec = importlib.util.spec_from_loader("relabel_under_test", loader)
mod = importlib.util.module_from_spec(spec)
loader.exec_module(mod)
mock_url = os.environ.get("SIMPLE_BOARD_BEARER_VERIFY_URL", "")
if mock_url:
    def fake_transport(url, headers, body, timeout):
        req = urllib.request.Request(mock_url, data=body, headers=headers, method="POST")
        with urllib.request.build_opener().open(req, timeout=timeout) as resp:
            return resp.read(mod.load_board().BEARER_MAX_RESPONSE + 1)
    mod.load_board().BEARER_TRANSPORT = fake_transport
sys.exit(mod.main())
"""

# Test-process network sandbox (sitecustomize on PYTHONPATH):  DNS and every non-loopback
# connection are refused inside the child, so a test can never probe the real Worker.
NETWORK_SANDBOX = r"""
import socket
_LOOPBACK = ("127.0.0.1", "::1", "localhost")
def _blocked(host):
    return not (isinstance(host, str) and host in _LOOPBACK)
def _dns_guard(name, real):
    def guarded(host, *a, **k):
        if _blocked(host):
            raise socket.gaierror(-2, "test sandbox: DNS blocked")
        return real(host, *a, **k)
    guarded._sandbox_guard = True
    return guarded
for _name in ("getaddrinfo", "gethostbyname", "gethostbyname_ex", "gethostbyaddr", "getnameinfo"):
    socket.__dict__[_name]  # must exist
    globals()["_real_" + _name] = socket.__dict__[_name]
    setattr(socket, _name, _dns_guard(_name, globals()["_real_" + _name]))
_real_connect = socket.socket.connect
def _connect(self, address):
    host = address[0] if isinstance(address, tuple) else address
    if _blocked(host):
        raise OSError(111, "test sandbox: outbound connection blocked")
    return _real_connect(self, address)
_connect._sandbox_guard = True
socket.socket.connect = _connect
_real_connect_ex = socket.socket.connect_ex
def _connect_ex(self, address):
    host = address[0] if isinstance(address, tuple) else address
    if _blocked(host):
        return 111  # ECONNREFUSED, without ever attempting the connection
    return _real_connect_ex(self, address)
_connect_ex._sandbox_guard = True
socket.socket.connect_ex = _connect_ex
"""

# Sentinel-first sandbox self-check, run as a child with NETWORK_SANDBOX on PYTHONPATH:
# guards are verified present BEFORE any denial is exercised, and every denial is
# answered by the guard itself (no real external connect or DNS is ever attempted).
SANDBOX_PROBE = r"""
import socket, sys
missing = []
for name in ("getaddrinfo", "gethostbyname", "gethostbyname_ex", "gethostbyaddr", "getnameinfo"):
    if not getattr(getattr(socket, name), "_sandbox_guard", False):
        missing.append(name)
for meth in ("connect", "connect_ex"):
    if not getattr(getattr(socket.socket, meth), "_sandbox_guard", False):
        missing.append("socket." + meth)
if missing:
    print("guards missing: " + ", ".join(missing))
    sys.exit(1)
ext = ("203.0.113.1", 443)  # TEST-NET-3, documentation-only;  never really attempted
for name in ("getaddrinfo", "gethostbyname", "gethostbyname_ex"):
    try:
        getattr(socket, name)("sandbox-probe.invalid")
        print(name + " did not refuse")
        sys.exit(1)
    except socket.gaierror:
        pass
for call in (lambda: socket.gethostbyaddr("203.0.113.1"), lambda: socket.getnameinfo(ext, 0)):
    try:
        call()
        print("reverse/nameinfo did not refuse")
        sys.exit(1)
    except socket.gaierror:
        pass
try:
    socket.socket().connect(ext)
    print("connect did not refuse")
    sys.exit(1)
except OSError:
    pass
if socket.socket().connect_ex(ext) == 0:
    print("connect_ex did not refuse")
    sys.exit(1)
srv = socket.socket()
srv.bind(("127.0.0.1", 0))
srv.listen(1)
port = srv.getsockname()[1]
c = socket.socket()
c.settimeout(5)
c.connect(("127.0.0.1", port))
conn, _ = srv.accept()
c.close(); conn.close(); srv.close()
if socket.gethostbyname("localhost") != "127.0.0.1":
    print("loopback DNS broke")
    sys.exit(1)
print("SANDBOX OK")
"""

BEARER_ECHO = "sbt_echo_" + "A" * 43
BEARER_FIXER = "gbb_fixer_" + "B" * 43
BEARER_VERIFY_KEY = "verify-key-" + "k" * 40


def make_verify_handler(store):
    """A mock of the ST-MCP gr-bearer-verify/1 route: HMAC-checked, fixed reason codes."""

    class VerifyHandler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _answer(self, status, obj):
            data = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(n)
            sig = self.headers.get("X-Agent-Sync-Signature") or ""
            good = hmac.new(store["key"].encode(), body, hashlib.sha256).hexdigest()
            if not hmac.compare_digest(sig, good):
                self._answer(401, {"ok": False, "reason": "bad_signature"})
                return
            try:
                q = json.loads(body)
            except ValueError:
                self._answer(400, {"ok": False, "reason": "bad_json"})
                return
            store["queries"].append(q)
            mode = store["mode"]
            future = int((time.time() + 3600) * 1000)
            if mode == "ok":
                self._answer(200, {"ok": True, "identity": q.get("identity"), "expires_at": future})
            elif mode == "wrong-identity":
                self._answer(200, {"ok": True, "identity": "CODEX", "expires_at": future})
            elif mode == "past-expiry":
                self._answer(200, {"ok": True, "identity": q.get("identity"), "expires_at": 1000})
            elif mode == "nan-expiry":
                self._answer(
                    200,
                    ('{"ok":true,"identity":"%s","expires_at":NaN}' % q.get("identity")).encode(),
                )
            elif mode == "inf-expiry":
                self._answer(
                    200,
                    (
                        '{"ok":true,"identity":"%s","expires_at":Infinity}' % q.get("identity")
                    ).encode(),
                )
            elif mode == "bool-expiry":
                self._answer(
                    200,
                    ('{"ok":true,"identity":"%s","expires_at":true}' % q.get("identity")).encode(),
                )
            elif mode == "not-json":
                self._answer(200, b"not json")
            else:  # expired / mismatch / paused
                self._answer(200, {"ok": False, "reason": mode})

    return VerifyHandler


class BearerSeatTests(BoardTest):
    """Bearer identity:  the bearer proves the seat;  the environment never picks one."""

    def setUp(self):
        super().setUp()
        (self.ops_vault / "fleet-seats" / "ECHO_SEAT_BEARER").write_text(BEARER_ECHO)
        (self.ops_vault / "fleet-seats" / "FIXER_BEARER").write_text(BEARER_FIXER)
        self.bearer_env = {"SIMPLE_BOARD_BEARER_LOCATOR": "/fleet-seats:ECHO_SEAT_BEARER"}

    def assert_clean_failure(self, p, code=6):
        self.assertEqual(p.returncode, code, f"stdout:\n{p.stdout}\nstderr:\n{p.stderr}")
        self.assertNotIn(BEARER_ECHO, p.stdout + p.stderr)
        self.assertNotIn(BEARER_FIXER, p.stdout + p.stderr)
        self.assertNotIn(hashlib.sha256(BEARER_ECHO.encode()).hexdigest(), p.stdout + p.stderr)
        self.assertNotIn(BEARER_VERIFY_KEY, p.stdout + p.stderr)

    def test_bearer_seat_signs_a_write(self):
        p = self.board(
            "comment", "OPS-10", "--by", "ECHO", "--text", "Bearer run.", extra=self.bearer_env
        )
        self.assertIn("as ECHO", p.stdout)
        body = self.comments_of(self.issue_by_ident("OPS-10"))[-1]
        self.assertTrue(body.startswith("[ECHO\u00b75e55105a] Bearer run."), body)
        q = self.verify_store["queries"][-1]
        self.assertEqual(q["contract"], "gr-bearer-verify/1")
        self.assertEqual(q["identity"], "ECHO")
        self.assertEqual(q["hash"], hashlib.sha256(BEARER_ECHO.encode()).hexdigest())
        self.assertNotIn(BEARER_ECHO, json.dumps(q))

    def test_bearer_beats_the_agent_seat_environment(self):
        # Base env still says AGENT_SEAT=CLAUDE; the verified bearer wins and CLAUDE is ignored.
        p = self.board(
            "comment", "OPS-10", "--by", "ECHO", "--text", "Whose seat?", extra=self.bearer_env
        )
        self.assertIn("as ECHO", p.stdout)
        self.assertNotIn("as CLAUDE", p.stdout)

    def test_gbb_bearer_maps_to_its_gb_role(self):
        extra = dict(self.bearer_env, SIMPLE_BOARD_BEARER_LOCATOR="/fleet-seats:FIXER_BEARER")
        self.board("stats", extra=extra)
        self.assertEqual(self.verify_store["queries"][-1]["identity"], "GB-FIXER")

    def test_bad_signature_is_refused(self):
        (self.ops_vault / "tools" / "GR_BEARER_VERIFY_KEY").write_text("wrong-" + "k" * 40)
        p = self.board("stats", extra=self.bearer_env, code=None)
        self.assert_clean_failure(p)
        self.assertIn("bad_signature", p.stderr)

    def test_mismatch_is_refused(self):
        self.verify_store["mode"] = "mismatch"
        p = self.board("stats", extra=self.bearer_env, code=None)
        self.assert_clean_failure(p)
        self.assertIn("mismatch", p.stderr)

    def test_expired_is_refused(self):
        self.verify_store["mode"] = "expired"
        self.assert_clean_failure(self.board("stats", extra=self.bearer_env, code=None))

    def test_paused_is_refused(self):
        self.verify_store["mode"] = "paused"
        self.assert_clean_failure(self.board("stats", extra=self.bearer_env, code=None))

    def test_an_answer_for_another_identity_is_refused(self):
        self.verify_store["mode"] = "wrong-identity"
        self.assert_clean_failure(self.board("stats", extra=self.bearer_env, code=None))

    def test_a_past_expiry_is_refused(self):
        self.verify_store["mode"] = "past-expiry"
        self.assert_clean_failure(self.board("stats", extra=self.bearer_env, code=None))

    def test_a_non_finite_or_boolean_expiry_is_refused(self):
        for mode in ("nan-expiry", "inf-expiry", "bool-expiry"):
            self.verify_store["mode"] = mode
            self.assert_clean_failure(self.board("stats", extra=self.bearer_env, code=None))

    def test_a_non_canonical_verified_seat_is_refused_not_remapped(self):
        # A verifier answering an alias spelling (INSTINCT is the retired spelling of WISE) is refused:
        # the CLI never re-maps a verified identity.
        alias = "sbt_instinct_" + "C" * 43
        (self.ops_vault / "fleet-seats" / "INSTINCT_BEARER").write_text(alias)
        p = self.board(
            "stats",
            extra=dict(self.bearer_env, SIMPLE_BOARD_BEARER_LOCATOR="/fleet-seats:INSTINCT_BEARER"),
            code=None,
        )
        self.assert_clean_failure(p)
        self.assertIn("non-canonical", p.stderr)

    def test_a_non_json_answer_is_refused(self):
        self.verify_store["mode"] = "not-json"
        self.assert_clean_failure(self.board("stats", extra=self.bearer_env, code=None))

    def test_an_unreachable_verifier_is_refused(self):
        extra = dict(
            self.bearer_env,
            SIMPLE_BOARD_BEARER_VERIFY_URL="http://127.0.0.1:9/internal/gr-bearer-verify",
        )
        p = self.board("stats", extra=extra, code=None)
        self.assert_clean_failure(p)
        self.assertIn("unreachable", p.stderr)

    def test_an_untrusted_verify_url_env_cannot_pick_an_identity(self):
        # Run the CLI binary DIRECTLY (no test bootstrap) under a test-process network
        # sandbox (sitecustomize:  DNS and non-loopback connects are blocked, so this test
        # never produces a real external call).  The caller's SIMPLE_BOARD_BEARER_VERIFY_URL
        # loopback mock stays fully reachable but is never queried;  the CLI's fixed
        # production endpoint attempt fails closed.
        sandbox = self.tmp / "net-sandbox"
        sandbox.mkdir()
        (sandbox / "sitecustomize.py").write_text(NETWORK_SANDBOX)
        e = dict(
            self.env,
            **self.bearer_env,
            SIMPLE_BOARD_BEARER_VERIFY_URL=self.verify_url,
            PYTHONPATH=str(sandbox),
        )
        p = subprocess.run(
            [PY, str(CLI), "stats"], env=e, capture_output=True, text=True, timeout=60
        )
        self.assert_clean_failure(p)
        # "unreachable" proves the sandbox engaged:  a real Worker answer would be a
        # fixed-reason refusal (bad_signature), not a transport failure.
        self.assertIn("unreachable", p.stderr)
        self.assertEqual(self.verify_store["queries"], [], "the caller's mock was never queried")

    def test_a_malformed_bearer_value_is_refused(self):
        (self.ops_vault / "fleet-seats" / "ECHO_SEAT_BEARER").write_text("not-a-bearer")
        p = self.board("stats", extra=self.bearer_env, code=None)
        self.assert_clean_failure(p)
        self.assertIn("not an admin-minted bearer", p.stderr)

    def test_a_missing_bearer_value_is_refused(self):
        (self.ops_vault / "fleet-seats" / "ECHO_SEAT_BEARER").unlink()
        self.assert_clean_failure(self.board("stats", extra=self.bearer_env, code=None))

    def test_a_missing_verify_key_is_refused(self):
        (self.ops_vault / "tools" / "GR_BEARER_VERIFY_KEY").unlink()
        self.assert_clean_failure(self.board("stats", extra=self.bearer_env, code=None))

    def test_malformed_locators_are_refused(self):
        for bad in (
            "nope",
            "/fleet-seats",
            "fleet-seats:ECHO_SEAT_BEARER",
            "/fleet-seats:lowercase",
            "/../tools:KEY",
            "/fleet-seats:KEY:EXTRA",
            "/a/../b:KEY",
            "/a/./b:KEY",
            "/fleet-seats/:KEY",
            "//fleet-seats:KEY",
            "/fleet-seats//x:KEY",
            "/fleet-seats:KEY_NAME_OK:EXTRA2",
        ):
            p = self.board(
                "stats", extra=dict(self.bearer_env, SIMPLE_BOARD_BEARER_LOCATOR=bad), code=None
            )
            self.assert_clean_failure(p)

    def test_without_a_locator_the_environment_cannot_pick_an_identity(self):
        # AGENT_SEAT stays set but is inert:  with no locator the write refuses (exit 6),
        # reads run without an identity, and the verifier is never asked.
        p = self.board(
            "comment",
            "OPS-10",
            "--by",
            "CLAUDE",
            "--text",
            "x",
            extra={"SIMPLE_BOARD_BEARER_LOCATOR": ""},
            code=6,
        )
        self.assertIn("no verified seat", p.stderr)
        self.assertEqual(self.mock.ops, [])
        p = self.board("whoami", extra={"SIMPLE_BOARD_BEARER_LOCATOR": ""})
        self.assertIn("no bearer", p.stdout)
        self.assertEqual(self.verify_store["queries"], [])

    def test_every_seated_run_verifies_its_bearer(self):
        # The harness binds the launcher locator for AGENT_SEAT=CLAUDE:  the run verifies.
        self.board("stats")
        self.assertEqual(self.verify_store["queries"][-1]["identity"], "CLAUDE")


class BearerTransportSeamTests(unittest.TestCase):
    """The verify transport seam is import-only:  the CLI binary reads no endpoint or
    transport from the environment, and the fixed endpoint is the trusted https origin."""

    def test_the_endpoint_is_the_fixed_trusted_origin(self):
        cli = _cli()
        self.assertIsNone(cli.BEARER_TRANSPORT)
        self.assertEqual(
            cli.BEARER_VERIFY_URL, "https://agents.simplewithus.com/internal/gr-bearer-verify"
        )
        self.assertNotIn(
            "SIMPLE_BOARD_BEARER_VERIFY_URL", inspect.getsource(cli.bearer_verified_seat)
        )

    def test_the_sandbox_blocks_every_network_entry_path(self):
        sandbox = Path(tempfile.mkdtemp(prefix="net-sandbox-"))
        (sandbox / "sitecustomize.py").write_text(NETWORK_SANDBOX)
        p = subprocess.run(
            [PY, "-c", SANDBOX_PROBE],
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONPATH": str(sandbox)},
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(p.returncode, 0, f"stdout:\n{p.stdout}\nstderr:\n{p.stderr}")
        self.assertIn("SANDBOX OK", p.stdout)

    def test_every_verifier_identity_is_already_a_canonical_seat_tag(self):
        # The verifier can only answer KNOWN_SEATS (ST-MCP config.js) or an enabled GB persona
        # (gb-identities.json), and sbt_/gbb_ bearers are minted for those only.  The CLI refuses a
        # non-canonical answer instead of re-mapping it, so this guards drift:  if the verifier set ever
        # gains an alias spelling, this fails and the contract is revisited rather than silently remapped.
        root = Path(__file__).resolve().parents[1]
        cfg = (root / "agent-sync-mcp" / "src" / "config.js").read_text()
        m = re.search(r"KNOWN_SEATS = Object\.freeze\(\[([^\]]+)\]\)", cfg)
        self.assertIsNotNone(m)
        seats = re.findall(r'"([A-Z][A-Z0-9-]*)"', m.group(1))
        gb = json.loads((root / "agent_sync" / "mcp" / "gb-identities.json").read_text())["enabled"]
        # This pins only what the hosted verifier answers TODAY.  It is not the fleet roster:  other active
        # tags (AG, CLUTCH, FX, GROK, MC, MM, OPENCODE, BotFleet roles) have no verifier identity yet and
        # need verifier/admin work, not an alias map here.
        self.assertGreaterEqual(len(seats), 9)
        cli = _cli()
        for ident in [*seats, *gb]:
            self.assertEqual(cli.norm_seat(ident), ident, ident)

    def test_in_process_transport_injection(self):
        cli = _cli()
        tmp = Path(tempfile.mkdtemp(prefix="board-seam-"))
        (tmp / "tools").mkdir()
        (tmp / "fleet-seats").mkdir()
        (tmp / "tools" / "GR_BEARER_VERIFY_KEY").write_text(BEARER_VERIFY_KEY)
        (tmp / "fleet-seats" / "ECHO_SEAT_BEARER").write_text(BEARER_ECHO)
        stub = tmp / "fleet-secrets"
        stub.write_text(
            FAKE_FLEET_SECRETS.replace("@VAULT@", str(tmp))
            .replace("@CALLS@", str(tmp / "calls.log"))
            .replace("@PY@", PY)
        )
        stub.chmod(0o700)
        calls = []

        def fake(url, headers, body, timeout):
            calls.append((url, body))
            return json.dumps(
                {"ok": True, "identity": "ECHO", "expires_at": int((time.time() + 3600) * 1000)}
            ).encode()

        cli.BEARER_TRANSPORT = fake
        try:
            with unittest.mock.patch.dict(
                os.environ,
                {
                    "SIMPLE_BOARD_BEARER_LOCATOR": "/fleet-seats:ECHO_SEAT_BEARER",
                    "LINEAR_BOARD_FLEET_SECRETS": str(stub),
                },
            ):
                self.assertEqual(cli.bearer_verified_seat(), "ECHO")
        finally:
            cli.BEARER_TRANSPORT = None
        url, body = calls[0]
        self.assertEqual(url, "https://agents.simplewithus.com/internal/gr-bearer-verify")
        q = json.loads(body)
        self.assertEqual(q["identity"], "ECHO")
        self.assertEqual(q["hash"], hashlib.sha256(BEARER_ECHO.encode()).hexdigest())
        self.assertNotIn(BEARER_ECHO, body.decode())


class SeatAliasTests(unittest.TestCase):
    """Any spelling of GROK followed by BUILD is the seat GROK (owner 2026-10-10);  nothing else is aliased."""

    def test_every_grok_build_spelling_resolves_to_grok(self):
        cli = load_cli()
        for raw in GROK_ALIAS_SPELLINGS:
            self.assertEqual(cli.norm_seat(raw), "GROK", repr(raw))

    def test_near_misses_and_other_retired_tags_are_not_aliased(self):
        cli = load_cli()
        for raw in GROK_NOT_ALIASES:
            self.assertEqual(cli.norm_seat(raw), raw.strip().upper(), repr(raw))
        self.assertEqual(cli.norm_seat(None), "")

    def test_mine_filters_on_the_grok_claim_label(self):
        cli = load_cli()
        want = cli.list_filter(cli.build_parser().parse_args(["list", "--mine", "GROK"]))
        self.assertIn({"labels": {"some": {"name": {"eq": "claimed:GROK"}}}}, want[0])
        for raw in ("Grok-Build", "grok_build", "Grok (Build)"):
            got = cli.list_filter(cli.build_parser().parse_args(["list", "--mine", raw]))
            self.assertEqual(got, want, raw)

    def test_mine_wise_includes_legacy_instinct_claim_label(self):
        cli = load_cli()
        want = cli.list_filter(cli.build_parser().parse_args(["list", "--mine", "WISE"]))
        self.assertIn(
            {"labels": {"some": {"name": {"in": ["claimed:WISE", "claimed:INSTINCT"]}}}}, want[0]
        )
        got = cli.list_filter(cli.build_parser().parse_args(["list", "--mine", "Instinct"]))
        self.assertEqual(got, want)

    def test_pattern_is_the_one_in_fleet_apps_json(self):
        """The board script imports no package, so it carries its own copy of the GROK row's aliasPattern."""
        rows = json.loads((HERE.parents[1] / "fleet-apps.json").read_text())["seats"]
        grok = next(r for r in rows if r["tag"] == "GROK")
        self.assertEqual(load_cli().GROK_ALIAS_PATTERN, grok["aliasPattern"])

    def test_every_botfleet_roster_seat_has_its_own_linear_app(self):
        """A BF seat in SEAT_ROSTER that is missing from SEAT_APP_NAMES signs its writes as Fleet."""
        cli = load_cli()
        bf = sorted(s for s in cli.SEAT_ROSTER if s.startswith("BF-"))
        self.assertIn("BF-PRODUCER", bf)
        self.assertEqual([s for s in bf if s not in cli.SEAT_APP_NAMES], [])
        for seat in bf:
            role = seat.split("-", 1)[1]
            self.assertEqual(cli.SEAT_APP_NAMES[seat], "BF-" + role[:1] + role[1:].lower(), seat)


class SafetyTests(BoardTest):
    def test_writes_require_agent_seat(self):
        for args in (
            ["file", "--title", "x"],
            ["claim", B_A[:8], "--by", "CLAUDE"],
            ["status", B_A[:8], "completed"],
            ["comment", B_A[:8], "--by", "CLAUDE", "--text", "x"],
            ["comment", B_A[:8], "--by", "CLAUDE", "--text-file", "/nonexistent"],
            ["label", "add", B_A[:8], "needs-owner"],
        ):
            p = self.board(*args, drop=("AGENT_SEAT",), code=6)
            self.assertIn("no verified seat", p.stderr)
        self.assertEqual(self.mock.ops, [])
        self.assertEqual(self.mock.mints, 0)

    def test_reads_work_without_agent_seat(self):
        """No bearer locator is bound in plain shells, `zsh -lic` and the listener's `env -i`;
        the skills' reads (`board stats`, `board list --status open,in_progress --severity P0,P1
        --limit 25`) must still run, without an identity."""
        p = self.board(
            "list",
            "--status",
            "open,in_progress",
            "--severity",
            "P0,P1",
            "--limit",
            "25",
            drop=("AGENT_SEAT",),
        )
        self.assertIn("2 shown of", p.stdout)
        self.board("show", "OPS-10", drop=("AGENT_SEAT",))
        self.assertIn("total 9", self.board("stats", drop=("AGENT_SEAT",)).stdout)
        p = self.board("whoami", drop=("AGENT_SEAT",))
        self.assertIn("no bearer", p.stdout)
        self.assertIn("viewer:      Fleet (app user)", p.stdout)

    def test_missing_credentials(self):
        self.creds.unlink()
        p = self.board("list", extra={"LINEAR_API" + "_KEY": "lin_api_should_be_ignored"}, code=4)
        self.assertIn("no Fleet credentials", p.stderr)
        self.assertEqual((self.mock.mints, self.mock.ops), (0, []))

    def test_cloud_seat_credentials_from_environment(self):
        self.creds.unlink()
        p = self.board(
            "whoami", extra={"LINEAR_CLIENT_ID": CLIENT_ID, "LINEAR_CLIENT_SECRET": CLIENT_SECRET}
        )
        self.assertIn("credentials: environment", p.stdout)

    def test_writes_need_a_bearer_verified_seat(self):
        for args in (
            ["file", "--title", "x"],
            ["claim", B_A[:8], "--by", "CLAUDE"],
            ["status", B_A[:8], "completed"],
            ["comment", B_A[:8], "--by", "CLAUDE", "--text", "x"],
            ["file", "--title", "x", "--desc-file", "/nonexistent", "--label", "needs-owner"],
            ["comment", B_A[:8], "--by", "CLAUDE", "--text-file", "/nonexistent"],
            ["label", "add", B_A[:8], "needs-owner"],
            ["label", "remove", B_A[:8], "needs-owner"],
        ):
            p = self.board(*args, drop=("AGENT_SEAT",), code=6)
            self.assertIn("bearer-verified seat", p.stderr)
        self.assertEqual((self.mock.mints, self.mock.ops), (0, []))
        self.board("list", drop=("AGENT_SEAT",))  # reads still work
        self.board("whoami", drop=("AGENT_SEAT",))

    def test_by_must_match_seat(self):
        p = self.board("comment", B_A[:8], "--by", "CODEX", "--text", "hi", code=2)
        self.assertIn("does not match the verified seat CLAUDE", p.stderr)
        self.board(
            "comment", B_A[:8], "--by", "@claude", "--text", "hi"
        )  # normalized match is fine
        self.assertEqual(self.mock.violations, [])

    def test_grok_build_seat_is_grok(self):
        """AGENT_SEAT and --by in any GROK-BUILD spelling sign as GROK, and another seat is still refused."""
        for seat_env in ("GROK-BUILD", "Grok (Build)"):
            for by in ("Grok-Build", "GROK", "grok_build"):
                p = self.board(
                    "comment",
                    B_A[:8],
                    "--by",
                    by,
                    "--text",
                    f"alias {seat_env} {by}",
                    extra={"AGENT_SEAT": seat_env},
                )
                self.assertIn("as GROK", p.stdout)
        p = self.board(
            "comment",
            B_A[:8],
            "--by",
            "CODEX",
            "--text",
            "hi",
            extra={"AGENT_SEAT": "GROK-BUILD"},
            code=2,
        )
        self.assertIn("does not match the verified seat GROK", p.stderr)
        p = self.board(
            "comment",
            B_A[:8],
            "--by",
            "GROK-WEB",
            "--text",
            "hi",
            extra={"AGENT_SEAT": "GROK-BUILD"},
            code=2,
        )
        self.assertIn("does not match the verified seat GROK", p.stderr)
        p = self.board(
            "comment",
            B_A[:8],
            "--by",
            "GROKBUILDER",
            "--text",
            "hi",
            extra={"AGENT_SEAT": "GROKBUILDER"},
        )
        self.assertIn("as GROKBUILDER", p.stdout)  # a near miss stays itself
        p = self.board(
            "comment",
            B_A[:8],
            "--by",
            "GROK",
            "--text",
            "hi",
            extra={"AGENT_SEAT": "GROKBUILDER"},
            code=2,
        )
        self.assertIn("does not match the verified seat GROKBUILDER", p.stderr)
        self.assertEqual(self.mock.violations, [])

    def test_url_override_must_be_loopback(self):
        p = self.board(
            "list", extra={"LINEAR_BOARD_API_URL": "https://evil.example/graphql"}, code=2
        )
        self.assertIn("refusing", p.stderr)
        self.assertEqual(self.mock.mints, 0)

    def test_backend_fallback_execs_old_cli(self):
        old = self.tmp / "old-board"
        old.write_text("import sys\nprint('OLD BOARD', sys.argv[1:])\n")
        extra = {"BOARD_BACKEND": "mac-collab", "BOARD_MAC_COLLAB_CLI": str(old)}
        p = self.board(
            "list", "--app", "AFC", drop=("AGENT_SEAT",), extra=extra
        )
        self.assertEqual(p.stdout.strip(), "OLD BOARD ['list', '--app', 'AFC']")
        self.assertEqual(self.mock.ops, [])
        # a write never rides the rollback backend:  it would skip the bearer-verified seat
        for verb in ("claim", "file", "status", "comment"):
            p = self.board(verb, "abc", "--by", "X", extra=extra, code=6)
            self.assertIn("bearer-verified seat", p.stderr, verb)
            self.assertNotIn("OLD BOARD", p.stdout, verb)
        self.assertEqual(self.mock.ops, [])

    def test_mac_collab_backend_refuses_the_write_verbs(self):
        # Writes must die with EXIT_AUTH before the archived CLI runs: the rollback
        # backend cannot prove a bearer-verified seat (same contract as
        # test_backend_fallback_execs_old_cli).
        old = self.tmp / "old-board"
        old.write_text("import sys\nprint('OLD BOARD', sys.argv[1:])\n")
        extra = {"BOARD_BACKEND": "mac-collab", "BOARD_MAC_COLLAB_CLI": str(old)}
        for args in (
            ["file", "--title", "x", "--desc", "d"],
            ["claim", "abc", "--by", "X"],
            ["status", "OPS-10", "completed"],
            ["comment", "OPS-10", "--by", "X", "--text", "hi"],
        ):
            p = self.board(*args, extra=extra, drop=("AGENT_SEAT",), code=6)
            self.assertIn("bearer-verified seat", p.stderr, args)
            self.assertNotIn("OLD BOARD", p.stdout, args)

    def test_source_never_reads_admin_or_personal_keys(self):
        for path in (CLI, RELABEL):
            src = path.read_text()
            for needle in ("LINEAR_API" + "_KEY", "ADMIN" + "_TOKEN", "global" + "-api"):
                self.assertNotIn(needle, src, path.name)


class AuthTests(BoardTest):
    def test_whoami_mints_once_and_caches(self):
        p = self.board("whoami")
        self.assertIn("viewer:      Fleet (app user)", p.stdout)
        self.assertIn("seat:        CLAUDE", p.stdout)
        self.assertIn("writes:      enabled", p.stdout)
        self.board("whoami")
        self.assertEqual(self.mock.mints, 1)
        self.assertEqual(self.mock.scopes, [EXPECTED_SCOPE])
        self.assertIn(
            "token store: Infisical Fleet-OPS prod /linear CACHE_FLEET (no token on disk)", p.stdout
        )

    def test_whoami_refuses_a_human_viewer(self):
        self.mock.viewer = {
            "id": "u1",
            "name": "Jay",
            "displayName": "Jay",
            "email": "j@x",
            "app": False,
        }
        p = self.board("whoami", code=6)
        self.assertIn("is a person", p.stderr)

    def test_rejected_token_is_not_reminted_within_a_day(self):
        self.board("whoami")
        self.mock.tokens.clear()
        p = self.board("list", code=6)
        self.assertIn("under 24 hours ago", p.stderr)
        self.assertEqual(self.mock.mints, 1)

    def test_rejected_token_reminted_after_a_day(self):
        self.board("whoami")
        cache = json.loads(self.token_cache.read_text())
        cache["minted_at"] -= 2 * 86400
        self.token_cache.write_text(json.dumps(cache))
        self.mock.tokens.clear()
        p = self.board("whoami")
        self.assertEqual(self.mock.mints, 2)

    def test_token_is_reminted_early_on_day_28(self):
        self.board("whoami")
        cache = json.loads(self.token_cache.read_text())
        cache["minted_at"] -= 27 * 86400  # day 27:  still used as is
        self.token_cache.write_text(json.dumps(cache))
        self.board("whoami")
        self.assertEqual(self.mock.mints, 1)
        cache["minted_at"] -= 2 * 86400  # day 29:  the next token is minted while this one works
        self.token_cache.write_text(json.dumps(cache))
        p = self.board("whoami")
        self.assertEqual(self.mock.mints, 2)

    def test_a_failed_early_remint_keeps_the_cached_token(self):
        self.board("whoami")
        cache = json.loads(self.token_cache.read_text())
        cache["minted_at"] -= 29 * 86400
        self.token_cache.write_text(json.dumps(cache))
        self.mock.mint_down = True
        p = self.board("whoami")
        self.assertIn("re-minting the", p.stderr)
        self.assertIn("using the stored one", p.stderr)
        self.assertIn("viewer:", p.stdout)
        self.assertEqual(self.mock.mints, 1)

    def test_rotated_credentials_mint_again(self):
        self.board("whoami")
        global CLIENT_SECRET
        old = CLIENT_SECRET
        try:
            CLIENT_SECRET = "rotated-secret-value"
            self.ops_put("FLEET_LINEAR_CLIENT_SECRET", CLIENT_SECRET)
            self.board("whoami")
            self.assertEqual(self.mock.mints, 2)
        finally:
            CLIENT_SECRET = old

    def test_rate_limit_retry(self):
        self.mock.ratelimit400 = 2
        self.mock.http429 = 1
        p = self.board("show", B_C[:8])
        self.assertIn("Socratic chart bug", p.stdout)
        self.assertEqual(self.mock.ratelimit400 + self.mock.http429, 0)

    def test_rate_limit_gives_up_past_max_wait(self):
        self.mock.ratelimit400 = 50
        self.mock.reset_in_ms = 600000
        p = self.board("show", B_C[:8], extra={"LINEAR_BOARD_MAX_WAIT": "1"}, code=5)
        self.assertIn("rate limit", p.stderr)
        self.assertIn("not applied", p.stderr)


class ReadTests(BoardTest):
    def test_list_sorted_paginated_text(self):
        p = self.board("list", "--limit", "4")
        lines = [ln for ln in p.stdout.splitlines() if ln and not ln.startswith(" ")]
        self.assertTrue(lines[0].startswith("4 shown of 9 matching"), p.stdout)
        first = lines[1:5]
        self.assertTrue(first[0].startswith(B_B[:8] + "  P0  in_progress  OPS  OPS-11"), first)
        self.assertTrue(first[1].startswith(B_A[:8] + "  P1  open         OPS  OPS-10"), first)
        self.assertIn("@CODEX Mac", p.stdout)
        self.assertGreater(
            self.mock.ops.count("BoardList"), 2
        )  # followed cursors at two nodes a page

    def test_list_json_and_filters(self):
        rows = json.loads(
            self.board(
                "list", "--json", "--status", "open,in_progress", "--severity", "P0,P1"
            ).stdout
        )
        self.assertEqual([r["identifier"] for r in rows], ["OPS-11", "OPS-10"])
        self.assertEqual(rows[0]["addressed_by"], "CODEX")
        self.assertEqual(rows[0]["seats"], ["CODEX"])
        self.assertEqual(rows[1]["status"], "open")
        self.assertIsNone(rows[1]["addressed_by"])
        rows = json.loads(self.board("list", "--json", "--app", "socratic-trade").stdout)
        self.assertEqual([r["identifier"] for r in rows], ["ST-1"])
        rows = json.loads(self.board("list", "--json", "--app", "st").stdout)
        self.assertEqual([r["identifier"] for r in rows], ["ST-1"])
        rows = json.loads(self.board("list", "--json", "--app", "agent-bar").stdout)
        self.assertEqual([r["app"] for r in rows], ["agent-bar"])
        rows = json.loads(self.board("list", "--json", "--mine", "codex").stdout)
        self.assertEqual([r["identifier"] for r in rows], ["OPS-11"])
        rows = json.loads(self.board("list", "--json", "--kind", "review-finding").stdout)
        self.assertEqual(sorted(r["identifier"] for r in rows), ["CT-8", "ST-1"])
        rows = json.loads(self.board("list", "--json", "--search", "DIGEST").stdout)
        self.assertEqual([r["identifier"] for r in rows], ["OPS-10"])
        rows = json.loads(self.board("list", "--json", "--severity", "P4").stdout)
        self.assertEqual([r["title"] for r in rows], ["Lowest one"])
        rows = json.loads(self.board("list", "--json", "--severity", "P3").stdout)
        self.assertEqual(sorted(r["title"] for r in rows), ["Legal compliance five", "Low one"])
        p = self.board("list", "--severity", "P3", "--limit", "1")
        self.assertIn("1 shown of 2 matching", p.stdout)
        p = self.board("list", "--status", "bogus", code=2)
        self.assertIn("unknown status", p.stderr)

    def test_list_count_cap(self):
        p = self.board("list", "--limit", "1", extra={"LINEAR_BOARD_COUNT_CAP": "3"})
        self.assertIn("1 shown of 3+ matching", p.stdout)

    def test_show_by_every_id_form(self):
        for ident in (B_A, hy(B_A), B_A[:8], B_A[:12], "OPS-10", "ops-10"):
            p = self.board("show", ident)
            self.assertIn("P1  open  OPS  (agent-report)", p.stdout, ident)
        p = self.board("show", B_D[:8])  # board id merged into an issue with another uuid
        self.assertIn("Merged GitHub issue", p.stdout)
        p = self.board("show", B_D)
        self.assertIn("Merged GitHub issue", p.stdout)
        p = self.board("show", D_LINEAR[:8])
        self.assertIn("Merged GitHub issue", p.stdout)

    def test_show_id_errors(self):
        p = self.board("show", B_F[:8], code=2)
        self.assertIn("ambiguous", p.stderr)
        self.assertIn(B_G, self.board("show", B_F[:31], code=2).stderr)
        self.board("show", B_F)
        p = self.board("show", "deadbeef", code=1)
        self.assertIn("use the identifier", p.stderr)
        p = self.board("show", "abc", code=2)
        self.assertIn("too short", p.stderr)
        p = self.board("show", "OPS-999", code=1)
        self.assertIn("no issue OPS-999", p.stderr)

    def test_show_text_and_json(self):
        p = self.board("show", "OPS-10")
        out = p.stdout
        self.assertIn("--- Description ---\nDigest body text.", out)
        self.assertIn("--- Fix ---\nShip it.", out)
        self.assertIn("--- Resolution ---\nOld resolution.", out)
        self.assertIn("  reported_by: grok", out)
        self.assertIn("  location: ~/apps/old @ main", out)
        self.assertIn("claude (mac, ~/apps/x): First look.", out)
        self.assertIn("codex: Second look.", out)
        d = json.loads(self.board("show", "OPS-10", "--json").stdout)
        self.assertEqual(len(d["comments"]), 3)  # two pages of comments
        self.assertEqual(d["recommended_fix"], "Ship it.")
        self.assertEqual(d["severity"], "P1")
        self.assertEqual(d["id"], hy(B_A))
        capped = json.loads(self.board("show", "OPS-10", "--json", "--comment-limit", "1").stdout)
        self.assertEqual(len(capped["comments"]), 1)  # oldest first, like the hosted tool
        self.assertEqual(capped["comments_total"], 3)
        self.assertEqual(capped["resolution"], d["resolution"])  # still derived from every comment
        text = self.board("show", "OPS-10", "--comment-limit", "1").stdout
        self.assertIn("claude (mac, ~/apps/x): First look.", text)
        self.assertNotIn("codex: Second look.", text)  # the text path honours the cap too
        self.assertIn("(showing 1 of 3 comments)", text)
        self.assertNotIn("showing", out)  # no note when nothing was capped
        self.assertIn(
            "--- Resolution ---\nOld resolution.", text
        )  # still derived from every comment

    def test_listed_new_issue_prefix_resolves(self):
        new = self.mock.add_issue(str(uuid.uuid4()), "AFC", "Todo", "Made in the Linear UI", 3)
        self.board("show", new["id"][:8], code=1)
        self.board("list", "--app", "AFC")
        p = self.board("show", new["id"][:8])
        self.assertIn("Made in the Linear UI", p.stdout)

    def test_stats_and_cache(self):
        p = self.board("stats")
        lines = p.stdout.splitlines()
        self.assertEqual(lines[0], "total 9   open 8   P0/P1 open 2   done 1")
        self.assertIn("  agent-report     4", lines)
        self.assertIn("  apps: AFC, CT, OPS, ST", lines)
        before = len(self.mock.ops)
        self.board("stats")
        self.assertEqual(len(self.mock.ops), before)


class WriteTests(BoardTest):
    def test_file_creates_todo_with_labels(self):
        p = self.board(
            "file",
            "--title",
            "Scout drops rows on 502",
            "--app",
            "congress-trade",
            "--severity",
            "P1",
            "--desc",
            "Details.",
            "--fix",
            "Retry.",
            "--env",
            "cloud",
            "--where",
            "~/x @ y",
            "--url",
            "https://github.com/o/r/issues/1",
        )
        m = re.match(
            r"filed ([0-9a-f-]{36})  \(P1 CT\)  \(sessions: " + BASE_SESSION + r"\)  CT-9$",
            p.stdout.strip(),
        )
        self.assertTrue(m, p.stdout)
        iss = self.mock.issues[m.group(1)]
        self.assertEqual((iss["state"], iss["priority"], iss["team"]), ("Todo", 2, "CT"))
        self.assertEqual(self.label_names(iss), ["Env/Cloud", "Kind/agent-report", "Severity/P1"])
        self.assertTrue(
            iss["description"].startswith("\\[CLAUDE·5e55105a\\] Filed with the board CLI."),
            iss["description"],
        )
        self.assertIn("## Recommended fix\n\nRetry.", iss["description"])
        self.assertEqual(
            [a["url"] for a in self.mock.attachments], ["https://github.com/o/r/issues/1"]
        )
        d = json.loads(self.board("show", "CT-9", "--json").stdout)
        self.assertEqual(
            (d["description"], d["recommended_fix"], d["reported_by"]),
            ("Details.", "Retry.", "CLAUDE"),
        )

    def test_file_round_trips_through_linear_markdown(self):
        p = self.board(
            "file",
            "--title",
            "Brackets",
            "--desc",
            "[note] see ~/x and `[code]`",
            "--fix",
            "- step one\n- step two",
            "--where",
            "~/apps/lanes/X @ b",
            "--env",
            "Mac",
            extra={"CLAUDE_CODE_SESSION_ID": "6db9ee77-0000"},
        )
        iss = self.issue_by_ident(p.stdout.split()[-1])
        self.assertIn(
            "\\[note\\] see \\~/x", iss["description"]
        )  # the mock escaped it, as Linear does
        d = json.loads(self.board("show", iss["identifier"], "--json").stdout)
        self.assertEqual(d["description"], "[note] see ~/x and `[code]`")
        self.assertEqual(d["recommended_fix"], "* step one\n* step two")
        self.assertEqual((d["reported_by"], d["location"]), ("CLAUDE", "~/apps/lanes/X @ b"))
        out = self.board("show", iss["identifier"]).stdout
        self.assertNotIn("\\[", out)

    def test_file_default_app_and_fallback_slug(self):
        p = self.board("file", "--title", "infra thing")
        self.assertIn("(-- OPS)", p.stdout)
        p = self.board("file", "--title", "new app thing", "--app", "brand-new-app")
        self.assertIn("(-- brand-new-app)", p.stdout)
        iss = self.issue_by_ident(p.stdout.split()[-1])
        self.assertEqual(iss["team"], "AFC")
        self.assertIn("app:brand-new-app", self.label_names(iss))
        p = self.board("file", "--title", "x", "--kind", "nonsense", code=2)

    def test_fleetlink_files_to_its_own_team(self):
        # FleetLink and its legacy Worker repo share the one FL team;  an unmapped slug still falls back to AFC
        for app in ("fleetlink", "FleetLink", "fleetlink-legacy", "FL"):
            p = self.board("file", "--title", f"routing {app}", "--app", app)
            self.assertIn("(-- FL)", p.stdout, app)
            iss = self.issue_by_ident(p.stdout.split()[-1])
            self.assertEqual(iss["team"], "FL", app)
            self.assertFalse([n for n in self.label_names(iss) if n.startswith("app:")], app)

    def test_codecaps_files_to_its_own_team(self):
        # CodeCaps routes to the CC team by slug, display name and key (owner 2026-10-11)
        for app in ("codecaps", "CodeCaps", "CC"):
            p = self.board("file", "--title", f"routing {app}", "--app", app)
            self.assertIn("(-- CC)", p.stdout, app)
            iss = self.issue_by_ident(p.stdout.split()[-1])
            self.assertEqual(iss["team"], "CC", app)
            self.assertFalse([n for n in self.label_names(iss) if n.startswith("app:")], app)

    def test_swu_files_to_its_own_team(self):
        # SWU is a team key with no BOARD_SLUG_TEAM entry (AFC-292):  a missing key
        # sent Simple With Us website issues to AFC with an app:swu label.
        for app in ("SWU", "swu"):
            p = self.board("file", "--title", f"routing {app}", "--app", app)
            self.assertIn("(-- SWU)", p.stdout, app)
            iss = self.issue_by_ident(p.stdout.split()[-1])
            self.assertEqual(iss["team"], "SWU", app)
            self.assertFalse([n for n in self.label_names(iss) if n.startswith("app:")], app)

    def test_fll_is_not_a_team_key(self):
        # FLL is a registry key only;  fleetlink-legacy files under FL, and a bare
        # FLL stays on the AFC fallback rather than inventing a team.
        p = self.board("file", "--title", "routing FLL", "--app", "FLL")
        iss = self.issue_by_ident(p.stdout.split()[-1])
        self.assertEqual(iss["team"], "AFC")
        self.assertIn("app:fll", self.label_names(iss))

    def test_file_uid_is_idempotent(self):
        p1 = self.board(
            "file", "--title", "Effort row", "--app", "socratic-trade", "--uid", "effort-abc"
        )
        n = len(self.mock.issues)
        p2 = self.board("file", "--title", "Effort row again", "--app", "ST", "--uid", "effort-abc")
        self.assertEqual(len(self.mock.issues), n)
        self.assertEqual(p1.stdout.split()[1], p2.stdout.split()[1])
        self.assertIn("already filed", p2.stdout)
        self.assertIn(
            "https://board.jays.services/uid/ST/effort-abc",
            [a["url"] for a in self.mock.attachments],
        )

    def test_file_uid_finds_imported_footer(self):
        n = len(self.mock.issues)
        p = self.board(
            "file",
            "--title",
            "Legal compliance five",
            "--app",
            "congress",
            "--uid",
            "legal/LEGAL-05",
        )
        self.assertEqual(len(self.mock.issues), n)
        self.assertIn(hy(B_H), p.stdout)
        self.assertIn("already filed", p.stdout)

    def test_file_uid_finds_attachment(self):
        target = self.mock.issues[hy(B_C)]
        self.mock.attachments.append(
            {
                "issue": target["id"],
                "url": "https://board.jays.services/uid/ST/zulip%3A42",
                "title": "x",
            }
        )
        p = self.board("file", "--title", "From Zulip", "--app", "trading", "--uid", "zulip:42")
        self.assertIn(target["id"], p.stdout)

    def test_claim_open_issue(self):
        p = self.board(
            "claim",
            B_A[:8],
            "--by",
            "CLAUDE",
            "--env",
            "Mac",
            "--where",
            "~/apps/lanes/X @ claude/x",
            extra={"CLAUDE_CODE_SESSION_ID": "b854d9ad-1111-2222-3333-444455556666"},
        )
        self.assertEqual(
            p.stdout.strip(),
            f"claimed {B_A[:8]} by CLAUDE -> in_progress  (sessions: b854d9ad-1111-2222-3333-444455556666)  OPS-10",
        )
        iss = self.mock.issues[hy(B_A)]
        self.assertEqual(iss["state"], "In Progress")
        self.assertIn("claimed:CLAUDE", self.label_names(iss))
        self.assertIn("seat:CLAUDE", self.label_names(iss))
        self.assertIn("Env/Mac", self.label_names(iss))
        body = self.comments_of(iss)[-1]
        self.assertTrue(body.startswith("[CLAUDE·b854d9ad] CLAIMED"), body)
        self.assertIn("Where: Mac, ~/apps/lanes/X @ claude/x", body)
        d = json.loads(self.board("show", "OPS-10", "--json").stdout)
        self.assertEqual(d["location"], "Mac, ~/apps/lanes/X @ claude/x")
        rows = json.loads(self.board("list", "--json", "--mine", "CLAUDE").stdout)
        self.assertEqual([r["identifier"] for r in rows], ["OPS-10"])

    def test_claim_legacy_instinct_label_is_wise_own_claim(self):
        # An In Progress issue carrying the legacy claimed:INSTINCT label:  WISE
        # claims it as its own without --steal, and the legacy label is replaced.
        iss = self.mock.issues[hy(B_B)]
        iss["labels"] = [
            lid for lid in iss["labels"] if self.mock.labels[lid]["name"] != "claimed:CODEX"
        ]
        iss["labels"].append(self.mock._label("claimed:INSTINCT", None))
        p = self.board(
            "claim", "OPS-11", "--by", "WISE", "--env", "cloud", extra={"AGENT_SEAT": "WISE"}
        )
        self.assertEqual(p.returncode, 0, p.stderr)
        names = self.label_names(self.mock.issues[hy(B_B)])
        self.assertIn("claimed:WISE", names)
        self.assertNotIn("claimed:INSTINCT", names)

    def test_claim_refused_when_held_then_steal(self):
        p = self.board("claim", "OPS-11", "--by", "CLAUDE", code=3)
        self.assertIn("claimed by CODEX", p.stderr)
        p = self.board("claim", "OPS-11", "--by", "CLAUDE", "--steal", code=2)
        self.assertIn("--reason", p.stderr)
        self.assertEqual(
            self.mock.issues[hy(B_B)]["labels"].count(self.mock.lid("claimed:CODEX")), 1
        )
        self.board(
            "claim",
            "OPS-11",
            "--by",
            "CLAUDE",
            "--steal",
            "--reason",
            "CODEX is capped out",
            "--env",
            "cloud",
        )
        iss = self.mock.issues[hy(B_B)]
        names = self.label_names(iss)
        self.assertIn("claimed:CLAUDE", names)
        self.assertNotIn("claimed:CODEX", names)
        self.assertIn("seat:CODEX", names)  # attribution survives a steal
        self.assertIn("seat:CLAUDE", names)
        self.assertIn("Env/Cloud", names)
        self.assertNotIn("Env/Mac", names)
        self.assertIn("CLAIMED from CODEX: CODEX is capped out", self.comments_of(iss)[-1])
        self.board("claim", "OPS-11", "--by", "CLAUDE")  # re-claiming your own claim is fine

    def test_claim_creates_missing_seat_label(self):
        self.board("claim", "ST-1", "--by", "GROK-WEB", extra={"AGENT_SEAT": "GROK-WEB"})
        names = self.label_names(self.mock.issues[hy(B_C)])
        self.assertIn("seat:GROK-WEB", names)
        self.assertIn("claimed:GROK-WEB", names)

    def test_status_values_and_resolution(self):
        p = self.board("status", "OPS-10", "completed", "--resolution", "Landed in #2894.")
        self.assertEqual(p.stdout.strip(), f"{B_A[:8]} -> completed{TAIL}  OPS-10")
        iss = self.mock.issues[hy(B_A)]
        self.assertEqual(iss["state"], "Done")
        self.assertEqual(
            self.comments_of(iss)[-1],
            f"[CLAUDE·5e55105a] Resolution (completed): Landed in #2894.\n\nSessions: {BASE_SESSION}",
        )
        d = json.loads(self.board("show", "OPS-10", "--json").stdout)
        self.assertEqual(d["resolution"], "Landed in #2894.")
        for value, state in (
            ("addressed", "In Review"),
            ("deployed", "Deployed"),
            ("wontfix", "Won't Fix"),
            ("open", "Todo"),
            ("in_progress", "In Progress"),
            ("parked", "Parked"),
        ):
            self.board("status", B_C[:8], value)
            self.assertEqual(self.mock.issues[hy(B_C)]["state"], state)
        self.assertEqual(
            self.comments_of(self.mock.issues[hy(B_C)])[-1],
            f"[CLAUDE·5e55105a] Status: parked\n\nSessions: {BASE_SESSION}",
        )
        self.board("status", B_C[:8], "bogus", code=2)
        self.board("status", B_C[:8], "completed", "--of", "OPS-10", code=2)

    def test_status_duplicate_via_relation(self):
        p = self.board("status", B_C[:8], "duplicate", code=2)
        self.assertIn("needs --of", p.stderr)
        p = self.board(
            "status", B_C[:8], "duplicate", "--of", "OPS-10", "--resolution", "Same digest bug."
        )
        self.assertEqual(p.stdout.strip(), f"{B_C[:8]} -> duplicate{TAIL}  ST-1")
        rel = self.mock.relations[-1]
        self.assertEqual(
            (rel["type"], rel["issue"], rel["related"]), ("duplicate", hy(B_C), hy(B_A))
        )
        self.assertEqual(self.mock.issues[hy(B_C)]["state"], "Duplicate")
        self.assertNotIn("BoardUpdate", self.mock.ops)
        self.assertIn(
            "Resolution (duplicate of OPS-10): Same digest bug.",
            self.comments_of(self.mock.issues[hy(B_C)])[-1],
        )
        p = self.board("show", "OPS-10")
        self.assertIn("duplicated by: ST-1", p.stdout)
        self.board("status", "OPS-10", "duplicate", "--of", "OPS-10", code=2)

    def test_comment(self):
        p = self.board(
            "comment",
            "OPS-10",
            "--by",
            "CLAUDE",
            "--text",
            "Verified on main.",
            "--env",
            "Mac",
            "--where",
            "~/apps/lanes/A @ b",
            "--session",
            "sess-1",
            "--session",
            "sess-2",
        )
        self.assertEqual(
            p.stdout.strip(),
            f"commented on {B_A[:8]} as CLAUDE  (sessions: sess-1, sess-2)  OPS-10",
        )
        body = self.comments_of(self.mock.issues[hy(B_A)])[-1]
        self.assertEqual(
            body,
            "[CLAUDE·sess1] Verified on main.\n\nWhere: Mac, ~/apps/lanes/A @ b\n\nSessions: sess-1, sess-2",
        )
        p = self.board("show", "OPS-10")
        self.assertIn("CLAUDE (Mac, ~/apps/lanes/A @ b): Verified on main.", p.stdout)


def footer(line: str) -> str:
    """A description shaped like the importer's, as Linear hands it back."""
    return linear_markdown(
        "Body of the imported issue.\n\n---\n\n**Provenance (migrated from the fleet board and GitHub)**\n\n"
        "- Migration key: `board:x`\n- Board row: `x` (kind agent-report, app usage-monitor, status in_progress)\n"
        f"  - {line}\n  - Env mac, location sat, oct 3, 2026; ~/apps/x @ y\n\n<!-- migrated-from: board:x -->"
    )


class ClaimModelTests(BoardTest):
    """seat:<TAG> labels are attribution (the import put them on reporters too); a claim is a claimed:<TAG>
    label, or for an imported In Progress issue without one, the provenance footer's "addressed by"."""

    def legacy(
        self, title, line, state="In Progress", labels=("seat:CLAUDE", "seat:CODEX"), team="AFC"
    ):
        return self.mock.add_issue(
            str(uuid.uuid4()),
            team,
            state,
            title,
            3,
            ["Severity/P2", "Kind/agent-report", "imported", *labels],
            "2026-10-05T12:00:00Z",
            footer(line),
        )

    def holder(self, iss):
        return json.loads(self.board("show", iss["identifier"], "--json").stdout)["addressed_by"]

    def test_um_1447_shape_reporter_and_claimant_labels(self):
        """Reported by CLAUDE, claimed by CODEX, both labelled:  CODEX may claim it, CLAUDE may not."""
        iss = self.legacy("UM-1447 shape", "Reported by claude, addressed by codex")
        d = json.loads(self.board("show", iss["identifier"], "--json").stdout)
        self.assertEqual((d["addressed_by"], d["seats"]), ("CODEX", ["CLAUDE", "CODEX"]))
        before = list(iss["labels"])
        p = self.board("claim", iss["identifier"], "--by", "CLAUDE", code=3)
        self.assertIn("claimed by CODEX", p.stderr)
        self.assertEqual(
            (iss["labels"], iss["state"]), (before, "In Progress")
        )  # a refusal changes nothing
        self.board("claim", iss["identifier"], "--by", "CODEX", extra={"AGENT_SEAT": "CODEX"})
        names = self.label_names(iss)
        self.assertEqual(
            [n for n in names if n.startswith(("seat:", "claimed:"))],
            ["claimed:CODEX", "seat:CLAUDE", "seat:CODEX"],
        )
        self.board(
            "claim", iss["identifier"], "--by", "CLAUDE", code=3
        )  # still refused, by the marker now
        self.board(
            "claim", iss["identifier"], "--by", "CODEX", extra={"AGENT_SEAT": "CODEX"}
        )  # own claim again

    def test_steal_keeps_every_attribution_label(self):
        iss = self.legacy("Stolen", "Reported by claude, addressed by codex")
        p = self.board("claim", iss["identifier"], "--by", "CLAUDE", "--steal", code=2)
        self.assertIn("--reason", p.stderr)
        self.board(
            "claim",
            iss["identifier"],
            "--by",
            "CLAUDE",
            "--steal",
            "--reason",
            "CODEX is capped out",
        )
        names = self.label_names(iss)
        self.assertEqual(
            [n for n in names if n.startswith(("seat:", "claimed:"))],
            ["claimed:CLAUDE", "seat:CLAUDE", "seat:CODEX"],
        )
        self.assertIn("CLAIMED from CODEX: CODEX is capped out", self.comments_of(iss)[-1])
        self.assertEqual(self.holder(iss), "CLAUDE")  # the marker wins over the old footer
        self.board(
            "claim", iss["identifier"], "--by", "CODEX", extra={"AGENT_SEAT": "CODEX"}, code=3
        )

    def test_footer_forms_aliases_and_unmapped_claimants(self):
        cases = [
            ("Addressed by designer", "BF-DESIGNER"),  # no reporter, role word, lower case
            ("Reported by claude, codex, addressed by minimax", "MM"),
            ("Reported by x, addressed by harness", "CLUTCH"),
            ("Reported by x, addressed by grok-build", "GROK"),
            ("Reported by claude, addressed by codex, cursor", "CODEX,CURSOR"),
            ("Reported by claude, addressed by jay", None),
        ]  # not a seat: nobody holds it
        for i, (line, want) in enumerate(cases):
            iss = self.legacy(f"Footer {i}", line)
            self.assertEqual(self.holder(iss), want, line)
        free = self.legacy("Unmapped claimant", "Reported by claude, addressed by jay")
        self.board("claim", free["identifier"], "--by", "CLAUDE")  # no holder, so no refusal

    def test_reporter_only_label_is_attribution_not_a_claim(self):
        """296 imported Todo/Backlog issues carry a reporter's seat label:  no @SEAT, not in --mine."""
        todo = self.legacy(
            "Reported only", "Reported by codex", state="Todo", labels=("seat:CODEX",)
        )
        p = self.board("list", "--search", "Reported only")
        self.assertNotIn("@", p.stdout)
        rows = json.loads(self.board("list", "--json", "--mine", "CODEX").stdout)
        self.assertEqual([r["identifier"] for r in rows], ["OPS-11"])
        rows = json.loads(self.board("list", "--json", "--search", "Reported only").stdout)
        self.assertEqual((rows[0]["addressed_by"], rows[0]["seats"]), (None, ["CODEX"]))
        self.board("claim", todo["identifier"], "--by", "CLAUDE")
        self.assertIn("seat:CODEX", self.label_names(todo))
        rows = json.loads(self.board("list", "--json", "--mine", "CODEX").stdout)
        self.assertEqual([r["identifier"] for r in rows], ["OPS-11"])

    def test_mine_follows_the_marker_not_the_reporter(self):
        iss = self.legacy("Mine?", "Reported by claude, addressed by codex")
        self.assertEqual(json.loads(self.board("list", "--json", "--mine", "CLAUDE").stdout), [])
        self.board("claim", iss["identifier"], "--by", "CODEX", extra={"AGENT_SEAT": "CODEX"})
        mine = json.loads(self.board("list", "--json", "--mine", "CODEX").stdout)
        self.assertEqual(
            sorted(r["identifier"] for r in mine), sorted(["OPS-11", iss["identifier"]])
        )
        self.assertEqual(json.loads(self.board("list", "--json", "--mine", "CLAUDE").stdout), [])

    def test_list_says_when_in_progress_rows_have_no_marker(self):
        iss = self.legacy("Unmarked", "Reported by claude, addressed by codex")
        p = self.board("list", "--status", "in_progress")
        self.assertIn("no claim marker", p.stderr)
        self.assertEqual(self.board("list", "--status", "in_progress", "--json").stderr, "")
        self.board("claim", iss["identifier"], "--by", "CODEX", extra={"AGENT_SEAT": "CODEX"})
        self.assertNotIn("no claim marker", self.board("list", "--status", "in_progress").stderr)

    def test_open_releases_the_claim(self):
        self.board("claim", "OPS-10", "--by", "CLAUDE")
        self.assertEqual(self.holder(self.issue_by_ident("OPS-10")), "CLAUDE")
        self.board("status", "OPS-10", "open")
        names = self.label_names(self.issue_by_ident("OPS-10"))
        self.assertNotIn("claimed:CLAUDE", names)
        self.assertIn("seat:CLAUDE", names)
        self.assertEqual(json.loads(self.board("list", "--json", "--mine", "CLAUDE").stdout), [])
        self.board("claim", "OPS-10", "--by", "CODEX", extra={"AGENT_SEAT": "CODEX"})
        self.board("status", "OPS-10", "completed")  # closing keeps who addressed it
        self.assertIn("claimed:CODEX", self.label_names(self.issue_by_ident("OPS-10")))

    def test_claiming_a_closed_issue_replaces_the_old_marker(self):
        self.board("claim", "ST-1", "--by", "CODEX", extra={"AGENT_SEAT": "CODEX"})
        self.board("status", "ST-1", "completed")
        self.board("claim", "ST-1", "--by", "CLAUDE")  # not In Progress, so no refusal
        names = [
            n for n in self.label_names(self.issue_by_ident("ST-1")) if n.startswith("claimed:")
        ]
        self.assertEqual(names, ["claimed:CLAUDE"])


class SessionIdTests(BoardTest):
    """AFC-361:  file, claim, status and comment need a session id unless the seat has one thread with the
    owner.  The exemption comes from AGENT_SEAT, never from a flag, and a refusal writes nothing."""

    PLATFORM_VARS = (
        "CLAUDE_CODE_SESSION_ID",
        "CODEX_THREAD_ID",
        "CURSOR_CONVERSATION_ID",
        "CURSOR_AGENT_CHAT_ID",
        "GROK_SESSION_ID",
        "OPENCODE_SESSION_ID",
        "__MAVIS_RUNTIME_SESSION_ID",
        "AGENT_SESSION",
    )
    WRITES = (
        ("comment", "OPS-10", "--by", "{seat}", "--text", "hello"),
        ("claim", "OPS-10", "--by", "{seat}"),
        ("status", "OPS-10", "addressed", "--by", "{seat}"),
        ("file", "--title", "session test", "--app", "OPS", "--by", "{seat}"),
    )

    def bare(self, seat, *args, extra=None, code=0):
        """Run as `seat` with every session variable removed from the environment."""
        keep = extra or {}
        drop = tuple(v for v in self.PLATFORM_VARS if v not in keep)
        return self.board(*args, extra=dict(keep, AGENT_SEAT=seat), drop=drop, code=code)

    def args_for(self, write, seat):
        return [a.format(seat=seat) for a in write]

    def test_every_write_verb_refuses_without_a_session_and_writes_nothing(self):
        for write in self.WRITES:
            n = len(self.mock.calls)
            comments = [list(i.get("comments", [])) for i in self.mock.issues.values()]
            issues = len(self.mock.issues)
            p = self.bare("CLAUDE", *self.args_for(write, "CLAUDE"), code=2)
            self.assertIn("no session id", p.stderr, write[0])
            self.assertIn("--session <id>", p.stderr)
            self.assertIn("AGENT_SESSION", p.stderr)
            self.assertIn("CLAUDE_CODE_SESSION_ID", p.stderr)  # names the seat's own variable
            self.assertEqual(p.stdout, "")
            self.assertEqual(len(self.mock.issues), issues, write[0])
            self.assertEqual(
                comments, [list(i.get("comments", [])) for i in self.mock.issues.values()], write[0]
            )
            self.assertEqual(
                self.mock.calls[n:], [], f"{write[0]} reached Linear before it refused"
            )

    def test_exempt_seats_write_without_a_session(self):
        for seat in (
            "GROK-BOT",
            "GB-FIXER",
            "GB-DIRECTOR",
            "MA",
            "ROB",
            "JET",
            "WISE",
            "INSTINCT",
            "ECHO",
        ):
            p = self.bare(seat, "comment", "OPS-10", "--by", seat, "--text", f"exempt {seat}")
            self.assertNotIn("sessions:", p.stdout, seat)
            self.assertIn("commented on", p.stdout, seat)

    def test_exemption_is_not_a_flag(self):
        for flag in (("--exempt",), ("--no-session",), ("--session", "")):
            p = self.bare(
                "CLAUDE", "comment", "OPS-10", "--by", "CLAUDE", "--text", "x", *flag, code=2
            )
            self.assertNotIn("commented on", p.stdout)
        # --by does not make a non-exempt seat exempt either:  it must equal AGENT_SEAT.
        p = self.bare("CLAUDE", "comment", "OPS-10", "--by", "WISE", "--text", "x", code=2)
        self.assertIn("does not match the verified seat CLAUDE", p.stderr)

    def test_an_exempt_seat_that_has_an_id_still_records_it(self):
        p = self.bare(
            "WISE",
            "comment",
            "OPS-10",
            "--by",
            "WISE",
            "--text",
            "with id",
            extra={"AGENT_SESSION": "wise-thread-1"},
        )
        self.assertIn("(sessions: wise-thread-1)", p.stdout)

    def test_platform_variables_are_found_for_each_seat(self):
        cases = {
            "CLAUDE": "CLAUDE_CODE_SESSION_ID",
            "CODEX": "CODEX_THREAD_ID",
            "GROK": "GROK_SESSION_ID",
            "OPENCODE": "OPENCODE_SESSION_ID",
            "CURSOR": "CURSOR_CONVERSATION_ID",
            "MM": "__MAVIS_RUNTIME_SESSION_ID",
        }
        for seat, var in cases.items():
            sid = f"{seat.lower()}-sess-0001"
            p = self.bare(
                seat, "comment", "OPS-10", "--by", seat, "--text", f"auto {seat}", extra={var: sid}
            )
            self.assertIn(f"(sessions: {sid})", p.stdout, seat)
        p = self.bare(
            "CURSOR",
            "comment",
            "OPS-10",
            "--by",
            "CURSOR",
            "--text",
            "chat id",
            extra={"CURSOR_AGENT_CHAT_ID": "cursor-chat-0002"},
        )
        self.assertIn("(sessions: cursor-chat-0002)", p.stdout)

    def test_a_seat_with_its_own_variable_ignores_other_platforms(self):
        """A CLAUDE session started from inside Codex must not sign with Codex's thread id."""
        p = self.bare(
            "CLAUDE",
            "comment",
            "OPS-10",
            "--by",
            "CLAUDE",
            "--text",
            "x",
            extra={"CODEX_THREAD_ID": "codex-thread-0003"},
            code=2,
        )
        self.assertIn("no session id", p.stderr)

    def test_a_seat_with_no_variable_of_its_own_tries_every_platform_variable(self):
        p = self.bare(
            "BF-FIXER",
            "comment",
            "OPS-10",
            "--by",
            "BF-FIXER",
            "--text",
            "bot",
            extra={"CODEX_THREAD_ID": "codex-thread-0004"},
        )
        self.assertIn("(sessions: codex-thread-0004)", p.stdout)
        p = self.bare("FX", "comment", "OPS-10", "--by", "FX", "--text", "x", code=2)
        self.assertIn("FX has no session variable of its own", p.stderr)

    def test_precedence_is_flag_then_agent_session_then_platform(self):
        env = {"AGENT_SESSION": "generic-0005", "CLAUDE_CODE_SESSION_ID": "platform-0006"}
        p = self.bare("CLAUDE", "comment", "OPS-10", "--by", "CLAUDE", "--text", "a", extra=env)
        self.assertIn("(sessions: generic-0005)", p.stdout)
        p = self.bare(
            "CLAUDE",
            "comment",
            "OPS-10",
            "--by",
            "CLAUDE",
            "--text",
            "b",
            "--session",
            "flag-0007",
            extra=env,
        )
        self.assertIn("(sessions: flag-0007)", p.stdout)
        p = self.bare(
            "CLAUDE",
            "comment",
            "OPS-10",
            "--by",
            "CLAUDE",
            "--text",
            "c",
            extra={"CLAUDE_CODE_SESSION_ID": "platform-0006"},
        )
        self.assertIn("(sessions: platform-0006)", p.stdout)

    def test_a_malformed_id_is_refused(self):
        for bad in ("a b c d", "x", "-leading", "semi;colon", "q" * 129):
            p = self.bare(
                "CLAUDE",
                "comment",
                "OPS-10",
                "--by",
                "CLAUDE",
                "--text",
                "x",
                f"--session={bad}",
                code=2,
            )
            self.assertIn("is not valid", p.stderr)

    def test_file_and_claim_record_the_id_on_the_issue(self):
        sid = "file-claim-0008"
        p = self.bare(
            "CLAUDE",
            "file",
            "--title",
            "records session",
            "--app",
            "OPS",
            "--by",
            "CLAUDE",
            "--session",
            sid,
        )
        self.assertIn(f"(sessions: {sid})", p.stdout)
        ident = re.search(r"(OPS-\d+)\s*$", p.stdout.strip()).group(1)
        issue = self.issue_by_ident(ident)
        self.assertIn(f"* Sessions: {sid}", issue["description"])  # Linear stores the bullet as *
        p = self.bare("CLAUDE", "claim", "OPS-10", "--by", "CLAUDE", "--session", sid)
        self.assertIn(f"(sessions: {sid})", p.stdout)
        self.assertTrue(
            any(f"Sessions: {sid}" in c for c in self.comments_of(self.issue_by_ident("OPS-10")))
        )


class PartialWriteTests(BoardTest):
    """A verb with several requests that stops part way says what landed, and a rerun finishes it."""

    SESSION = {"CLAUDE_CODE_SESSION_ID": "5e55105a-0000-4000-8000-000000000000"}

    def stop(self, op, **extra):
        self.mock.limit_ops[op] = 50
        self.mock.reset_in_ms = 600000
        return dict(self.SESSION, LINEAR_BOARD_MAX_WAIT="1", **extra)

    def claimed_comments(self, ident):
        return [b for b in self.comments_of(self.issue_by_ident(ident)) if "CLAIMED" in b]

    def test_claim_stopped_before_its_comment(self):
        p = self.board("claim", "OPS-10", "--by", "CLAUDE", extra=self.stop("BoardComment"), code=5)
        self.assertIn("stopped part way", p.stderr)
        self.assertIn("OPS-10 is In Progress with CLAUDE's claim label", p.stderr)
        self.assertNotIn("was not applied", p.stderr)
        iss = self.issue_by_ident("OPS-10")
        self.assertEqual(
            (iss["state"], "claimed:CLAUDE" in self.label_names(iss)), ("In Progress", True)
        )
        self.assertEqual(self.claimed_comments("OPS-10"), [])
        self.mock.limit_ops.clear()
        p = self.board(
            "claim", "OPS-10", "--by", "CLAUDE", extra=self.SESSION
        )  # the rerun finishes it
        self.assertTrue(p.stdout.startswith("claimed "), p.stdout)
        self.assertEqual(len(self.claimed_comments("OPS-10")), 1)
        p = self.board(
            "claim", "OPS-10", "--by", "CLAUDE", extra=self.SESSION
        )  # and a third run adds nothing
        self.assertEqual(len(self.claimed_comments("OPS-10")), 1)
        self.assertIn("already on OPS-10", p.stderr)

    def test_status_stopped_before_its_comment(self):
        p = self.board(
            "status",
            "OPS-10",
            "completed",
            "--resolution",
            "Landed in #5.",
            extra=self.stop("BoardComment"),
            code=5,
        )
        self.assertIn("OPS-10 moved to Done", p.stderr)
        self.assertEqual(self.issue_by_ident("OPS-10")["state"], "Done")
        self.mock.limit_ops.clear()
        for _ in range(2):
            self.board(
                "status", "OPS-10", "completed", "--resolution", "Landed in #5.", extra=self.SESSION
            )
        done = [b for b in self.comments_of(self.issue_by_ident("OPS-10")) if "Landed in #5." in b]
        self.assertEqual(len(done), 1)

    def test_file_prints_the_id_before_its_attachments(self):
        n = len(self.mock.issues)
        p = self.board(
            "file",
            "--title",
            "Needs an attachment",
            "--url",
            "https://github.com/o/r/issues/9",
            extra=self.stop("BoardAttach"),
            code=5,
        )
        m = re.match(
            r"filed ([0-9a-f-]{36})  \(-- OPS\)  \(sessions: "
            + self.SESSION["CLAUDE_CODE_SESSION_ID"]
            + r"\)  (OPS-\d+)$",
            p.stdout.strip(),
        )
        self.assertTrue(m, p.stdout)
        self.assertEqual(len(self.mock.issues), n + 1)
        self.assertIn(f"filed {m.group(2)} ({m.group(1)})", p.stderr)
        self.assertIn("do not file it again", p.stderr)
        self.assertNotIn("was not applied", p.stderr)

    def test_comment_rerun_is_a_no_op_and_other_words_post(self):
        args = ("comment", "OPS-10", "--by", "CLAUDE", "--text", "Verified on main.")
        before = len(self.comments_of(self.issue_by_ident("OPS-10")))
        self.board(*args, extra=self.SESSION)
        p = self.board(*args, extra=self.SESSION)
        self.assertEqual(
            p.stdout.strip(),
            f"commented on {B_A[:8]} as CLAUDE  (sessions: {self.SESSION['CLAUDE_CODE_SESSION_ID']})  OPS-10",
        )
        self.assertIn("not posting it again", p.stderr)
        self.assertEqual(len(self.comments_of(self.issue_by_ident("OPS-10"))), before + 1)
        self.board(
            *args, extra={"CLAUDE_CODE_SESSION_ID": "0ther000-0000-4000-8000-000000000000"}
        )  # new session
        self.board(
            "comment", "OPS-10", "--by", "CLAUDE", "--text", "Different words.", extra=self.SESSION
        )
        self.assertEqual(len(self.comments_of(self.issue_by_ident("OPS-10"))), before + 3)

    def test_comment_ids_are_fixed_per_issue_seat_verb_and_text(self):
        cli = load_cli()
        a = cli.comment_id("i1", "CLAUDE", "comment", "body", ["s1"])
        self.assertEqual(a, cli.comment_id("i1", "CLAUDE", "comment", "body", ["s1"]))
        for other in (
            cli.comment_id("i2", "CLAUDE", "comment", "body", ["s1"]),
            cli.comment_id("i1", "CODEX", "comment", "body", ["s1"]),
            cli.comment_id("i1", "CLAUDE", "status", "body", ["s1"]),
            cli.comment_id("i1", "CLAUDE", "comment", "other", ["s1"]),
            cli.comment_id("i1", "CLAUDE", "comment", "body", ["s2"]),
        ):
            self.assertNotEqual(a, other)
        self.assertEqual(str(uuid.UUID(a)), a)


class DuplicateTargetTests(BoardTest):
    def test_duplicate_without_a_target_is_refused_with_the_way_out(self):
        p = self.board("status", "ST-1", "duplicate", code=2)
        self.assertIn("needs --of", p.stderr)
        self.assertIn("wontfix", p.stderr)
        p = self.board(
            "status", "ST-1", "duplicate", "--resolution", "same bug as the commit deadbeef", code=2
        )
        self.assertIn("needs --of", p.stderr)
        self.assertEqual(self.mock.relations, [])

    def test_target_named_in_the_resolution(self):
        p = self.board(
            "status", "ST-1", "duplicate", "--resolution", "Dup of ops-10: same digest bug."
        )
        self.assertEqual(p.stdout.strip(), f"{B_C[:8]} -> duplicate{TAIL}  ST-1")
        self.assertIn("taking --of OPS-10", p.stderr)
        rel = self.mock.relations[-1]
        self.assertEqual((rel["issue"], rel["related"]), (hy(B_C), hy(B_A)))
        self.assertIn(
            "Resolution (duplicate of OPS-10): Dup of ops-10: same digest bug.",
            self.comments_of(self.issue_by_ident("ST-1"))[-1],
        )

    def test_target_forms(self):
        cli = load_cli()
        for text, want in (
            ("duplicate of AFC-3", "AFC-3"),
            ("DUP OF CT-7.", "CT-7"),
            ("dup: `OPS-10`", "OPS-10"),
            ("dupe of ST-1 (see PR)", "ST-1"),
            (f"duplicate of {hy(B_A)}", hy(B_A)),
            (hy(B_A), hy(B_A)),
            ("OPS-10", "OPS-10"),
            ("dup of OPS-10, dup of OPS-10", "OPS-10"),
            ("duplicate of 236a2fa7e2ab4d24be03b0414750e0d8", B_A),
        ):
            self.assertEqual(cli.duplicate_target(text), want, text)
        for text in (
            "",
            "deadbeef",
            "duplicate of deadbeef",
            "fixed in a1b2c3d4",
            "duplicated by OPS-10",
            "dup of OPS-10 and dup of ST-1",
            "dup of PR-123",
            "see OPS-10",
            "same as OPS-10",
            "dupont-5",
        ):
            self.assertIsNone(cli.duplicate_target(text), text)


class RelabelTests(BoardTest):
    """relabel_claims.py:  the owner-approved cutover step that gives imported live claims their marker."""

    def setUp(self):
        super().setUp()
        mk = lambda t, line, st, labels: self.mock.add_issue(
            str(uuid.uuid4()),
            "AFC",
            st,
            t,
            3,
            ["Kind/agent-report", "imported", *labels],
            "2026-10-05T12:00:00Z",
            footer(line),
        )
        self.a = mk(
            "Claimed by codex",
            "Reported by claude, addressed by codex",
            "In Progress",
            ("seat:CLAUDE", "seat:CODEX"),
        )
        self.b = mk("In review by claude", "Addressed by claude", "In Review", ("seat:CLAUDE",))
        self.c = mk(
            "Nobody", "Reported by claude, addressed by jay", "In Progress", ("seat:CLAUDE",)
        )
        self.d = mk(
            "Done long ago",
            "Reported by claude, addressed by codex",
            "Done",
            ("seat:CLAUDE", "seat:CODEX"),
        )
        self.labels_before = {i["id"]: list(i["labels"]) for i in self.mock.issues.values()}

    def relabel(self, *args, extra=None, drop=(), code=0):
        e = dict(self.env, **(extra or {}))
        for k in drop:
            e.pop(k, None)
        # The harness plays the trusted launcher:  a write needs a bearer-verified seat,  so a run is bound
        # to a minted bearer and its verify route.  A test that wants no seat drops the locator.
        if "SIMPLE_BOARD_BEARER_LOCATOR" not in drop and "SIMPLE_BOARD_BEARER_LOCATOR" not in e:
            e["SIMPLE_BOARD_BEARER_LOCATOR"] = self.bearer_locator_for("CLAUDE")
        if "SIMPLE_BOARD_BEARER_VERIFY_URL" not in drop and "SIMPLE_BOARD_BEARER_VERIFY_URL" not in e:
            e["SIMPLE_BOARD_BEARER_VERIFY_URL"] = self.verify_url
        p = subprocess.run(
            [PY, "-c", RELABEL_BOOTSTRAP, str(RELABEL), *args],
            env=e, capture_output=True, text=True, timeout=60
        )
        self.assertEqual(
            p.returncode, code, f"exit {p.returncode}\nstdout:\n{p.stdout}\nstderr:\n{p.stderr}"
        )
        for tok in self.mock.tokens:
            self.assertNotIn(tok, p.stdout + p.stderr)
        return p

    def test_dry_run_writes_nothing(self):
        p = self.relabel()
        self.assertIn("2 issue(s) to mark, 1 already marked, 1 with no claimant", p.stdout)
        self.assertIn(f"{self.a['identifier']}", p.stdout)
        self.assertIn("claimed:CODEX", p.stdout)
        self.assertIn("dry run: nothing written", p.stdout)
        self.assertEqual(
            {i["id"]: i["labels"] for i in self.mock.issues.values()}, self.labels_before
        )
        self.assertNotIn("BoardUpdate", self.mock.ops)

    def test_apply_needs_a_bearer_verified_seat(self):
        p = self.relabel("--apply", drop=("SIMPLE_BOARD_BEARER_LOCATOR",), code=6)
        self.assertIn("bearer-verified seat", p.stderr)
        self.assertEqual(self.mock.ops, [])

    def test_apply_is_additive_idempotent_and_resumable(self):
        p = self.relabel("--apply", "--limit", "1")
        self.assertIn("stopped at --limit 1", p.stdout)
        self.assertEqual(self.mock.ops.count("BoardUpdate"), 1)
        p = self.relabel("--apply")
        self.assertIn("1 issue(s) to mark, 2 already marked", p.stdout)
        names = lambda i: self.label_names(i)
        self.assertIn("claimed:CODEX", names(self.a))
        self.assertIn("claimed:CLAUDE", names(self.b))
        self.assertEqual([n for n in names(self.c) if n.startswith("claimed:")], [])
        self.assertEqual(
            [n for n in names(self.d) if n.startswith("claimed:")], []
        )  # closed rows are not touched
        for i in (self.a, self.b, self.c):
            self.assertTrue(set(self.labels_before[i["id"]]) <= set(i["labels"]))  # nothing removed
        n_updates = self.mock.ops.count("BoardUpdate")
        self.relabel("--apply")
        self.assertEqual(self.mock.ops.count("BoardUpdate"), n_updates)
        # the board now sees the claim without reading any footer
        rows = json.loads(self.board("list", "--json", "--mine", "CODEX").stdout)
        self.assertEqual(
            sorted(r["identifier"] for r in rows), sorted(["OPS-11", self.a["identifier"]])
        )

    def test_uses_the_fleet_credentials_only(self):
        self.creds.unlink()
        p = self.relabel(code=4)
        self.assertIn("no Fleet credentials", p.stderr)
        self.assertEqual((self.mock.mints, self.mock.ops), (0, []))


WRITE_OPS = {
    "BoardFile",
    "BoardUpdate",
    "BoardComment",
    "BoardRelate",
    "BoardAttach",
    "BoardLabelCreate",
}


class SeatAppTests(BoardTest):
    """One Linear app per seat (owner 2026-10-10):  credentials by seat, a token cache and a lock per app,
    the Fleet fallback, the cloud runtime's variables, and the swapped-credentials check."""

    def seat_app(self, seat, name, cid=None, secret=None, register=True, file=True):
        """Register a seat app with the mock and, unless file=False, put its pair in the fake Fleet-OPS /linear."""
        cid = cid or f"{seat.lower()}-app-client-id"
        secret = secret or f"{seat.lower()}-app-client-secret-value"
        if register:
            self.mock.add_client(cid, secret, name)
        if file:
            k = seat.replace("-", "_")
            self.ops_put(f"{k}_LINEAR_CLIENT_ID", cid)
            self.ops_put(f"{k}_LINEAR_CLIENT_SECRET", secret)
        return cid, secret

    def cache(self, seat):
        return self.ops_vault / ("CACHE_" + seat.replace("-", "_"))

    def as_seat(self, seat, *args, extra=None, **kw):
        return self.board(*args, extra=dict(extra or {}, AGENT_SEAT=seat), **kw)

    def callers_since(self, n):
        return {c for _, c in self.mock.calls[n:]}

    def test_secrets_are_read_one_named_key_at_a_time_never_by_dumping_the_store(self):
        self.seat_app("CLAUDE", "Claude")
        p = self.as_seat("CLAUDE", "whoami")
        self.assertIn("identity:    Claude (the CLAUDE seat's own Linear app)", p.stdout)
        self.assertFalse(
            (self.ops_vault / ".run-called").exists(), "board called `fleet-secrets run`"
        )

    def test_two_seats_use_their_own_app_token_and_cache(self):
        claude, _ = self.seat_app("CLAUDE", "Claude")
        codex, _ = self.seat_app("CODEX", "Codex")
        p = self.as_seat("CLAUDE", "whoami")
        self.assertIn("identity:    Claude (the CLAUDE seat's own Linear app)", p.stdout)
        self.assertIn("viewer:      Claude (app user)", p.stdout)
        self.assertIn(
            "credentials: Infisical Fleet-OPS prod /linear (CLAUDE_LINEAR_CLIENT_ID, CLAUDE_LINEAR_CLIENT_SECRET)",
            p.stdout,
        )
        self.assertIn("token store: Infisical Fleet-OPS prod /linear CACHE_CLAUDE", p.stdout)
        self.assertNotIn("notice:", p.stdout)
        self.assertNotIn(claude, p.stdout + p.stderr)  # the path is printed, never a value
        p = self.as_seat("CODEX", "whoami")
        self.assertIn("identity:    Codex (the CODEX seat's own Linear app)", p.stdout)
        self.assertIn("viewer:      Codex (app user)", p.stdout)
        # switching seats back and forth never mints again and never borrows the other seat's token
        for seat, cid in (
            ("CLAUDE", claude),
            ("CODEX", codex),
            ("CLAUDE", claude),
            ("CODEX", codex),
        ):
            n = len(self.mock.calls)
            self.as_seat(seat, "list")
            self.assertEqual(self.callers_since(n), {cid}, seat)
        self.assertEqual(self.mock.mints_by, {claude: 1, codex: 1})
        self.assertEqual(self.mock.scopes, [EXPECTED_SCOPE, EXPECTED_SCOPE])
        self.assertFalse(self.token_cache.exists(), "a seat app wrote the Fleet cache")
        a, b = (json.loads(self.cache(s).read_text()) for s in ("CLAUDE", "CODEX"))
        self.assertNotEqual(a["access_token"], b["access_token"])
        self.assertNotEqual(a["cred_fp"], b["cred_fp"])

    def test_seat_app_writes_keep_the_seat_label_and_comment(self):
        claude, _ = self.seat_app("CLAUDE", "Claude")
        n = len(self.mock.calls)
        p = self.as_seat("CLAUDE", "claim", B_A[:8], "--by", "CLAUDE", "--session", "b854d9ad-0000")
        self.assertIn(f"claimed {B_A[:8]} by CLAUDE -> in_progress", p.stdout)
        iss = self.mock.issues[hy(B_A)]
        self.assertIn("claimed:CLAUDE", self.label_names(iss))
        self.assertIn("seat:CLAUDE", self.label_names(iss))
        self.assertTrue(self.comments_of(iss)[-1].startswith("[CLAUDE·b854d9ad] CLAIMED"))
        self.assertEqual(self.callers_since(n), {claude})
        self.assertEqual(self.mock.ops.count("BoardWhoami"), 1)  # identity confirmed once per token
        self.as_seat("CLAUDE", "comment", B_A[:8], "--by", "CLAUDE", "--text", "second write")
        self.assertEqual(self.mock.ops.count("BoardWhoami"), 1)  # kept in the token's cache entry
        self.assertEqual(self.mock.mints_by, {claude: 1})
        self.assertEqual(json.loads(self.cache("CLAUDE").read_text())["viewer"]["name"], "Claude")

    def test_seat_with_no_app_falls_back_to_fleet(self):
        self.seat_app("CLAUDE", "Claude")
        p = self.as_seat("CODEX", "whoami")
        self.assertIn("identity:    Fleet (the shared app; fallback for CODEX)", p.stdout)
        self.assertIn("viewer:      Fleet (app user)", p.stdout)
        self.assertIn(
            "credentials: Infisical Fleet-OPS prod /linear (FLEET_LINEAR_CLIENT_ID, FLEET_LINEAR_CLIENT_SECRET)",
            p.stdout,
        )
        self.assertIn("token store: Infisical Fleet-OPS prod /linear CACHE_FLEET", p.stdout)
        notices = [ln for ln in p.stdout.splitlines() if ln.startswith("notice:")]
        self.assertEqual(len(notices), 1, p.stdout)
        self.assertIn(
            "CODEX has no Linear app yet (no CODEX_LINEAR_CLIENT_ID in Fleet-OPS /linear)",
            notices[0],
        )
        n = len(self.mock.calls)
        p = self.as_seat(
            "CODEX", "comment", B_A[:8], "--by", "CODEX", "--text", "a Fleet fallback write"
        )
        self.assertIn("as CODEX", p.stdout)
        self.assertEqual(self.callers_since(n), {CLIENT_ID})
        self.assertNotIn(
            "BoardWhoami", [o for o, _ in self.mock.calls[n:]]
        )  # the Fleet path is unchanged
        # reads without AGENT_SEAT use Fleet, even with a seat pair present
        n = len(self.mock.calls)
        p = self.board("whoami", drop=("AGENT_SEAT",))
        self.assertIn("identity:    Fleet (the shared app)\n", p.stdout)
        self.assertNotIn("notice:", p.stdout)
        self.board("list", drop=("AGENT_SEAT",))
        self.assertEqual(self.callers_since(n), {CLIENT_ID})
        # a seat outside the app table never reads a key of its name.  GB-HOUSEKEEPER is the live
        # example:  it is in SEAT_ROSTER, and it has no SEAT_APP_NAMES entry, so resolve_identity
        # must fall back to Fleet without ever reading GB_HOUSEKEEPER_LINEAR_CLIENT_ID.
        cli = load_cli()
        self.assertIn("GB-HOUSEKEEPER", cli.SEAT_ROSTER)
        self.assertNotIn("GB-HOUSEKEEPER", cli.SEAT_APP_NAMES)
        self.ops_put("GB_HOUSEKEEPER_LINEAR_CLIENT_ID", "gb-housekeeper-id")
        self.ops_put("GB_HOUSEKEEPER_LINEAR_CLIENT_SECRET", "gb-housekeeper-secret")
        p = self.as_seat("GB-HOUSEKEEPER", "whoami")
        self.assertIn(
            "notice:      GB-HOUSEKEEPER has no Linear app yet (none in the seat table)", p.stdout
        )
        self.assertIn(
            "credentials: Infisical Fleet-OPS prod /linear (FLEET_LINEAR_CLIENT_ID, FLEET_LINEAR_CLIENT_SECRET)",
            p.stdout,
        )
        self.assertNotIn("GB_HOUSEKEEPER_LINEAR", p.stdout)
        self.assertEqual(self.mock.mints_by, {CLIENT_ID: 1})

    def test_botfleet_producer_uses_its_own_app_not_fleet(self):
        name = load_cli().SEAT_APP_NAMES["BF-PRODUCER"]
        producer, _ = self.seat_app("BF-PRODUCER", name)
        p = self.as_seat("BF-PRODUCER", "whoami")
        self.assertIn(f"identity:    {name} (the BF-PRODUCER seat's own Linear app)", p.stdout)
        self.assertIn(
            "credentials: Infisical Fleet-OPS prod /linear (BF_PRODUCER_LINEAR_CLIENT_ID, BF_PRODUCER_LINEAR_CLIENT_SECRET)",
            p.stdout,
        )
        self.assertNotIn("notice:", p.stdout)
        self.assertNotIn("fallback", p.stdout)
        self.assertEqual(self.mock.mints_by, {producer: 1})

    def test_grok_web_hyphen_and_grok_build_alias(self):
        gw, gw_sec = self.seat_app("GROK-WEB", "Grok Web")
        p = self.as_seat("GROK-WEB", "whoami")
        self.assertIn("identity:    Grok Web (the GROK-WEB seat's own Linear app)", p.stdout)
        self.assertIn(
            "credentials: Infisical Fleet-OPS prod /linear (GROK_WEB_LINEAR_CLIENT_ID, GROK_WEB_LINEAR_CLIENT_SECRET)",
            p.stdout,
        )
        self.assertIn("token store: Infisical Fleet-OPS prod /linear CACHE_GROK_WEB", p.stdout)
        # the environment form spells the hyphen as an underscore
        self.ops_drop_seat("GROK-WEB")
        self.cache("GROK-WEB").unlink()
        p = self.as_seat(
            "GROK-WEB",
            "whoami",
            extra={"LINEAR_GROK_WEB_CLIENT_ID": gw, "LINEAR_GROK_WEB_CLIENT_SECRET": gw_sec},
        )
        self.assertIn(
            "credentials: environment (LINEAR_GROK_WEB_CLIENT_ID, LINEAR_GROK_WEB_CLIENT_SECRET)",
            p.stdout,
        )
        self.assertIn("viewer:      Grok Web (app user)", p.stdout)
        self.assertIn("token store: Infisical Fleet-OPS prod /linear CACHE_GROK_WEB", p.stdout)
        # every GROK-BUILD spelling is GROK, and reads GROK_LINEAR_CLIENT_ID
        self.seat_app("GROK", "Grok")
        p = self.as_seat("Grok-Build", "whoami")
        self.assertIn("identity:    Grok (the GROK seat's own Linear app)", p.stdout)
        self.assertIn(
            "credentials: Infisical Fleet-OPS prod /linear (GROK_LINEAR_CLIENT_ID, GROK_LINEAR_CLIENT_SECRET)",
            p.stdout,
        )

    def test_seat_credentials_from_the_environment(self):
        codex, sec = self.seat_app("CODEX", "Codex", file=False)
        envs = {"LINEAR_CODEX_CLIENT_ID": codex, "LINEAR_CODEX_CLIENT_SECRET": sec}
        p = self.as_seat("CODEX", "whoami", extra=envs)
        self.assertIn("identity:    Codex (the CODEX seat's own Linear app)", p.stdout)
        self.assertIn(
            "credentials: environment (LINEAR_CODEX_CLIENT_ID, LINEAR_CODEX_CLIENT_SECRET)",
            p.stdout,
        )
        self.assertIn("token store: Infisical Fleet-OPS prod /linear CACHE_CODEX", p.stdout)
        self.as_seat(
            "CODEX",
            "comment",
            B_A[:8],
            "--by",
            "CODEX",
            "--text",
            "from the environment",
            extra=envs,
        )
        # another seat's variables are not this seat's
        p = self.as_seat("CLAUDE", "whoami", extra=envs)
        self.assertIn("viewer:      Fleet (app user)", p.stdout)
        # the Fleet-OPS pair comes before the seat's variables
        self.seat_app("CODEX", "Codex", cid=codex, secret=sec, register=False)
        p = self.as_seat("CODEX", "whoami", extra=envs)
        self.assertIn(
            "credentials: Infisical Fleet-OPS prod /linear (CODEX_LINEAR_CLIENT_ID, CODEX_LINEAR_CLIENT_SECRET)",
            p.stdout,
        )
        self.assertEqual(self.mock.mints_by, {codex: 1, CLIENT_ID: 1})

    def test_cloud_pair_wins_and_may_hold_fleet_or_the_seat_app(self):
        claude, csec = self.seat_app("CLAUDE", "Claude")
        fleet_pair = {"LINEAR_CLIENT_ID": CLIENT_ID, "LINEAR_CLIENT_SECRET": CLIENT_SECRET}
        p = self.as_seat("CLAUDE", "whoami", extra=fleet_pair)  # it wins over the Fleet-OPS pair
        self.assertIn("credentials: environment (LINEAR_CLIENT_ID, LINEAR_CLIENT_SECRET)", p.stdout)
        self.assertIn(
            "identity:    Fleet (the shared app, from the environment; fallback for CLAUDE)",
            p.stdout,
        )
        self.assertIn(
            "notice:      the environment's LINEAR_CLIENT_ID and LINEAR_CLIENT_SECRET are the shared Fleet app's",
            p.stdout,
        )
        self.as_seat(
            "CLAUDE",
            "comment",
            B_A[:8],
            "--by",
            "CLAUDE",
            "--text",
            "cloud Fleet pair",
            extra=fleet_pair,
        )
        p = self.as_seat(
            "CLAUDE", "whoami", extra={"LINEAR_CLIENT_ID": claude, "LINEAR_CLIENT_SECRET": csec}
        )
        self.assertIn(
            "identity:    Claude (the CLAUDE seat's own Linear app, from the environment)", p.stdout
        )
        self.assertNotIn("notice:", p.stdout)
        self.as_seat(
            "CLAUDE",
            "comment",
            B_A[:8],
            "--by",
            "CLAUDE",
            "--text",
            "cloud seat pair",
            extra={"LINEAR_CLIENT_ID": claude, "LINEAR_CLIENT_SECRET": csec},
        )
        # the pair holding another seat's app may read but not write
        codex, xsec = self.seat_app("CODEX", "Codex", file=False)
        wrong = {"LINEAR_CLIENT_ID": codex, "LINEAR_CLIENT_SECRET": xsec}
        before = [o for o in self.mock.ops if o in WRITE_OPS]
        p = self.as_seat(
            "CLAUDE",
            "comment",
            B_A[:8],
            "--by",
            "CLAUDE",
            "--text",
            "wrong app",
            extra=wrong,
            code=4,
        )
        self.assertIn("the 'Codex' app, not 'Claude' or 'Fleet'", p.stderr)
        self.assertEqual([o for o in self.mock.ops if o in WRITE_OPS], before)
        self.as_seat("CLAUDE", "list", extra=wrong)
        self.as_seat("CLAUDE", "whoami", extra=wrong, code=6)

    def test_swapped_seat_credentials_refuse_writes_and_reads_work(self):
        codex, sec = self.seat_app("CODEX", "Codex", file=False)
        self.seat_app(
            "CLAUDE", "Claude", cid=codex, secret=sec, register=False
        )  # CLAUDE's pair holds Codex's
        self.assertIn("shown of", self.as_seat("CLAUDE", "list").stdout)
        self.as_seat("CLAUDE", "show", "OPS-10")
        self.assertIn("total 9", self.as_seat("CLAUDE", "stats").stdout)
        n_ops = len(self.mock.ops)
        for args in (
            ["comment", B_A[:8], "--by", "CLAUDE", "--text", "x"],
            ["claim", B_C[:8], "--by", "CLAUDE"],
            ["status", B_A[:8], "completed"],
            ["file", "--title", "x"],
        ):
            p = self.as_seat("CLAUDE", *args, code=4)
            self.assertIn("refusing to write as CLAUDE", p.stderr)
            self.assertIn("belong to the 'Codex' app, not 'Claude'", p.stderr)
            self.assertIn("look swapped", p.stderr)
            self.assertIn("Reads still work", p.stderr)
            # Every exit-4 line names the variable the operator has to fix, so a caller
            # downstream never has to fall back to a nameless "no credentials".
            self.assertIn("LINEAR_CLIENT_ID", p.stderr)
            self.assertIn("LINEAR_CLIENT_SECRET", p.stderr)
        self.assertEqual([o for o in self.mock.ops if o in WRITE_OPS], [])
        self.assertEqual(self.mock.ops[n_ops:], ["BoardWhoami"])  # asked once, kept with the token
        p = self.as_seat("CLAUDE", "whoami", code=6)
        self.assertIn("viewer:      Codex (app user)", p.stdout)
        self.assertIn("look swapped", p.stderr)

    def test_renamed_app_names_are_accepted(self):
        # the owner renames seat apps in Linear (Antigravity, Antigravity (AG), OC, FX.sh, Grok (Build) ...);
        # the seat tags stay as they are and none of those renames may lock a seat out of writing
        names = {
            "AG": "Antigravity (AG)",
            "MM": "MiniMax",
            "FX": "FX.sh",
            "JET": "Jet (OpenAI dot)",
            "MC": "Muse Code (MC)",
            "MA": "Muse Assist (MA)",
            "GROK": "Grok (Build)",
            "GROK-WEB": "Grok (Web/iOS)",
            "OPENCODE": "OC",
            "ECHO": "Echo (Instinct)",
            "WISE": "Wise (Instinct)",
        }
        for seat, name in names.items():
            self.seat_app(seat, name)
        for seat in names:
            self.as_seat(seat, "comment", B_A[:8], "--by", seat, "--text", "alias name is accepted")
            self.assertIn("(app user)", self.as_seat(seat, "whoami").stdout)
        # whoami names the app Linear reports, not the primary name from the table
        self.assertIn(
            f"identity:    {names['AG']} (the AG seat's own Linear app)",
            self.as_seat("AG", "whoami").stdout,
        )
        # Legacy and display aliases resolve to WISE, and sign as WISE with Wise's app.
        self.as_seat("WISE", "comment", B_A[:8], "--by", "WISE", "--text", "WISE is INSTINCT")
        self.assertIn("seat:        WISE (bearer-verified)", self.as_seat("WISE", "whoami").stdout)
        for alias in ("Wise-Instinct", "Echo-Instinct", "Instinct"):
            self.as_seat(
                alias, "comment", B_A[:8], "--by", alias, "--text", "alias resolves to WISE"
            )
            self.assertIn(
                "seat:        WISE (bearer-verified)", self.as_seat(alias, "whoami").stdout
            )
        for alias in ("Muse-Assist", "Rob", "Rob-Muse"):
            self.as_seat(alias, "comment", B_A[:8], "--by", alias, "--text", "alias resolves to MA")
            self.assertIn("seat:        MA (bearer-verified)", self.as_seat(alias, "whoami").stdout)
        # OpenCode and OC are interchangeable, and case never matters
        self.seat_app("OPENCODE", "OpenCode")
        self.as_seat(
            "OPENCODE", "comment", B_A[:8], "--by", "OPENCODE", "--text", "primary name is accepted"
        )
        self.seat_app("OPENCODE", "oc")
        self.as_seat(
            "OPENCODE", "comment", B_A[:8], "--by", "OPENCODE", "--text", "case is ignored"
        )
        # a wrong name is still refused
        self.seat_app("CLAUDE", "Antigravity")
        self.assertIn(
            "look swapped",
            self.as_seat(
                "CLAUDE", "comment", B_A[:8], "--by", "CLAUDE", "--text", "x", code=4
            ).stderr,
        )

    # The one deliberate share:  two Jet logins, one Linear app (owner,
    # Oct 23 2026 - scripts/agent-sync-mcp/src/linear.js).  JET is wake-only
    # with no tool surface, so no credential can swap unseen.
    SHARED_APP_NAME_SEATS = (frozenset({"JET", "JET-CHAT"}),)

    def test_accepted_app_names_belong_to_one_seat(self):
        # a name two seats share would let their credentials be swapped without the check noticing
        cli = load_cli()
        owner = {}
        for seat in cli.SEAT_APP_NAMES:
            for name in {n.casefold() for n in (*cli.seat_app_names(seat), seat)}:
                previous = owner.get(name.casefold())
                if previous is not None:
                    self.assertTrue(
                        any({previous, seat} <= shared for shared in self.SHARED_APP_NAME_SEATS),
                        f"{name!r} is accepted for both {previous} and {seat}",
                    )
                owner[name.casefold()] = seat
        self.assertEqual(set(cli.SEAT_APP_ALIASES) - set(cli.SEAT_APP_NAMES), set())

    def test_remint_rules_apply_per_cache_file(self):
        claude, _ = self.seat_app("CLAUDE", "Claude")
        codex, _ = self.seat_app("CODEX", "Codex")
        self.as_seat("CLAUDE", "whoami")
        self.as_seat("CODEX", "whoami")
        self.mock.tokens.clear()  # Linear refuses both tokens
        p = self.as_seat("CLAUDE", "list", code=6)
        self.assertIn("Linear refused the stored Claude seat app token", p.stderr)
        self.assertIn("under 24 hours ago", p.stderr)
        # CLAUDE's mint is two days old, so it mints again; CODEX's is not, so CODEX still refuses
        c = json.loads(self.cache("CLAUDE").read_text())
        c["minted_at"] -= 2 * 86400
        self.cache("CLAUDE").write_text(json.dumps(c))
        self.as_seat("CLAUDE", "whoami")
        self.assertIn("under 24 hours ago", self.as_seat("CODEX", "list", code=6).stderr)
        self.assertEqual(self.mock.mints_by, {claude: 2, codex: 1})
        # rotating CODEX's credentials mints for CODEX alone, and CLAUDE's token stays put
        claude_tok = json.loads(self.cache("CLAUDE").read_text())["access_token"]
        self.mock.clients[codex]["secret"] = "codex-rotated-secret-value"
        self.seat_app(
            "CODEX", "Codex", cid=codex, secret="codex-rotated-secret-value", register=False
        )
        self.as_seat("CODEX", "list")
        self.as_seat("CLAUDE", "list")
        self.assertEqual(self.mock.mints_by, {claude: 2, codex: 2})
        self.assertEqual(json.loads(self.cache("CLAUDE").read_text())["access_token"], claude_tok)
        self.assertFalse(self.token_cache.exists())

    def test_concurrent_runs_with_an_expired_blob_both_succeed(self):
        """No lock any more:  two runs that both see an expired blob each mint and write;  last writer wins and
        both work, and a third run uses whichever blob is stored."""
        self.seat_app("CLAUDE", "Claude")
        self.board("whoami", extra={"AGENT_SEAT": "CLAUDE"})
        blob = json.loads(self.cache("CLAUDE").read_text())
        blob["expires_at"] = 1.0
        blob["minted_at"] -= 3 * 86400
        self.cache("CLAUDE").write_text(json.dumps(blob))
        procs = [
            subprocess.Popen(
                [PY, "-c", CLI_BOOTSTRAP, str(CLI), "list"],
                env=self.seated_env("CLAUDE"),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for _ in range(2)
        ]
        outs = [pr.communicate(timeout=60) for pr in procs]
        self.assertEqual([pr.returncode for pr in procs], [0, 0], outs)
        self.assertGreaterEqual(self.mock.mints, 2)
        stored = json.loads(self.cache("CLAUDE").read_text())
        self.assertIn(stored["access_token"], self.mock.tokens)
        n = self.mock.mints
        self.as_seat("CLAUDE", "list")
        self.assertEqual(self.mock.mints, n, "the stored blob was used")

    def test_incomplete_seat_pair_uses_fleet_and_no_credentials_names_both(self):
        self.ops_put("CLAUDE_LINEAR_CLIENT_ID", "only-the-id")
        p = self.as_seat("CLAUDE", "whoami")
        self.assertIn("identity:    Fleet (the shared app; fallback for CLAUDE)", p.stdout)
        # with no Fleet pair either, the message names both
        self.ops_drop_seat("CLAUDE")
        self.creds.unlink()
        p = self.as_seat("CLAUDE", "list", code=4)
        self.assertIn("no Fleet credentials", p.stderr)
        self.assertIn("CLAUDE has no app credentials of its own", p.stderr)
        self.assertIn("Infisical Fleet-OPS /linear", p.stderr)

    def test_nothing_is_read_from_the_secrets_folder(self):
        """AFC-359:  a decoy secrets folder under HOME is never opened, and no token cache lands there."""
        home = self.tmp / "home"
        decoy = home / ("." + "secrets") / "Linear"
        (decoy / "cache").mkdir(parents=True)
        for name in ("Fleet.env", "CLAUDE.env"):
            (decoy / name).write_text("LINEAR_CLIENT_ID=decoy\nLINEAR_CLIENT_SECRET=decoy\n")
        before = sorted(str(q.relative_to(home)) for q in (home / ("." + "secrets")).rglob("*"))
        self.seat_app("CLAUDE", "Claude")
        p = self.as_seat("CLAUDE", "whoami", drop=())
        self.assertIn(
            "credentials: Infisical Fleet-OPS prod /linear (CLAUDE_LINEAR_CLIENT_ID", p.stdout
        )
        self.assertIn("token store: Infisical Fleet-OPS prod /linear CACHE_CLAUDE", p.stdout)
        after = sorted(str(q.relative_to(home)) for q in (home / ("." + "secrets")).rglob("*"))
        self.assertEqual(after, before)
        self.assertEqual(
            sorted(
                str(q.relative_to(home))
                for q in home.rglob("*")
                if q.is_file() and ".secrets" not in str(q)
            ),
            [],
        )  # no token file, no cache, nothing

    def test_relabel_stays_on_the_fleet_app(self):
        self.seat_app("CLAUDE", "Claude")
        n = len(self.mock.calls)
        p = subprocess.run(
            [PY, str(RELABEL)], env=dict(self.env), capture_output=True, text=True, timeout=60
        )
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(self.callers_since(n), {CLIENT_ID})

    def test_no_token_or_secret_reaches_output(self):
        claude, sec = self.seat_app("CLAUDE", "Claude")
        self.mock.clients[claude]["secret"] = "rotated-in-linear-not-in-the-file"
        p = self.as_seat("CLAUDE", "whoami", code=6)
        self.assertIn("minting the Claude seat app token failed", p.stderr)
        self.assertIn("http=401", p.stderr)
        for needle in (sec, claude, "rotated-in-linear-not-in-the-file"):
            self.assertNotIn(needle, p.stdout + p.stderr)
        self.mock.clients[claude]["secret"] = sec
        for args in (
            ["whoami"],
            ["list"],
            ["show", "OPS-10"],
            ["comment", B_A[:8], "--by", "CLAUDE", "--text", "x"],
        ):
            p = self.as_seat("CLAUDE", *args)
            tok = json.loads(self.cache("CLAUDE").read_text())["access_token"]
            for needle in (sec, claude, tok, CLIENT_SECRET):
                self.assertNotIn(needle, p.stdout + p.stderr, args)


class FileInputTests(BoardTest):
    """--desc-file, --fix-file and --text-file keep free text out of argv.  A bad input exits 2 before any
    credential is read or request is sent."""

    def untouched(self):
        self.assertEqual(self.mock.ops, [])
        self.assertEqual(self.mock.mints, 0)

    def test_description_and_fix_round_trip_from_files(self):
        desc = self.private(
            "desc.md", "First paragraph, with é and — marks.\n\nSecond paragraph.\n"
        )
        fix = self.private(
            "fix.md", "Retry once, then give up.\n", mode=0o400
        )  # 0400 is accepted too
        p = self.board(
            "file", "--title", "From files", "--app", "AFC", "--desc-file", desc, "--fix-file", fix
        )
        d = json.loads(self.board("show", p.stdout.split()[-1], "--json").stdout)
        self.assertEqual(
            d["description"], "First paragraph, with é and — marks.\n\nSecond paragraph."
        )
        self.assertEqual(d["recommended_fix"], "Retry once, then give up.")
        self.assertEqual(d["reported_by"], "CLAUDE")

    def test_description_and_fix_from_standard_input(self):
        p = self.board(
            "file",
            "--title",
            "From stdin",
            "--app",
            "AFC",
            "--desc-file",
            "-",
            stdin="Description from standard input.\n",
        )
        d = json.loads(self.board("show", p.stdout.split()[-1], "--json").stdout)
        self.assertEqual(d["description"], "Description from standard input.")
        desc = self.private("d.md", "Description from a file.\n")
        p = self.board(
            "file",
            "--title",
            "Fix from stdin",
            "--app",
            "AFC",
            "--desc-file",
            desc,
            "--fix-file",
            "-",
            stdin="Fix from standard input.\n",
        )
        d = json.loads(self.board("show", p.stdout.split()[-1], "--json").stdout)
        self.assertEqual(
            (d["description"], d["recommended_fix"]),
            ("Description from a file.", "Fix from standard input."),
        )

    def test_standard_input_cannot_feed_two_flags(self):
        p = self.board(
            "file", "--title", "x", "--desc-file", "-", "--fix-file", "-", stdin="text", code=2
        )
        self.assertIn("only one", p.stderr)
        self.untouched()

    def test_exactly_64_kb_is_accepted_and_one_more_byte_is_not(self):
        ok = self.private("ok.md", "a" * 65536)
        self.board("file", "--title", "Full size", "--app", "AFC", "--desc-file", ok)
        self.board(
            "file",
            "--title",
            "Full size stdin",
            "--app",
            "AFC",
            "--desc-file",
            "-",
            stdin="a" * 65536,
        )
        n = len(self.mock.ops)
        big = self.private("big.md", "a" * 65537)
        p = self.board("file", "--title", "Too big", "--desc-file", big, code=2)
        self.assertIn("more than 64 KB", p.stderr)
        p = self.board(
            "file", "--title", "Too big stdin", "--desc-file", "-", stdin="a" * 65537, code=2
        )
        self.assertIn("more than 64 KB", p.stderr)
        self.assertEqual(len(self.mock.ops), n)

    def test_unsafe_or_unreadable_inputs_are_refused_before_any_request(self):
        entry_points = (
            lambda path: ["file", "--title", "x", "--desc-file", path],
            lambda path: ["file", "--title", "x", "--fix-file", path],
            lambda path: ["comment", B_A[:8], "--by", "CLAUDE", "--text-file", path],
        )
        real = self.private("real.md", "secret words")
        link = self.tmp / "link.md"
        link.symlink_to(real)
        adir = self.tmp / "adir"
        adir.mkdir(mode=0o700)
        fifo = self.tmp / "fifo.md"
        os.mkfifo(fifo, 0o600)
        cases = [
            (self.private("m644.md", "x", 0o644), "mode 0644"),
            (self.private("m640.md", "x", 0o640), "mode 0640"),
            (self.private("m660.md", "x", 0o660), "mode 0660"),
            (self.private("m604.md", "x", 0o604), "mode 0604"),
            (self.private("m666.md", "x", 0o666), "mode 0666"),
            (self.private("m700.md", "x", 0o700), "mode 0700"),
            (self.private("m200.md", "x", 0o200), "cannot open"),
            (str(link), "symbolic link"),
            (str(adir), "not a regular file"),
            (str(fifo), "not a regular file"),
            (str(self.tmp / "missing.md"), "cannot open"),
            (self.private("big.md", b"a" * 65537), "more than 64 KB"),
            (self.private("bad.md", b"caf\xe9 \xff\xfe"), "not valid UTF-8"),
        ]
        for make in entry_points:
            for path, why in cases:
                p = self.board(*make(path), code=2)
                self.assertIn(why, p.stderr, (make(path)[0], path))
                self.assertNotIn("secret words", p.stdout + p.stderr)
        self.untouched()

    def test_a_file_owned_by_someone_else_is_refused(self):
        cli = load_cli()
        path = self.private("mine.md", "text")
        self.assertEqual(cli.read_private_file(path, "--desc-file"), b"text")
        err = io.StringIO()
        with (
            unittest.mock.patch.object(cli.os, "geteuid", return_value=os.geteuid() + 1),
            contextlib.redirect_stderr(err),
            self.assertRaises(SystemExit) as cm,
        ):
            cli.read_private_file(path, "--desc-file")
        self.assertEqual(cm.exception.code, 2)
        self.assertIn("not owned by you", err.getvalue())

    def test_standard_input_that_is_not_utf8_is_refused(self):
        p = subprocess.run(
            [PY, "-c", CLI_BOOTSTRAP, str(CLI), "file", "--title", "x", "--desc-file", "-"],
            env=self.seated_env("CLAUDE"),
            input=b"caf\xe9",
            capture_output=True,
            timeout=60,
        )
        self.assertEqual(p.returncode, 2, p.stderr)
        self.assertIn(b"not valid UTF-8", p.stderr)
        self.untouched()

    def test_a_flag_and_its_file_form_are_exclusive(self):
        f = self.private("f.md", "text")
        for args in (
            ["file", "--title", "x", "--desc", "a", "--desc-file", f],
            ["file", "--title", "x", "--fix", "a", "--fix-file", f],
            ["comment", B_A[:8], "--by", "CLAUDE", "--text", "a", "--text-file", f],
        ):
            p = self.board(*args, code=2)
            self.assertIn("not allowed with", p.stderr)
        p = self.board("comment", B_A[:8], "--by", "CLAUDE", code=2)
        self.assertIn("one of the arguments --text --text-file is required", p.stderr)
        self.untouched()

    def test_comment_from_a_file_needs_no_text_but_still_needs_by(self):
        f = self.private("c.md", "Checked on main.\n\nNo change needed.\n")
        p = self.board("comment", "OPS-10", "--by", "CLAUDE", "--text-file", f)
        self.assertEqual(p.stdout.strip(), f"commented on {B_A[:8]} as CLAUDE{TAIL}  OPS-10")
        iss = self.mock.issues[hy(B_A)]
        self.assertEqual(
            self.comments_of(iss)[-1],
            f"[CLAUDE·5e55105a] Checked on main.\n\nNo change needed.\n\nSessions: {BASE_SESSION}",
        )
        n = len(self.mock.comments)
        p = self.board(
            "comment", "OPS-10", "--by", "CLAUDE", "--text-file", f
        )  # same words, same day
        self.assertIn("already on", p.stderr)
        self.assertEqual(len(self.mock.comments), n)
        p = self.board(
            "comment",
            "OPS-10",
            "--by",
            "CLAUDE",
            "--text-file",
            "-",
            "--session",
            "sess-1",
            stdin="From standard input.\n",
        )
        self.assertEqual(
            self.comments_of(iss)[-1], "[CLAUDE·sess1] From standard input.\n\nSessions: sess-1"
        )
        ops = len(self.mock.ops)
        p = self.board("comment", "OPS-10", "--text-file", f, code=2)
        self.assertIn("--by", p.stderr)
        self.assertEqual(len(self.mock.ops), ops)
        p = self.board("comment", "OPS-10", "--by", "CODEX", "--text-file", f, code=2)
        self.assertIn("does not match the verified seat CLAUDE", p.stderr)

    def test_an_empty_comment_file_is_refused(self):
        p = self.board(
            "comment",
            "OPS-10",
            "--by",
            "CLAUDE",
            "--text-file",
            self.private("e.md", " \n\n"),
            code=2,
        )
        self.assertIn("comment text is empty", p.stderr)
        p = self.board(
            "comment", "OPS-10", "--by", "CLAUDE", "--text-file", "-", stdin="\n", code=2
        )
        self.assertIn("comment text is empty", p.stderr)
        self.untouched()

    def test_a_gated_write_never_reads_its_file(self):
        """A write with no bearer-verified seat exits 6 and no seat exits 2, with the file untouched (a
        missing one would otherwise be an exit 2 of its own)."""
        for args in (
            ["file", "--title", "x", "--desc-file", "/nonexistent", "--fix-file", "-"],
            ["comment", B_A[:8], "--by", "CLAUDE", "--text-file", "-"],
            ["label", "add", B_A[:8], "needs-owner"],
        ):
            self.board(*args, drop=("AGENT_SEAT",), code=6, stdin="")
        self.untouched()


class FlatLabelTests(BoardTest):
    """needs-owner, ask-capture, fleet-report and stale-claim:  flat workspace labels, and nothing else."""

    def file(self, title, *labels, uid=None, code=0):
        args = ["file", "--title", title, "--app", "AFC"]
        for name in labels:
            args += ["--label", name]
        if uid:
            args += ["--uid", uid]
        return self.board(*args, code=code)

    def ident(self, p):
        return p.stdout.split()[-1]

    def test_the_label_set_and_kinds_are_fixed(self):
        cli = load_cli()
        self.assertEqual(
            cli.FLAT_LABELS, ("needs-owner", "ask-capture", "fleet-report", "stale-claim")
        )
        self.assertEqual(
            cli.KINDS, ("agent-report", "review-finding", "effort-row", "github-issue", "sentry")
        )
        self.assertEqual(cli.CLOSING_REMOVES, ("needs-owner", "ask-capture"))
        self.assertEqual(
            cli.CLOSING_STATUSES, ("completed", "deployed", "wontfix", "duplicate", "canceled")
        )

    def test_file_label_creates_one_flat_workspace_label_then_reuses_it(self):
        p1 = self.file("Decision one", "needs-owner", uid="d1")
        self.assertEqual(len(self.mock.label_creates), 1)
        made = self.mock.label_creates[0]
        self.assertEqual(made["name"], "needs-owner")
        self.assertNotIn("parentId", made)
        self.assertNotIn("teamId", made)
        lb = self.mock.labels[made["id"]]
        self.assertEqual((lb["parent"], lb["team"], lb["isGroup"]), (None, None, False))
        p2 = self.file("Decision two", "needs-owner", uid="d2")
        self.assertEqual(len(self.mock.label_creates), 1)  # created once, then found
        for p in (p1, p2):
            iss = self.issue_by_ident(self.ident(p))
            self.assertEqual(self.label_names(iss), ["Kind/agent-report", "needs-owner"])
            self.assertIn(made["id"], iss["labels"])
            self.assertEqual(iss["state"], "Todo")
        cache = json.loads((self.tmp / "cache" / "workspace.json").read_text())
        self.assertEqual(cache["flat"]["needs-owner"], made["id"])

    def test_labels_repeat_dedupe_and_ignore_case(self):
        p = self.file(
            "All four", "needs-owner", "ask-capture", "Fleet-Report", "needs-owner", " stale-claim "
        )
        iss = self.issue_by_ident(self.ident(p))
        self.assertEqual(
            self.label_names(iss),
            ["Kind/agent-report", "ask-capture", "fleet-report", "needs-owner", "stale-claim"],
        )
        self.assertEqual(
            sorted(c["name"] for c in self.mock.label_creates),
            ["ask-capture", "fleet-report", "needs-owner"],
        )  # stale-claim already existed
        self.assertEqual(self.mock.violations, [])

    def test_a_repeat_uid_with_labels_changes_nothing(self):
        p1 = self.file("Once only", "needs-owner", "ask-capture", uid="once")
        before = {
            k: self.mock.ops.count(k) for k in ("BoardFile", "BoardUpdate", "BoardLabelCreate")
        }
        p2 = self.file("Once only", "needs-owner", "ask-capture", uid="once")
        self.assertIn("already filed", p2.stdout)
        self.assertEqual(p1.stdout.split()[1], p2.stdout.split()[1])
        self.assertEqual({k: self.mock.ops.count(k) for k in before}, before)
        self.assertEqual(
            len([i for i in self.mock.issues.values() if i["title"] == "Once only"]), 1
        )

    def test_other_labels_are_refused_with_no_request(self):
        why = {
            "claimed:CODEX": "claim",
            "claimed:CLAUDE": "claim",
            "seat:CLAUDE": "attribution",
            "Kind/agent-report": "--kind",
            "kind/review-finding": "--kind",
            "agent-report": "Kind",
            "sentry": "Kind",
            "Severity/P1": "--severity",
            "P0": "Severity",
            "p3": "Severity",
            "Env/Mac": "--env",
            "Mac": "Env",
            "cloud": "Env",
            "Kind": "--kind",
            "imported": "not one of",
            "app:agent-bar": "not one of",
            "needs owner": "not one of",
            "needs-owner-2": "not one of",
            "": "not one of",
        }
        for name, words in why.items():
            for args in (
                ["file", "--title", "x", "--label", name],
                ["label", "add", B_A[:8], name],
                ["label", "remove", B_A[:8], name],
                ["list", "--label", name],
            ):
                p = self.board(*args, code=2)
                self.assertIn(
                    "Allowed:  needs-owner, ask-capture, fleet-report, stale-claim", p.stderr, args
                )
                self.assertIn(words, p.stderr, (name, p.stderr))
        self.board(
            "file", "--title", "x", "--label", "needs-owner", "--label", "Kind/sentry", code=2
        )
        self.assertEqual(self.mock.ops, [])
        self.assertEqual(self.mock.mints, 0)

    def test_fleet_report_is_a_label_never_a_kind(self):
        p = self.board("file", "--title", "x", "--kind", "fleet-report", code=2)
        self.assertIn("--label fleet-report", p.stderr)
        self.assertEqual(self.mock.ops, [])
        p = self.file("A fleet report", "fleet-report")
        iss = self.issue_by_ident(self.ident(p))
        self.assertEqual(self.label_names(iss), ["Kind/agent-report", "fleet-report"])

    def test_label_add_and_remove(self):
        iss = self.mock.issues[hy(B_A)]
        before = list(iss["labels"])
        comments = len(self.mock.comments)
        p = self.board("label", "add", "OPS-10", "needs-owner")
        self.assertEqual(p.stdout.strip(), f"labeled {B_A[:8]} +needs-owner  OPS-10")
        lid = self.mock.label_creates[0]["id"]
        self.assertEqual(iss["labels"], before + [lid])
        self.assertEqual(self.mock.updates[-1], ("OPS-10", {"addedLabelIds": [lid]}))
        n = len(self.mock.updates)
        p = self.board("label", "add", "OPS-10", "Needs-Owner")
        self.assertEqual(
            p.stdout.strip(), f"{B_A[:8]} already has needs-owner  (unchanged)  OPS-10"
        )
        self.assertEqual(len(self.mock.updates), n)
        p = self.board("label", "remove", "OPS-10", "needs-owner", "--by", "claude")
        self.assertEqual(p.stdout.strip(), f"unlabeled {B_A[:8]} -needs-owner  OPS-10")
        self.assertEqual(self.mock.updates[-1], ("OPS-10", {"removedLabelIds": [lid]}))
        self.assertEqual(iss["labels"], before)  # every other label, seat: and Kind included, kept
        n = len(self.mock.updates)
        p = self.board("label", "remove", "OPS-10", "needs-owner")
        self.assertEqual(
            p.stdout.strip(), f"{B_A[:8]} does not have needs-owner  (unchanged)  OPS-10"
        )
        self.assertEqual(len(self.mock.updates), n)
        self.assertEqual(len(self.mock.comments), comments)  # a label change posts no comment
        self.assertEqual(len(self.mock.label_creates), 1)

    def test_label_remove_never_creates_a_label(self):
        p = self.board("label", "remove", "OPS-10", "fleet-report")
        self.assertIn("(unchanged)", p.stdout)
        self.assertEqual(self.mock.label_creates, [])
        self.assertEqual(self.mock.ops.count("BoardUpdate"), 0)

    def test_label_by_must_match_the_seat(self):
        p = self.board("label", "add", "OPS-10", "needs-owner", "--by", "CODEX", code=2)
        self.assertIn("does not match the verified seat CLAUDE", p.stderr)
        self.assertEqual(self.mock.ops, [])

    def test_a_team_scoped_label_of_the_same_name_is_not_used(self):
        self.mock._label("needs-owner", None, "lbl-team-needs-owner", team="OPS")
        self.board("label", "add", "ST-1", "needs-owner")
        iss = self.mock.issues[hy(B_C)]
        self.assertNotIn("lbl-team-needs-owner", iss["labels"])
        self.assertEqual(len(self.mock.label_creates), 1)  # a workspace label was made instead
        self.assertIn(self.mock.label_creates[0]["id"], iss["labels"])

    def test_an_existing_workspace_label_is_found_not_recreated(self):
        self.board(
            "file", "--title", "Warm the cache", "--app", "AFC"
        )  # writes the workspace cache
        lid = self.mock._label("needs-owner", None)  # made in Linear's UI since
        self.board("label", "add", "ST-1", "needs-owner")
        self.assertEqual(self.mock.label_creates, [])
        self.assertIn(lid, self.mock.issues[hy(B_C)]["labels"])

    def test_a_cache_from_before_flat_labels_is_read_again(self):
        self.board("file", "--title", "Warm the cache", "--app", "AFC")
        path = self.tmp / "cache" / "workspace.json"
        d = json.loads(path.read_text())
        d.pop("flat")
        path.write_text(json.dumps(d))
        n = self.mock.ops.count("BoardLabels")
        self.board("label", "add", "ST-1", "stale-claim")
        self.assertGreater(self.mock.ops.count("BoardLabels"), n)
        self.assertEqual(self.mock.label_creates, [])
        self.assertIn(self.mock.lid("stale-claim"), self.mock.issues[hy(B_C)]["labels"])

    def test_list_label_filters_and_json_rows_carry_labels(self):
        q1 = self.file("Q1", "needs-owner")
        self.file("Q2", "needs-owner", "ask-capture")
        self.file("Q3", "ask-capture")
        self.file("Q4")
        rows = json.loads(self.board("list", "--json", "--label", "needs-owner").stdout)
        self.assertEqual(sorted(r["title"] for r in rows), ["Q1", "Q2"])
        for r in rows:
            self.assertIn("needs-owner", r["labels"])
            self.assertIn("Kind/agent-report", r["labels"])
        rows = json.loads(
            self.board("list", "--json", "--label", "needs-owner", "--label", "ask-capture").stdout
        )
        self.assertEqual([r["title"] for r in rows], ["Q2"])
        rows = json.loads(
            self.board("list", "--json", "--label", "ask-capture", "--status", "in_progress").stdout
        )
        self.assertEqual(rows, [])
        p = self.board("list", "--label", "needs-owner", "--app", "AFC")
        self.assertIn("2 shown of 2 matching", p.stdout)
        p = self.board("list", "--label", "ask-capture", "--limit", "1")
        self.assertIn("1 shown of 2 matching", p.stdout)
        self.board("list", "--label", "needs-owner", drop=("AGENT_SEAT",))  # a read needs no seat
        d = json.loads(self.board("show", self.ident(q1), "--json").stdout)
        self.assertEqual(sorted(d["labels"]), ["Kind/agent-report", "needs-owner"])

    def issue_with(self, title, *labels):
        """An open issue carrying the named labels, plus Kind/agent-report."""
        iss = self.mock.add_issue(
            str(uuid.uuid4()), "AFC", "In Progress", title, 3, ["Kind/agent-report"]
        )
        for name in labels:
            self.board("label", "add", iss["identifier"], name)
        return iss

    def test_closing_statuses_remove_needs_owner_and_ask_capture(self):
        for value in ("completed", "deployed", "wontfix", "canceled"):
            iss = self.issue_with(
                f"Close as {value}", "needs-owner", "ask-capture", "fleet-report", "stale-claim"
            )
            before = len(self.mock.updates)
            self.board("status", iss["identifier"], value, "--resolution", "Answered.")
            self.assertEqual(
                self.label_names(iss), ["Kind/agent-report", "fleet-report", "stale-claim"], value
            )
            ident, inp = self.mock.updates[-1]
            self.assertEqual(len(self.mock.updates), before + 1)  # one update:  state and labels
            self.assertEqual(sorted(inp), ["removedLabelIds", "stateId"])
            self.assertEqual(len(inp["removedLabelIds"]), 2)

    def test_closing_without_those_labels_sends_no_label_change(self):
        iss = self.issue_with("Plain", "fleet-report")
        self.board("status", iss["identifier"], "completed")
        self.assertEqual(sorted(self.mock.updates[-1][1]), ["stateId"])
        self.assertIn("fleet-report", self.label_names(iss))

    def test_duplicate_removes_them_with_a_label_only_update(self):
        iss = self.issue_with("Dup with labels", "needs-owner", "ask-capture", "fleet-report")
        before = len(self.mock.updates)
        p = self.board(
            "status",
            iss["identifier"],
            "duplicate",
            "--of",
            "OPS-10",
            "--resolution",
            "Same thing.",
        )
        self.assertEqual(iss["state"], "Duplicate")
        self.assertEqual(self.label_names(iss), ["Kind/agent-report", "fleet-report"])
        self.assertEqual(len(self.mock.updates), before + 1)
        self.assertEqual(
            sorted(self.mock.updates[-1][1]), ["removedLabelIds"]
        )  # the relation moved the state
        self.assertIn("Resolution (duplicate of OPS-10): Same thing.", self.comments_of(iss)[-1])
        self.assertIn("-> duplicate", p.stdout)

    def test_other_statuses_keep_the_labels(self):
        iss = self.issue_with("Keeps", "needs-owner", "ask-capture")
        for value in ("in_progress", "addressed", "parked", "open", "backlog"):
            self.board("status", iss["identifier"], value)
            self.assertEqual(
                self.label_names(iss), ["Kind/agent-report", "ask-capture", "needs-owner"], value
            )

    def test_mac_collab_backend_refuses_the_linear_only_flags(self):
        old = self.tmp / "old-board"
        old.write_text("import sys\nprint('OLD BOARD', sys.argv[1:])\n")
        extra = {"BOARD_BACKEND": "mac-collab", "BOARD_MAC_COLLAB_CLI": str(old)}
        for args in (
            ["file", "--title", "x", "--desc-file", "/nonexistent"],
            ["file", "--title", "x", "--fix-file=f"],
            ["file", "--title", "x", "--label", "needs-owner"],
            ["file", "--title", "x", "--label=needs-owner"],
            ["comment", "OPS-10", "--by", "CLAUDE", "--text-file", "-"],
            ["list", "--label", "needs-owner"],
            ["label", "add", "OPS-10", "needs-owner"],
        ):
            p = self.board(*args, extra=extra, drop=("AGENT_SEAT",), code=2)
            self.assertIn("Linear only", p.stderr, args)
            self.assertNotIn("OLD BOARD", p.stdout, args)
        # A write verb with no Linear-only flag still does not reach the archived CLI:
        # the frozen findings.db is read-only, so the backend refuses it first.
        p = self.board(
            "list", "--app", "AFC", extra=extra, drop=("AGENT_SEAT",)
        )
        self.assertEqual(p.stdout.strip(), "OLD BOARD ['list', '--app', 'AFC']")
        self.assertEqual(self.mock.ops, [])


def load_cli():
    """The CLI as a module, for unit checks of its pure functions."""
    import importlib.machinery
    import importlib.util

    loader = importlib.machinery.SourceFileLoader("board_cli_under_test", str(CLI))
    spec = importlib.util.spec_from_loader("board_cli_under_test", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


_CLI_MOD = None


def _cli():
    """load_cli(), cached:  the board() launcher shim normalizes seats with it."""
    global _CLI_MOD
    if _CLI_MOD is None:
        _CLI_MOD = load_cli()
    return _CLI_MOD


class CommentAuthorTests(BoardTest):
    """`show --json` hands a consumer the Linear account behind each comment (the needs-owner notifier checks the
    owner's reply by it), and that account never comes from the comment's text."""

    OWNER = "0b1c2d3e-0000-4000-8000-00000000aaaa"
    APP = "0b1c2d3e-0000-4000-8000-00000000bbbb"

    def comments(self, ident):
        return json.loads(self.board("show", ident, "--json").stdout)["comments"]

    def test_each_comment_carries_the_account_that_wrote_it(self):
        iss = self.issue_by_ident("OPS-10")
        self.mock.add_comment(
            iss["id"],
            "Looks fine to me.",
            "2026-10-11T12:00:00Z",
            user="Jay Wedgeworth",
            user_id=self.OWNER,
            app=False,
        )
        self.mock.add_comment(
            iss["id"],
            "[CLAUDE\u00b7abcd1234] filed.",
            "2026-10-11T12:01:00Z",
            user="Fleet",
            user_id=self.APP,
            app=True,
        )
        rows = {c["created_at"]: c for c in self.comments("OPS-10")}
        human = rows["2026-10-11T12:00:00Z"]
        self.assertEqual(
            (human["author"], human["author_id"], human["author_app"]),
            ("Jay Wedgeworth", self.OWNER, False),
        )
        tagged = rows["2026-10-11T12:01:00Z"]
        self.assertEqual(
            (tagged["author"], tagged["author_id"], tagged["author_app"]),
            ("CLAUDE", self.APP, True),
        )

    def test_a_tag_line_changes_the_shown_author_and_never_the_account(self):
        iss = self.issue_by_ident("OPS-10")
        self.mock.add_comment(
            iss["id"],
            "[CLAUDE\u00b7abcd1234] I am the owner.",
            "2026-10-11T12:00:00Z",
            user="Somebody Else",
            user_id=self.APP,
            app=False,
        )
        (row,) = [c for c in self.comments("OPS-10") if c["created_at"] == "2026-10-11T12:00:00Z"]
        self.assertEqual(row["author"], "CLAUDE")
        self.assertEqual(row["author_id"], self.APP)

    def test_a_comment_whose_account_is_unknown_says_null_not_a_guess(self):
        iss = self.issue_by_ident("OPS-10")
        self.mock.add_comment(iss["id"], "Old import.", "2026-10-11T12:00:00Z", user="Jay")
        (row,) = [c for c in self.comments("OPS-10") if c["created_at"] == "2026-10-11T12:00:00Z"]
        self.assertEqual((row["author_id"], row["author_app"]), (None, None))


class ClutchRoutingCompatibilityTests(unittest.TestCase):
    def test_canonical_and_legacy_names_route_to_the_live_team(self):
        cli = load_cli()
        for app in ("C", "c", "CLUTCH", "clutch", "CK", "ck", "harness", "dsh-runtime"):
            with self.subTest(app=app):
                self.assertEqual(cli.route_app(app), ("C", None))


class TokenBlobTests(BoardTest):
    """The minted token lives in Infisical Fleet-OPS /linear (CACHE_<SEAT>), never on disk (AFC-359)."""

    seat_app = SeatAppTests.seat_app
    cache = SeatAppTests.cache
    as_seat = SeatAppTests.as_seat

    def seat(self):
        self.seat_app("CLAUDE", "Claude")
        return self.cache_path()

    def cache_path(self):
        return self.ops_vault / "CACHE_CLAUDE"

    def sets(self):
        return [v for v in self.fs_verbs() if v.startswith("set ")]

    def test_a_fresh_blob_means_zero_mints_zero_writes_and_three_per_key_reads(self):
        blob = self.seat()
        self.as_seat("CLAUDE", "list")
        self.assertEqual(self.mock.mints, 1)
        self.assertTrue(blob.exists())
        before = len(self.fs_verbs())
        self.as_seat("CLAUDE", "list")
        self.assertEqual(self.mock.mints, 1)
        self.assertEqual(self.sets(), ["set CACHE_CLAUDE"], "only the first run wrote")
        # AFC-361:  no bundle dump, one `fleet-secrets get` per key (bearer, verify key, then
        # client id, client secret, blob)
        self.assertEqual(
            self.fs_verbs()[before:],
            [
                "get CLAUDE_SEAT_BEARER",
                "get GR_BEARER_VERIFY_KEY",
                "get CLAUDE_LINEAR_CLIENT_ID",
                "get CLAUDE_LINEAR_CLIENT_SECRET",
                "get CACHE_CLAUDE",
            ],
        )

    def test_an_expired_blob_means_one_mint_and_one_write_with_the_value_on_stdin(self):
        blob = self.seat()
        self.as_seat("CLAUDE", "list")
        data = json.loads(blob.read_text())
        data["expires_at"] = 1.0
        data["minted_at"] -= 3 * 86400
        blob.write_text(json.dumps(data))
        n_sets = len(self.sets())
        self.as_seat("CLAUDE", "list")
        self.assertEqual(self.mock.mints, 2)
        self.assertEqual(len(self.sets()), n_sets + 1)
        # the fake refuses any `set` that carries the value in an argument, so reaching here proves stdin
        stored = json.loads(blob.read_text())
        self.assertIn(stored["access_token"], self.mock.tokens)
        self.assertEqual(
            sorted(stored),
            ["access_token", "cred_fp", "expires_at", "minted_at", "scope", "token_type"],
        )

    def test_a_failed_write_still_works_and_leaves_nothing_on_disk(self):
        blob = self.seat()
        p = self.as_seat("CLAUDE", "list", extra={"FS_FAIL_SET": "1"})
        self.assertIn("could not save the new Claude seat app token in Infisical", p.stderr)
        self.assertIn("CACHE_CLAUDE", p.stderr)
        self.assertIn("using it for this run only", p.stderr)
        self.assertFalse(blob.exists())
        self.assertEqual(sorted(str(q) for q in (self.tmp / "home").rglob("*")), [])
        tokens = list(self.mock.tokens)
        for q in self.tmp.rglob("*"):
            if q.is_file() and q != self.fs_calls:
                data = q.read_text(errors="ignore")
                for t in tokens:
                    self.assertNotIn(t, data, f"a token was written to {q}")

    def test_the_viewer_is_kept_in_the_blob(self):
        blob = self.seat()
        self.as_seat("CLAUDE", "whoami")
        self.assertEqual(json.loads(blob.read_text())["viewer"]["name"], "Claude")
        before = len(self.sets())
        self.as_seat("CLAUDE", "whoami")
        self.assertEqual(len(self.sets()), before, "an unchanged viewer is not written again")

    def test_no_file_appears_under_home(self):
        self.seat()
        for args in (["whoami"], ["list"], ["comment", B_A[:8], "--by", "CLAUDE", "--text", "x"]):
            self.as_seat("CLAUDE", *args)
        self.assertEqual(sorted(str(q) for q in (self.tmp / "home").rglob("*")), [])
        self.assertFalse(list(self.tmp.rglob("*.token")))

    def test_the_cloud_pair_has_no_store_and_mints_each_run(self):
        pair = {"LINEAR_CLIENT_ID": CLIENT_ID, "LINEAR_CLIENT_SECRET": CLIENT_SECRET}
        p = self.board("whoami", extra=pair)
        self.assertIn("token store: none on this host", p.stdout)
        self.board("list", extra=pair)
        self.assertEqual(self.mock.mints, 2)
        self.assertEqual(self.sets(), [])


class CredentialPreviewTests(unittest.TestCase):
    """A credential preview:  enough to tell two credentials apart, never enough to use one.

    The board owns the credential, so the board is the layer that hands back a preview of it
    to a caller that has to name the credential without printing it."""

    def test_a_long_credential_shows_its_first_eight_and_last_four(self):
        cli = load_cli()
        secret = "abcdefgh" + "x" * 20 + "wxyz"
        self.assertEqual(cli.credential_preview(secret), "abcdefgh…wxyz")

    def test_a_short_credential_shows_one_character_only(self):
        # Twelve of thirteen characters is the whole credential.  A value under twenty-four
        # characters is not a preview at all, so it shows its single leading character.
        cli = load_cli()
        self.assertEqual(cli.credential_preview("lin_api_123456"), "l…")
        self.assertEqual(cli.credential_preview("a" * 23), "a…")
        self.assertEqual(cli.credential_preview("a" * 24), "a" * 8 + "…" + "a" * 4)

    def test_an_empty_credential_has_no_preview(self):
        self.assertEqual(load_cli().credential_preview(""), "")
        self.assertEqual(load_cli().credential_preview(None), "")


class CredentialFailureTests(BoardTest):
    """A credential the board could not use:  what it says about it, and what it never says.

    The board owns the credential, so the board is the layer that can hand back a safe preview
    of it, and every exit-4 line names the variable the operator has to fix.  A caller
    downstream reads those fields and never the sentence around them, because the sentence is
    third-party text that can carry anything."""

    seat_app = SeatAppTests.seat_app
    as_seat = SeatAppTests.as_seat

    def test_a_rejected_credential_names_its_masked_preview(self):
        self.board("whoami")
        tokens = set(self.mock.tokens)
        self.mock.tokens.clear()
        p = self.board("list", code=6)
        self.assertIn("credential=", p.stderr)
        preview = p.stderr.rsplit("credential=", 1)[1].split()[0]
        self.assertIn("…", preview, "a masked preview, not the credential")
        for token in tokens:
            self.assertNotIn(token, preview, "the token itself is never the preview")

    def test_a_swapped_seat_credential_names_the_variable_to_fix(self):
        # Every exit-4 line names the variable, so a caller downstream never has to fall back
        # to a nameless "no credentials".
        codex, sec = self.seat_app("CODEX", "Codex", file=False)
        self.seat_app("CLAUDE", "Claude", cid=codex, secret=sec, register=False)
        p = self.as_seat("CLAUDE", "comment", B_A[:8], "--by", "CLAUDE", "--text", "x", code=4)
        self.assertIn("refusing to write as CLAUDE", p.stderr)
        self.assertIn("LINEAR_CLIENT_ID", p.stderr)
        self.assertIn("LINEAR_CLIENT_SECRET", p.stderr)

    def test_a_missing_credential_names_the_variable_to_fix(self):
        self.creds.unlink()
        p = self.board("list", code=4)
        self.assertIn("FLEET_LINEAR_CLIENT_ID", p.stderr)
        self.assertIn("FLEET_LINEAR_CLIENT_SECRET", p.stderr)

    # ---- the structured fields a caller downstream reads ---------------------------------------
    def test_a_missing_credential_hands_back_the_variable_as_a_field(self):
        # The board owns the credential, so it is the board that says which variable an
        # operator has to set, and it says it as a field:  a caller never has to parse
        # the sentence around it.
        self.creds.unlink()
        p = self.board("list", code=4)
        self.assertIn("credential_field=", p.stderr)
        field = p.stderr.rsplit("credential_field=", 1)[1].split()[0]
        self.assertIn("FLEET_LINEAR_CLIENT_ID", field)
        self.assertIn("FLEET_LINEAR_CLIENT_SECRET", field)
        self.assertIn("LINEAR_CLIENT_ID", field)
        self.assertIn("LINEAR_CLIENT_SECRET", field)

    def test_a_swapped_credential_hands_back_the_variable_as_a_field(self):
        codex, sec = self.seat_app("CODEX", "Codex", file=False)
        self.seat_app("CLAUDE", "Claude", cid=codex, secret=sec, register=False)
        p = self.as_seat("CLAUDE", "comment", B_A[:8], "--by", "CLAUDE", "--text", "x", code=4)
        self.assertIn("credential_field=LINEAR_CLIENT_ID,LINEAR_CLIENT_SECRET", p.stderr)

    def test_a_rejected_credential_hands_back_the_variable_as_a_field(self):
        claude, _ = self.seat_app("CLAUDE", "Claude")
        self.mock.clients[claude]["secret"] = "rotated-in-linear-not-in-the-file"
        p = self.as_seat("CLAUDE", "whoami", code=6)
        self.assertIn(
            "credential_field=CLAUDE_LINEAR_CLIENT_ID,CLAUDE_LINEAR_CLIENT_SECRET", p.stderr
        )

    def test_a_failed_mint_says_what_failed_and_nothing_of_the_providers_own_text(self):
        # The token endpoint's error body is Linear's text and can carry anything, so
        # the sentence says what failed and the machine-readable parts come back as
        # fields of their own.
        claude, _ = self.seat_app("CLAUDE", "Claude")
        self.mock.clients[claude]["secret"] = "rotated-in-linear-not-in-the-file"
        p = self.as_seat("CLAUDE", "whoami", code=6)
        self.assertIn("minting the Claude seat app token failed", p.stderr)
        self.assertIn("error_type=HTTPError", p.stderr)
        self.assertIn("http=401", p.stderr)
        self.assertNotIn(
            "invalid_client", p.stderr, "the provider's own text is not in the message"
        )
        self.assertNotIn("bad client", p.stderr)

    def test_a_request_id_is_quoted_back_when_the_response_carries_one(self):
        claude, _ = self.seat_app("CLAUDE", "Claude")
        self.mock.request_id = "req-abc12345"
        self.mock.clients[claude]["secret"] = "rotated-in-linear-not-in-the-file"
        p = self.as_seat("CLAUDE", "whoami", code=6)
        self.assertIn("request_id=req-abc12345", p.stderr)

    def test_no_request_id_is_invented_when_the_response_carries_none(self):
        claude, _ = self.seat_app("CLAUDE", "Claude")
        self.mock.clients[claude]["secret"] = "rotated-in-linear-not-in-the-file"
        p = self.as_seat("CLAUDE", "whoami", code=6)
        self.assertNotIn("request_id=", p.stderr)


class ErrorFieldTests(unittest.TestCase):
    """The allow-listed fields of a failure:  what a caller downstream may read."""

    def test_the_fields_of_an_error_are_the_allow_listed_ones(self):
        cli = load_cli()
        err = cli.GqlError(
            [
                {
                    "message": "Authentication required",
                    "extensions": {"code": "AUTHENTICATION_ERROR"},
                }
            ],
            None,
            401,
            {"x-request-id": "req-abc12345"},
        )
        fields = cli.error_fields(
            err, "lin_api_…wxyz", "environment (LINEAR_CLIENT_ID, LINEAR_CLIENT_SECRET)"
        )
        self.assertEqual(fields["error_type"], "GqlError")
        self.assertEqual(fields["http"], "401")
        self.assertEqual(fields["request_id"], "req-abc12345")
        self.assertEqual(fields["credential"], "lin_api_…wxyz")
        self.assertEqual(fields["credential_field"], "LINEAR_CLIENT_ID,LINEAR_CLIENT_SECRET")

    def test_a_failure_with_no_status_and_no_request_id_carries_neither(self):
        cli = load_cli()
        fields = cli.error_fields(cli.GqlError([{"message": "boom"}]))
        self.assertEqual(fields, {"error_type": "GqlError"})

    def test_the_variable_names_come_from_the_places_the_credentials_came_from(self):
        cli = load_cli()
        self.assertEqual(
            cli.credential_vars(
                "Infisical Example-Project staging /example-path (CLAUDE_LINEAR_CLIENT_ID, CLAUDE_LINEAR_CLIENT_SECRET)"
            ),
            "CLAUDE_LINEAR_CLIENT_ID,CLAUDE_LINEAR_CLIENT_SECRET",
        )
        self.assertEqual(
            cli.credential_vars("environment (LINEAR_CLIENT_ID, LINEAR_CLIENT_SECRET)"),
            "LINEAR_CLIENT_ID,LINEAR_CLIENT_SECRET",
        )
        self.assertEqual(cli.credential_vars(""), "")


class OwnershipTests(BoardTest):
    """Smart ownership:  with LINEAR_BOARD_OWNER=1 a claim also mirrors onto the seat's own
    Linear user, so the board's assignee column agrees with the claim label, and releasing or
    closing clears it again.  The label stays the record of who holds an issue, and a seat with
    no Linear user of its own still claims:  it simply owns nothing."""

    OWNED = {"LINEAR_BOARD_OWNER": "1"}

    def todo_issue(self, title="Ownership probe"):
        return self.mock.add_issue(
            str(uuid.uuid4()), "AFC", "Todo", title, 3, ["Kind/agent-report"]
        )

    def test_by_default_a_claim_stays_a_label_and_writes_no_assignee(self):
        """The frozen contract:  the seat rides in the label and the tag line, not a Linear field."""
        iss = self.todo_issue()
        self.board("claim", iss["identifier"], "--by", "CLAUDE")
        self.assertNotIn("assigneeId", self.mock.updates[-1][1])
        self.assertIn("claimed:CLAUDE", self.label_names(iss))

    def test_claim_mirrors_ownership_onto_the_seats_linear_user(self):
        iss = self.todo_issue()
        self.board(
            "claim", iss["identifier"], "--by", "CLAUDE", extra=self.OWNED
        )  # AGENT_SEAT=CLAUDE
        inp = self.mock.updates[-1][1]
        self.assertEqual(inp.get("assigneeId"), "user-claude")  # seat CLAUDE -> user Claude
        self.assertIn("claimed:CLAUDE", self.label_names(iss))  # the label is still the record

    def test_a_seat_with_no_linear_user_still_claims_and_assigns_nobody(self):
        iss = self.todo_issue()
        self.board(
            "claim",
            iss["identifier"],
            "--by",
            "SENTRY",
            extra=dict(self.OWNED, AGENT_SEAT="SENTRY"),
        )
        self.assertNotIn("assigneeId", self.mock.updates[-1][1])
        self.assertIn("claimed:SENTRY", self.label_names(iss))

    def test_a_deactivated_seat_is_never_assigned(self):
        """A seat Linear has deactivated owns nothing:  the label is kept, the assignee is not."""
        iss = self.todo_issue()
        self.board(
            "claim",
            iss["identifier"],
            "--by",
            "GB-RETIRED",
            extra=dict(self.OWNED, AGENT_SEAT="GB-RETIRED"),
        )
        self.assertNotIn("assigneeId", self.mock.updates[-1][1])
        self.assertIn("claimed:GB-RETIRED", self.label_names(iss))

    def test_a_steal_moves_the_owner_with_the_claim(self):
        iss = self.mock.add_issue(
            str(uuid.uuid4()),
            "AFC",
            "In Progress",
            "Held",
            3,
            ["Kind/agent-report", "seat:CODEX", "claimed:CODEX"],
        )
        self.board(
            "claim",
            iss["identifier"],
            "--by",
            "CLAUDE",
            "--steal",
            "--reason",
            "CODEX is capped out",
            extra=self.OWNED,
        )
        self.assertEqual(self.mock.updates[-1][1].get("assigneeId"), "user-claude")
        self.assertEqual(iss.get("assignee"), "user-claude")

    def test_closing_clears_the_owner(self):
        iss = self.todo_issue()
        self.board("claim", iss["identifier"], "--by", "CLAUDE", extra=self.OWNED)
        self.assertEqual(iss.get("assignee"), "user-claude")
        self.board("status", iss["identifier"], "completed", extra=self.OWNED)
        self.assertIsNone(self.mock.updates[-1][1].get("assigneeId"))
        self.assertIsNone(iss.get("assignee"))

    def test_releasing_the_claim_clears_the_owner(self):
        iss = self.todo_issue()
        self.board("claim", iss["identifier"], "--by", "CLAUDE", extra=self.OWNED)
        self.board("status", iss["identifier"], "open", extra=self.OWNED)
        self.assertIsNone(self.mock.updates[-1][1].get("assigneeId"))
        self.assertIsNone(iss.get("assignee"))

    def test_duplicate_clears_the_owner(self):
        """The duplicate relation path skips the CLOSING_STATUSES branch, so it
        clears the assignee in its own cleanup update instead."""
        iss = self.todo_issue()
        canon = self.todo_issue(title="Canonical")
        self.board("claim", iss["identifier"], "--by", "CLAUDE", extra=self.OWNED)
        self.assertEqual(iss.get("assignee"), "user-claude")
        self.board(
            "status", iss["identifier"], "duplicate", "--of", canon["identifier"], extra=self.OWNED
        )
        self.assertIn("assigneeId", self.mock.updates[-1][1])
        self.assertIsNone(self.mock.updates[-1][1].get("assigneeId"))
        self.assertIsNone(iss.get("assignee"))

    def test_steal_by_a_userless_seat_clears_the_stale_owner(self):
        """A steal moves the claim even when the taker has no Linear user;  the
        old owner's assignee goes with it instead of lingering stale."""
        iss = self.todo_issue()
        self.board(
            "claim", iss["identifier"], "--by", "CODEX", extra=dict(self.OWNED, AGENT_SEAT="CODEX")
        )
        self.assertEqual(iss.get("assignee"), "user-codex")
        self.board(
            "claim",
            iss["identifier"],
            "--by",
            "SENTRY",
            "--steal",
            "--reason",
            "CODEX is gone",
            extra=dict(self.OWNED, AGENT_SEAT="SENTRY"),
        )
        self.assertIn("assigneeId", self.mock.updates[-1][1])
        self.assertIsNone(self.mock.updates[-1][1].get("assigneeId"))
        self.assertIsNone(iss.get("assignee"))
        self.assertIn("claimed:SENTRY", self.label_names(iss))

    def test_release_without_a_claim_keeps_a_human_assignee(self):
        """open/backlog clear the assignee only when a claim marker was actually
        released;  an unclaimed issue keeps whoever owns it."""
        iss = self.todo_issue()
        iss["assignee"] = "user-claude"
        self.board("status", iss["identifier"], "open", extra=self.OWNED)
        self.assertNotIn("assigneeId", self.mock.updates[-1][1])
        self.assertEqual(iss.get("assignee"), "user-claude")

    def test_closing_by_default_writes_no_assignee_at_all(self):
        iss = self.todo_issue()
        self.board("claim", iss["identifier"], "--by", "CLAUDE")
        self.board("status", iss["identifier"], "completed")
        self.assertEqual(sorted(self.mock.updates[-1][1]), ["stateId"])

    def test_the_users_listing_is_gated_on_the_flag(self):
        """With the flag off no board command touches the users listing, so a
        refused listing cannot abort anything."""
        self.mock.fail_users = True
        iss = self.todo_issue()
        self.board("claim", iss["identifier"], "--by", "CLAUDE")
        self.assertNotIn("assigneeId", self.mock.updates[-1][1])
        self.assertIn("claimed:CLAUDE", self.label_names(iss))
        self.board("status", iss["identifier"], "completed")

    def test_a_refused_users_listing_degrades_to_a_label_only_claim(self):
        self.mock.fail_users = True
        iss = self.todo_issue()
        p = self.board("claim", iss["identifier"], "--by", "CLAUDE", extra=self.OWNED)
        self.assertNotIn("assigneeId", self.mock.updates[-1][1])
        self.assertIn("claimed:CLAUDE", self.label_names(iss))
        self.assertIn("ownership stays off", p.stderr)
        # The warning carries only allow-listed fields, never the response body.
        self.assertIn("error=GqlError", p.stderr)
        self.assertNotIn("users listing refused", p.stderr)

    def test_case_variants_of_one_seat_assign_nobody(self):
        """Two names differing only in case match the seat ambiguously, so the
        claim lands on the label rather than risking the wrong user."""
        self.mock.users["claude"] = {
            "id": "user-claude-lower",
            "name": "claude",
            "displayName": "claude",
            "active": True,
        }
        iss = self.todo_issue()
        self.board("claim", iss["identifier"], "--by", "CLAUDE", extra=self.OWNED)
        self.assertNotIn("assigneeId", self.mock.updates[-1][1])
        self.assertIn("claimed:CLAUDE", self.label_names(iss))

    def test_an_exact_name_wins_over_case_variants(self):
        self.mock.users["CLAUDE"] = {
            "id": "user-claude-upper",
            "name": "CLAUDE",
            "displayName": "CLAUDE",
            "active": True,
        }
        iss = self.todo_issue()
        self.board("claim", iss["identifier"], "--by", "CLAUDE", extra=self.OWNED)
        self.assertEqual(self.mock.updates[-1][1].get("assigneeId"), "user-claude-upper")

    def test_two_users_sharing_one_name_assign_nobody(self):
        """Two active users under one name (either could be paginated first)
        own nothing, so the claim cannot assign the wrong one by order."""
        self.mock.users["dup-a"] = {
            "id": "user-dup-a",
            "name": "Dup",
            "displayName": "Dup",
            "active": True,
        }
        self.mock.users["dup-b"] = {
            "id": "user-dup-b",
            "name": "Dup",
            "displayName": "Dup",
            "active": True,
        }
        iss = self.todo_issue()
        self.board(
            "claim", iss["identifier"], "--by", "DUP", extra=dict(self.OWNED, AGENT_SEAT="DUP")
        )
        self.assertNotIn("assigneeId", self.mock.updates[-1][1])
        self.assertIn("claimed:DUP", self.label_names(iss))

    def test_malformed_user_records_degrade_to_a_label_only_claim(self):
        """One malformed record (empty id, blank name, non-boolean active)
        degrades ownership to nothing instead of aborting the verb or serving
        a partial identity set:  the claim lands on the label, like the
        refused-listing path."""
        self.mock.users["bad-id"] = {
            "id": "",
            "name": "BadId",
            "displayName": "BadId",
            "active": True,
        }
        self.mock.users["bad-name"] = {
            "id": "user-bad-name",
            "name": "   ",
            "displayName": "bad",
            "active": True,
        }
        self.mock.users["bad-active"] = {
            "id": "user-bad-active",
            "name": "BadActive",
            "displayName": "BadActive",
            "active": "yes",
        }
        self.mock.users["none-id"] = {
            "id": None,
            "name": "NoneId",
            "displayName": "NoneId",
            "active": True,
        }
        iss = self.todo_issue()
        p = self.board("claim", iss["identifier"], "--by", "CLAUDE", extra=self.OWNED)
        self.assertNotIn("assigneeId", self.mock.updates[-1][1])
        self.assertIn("claimed:CLAUDE", self.label_names(iss))
        self.assertIn("unexpected shape", p.stderr)
        self.assertIn("ownership stays off", p.stderr)
        self.assertIn("Linear user response has an invalid", p.stderr)

    def test_parse_linear_users_rejects_malformed_records(self):
        """The boundary validator raises a precise ValueError on the first
        malformed record;  deactivated users skip silently and shared names
        collapse to nobody."""
        mod = load_cli()
        with self.assertRaisesRegex(ValueError, "must contain user objects"):
            mod.parse_linear_users([None])
        with self.assertRaisesRegex(ValueError, "invalid id"):
            mod.parse_linear_users([{"id": "  ", "name": "X", "active": True}])
        with self.assertRaisesRegex(ValueError, "invalid name"):
            mod.parse_linear_users([{"id": "u", "name": "", "active": True}])
        with self.assertRaisesRegex(ValueError, "invalid active flag"):
            mod.parse_linear_users([{"id": "u", "name": "X", "active": "yes"}])
        users = mod.parse_linear_users(
            [
                {"id": "user-a", "name": "A", "active": True},
                {"id": "user-old", "name": "Old", "active": False},
            ]
        )
        self.assertEqual(users, {"A": "user-a"})
        users = mod.parse_linear_users(
            [
                {"id": "u1", "name": "Dup", "active": True},
                {"id": "u2", "name": "Dup", "active": True},
            ]
        )
        self.assertIsNone(users["Dup"])

    def test_gql_summary_logs_only_allow_listed_fields(self):
        """An external error is logged as error name, status, code, and
        request id:  never the response body."""
        mod = load_cli()
        err = mod.GqlError(
            [{"message": "secret body", "extensions": {"code": "BAD"}}],
            status=400,
            request_id="req-1",
        )
        summary = mod.gql_summary(err)
        self.assertIn("error=GqlError", summary)
        self.assertIn("status=400", summary)
        self.assertIn("code=BAD", summary)
        self.assertIn("request_id=req-1", summary)
        self.assertNotIn("secret body", summary)

    def test_a_cached_answer_is_used_without_a_refresh(self):
        iss = self.todo_issue()
        self.board("claim", iss["identifier"], "--by", "CLAUDE", extra=self.OWNED)
        # Linear gains a case variant after the snapshot:  a refresh would see
        # two matches and assign nobody, so a mirrored owner proves the cache
        # answered without one.
        self.mock.users["claude"] = {
            "id": "user-claude-lower",
            "name": "claude",
            "displayName": "claude",
            "active": True,
        }
        iss2 = self.todo_issue(title="Second probe")
        self.board("claim", iss2["identifier"], "--by", "CLAUDE", extra=self.OWNED)
        self.assertEqual(self.mock.updates[-1][1].get("assigneeId"), "user-claude")

    def test_a_refused_assignee_still_lands_the_claim(self):
        """A user deactivated after the snapshot is refused by Linear;  the
        claim retries without the owner instead of losing state and labels.
        The refused update applies nothing, so the retry carries the state
        and the labels itself."""
        iss = self.todo_issue()
        self.board("claim", iss["identifier"], "--by", "CLAUDE", extra=self.OWNED)
        self.mock.users["Claude"]["active"] = False
        iss2 = self.todo_issue(title="Second probe")
        p = self.board("claim", iss2["identifier"], "--by", "CLAUDE", extra=self.OWNED)
        first, second = self.mock.updates[-2], self.mock.updates[-1]
        self.assertEqual(first[1].get("assigneeId"), "user-claude")
        self.assertNotIn("assigneeId", second[1])
        self.assertIn("stateId", second[1])
        self.assertIn("addedLabelIds", second[1])
        self.assertEqual(iss2["state"], "In Progress")
        self.assertIn("claimed:CLAUDE", self.label_names(iss2))
        self.assertTrue(any("CLAIMED" in c for c in self.comments_of(iss2)))
        self.assertIsNone(iss2.get("assignee"))
        self.assertIn("refused the assignee", p.stderr)
        self.assertIn("retrying the claim without", p.stderr)
        self.assertNotIn("landed without an owner", p.stderr)
        # The warning carries only allow-listed fields, never the response body.
        self.assertIn("error=GqlError", p.stderr)
        self.assertNotIn("not found or deactivated", p.stderr)
        self.assertIn("(Linear refused the assignee; the label holds the claim)", p.stdout)

    def test_a_failed_retry_reports_no_landing(self):
        """When the retry after a refused assignee also fails, the log must
        not claim the landing it never made, and the issue stays untouched."""
        iss = self.todo_issue()
        self.board("claim", iss["identifier"], "--by", "CLAUDE", extra=self.OWNED)
        self.mock.users["Claude"]["active"] = False
        self.mock.fail_ownerless_updates = True
        iss2 = self.todo_issue(title="Second probe")
        p = self.board("claim", iss2["identifier"], "--by", "CLAUDE", extra=self.OWNED, code=1)
        self.assertIn("refused the assignee", p.stderr)
        self.assertIn("retrying the claim without", p.stderr)
        self.assertNotIn("landed", p.stderr)
        self.assertEqual(iss2["state"], "Todo")
        self.assertNotIn("claimed:CLAUDE", self.label_names(iss2))
        self.assertEqual(self.comments_of(iss2), [])

    def test_a_refused_assignee_clear_still_lands_the_steal(self):
        """A steal by a seat with no Linear user clears the stale owner;  when
        Linear refuses the clear, the steal retries without the owner field
        instead of losing the claim.  The stale assignee lingers visibly:  the
        claim matters more than the mirror."""
        iss = self.todo_issue()
        self.board(
            "claim", iss["identifier"], "--by", "CODEX", extra=dict(self.OWNED, AGENT_SEAT="CODEX")
        )
        self.assertEqual(iss.get("assignee"), "user-codex")
        self.mock.fail_assignee_clear = True
        p = self.board(
            "claim",
            iss["identifier"],
            "--by",
            "SENTRY",
            "--steal",
            "--reason",
            "CODEX is gone",
            extra=dict(self.OWNED, AGENT_SEAT="SENTRY"),
        )
        first, second = self.mock.updates[-2], self.mock.updates[-1]
        self.assertIn("assigneeId", first[1])
        self.assertIsNone(first[1].get("assigneeId"))
        self.assertNotIn("assigneeId", second[1])
        self.assertEqual(iss["state"], "In Progress")
        self.assertIn("claimed:SENTRY", self.label_names(iss))
        self.assertTrue(any("CLAIMED" in c for c in self.comments_of(iss)))
        self.assertEqual(iss.get("assignee"), "user-codex")
        self.assertIn("refused the assignee clear", p.stderr)
        self.assertIn("retrying the claim without it", p.stderr)

    def test_a_userless_seat_does_not_repay_teams_and_labels(self):
        """A user miss re-reads only the users listing, so a seat with no
        Linear user never repays teams and labels on a warm cache."""
        self.mock._label("claimed:SENTRY", None)
        self.mock._label("seat:SENTRY", None)
        iss = self.todo_issue()
        self.board("claim", iss["identifier"], "--by", "CLAUDE", extra=self.OWNED)
        teams = self.mock.ops.count("BoardTeams")
        labels = self.mock.ops.count("BoardLabels")
        iss2 = self.todo_issue(title="Second probe")
        self.board(
            "claim",
            iss2["identifier"],
            "--by",
            "SENTRY",
            extra=dict(self.OWNED, AGENT_SEAT="SENTRY"),
        )
        self.assertEqual(self.mock.ops.count("BoardTeams"), teams)
        self.assertEqual(self.mock.ops.count("BoardLabels"), labels)
        self.assertNotIn("assigneeId", self.mock.updates[-1][1])
        self.assertIn("claimed:SENTRY", self.label_names(iss2))

    def test_closing_an_unclaimed_issue_keeps_a_human_assignee(self):
        """Close clears the assignee only on a claimed issue, like release;
        closing what no seat claimed keeps whoever owns it."""
        iss = self.todo_issue()
        iss["assignee"] = "user-claude"
        self.board("status", iss["identifier"], "completed", extra=self.OWNED)
        self.assertNotIn("assigneeId", self.mock.updates[-1][1])
        self.assertEqual(iss.get("assignee"), "user-claude")

    def test_duplicate_of_an_unclaimed_issue_keeps_a_human_assignee(self):
        """The duplicate cleanup path carries the same claimed-only guard as
        every other close:  open-loop labels go, the human assignee stays."""
        iss = self.todo_issue()
        canon = self.todo_issue(title="Canonical")
        iss["labels"].append(self.mock._label("needs-owner", None))
        iss["assignee"] = "user-claude"
        self.board(
            "status", iss["identifier"], "duplicate", "--of", canon["identifier"], extra=self.OWNED
        )
        self.assertNotIn("assigneeId", self.mock.updates[-1][1])
        self.assertIn("removedLabelIds", self.mock.updates[-1][1])
        self.assertEqual(iss.get("assignee"), "user-claude")

    def test_the_claimed_line_names_the_ownership_outcome(self):
        """With the flag on, the printed claimed line says whether the owner
        mirrored;  without the flag the line carries no ownership note."""
        iss = self.todo_issue()
        p = self.board("claim", iss["identifier"], "--by", "CLAUDE", extra=self.OWNED)
        self.assertIn("(assignee CLAUDE)", p.stdout)
        iss2 = self.todo_issue(title="Second probe")
        p = self.board(
            "claim",
            iss2["identifier"],
            "--by",
            "SENTRY",
            extra=dict(self.OWNED, AGENT_SEAT="SENTRY"),
        )
        self.assertIn("(no Linear user for the seat)", p.stdout)
        iss3 = self.todo_issue(title="Third probe")
        p = self.board("claim", iss3["identifier"], "--by", "CLAUDE")
        self.assertNotIn("(assignee", p.stdout)
        self.assertNotIn("(no Linear user", p.stdout)

    def test_a_refused_assignee_clear_still_lands_the_status_change(self):
        """Closing folds the clear into the mandatory state update;  when Linear
        refuses it, the status retries without the owner instead of losing the
        state change, the label removal, or the comment."""
        iss = self.todo_issue()
        self.board("claim", iss["identifier"], "--by", "CLAUDE", extra=self.OWNED)
        iss["labels"].append(self.mock._label("needs-owner", None))
        self.mock.fail_assignee_clear = True
        p = self.board("status", iss["identifier"], "completed", extra=self.OWNED)
        self.assertEqual(p.returncode, 0)
        first, second = self.mock.updates[-2], self.mock.updates[-1]
        self.assertIn("assigneeId", first[1])
        self.assertIsNone(first[1].get("assigneeId"))
        self.assertNotIn("assigneeId", second[1])
        self.assertIn("stateId", second[1])
        self.assertIn("removedLabelIds", second[1])
        self.assertEqual(iss["state"], "Done")
        self.assertNotIn("needs-owner", self.label_names(iss))
        self.assertTrue(any("Status: completed" in c for c in self.comments_of(iss)))
        self.assertEqual(iss.get("assignee"), "user-claude")
        self.assertIn("refused the assignee clear", p.stderr)
        self.assertIn("retrying the status change without it", p.stderr)
        self.assertIn("error=GqlError", p.stderr)
        self.assertNotIn("assignee clear refused", p.stderr)

    def test_a_refused_assignee_clear_still_lands_the_duplicate_cleanup(self):
        """The duplicate cleanup runs after the relation landed;  a refused clear
        there must not fail the verb and skip the comment.  Labels still go, the
        comment still posts, and only a label-removal failure aborts."""
        iss = self.todo_issue()
        canon = self.todo_issue(title="Canonical")
        self.board("claim", iss["identifier"], "--by", "CLAUDE", extra=self.OWNED)
        iss["labels"].append(self.mock._label("needs-owner", None))
        self.mock.fail_assignee_clear = True
        p = self.board(
            "status", iss["identifier"], "duplicate", "--of", canon["identifier"], extra=self.OWNED
        )
        self.assertEqual(p.returncode, 0)
        first, second = self.mock.updates[-2], self.mock.updates[-1]
        self.assertIn("assigneeId", first[1])
        self.assertIsNone(first[1].get("assigneeId"))
        self.assertNotIn("assigneeId", second[1])
        self.assertIn("removedLabelIds", second[1])
        self.assertEqual(iss["state"], "Duplicate")
        self.assertNotIn("needs-owner", self.label_names(iss))
        self.assertTrue(any("Duplicate of" in c for c in self.comments_of(iss)))
        self.assertEqual(iss.get("assignee"), "user-claude")
        self.assertIn("refused the assignee clear", p.stderr)
        self.assertIn("retrying the duplicate cleanup without it", p.stderr)
        self.assertIn("error=GqlError", p.stderr)
        self.assertNotIn("assignee clear refused", p.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
