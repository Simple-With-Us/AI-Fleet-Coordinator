#!/usr/bin/env python3
"""Serialized exact-main code deployment. Activation and credentials are external."""

import datetime
import fnmatch
import json
import os
import pathlib
import re
import subprocess
import sys
import tomllib
import urllib.error
import urllib.request

ACCOUNT = "3a9368057468d0909cafaa85df12d1b7"
TARGETS = json.load(open("scripts/worker-targets.json"))
# Cloudflare answers error 1010 (HTTP 403) to Python's default urllib User-Agent on
# the jays.services zones, so every request names this client explicitly.
USER_AGENT = "AI-Fleet-Coordinator-worker-deploy/1.0"
# The account watchdog marks a Worker it stopped with this message prefix.  Automation
# must never redeploy over a trip, because that would silently undo it.  Re-arm is manual.
GUARD_TRIPPED_PREFIX = "GUARD-TRIPPED"
MAPPED_DEPLOYER = "worker-deploy"


def api(path):
    request = urllib.request.Request(
        "https://api.cloudflare.com/client/v4/accounts/" + ACCOUNT + path,
        headers={
            "Authorization": "Bearer " + os.environ["CLOUDFLARE_API_TOKEN"],
            "User-Agent": USER_AGENT,
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        body = json.load(response)
    if not body.get("success"):
        raise RuntimeError("Cloudflare read rejected")
    return body["result"]


def current_main():
    req = urllib.request.Request(
        "https://api.github.com/repos/" + os.environ["GITHUB_REPOSITORY"] + "/git/ref/heads/main",
        headers={
            "Authorization": "Bearer " + os.environ["GH_TOKEN"],
            "Accept": "application/vnd.github+json",
            "User-Agent": USER_AGENT,
        },
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.load(response)["object"]["sha"]


def live_deployment(name, read=None):
    read = read or api
    deployments = read("/workers/scripts/" + name + "/deployments")["deployments"]
    return deployments[0] if deployments else None


def deployed_tag(name, read=None):
    """The tag of the code that is live, or None when no exact tag can be proven.

    `wrangler secret put` creates an untagged version carrying the same code, so those
    are walked back to the version they copied.  Any other untagged version (a hand or
    API upload) may hold different code, so it stays None and is never walked past.
    """
    read = read or api
    deployment = live_deployment(name, read)
    if not deployment:
        return None
    versions = deployment.get("versions", [])
    if len(versions) != 1 or versions[0].get("percentage") != 100:
        return None
    version = read("/workers/scripts/" + name + "/versions/" + versions[0]["version_id"])
    annotations = version.get("annotations") or {}
    if annotations.get("workers/tag") or annotations.get("workers/triggered_by") != "secret":
        return annotations.get("workers/tag")
    number = version.get("number")
    if type(number) is not int:
        return None
    # Bounded on purpose: a longer run of secret versions reads as unknown, which is
    # the fail-closed answer, and the next tagged deploy clears it.
    listing = read("/workers/scripts/" + name + "/versions?per_page=50")["items"]
    earlier = [v for v in listing if type(v.get("number")) is int and v["number"] < number]
    for candidate in sorted(earlier, key=lambda v: v["number"], reverse=True):
        annotations = candidate.get("annotations") or {}
        if annotations.get("workers/tag") or annotations.get("workers/triggered_by") != "secret":
            return annotations.get("workers/tag")
    return None


def guard_tripped(name, read=None):
    """True when the live deployment or its version carries the watchdog's trip message."""
    read = read or api
    deployment = live_deployment(name, read)
    if not deployment:
        return False
    messages = [(deployment.get("annotations") or {}).get("workers/message")]
    for entry in deployment.get("versions", []):
        version = read("/workers/scripts/" + name + "/versions/" + entry["version_id"])
        messages.append((version.get("annotations") or {}).get("workers/message"))
    return any(isinstance(m, str) and m.startswith(GUARD_TRIPPED_PREFIX) for m in messages)


def path_is_relevant(path, target):
    if any(fnmatch.fnmatchcase(path, glob) for glob in target.get("ignore", [])):
        return False
    return any(fnmatch.fnmatchcase(path, glob) for glob in target["paths"])


def worker_changed(live, sha, target, cwd=None):
    """False only when git proves none of this Worker's own files differ from the live tag.

    Any doubt (no tag, malformed tag, a commit outside this checkout, a tag that is not
    an ancestor, a failed diff) counts as changed.  A deploy-script-only or unrelated
    merge therefore redeploys nothing and leaves the Worker's Durable Objects running.
    """
    if not re.fullmatch(r"[0-9a-f]{40}", live or ""):
        return True

    def git(*args):
        return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)

    if git("cat-file", "-e", live + "^{commit}").returncode != 0:
        return True
    if git("merge-base", "--is-ancestor", live, sha).returncode != 0:
        return True
    diff = git("diff", "--name-only", "--no-renames", "-z", live, sha)
    if diff.returncode != 0:
        return True
    return any(path_is_relevant(path, target) for path in diff.stdout.split("\0") if path)


def config_limits(path):
    """The `limits` a Wrangler config declares, or {} when it declares none."""
    text = pathlib.Path(path).read_text()
    if path.endswith(".toml"):
        return tomllib.loads(text).get("limits") or {}
    # A full JSONC parser is not needed: whole-line comments are the only thing that
    # could fake a match, and a block that will not parse fails closed by raising.
    uncommented = re.sub(r"^\s*//.*$", "", text, flags=re.M)
    found = re.search(r'"limits"\s*:\s*(\{[^{}]*\})', uncommented)
    return json.loads(found.group(1)) if found else {}


def dropped_limits(live, declared):
    """Live CPU or subrequest limits a deploy of this config would silently remove."""
    return sorted(key for key in (live or {}) if key not in (declared or {}))


def seconds_since_last_deploy(name, read=None, now=None):
    deployment = live_deployment(name, read)
    if not deployment:
        return None
    created = datetime.datetime.fromisoformat(deployment["created_on"].replace("Z", "+00:00"))
    now = now or datetime.datetime.now(datetime.timezone.utc)
    return (now - created).total_seconds()


def probe(target):
    url = target.get("health")
    if not url:
        raise RuntimeError("Functional probe not configured for " + target.get("name", "target"))
    headers = {"User-Agent": USER_AGENT}
    if target.get("access"):
        headers["CF-Access-Client-Id"] = os.environ["WORKER_HEALTH_ACCESS_CLIENT_ID"]
        headers["CF-Access-Client-Secret"] = os.environ["WORKER_HEALTH_ACCESS_CLIENT_SECRET"]
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=30) as response:
        if response.status != 200:
            raise RuntimeError("Health probe failed")
        data = json.load(response)
    if target.get("ok") and data.get("ok") is not True:
        raise RuntimeError("Health response not ready")
    if target.get("sections") and not isinstance(data.get("sections"), list):
        raise RuntimeError("Invalid sections response")


def mcp_message(raw, content_type, request_id):
    """One JSON-RPC response from a JSON body or a text/event-stream body."""
    if "text/event-stream" not in (content_type or ""):
        return json.loads(raw)
    for line in raw.decode().splitlines():
        if line.startswith("data:"):
            message = json.loads(line[5:])
            if message.get("id") == request_id:
                return message
    raise RuntimeError("MCP canary: no response in the event stream")


def mcp_post(url, body, bearer=None, session=None, protocol=None):
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "User-Agent": USER_AGENT,
    }
    if bearer:
        headers["Authorization"] = "Bearer " + bearer
    if session:
        headers["Mcp-Session-Id"] = session
    if protocol:
        headers["MCP-Protocol-Version"] = protocol
    request = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.headers, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.headers, error.read()


