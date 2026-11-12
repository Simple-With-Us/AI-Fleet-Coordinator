#!/usr/bin/env python3
"""Surface Grok Bot and Shellular Cursor chats onto desktop + iOS.

Cloud Agents already sync to the desktop Agents Window, cursor.com/agents,
and the iOS Cursor inbox.  Local `cursor-agent` / ACP sessions do not.
This module talks to the Cloud Agents API and installs the Shellular
override that turns new Shellular Cursor chats into Cloud Agents.

Never print API keys or secret-file contents.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

API_ROOT = "https://api.cursor.com"
AGENTS_INBOX = "https://cursor.com/agents"
STATE_DIR = Path.home() / ".cursor" / "fleet-chat-surfaces"
LIVE_DIR = Path.home() / "apps" / "cursor-chat-surfaces"
DSH_RUNTIME = Path.home() / "apps" / "dsh-runtime"
SHELLULAR_AGENTS = Path.home() / ".shellular" / "agents.json"
ACP_SESSIONS = Path.home() / ".cursor" / "acp-sessions"
API_KEY_NAMES = ("CURSOR_API_KEY", "CURSOR_SYNC_API_KEY")
FLEET_SECRETS_BIN = (
    Path.home() / ".local" / "bin" / "fleet-secrets"
)  # OPS-818:  Fleet-OPS in Infisical
REPO_SCRIPTS = Path(__file__).resolve().parent
LIVE_FILES = (
    "cursor_chat_surfaces.py",
    "cursor_acp_cloud_bridge.py",
    "cursor-chat-surfaces",
    "cursor-machine-worker.sh",
)

JsonDict = dict[str, Any]


def state_path(*parts: str) -> Path:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_DIR.chmod(0o700)
    return STATE_DIR.joinpath(*parts)


def _env_api_key() -> str | None:
    for name in API_KEY_NAMES:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return None


def _fleet_secrets_key() -> str | None:
    """CURSOR_SYNC_API_KEY (or CURSOR_API_KEY) from `fleet-secrets get`, or None.  Never logged."""
    binary = shutil.which("fleet-secrets") or (
        str(FLEET_SECRETS_BIN) if FLEET_SECRETS_BIN.is_file() else None
    )
    if not binary:
        return None
    for name in API_KEY_NAMES:
        try:
            proc = subprocess.run([binary, "get", name], capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.SubprocessError):
            return None
        value = proc.stdout.strip() if proc.returncode == 0 else ""
        if value:
            return value
    return None


def load_api_key() -> str | None:
    """CURSOR_API_KEY or CURSOR_SYNC_API_KEY from the environment, else Infisical Fleet-OPS (`fleet-secrets get`)."""
    env = _env_api_key()
    if env:
        return env
    return _fleet_secrets_key()


def github_https_url(remote: str) -> str | None:
    remote = remote.strip()
    if not remote:
        return None
    if remote.startswith("git@github.com:"):
        path = remote[len("git@github.com:") :]
        path = path.removesuffix(".git")
        return f"https://github.com/{path}"
    if remote.startswith("ssh://git@github.com/"):
        path = remote[len("ssh://git@github.com/") :]
        path = path.removesuffix(".git")
        return f"https://github.com/{path}"
    if remote.startswith("https://github.com/"):
        url = remote
        url = url.removesuffix(".git")
        return url
    return None


def git_output(args: list[str], cwd: str) -> str | None:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
            timeout=8,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return (proc.stdout or "").strip() or None


def repo_from_cwd(cwd: str | None) -> JsonDict | None:
    if not cwd:
        return None
    root = git_output(["rev-parse", "--show-toplevel"], cwd)
    if not root:
        return None
    remote = git_output(["remote", "get-url", "origin"], root)
    if not remote:
        return None
    url = github_https_url(remote)
    if not url:
        return None
    ref = git_output(["rev-parse", "--abbrev-ref", "HEAD"], root) or "main"
    if ref == "HEAD":
        ref = git_output(["rev-parse", "--short", "HEAD"], root) or "main"
    return {"url": url, "startingRef": ref}


def extract_prompt_text(prompt: Any) -> str:
    if isinstance(prompt, str):
        return prompt.strip()
    if not isinstance(prompt, list):
        return ""
    chunks: list[str] = []
    for block in prompt:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text" and isinstance(block.get("text"), str):
            chunks.append(block["text"])
        elif kind == "resource":
            resource = block.get("resource")
            if isinstance(resource, dict) and isinstance(resource.get("text"), str):
                chunks.append(resource["text"])
            elif isinstance(resource, dict) and isinstance(resource.get("uri"), str):
                chunks.append(resource["uri"])
    return "\n".join(chunk.strip() for chunk in chunks if chunk.strip()).strip()


def agent_web_url(agent_id: str) -> str:
    if agent_id.startswith("http"):
        return agent_id
    return f"{AGENTS_INBOX}/{agent_id}"


def _basic_auth_header(api_key: str) -> str:
    token = base64.b64encode(f"{api_key}:".encode()).decode("ascii")
    return f"Basic {token}"


def api_request(
    method: str,
    path: str,
    api_key: str,
    body: JsonDict | None = None,
    timeout: int = 60,
) -> JsonDict:
    url = API_ROOT + path
    data = None
    headers = {
        "Authorization": _basic_auth_header(api_key),
        "Accept": "application/json",
    }
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as err:
        detail = err.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"Cursor API {err.code} on {method} {path}: {detail}") from err
    except urllib.error.URLError as err:
        raise RuntimeError(f"Cursor API unreachable: {err.reason}") from err
    if not raw:
        return {}
    parsed = json.loads(raw)
    if isinstance(parsed, dict):
        return parsed
    return {"data": parsed}


def list_cloud_agents(api_key: str, limit: int = 20) -> list[JsonDict]:
    query = urllib.parse.urlencode({"limit": str(limit)})
    payload = api_request("GET", f"/v1/agents?{query}", api_key)
    agents = payload.get("agents") or payload.get("data") or []
    if isinstance(agents, list):
        return [a for a in agents if isinstance(a, dict)]
    return []


def create_cloud_agent(
    api_key: str,
    prompt_text: str,
    cwd: str | None,
    name: str | None = None,
    on_mac: bool = False,
    machine_name: str = "jay-mac",
) -> JsonDict:
    body: JsonDict = {"prompt": {"text": prompt_text}}
    if name:
        body["name"] = name[:100]
    repo = repo_from_cwd(cwd)
    if repo:
        body["repos"] = [repo]
    if on_mac:
        body["env"] = {"type": "machine", "name": machine_name}
    return api_request("POST", "/v1/agents", api_key, body)


def follow_up_cloud_agent(api_key: str, agent_id: str, prompt_text: str) -> JsonDict:
    body = {"prompt": {"text": prompt_text}}
    return api_request("POST", f"/v1/agents/{agent_id}/runs", api_key, body)


def get_cloud_agent(api_key: str, agent_id: str) -> JsonDict:
    return api_request("GET", f"/v1/agents/{agent_id}", api_key)


def get_conversation(api_key: str, agent_id: str) -> JsonDict | None:
    try:
        return api_request("GET", f"/v1/agents/{agent_id}/conversation", api_key)
    except RuntimeError:
        return None


def open_url(url: str) -> None:
    subprocess.run(["open", url], check=False, timeout=10)


def worker_running() -> bool:
    try:
        proc = subprocess.run(
            ["pgrep", "-f", "agent worker"],
            check=False,
            capture_output=True,
            timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def cli_login_line() -> str:
    agent = shutil.which("agent") or str(Path.home() / ".local" / "bin" / "agent")
    try:
        proc = subprocess.run(
            [agent, "status"],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "CLI status unavailable"
    line = (proc.stdout or proc.stderr or "").strip().splitlines()
    return line[0] if line else "CLI status empty"


def load_json(path: Path, default: Any) -> Any:
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default


def save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")
        os.replace(tmp, path)
        path.chmod(0o600)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def session_map() -> JsonDict:
    return load_json(state_path("sessions.json"), {})


def put_session_mapping(session_id: str, agent: JsonDict) -> None:
    data = session_map()
    data[session_id] = {
        "agentId": agent.get("id") or (agent.get("agent") or {}).get("id"),
        "url": agent.get("url") or (agent.get("agent") or {}).get("url"),
        "name": agent.get("name") or (agent.get("agent") or {}).get("name"),
    }
    save_json(state_path("sessions.json"), data)


def unwrap_created_agent(payload: JsonDict) -> JsonDict:
    if isinstance(payload.get("agent"), dict):
        agent = dict(payload["agent"])
        if isinstance(payload.get("run"), dict) and payload["run"].get("id"):
            agent["latestRunId"] = payload["run"]["id"]
        return agent
    return payload


def seen_ids() -> set[str]:
    raw = load_json(state_path("seen-agents.json"), [])
    if isinstance(raw, list):
        return {str(item) for item in raw}
    return set()


def save_seen_ids(ids: set[str]) -> None:
    save_json(state_path("seen-agents.json"), sorted(ids))


def list_acp_sessions() -> list[JsonDict]:
    rows: list[JsonDict] = []
    if not ACP_SESSIONS.is_dir():
        return rows
    for meta in ACP_SESSIONS.glob("*/meta.json"):
        payload = load_json(meta, {})
        if not isinstance(payload, dict):
            continue
        rows.append(
            {
                "id": meta.parent.name,
                "cwd": payload.get("cwd"),
                "title": payload.get("title"),
                "updatedMs": int(meta.stat().st_mtime * 1000),
            }
        )
    rows.sort(key=lambda row: int(row.get("updatedMs") or 0), reverse=True)
    return rows


def shellular_cursor_entries() -> list[JsonDict]:
    script = str(LIVE_DIR / "cursor_acp_cloud_bridge.py")
    return [
        {
            "id": "cursor",
            "name": "Cursor",
            "title": "Cursor Cloud",
            "command": sys.executable,
            "args": [script],
        },
        {
            "id": "cursor-local",
            "name": "Cursor local",
            "title": "Cursor CLI (local only)",
            "command": "cursor-agent",
            "args": ["acp"],
        },
        {
            "id": "deepseek",
            "name": "DeepSeek",
            "title": "DeepSeek",
            "icon": "https://unpkg.com/@lobehub/icons-static-png@latest/light/deepseek-color.png",
            "command": str(DSH_RUNTIME / "dsh-acp.sh"),
            "args": [],
            "env": {
                "DSH_HOME": str(Path.home() / ".dsh"),
            },
        },
    ]


def merge_shellular_agents(existing: JsonDict) -> JsonDict:
    data = dict(existing) if isinstance(existing, dict) else {}
    custom = list(data.get("custom") or [])
    by_id = {
        row.get("id"): i for i, row in enumerate(custom) if isinstance(row, dict) and row.get("id")
    }
    for entry in shellular_cursor_entries():
        idx = by_id.get(entry["id"])
        if idx is None:
            custom.append(entry)
            by_id[entry["id"]] = len(custom) - 1
        else:
            custom[idx] = entry
    data["custom"] = custom
    if "disabled" not in data:
        data["disabled"] = []
    return data


def copy_live_files() -> None:
    LIVE_DIR.mkdir(parents=True, exist_ok=True)
    for name in LIVE_FILES:
        src = REPO_SCRIPTS / name
        if not src.is_file():
            raise FileNotFoundError(f"missing {src}")
        dest = LIVE_DIR / name
        shutil.copy2(src, dest)
        mode = dest.stat().st_mode
        dest.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def cmd_status() -> int:
    key = load_api_key()
    print("Cursor chat surfaces")
    print(f"  CLI: {cli_login_line()}")
    print(
        "  API key: "
        + (
            "present"
            if key
            else "MISSING (CURSOR_SYNC_API_KEY in Infisical Fleet-OPS via `fleet-secrets get`, "
            "or CURSOR_API_KEY in the environment)"
        )
    )
    print(f"  My Machines worker: {'up' if worker_running() else 'down'}")
    print(
        f"  Live install: {LIVE_DIR} {'yes' if (LIVE_DIR / 'cursor_acp_cloud_bridge.py').is_file() else 'no'}"
    )
    agents_file = load_json(SHELLULAR_AGENTS, {})
    custom = agents_file.get("custom") if isinstance(agents_file, dict) else []
    cursor_row = next(
        (row for row in custom if isinstance(row, dict) and row.get("id") == "cursor"),
        None,
    )
    if cursor_row and "cursor_acp_cloud_bridge.py" in " ".join(cursor_row.get("args") or []):
        print("  Shellular Cursor spawn: cloud bridge")
    else:
        print("  Shellular Cursor spawn: built-in cursor-agent acp (local only)")
    deepseek_row = next(
        (row for row in custom if isinstance(row, dict) and row.get("id") == "deepseek"),
        None,
    )
    deepseek_cmd = str((deepseek_row or {}).get("command") or "")
    if deepseek_row and "dsh-acp.sh" in deepseek_cmd:
        print("  Shellular DeepSeek spawn: pinned dsh-acp.sh")
    elif deepseek_row:
        print("  Shellular DeepSeek spawn: not pinned (need dsh-acp.sh)")
    else:
        print("  Shellular DeepSeek spawn: missing")
    if key:
        try:
            agents = list_cloud_agents(key, limit=5)
            print(f"  Cloud agents (latest {len(agents)}):")
            for agent in agents:
                ident = agent.get("id", "?")
                name = agent.get("name") or "(unnamed)"
                print(f"    {ident}  {name}")
                print(f"      {agent.get('url') or agent_web_url(str(ident))}")
        except RuntimeError as err:
            print(f"  Cloud list failed: {err}")
    acp = list_acp_sessions()[:5]
    print(f"  Local ACP sessions (not on iOS unless handed off): {len(list_acp_sessions())}")
    for row in acp:
        print(f"    {row['id']}  {row.get('title') or '(no title)'}  {row.get('cwd') or ''}")
    print("  Desktop: Agents Window / Cloud Agents panel, or " + AGENTS_INBOX)
    print("  iOS: Cursor app inbox (same backend).  Local CLI threads never appear there.")
    return 0 if key else 2


def cmd_list_cloud() -> int:
    key = load_api_key()
    if not key:
        print(
            "No Cursor API key.  Use CURSOR_SYNC_API_KEY (`fleet-secrets get CURSOR_SYNC_API_KEY`).",
            file=sys.stderr,
        )
        return 2
    agents = list_cloud_agents(key, limit=30)
    if not agents:
        print("No cloud agents returned.")
        return 0
    for agent in agents:
        ident = agent.get("id", "?")
        print(
            f"{ident}\t{agent.get('name') or '(unnamed)'}\t{agent.get('url') or agent_web_url(str(ident))}"
        )
    return 0


def cmd_open(target: str) -> int:
    key = load_api_key()
    if target in {"inbox", "agents", ""}:
        open_url(AGENTS_INBOX)
        print(AGENTS_INBOX)
        return 0
    if target == "latest":
        if not key:
            print("latest needs CURSOR_SYNC_API_KEY / CURSOR_API_KEY", file=sys.stderr)
            return 2
        agents = list_cloud_agents(key, limit=1)
        if not agents:
            print("No cloud agents.")
            return 1
        target = str(agents[0].get("id") or "")
    url = agent_web_url(target)
    open_url(url)
    print(url)
    return 0


def cmd_watch(open_new: bool, once: bool) -> int:
    key = load_api_key()
    if not key:
        print(
            "watch needs CURSOR_SYNC_API_KEY / CURSOR_API_KEY "
            "(Grok Bot still shows natively in Agents Window).",
            file=sys.stderr,
        )
        return 2
    known = seen_ids()
    seed = not known
    while True:
        agents = list_cloud_agents(key, limit=30)
        fresh: list[JsonDict] = []
        for agent in agents:
            ident = str(agent.get("id") or "")
            if not ident:
                continue
            if ident not in known:
                fresh.append(agent)
                known.add(ident)
        save_seen_ids(known)
        if seed:
            seed = False
            print(f"seeded {len(known)} existing cloud agents (not opened)")
        else:
            for agent in fresh:
                ident = agent.get("id")
                url = agent.get("url") or agent_web_url(str(ident))
                print(f"NEW {ident}  {agent.get('name') or '(unnamed)'}  {url}")
                if open_new:
                    open_url(str(url))
        if once:
            return 0
        try:
            time.sleep(20)
        except KeyboardInterrupt:
            print("stopped")
            return 0


def cmd_handoff_acp(session_id: str | None, on_mac: bool) -> int:
    key = load_api_key()
    if not key:
        print("handoff-acp needs CURSOR_SYNC_API_KEY / CURSOR_API_KEY", file=sys.stderr)
        return 2
    sessions = list_acp_sessions()
    if session_id:
        row = next((item for item in sessions if item["id"] == session_id), None)
        if row is None:
            print(f"unknown ACP session {session_id}", file=sys.stderr)
            return 1
    else:
        if not sessions:
            print("no ACP sessions under ~/.cursor/acp-sessions", file=sys.stderr)
            return 1
        row = sessions[0]
    mapped = session_map().get(row["id"])
    if isinstance(mapped, dict) and mapped.get("agentId"):
        print(f"already mapped {row['id']} -> {mapped['agentId']}")
        print(mapped.get("url") or agent_web_url(str(mapped["agentId"])))
        return 0
    title = row.get("title") or "Shellular Cursor session"
    cwd = str(row.get("cwd") or Path.home())
    prompt = (
        f"Continue this local Cursor CLI/ACP session from Shellular.\n"
        f"Title: {title}\n"
        f"Workspace: {cwd}\n"
        f"Local ACP id: {row['id']}\n"
        f"Inspect the workspace and pick up the work.  Do not restart unrelated tasks."
    )
    payload = create_cloud_agent(
        key,
        prompt,
        cwd,
        name=f"Shellular · {title}"[:100],
        on_mac=on_mac,
    )
    agent = unwrap_created_agent(payload)
    put_session_mapping(str(row["id"]), agent)
    ident = str(agent.get("id") or "")
    url = str(agent.get("url") or agent_web_url(ident))
    print(url)
    open_url(url)
    return 0


def cmd_install(shellular: bool) -> int:
    copy_live_files()
    print(f"copied helpers to {LIVE_DIR}")
    print(f"command: {LIVE_DIR / 'cursor-chat-surfaces'} status")
    if shellular:
        existing = load_json(SHELLULAR_AGENTS, {"custom": [], "disabled": []})
        if not isinstance(existing, dict):
            existing = {"custom": [], "disabled": []}
        merged = merge_shellular_agents(existing)
        save_json(SHELLULAR_AGENTS, merged)
        print(
            f"updated {SHELLULAR_AGENTS} "
            "(cursor -> cloud bridge, cursor-local ACP, deepseek -> dsh-acp.sh)"
        )
        print("restart Shellular to pick up the spawn: pm2 restart shellular")
    if not load_api_key():
        print(
            "Still need CURSOR_SYNC_API_KEY (`fleet-secrets get CURSOR_SYNC_API_KEY`).  "
            "Without it the Cursor bridge execs local cursor-agent acp and iOS cannot see those chats."
        )
        return 2
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="List, open, and hand off Cursor Cloud Agents for desktop + iOS."
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="Auth, worker, Shellular spawn, recent agents")
    sub.add_parser("list-cloud", help="GET /v1/agents")
    open_p = sub.add_parser("open", help="Open inbox, latest, or bc- id")
    open_p.add_argument("target", nargs="?", default="inbox")
    watch_p = sub.add_parser("watch", help="Poll for new cloud agents")
    watch_p.add_argument("--open", action="store_true", dest="open_new")
    watch_p.add_argument("--once", action="store_true")
    handoff = sub.add_parser("handoff-acp", help="Create a Cloud Agent from a local ACP session")
    handoff.add_argument("session_id", nargs="?")
    handoff.add_argument(
        "--on-mac", action="store_true", help="Route to My Machines worker jay-mac"
    )
    inst = sub.add_parser("install", help="Copy helpers to ~/apps and optionally patch Shellular")
    inst.add_argument("--shellular", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    cmd = args.cmd
    if cmd == "status":
        return cmd_status()
    if cmd == "list-cloud":
        return cmd_list_cloud()
    if cmd == "open":
        return cmd_open(str(args.target))
    if cmd == "watch":
        return cmd_watch(bool(args.open_new), bool(args.once))
    if cmd == "handoff-acp":
        return cmd_handoff_acp(args.session_id, bool(args.on_mac))
    if cmd == "install":
        return cmd_install(bool(args.shellular))
    parser.error(f"unknown command {cmd}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
