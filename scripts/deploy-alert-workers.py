"""Release only reviewed alert Workers after successful exact-main CI."""

import glob
import importlib.util
import json
import os
import pathlib
import re
import subprocess
import sys
import tomllib
import urllib.error
import urllib.request

spec = importlib.util.spec_from_file_location("mapped", "scripts/deploy-mapped-worker.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
# scripts/worker-targets.json is the one mapping; a Worker belongs to one pipeline.
TARGETS = {
    name: target
    for name, target in m.TARGETS.items()
    if target["deployer"] == "alert-workers-deploy"
}

# A commit in these needs a qualified review before a target release may ship it.
# They do not by themselves make a target change: that is each target's own "paths".
RELEASE_PATHS = [
    "scripts/deploy-alert-workers.py",
    "scripts/deploy-mapped-worker.py",
    "scripts/worker-targets.json",
    "scripts/infisical-fetch.mjs",
    "scripts/agent-sync-mcp/package.json",
    "scripts/agent-sync-mcp/package-lock.json",
    ".github/workflows/alert-workers-deploy.yml",
    "docs/releases/alert-credentials-qualified.json",
]

# What a credential qualification actually proved: how the workflow delivers the
# credential and reads with it.  The rest of the release source (a lockfile, an
# action bump, a target list) has no bearing on that proof and is covered by the
# per-commit review gate instead, so it must not void a receipt.
CREDENTIAL_WORKFLOW = ".github/workflows/alert-workers-deploy.yml"
CREDENTIAL_STEPS = (
    "Load existing scoped release credential",
    "Qualify existing credential delivery without deployment",
)
CREDENTIAL_PATHS = ["scripts/infisical-fetch.mjs"]
# Bumped when the receipt's meaning changes, so an older receipt cannot arm a release.
RECEIPT_GATE = 2


def git(*args):
    return subprocess.check_output(["git", *args], text=True, timeout=30).strip()


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
    dropped = m.dropped_limits(settings.get("limits"), config.get("limits"))
    if dropped:
        raise RuntimeError("Release would drop live limits: " + ", ".join(dropped))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request_json(url, token, data=None):
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": "Bearer " + token,
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "User-Agent": m.USER_AGENT,
        },
    )
    with urllib.request.build_opener(NoRedirect()).open(req, timeout=30) as response:
        if 300 <= response.status < 400:
            raise RuntimeError("Redirect not allowed")
        return json.load(response)


def github(path):
    body = request_json(
        "https://api.github.com/repos/" + os.environ["GITHUB_REPOSITORY"] + path,
        os.environ["GH_TOKEN"],
    )
    if isinstance(body, dict) and "message" in body:
        raise RuntimeError("GitHub read rejected")
    return body


def cloudflare(path):
    body = request_json(
        "https://api.cloudflare.com/client/v4/accounts/" + m.ACCOUNT + path,
        os.environ["CLOUDFLARE_API_TOKEN"],
    )
    if not body.get("success"):
        raise RuntimeError("Cloudflare read rejected")
    return body["result"]


def current_main():
    return github("/git/ref/heads/main")["object"]["sha"]


def deployed_tag(name):
    return m.deployed_tag(name, cloudflare)


def guard_tripped(name):
    return m.guard_tripped(name, cloudflare)


def outstanding_requests(reviews):
    # COMMENTED does not clear a formal request. Only a later decisive review
    # by that same reviewer supersedes its prior decision; never mutate history.
    decisions = {}
    decisive = [r for r in reviews if r.get("state") in ("APPROVED", "CHANGES_REQUESTED")]
    if any(type(r.get("id")) is not int for r in decisive):
        raise RuntimeError("Missing formal review sequence")
    for review in sorted(decisive, key=lambda r: r["id"]):
        state = review.get("state")
        if state in ("APPROVED", "CHANGES_REQUESTED"):
            user = review.get("user")
            reviewer = user.get("id") if isinstance(user, dict) else None
            if type(reviewer) is not int:
                raise RuntimeError("Missing formal reviewer identity")
            decisions[reviewer] = state
    return any(state == "CHANGES_REQUESTED" for state in decisions.values())


