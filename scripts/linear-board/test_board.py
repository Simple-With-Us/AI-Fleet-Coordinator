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
attribution label, or when anything writes an issue straight into the
Duplicate state.  It can also rate-limit single operations (limit_ops), to
stop a verb part way between two of its requests.
"""
from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
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
CONN_FIELDS = ("issues", "comments", "labels", "issueLabels", "teams", "states", "attachments",
               "relations", "inverseRelations", "attachmentsForURL")
CONN_RE = re.compile(r"\b(" + "|".join(CONN_FIELDS) + r")\b(\s*\(([^)]*)\))?\s*\{")
STATES = [("Backlog", "backlog"), ("Todo", "unstarted"), ("In Progress", "started"), ("In Review", "started"),
          ("Done", "completed"), ("Deployed", "completed"), ("Canceled", "canceled"), ("Won't Fix", "canceled"),
          ("Duplicate", "duplicate"), ("Parked", "backlog")]


def linear_markdown(text: str) -> str:
    """What Linear hands back for a description it stored (observed on
    imported issues, Sat, Oct 10, 2026):  [ ] and ~ escaped outside code
    spans, and - bullets turned into * bullets.  Comment bodies come back as
    sent, so the mock stores those unchanged."""
    def esc(seg):
        return re.sub(r"([\[\]~])", r"\\\1", seg)
    out, pos = [], 0
    for m in re.finditer(r"(`+)(.+?)\1", text, re.S):
        out.append(esc(text[pos:m.start()]))
        out.append(m.group(0))
        pos = m.end()
    out.append(esc(text[pos:]))
    return re.sub(r"(?m)^(\s*)- ", r"\1* ", "".join(out))


def hy(h: str) -> str:
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"


# Board ids used by the seed data (32 hex, as on the board).
B_A = "236a2fa7e2ab4d24be03b0414750e0d8"   # OPS-10, Todo, P1, three comments
B_B = "9f3afe1a801747d6933486954c70347d"   # OPS-11, In Progress, claimed by CODEX
B_C = "4ca246e5aaaa4bbb8ccc0ddd1eee2fff"   # ST-5, Todo, P2
B_D = "51ab845251124a499cf232f6572dadf8"   # merged into CT-7 (another uuid)
D_LINEAR = "8b181573-f795-435a-98bb-bdb795903f04"
B_F = "abcdef12000040008000000000000001"   # ambiguous prefix pair
B_G = "abcdef12000040008000000000000002"
B_H = "c0ffee00111142228333444455556666"   # CT-8, imported with an external uid footer


class MockLinear:
    def __init__(self):
        self.lock = threading.Lock()
        self.page_cap = 2
        self.ops: list[str] = []
        self.violations: list[str] = []
        self.mints = 0
        self.scopes: list[str] = []
        self.tokens: set[str] = set()
        self.ratelimit400 = 0
        self.limit_ops: dict = {}       # operation name -> requests to rate-limit (400 RATELIMITED)
        self.http429 = 0
        self.reset_in_ms = 50
        self.revoke_all_tokens = False
        self.viewer = {"id": "app-user-1", "name": "Fleet", "displayName": "Fleet",
                       "email": "fleet@oauthapp.linear.app", "app": True}
        self.org = {"name": "Simple With Us", "urlKey": "simple-with-us"}
        self.teams: dict = {}
        for i, key in enumerate(("OPS", "ST", "CT", "AFC")):
            self.teams[key] = {"id": f"team-{key}", "name": key + " team", "n": 0,
                               "states": {n: {"id": f"st-{key}-{j}", "name": n, "type": t}
                                          for j, (n, t) in enumerate(STATES)}}
        self.labels: dict = {}
        for g, kids in (("Severity", ["P0", "P1", "P2", "P3", "P4"]), ("Env", ["Mac", "Cloud"]),
                        ("Kind", ["agent-report", "review-finding", "effort-row", "github-issue", "sentry"])):
            self._label(g, None)
            for k in kids:
                self._label(k, g)
        for name in ("seat:CLAUDE", "seat:CODEX", "claimed:CODEX", "stale-claim", "imported", "app:agent-bar"):
            self._label(name, None)
        self.issues: dict = {}
        self.comments: dict = {}
        self.relations: list = []
        self.attachments: list = []
        self._seed()

    # ----------------------------------------------------------- seed
    def _label(self, name, parent, lid=None):
        lid = lid or f"lbl-{(parent + '-') if parent else ''}{name}"
        self.labels[lid] = {"id": lid, "name": name, "parent": parent, "team": None,
                            "isGroup": parent is None and name in ("Severity", "Env", "Kind")}
        return lid

    def lid(self, key):
        for lb in self.labels.values():
            k = f"{lb['parent']}/{lb['name']}" if lb["parent"] else lb["name"]
            if k == key:
                return lb["id"]
        raise KeyError(key)

    def add_issue(self, uid, team, state, title, priority=0, labels=(), created="2026-10-01T12:00:00Z",
                  description="*No description on the board.*"):
        t = self.teams[team]
        t["n"] += 1
        ident = f"{team}-{t['n']}"
        self.issues[uid] = {"id": uid, "identifier": ident, "team": team, "state": state, "title": title,
                            "priority": priority, "labels": [self.lid(k) for k in labels],
                            "createdAt": created, "updatedAt": created, "description": description}
        return self.issues[uid]

    def add_comment(self, issue_id, body, created, user="Jay"):
        cid = str(uuid.uuid4())
        self.comments[cid] = {"id": cid, "issue": issue_id, "body": body, "createdAt": created,
                              "user": {"name": user}}
        return cid

    def _seed(self):
        for _ in range(9):           # OPS-1..9 are filler so OPS-10 matches the real identifier
            self.teams["OPS"]["n"] += 1
        a = self.add_issue(hy(B_A), "OPS", "Todo", "Fleet daily digest", 2, ["Severity/P1", "Kind/agent-report", "imported"],
                           "2026-10-02T12:00:00Z",
                           "Digest body text.\n\n## Recommended fix\n\nShip it.\n\n---\n\n**Provenance (migrated)**\n\n"
                           "* Board row: `x` (kind agent-report, app fleet-infra)\n  * Reported by grok, addressed by claude\n"
                           "  * Env mac, location \\~/apps/old @ main\n\n<!-- migrated-from: board:x -->")
        self.add_comment(a["id"], "**claude** · Thu, Oct 2, 2026, 7:00am (mac, ~/apps/x)\n\nFirst look.\n\n"
                         "<!-- migrated-comment: k author:claude at:z -->", "2026-10-02T13:00:00Z")
        self.add_comment(a["id"], "**codex** · Thu, Oct 2, 2026, 8:00am\n\nSecond look.", "2026-10-02T14:00:00Z")
        self.add_comment(a["id"], "**Resolution, by CLAUDE** · Thu, Oct 2, 2026, 9:00am\n\nOld resolution.",
                         "2026-10-02T15:00:00Z")
        self.add_issue(hy(B_B), "OPS", "In Progress", "Listener work", 1,
                       ["Severity/P0", "Kind/agent-report", "seat:CODEX", "claimed:CODEX", "Env/Mac"],
                       "2026-10-03T12:00:00Z")
        self.add_issue(hy(B_C), "ST", "Todo", "Socratic chart bug", 3, ["Severity/P2", "Kind/review-finding"],
                       "2026-10-04T12:00:00Z")
        self.teams["CT"]["n"] = 6
        self.add_issue(D_LINEAR, "CT", "Done", "Merged GitHub issue", 0, ["Kind/github-issue"], "2026-09-01T12:00:00Z")
        self.add_issue(hy(B_H), "CT", "Todo", "Legal compliance five", 4, ["Severity/P3", "Kind/review-finding"],
                       "2026-09-02T12:00:00Z",
                       "Text.\n\n---\n\n* External uid: `legal/LEGAL-05`\n")
        self.add_issue(hy(B_F), "AFC", "Todo", "Ambiguous one", 0, ["Kind/agent-report"], "2026-09-05T12:00:00Z")
        self.add_issue(hy(B_G), "AFC", "Todo", "Ambiguous two", 0, ["Kind/agent-report", "app:agent-bar"],
                       "2026-09-06T12:00:00Z")
        self.add_issue(str(uuid.uuid4()), "OPS", "Todo", "Low one", 4, ["Severity/P3"], "2026-09-07T12:00:00Z")
        self.add_issue(str(uuid.uuid4()), "OPS", "Todo", "Lowest one", 4, ["Severity/P4"], "2026-09-08T12:00:00Z")

    # ----------------------------------------------------------- rendering
    def label_node(self, lid):
        lb = self.labels[lid]
        return {"id": lid, "name": lb["name"], "parent": {"name": lb["parent"]} if lb["parent"] else None}

    def node(self, iss):
        t = self.teams[iss["team"]]
        st = t["states"][iss["state"]]
        return {
            "id": iss["id"], "identifier": iss["identifier"], "title": iss["title"],
            "url": f"https://linear.app/simple-with-us/issue/{iss['identifier']}", "priority": iss["priority"],
            "createdAt": iss["createdAt"], "updatedAt": iss["updatedAt"], "description": iss["description"],
            "state": dict(st), "team": {"id": t["id"], "key": iss["team"], "name": t["name"]},
            "labels": {"nodes": [self.label_node(x) for x in iss["labels"]]},
            "attachments": {"nodes": [{"url": a["url"], "title": a["title"]} for a in self.attachments
                                      if a["issue"] == iss["id"]]},
            "relations": {"nodes": [{"type": r["type"], "relatedIssue": self.brief(r["related"])}
                                    for r in self.relations if r["issue"] == iss["id"]]},
            "inverseRelations": {"nodes": [{"type": r["type"], "issue": self.brief(r["issue"])}
                                           for r in self.relations if r["related"] == iss["id"]]},
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
        chunk = items[start:start + min(first, self.page_cap)]
        end = start + len(chunk)
        return {"pageInfo": {"hasNextPage": end < len(items), "endCursor": str(end)}, "nodes": chunk}

    # ----------------------------------------------------------- operations
    def handle(self, op, q, v):
        for m in CONN_RE.finditer(q):
            if "first:" not in (m.group(3) or ""):
                self.violations.append(f"{op}: {m.group(1)} without first:")
        if op == "BoardWhoami":
            return {"viewer": self.viewer, "organization": self.org}, None
        if op == "BoardTeams":
            nodes = [{"id": t["id"], "key": k, "name": t["name"],
                      "issueCount": sum(1 for i in self.issues.values() if i["team"] == k),
                      "states": {"nodes": list(t["states"].values())}} for k, t in self.teams.items()]
            return {"teams": self.page(nodes, v.get("first"), v.get("after"))}, None
        if op == "BoardLabels":
            nodes = [{"id": lb["id"], "name": lb["name"], "isGroup": lb["isGroup"],
                      "parent": {"name": lb["parent"]} if lb["parent"] else None, "team": None}
                     for lb in self.labels.values()]
            return {"issueLabels": self.page(nodes, v.get("first"), v.get("after"))}, None
        if op == "BoardIssue":
            iss = self.find(v["id"])
            if not iss:
                return None, [{"message": "Entity not found: Issue", "extensions": {"code": "INVALID_INPUT"}}]
            return {"issue": self.node(iss)}, None
        if op == "BoardComments":
            iss = self.find(v["id"])
            cs = sorted((c for c in self.comments.values() if c["issue"] == iss["id"]),
                        key=lambda c: c["createdAt"], reverse=True)
            nodes = [{"id": c["id"], "body": c["body"], "createdAt": c["createdAt"], "user": c["user"]} for c in cs]
            return {"issue": {"comments": self.page(nodes, v.get("first"), v.get("after"))}}, None
        if op in ("BoardList", "BoardCount", "BoardRelabel"):
            hits = [i for i in self.issues.values() if self.match(i, v.get("filter"))]
            hits.sort(key=lambda i: i["createdAt"], reverse=True)
            nodes = [self.node(i) for i in hits]
            return {"issues": self.page(nodes, v.get("first"), v.get("after"))}, None
        if op == "BoardUidAttachment":
            nodes = [{"issue": self.brief(a["issue"])} for a in self.attachments if a["url"] == v["url"]]
            return {"attachmentsForURL": self.page(nodes, v.get("first"), v.get("after"))}, None
        if op == "BoardCommentOne":
            c = self.comments.get(v["id"])
            return ({"comment": {"id": c["id"]}}, None) if c else \
                (None, [{"message": "Entity not found: Comment"}])
        if op == "BoardFile":
            inp = v["input"]
            if inp.get("id") in self.issues:
                return {"issueCreate": None}, [{"message": "Issue with this id already exists"}]
            team = next(k for k, t in self.teams.items() if t["id"] == inp["teamId"])
            st = next(s for s in self.teams[team]["states"].values() if s["id"] == inp.get("stateId"))
            if st["type"] == "duplicate":
                self.violations.append("issueCreate straight into Duplicate")
                return {"issueCreate": None}, [{"message": "invalid state"}]
            t = self.teams[team]
            t["n"] += 1
            now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            iid = inp.get("id") or str(uuid.uuid4())
            self.issues[iid] = {"id": iid, "identifier": f"{team}-{t['n']}", "team": team, "state": st["name"],
                                "title": inp["title"], "priority": inp.get("priority", 0),
                                "labels": list(inp.get("labelIds") or []), "createdAt": now, "updatedAt": now,
                                "description": linear_markdown(inp.get("description") or "")}
            for lid in self.issues[iid]["labels"]:
                if lid not in self.labels:
                    self.violations.append(f"unknown label id {lid}")
            return {"issueCreate": {"success": True, "issue": self.node(self.issues[iid])}}, None
        if op == "BoardUpdate":
            iss = self.find(v["id"])
            inp = v["input"]
            if "labelIds" in inp:
                self.violations.append("issueUpdate with labelIds (replaces every label)")
            if "stateId" in inp:
                st = next(s for s in self.teams[iss["team"]]["states"].values() if s["id"] == inp["stateId"])
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
                if lid not in self.labels:
                    return {"issueUpdate": None}, [{"message": f"label {lid} not found"}]
                if lid not in iss["labels"]:
                    iss["labels"].append(lid)
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
            self.comments[cid] = {"id": cid, "issue": iss["id"], "body": inp["body"],
                                  "createdAt": f"2026-10-10T{20 + len(self.comments) // 3600:02d}:{len(self.comments) // 60 % 60:02d}:{len(self.comments) % 60:02d}.000Z",
                                  "user": {"name": "Fleet"}}
            return {"commentCreate": {"success": True, "comment": {"id": cid}}}, None
        if op == "BoardRelate":
            inp = v["input"]
            self.relations.append({"id": inp.get("id"), "type": inp["type"], "issue": inp["issueId"],
                                   "related": inp["relatedIssueId"]})
            if inp["type"] == "duplicate":
                self.issues[inp["issueId"]]["state"] = "Duplicate"   # Linear's own move
            return {"issueRelationCreate": {"success": True, "issueRelation": {"id": inp.get("id")}}}, None
        if op == "BoardAttach":
            inp = v["input"]
            self.attachments = [a for a in self.attachments if not (a["issue"] == inp["issueId"] and a["url"] == inp["url"])]
            self.attachments.append({"issue": inp["issueId"], "url": inp["url"], "title": inp.get("title")})
            return {"attachmentCreate": {"success": True, "attachment": {"id": "att"}}}, None
        if op == "BoardLabelCreate":
            inp = v["input"]
            self._label(inp["name"], None, inp.get("id"))
            return {"issueLabelCreate": {"success": True, "issueLabel": {"id": inp.get("id"), "name": inp["name"]}}}, None
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
                    if form.get("client_id") != CLIENT_ID or form.get("client_secret") != CLIENT_SECRET:
                        return self._send(401, {"error": "invalid_client", "error_description": "bad client"})
                    mock.mints += 1
                    tok = f"lin_oauth_mocktoken_{mock.mints}_{uuid.uuid4().hex}"
                    mock.tokens.add(tok)
                    return self._send(200, {"access_token": tok, "token_type": "Bearer",
                                            "expires_in": 2592000, "scope": form.get("scope")})
                if self.path != "/graphql":
                    return self._send(404, {})
                auth = self.headers.get("Authorization") or ""
                if not auth.startswith("Bearer ") or auth[7:] not in mock.tokens or mock.revoke_all_tokens:
                    return self._send(401, {"errors": [{"message": "Authentication required",
                                                        "extensions": {"code": "AUTHENTICATION_ERROR"}}]})
                req = json.loads(body or b"{}")
                q = req.get("query") or ""
                m = re.search(r"\b(query|mutation)\s+(\w+)", q)
                op = m.group(2) if m else "?"
                mock.ops.append(op)
                reset = str(int(time.time() * 1000) + mock.reset_in_ms)
                if mock.ratelimit400 > 0:
                    mock.ratelimit400 -= 1
                    return self._send(400, {"errors": [{"message": "Rate limit exceeded",
                                                        "extensions": {"code": "RATELIMITED"}}]},
                                      {"X-RateLimit-Requests-Reset": reset})
                if mock.limit_ops.get(op, 0) > 0:
                    mock.limit_ops[op] -= 1
                    return self._send(400, {"errors": [{"message": "Rate limit exceeded",
                                                        "extensions": {"code": "RATELIMITED"}}]},
                                      {"X-RateLimit-Requests-Reset": reset})
                if mock.http429 > 0:
                    mock.http429 -= 1
                    return self._send(429, {"errors": [{"message": "Too many requests"}]},
                                      {"X-RateLimit-Requests-Reset": reset})
                data, errs = mock.handle(op, q, req.get("variables") or {})
                out = {"data": data}
                if errs:
                    out["errors"] = errs
                return self._send(200, out)
    return H


class BoardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="board-test-"))
        self.mock = MockLinear()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.mock))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        port = self.server.server_address[1]
        (self.tmp / "home").mkdir()
        self.creds = self.tmp / "secrets" / "Fleet.env"
        self.creds.parent.mkdir(parents=True)
        self.creds.write_text(f'LINEAR_CLIENT_ID="{CLIENT_ID}"\nexport LINEAR_CLIENT_SECRET={CLIENT_SECRET}\n')
        self.token_cache = self.tmp / "secrets" / "cache" / "Fleet.token"
        ids = self.tmp / "board-ids.txt"
        ids.write_text("# test ids\n" + "\n".join([B_A, B_B, B_C, f"{B_D}\t{D_LINEAR}", B_F, B_G, B_H]) + "\n")
        self.env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(self.tmp / "home"),
            "LANG": "en_US.UTF-8", "AGENT_SEAT": "CLAUDE",
            "LINEAR_FLEET_ENV": str(self.creds), "LINEAR_FLEET_TOKEN_CACHE": str(self.token_cache),
            "LINEAR_BOARD_API_URL": f"http://127.0.0.1:{port}/graphql",
            "LINEAR_BOARD_TOKEN_URL": f"http://127.0.0.1:{port}/oauth/token",
            "LINEAR_BOARD_CACHE_DIR": str(self.tmp / "cache"), "LINEAR_BOARD_IDS": str(ids),
            "LINEAR_BOARD_BACKOFF_BASE": "0.01", "LINEAR_BOARD_DUP_WAIT": "0", "LINEAR_BOARD_WRITES": "1",
        }

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.assertEqual(self.mock.violations, [], "mock recorded contract violations")

    def board(self, *args, extra=None, drop=(), code=0):
        e = dict(self.env, **(extra or {}))
        for k in drop:
            e.pop(k, None)
        p = subprocess.run([PY, str(CLI), *args], env=e, capture_output=True, text=True, timeout=60)
        for tok in self.mock.tokens:
            self.assertNotIn(tok, p.stdout + p.stderr, "a token reached the output")
        self.assertNotIn(CLIENT_SECRET, p.stdout + p.stderr, "the client secret reached the output")
        if code is not None:
            self.assertEqual(p.returncode, code, f"exit {p.returncode}\nstdout:\n{p.stdout}\nstderr:\n{p.stderr}")
        return p

    def issue_by_ident(self, ident):
        return next(i for i in self.mock.issues.values() if i["identifier"] == ident)

    def label_names(self, iss):
        return sorted((self.mock.labels[x]["parent"] + "/" if self.mock.labels[x]["parent"] else "")
                      + self.mock.labels[x]["name"] for x in iss["labels"])

    def comments_of(self, iss):
        return [c["body"] for c in sorted(self.mock.comments.values(), key=lambda c: c["createdAt"])
                if c["issue"] == iss["id"]]


GROK_ALIAS_SPELLINGS = ("GROK-BUILD", "Grok-Build", "grok-build", "grok_build", "Grok Build", "GROK.BUILD",
                        "Grok (Build)", "grok/build/", "GrokBuild", "GROK--BUILD!!", "  grok-build  ", "@Grok-Build",
                        "[GROK-BUILD]", "`grok-build`")
GROK_NOT_ALIASES = ("GROKBUILDER", "grok-build-bot", "grok2build", "GROK-WEB", "GROK-BOT", "GB-COMPILER",
                    "BF-BUILDER", "Grok (Web/iOS)", "GROK", "", "MONET", "RENOIR", "HARNESS", "MINIMAX",
                    "DEEPSEEK", "KIMI", "build", "grok")


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

    def test_pattern_is_the_one_in_fleet_apps_json(self):
        """The board script imports no package, so it carries its own copy of the GROK row's aliasPattern."""
        rows = json.loads((HERE.parents[1] / "fleet-apps.json").read_text())["seats"]
        grok = next(r for r in rows if r["tag"] == "GROK")
        self.assertEqual(load_cli().GROK_ALIAS_PATTERN, grok["aliasPattern"])


