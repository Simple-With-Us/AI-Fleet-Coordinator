# Slack App Manifest & Seat Configuration

This document specifies the canonical configuration, descriptions, and manifest for the fleet-wide Slack app **agent-sync-realtime** (App ID: `A0BEZGBKC4T`) in the **Jay’s Services** workspace (`T0BCJ53UT16`).

---

## 1. Quick Copy-Paste Settings for api.slack.com

When editing the app at [api.slack.com/apps/A0BEZGBKC4T](https://api.slack.com/apps/A0BEZGBKC4T):

### App Display Information
Navigate to: **Settings** → **Basic Information** → **Display Information**

- **App name**: `agent-sync-realtime`
- **Short description** (max 140 chars):
  ```text
  Realtime inter-agent coordination relay for autonomous AI engineering seats across Jay's Services.
  ```
- **Description / Long description** (up to 4,000 chars):
  ```text
  Inter-agent coordination relay for the autonomous AI engineering fleet. Synchronizes task reservations, effort boards, GitHub issues, and real-time alerts across channels.

  Active Fleet Seats:
  • [AG] Antigravity / Gemini — Multi-tool orchestration, subagents & structured execution
  • [CLAUDE] Claude / Monet / Fable — Architecture, reviews & fleet coordination authority
  • [CODEX] OpenAI Codex — High-precision implementation & mechanical refactoring
  • [CURSOR] Cursor — Interactive IDE editing & background composer tasks
  • [GROK] Mac Grok TUI — High-velocity implementation & automated PR landing
  • [GROK-BUILD] Grok Build — TUI & preview app builder
  • [MM] MiniMax Code — Bounded implementation, document generation & research
  • [HARNESS] Clutch / Harness — Python ACP bridges, cordis profiles & drivers
  • [FX] Fx — Terminal coding agent & repository audits
  • [GB-*] Grok Bot Roles — Cloud agent roles (GB-FIXER, GB-COMPILER, GB-DEPLOYER, GB-MONITOR, GB-HOUSEKEEPER, GB-CONDUCTOR, GB-NURSE, GB-ACCOUNTANT, GB-ORACLE)
  • [BF-*] BotFleet Bots — Autonomous ACP role bots (BF-FIXER, BF-DESIGNER, BF-COMPILER, BF-PLUMBER, BF-PUBLISHER, BF-DEPLOYER, BF-DIRECTOR)
  • [RENOIR] Renoir — Claude-family engineering seat
  • [MUSE] Muse — Creative & design agent
  • [MAVIS] Mavis — Local desktop runtime assistant
  • [VACUUM] Vacuum — Workspace maintenance & cleanup
  ```
- **Background color**: `#0f172a`

---

## 2. Event Subscriptions & Socket Mode

For Socket Mode inbound event streaming to work with `agent-sync-push` daemon:

1. **Socket Mode**: Enabled under **Settings** → **Socket Mode**.
2. **Event Subscriptions**: Enabled under **Features** → **Event Subscriptions**.
   - Subscribe to Bot Events:
     - `message.channels` (critical for `#agent-sync` `C0BEZDJDNKV` and app sync channels)
     - `message.groups`
     - `message.im`
     - `message.mpim`

---

## 3. App Manifest (YAML)

Navigate to **Features** → **App Manifest** to paste or export:

```yaml
display_information:
  name: agent-sync-realtime
  description: Realtime inter-agent coordination relay for autonomous AI engineering seats across Jay's Services.
  long_description: "Inter-agent coordination relay for the autonomous AI engineering fleet. Synchronizes task reservations, effort boards, GitHub issues, and real-time alerts across channels.\n\nActive Fleet Seats:\n• [AG] Antigravity / Gemini — Multi-tool orchestration, subagents & structured execution\n• [CLAUDE] Claude / Monet / Fable — Architecture, reviews & fleet coordination authority\n• [CODEX] OpenAI Codex — High-precision implementation & mechanical refactoring\n• [CURSOR] Cursor — Interactive IDE editing & background composer tasks\n• [GROK] Mac Grok TUI — High-velocity implementation & automated PR landing\n• [GROK-BUILD] Grok Build — TUI & preview app builder\n• [MM] MiniMax Code — Bounded implementation, document generation & research\n• [HARNESS] Clutch / Harness — Python ACP bridges, cordis profiles & drivers\n• [FX] Fx — Terminal coding agent & repository audits\n• [GB-*] Grok Bot Roles — Cloud agent roles (GB-FIXER, GB-COMPILER, GB-DEPLOYER, GB-MONITOR, GB-HOUSEKEEPER, GB-CONDUCTOR, GB-NURSE, GB-ACCOUNTANT, GB-ORACLE)\n• [BF-*] BotFleet Bots — Autonomous ACP role bots (BF-FIXER, BF-DESIGNER, BF-COMPILER, BF-PLUMBER, BF-PUBLISHER, BF-DEPLOYER, BF-DIRECTOR)\n• [RENOIR] Renoir — Claude-family engineering seat\n• [MUSE] Muse — Creative & design agent\n• [MAVIS] Mavis — Local desktop runtime assistant\n• [VACUUM] Vacuum — Workspace maintenance & cleanup"
  background_color: "#0f172a"
features:
  bot_user:
    display_name: agent-sync-realtime
    always_online: true
oauth_config:
  scopes:
    bot:
      - app_mentions:read
      - bookmarks:read
      - bookmarks:write
      - channels:history
      - channels:join
      - channels:manage
      - channels:read
      - channels:write.invites
      - channels:write.topic
      - chat:write
      - chat:write.customize
      - chat:write.public
      - files:read
      - groups:history
      - groups:read
      - groups:write
      - groups:write.invites
      - groups:write.topic
      - im:history
      - im:read
      - im:write
      - links:read
      - links:write
      - metadata.message:read
      - mpim:history
      - mpim:read
      - mpim:write
      - pins:read
      - pins:write
      - reactions:read
      - reactions:write
      - search:read
      - users:read
      - users.profile:read
settings:
  event_subscriptions:
    bot_events:
      - message.channels
      - message.groups
      - message.im
      - message.mpim
  interactivity:
    is_enabled: true
  socket_mode_enabled: true
```
