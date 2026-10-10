---
name: fleet-infra
description: Access private fleet infrastructure inventory (host IPs, Tailscale mesh IPs, Coolify container UUIDs, Infisical project IDs, and SSH keys) maintained in Simple-With-Us/fleet-ops:ATTACK-MAP.md. Use when locating production servers, configuring environment variables, verifying edge routing, or handling infrastructure secrets without leaking them into public repos.
---

# Fleet Infrastructure & Private Inventory Access (ALL AGENTS)

> **This install is for `CODEX`.**  Chat tag `[CODEX·session8]`.  Notes `Codex`.  Branches `codex/`.  Lanes `~/apps/lanes/<Repo>/codex-<slug>`.  Do not inherit another seat's tag from a shared template.  Zulip bot `codex-bot@simplewithus.zulipchat.com`, credential file `~/.secrets/Zulip/Codex-zuliprc` (mode 600).  Session tag `[CODEX·session8]`, and the `agent-sync` CLI writes it for you.


All fleet repositories except `fleet-ops` are **public**.  To protect origin infrastructure from direct attacks, scanning, and DDoS, production host IPs, Tailscale IPs, Coolify container/server UUIDs, hardware serials, and secret keys must **never** be committed to public repositories or printed to chat/logs.

## Canonical Inventory Location

- **Local Workstation Agents (Mac/Terminal):**
  Read directly from `/Users/jay/Code/fleet-ops/ATTACK-MAP.md`.
  GitHub repo: [`Simple-With-Us/fleet-ops:ATTACK-MAP.md`](https://github.com/Simple-With-Us/fleet-ops/blob/main/ATTACK-MAP.md) (private).

- **Cloud / Remote Agents (without direct repo access):**
  Fetch securely from the Mac agent relay using `MAC_COLLAB_TOKEN`:
  ```bash
  curl -fsS -H "Authorization: Bearer ${MAC_COLLAB_TOKEN}" https://mac.jays.services/files/ATTACK-MAP.md
  ```

## What Lives in `fleet-ops:ATTACK-MAP.md`

1. **Host Topology & IP Addresses:**
   - Production Hetzner Linux host public IP, Tailscale MagicDNS (`server.boa-roygbiv.ts.net`), and mesh IP (`100.69.77.26`).
   - Mac workstation local relay Tailscale IP (`100.113.106.39`).
   - iPhone operator client Tailscale IP (`100.100.72.76`).
   - Retired Oracle server references and transition logs.

2. **Coolify Container & Server UUIDs:**
   - Server UUIDs for Hetzner (`fleet-hetzner-nbg1`).
   - Application UUIDs for `Socratic.Trade`, `Congress.Trade`, and `Usage-Monitor`.

3. **Infisical Project IDs:**
   - Project workspace IDs for ST, CT, Shared, and UM scopes.

4. **Edge & Access Control Rules:**
   - Cloudflare origin isolation rules (blocking non-CF traffic on `:80`/`:443`).
   - SSH `:22` access restricted to Tailscale mesh (`macbook` and `iphone`).
   - Coolify control plane `:8000` bound to Tailscale IP.

## Invariants for Public Repositories

1. **Never Hardcode Infrastructure IDs in Public Repos:**
   Always read values from environment variables (`INFISICAL_PROJECT_ID`, `COOLIFY_ST_APP_UUID`, `HETZNER_HOST_IP`, etc.) or query them dynamically via authenticated APIs.

2. **Secrets Handoff Protection:**
   Read credentials from `~/.secrets/global-api-keys` or Infisical.  Never `cat`, `grep -E '^[A-Z0-9_]+='`, or log secret values into transcripts.

3. **Mock Data in Tests & Documentation:**
   Use RFC 5737 documentation ranges (`192.0.2.1`, `198.51.100.1`, `203.0.113.1`) and generic mock UUIDs (`mock-app-uuid`, `mock-project-id`) in test fixtures and public rollouts.
