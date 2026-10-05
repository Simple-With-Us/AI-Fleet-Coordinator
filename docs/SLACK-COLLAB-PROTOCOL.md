# Slack Multi-Room & Real-Time Agent Collaboration Protocol

Canonical reference for multi-agent real-time communication across all fleet platforms (Antigravity, Cursor, Claude Code, Codex, Grok, MiniMax, DeepSeek).

---

## 1. Architectural Model

Fleet coordination operates on two distinct communication layers:

### A. Intermittent Macro-Coordination (Broadcasting)
* **Channel:** `#agent-sync` (ID: `C0BEZDJDNKV`).
* **Purpose:** High-level claim and closeout lifecycle, global alerts, and deployment locks.
* **When to Post:**
  1. **Start of Work (Claim):** Triple-claim across board, GitHub issues, and Slack.
  2. **End of Work (Closeout):** Triple-closeout across board, GitHub issues, and Slack.
  3. **High-Severity Events (`[SENDER->FLEET]`):** Production down, merge block, deployment objection.
* **Format:** Strict header syntax:
  ```text
  [SENDER] sync-N
  repo: <project>
  Claiming task / description
  ```

### B. Intense Active Collaboration (Focused Multi-Agent Lanes)
* **Channel:** Dedicated **App Room** (e.g., `#botfleet`, `#codecaps`, `#hoghunter`, `#socratictrade`, `#congresstrade`, `#usage-monitor`).
* **Format:** **Threaded Discussions (`thread_ts`)**.
* **When to Use:**
  1. Multi-agent pair programming, refactoring, or bug investigation.
  2. Builder + verifier loops (e.g. implementation agent paired with review/testing agent).
  3. Cross-repository API contract reviews and schema sync.
* **Why Threads:**
  - Prevents top-level channel spam during rapid multi-turn exchanges.
  - Maintains strict conversational history and task context in a single parent thread.
  - Allows background watcher agents and human operators to follow or mute specific discussions.

---

## 2. Channel Directory

Always resolve channels by ID to survive display name changes:

| App / Purpose | Channel Name | Channel ID |
|---|---|---|
| **Fleet Cross-App Coordination** | `#agent-sync` | `C0BEZDJDNKV` |
| **AI Fleet Coordinator** | `#ai-fleet-coordinator` | `C0C6JNBRZNE` |
| **Fleet Operations** | `#fleet-ops` | `C0C6DHY8D0D` |
| **Socratic Trade** | `#socratictrade` | `C0BBPSEBNAW` |
| **Congress Trade** | `#congresstrade` | `C0BDJ7A74KZ` |
| **Congress Trading Shared** | `#congress-trading-shared` | `C0C63FY812T` |
| **Usage Monitor** | `#usage-monitor` | `C0C6LR70TLZ` |
| **BotFleet** | `#botfleet` | `C0C6CJEQXV1` |
| **CodeCaps** | `#codecaps` | `C0C6NFR5QRJ` |
| **HogHunter** | `#hoghunter` | `C0C6LPC8JPK` |
| **Autorotate** | `#autorotate` | `C0C63FTKWNB` |
| **Clutch** | `#clutch` | `C0C6NH6A17W` |
| **ContactLogo** | `#contactlogo` | `C0C6JNDDX9Q` |
| **DealDex** | `#dealdex` | `C0C63FVB3AT` |
| **FleetLink** | `#fleetlink` | `C0C6DJ05ZPX` |
| **Personal Site** | `#personal-site` | `C0C6GU8B222` |
| **Simple With Us** | `#simple-with-us` | `C0C7D8CT9EU` |

---

## 3. Tooling & Interfaces

### A. The `slack-collab` MCP Server (Preferred for Agents)
Every agent seat with MCP capabilities can attach to `slack-collab`.

* **Launcher:** `/Users/jay/apps/mcp-servers/slack-collab-launch.sh`
* **Implementation:** `/Users/jay/apps/slack-collab-mcp/`
* **Configuration:**
  - Cursor: `~/.cursor/mcp.json`
  - Antigravity / Gemini: `~/.gemini/antigravity/mcp/slack-collab/`
  - Claude Code: `claude mcp add slack-collab /Users/jay/apps/mcp-servers/slack-collab-launch.sh`
* **Available MCP Tools:**
  1. `slack_send_message`: Send a message to any channel (supports starting a thread via `thread_ts`).
  2. `slack_reply_thread`: Send a message into an existing thread (`channel`, `thread_ts`, `text`, optional `reply_broadcast`).
  3. `slack_read_channel`: Read the latest messages from a channel or fetch all replies in a specific thread (`channel`, `limit`, `thread_ts`).
  4. `slack_list_channels`: List all joined Slack channels with IDs.

### B. Command-Line Helper (`agent-sync-websocket.py`)
For shell environments, scripts, or agents without MCP:
```bash
# Post a claim or top-level message to #agent-sync:
AGENT_TAG=AG python3 /Users/jay/apps/agent-sync-websocket.py --post --text "[AG] repo: BotFleet | Claiming #123"

# Post into an app channel:
AGENT_TAG=AG python3 /Users/jay/apps/agent-sync-websocket.py --post --channel botfleet --text "Starting migration pass"

# Reply in a thread:
AGENT_TAG=AG python3 /Users/jay/apps/agent-sync-websocket.py --post --channel botfleet --thread 1791191465.129419 --text "Tests passing on main"
```

### C. Background Real-Time Relay
* **PM2 Process:** `agent-sync-push` running `/Users/jay/apps/agent-sync-push/daemon.js`.
* Automatically mirrors active messages and threads across listening seats.
* Connects via Slack Socket Mode (WebSocket) using `SLACK_SYNC_WEBSOCKET` token.

---

## 4. Collaborative Workflow (Step-by-Step)

When two agents work together on a task:

1. **Initiate:**
   Agent A posts a message in the target app channel:
   ```text
   [AGENT_A->AGENT_B] repo: Socratic.Trade | Starting work on auth session refactor.  Let's pair in this thread.
   ```
   Agent A captures the returned message timestamp `ts` (e.g. `1791195733.345169`).

2. **Engage in Thread:**
   Both Agent A and Agent B use `slack_reply_thread` (or `--post --thread <ts>`):
   - Agent B: `[AGENT_B->AGENT_A] Verified the token refresh route.  Reviewing your PR diff now.`
   - Agent A: `[AGENT_A->AGENT_B] Addressed feedback in commit 4b280a.  Running test suite.`

3. **Wrap Up & Broadcast:**
   Once resolved:
   - Post final summary inside the thread with `reply_broadcast: true` so the channel gets the resolution.
   - Post standard closeout in `#agent-sync`:
     ```text
     [AGENT_A] repo: Socratic.Trade | Completed auth session refactor (PR #456).  Paired with AGENT_B.
     ```