def mcp_canary(target):
    """Prove the real /mcp path works, which the static /health route cannot.

    Reads only: an unauthenticated initialize must get the OAuth 401, then the canary
    bearer must initialize, list the tools and call the read-only `identities` tool.
    Failure text never includes the bearer or a response body.
    """
    canary = target.get("canary")
    if not canary:
        return
    url = canary["url"]
    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "worker-deploy-canary", "version": "1.0"},
        },
    }
    status, headers, _ = mcp_post(url, initialize)
    if status != 401 or "resource_metadata" not in (headers.get("WWW-Authenticate") or ""):
        raise RuntimeError("MCP canary: unauthenticated initialize did not answer the OAuth 401")
    bearer = os.environ.get(canary["bearer_env"])
    if not bearer:
        raise RuntimeError("MCP canary: " + canary["bearer_env"] + " is not delivered")
    status, headers, raw = mcp_post(url, initialize, bearer)
    if status != 200:
        raise RuntimeError("MCP canary: initialize answered " + str(status))
    result = mcp_message(raw, headers.get("Content-Type"), 1).get("result") or {}
    protocol = result.get("protocolVersion")
    if not protocol:
        raise RuntimeError("MCP canary: initialize returned no protocol version")
    session = headers.get("Mcp-Session-Id")
    notified = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    status, _, _ = mcp_post(url, notified, bearer, session, protocol)
    if status >= 400:
        raise RuntimeError("MCP canary: initialized notification answered " + str(status))
    listing = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
    status, headers, raw = mcp_post(url, listing, bearer, session, protocol)
    if status != 200:
        raise RuntimeError("MCP canary: tools/list answered " + str(status))
    tools = (mcp_message(raw, headers.get("Content-Type"), 2).get("result") or {}).get("tools")
    names = {tool.get("name") for tool in tools or []}
    if canary["tool"] not in names:
        raise RuntimeError("MCP canary: tools/list lacks " + canary["tool"])
    call = {
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tools/call",
        "params": {"name": canary["tool"], "arguments": {}},
    }
    status, headers, raw = mcp_post(url, call, bearer, session, protocol)
    if status != 200:
        raise RuntimeError("MCP canary: tools/call answered " + str(status))
    answer = mcp_message(raw, headers.get("Content-Type"), 3)
    if answer.get("error") or (answer.get("result") or {}).get("isError"):
        raise RuntimeError("MCP canary: " + canary["tool"] + " returned an error")