class SafetyTests(BoardTest):
    def test_writes_require_agent_seat(self):
        for args in (["file", "--title", "x"], ["claim", B_A[:8], "--by", "CLAUDE"],
                     ["status", B_A[:8], "completed"], ["comment", B_A[:8], "--by", "CLAUDE", "--text", "x"]):
            p = self.board(*args, drop=("AGENT_SEAT",), code=2)
            self.assertIn("AGENT_SEAT is not set", p.stderr)
        self.assertEqual(self.mock.ops, [])
        self.assertEqual(self.mock.mints, 0)

    def test_reads_work_without_agent_seat(self):
        """AGENT_SEAT is unset in plain shells, `zsh -lic` and the listener's `env -i`; the skills' reads
        (`board stats`, `board list --status open,in_progress --severity P0,P1 --limit 25`) must still run."""
        p = self.board("list", "--status", "open,in_progress", "--severity", "P0,P1", "--limit", "25",
                       drop=("AGENT_SEAT",))
        self.assertIn("2 shown of", p.stdout)
        self.board("show", "OPS-10", drop=("AGENT_SEAT",))
        self.assertIn("total 9", self.board("stats", drop=("AGENT_SEAT",)).stdout)
        p = self.board("whoami", drop=("AGENT_SEAT",))
        self.assertIn("AGENT_SEAT is not set", p.stdout)
        self.assertIn("viewer:      Fleet (app user)", p.stdout)

    def test_missing_credentials(self):
        self.creds.unlink()
        p = self.board("list", extra={"LINEAR_API" + "_KEY": "lin_api_should_be_ignored"}, code=4)
        self.assertIn("no Fleet credentials", p.stderr)
        self.assertEqual((self.mock.mints, self.mock.ops), (0, []))

    def test_cloud_seat_credentials_from_environment(self):
        self.creds.unlink()
        p = self.board("whoami", extra={"LINEAR_CLIENT_ID": CLIENT_ID, "LINEAR_CLIENT_SECRET": CLIENT_SECRET})
        self.assertIn("credentials: environment", p.stdout)

    def test_credentials_file_must_be_env_file(self):
        p = self.board("list", extra={"LINEAR_FLEET_ENV": str(self.tmp / "secrets" / "keys")}, code=2)
        self.assertIn(".env file", p.stderr)

    def test_writes_disabled_by_default(self):
        for args in (["file", "--title", "x"], ["claim", B_A[:8], "--by", "CLAUDE"],
                     ["status", B_A[:8], "completed"], ["comment", B_A[:8], "--by", "CLAUDE", "--text", "x"]):
            p = self.board(*args, drop=("LINEAR_BOARD_WRITES",), code=7)
            self.assertIn("writes to Linear are off", p.stderr)
        self.assertEqual((self.mock.mints, self.mock.ops), (0, []))
        self.board("list", drop=("LINEAR_BOARD_WRITES",))       # reads still work
        self.board("whoami", drop=("LINEAR_BOARD_WRITES",))

    def test_by_must_match_seat(self):
        p = self.board("comment", B_A[:8], "--by", "CODEX", "--text", "hi", code=2)
        self.assertIn("does not match AGENT_SEAT=CLAUDE", p.stderr)
        self.board("comment", B_A[:8], "--by", "@claude", "--text", "hi")   # normalized match is fine
        self.assertEqual(self.mock.violations, [])

    def test_grok_build_seat_is_grok(self):
        """AGENT_SEAT and --by in any GROK-BUILD spelling sign as GROK, and another seat is still refused."""
        for seat_env in ("GROK-BUILD", "Grok (Build)"):
            for by in ("Grok-Build", "GROK", "grok_build"):
                p = self.board("comment", B_A[:8], "--by", by, "--text", f"alias {seat_env} {by}",
                               extra={"AGENT_SEAT": seat_env})
                self.assertIn("as GROK", p.stdout)
        p = self.board("comment", B_A[:8], "--by", "CODEX", "--text", "hi", extra={"AGENT_SEAT": "GROK-BUILD"}, code=2)
        self.assertIn("does not match AGENT_SEAT=GROK", p.stderr)
        p = self.board("comment", B_A[:8], "--by", "GROK-WEB", "--text", "hi", extra={"AGENT_SEAT": "GROK-BUILD"}, code=2)
        self.assertIn("does not match AGENT_SEAT=GROK", p.stderr)
        p = self.board("comment", B_A[:8], "--by", "GROKBUILDER", "--text", "hi", extra={"AGENT_SEAT": "GROKBUILDER"})
        self.assertIn("as GROKBUILDER", p.stdout)        # a near miss stays itself
        p = self.board("comment", B_A[:8], "--by", "GROK", "--text", "hi", extra={"AGENT_SEAT": "GROKBUILDER"}, code=2)
        self.assertIn("does not match AGENT_SEAT=GROKBUILDER", p.stderr)
        self.assertEqual(self.mock.violations, [])

    def test_url_override_must_be_loopback(self):
        p = self.board("list", extra={"LINEAR_BOARD_API_URL": "https://evil.example/graphql"}, code=2)
        self.assertIn("refusing", p.stderr)
        self.assertEqual(self.mock.mints, 0)

    def test_backend_fallback_execs_old_cli(self):
        old = self.tmp / "old-board"
        old.write_text("import sys\nprint('OLD BOARD', sys.argv[1:])\n")
        p = self.board("claim", "abc", "--by", "X", drop=("AGENT_SEAT", "LINEAR_BOARD_WRITES"),
                       extra={"BOARD_BACKEND": "mac-collab", "BOARD_MAC_COLLAB_CLI": str(old)})
        self.assertEqual(p.stdout.strip(), "OLD BOARD ['claim', 'abc', '--by', 'X']")
        self.assertEqual(self.mock.ops, [])

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
        self.assertIn("mints on this host: 1", p.stdout)
        self.assertIn("writes:      enabled", p.stdout)
        self.board("whoami")
        self.assertEqual(self.mock.mints, 1)
        self.assertEqual(self.mock.scopes, [EXPECTED_SCOPE])
        mode = stat.S_IMODE(self.token_cache.stat().st_mode)
        self.assertEqual(mode, 0o600)

    def test_whoami_refuses_a_human_viewer(self):
        self.mock.viewer = {"id": "u1", "name": "Jay", "displayName": "Jay", "email": "j@x", "app": False}
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
        self.assertIn("mints on this host: 2", p.stdout)
        self.assertEqual(self.mock.mints, 2)

    def test_rotated_credentials_mint_again(self):
        self.board("whoami")
        global CLIENT_SECRET
        old = CLIENT_SECRET
        try:
            CLIENT_SECRET = "rotated-secret-value"
            self.creds.write_text(f"LINEAR_CLIENT_ID={CLIENT_ID}\nLINEAR_CLIENT_SECRET={CLIENT_SECRET}\n")
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
        self.assertGreater(self.mock.ops.count("BoardList"), 2)   # followed cursors at two nodes a page

    def test_list_json_and_filters(self):
        rows = json.loads(self.board("list", "--json", "--status", "open,in_progress", "--severity", "P0,P1").stdout)
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
        p = self.board("show", B_D[:8])   # board id merged into an issue with another uuid
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
        self.assertEqual(len(d["comments"]), 3)                     # two pages of comments
        self.assertEqual(d["recommended_fix"], "Ship it.")
        self.assertEqual(d["severity"], "P1")
        self.assertEqual(d["id"], hy(B_A))

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
        p = self.board("file", "--title", "Scout drops rows on 502", "--app", "congress-trade", "--severity", "P1",
                       "--desc", "Details.", "--fix", "Retry.", "--env", "cloud", "--where", "~/x @ y",
                       "--url", "https://github.com/o/r/issues/1")
        m = re.match(r"filed ([0-9a-f-]{36})  \(P1 CT\)  CT-9$", p.stdout.strip())
        self.assertTrue(m, p.stdout)
        iss = self.mock.issues[m.group(1)]
        self.assertEqual((iss["state"], iss["priority"], iss["team"]), ("Todo", 2, "CT"))
        self.assertEqual(self.label_names(iss), ["Env/Cloud", "Kind/agent-report", "Severity/P1"])
        self.assertTrue(iss["description"].startswith("\\[CLAUDE\\] Filed with the board CLI."), iss["description"])
        self.assertIn("## Recommended fix\n\nRetry.", iss["description"])
        self.assertEqual([a["url"] for a in self.mock.attachments], ["https://github.com/o/r/issues/1"])
        d = json.loads(self.board("show", "CT-9", "--json").stdout)
        self.assertEqual((d["description"], d["recommended_fix"], d["reported_by"]), ("Details.", "Retry.", "CLAUDE"))

    def test_file_round_trips_through_linear_markdown(self):
        p = self.board("file", "--title", "Brackets", "--desc", "[note] see ~/x and `[code]`", "--fix",
                       "- step one\n- step two", "--where", "~/apps/lanes/X @ b", "--env", "Mac",
                       extra={"CLAUDE_CODE_SESSION_ID": "6db9ee77-0000"})
        iss = self.issue_by_ident(p.stdout.split()[-1])
        self.assertIn("\\[note\\] see \\~/x", iss["description"])        # the mock escaped it, as Linear does
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

    def test_file_uid_is_idempotent(self):
        p1 = self.board("file", "--title", "Effort row", "--app", "socratic-trade", "--uid", "effort-abc")
        n = len(self.mock.issues)
        p2 = self.board("file", "--title", "Effort row again", "--app", "ST", "--uid", "effort-abc")
        self.assertEqual(len(self.mock.issues), n)
        self.assertEqual(p1.stdout.split()[1], p2.stdout.split()[1])
        self.assertIn("already filed", p2.stdout)
        self.assertIn("https://board.jays.services/uid/ST/effort-abc", [a["url"] for a in self.mock.attachments])

    def test_file_uid_finds_imported_footer(self):
        n = len(self.mock.issues)
        p = self.board("file", "--title", "Legal compliance five", "--app", "congress", "--uid", "legal/LEGAL-05")
        self.assertEqual(len(self.mock.issues), n)
        self.assertIn(hy(B_H), p.stdout)
        self.assertIn("already filed", p.stdout)

    def test_file_uid_finds_attachment(self):
        target = self.mock.issues[hy(B_C)]
        self.mock.attachments.append({"issue": target["id"], "url": "https://board.jays.services/uid/ST/zulip%3A42",
                                      "title": "x"})
        p = self.board("file", "--title", "From Zulip", "--app", "trading", "--uid", "zulip:42")
        self.assertIn(target["id"], p.stdout)

    def test_claim_open_issue(self):
        p = self.board("claim", B_A[:8], "--by", "CLAUDE", "--env", "Mac", "--where", "~/apps/lanes/X @ claude/x",
                       extra={"CLAUDE_CODE_SESSION_ID": "b854d9ad-1111-2222-3333-444455556666"})
        self.assertEqual(p.stdout.strip(),
                         f"claimed {B_A[:8]} by CLAUDE -> in_progress  (sessions: b854d9ad-1111-2222-3333-444455556666)  OPS-10")
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

    def test_claim_refused_when_held_then_steal(self):
        p = self.board("claim", "OPS-11", "--by", "CLAUDE", code=3)
        self.assertIn("claimed by CODEX", p.stderr)
        p = self.board("claim", "OPS-11", "--by", "CLAUDE", "--steal", code=2)
        self.assertIn("--reason", p.stderr)
        self.assertEqual(self.mock.issues[hy(B_B)]["labels"].count(self.mock.lid("claimed:CODEX")), 1)
        self.board("claim", "OPS-11", "--by", "CLAUDE", "--steal", "--reason", "CODEX is capped out", "--env", "cloud")
        iss = self.mock.issues[hy(B_B)]
        names = self.label_names(iss)
        self.assertIn("claimed:CLAUDE", names)
        self.assertNotIn("claimed:CODEX", names)
        self.assertIn("seat:CODEX", names)      # attribution survives a steal
        self.assertIn("seat:CLAUDE", names)
        self.assertIn("Env/Cloud", names)
        self.assertNotIn("Env/Mac", names)
        self.assertIn("CLAIMED from CODEX: CODEX is capped out", self.comments_of(iss)[-1])
        self.board("claim", "OPS-11", "--by", "CLAUDE")             # re-claiming your own claim is fine

    def test_claim_creates_missing_seat_label(self):
        self.board("claim", "ST-1", "--by", "GROK-WEB", extra={"AGENT_SEAT": "GROK-WEB"})
        names = self.label_names(self.mock.issues[hy(B_C)])
        self.assertIn("seat:GROK-WEB", names)
        self.assertIn("claimed:GROK-WEB", names)

    def test_status_values_and_resolution(self):
        p = self.board("status", "OPS-10", "completed", "--resolution", "Landed in #2894.")
        self.assertEqual(p.stdout.strip(), f"{B_A[:8]} -> completed  OPS-10")
        iss = self.mock.issues[hy(B_A)]
        self.assertEqual(iss["state"], "Done")
        self.assertEqual(self.comments_of(iss)[-1], "[CLAUDE] Resolution (completed): Landed in #2894.")
        d = json.loads(self.board("show", "OPS-10", "--json").stdout)
        self.assertEqual(d["resolution"], "Landed in #2894.")
        for value, state in (("addressed", "In Review"), ("deployed", "Deployed"), ("wontfix", "Won't Fix"),
                             ("open", "Todo"), ("in_progress", "In Progress"), ("parked", "Parked")):
            self.board("status", B_C[:8], value)
            self.assertEqual(self.mock.issues[hy(B_C)]["state"], state)
        self.assertEqual(self.comments_of(self.mock.issues[hy(B_C)])[-1], "[CLAUDE] Status: parked")
        self.board("status", B_C[:8], "bogus", code=2)
        self.board("status", B_C[:8], "completed", "--of", "OPS-10", code=2)

    def test_status_duplicate_via_relation(self):
        p = self.board("status", B_C[:8], "duplicate", code=2)
        self.assertIn("needs --of", p.stderr)
        p = self.board("status", B_C[:8], "duplicate", "--of", "OPS-10", "--resolution", "Same digest bug.")
        self.assertEqual(p.stdout.strip(), f"{B_C[:8]} -> duplicate  ST-1")
        rel = self.mock.relations[-1]
        self.assertEqual((rel["type"], rel["issue"], rel["related"]), ("duplicate", hy(B_C), hy(B_A)))
        self.assertEqual(self.mock.issues[hy(B_C)]["state"], "Duplicate")
        self.assertNotIn("BoardUpdate", self.mock.ops)
        self.assertIn("Resolution (duplicate of OPS-10): Same digest bug.", self.comments_of(self.mock.issues[hy(B_C)])[-1])
        p = self.board("show", "OPS-10")
        self.assertIn("duplicated by: ST-1", p.stdout)
        self.board("status", "OPS-10", "duplicate", "--of", "OPS-10", code=2)

    def test_comment(self):
        p = self.board("comment", "OPS-10", "--by", "CLAUDE", "--text", "Verified on main.", "--env", "Mac",
                       "--where", "~/apps/lanes/A @ b", "--session", "s-1", "--session", "s-2")
        self.assertEqual(p.stdout.strip(), f"commented on {B_A[:8]} as CLAUDE  (sessions: s-1, s-2)  OPS-10")
        body = self.comments_of(self.mock.issues[hy(B_A)])[-1]
        self.assertEqual(body, "[CLAUDE·s1] Verified on main.\n\nWhere: Mac, ~/apps/lanes/A @ b\n\nSessions: s-1, s-2")
        p = self.board("show", "OPS-10")
        self.assertIn("CLAUDE (Mac, ~/apps/lanes/A @ b): Verified on main.", p.stdout)


