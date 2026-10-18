#!/usr/bin/env python3
"""Pair every fleet GitHub issue with exactly one Linear issue, and report or repair drift (AFC-339).

    python3 scripts/gh_linear_pairing.py report [--json PATH] [--fail-on CLASSES]
    python3 scripts/gh_linear_pairing.py plan --out DIR
    python3 scripts/gh_linear_pairing.py apply DIR/plan-link.jsonl [--limit N] [--per-hour R] [--only-real]

`report` and `plan` only read.  `report` inventories both sides and prints the drift classes;  `plan`
also writes one JSONL file per write phase (link, create, close-github, cancel-linear) for the owner
to approve.  `apply` executes one plan file, re-checking live state before every operation, writing
a ledger beside the plan so a rerun skips what already landed, and pacing itself under --per-hour.
Linear writes go through `board api mutation`, so they need LINEAR_BOARD_WRITES=1 and carry the
seat's tags;  GitHub writes also need PAIRING_GITHUB_WRITES=1.

Linear reads use the board CLI's own client (scripts/linear-board/board) with the seat's credentials,
so set AGENT_SEAT.  GitHub reads use GITHUB_TOKEN.  No credential is printed.

Pair identity is the GitHub issue (canonical repo name, number).  A Linear issue claims a GitHub issue
by a GitHub attachment (authoritative) or by its import footer `<!-- migrated-from: ... gh:O/R#N -->`.
Owners in footers and attachment URLs are hints only:  repos are matched by name, because repo
transfers left both `jaywedgeworth22/...` and `Simple-With-Us/...` spellings for one issue.
"""

from __future__ import annotations

import argparse
import importlib.machinery
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
GITHUB_API = "https://api.github.com"
IMPORT_LABEL = "github-issue"

# Every fleet repo whose issues are paired, with the Linear team its new twins go to.  Clutch's Linear
# key is C and CodeCaps's is CC (Linear, 2026-10-16).  Kodus-Config and homebrew-tap have no team of
# their own;  their imported twins live in AFC.  fleetlink-legacy is gone (404) and is left out.
REPOS: dict[str, str] = {
    "Simple-With-Us/AI-Fleet-Coordinator": "AFC",
    "Simple-With-Us/Autorotate": "AR",
    "Simple-With-Us/BotFleet": "BF",
    "Simple-With-Us/Clutch": "C",
    "Simple-With-Us/CodeCaps": "CC",
    "Simple-With-Us/Congress.Trade": "CT",
    "Simple-With-Us/ContactLogo": "CL",
    "Simple-With-Us/DealDex": "DD",
    "Simple-With-Us/FleetLink": "FL",
    "Simple-With-Us/HogHunter": "HH",
    "Simple-With-Us/Kodus-Config": "AFC",
    "Simple-With-Us/Personal-Site": "PS",
    "Simple-With-Us/Simple-With-Us": "SWU",
    "Simple-With-Us/Socratic-Trade": "ST",
    "Simple-With-Us/Usage-Monitor": "UM",
    "Simple-With-Us/congress-trading-shared": "CTS",
    "Simple-With-Us/homebrew-tap": "AFC",
    "jaywedgeworth22/Fleet-OPS": "OPS",
}

# Twins the import placed outside their repo's team on purpose (research 2026-10-18).  Reported as
# allowed rather than as drift.
TEAM_EXCEPTIONS: dict[tuple[str, int], str] = {
    **{("simple-with-us", n): "AFC" for n in (13, 15, 19, 22, 40)},
    ("ai-fleet-coordinator", 307): "FL",
}

TEAM_BY_NAME = {repo.split("/", 1)[1].lower(): team for repo, team in REPOS.items()}

OPEN_TYPES = {"triage", "backlog", "unstarted", "started"}
CLOSED_TYPES = {"completed", "canceled", "duplicate"}

