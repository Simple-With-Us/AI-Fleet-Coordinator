---
name: fleet-infra
description: Access private fleet infrastructure inventory (host IPs, Tailscale mesh IPs, Coolify container UUIDs, Infisical project IDs, and SSH keys) maintained in Simple-With-Us/fleet-ops:ATTACK-MAP.md.  Use when locating production servers, configuring environment variables, verifying edge routing, or handling infrastructure secrets without leaking them into public repos.
---

# Fleet Infrastructure & Private Inventory Access (ALL AGENTS)

> **This install is for `MC`.**  Chat tag `[MC·session8]`.  Notes `Muse Code`.  Branches `muse-code/`.  Lanes `~/apps/lanes/<Repo>/muse-code-<slug>`.  Do not inherit another seat's tag from a shared template.  Zulip bot `mc-bot@simplewithus.zulipchat.com`, credential in Infisical Fleet-OPS `/zulip` (`MC_ZULIP_EMAIL`, `MC_ZULIP_KEY`, `MC_ZULIP_SITE`).  Session tag `[MC·session8]`, and the `simple-chat` CLI writes it for you.

> **Runtime (Muse Code).**  Muse Code (`muse` CLI) is the interactive terminal coding agent (`[MC]`).  Notes name `Muse Code`.  Branches `muse-code/`.  New lanes are made with `~/apps/lane new <app> <slug>` at `~/apps/lanes/<Repo>/muse-code-<slug>`, and `AGENT_SEAT=MC` comes from the `muse-seat` wrapper, never from a guess.  Start `muse` inside a lane; `muse -w` appears to make a worktree inside `~/Code/<App>/.muse/worktrees`, so do not use it.  Distinct from **Muse Assist** (`[MA]`, former tag `[MUSE]`), which is the cloud VM batch compute / creative assistant dispatched via Mac/iOS apps.  Project `AGENTS.md` and `CLAUDE.md` load automatically when the workspace is trusted in `~/.config/muse/trust.json`, and the user-level `~/.claude/CLAUDE.md` loads as a fallback.  Skills installed here (`~/.config/muse/skills`) shadow foreign personal skills.  Setup checklist: `docs/MUSE-ONBOARDING.md`.


All fleet repositories except `fleet-ops` are **public**.  To protect origin infrastructure from direct attacks, scanning, and DDoS, production host IPs, Tailscale IPs, Coolify container/server UUIDs, hardware serials, and secret keys must **never** be committed to public repositories or printed to chat/logs.

## Canonical Inventory Location

- **Local Workstation Agents (Mac/Terminal):**
  Read directly from `/Users/jay/Code/fleet-ops/ATTACK-MAP.md`.
  GitHub repo: [`Simple-With-Us/fleet-ops:ATTACK-MAP.md`](https://github.com/Simple-With-Us/fleet-ops/blob/main/ATTACK-MAP.md) (private).

- **Cloud / Remote Agents (without a local clone):**
  A seat with read access to the private repo fetches the file with `gh`:
  ```bash
  gh api repos/Simple-With-Us/Fleet-OPS/contents/ATTACK-MAP.md -H 'Accept: application/vnd.github.raw'
  ```
  A seat without access asks a Mac seat or the owner.  The old Mac relay (`mac.jays.services/files/ATTACK-MAP.md`) was retired 2026-10-19.

## What Lives in `fleet-ops:ATTACK-MAP.md`

1. **Host Topology & IP Addresses:**
   - The production Hetzner Linux host public IP, its Tailscale MagicDNS name, and its mesh IP.
   - The Mac workstation local relay Tailscale IP.
   - The iPhone operator client Tailscale IP.
   - Retired Oracle server references and transition logs.
   - The values themselves are private deployment inventory: they live in `fleet-ops:ATTACK-MAP.md` and in the operator's own host table on that Mac, and this pack names where they live rather than carrying them.

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
   Read credentials with `fleet-secrets get NAME` (Infisical Fleet-OPS) or from the app's own Infisical project.  Never `cat`, `grep -E '^[A-Z0-9_]+='`, or log secret values into transcripts.

3. **Mock Data in Tests & Documentation:**
   Use RFC 5737 documentation ranges (`192.0.2.1`, `198.51.100.1`, `203.0.113.1`) and generic mock UUIDs (`mock-app-uuid`, `mock-project-id`) in test fixtures and public rollouts.
