# Onboarding links + subagent / model economics

Historical identifiers have been redacted. These records do not certify current deployment state.

## Context

Owner asked whether (1) agents must use sub-agents whenever they help and pick the most economical effective model per task (even if lower or higher than their session), and (2) onboarding docs are linked from the places agents start.  Economics was already in [retired identifier] but weaker.  Onboarding lived in README / QUICKSTART / DealDex only.

## What changed

- [retired identifier] § Delegation: spawn whenever it helps; right-size per task, not per session.
- Start-here tables (local path + GitHub URL) in TEMPLATE-AGENTS, both ONBOARDING docs, README, EFFORT-LOG-PROTOCOL, ST/CT/UM/DD AGENTS.md.
- Live `~/apps/FLEET-PROTOCOL.md`, `EFFORT-LOG-PROTOCOL.md`, `AGENT-COORDINATION-QUICKSTART.md`, and Grok/Claude/Codex/Gemini global instruction files.

## Verify

`python3 scripts/check-fleet-registry.py`
