# Fleet RAG nightly ingest (AFC-687)

The nightly Fleet RAG ingest is a scheduled job with no LLM.  The Hetzner host runs `recall ingest` after `code-main-sync`.  The Mac pushes skill and memory files to a mirror the server reads.  Both jobs ship disabled.  Jay turns each one on.

The Qdrant collection and the TEI embedder are the mesh endpoints in `docs/RAG-FLEET-INFRA.md`.  The doc walk reads `FLEET_CODE_ROOT` from `/etc/fleet/lanes.conf`.  The code checkouts stay on main through `code-main-sync.timer`.

## Server job

Units: `scripts/systemd/fleet-rag-nightly.service` and `scripts/systemd/fleet-rag-nightly.timer`.  The example environment is `scripts/systemd/fleet-rag-nightly.env.example`.

The timer is `02:30` America/Chicago.  Confirm the next elapse with `systemd-analyze calendar` before enabling it.  A missed night runs after boot (`Persistent=true`).

The service starts only when `/etc/fleet/rag-nightly.env` exists.  The process runs only when `FLEET_RAG_NIGHTLY` is exactly `1`.  Any other value exits 0 before a fetch, a sync, or an ingest.  The service file has no install section.  Copying the unit does not enable the timer.

Each run takes `~/.local/state/fleet-rag-nightly/nightly.lock`.  A busy lock exits 0 and stays quiet.  The ingest then takes its own lock and waits, because this source set attests health.

The command is:

```text
recall ingest --source linear,doc,skill,memory --prune --no-heartbeat
```

`--no-heartbeat` keeps the process off `fleet-secrets`.  That reader can use a machine identity.  This job never calls it.

The nightly accepts a recent successful code-main-sync and does not start that unit.  The read is `systemctl show` on `code-main-sync.service`, which the service account can do.  It passes when the unit is not running, `Result` is `success`, `ExecMainStatus` is 0, and that run started within the last hour.  A success older than an hour does not count.  If the unit is still running, the job polls that same read until the oneshot finishes or 30 minutes pass.  It does not call `systemctl start` to wait.

The first night, 2:30am CT on Nov 11 2026, exited `code-main-sync-exit-1` before ingest.  The job ran as User=jay and `systemctl start` returned Access denied, while `code-main-sync.timer` had already succeeded.  A start is attempted only when that read is a failed or stale run, or did not answer, and the process is allowed to start the unit.  Root may start it.  `FLEET_RAG_CODE_SYNC_START=1` may start it.  `FLEET_RAG_CODE_SYNC_BY_UNIT=1`, which the unit sets, never starts it.  If a start comes back Access denied or interactive authentication required, the job reads the result again and still accepts a fresh success.  A denial with nothing fresh skips ingest, writes the summary, posts an alert, and the process exits with status 1.  The lock behavior is unchanged.

`fleet-rag-nightly.service` still has `Wants=code-main-sync.service` and `After=code-main-sync.service`, using `Wants=` rather than `Requires=`, so a failed sync still reaches the job.  That ordering is not required for the denied-start fix.  The script in the checkout is what stops the denied start.

When `FLEET_CODE_SYNC_STATE` is set, code-main-sync writes a JSON stamp (`ok`, `exit`, `at`) at mode 0644 after a finished run, and not after a dry run.  The nightly trusts that file only when `systemctl show` did not answer, the file is not a symlink, it is not group or world writable, and a non-root reader does not own it.  A failed or stale Result from systemd is final, so an older stamp cannot hide it.  No polkit rule is required, and no unit reinstall is required for the denied-start failure.  After merge, the checkout named by `FLEET_RAG_CHECKOUT` has to be on this commit before the next 02:30 run.  The timer fast-forwards clean clones under `FLEET_CODE_ROOT`.  If the nightly checkout is that clone, the timer does it.  A manual root run with the flag unset still starts the unit when the last success is stale.

Prune uses the guards from AFC-677. Nothing is pruned when any source in the run failed.  A prune is refused when the source yielded nothing and documents would go, when more documents would go than were yielded, or when a whole `doc/<repo>/` of 5 or more documents would vanish.  `FLEET_RAG_PRUNE_FORCE=1` lifts only the second and third of those.  Linear rows are still skipped, because that generator is not a deletion inventory.

