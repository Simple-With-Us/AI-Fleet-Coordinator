# Coolify host egress routing — do not touch

**Standing rule: never change default egress routing (exit node or full-tunnel VPN) on the Coolify host.**

## Why
On 2026-10-04, enabling a Tailscale exit node for the Coolify host broke ALL inbound traffic (Cloudflare 522 errors, about a 20 minute outage).  Tailscale's exit-node policy routing conflicts with Docker's iptables rules on that host.  Reverting the exit-node setting restored the site immediately.

## What is allowed
- Targeted routing that leaves the default route untouched (for example, the Mango WireGuard tunnel's specific /24 routes used for residential egress on particular API calls).
- Read-only inspection of routes and Tailscale state.

## If inbound traffic breaks after a networking change
1. Check the host's default route and Tailscale exit-node setting first.
2. Revert the networking change before doing anything else.
3. A 30-minute automated guardrail watches both values and alerts on drift.  It never changes anything itself — any revert is proposed for approval, never applied silently.