FOOTER_RE = re.compile(r"<!--\s*migrated-from:(.*?)-->", re.DOTALL)
KEY_RE = re.compile(r"\bgh:([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)#(\d+)")
ISSUE_URL_RE = re.compile(
    r"^https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/issues/(\d+)/?(?:[?#].*)?$"
)

# Drift classes, in report order.  The first three must be zero for the pairing to be 1:1.
CLASSES = {
    "missing_twin": "GitHub issue with no Linear twin (noise excluded)",
    "conflict": "more than one Linear issue claims a GitHub issue, or one Linear issue claims several",
    "gh_closed_linear_open": "GitHub closed while its twin is open in Linear",
    "gh_open_linear_closed": "GitHub open while its twin is closed in Linear (Linear wins)",
    "unlinked": "twin found by import footer, but no GitHub attachment yet",
    "orphan_linear": f"Linear issue labelled {IMPORT_LABEL} whose GitHub issue cannot be found",
    "team_mismatch": "twin in a team other than its repo's",
    "footer_disagrees": "GitHub attachment and import footer name different GitHub issues",
}


# --------------------------------------------------------------------------------- data model


@dataclass
class GhIssue:
    repo: str  # canonical owner/name, after GitHub's redirect for a moved repo
    number: int
    node_id: str
    url: str
    state: str  # open | closed
    state_reason: str | None
    title: str
    author: str
    labels: list[str]
    created_at: str
    archived: bool = False

    @property
    def key(self) -> tuple[str, int]:
        return repo_name(self.repo), self.number


@dataclass
class LinIssue:
    id: str
    identifier: str
    team: str
    state_type: str
    title: str
    created_at: str
    labelled: bool = False
    footer_keys: set[tuple[str, int]] = field(default_factory=set)
    attached_keys: set[tuple[str, int]] = field(default_factory=set)

    @property
    def claims(self) -> set[tuple[str, int]]:
        """GitHub issues this Linear issue stands for:  its attachments when it has any, else its footer."""
        return self.attached_keys or self.footer_keys

    @property
    def is_open(self) -> bool:
        return self.state_type in OPEN_TYPES


def repo_name(full: str) -> str:
    return full.split("/", 1)[-1].lower()


def footer_keys(description: str) -> set[tuple[str, int]]:
    keys = set()
    for footer in FOOTER_RE.findall(description or ""):
        for _owner, repo, number in KEY_RE.findall(footer):
            keys.add((repo.lower(), int(number)))
    return keys


def url_key(url: str) -> tuple[str, int] | None:
    match = ISSUE_URL_RE.match(url or "")
    return (match.group(2).lower(), int(match.group(3))) if match else None


def noise_kind(issue: GhIssue) -> str | None:
    """Automation output that is paired when it already has a twin, but never gets a new one."""
    if repo_name(issue.repo) == "socratic-trade" and issue.title == "Merge shepherd status":
        return "shepherd"
    if "effort-board" in issue.labels and issue.author == "github-actions[bot]":
        return "effort-board"
    if issue.title == "Dependency Dashboard" and issue.author == "renovate[bot]":
        return "renovate"
    return None


# --------------------------------------------------------------------------------- classification


@dataclass
class Pairing:
    pairs: list[tuple[GhIssue, LinIssue]]
    drift: dict[str, list[dict]]
    noise_unpaired: dict[str, list[GhIssue]]
    totals: dict[str, int]


