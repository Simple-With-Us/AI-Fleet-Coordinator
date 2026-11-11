# 2026-08-31 — Fleet Sentry Monitor Expansion: DealDex & BotFleet (Antigravity, `ag/sentry-observability-expansion`)

## Summary
Expands `fleet-sentry-monitor/monitor.py` in `AI-Fleet-Coordinator`:
- Added `dealdex` (`https://dealdex.net/`) and `botfleet` (`https://botfleet.app/`) to `PROD_HEALTH_ENDPOINTS` with support for both JSON and HTTP 200 health responses.
- Added `dealdex` and `botfleet` to `PM2_TAGS` for automatic process crash-loop alerting and breadcrumb tagging.
- Provisioned Sentry projects `botfleet` (id `4512009246736384`), `autorotate` (id `4512009246736385`), and `contactlogo` (id `4512009246801921`) under Sentry org `jays-services`.

## Verification
- `python3 -m py_compile fleet-sentry-monitor/monitor.py` — 0 errors.
