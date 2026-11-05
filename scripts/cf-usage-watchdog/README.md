# Cloudflare account usage watchdog

A one-minute timer on the server that reads one Cloudflare account's usage and alerts, or in enforce mode stops the Worker behind a breach.  It exists because nothing else on the account can stop a Worker: billing alerts arrive a day late, and a Worker cannot watch the cap it shares.  It is the outer layer of AFC-617;  per-Worker `limits` and the in-code budget guard stop a loop first, and they do not depend on this.

`watchdog.py` uses the standard library and this repo's `fleet_knobs`.  `config.json` holds the rules, so they stay in git.

## What it reads

One GraphQL query per run covers Workers invocations, Durable Object invocations and periodic groups, D1, KV, R2 operations and R2 storage.  The query was run against the live account on Nov 4, 2026 and returned every dataset.  Day-to-date starts at midnight UTC, which is 6:00 PM in Chicago.  The 5-minute rate leaves out the newest 2 minutes, because the analytics run about 1 to 2 minutes behind.

Who owns what (which Worker binds which D1 database, KV namespace or R2 bucket, and which script a Durable Object namespace belongs to) comes from the live API once a day and stays in the state file, so no private inventory is in the repo.  A resource no Worker binds, such as a bucket reached by an S3 client, can only alert.

## Rules and profiles

`python3 watchdog.py thresholds` prints every rule with its dollar estimate.

- **paid**:  absolute runaway catchers per Worker or resource, about 10 times the measured 14-day peak.  The dollar figures are list price with the monthly allowances ignored, so they are upper bounds.  An account-wide rule only alerts on this profile.
- **free**:  80% of each Free cap, account-wide, because the caps are account-wide.  The 5-minute rate is 10% of the daily cap, so a breach means the cap would go in under an hour.  R2 uses a month-to-date rule, because its allowance is monthly and it bills on every plan.  An account-wide breach trips the largest contributor, which on most days is simple-tools-mcp.

## Modes

Mode and profile are the fleet knobs `CF_USAGE_WATCHDOG_MODE` (`observe` by default, or `enforce`) and `CF_USAGE_WATCHDOG_PROFILE` (`paid` by default, or `free`).  A failed knob read means observe.  A file named `STOP` in the state directory forces observe.

- **observe** alerts and logs what it would have tripped.  It changes nothing on Cloudflare.
- **enforce** also runs the actions below.  At most 1 Worker trips per run and 3 per UTC day, so a bad reading cannot take the whole account down.

A failed poll alerts after 5 minutes and never trips.  The run exits 1 on a failed poll or an alert nobody received, so the unit shows as failed.

## Actions, in order

Each one is logged with the version that was live before it, in the journal and in `events.jsonl`.

1. Post to Zulip through `simple-chat` as the CLAUDE seat, and page through Pushover.
2. `PUT /workers/scripts/{name}/secrets` with `GUARD_MODE=off`, then label the live deployment with a message starting `GUARD-TRIPPED` so a deployer can refuse to undo the trip.
3. If the rate has not fallen after 2 polls that hold minutes from after the trip, the fallback stop:  empty the cron triggers, turn off workers.dev and preview URLs, and detach custom domains.  With the 2-minute lag that is about 5 minutes after the trip.  The rate has fallen when it is under the threshold (a rate trip), or under half the rate at the trip (a day trip).

Requests a stopped Worker rejects still count, so an outside caller in a loop keeps the request rate up until step 3 removes the hostname.

**The alert path has a known weak spot.**  On this server `simple-chat` reaches Zulip through the hosted server, which is the simple-tools-mcp Worker on this same account, with no direct fallback.  If that Worker is the one tripped, or the account is capped, the Zulip post fails.  Pushover is then the only alert that goes out, so configure it before turning on enforce.  The daily check names it when it is missing.

## Why the stop is the fallback and not a stub version

The audit proposed a pre-uploaded stub version that keeps the Durable Object classes and answers 503.  The docs make that plausible but not proven here:  a version can be uploaded without deploying, only the last 100 uploaded versions can be deployed, every secret change creates a version, and no deployment can cross a Durable Object lifecycle change, so a stub would need re-uploading on a schedule.  Whether Cloudflare accepts a stub with these class exports and bindings can only be shown by uploading one, which this work did not do.  So this uses the documented fallback.

The cost is one real gap:  **the fallback does not stop Durable Object alarms.**  A Worker that has bundled the budget guard stops them through `GUARD_MODE=off`.  One that has not keeps running them, and the alert says to check it by hand.  A stub can replace the fallback after a rehearsal on a scratch Worker.

## Re-arm

Re-arm is manual.  `python3 watchdog.py status` lists trips.  `python3 watchdog.py rearm WORKER` prints the plan, and `--apply` deletes `GUARD_MODE` and restores the cron triggers, workers.dev and domains the trip recorded.  It does not roll the code back:  the Worker stays on the version the trip left live, and the previous version id is in the log.

## Daily check

Once a UTC day it compares the live Worker list, `limits`, workers.dev and preview URLs with `scripts/worker-registry.json`.  The file may be absent, and its absence is only logged.  It posts to Zulip when the differences change, and it never pages.  It also names a missing page path or heartbeat.

## Credentials

Everything is in Infisical, project AI Fleet Coordinator, folder `/cf-usage-watchdog`, read through `fleet-secrets` and held in memory only.

| Key | Holds | Needed |
| --- | --- | --- |
| `CF_USAGE_WATCHDOG_API_TOKEN` | Cloudflare token | Yes |
| `PUSHOVER_APP_TOKEN`, `PUSHOVER_USER_KEY` | The direct page path | Before enforce |
| `CF_USAGE_WATCHDOG_HEARTBEAT_URL` | Better Stack heartbeat URL, pinged after each good poll | Optional |

The Cloudflare token needs **Account Analytics Read** and **Workers Scripts Write**, on this account only.  Observe mode reads with it;  only enforce and re-arm write.

The unit logs in with a read-only Infisical service token in `/etc/cf-usage-watchdog/infisical.env`, owned by root with mode 0600, holding the line `INFISICAL_TOKEN=...`.  It needs to read `/` (for the two knobs) and `/cf-usage-watchdog` in the prod environment of AI Fleet Coordinator.  Record it in the Infisical Service Tokens tab of the registry sheet.

## Install

Merging installs nothing.  The code runs from the managed checkout, `~/.local/share/fleet-cli/repo`, which follows `origin/main`.

```
sudo install -d -m 0700 /etc/cf-usage-watchdog
sudo install -m 0600 /dev/null /etc/cf-usage-watchdog/infisical.env   # then add the one line
sudo install -m 644 scripts/systemd/cf-usage-watchdog.service scripts/systemd/cf-usage-watchdog.timer /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now cf-usage-watchdog.timer
```

Watch it in observe mode for a week before setting the mode knob to enforce.  Deployers must learn to refuse a `GUARD-TRIPPED` version first, since a deploy would restore cron triggers.

## Tests

```
python3 scripts/cf-usage-watchdog/test_watchdog.py
```
