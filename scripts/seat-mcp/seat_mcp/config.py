"""Paths and constants for seat-mcp.

Loopback only.  Token lives in Infisical Fleet-OPS /seat-mcp (SEAT_MCP_TOKEN), never in agents.json or a file.
"""

from __future__ import annotations

from pathlib import Path

HOME = Path("/Users/jay")
APP_ROOT = HOME / "apps" / "seat-mcp"
FLEET_SECRETS_BIN = HOME / ".local" / "bin" / "fleet-secrets"
TOKEN_FOLDER = "/seat-mcp"
TOKEN_NAME = "SEAT_MCP_TOKEN"
JOBS_DIR = HOME / ".seat-mcp" / "jobs"
AGENTS_JSON = HOME / ".shellular" / "agents.json"

BIND_HOST = "127.0.0.1"
BIND_PORT = 8793
MCP_PATH = "/mcp"

DSH_SH = HOME / "apps" / "dsh-runtime" / "dsh.sh"
GROK_ACP_CLIENT = HOME / "apps" / "grok-acp-runtime" / "acp-client.py"
GROK_LEADER_CLIENT = HOME / "apps" / "grok-acp-runtime" / "leader-client.py"
GROK_DRIVE = HOME / "apps" / "grok-acp-runtime" / "grok-drive.py"
GROK_ACP_PYTHON = Path("/usr/bin/python3")
GROK_BIN = HOME / ".grok" / "bin" / "grok"
GROK_ACP_HOST = "127.0.0.1"
GROK_ACP_PORT = 12419

# Allowed workspace roots:  /Users/jay/Code and /Users/jay/apps.
ALLOWED_CWD_ROOTS = (
    HOME / "Code",
    HOME / "apps",
)

DEFAULT_CWD = HOME / "apps"
DEFAULT_TIMEOUT_SEC = {
    "deepseek": 300,
    "grok": 180,
    "grok-tui": 25,
    "_echo": 15,
    "_sleep": 30,
}
MAX_TIMEOUT_SEC = 900
# session/new must return (and flush sessionId) before this, or the job fails.
GROK_SESSION_WAIT_SEC = 50
TAIL_BYTES = 16384
HEARTBEAT_SEC = 2.0
WEDGE_SEC = 15.0
KILL_GRACE_SEC = 1.0

# v1 seats we can actually spawn.  Shellular names are informational only.
IMPLEMENTED_SEATS = ("deepseek", "grok", "grok-tui")
TEST_SEATS = ("_echo", "_sleep")