The doc diet excludes coverage and framework output, vendored and generated trees, lockfiles, minified files, and binaries, on top of the older `node_modules`, dist, and build skips.  A dry run writes `doc-diet-report.json` beside the state file, mode 0600, with document and chunk counts before and after.  Newly excluded documents stay in the yielded set, so a normal `--prune` does not delete them.  Passing `--prune-excluded`, or setting `FLEET_RAG_PRUNE_EXCLUDED` to 1, releases every held id.  `FLEET_RAG_PRUNE_EXCLUDED_ALLOW` releases an exact doc id, or a prefix that ends in `*`.  That release needs a dry-run stamp whose `rules_hash` matches the current rules.  Force does not lift a missing stamp.  When the gone set includes a released id, the prune also refuses if more than 20% of that source's tracked documents would go.  Exactly 20% is allowed.  `FLEET_RAG_PRUNE_FORCE=1` lifts that cap the same way it lifts the yielded and whole-repo guards.  It does not lift a failed source or the stamp.  Identical chunk bodies are embedded once per doc-source run.  Later paths are stored on `also_paths`.

## Linear incremental read

The Linear source keeps an `updatedAt` cursor in the ingest state under `_linear_sync`.  A nightly run asks Linear only for issues updated since that cursor, minus a safety overlap.  The overlap defaults to 2 hours (`FLEET_RAG_LINEAR_OVERLAP_HOURS`, from 1 to 168).  The cursor moves only after that source finishes without error.  A dry run, a `--limit` run, a `--since` run that skips an issue, a skipped issue, or a failed read leaves the cursor where it was.

A full id sweep runs when the cursor is missing, corrupt, or older than the sweep interval, and when the last successful full sweep is older than that same interval.  The interval defaults to 7 days (`FLEET_RAG_LINEAR_SWEEP_DAYS`, from 1 to 90).  An invalid value keeps the default.  `--full`, also forwarded by `recall ingest --full`, sets `FLEET_RAG_LINEAR_FULL` for that run.  That forces a full id sweep and refetches every issue body.  A scheduled sweep still uses the per-issue `updatedAt` cache.  The content hash still skips re-embedding when the body is unchanged.

A full read that returns zero issues aborts.  That is a broken read, not an empty board.  An incremental read that returns zero changed issues is a quiet window, and the cursor still advances.  Linear rows are still not pruned.  The seat order is unchanged: BF-ORACLE, then GB-ORACLE.

The full id scan of about 14.9k issues is about 19 minutes of a 47-minute nightly.  Incremental nights skip that scan.  They read the overlap window plus issues updated since the last successful cursor.  One night each week still pays for the full id sweep.

Apple Notes stay out of the command.  A run that does include `apple-note` records a warning and does not prune that source.  Force does not override that.  Notes are upserted at write time (AFC-685).

Skill, memory, and apple-note ingest and prune wait on the Mac mirror marker.  On the server that file is `/home/jay/fleet-rag-mirror/current/.complete`.  A missing, incomplete, or stale marker skips ingest and prune for those three sources, records `mirror_stale` in `~/.local/state/fleet-rag-nightly/last.json` with the marker age in `age_s`, and still lets doc and linear run.  The skip is not a failed source.  Force does not override it, so those sources are never pruned from stale data.  One missed night stays fresh (36 hours).  Two missed nights go stale.  `FLEET_RAG_MIRROR_MAX_AGE_S` can move that window between 1 hour and 14 days.  An invalid value stays at 36 hours.

If `FLEET_RAG_MIRROR` is unset, the job points it at a directory that is not created, records guard reason `unconfigured` and `mirror_stale` with a null age, and does not ingest or prune skill, memory, or apple-note.  If `FLEET_CODE_ROOT` is unset, doc prune is skipped with reason `code-root-unset`.  The Mac `--all` run does not set that requirement, so its doc prune is unchanged.  The ingest CLI uses the same rule.  A set `FLEET_RAG_MIRROR` makes that directory's `.complete` file the marker.  When the variable is unset and `/home/jay/fleet-rag-mirror` exists, or `HOME` is `/home/jay`, the CLI checks `/home/jay/fleet-rag-mirror/current/.complete`.  A fresh marker makes that tree the skill and memory source.  The ingest report carries the same `mirror_stale` object.

The server source set attests health, and so does `--all`.  Merging this change leaves a Mac `--all --prune` able to update the sentinel.  Apple Notes remain in `--all`.

A successful run is quiet.  Zulip gets a post on the OPS stream, topic `fleet-rag-nightly`, when `ok` is false, a prune guard trips, the mirror is missing or stale, or `code-main-sync` failed or is too old to count for tonight.  The tag is `[GB-ORACLE 📁AFC]`.  The same failure on consecutive nights posts a short “still failing, night N” line instead of repeating the full detail.  A failed alert post does not change the ingest exit.  The summary is `~/.local/state/fleet-rag-nightly/last.json`, mode 0600.  It holds reasons, counts, `alert_fingerprint`, and `alert_streak`.  When a real prune ran, it also records `qdrant_snapshot.name` (and `path` when the snapshot was archived under `qdrant-prune-snapshots/`).  Secret values are withheld.