def main():
    name = sys.argv[1]
    if name not in TARGETS:
        raise SystemExit("Unknown mapped Worker")
    target = TARGETS[name]
    if target.get("deployer") != MAPPED_DEPLOYER:
        raise SystemExit(name + " is released by another pipeline, not this one")
    if not target.get("health"):
        raise SystemExit("No approved CI functional probe; activation blocked")
    gate = target.get("gate_variable")
    if gate and os.environ.get(gate) != "true":
        print(gate + " is not armed; skipping " + name)
        return
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    if current_main() != sha:
        print("Superseded; skipping")
        return
    if guard_tripped(name):
        raise SystemExit(name + " was stopped by the budget guard; re-arm by hand before deploying")
    tag = deployed_tag(name)
    if tag == sha:
        probe(target)
        print("Already deployed and healthy")
        return
    if not worker_changed(tag, sha, target):
        probe(target)
        print("No change to this Worker's files since " + tag[:12] + "; healthy, skipping")
        return
    minimum = target.get("min_deploy_interval_seconds")
    if minimum:
        elapsed = seconds_since_last_deploy(name)
        if elapsed is not None and elapsed < minimum:
            print("Deployed " + str(int(elapsed)) + " s ago; the next scheduled run retries")
            return
    # A deploy this job cannot verify afterward never starts.
    canary = target.get("canary")
    if canary and not os.environ.get(canary["bearer_env"]):
        raise SystemExit(canary["bearer_env"] + " is not delivered; deploy blocked")
    settings = api("/workers/scripts/" + name + "/settings")
    dropped = dropped_limits(settings.get("limits"), config_limits(target["config"]))
    if dropped:
        raise SystemExit("This deploy would drop live limits: " + ", ".join(dropped))
    # Do not start a deployment unless probe credentials are present and old live service is healthy.
    probe(target)
    if current_main() != sha:
        print("Superseded before upload; skipping")
        return
    if guard_tripped(name):
        raise SystemExit(name + " was stopped by the budget guard; re-arm by hand before deploying")
    subprocess.run(
        [*target["command"], "--keep-vars", "--strict", "--tag", sha, "--message", "main " + sha],
        check=True,
        env={**os.environ, "CLOUDFLARE_ACCOUNT_ID": ACCOUNT, "WRANGLER_SEND_METRICS": "false"},
    )
    if deployed_tag(name) != sha:
        raise RuntimeError("Exact version receipt does not match")
    probe(target)
    mcp_canary(target)
    print("Exact Worker version and functional probe verified: " + name + " " + sha)


if __name__ == "__main__":
    main()