def classify(gh: list[GhIssue], lin: list[LinIssue], known_repos: set[str]) -> Pairing:
    """Join both sides on the GitHub issue key and sort every item into a drift class."""
    by_key = {issue.key: issue for issue in gh}
    claimants: dict[tuple[str, int], list[LinIssue]] = {}
    drift: dict[str, list[dict]] = {name: [] for name in CLASSES}

    for issue in lin:
        claims = issue.claims
        if (
            issue.attached_keys
            and issue.footer_keys
            and not issue.footer_keys <= issue.attached_keys
        ):
            drift["footer_disagrees"].append(
                {
                    "linear": issue.identifier,
                    "attached": sorted(issue.attached_keys),
                    "footer": sorted(issue.footer_keys),
                }
            )
        if len(claims) > 1:
            drift["conflict"].append({"linear": issue.identifier, "claims": sorted(claims)})
            continue
        found = [k for k in claims if k in by_key]
        if not found:
            if issue.labelled:
                why = (
                    "no import footer"
                    if not claims
                    else (
                        "repo not paired"
                        if next(iter(claims))[0] not in known_repos
                        else "GitHub issue not found"
                    )
                )
                drift["orphan_linear"].append(
                    {"linear": issue.identifier, "claims": sorted(claims), "why": why}
                )
            continue
        claimants.setdefault(found[0], []).append(issue)

    pairs = []
    noise_unpaired: dict[str, list[GhIssue]] = {}
    for key, issue in sorted(by_key.items()):
        twins = claimants.get(key, [])
        ref = {"github": issue.url, "title": issue.title[:90]}
        if not twins:
            kind = noise_kind(issue)
            if kind:
                noise_unpaired.setdefault(kind, []).append(issue)
            else:
                drift["missing_twin"].append(dict(ref, state=issue.state))
            continue
        if len(twins) > 1:
            drift["conflict"].append(dict(ref, linear=[t.identifier for t in twins]))
            continue
        twin = twins[0]
        pairs.append((issue, twin))
        ref = dict(
            ref, linear=twin.identifier, linear_state=twin.state_type, noise=noise_kind(issue)
        )
        if key not in twin.attached_keys:
            drift["unlinked"].append(ref)
        if issue.state == "open" and twin.state_type in CLOSED_TYPES:
            drift["gh_open_linear_closed"].append(ref)
        elif issue.state == "closed" and twin.is_open:
            drift["gh_closed_linear_open"].append(ref)
        expected = TEAM_EXCEPTIONS.get(key) or TEAM_BY_NAME.get(key[0])
        if expected and twin.team != expected:
            drift["team_mismatch"].append(dict(ref, team=twin.team, expected=expected))

    totals = {
        "github_issues": len(gh),
        "github_open": sum(i.state == "open" for i in gh),
        "linear_issues": len(lin),
        "pairs": len(pairs),
        "linked": sum(1 for g, t in pairs if g.key in t.attached_keys),
        **{f"noise_unpaired_{k}": len(v) for k, v in noise_unpaired.items()},
    }
    return Pairing(pairs, drift, noise_unpaired, totals)


# --------------------------------------------------------------------------------- plans