def reviewed(sha):
    # A target-changing commit must be the merge of a real, non-draft PR.
    prs = github("/commits/" + sha + "/pulls")
    if len(prs) >= 30:
        raise RuntimeError("Incomplete commit PR association evidence")
    for pr in prs:
        if not pr.get("merged_at") or pr.get("draft") or pr.get("merge_commit_sha") != sha:
            continue
        number = str(pr["number"])
        reviews = github("/pulls/" + number + "/reviews?per_page=100")
        if len(reviews) >= 100:
            raise RuntimeError("Review pagination needs explicit resolution")
        if outstanding_requests(reviews):
            raise RuntimeError("Changes-requested review needs explicit resolution")
        qualified = [
            r
            for r in reviews
            if isinstance(r.get("user"), dict)
            and r["user"].get("login") == "kody-ai[bot]"
            and r.get("state") in ("APPROVED", "COMMENTED")
        ]
        # COMMENTED is a completed substantive Kody review, not an issue comment.
        # The owner permits reviewed source with findings appropriately resolved.
        # Review must cover the actual composition, not any older review on the PR.
        if not qualified:
            continue
        head = pr["head"]["sha"]
        if not any(r.get("commit_id") == head for r in qualified):
            continue
        # GraphQL includes actual thread resolution, unlike review state.
        owner, repo = os.environ["GITHUB_REPOSITORY"].split("/")
        query = "query($owner:String!,$repo:String!,$number:Int!){repository(owner:$owner,name:$repo){pullRequest(number:$number){reviewThreads(first:100){pageInfo{hasNextPage} nodes{isResolved}}}}}"
        body = request_json(
            "https://api.github.com/graphql",
            os.environ["GH_TOKEN"],
            json.dumps(
                {"query": query, "variables": {"owner": owner, "repo": repo, "number": int(number)}}
            ).encode(),
        )
        if body.get("errors"):
            raise RuntimeError("Incomplete review thread evidence")
        threads = body["data"]["repository"]["pullRequest"]["reviewThreads"]
        if threads["pageInfo"]["hasNextPage"] or any(not t["isResolved"] for t in threads["nodes"]):
            raise RuntimeError("Unresolved or incomplete review thread evidence")
        return
    raise RuntimeError("Target commit lacks qualified merged Kody-reviewed PR")


def probe(target):
    req = urllib.request.Request(target["health"], headers={"User-Agent": m.USER_AGENT})
    try:
        with urllib.request.build_opener(NoRedirect()).open(req, timeout=30) as response:
            raw = response.read(65537)
            if response.status != 200 or len(raw) > 65536:
                raise RuntimeError("Health failed or oversized")
        if target.get("ok") and json.loads(raw).get("ok") is not True:
            raise RuntimeError("Health not ready")
        if target.get("text") and raw.decode().strip() != target["text"]:
            raise RuntimeError("Invalid health response")
    except (urllib.error.URLError, ValueError, UnicodeError) as exc:
        raise RuntimeError("Health validation failed") from exc


def qualify_credentials():
    # Named delivery is required, but values never enter logs or artifacts.
    if not os.environ.get("CLOUDFLARE_API_TOKEN") or not os.environ.get("GH_TOKEN"):
        raise RuntimeError("Existing release credential delivery missing")
    current_main()
    for name, target in TARGETS.items():
        # An uncut-over target is never released, so it cannot block this proof either.
        if name == "pr-conflict-watch" and not cutover_receipt():
            print("Awaiting first verified real Zulip delivery; skipping " + name)
            continue
        # A receipt is accepted as proof the credential reads the live targets, so these
        # reads run for a stopped Worker too.  The watchdog's stop is a secret version with
        # a label, so the tag is still found through it.
        settings = cloudflare("/workers/scripts/" + name + "/settings")
        preflight_config(target, settings)
        tag = deployed_tag(name)
        if not isinstance(tag, str) or not re.fullmatch(r"[0-9a-f]{40}", tag):
            raise RuntimeError("Missing qualified live version baseline")
        # A stopped Worker fails its probe, and only that.  This step runs before the release
        # step on every trigger, so failing here would stop the other target's release as
        # well.  The release step still refuses this one and fails the run.
        if guard_tripped(name):
            print("Stopped by the budget guard; live reads done, probe skipped: " + name)
            continue
        probe(target)
    print("Existing credential delivery and scoped live reads qualified; no deployment")


def workflow_credential_steps(text):
    """The text of the workflow steps that deliver and qualify the credential."""
    lines = text.splitlines()
    blocks = []
    for name in CREDENTIAL_STEPS:
        starts = [i for i, line in enumerate(lines) if line.strip() == "- name: " + name]
        if len(starts) != 1:
            raise RuntimeError("Credential step not found: " + name)
        indent = len(lines[starts[0]]) - len(lines[starts[0]].lstrip())
        block = [lines[starts[0]]]
        for line in lines[starts[0] + 1 :]:
            depth = len(line) - len(line.lstrip())
            if line.strip() and depth <= indent:
                break
            block.append(line)
        blocks.append("\n".join(block).rstrip())
    return blocks


def credential_source_changed(source):
    # A credential-unrelated edit keeps the proof; a change to how the credential is
    # fetched, scoped or qualified voids it until a new nondeployment run is recorded.
    if git("diff", "--name-only", source, "HEAD", "--", *CREDENTIAL_PATHS):
        return True
    qualified = git("show", source + ":" + CREDENTIAL_WORKFLOW)
    current = git("show", "HEAD:" + CREDENTIAL_WORKFLOW)
    return workflow_credential_steps(qualified) != workflow_credential_steps(current)