## Mac job

Installer: `python3 scripts/fleet_rag/mirror_install.py`.  Template: `scripts/launchd/com.jay.fleet-rag-mirror.plist`.  Example paths live in `scripts/launchd/fleet-rag-mirror.env.example` (copy to a private 0600 env file; the job never reads that file at runtime).

`apply` without `--enable` writes the LaunchAgent and a config with `enabled` false, and does not call `launchctl`.  `--enable` is the only path that sets `enabled` true and bootstraps the agent.  A loaded agent still does nothing while `enabled` is false.

The schedule is 01:30 on the Mac clock, which is Central, so the trees land before 02:30. `RunAtLoad` is false.  A missed calendar run happens once when the Mac wakes.  An overlapping push exits 0.

The push rsyncs these trees into `<remote_root>/incoming`:

| Local | Remote tree under `current` |
|---|---|
| `~/.claude/skills` | `skills/claude` |
| `~/.cursor/skills` | `skills/cursor` |
| `~/.grok/skills` | `skills/grok` |
| `~/.claude/projects/*/memory` | `memory/claude-projects` |
| `~/.codex/memories` | `memory/codex` |

`--delete` is applied only to the incoming tree.  The live directory is `<remote_root>/current`, which is the value of `FLEET_RAG_MIRROR` on the server.  Promote swaps incoming onto current only after every rsynced tree's file count matches.  A failed rsync leaves current untouched.  A local tree that is missing is copied from current into incoming, so an absent Mac directory does not wipe the server copy.  The stamp `.complete` is written only by a successful promote.

The Mac side holds no Infisical token.  SSH uses the owner's existing key, in batch mode.

## Credentials

Service tokens only.  A set machine-identity variable refuses the run before any fetch.  The names are `INFISICAL_UNIVERSAL_AUTH_CLIENT_ID`, `INFISICAL_UNIVERSAL_AUTH_CLIENT_SECRET`, `INFISICAL_CLIENT_ID`, `INFISICAL_CLIENT_SECRET`, `INFISICAL_AUTOMATION_CLIENT_ID`, `INFISICAL_AUTOMATION_CLIENT_SECRET`, `INFISICAL_SHARED_CLIENT_ID`, and `INFISICAL_SHARED_CLIENT_SECRET`.

Each token file is a regular file, mode 0600 or 0400, not a symlink.  The value starts with `st.`.  The recall file is the env assignment from AFC-677.  The Linear and Zulip files are the raw token on one line.  The job logs the variable name and a short reason.  It does not log the value, and it does not put the value in the summary or the Zulip post.

1. Recall.  The nightly reads `/home/jay/.secrets/fleet-recall-infisical.env`, the file already on the server from AFC-677.  `FLEET_RECALL_INFISICAL_TOKEN_FILE` overrides that path.  The file holds `FLEET_RECALL_INFISICAL_TOKEN=<st. token>`.  An `export ` prefix and quotes are allowed.  Project shared-at-ct, the default in `fleet_rag.core`, prod, folder `/`.  `FLEET_RAG_INFISICAL_PROJECT` overrides that project id.  Keys read: `QDRANT_URL`, `QDRANT_API_KEY`, `QDRANT_FLEET_COLLECTION`, `TEI_URL`, `TEI_API_KEY`.  This is the existing recall service token.  All five keys are required.  The checkout named by FLEET_RAG_CHECKOUT is this repository, because the job imports scripts/simple_tools/secretscan.py from it.

2. Linear.  File path in `FLEET_RAG_LINEAR_INFISICAL_TOKEN_FILE`.  Project AI Fleet Coordinator, prod, folder `/linear`.  That is where the seat pairs live, not the shared project that holds the recall keys.  The project id comes from `FLEET_RAG_LINEAR_INFISICAL_PROJECT`, else from `INFISICAL_AI_FLEET_COORDINATOR_PROJECT_ID` or `~/.simple-tools/ai-fleet-coordinator-project-id`.  If none is set, the run stops with `linear-project-unset`.  Suggested token name `fleet-rag-nightly-linear-read`, read-only.  Keys requested: `LINEAR_BF_ORACLE_CLIENT_ID`, `LINEAR_BF_ORACLE_CLIENT_SECRET`, `LINEAR_GB_ORACLE_CLIENT_ID`, `LINEAR_GB_ORACLE_CLIENT_SECRET`.  One complete pair is enough.  The seat order is BF-ORACLE, then GB-ORACLE.  The job does not read cache tokens and does not use the automation identity.  A folder-scoped token can see other seats' Linear keys.  The job asks for these four names.  Jay mints this token.  It is not in the fleet knobs registry yet.  A registry row is an owner follow-up.

