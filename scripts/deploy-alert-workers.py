#!/usr/bin/env python3
"""Release only reviewed alert Workers after successful exact-main CI."""

import importlib.util
import json
import os
import pathlib
import re
import subprocess
import tomllib
import urllib.request

spec = importlib.util.spec_from_file_location("mapped", "scripts/deploy-mapped-worker.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
TARGETS = {
    "alert-ingress": {
        "root": "workers/alert-ingress/",
        "config": "workers/alert-ingress/wrangler.toml",
        "health": "https://alert-ingress.jays-services.workers.dev/health",
        "ok": True,
    },
    "pr-conflict-watch": {
        "root": "workers/pr-conflict-watch/zulip-cutover/",
        "config": "workers/pr-conflict-watch/zulip-cutover/wrangler.toml",
        "health": "https://pr-conflict-watch.jays-services.workers.dev/health",
        "text": "ok",
    },
}

RELEASE_PATHS = [
    "scripts/deploy-alert-workers.py",
    "scripts/deploy-mapped-worker.py",
    "scripts/worker-targets.json",
    "scripts/infisical-fetch.mjs",
    "scripts/agent-sync-mcp/package.json",
    "scripts/agent-sync-mcp/package-lock.json",
    ".github/workflows/alert-workers-deploy.yml",
]


def git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()


def cutover_receipt():
    path = pathlib.Path("docs/releases/pr-conflict-zulip-verified.json")
    if not path.exists():
        return False
    receipt = json.loads(path.read_text())
    if receipt.get("sender_id") != 1212033 or receipt.get("channel_id") != 642233:
        raise RuntimeError("Wrong cutover sender or audience")
    if not re.fullmatch(r"[0-9a-f]{40}", receipt.get("source_sha", "")) or not receipt.get(
        "version_id"
    ):
        raise RuntimeError("Missing exact cutover release receipt")
    if (
        not isinstance(receipt.get("message_id"), int)
        or receipt["message_id"] <= 0
        or not receipt.get("message_url", "").startswith("https://simplewithus.zulipchat.com/")
    ):
        raise RuntimeError("Missing verified first real Zulip delivery")
    if receipt.get("real_conflict_event") is not True:
        raise RuntimeError("Synthetic alerts cannot qualify the cutover")
    return True


def preflight_config(target, settings):
    # Existing runtime settings are not silently changed by a code-only release.
    config = tomllib.loads(pathlib.Path(target["config"]).read_text())
    for key in ("compatibility_date", "compatibility_flags", "logpush"):
        if key in config and config[key] != settings.get(key):
            raise RuntimeError("Runtime setting change requires manual reconciliation: " + key)
    live = {b["name"]: b for b in settings.get("bindings", [])}
    for binding in config.get("kv_namespaces", []):
        if live.get(binding["binding"], {}).get("namespace_id") != binding["id"]:
            raise RuntimeError("KV binding change is not an automatic code release")
    for binding in config.get("durable_objects", {}).get("bindings", []):
        found = live.get(binding["name"], {})
        if found.get("class_name") != binding["class_name"] or binding.get("script_name"):
            raise RuntimeError("Durable Object binding change requires manual reconciliation")
    if config.get("routes") or config.get("route"):
        raise RuntimeError("Route configuration is outside this code-only release")


def github(path):
    req = urllib.request.Request(
        "https://api.github.com/repos/" + os.environ["GITHUB_REPOSITORY"] + path,
        headers={
            "Authorization": "Bearer " + os.environ["GH_TOKEN"],
            "Accept": "application/vnd.github+json",
            "User-Agent": m.USER_AGENT,
        },
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.load(response)


def reviewed(sha):
    # A target-changing commit must be the merge of a real, non-draft PR.
    prs = github("/commits/" + sha + "/pulls")
    for pr in prs:
        if not pr.get("merged_at") or pr.get("draft") or pr.get("merge_commit_sha") != sha:
            continue
        number = str(pr["number"])
        reviews = github("/pulls/" + number + "/reviews?per_page=100")
        if len(reviews) >= 100:
            raise RuntimeError("Review pagination needs explicit resolution")
        qualified = [
            r
            for r in reviews
            if r.get("user", {}).get("login") == "kody-ai[bot]"
            and r.get("state") in ("APPROVED", "COMMENTED")
        ]
        # Review must cover the actual composition, not any older review on the PR.
        if not qualified:
            continue
        head = pr["head"]["sha"]
        if not any(
            r.get("commit_id") == head
            or not git(
                "diff",
                "--name-only",
                r["commit_id"],
                head,
                "--",
                *[t["root"] for t in TARGETS.values()],
                *RELEASE_PATHS,
            )
            for r in qualified
        ):
            continue
        # GraphQL includes actual thread resolution, unlike review state.
        owner, repo = os.environ["GITHUB_REPOSITORY"].split("/")
        query = "query($owner:String!,$repo:String!,$number:Int!){repository(owner:$owner,name:$repo){pullRequest(number:$number){reviewThreads(first:100){pageInfo{hasNextPage} nodes{isResolved}}}}}"
        req = urllib.request.Request(
            "https://api.github.com/graphql",
            data=json.dumps(
                {"query": query, "variables": {"owner": owner, "repo": repo, "number": int(number)}}
            ).encode(),
            headers={
                "Authorization": "Bearer " + os.environ["GH_TOKEN"],
                "Content-Type": "application/json",
                "User-Agent": m.USER_AGENT,
            },
        )
        with urllib.request.urlopen(req, timeout=30) as response:
            body = json.load(response)
        threads = body["data"]["repository"]["pullRequest"]["reviewThreads"]
        if threads["pageInfo"]["hasNextPage"] or any(not t["isResolved"] for t in threads["nodes"]):
            raise RuntimeError("Unresolved or incomplete review thread evidence")
        return
    raise RuntimeError("Target commit lacks qualified merged Kody-reviewed PR")


def probe(target):
    req = urllib.request.Request(target["health"], headers={"User-Agent": m.USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as response:
        raw = response.read(65536)
        if response.status != 200:
            raise RuntimeError("Health failed")
    if target.get("ok") and json.loads(raw).get("ok") is not True:
        raise RuntimeError("Health not ready")
    if target.get("text") and raw.decode().strip() != target["text"]:
        raise RuntimeError("Invalid health response")


def main():
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    if m.current_main() != sha:
        print("Superseded")
        return
    runs = github(
        "/actions/workflows/ci.yml/runs?branch=main&event=push&head_sha=" + sha + "&per_page=100"
    )["workflow_runs"]
    if not any(
        r["head_sha"] == sha and r["status"] == "completed" and r["conclusion"] == "success"
        for r in runs
    ):
        raise RuntimeError("Exact-main CI has not passed")
    for name, target in TARGETS.items():
        # An uncut-over target must not block the independent ingress release.
        if name == "pr-conflict-watch" and not cutover_receipt():
            print("Awaiting first verified real Zulip delivery; skipping " + name)
            continue
        live = m.deployed_tag(name)
        if not live:
            raise RuntimeError("No exact release baseline for " + name)
        subprocess.run(["git", "cat-file", "-e", live + "^{commit}"], check=True)
        changed = subprocess.check_output(
            [
                "git",
                "diff",
                "--name-only",
                live,
                sha,
                "--",
                target["root"],
                *RELEASE_PATHS,
                "docs/releases/pr-conflict-zulip-verified.json",
            ],
            text=True,
        ).splitlines()
        if not changed:
            probe(target)
            continue
        subprocess.run(["git", "merge-base", "--is-ancestor", live, sha], check=True)
        if git("diff", "--name-only", live, sha, "--", target["config"]):
            raise RuntimeError("Deployment configuration change requires manual reconciliation")
        commits = subprocess.check_output(
            [
                "git",
                "rev-list",
                "--first-parent",
                live + ".." + sha,
                "--",
                target["root"],
                *RELEASE_PATHS,
                "docs/releases/pr-conflict-zulip-verified.json",
            ],
            text=True,
        ).splitlines()
        if not commits:
            raise RuntimeError("Changed source without reviewable commit evidence")
        for commit in commits:
            reviewed(commit)
        # Initial cutover is manual and verified first. Automated code releases
        # cannot provision delivery credentials or reintroduce Slack.
        settings = m.api("/workers/scripts/" + name + "/settings")
        preflight_config(target, settings)
        if name == "pr-conflict-watch":
            names = {b["name"] for b in settings.get("bindings", [])}
            required = {
                "GITHUB_WEBHOOK_SECRET",
                "GITHUB_TOKEN",
                "PR_STATE",
                "ZULIP_SITE",
                "ZULIP_BOT_EMAIL",
                "ZULIP_BOT_API_KEY",
            }
            if not required <= names or "SLACK_BOT_TOKEN" in names:
                raise RuntimeError("Verified Zulip cutover prerequisite not met")
            subprocess.run(
                ["npm", "ci", "--ignore-scripts", "--prefix", target["root"]], check=True
            )
            subprocess.run(
                [
                    "node",
                    "--test",
                    target["root"] + "body.test.js",
                    target["root"] + "index.test.js",
                ],
                check=True,
            )
        else:
            subprocess.run(
                ["node", "--test", *__import__("glob").glob(target["root"] + "src/*.test.js")],
                check=True,
            )
        probe(target)
        if m.current_main() != sha:
            print("Superseded before release")
            return
        subprocess.run(
            [
                "scripts/agent-sync-mcp/node_modules/.bin/wrangler",
                "deploy",
                "--config",
                target["config"],
                "--keep-vars",
                "--strict",
                "--tag",
                sha,
                "--message",
                "reviewed main " + sha,
            ],
            check=True,
            env={
                **os.environ,
                "CLOUDFLARE_ACCOUNT_ID": m.ACCOUNT,
                "WRANGLER_SEND_METRICS": "false",
            },
        )
        after = m.api("/workers/scripts/" + name + "/settings")
        for key in set(settings) | set(after):
            if key != "annotations" and settings.get(key) != after.get(key):
                raise RuntimeError("Unexpected settings change: " + key)
        if m.deployed_tag(name) != sha:
            raise RuntimeError("Wrong version receipt")
        probe(target)
        print("Verified exact release: " + name + " " + sha)


if __name__ == "__main__":
    main()