def credential_receipt():
    path = pathlib.Path("docs/releases/alert-credentials-qualified.json")
    if not path.exists():
        return False
    receipt = json.loads(path.read_text())
    run_id = receipt.get("run_id")
    source = receipt.get("source_sha", "")
    if type(run_id) is not int or run_id <= 0 or not re.fullmatch(r"[0-9a-f]{40}", source):
        raise RuntimeError("Invalid credential qualification receipt")
    if receipt.get("gate") != RECEIPT_GATE:
        raise RuntimeError("Credential qualification receipt predates this gate; record a new one")
    run = github("/actions/runs/" + str(run_id))
    if (
        receipt.get("nondeployment") is not True
        or run.get("event") != "workflow_dispatch"
        or run.get("conclusion") != "success"
        or run.get("status") != "completed"
        or run.get("head_sha") != source
        or run.get("head_branch") != "main"
        or run.get("path") != ".github/workflows/alert-workers-deploy.yml"
    ):
        raise RuntimeError("Unqualified credential delivery run")
    jobs = github("/actions/runs/" + str(run_id) + "/jobs?per_page=100")
    jobs_list = jobs.get("jobs")
    if not isinstance(jobs_list, list) or jobs.get("total_count") != len(jobs_list):
        raise RuntimeError("Incomplete qualification job evidence")
    steps = [step for job in jobs_list for step in job.get("steps", [])]
    qualification = [
        step
        for step in steps
        if step.get("name") == "Qualify existing credential delivery without deployment"
    ]
    release = [
        step
        for step in steps
        if step.get("name") == "Verify reviews and release exact alert targets"
    ]
    if (
        len(qualification) != 1
        or qualification[0].get("conclusion") != "success"
        or len(release) != 1
        or release[0].get("conclusion") != "skipped"
    ):
        raise RuntimeError("Run did not prove nondeployment qualification")
    # Receipt itself arrives in a separately reviewed follow-up; the source that
    # delivers the credential must still match what the receipt qualified.
    if credential_source_changed(source):
        raise RuntimeError("Credential qualification source changed")
    return True


def main():
    if not credential_receipt():
        raise RuntimeError("Missing credential qualification receipt; deployment blocked")
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True, timeout=30).strip()
    if current_main() != sha:
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
    tripped = []
    for name, target in TARGETS.items():
        # An uncut-over target must not block the independent ingress release.
        if name == "pr-conflict-watch" and not cutover_receipt():
            print("Awaiting first verified real Zulip delivery; skipping " + name)
            continue
        # Never reconcile over the budget guard, and let the other target proceed.
        if guard_tripped(name):
            print("Stopped by the budget guard; re-arm by hand: " + name)
            tripped.append(name)
            continue
        live = deployed_tag(name)
        if not isinstance(live, str) or not re.fullmatch(r"[0-9a-f]{40}", live):
            raise RuntimeError("No exact release baseline for " + name)
        subprocess.run(["git", "cat-file", "-e", live + "^{commit}"], check=True, timeout=300)
        # Only this Worker's own files make it a release: a redeploy restarts its
        # Durable Object, so an unrelated merge must leave it alone.
        if not m.worker_changed(live, sha, target):
            probe(target)
            continue
        subprocess.run(["git", "merge-base", "--is-ancestor", live, sha], check=True, timeout=300)
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
            timeout=30,
        ).splitlines()
        if not commits:
            raise RuntimeError("Changed source without reviewable commit evidence")
        if len(commits) > 50:
            raise RuntimeError("Release range exceeds bounded review limit")
        for commit in commits:
            reviewed(commit)
        # Initial cutover is manual and verified first. Automated code releases
        # cannot provision delivery credentials or reintroduce Slack.
        settings = cloudflare("/workers/scripts/" + name + "/settings")
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
                ["npm", "ci", "--ignore-scripts", "--prefix", target["root"]],
                check=True,
                timeout=300,
            )
            subprocess.run(
                [
                    "node",
                    "--test",
                    target["root"] + "body.test.js",
                    target["root"] + "index.test.js",
                ],
                check=True,
                timeout=300,
            )
        else:
            tests = glob.glob(target["root"] + "src/*.test.js")
            if not tests:
                raise RuntimeError("Missing enforced target tests")
            subprocess.run(["node", "--test", *tests], check=True, timeout=300)
        probe(target)
        if current_main() != sha:
            print("Superseded before release")
            return
        if guard_tripped(name):
            print("Stopped by the budget guard; re-arm by hand: " + name)
            tripped.append(name)
            continue
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
            timeout=600,
            env={
                **os.environ,
                "CLOUDFLARE_ACCOUNT_ID": m.ACCOUNT,
                "WRANGLER_SEND_METRICS": "false",
            },
        )
        after = cloudflare("/workers/scripts/" + name + "/settings")
        for key in set(settings) | set(after):
            if key != "annotations" and settings.get(key) != after.get(key):
                raise RuntimeError("Unexpected settings change: " + key)
        if deployed_tag(name) != sha:
            raise RuntimeError("Wrong version receipt")
        probe(target)
        print("Verified exact release: " + name + " " + sha)
    if tripped:
        raise RuntimeError("Release refused, budget guard tripped: " + ", ".join(tripped))


if __name__ == "__main__":
    if sys.argv[1:] == ["--qualify-only"]:
        qualify_credentials()
    elif sys.argv[1:]:
        raise SystemExit("Unknown release arguments")
    else:
        main()