def footer(line: str) -> str:
    """A description shaped like the importer's, as Linear hands it back."""
    return linear_markdown(
        "Body of the imported issue.\n\n---\n\n**Provenance (migrated from the fleet board and GitHub)**\n\n"
        "- Migration key: `board:x`\n- Board row: `x` (kind agent-report, app usage-monitor, status in_progress)\n"
        f"  - {line}\n  - Env mac, location sat, oct 3, 2026; ~/apps/x @ y\n\n<!-- migrated-from: board:x -->")


class ClaimModelTests(BoardTest):
    """seat:<TAG> labels are attribution (the import put them on reporters too); a claim is a claimed:<TAG>
    label, or for an imported In Progress issue without one, the provenance footer's "addressed by"."""

    def legacy(self, title, line, state="In Progress", labels=("seat:CLAUDE", "seat:CODEX"), team="AFC"):
        return self.mock.add_issue(str(uuid.uuid4()), team, state, title, 3,
                                   ["Severity/P2", "Kind/agent-report", "imported", *labels],
                                   "2026-10-05T12:00:00Z", footer(line))

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
        self.assertEqual((iss["labels"], iss["state"]), (before, "In Progress"))   # a refusal changes nothing
        self.board("claim", iss["identifier"], "--by", "CODEX", extra={"AGENT_SEAT": "CODEX"})
        names = self.label_names(iss)
        self.assertEqual([n for n in names if n.startswith(("seat:", "claimed:"))],
                         ["claimed:CODEX", "seat:CLAUDE", "seat:CODEX"])
        self.board("claim", iss["identifier"], "--by", "CLAUDE", code=3)         # still refused, by the marker now
        self.board("claim", iss["identifier"], "--by", "CODEX", extra={"AGENT_SEAT": "CODEX"})   # own claim again

    def test_steal_keeps_every_attribution_label(self):
        iss = self.legacy("Stolen", "Reported by claude, addressed by codex")
        p = self.board("claim", iss["identifier"], "--by", "CLAUDE", "--steal", code=2)
        self.assertIn("--reason", p.stderr)
        self.board("claim", iss["identifier"], "--by", "CLAUDE", "--steal", "--reason", "CODEX is capped out")
        names = self.label_names(iss)
        self.assertEqual([n for n in names if n.startswith(("seat:", "claimed:"))],
                         ["claimed:CLAUDE", "seat:CLAUDE", "seat:CODEX"])
        self.assertIn("CLAIMED from CODEX: CODEX is capped out", self.comments_of(iss)[-1])
        self.assertEqual(self.holder(iss), "CLAUDE")      # the marker wins over the old footer
        self.board("claim", iss["identifier"], "--by", "CODEX", extra={"AGENT_SEAT": "CODEX"}, code=3)

    def test_footer_forms_aliases_and_unmapped_claimants(self):
        cases = [("Addressed by designer", "BF-DESIGNER"),            # no reporter, role word, lower case
                 ("Reported by claude, codex, addressed by minimax", "MM"),
                 ("Reported by x, addressed by harness", "CLUTCH"),
                 ("Reported by x, addressed by grok-build", "GROK"),
                 ("Reported by claude, addressed by codex, cursor", "CODEX,CURSOR"),
                 ("Reported by claude, addressed by jay", None)]      # not a seat: nobody holds it
        for i, (line, want) in enumerate(cases):
            iss = self.legacy(f"Footer {i}", line)
            self.assertEqual(self.holder(iss), want, line)
        free = self.legacy("Unmapped claimant", "Reported by claude, addressed by jay")
        self.board("claim", free["identifier"], "--by", "CLAUDE")      # no holder, so no refusal

    def test_reporter_only_label_is_attribution_not_a_claim(self):
        """296 imported Todo/Backlog issues carry a reporter's seat label:  no @SEAT, not in --mine."""
        todo = self.legacy("Reported only", "Reported by codex", state="Todo", labels=("seat:CODEX",))
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
        self.assertEqual(sorted(r["identifier"] for r in mine), sorted(["OPS-11", iss["identifier"]]))
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
        self.board("status", "OPS-10", "completed")           # closing keeps who addressed it
        self.assertIn("claimed:CODEX", self.label_names(self.issue_by_ident("OPS-10")))

    def test_claiming_a_closed_issue_replaces_the_old_marker(self):
        self.board("claim", "ST-1", "--by", "CODEX", extra={"AGENT_SEAT": "CODEX"})
        self.board("status", "ST-1", "completed")
        self.board("claim", "ST-1", "--by", "CLAUDE")          # not In Progress, so no refusal
        names = [n for n in self.label_names(self.issue_by_ident("ST-1")) if n.startswith("claimed:")]
        self.assertEqual(names, ["claimed:CLAUDE"])


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
        self.assertEqual((iss["state"], "claimed:CLAUDE" in self.label_names(iss)), ("In Progress", True))
        self.assertEqual(self.claimed_comments("OPS-10"), [])
        self.mock.limit_ops.clear()
        p = self.board("claim", "OPS-10", "--by", "CLAUDE", extra=self.SESSION)       # the rerun finishes it
        self.assertTrue(p.stdout.startswith("claimed "), p.stdout)
        self.assertEqual(len(self.claimed_comments("OPS-10")), 1)
        p = self.board("claim", "OPS-10", "--by", "CLAUDE", extra=self.SESSION)       # and a third run adds nothing
        self.assertEqual(len(self.claimed_comments("OPS-10")), 1)
        self.assertIn("already on OPS-10", p.stderr)

    def test_status_stopped_before_its_comment(self):
        p = self.board("status", "OPS-10", "completed", "--resolution", "Landed in #5.",
                       extra=self.stop("BoardComment"), code=5)
        self.assertIn("OPS-10 moved to Done", p.stderr)
        self.assertEqual(self.issue_by_ident("OPS-10")["state"], "Done")
        self.mock.limit_ops.clear()
        for _ in range(2):
            self.board("status", "OPS-10", "completed", "--resolution", "Landed in #5.", extra=self.SESSION)
        done = [b for b in self.comments_of(self.issue_by_ident("OPS-10")) if "Landed in #5." in b]
        self.assertEqual(len(done), 1)

    def test_file_prints_the_id_before_its_attachments(self):
        n = len(self.mock.issues)
        p = self.board("file", "--title", "Needs an attachment", "--url", "https://github.com/o/r/issues/9",
                       extra=self.stop("BoardAttach"), code=5)
        m = re.match(r"filed ([0-9a-f-]{36})  \(-- OPS\)  (OPS-\d+)$", p.stdout.strip())
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
        self.assertEqual(p.stdout.strip(), f"commented on {B_A[:8]} as CLAUDE  (sessions: {self.SESSION['CLAUDE_CODE_SESSION_ID']})  OPS-10")
        self.assertIn("not posting it again", p.stderr)
        self.assertEqual(len(self.comments_of(self.issue_by_ident("OPS-10"))), before + 1)
        self.board(*args, extra={"CLAUDE_CODE_SESSION_ID": "0ther000-0000-4000-8000-000000000000"})   # new session
        self.board("comment", "OPS-10", "--by", "CLAUDE", "--text", "Different words.", extra=self.SESSION)
        self.assertEqual(len(self.comments_of(self.issue_by_ident("OPS-10"))), before + 3)

    def test_comment_ids_are_fixed_per_issue_seat_verb_and_text(self):
        cli = load_cli()
        a = cli.comment_id("i1", "CLAUDE", "comment", "body", ["s1"])
        self.assertEqual(a, cli.comment_id("i1", "CLAUDE", "comment", "body", ["s1"]))
        for other in (cli.comment_id("i2", "CLAUDE", "comment", "body", ["s1"]),
                      cli.comment_id("i1", "CODEX", "comment", "body", ["s1"]),
                      cli.comment_id("i1", "CLAUDE", "status", "body", ["s1"]),
                      cli.comment_id("i1", "CLAUDE", "comment", "other", ["s1"]),
                      cli.comment_id("i1", "CLAUDE", "comment", "body", ["s2"])):
            self.assertNotEqual(a, other)
        self.assertEqual(str(uuid.UUID(a)), a)