3. Zulip, optional.  File path in `FLEET_RAG_ZULIP_INFISICAL_TOKEN_FILE`.  Project id in `FLEET_RAG_ZULIP_INFISICAL_PROJECT`.  Folder in `FLEET_RAG_ZULIP_INFISICAL_PATH` (preferred `/fleet-rag-nightly`, with exactly `ZULIP_SITE`, `ZULIP_EMAIL`, and `ZULIP_API_KEY`).  Leave the file unset until that token exists.  Ingest still runs.  The summary then says the alert was unconfigured.

The Mac push has no token.

## First server state

An empty `~/apps/fleet-rag/state` upserts what the walk sees and prunes nothing, because there are no extra state rows.  The next run prunes with the guards.  Jay may copy the Mac `ingest-state.json` into that directory before the first enabled run.  The guards still apply to that copy.  The copy is optional.

`HOME` on the unit is the account that should own that state.  A drop-in sets `User=` and `Group=` to the same account.  The tracked unit does not pick an account.

## Enable the server job

Do these on the host.  This repository does not run them.

1. Leave the recall token at `/home/jay/.secrets/fleet-recall-infisical.env`.  Mint the Linear token, and the Zulip token if alerts should post.  Put each of those values in its own 0600 file.  Point the environment at the files.
2. Confirm `/etc/fleet/lanes.conf` sets `FLEET_CODE_ROOT` and `FLEET_APPS_ROOT`.
3. Install the unit, the timer, and the env file from this checkout.  Leave `FLEET_RAG_NIGHTLY=0`.

```bash
sudo install -d -m 0755 /etc/fleet
sudo install -m 0644 scripts/systemd/fleet-rag-nightly.service /etc/systemd/system/
sudo install -m 0644 scripts/systemd/fleet-rag-nightly.timer /etc/systemd/system/
sudo install -m 0600 scripts/systemd/fleet-rag-nightly.env.example /etc/fleet/rag-nightly.env
```

4. Add the account drop-in with `sudo systemctl edit fleet-rag-nightly.service`:

```ini
[Service]
User=<account>
Group=<account>
```

5. Confirm the clock before the flag is turned on:

```bash
systemd-analyze calendar '*-*-* 02:30:00 America/Chicago'
```

6. Reload and start once while the flag is still 0. The summary says `enabled` false and the exit is 0.

```bash
sudo systemctl daemon-reload
sudo systemctl start fleet-rag-nightly.service
```

7. Set `FLEET_RAG_NIGHTLY=1` in the env file, then enable the timer:

```bash
sudo systemctl enable --now fleet-rag-nightly.timer
```

8. Read `last.json` after the first real run.  Success stays off Zulip.  A guard trip, stale mirror, code-sync failure, or ingest failure posts to OPS.

## Enable the Mac job

The Mac needs no Infisical token.  The SSH key must work non-interactively to the server.

```bash
# Paths below match scripts/launchd/fleet-rag-mirror.env.example — copy that file,
# edit the values for the Mac and server, keep it mode 0600, then:
set -a
source ~/Library/Application\ Support/fleet-rag/mirror-install.env
set +a
python3 scripts/fleet_rag/mirror_install.py \
  --ssh-target "$FLEET_RAG_MIRROR_SSH_TARGET" \
  --remote-root "$FLEET_RAG_MIRROR_REMOTE_ROOT" \
  --remote-script "$FLEET_RAG_MIRROR_REMOTE_SCRIPT" \
  --remote-python "$FLEET_RAG_MIRROR_REMOTE_PYTHON"
```

That writes `enabled` false and does not bootstrap.  Confirm the config, then turn the schedule on with the same command plus `--enable`.  LaunchAgent stdout and stderr land in `~/Library/Logs/fleet-rag-mirror.log`; the push also appends one summary line there each run.

`remote_root` is the parent.  `FLEET_RAG_MIRROR` on the server is `<remote_root>/current`.

## Roll back

Server: set `FLEET_RAG_NIGHTLY=0`.  The next start exits 0 before any ingest.  To unload the timer:

```bash
sudo systemctl disable --now fleet-rag-nightly.timer
```

Mac: run `apply` again without `--enable`, which writes `enabled` false.  Unload a bootstrapped agent with:

```bash
launchctl bootout gui/$(id -u)/com.jay.fleet-rag-mirror
```

The last promoted mirror stays on the server.  Ingest keeps pruning skill and memory against it while the stamp is fresh.  After the stamp goes stale, those two prunes stop and an alert posts.  Doc and linear keep their own prune rules.

## Checks

```bash
cd scripts && python3 -m unittest fleet_rag.tests.test_nightly fleet_rag.tests.test_mirror_logging fleet_rag.tests.test_ingest fleet_rag.tests.test_sources
```
