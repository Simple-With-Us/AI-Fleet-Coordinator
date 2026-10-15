# AFC-285 — Signed Mac facts to fleet-loops-data every 3 hours (open-loops F7)

## Summary

Open-loops unit F7 landed on main in PR #494 (2026-10-11).  This rollout ties the tracked docs and cloud gather path to the correct Linear issue **AFC-285**, and names the canonical `gb/mac-facts.json` URLs for GB-Deployer and BF-Deployer routines.

## Code (already on main from PR #494)

- `scripts/fleet_facts/publish.py` — `fleet-facts publish-mac` / `verify` / `publish`
- LaunchAgent `scripts/launchd/com.jay.mac-facts-publish.plist` (every 3 hours at :30)
- `scripts/mac-process-watch.sh` — `expect_scheduled` + launcher path
- Infisical knobs in `scripts/fleet_knobs/registry.py` (`FLEET_FACTS_SIGNING_KEY`, publish caps, `FLEET_LOOPS_DATA_ALLOWED_APPS`, …)

## This PR

- Linear id **AFC-287 → AFC-285** in plist, process-list docs and launch-agent tests (the board issue was always AFC-285).
- `scripts/open_loops/urls.py` — `MAC_FACTS_API_URL` and `MAC_FACTS_RAW_URL` constants.
- `open-loops gather --no-mac` — optional `OPEN_LOOPS_MAC_FACTS_URL` when `--mac-facts` is omitted;  records `mac_facts_url` in the facts when the spec is https.

## Verification (cloud)

```bash
cd scripts && python3 -m unittest discover -s fleet_facts/tests -t .
cd scripts && python3 -m unittest discover -s open_loops/tests -t .
python3 scripts/fleet_facts/check_fixtures.py
```

## Owner live steps (Mac — not done by this agent)

1. **L4** — Create private repo `Simple-With-Us/fleet-loops-data` (no webhooks, no deploy keys, no broad app access).
2. **L5** — Set `FLEET_FACTS_SIGNING_KEY` in Infisical (AI Fleet Coordinator, prod);  decide `FLEET_LOOPS_DATA_ALLOWED_APPS` if any installed app must read the repo.
3. `scripts/fleet_facts/install.sh --apply` and `fleet-facts publish-mac --dry-run`.
4. **L15** — Removable Volumes grant for `~/apps/fleet-facts/mac-facts-publish`.
5. **L16c** — Load `com.jay.mac-facts-publish` and refresh the live process list.

Cloud routines: set `OPEN_LOOPS_MAC_FACTS_URL` to `https://api.github.com/repos/Simple-With-Us/fleet-loops-data/contents/gb/mac-facts.json` and provide `FLEET_FACTS_SIGNING_KEY` plus a repo read token (`FLEET_LOOPS_DATA_TOKEN` or `GH_TOKEN`).

## Follow-ups

- None in code until L4 exists and the first publish succeeds.