def attachment_id(node_id: str) -> str:
    """A stable attachment id per GitHub issue, so a retried link cannot add a second attachment."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"github-issue:{node_id}"))


def build_plans(p: Pairing, gh: list[GhIssue]) -> dict[str, list[dict]]:
    """One ordered list of operations per write phase.  Real work comes before automation noise."""

    def rank(g: GhIssue, t: LinIssue) -> tuple:
        drifted = (g.state == "open") != t.is_open
        return (
            noise_kind(g) is not None,
            g.state != "open" or not t.is_open,
            not drifted,
            g.repo,
            g.number,
        )

    link = [
        {
            "op": "link",
            "linear": t.identifier,
            "linear_id": t.id,
            "github": g.url,
            "repo": g.repo,
            "number": g.number,
            "attachment_id": attachment_id(g.node_id),
            "noise": noise_kind(g),
        }
        for g, t in sorted(p.pairs, key=lambda gt: rank(*gt))
        if g.key not in t.attached_keys
    ]

    by_url = {g.url: g for g in gh}
    create = [
        {
            "op": "create",
            "github": d["github"],
            "repo": by_url[d["github"]].repo,
            "number": by_url[d["github"]].number,
            "team": TEAM_BY_NAME[repo_name(by_url[d["github"]].repo)],
            "attachment_id": attachment_id(by_url[d["github"]].node_id),
        }
        for d in p.drift["missing_twin"]
        if d["github"] in by_url
    ]

    close = []
    shepherd = "merge-shepherd duplicate (ST-4553)"
    for g, t in p.pairs:
        if g.state != "open" or g.archived:
            continue
        if noise_kind(g) == "shepherd":  # closed on both sides:  its twin is in the cancel plan
            close.append(
                {
                    "op": "close_github",
                    "github": g.url,
                    "repo": g.repo,
                    "number": g.number,
                    "state_reason": "not_planned",
                    "because": shepherd,
                    "noise": "shepherd",
                }
            )
        elif t.state_type in CLOSED_TYPES:
            close.append(
                {
                    "op": "close_github",
                    "github": g.url,
                    "repo": g.repo,
                    "number": g.number,
                    "state_reason": "completed" if t.state_type == "completed" else "not_planned",
                    "because": f"{t.identifier} is {t.state_type} in Linear",
                    "noise": noise_kind(g),
                }
            )
    for g in p.noise_unpaired.get("shepherd", []):
        if g.state == "open" and not g.archived:
            close.append(
                {
                    "op": "close_github",
                    "github": g.url,
                    "repo": g.repo,
                    "number": g.number,
                    "state_reason": "not_planned",
                    "because": shepherd,
                    "noise": "shepherd",
                }
            )
    close.sort(key=lambda op: (op["noise"] is not None, op["repo"], op["number"]))

    cancel = [
        {
            "op": "cancel_linear",
            "linear": t.identifier,
            "linear_id": t.id,
            "team": t.team,
            "github": g.url,
            "because": "twin of a merge-shepherd duplicate (ST-4553)",
        }
        for g, t in p.pairs
        if noise_kind(g) == "shepherd" and t.is_open
    ]
    return {"link": link, "create": create, "close-github": close, "cancel-linear": cancel}


# --------------------------------------------------------------------------------- GitHub client


class GitHub:
    def __init__(self, token: str | None = None, max_wait: float = 900.0) -> None:
        self.token = token or os.environ.get("GITHUB_TOKEN") or ""
        self.max_wait = max_wait
        self.requests = 0

    def call(self, method: str, url: str, body: dict | None = None) -> tuple[object, dict]:
        if not url.startswith("https://"):
            url = GITHUB_API + url
        data = json.dumps(body).encode() if body is not None else None
        waited = 0.0
        while True:
            req = urllib.request.Request(
                url,
                data=data,
                method=method,
                headers={
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                    "User-Agent": "fleet-gh-linear-pairing",
                    **({"Authorization": f"Bearer {self.token}"} if self.token else {}),
                },
            )
            self.requests += 1
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    raw = resp.read()
                    return (json.loads(raw) if raw else None), {
                        k.lower(): v for k, v in resp.headers.items()
                    }
            except urllib.error.HTTPError as exc:
                headers = {k.lower(): v for k, v in (exc.headers or {}).items()}
                raw = exc.read() or b""
                limited = exc.code == 429 or (
                    exc.code == 403
                    and (
                        headers.get("x-ratelimit-remaining") == "0"
                        or b"secondary rate limit" in raw.lower()
                    )
                )
                if not limited:
                    message = json.loads(raw or b"{}").get("message", "") if raw[:1] == b"{" else ""
                    raise GitHubError(
                        exc.code,
                        f"{method} {url.replace(GITHUB_API, '')}:  HTTP {exc.code} {message}",
                    ) from None
                wait = float(headers.get("retry-after") or 0) or max(
                    float(headers.get("x-ratelimit-reset") or 0) - time.time(), 60.0
                )
                if waited + wait > self.max_wait:
                    raise GitHubError(
                        exc.code, f"GitHub rate limit outlasted --max-wait ({int(self.max_wait)}s)"
                    ) from None
                time.sleep(wait)
                waited += wait

    def pages(self, path: str):
        url = GITHUB_API + path
        while url:
            data, headers = self.call("GET", url)
            yield from data or []
            match = re.search(r'<([^>]+)>;\s*rel="next"', headers.get("link", ""))
            url = match.group(1) if match else ""


class GitHubError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def fetch_github(
    client: GitHub, repos: dict[str, str], log=print
) -> tuple[list[GhIssue], dict[str, dict]]:
    issues, info = [], {}
    for full in repos:
        try:
            repo, _ = client.call("GET", f"/repos/{full}")
        except GitHubError as exc:
            log(f"skip  {full}:  {exc}")
            continue
        canonical = repo["full_name"]
        info[full] = {
            "canonical": canonical,
            "private": repo["private"],
            "archived": repo["archived"],
        }
        for item in client.pages(f"/repos/{canonical}/issues?state=all&per_page=100"):
            if "pull_request" in item:
                continue
            issues.append(
                GhIssue(
                    repo=canonical,
                    number=item["number"],
                    node_id=item["node_id"],
                    url=item["html_url"],
                    state=item["state"],
                    state_reason=item.get("state_reason"),
                    title=item["title"],
                    author=(item.get("user") or {}).get("login", ""),
                    labels=[label["name"] for label in item.get("labels", [])],
                    created_at=item["created_at"],
                    archived=repo["archived"],
                )
            )
    return issues, info


# --------------------------------------------------------------------------------- Linear client


def board_module():
    """The board CLI as a module, for its Linear client and the seat's credentials."""
    path = HERE / "linear-board" / "board"
    loader = importlib.machinery.SourceFileLoader("fleet_board", str(path))
    spec = importlib.util.spec_from_loader("fleet_board", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def linear_reader():
    board = board_module()
    seat = board.current_seat(required=True)
    auth = board.Auth(
        board.endpoint("LINEAR_BOARD_TOKEN_URL", board.TOKEN_URL), board.resolve_identity(seat)
    )
    return board.Linear(auth, board.endpoint("LINEAR_BOARD_API_URL", board.GRAPHQL_URL))


Q_LABEL = "query($name: String!) { issueLabels(first: 5, filter: { name: { eq: $name } }) { nodes { id } } }"
Q_IMPORTED = """query($label: ID, $first: Int, $after: String) {
  issues(first: $first, after: $after, includeArchived: true, filter: { labels: { some: { id: { eq: $label } } } }) {
    nodes { id identifier title createdAt team { key } state { type } description
            attachments(first: 25) { nodes { url } } }
    pageInfo { hasNextPage endCursor } } }"""
Q_ATTACHED = """query($first: Int, $after: String) {
  attachments(first: $first, after: $after, filter: { url: { contains: "/issues/" } }) {
    nodes { url issue { id identifier title createdAt team { key } state { type } } }
    pageInfo { hasNextPage endCursor } } }"""
Q_TEAMS = (
    "query { teams(first: 100) { nodes { id key states(first: 50) { nodes { id name type } } } } }"
)
Q_FOR_URL = "query($url: String!) { attachmentsForURL(url: $url, first: 10) { nodes { id issue { id identifier } } } }"
Q_STATE = "query($id: String!) { issue(id: $id) { id identifier team { key } state { type } } }"


def fetch_linear(reader) -> list[LinIssue]:
    label = (reader.call(Q_LABEL, {"name": IMPORT_LABEL})["issueLabels"]["nodes"] or [{}])[0].get(
        "id"
    )
    found: dict[str, LinIssue] = {}
    for node in reader.paginate(Q_IMPORTED, {"label": label}, ("issues",), 100):
        issue = _lin(node, labelled=True)
        issue.footer_keys = footer_keys(node.get("description") or "")
        issue.attached_keys = {k for a in node["attachments"]["nodes"] if (k := url_key(a["url"]))}
        found[issue.id] = issue
    for node in reader.paginate(Q_ATTACHED, {}, ("attachments",), 250):
        key, linked = url_key(node["url"]), node.get("issue")
        if not key or not linked:
            continue
        found.setdefault(linked["id"], _lin(linked, labelled=False)).attached_keys.add(key)
    return list(found.values())


def _lin(node: dict, labelled: bool) -> LinIssue:
    return LinIssue(
        id=node["id"],
        identifier=node["identifier"],
        team=node["team"]["key"],
        state_type=node["state"]["type"],
        title=node["title"],
        created_at=node["createdAt"],
        labelled=labelled,
    )


def board_mutation(document: str, variables: dict, key: str) -> dict:
    """One Linear write through the board CLI:  its write gate, seat tags and idempotency key."""
    proc = subprocess.run(
        [
            "board",
            "api",
            "mutation",
            document,
            "--vars",
            json.dumps(variables),
            "--idempotency-key",
            key,
            "--json",
            "--max-chars",
            "64000",
        ],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            (proc.stderr or proc.stdout).strip()[-400:] or f"board exited {proc.returncode}"
        )
    return json.loads(proc.stdout)


M_LINK = """mutation($issueId: String!, $url: String!, $id: String) {
  attachmentLinkGitHubIssue(issueId: $issueId, url: $url, id: $id) { success attachment { id url } } }"""
M_CREATE = """mutation($input: IssueCreateInput!) { issueCreate(input: $input) { success issue { id identifier } } }"""
M_STATE = """mutation($id: String!, $stateId: String!) { issueUpdate(id: $id, input: { stateId: $stateId }) { success } }"""


# --------------------------------------------------------------------------------- apply


class Applier:
    """Executes one plan, op by op, re-checking live state first.  Every outcome goes to the ledger."""

    def __init__(
        self,
        plan: Path,
        reader,
        github: GitHub,
        mutate=board_mutation,
        per_hour: float = 400.0,
        sleep=time.sleep,
        log=print,
    ) -> None:
        self.plan, self.reader, self.github, self.mutate = plan, reader, github, mutate
        self.ledger = plan.with_name(plan.stem + ".ledger.jsonl")
        self.gap = 3600.0 / per_hour if per_hour > 0 else 0.0
        self.sleep, self.log = sleep, log
        self._teams: dict[str, dict] | None = None

    def done(self) -> set[str]:
        if not self.ledger.exists():
            return set()
        rows = [json.loads(line) for line in self.ledger.read_text().splitlines() if line.strip()]
        return {row["key"] for row in rows if row["result"] in ("ok", "already")}

    def record(self, op: dict, result: str, detail: str = "") -> None:
        with self.ledger.open("a") as fh:
            fh.write(
                json.dumps(
                    {
                        "key": op_key(op),
                        "op": op["op"],
                        "result": result,
                        "detail": detail,
                        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    }
                )
                + "\n"
            )
        self.log(
            f"{result:8} {op['op']:13} {op.get('linear') or ''} {op.get('github', '')} {detail}".rstrip()
        )

    def run(
        self, ops: list[dict], limit: int | None = None, only_real: bool = False
    ) -> dict[str, int]:
        if (
            any(op["op"] in ("close_github",) for op in ops)
            and os.environ.get("PAIRING_GITHUB_WRITES") != "1"
        ):
            raise SystemExit(
                "GitHub writes are off:  set PAIRING_GITHUB_WRITES=1 to apply this plan"
            )
        skip = self.done()
        counts: dict[str, int] = {}
        wrote = 0
        for op in ops:
            if limit is not None and wrote >= limit:
                break
            if op_key(op) in skip or (only_real and op.get("noise")):
                continue
            try:
                result, detail = getattr(self, "do_" + op["op"])(op)
            except (GitHubError, RuntimeError, KeyError) as exc:
                result, detail = "error", str(exc)[:300]
            self.record(op, result, detail)
            counts[result] = counts.get(result, 0) + 1
            if result == "ok":
                wrote += 1
                self.sleep(self.gap)
        return counts

    def attached_to(self, url: str) -> list[str]:
        nodes = self.reader.call(Q_FOR_URL, {"url": url})["attachmentsForURL"]["nodes"]
        return [n["issue"]["identifier"] for n in nodes if n.get("issue")]

    def do_link(self, op: dict) -> tuple[str, str]:
        holders = self.attached_to(op["github"])
        if op["linear"] in holders:
            return "already", ""
        if holders:
            return "conflict", "already attached to " + ", ".join(holders)
        out = self.mutate(
            M_LINK,
            {"issueId": op["linear_id"], "url": op["github"], "id": op["attachment_id"]},
            "pairing-link-" + op["attachment_id"],
        )
        return (
            ("ok", "")
            if out.get("attachmentLinkGitHubIssue", {}).get("success")
            else ("error", json.dumps(out)[:200])
        )

    def teams(self) -> dict[str, dict]:
        if self._teams is None:
            self._teams = {t["key"]: t for t in self.reader.call(Q_TEAMS)["teams"]["nodes"]}
        return self._teams

    def state_id(self, team: str, kind: str, preferred: str) -> str:
        states = [s for s in self.teams()[team]["states"]["nodes"] if s["type"] == kind]
        named = [s for s in states if s["name"] == preferred]
        return (named or states)[0]["id"]

    def do_create(self, op: dict) -> tuple[str, str]:
        holders = self.attached_to(op["github"])
        if holders:
            return "already", "attached to " + ", ".join(holders)
        issue, _ = self.github.call("GET", f"/repos/{op['repo']}/issues/{op['number']}")
        label = (self.reader.call(Q_LABEL, {"name": IMPORT_LABEL})["issueLabels"]["nodes"] or [{}])[
            0
        ].get("id")
        closed = issue["state"] == "closed"
        team = self.teams()[op["team"]]
        description = twin_description(op["repo"], issue)
        out = self.mutate(
            M_CREATE,
            {
                "input": {
                    "teamId": team["id"],
                    "title": issue["title"],
                    "description": description,
                    "labelIds": [label] if label else [],
                    "stateId": self.state_id(
                        op["team"],
                        "completed" if closed else "unstarted",
                        "Done" if closed else "Todo",
                    ),
                }
            },
            "pairing-create-" + op["attachment_id"],
        )
        created = (out.get("issueCreate") or {}).get("issue") or {}
        if not created.get("id"):
            return "error", json.dumps(out)[:200]
        linked = self.mutate(
            M_LINK,
            {"issueId": created["id"], "url": op["github"], "id": op["attachment_id"]},
            "pairing-link-" + op["attachment_id"],
        )
        ok = linked.get("attachmentLinkGitHubIssue", {}).get("success")
        return ("ok" if ok else "error"), created.get("identifier", "") + (
            "" if ok else " created but not linked"
        )

    def do_close_github(self, op: dict) -> tuple[str, str]:
        issue, _ = self.github.call("GET", f"/repos/{op['repo']}/issues/{op['number']}")
        if issue["state"] == "closed":
            return "already", ""
        self.github.call(
            "PATCH",
            f"/repos/{op['repo']}/issues/{op['number']}",
            {"state": "closed", "state_reason": op["state_reason"]},
        )
        return "ok", op["state_reason"]

    def do_cancel_linear(self, op: dict) -> tuple[str, str]:
        issue = self.reader.call(Q_STATE, {"id": op["linear_id"]})["issue"]
        if issue["state"]["type"] in CLOSED_TYPES:
            return "already", issue["state"]["type"]
        out = self.mutate(
            M_STATE,
            {
                "id": op["linear_id"],
                "stateId": self.state_id(issue["team"]["key"], "canceled", "Canceled"),
            },
            "pairing-cancel-" + op["linear_id"],
        )
        return (
            ("ok", "")
            if out.get("issueUpdate", {}).get("success")
            else ("error", json.dumps(out)[:200])
        )


def op_key(op: dict) -> str:
    return f"{op['op']}:{op.get('linear') or ''}:{op['github']}"


def twin_description(repo: str, issue: dict) -> str:
    body = (issue.get("body") or "").strip()
    if len(body) > 60000:
        body = body[:60000] + "\n\n…(truncated;  the full text is on GitHub)"
    opened = issue["created_at"][:10]
    author = (issue.get("user") or {}).get("login", "")
    return (
        f"{body}\n\n---\n\n**Provenance (paired from GitHub by gh_linear_pairing, AFC-339)**\n\n"
        f"* GitHub:  [{repo}#{issue['number']}](<{issue['html_url']}>), opened by {author} on {opened}\n\n"
        f"<!-- migrated-from: gh:{repo}#{issue['number']} -->"
    )


# --------------------------------------------------------------------------------- CLI


def inventory(log=print) -> tuple[list[GhIssue], list[LinIssue], dict[str, dict]]:
    github = GitHub()
    if not github.token:
        raise SystemExit("set GITHUB_TOKEN to read GitHub")
    gh, info = fetch_github(github, REPOS, log=log)
    lin = fetch_linear(linear_reader())
    log(f"read {len(gh)} GitHub issues ({github.requests} requests) and {len(lin)} Linear issues")
    return gh, lin, info


def print_report(p: Pairing) -> None:
    for name, value in p.totals.items():
        print(f"{name:32} {value}")
    print()
    for name, desc in CLASSES.items():
        print(f"{len(p.drift[name]):6}  {name:24} {desc}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    rep = sub.add_parser("report", help="read-only:  inventory and drift classes")
    rep.add_argument("--json", help="write the full report here")
    rep.add_argument(
        "--fail-on", default="", help="comma-separated classes that make the exit code 1"
    )
    pln = sub.add_parser("plan", help="read-only:  report plus one plan file per write phase")
    pln.add_argument("--out", required=True)
    app = sub.add_parser("apply", help="execute one plan file (writes)")
    app.add_argument("plan")
    app.add_argument("--limit", type=int, help="stop after this many successful writes")
    app.add_argument(
        "--per-hour", type=float, default=400.0, help="pace writes (default 400 an hour)"
    )
    app.add_argument("--only-real", action="store_true", help="skip operations on automation noise")
    args = parser.parse_args(argv)

    if args.cmd == "apply":
        path = Path(args.plan)
        ops = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        counts = Applier(path, linear_reader(), GitHub(), per_hour=args.per_hour).run(
            ops, args.limit, args.only_real
        )
        print("  ".join(f"{k} {v}" for k, v in sorted(counts.items())) or "nothing to do")
        return 1 if counts.get("error") else 0

    gh, lin, info = inventory()
    pairing = classify(gh, lin, {repo_name(r) for r in REPOS})
    print_report(pairing)
    report = {
        "totals": pairing.totals,
        "drift": pairing.drift,
        "repos": info,
        "noise_unpaired": {k: [i.url for i in v] for k, v in pairing.noise_unpaired.items()},
    }
    if args.cmd == "report":
        if args.json:
            Path(args.json).write_text(json.dumps(report, indent=1))
        failing = [c for c in filter(None, args.fail_on.split(",")) if pairing.drift.get(c)]
        return 1 if failing else 0

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.json").write_text(json.dumps(report, indent=1))
    for phase, ops in build_plans(pairing, gh).items():
        (out / f"plan-{phase}.jsonl").write_text("".join(json.dumps(op) + "\n" for op in ops))
        print(f"plan-{phase}.jsonl  {len(ops)} operations")
    return 0


if __name__ == "__main__":
    sys.exit(main())