class DuplicateTargetTests(BoardTest):
    def test_duplicate_without_a_target_is_refused_with_the_way_out(self):
        p = self.board("status", "ST-1", "duplicate", code=2)
        self.assertIn("needs --of", p.stderr)
        self.assertIn("wontfix", p.stderr)
        p = self.board("status", "ST-1", "duplicate", "--resolution", "same bug as the commit deadbeef", code=2)
        self.assertIn("needs --of", p.stderr)
        self.assertEqual(self.mock.relations, [])

    def test_target_named_in_the_resolution(self):
        p = self.board("status", "ST-1", "duplicate", "--resolution", "Dup of ops-10: same digest bug.")
        self.assertEqual(p.stdout.strip(), f"{B_C[:8]} -> duplicate  ST-1")
        self.assertIn("taking --of OPS-10", p.stderr)
        rel = self.mock.relations[-1]
        self.assertEqual((rel["issue"], rel["related"]), (hy(B_C), hy(B_A)))
        self.assertIn("Resolution (duplicate of OPS-10): Dup of ops-10: same digest bug.",
                      self.comments_of(self.issue_by_ident("ST-1"))[-1])

    def test_target_forms(self):
        cli = load_cli()
        for text, want in (("duplicate of AFC-3", "AFC-3"), ("DUP OF CT-7.", "CT-7"), ("dup: `OPS-10`", "OPS-10"),
                           ("dupe of ST-1 (see PR)", "ST-1"), (f"duplicate of {hy(B_A)}", hy(B_A)),
                           (hy(B_A), hy(B_A)), ("OPS-10", "OPS-10"), ("dup of OPS-10, dup of OPS-10", "OPS-10"),
                           ("duplicate of 236a2fa7e2ab4d24be03b0414750e0d8", B_A)):
            self.assertEqual(cli.duplicate_target(text), want, text)
        for text in ("", "deadbeef", "duplicate of deadbeef", "fixed in a1b2c3d4", "duplicated by OPS-10",
                     "dup of OPS-10 and dup of ST-1", "dup of PR-123", "see OPS-10", "same as OPS-10",
                     "dupont-5"):
            self.assertIsNone(cli.duplicate_target(text), text)


class RelabelTests(BoardTest):
    """relabel_claims.py:  the owner-approved cutover step that gives imported live claims their marker."""

    def setUp(self):
        super().setUp()
        mk = lambda t, line, st, labels: self.mock.add_issue(     # noqa: E731
            str(uuid.uuid4()), "AFC", st, t, 3, ["Kind/agent-report", "imported", *labels], "2026-10-05T12:00:00Z",
            footer(line))
        self.a = mk("Claimed by codex", "Reported by claude, addressed by codex", "In Progress", ("seat:CLAUDE", "seat:CODEX"))
        self.b = mk("In review by claude", "Addressed by claude", "In Review", ("seat:CLAUDE",))
        self.c = mk("Nobody", "Reported by claude, addressed by jay", "In Progress", ("seat:CLAUDE",))
        self.d = mk("Done long ago", "Reported by claude, addressed by codex", "Done", ("seat:CLAUDE", "seat:CODEX"))
        self.labels_before = {i["id"]: list(i["labels"]) for i in self.mock.issues.values()}

    def relabel(self, *args, extra=None, drop=(), code=0):
        e = dict(self.env, **(extra or {}))
        for k in drop:
            e.pop(k, None)
        p = subprocess.run([PY, str(RELABEL), *args], env=e, capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, code, f"exit {p.returncode}\nstdout:\n{p.stdout}\nstderr:\n{p.stderr}")
        for tok in self.mock.tokens:
            self.assertNotIn(tok, p.stdout + p.stderr)
        return p

    def test_dry_run_writes_nothing(self):
        p = self.relabel(drop=("LINEAR_BOARD_WRITES",))
        self.assertIn("2 issue(s) to mark, 1 already marked, 1 with no claimant", p.stdout)
        self.assertIn(f"{self.a['identifier']}", p.stdout)
        self.assertIn("claimed:CODEX", p.stdout)
        self.assertIn("dry run: nothing written", p.stdout)
        self.assertEqual({i["id"]: i["labels"] for i in self.mock.issues.values()}, self.labels_before)
        self.assertNotIn("BoardUpdate", self.mock.ops)

    def test_apply_needs_writes_on(self):
        p = self.relabel("--apply", drop=("LINEAR_BOARD_WRITES",), code=7)
        self.assertIn("writes to Linear are off", p.stderr)
        self.assertEqual(self.mock.ops, [])

    def test_apply_is_additive_idempotent_and_resumable(self):
        p = self.relabel("--apply", "--limit", "1")
        self.assertIn("stopped at --limit 1", p.stdout)
        self.assertEqual(self.mock.ops.count("BoardUpdate"), 1)
        p = self.relabel("--apply")
        self.assertIn("1 issue(s) to mark, 2 already marked", p.stdout)
        names = lambda i: self.label_names(i)       # noqa: E731
        self.assertIn("claimed:CODEX", names(self.a))
        self.assertIn("claimed:CLAUDE", names(self.b))
        self.assertEqual([n for n in names(self.c) if n.startswith("claimed:")], [])
        self.assertEqual([n for n in names(self.d) if n.startswith("claimed:")], [])    # closed rows are not touched
        for i in (self.a, self.b, self.c):
            self.assertTrue(set(self.labels_before[i["id"]]) <= set(i["labels"]))        # nothing removed
        n_updates = self.mock.ops.count("BoardUpdate")
        self.relabel("--apply")
        self.assertEqual(self.mock.ops.count("BoardUpdate"), n_updates)
        # the board now sees the claim without reading any footer
        rows = json.loads(self.board("list", "--json", "--mine", "CODEX").stdout)
        self.assertEqual(sorted(r["identifier"] for r in rows), sorted(["OPS-11", self.a["identifier"]]))

    def test_uses_the_fleet_credentials_only(self):
        self.creds.unlink()
        p = self.relabel(code=4)
        self.assertIn("no Fleet credentials", p.stderr)
        self.assertEqual((self.mock.mints, self.mock.ops), (0, []))


def load_cli():
    """The CLI as a module, for unit checks of its pure functions."""
    import importlib.machinery
    import importlib.util
    loader = importlib.machinery.SourceFileLoader("board_cli_under_test", str(CLI))
    spec = importlib.util.spec_from_loader("board_cli_under_test", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


if __name__ == "__main__":
    unittest.main(verbosity=2)
