# Listener Settings Editor On The Server

AFC-710, work package OP-2 of the Operator plan.  Every listener setting lives in one file, `/data/listener.toml` inside the server's `eavesdropper` container, and until now only a hand edit could change it.  This adds the one writer a tool may use, and the helper verb that reaches it.  It is the kernel a later Muster control builds on;  it adds no button and changes nothing live.

**Not working yet, on purpose:**  apply is off in the allowlist, the installed helper on the server does not have the verb or the allowlist, and the running Eavesdropper image does not have the editor.  Each of those is an owner-gated step listed under Activation.

## What It Is

- `scripts/simple_tools/server/listener_settings_edit.py` is the editor.  It uses only the standard library, so the container runs it with `python3 -I` and the helper can load it from its bytes.
- `scripts/fleet-ops-listener-settings.json` is its one allowlist:  which settings may change, and the type and range of each.
- `scripts/fleet-ops-control.py` gains one verb, `listener-settings`, in two forms.

| Form | Where | What it does |
|---|---|---|
| `listener-settings get\|plan\|apply [--set KEY=VALUE ...] --file PATH [--expect SHA256] [--yes] [--json]` | a command line, never ssh | Runs the editor in this process on that file (a copy, a fixture). |
| `server listener-settings server --plan\|--yes [--json]` | the `fleet-ops-ctl` forced command, or a shell on the server | Runs the editor inside the container on the live file.  The request is one JSON object on standard input. |

Over ssh, `--plan` with nothing on standard input is a get;  `--plan` with `{"set": {"seat.ZC.enabled": false}}` is a plan;  `--yes` with the same object is the apply, and may carry the plan's hash as `expectPlanSHA256`.  `--file` and `--set` are not offered over ssh, and the `--from-muster` route does not offer the verb at all.

## What May Change

| Section | Keys |
|---|---|
| `[seat.X]` | `enabled`, `wake` (`inbox` or `http` only), `routine`, `budget`, `live` |
| `[daemon]` | `coalesce_seconds`, `coalesce_max_seconds`, `owner_coalesce_seconds`, `stale_after_minutes`, `held_alert_minutes`, `held_back_catch_up`, `peer_broadcast_reminder_minutes`, `presence_topics`, `host_wake_load_per_core` |
| `[linear_comments]`, `[linear_projects]`, `[notes_queue]`, `[listener_api]` | `enabled` |

A setting is named `table.key` or `seat.SEAT.key` and is set whole:  a `budget` or a `routine` is one value.  The editor changes settings;  it does not create a seat, and it does not remove a key.

## What Can Never Change

These are refused by the editor itself, whatever the allowlist says, and an allowlist that lists one is refused whole:

- a seat's `instance`, `creds`, `bot`, `bearer_file`, `bearer_locator`, `allow_admin`, `email_env`, `key_env` and `site_env`;
- the `[daemon]` `instance`, `owner_user_id`, `eligible_user_ids`, `owner_clients` and `outgoing_webhook_seats`;
- any key or sub-key whose name reads as a secret, a token, a credential, a path or a URL.

A routine's `url_env` and `key_env` are variable names, not values.  Each may stay as it is, or be one of the two names the seat's own code derives (`<CODE>_ROUTINE_URL` or `ZULIP_ALERT_<CODE>_ENDPOINT`, and the matching key names), so one seat's key can never be pointed at another seat's endpoint.  A string value that looks like a secret, a URL or a path is refused.

## How A Write Goes

1. The folder that holds the file is locked.  A second edit is refused at once.
2. The file is read without following a link, and every key and value is checked against the allowlist.
3. The edit is made on the text, so comments and every other line stay byte for byte.  The result is parsed and compared with the original:  the settings that differ must be exactly the requested ones with exactly the requested values, or nothing is written (`edit_unsafe`).
4. The candidate is written beside the file and judged by the daemon's own `check-config`.  A candidate it would not take is deleted, and the live file was never opened for writing (`check_failed`).
5. The old file is copied to `listener.toml.bak-settings-<UTC stamp>` (mode 600;  the newest twenty are kept), and the candidate replaces the live file with one rename.
6. The listener is told to reload (SIGHUP) and its `status.json` is read back.  A refused reload, or one that could not be sent, puts the old bytes back the same atomic way and reloads again, so the file on disk and the config the listener runs agree.

A plan does steps 2 to 4 with the candidate in a private temporary folder, and writes nothing beside the file.  It answers the settings that would change, the changed lines only (no context lines, so a pin or a variable name is never repeated), the check's verdict and a `planSHA256`.  An apply that names that hash does exactly what the plan showed, or is refused (`plan_mismatch`).

## Privilege And Audit

The container call is one fixed argv, run without a shell and without sudo:  `docker exec -i --user simpletools eavesdropper /usr/local/bin/python3 -I /app/scripts/simple_tools/server/listener_settings_edit.py --request-stdin`.  No setting, value or path from a request is a word of that argv.  The environment is the helper's pinned docker one, and docker's own words are only classified, never kept.  The file that is edited is the one the container's `SIMPLE_TOOLS_CONFIG` names;  a request cannot name a file.

An apply holds the helper's one action lock and writes one row to `ops-audit.jsonl` with the target `listener settings`.  The row names no setting and no value.  A get or a plan takes no lock and writes no row.

Anyone who can already run `docker exec` as that account can edit the file by hand.  The boundary for the ssh key is the forced command, the allowlist and the audit log, as for every other verb.

## Activation (Owner-Gated, Not Done By This Change)

1. **A ruling** that Muster may write the live `listener.toml` through the `fleet-ops-ctl` route, rather than a signed HTTPS endpoint.
2. **Install on the server**, as root, the way the eavesdropper-status verb was (`2026-11-07-eavesdropper-status-verb.md`, Deployment):  a hash-verified backup, then `fleet-ops-listener-settings.json` (root-owned, mode 0644) and the new `fleet-ops-control.py` into `/usr/local/lib/fleet-resource-control`.  The allowlist goes in first.  `scripts/hetzner/install-resource-control.sh` does not name the allowlist in its payload yet;  that is a follow-up.  Until this step, the server verb answers `unknown server verb`.
3. **Redeploy the Eavesdropper** from an image that holds this change, in a window the seat that runs the listener agrees.  Until then the verb answers `failed` with `editor_unavailable`.
4. **Turn apply on**:  set `"applyEnabled": true` in the allowlist through a reviewed change, then install that file.  Until then every apply answers `refused` with `apply_disabled`.
5. **The first live apply**, on a setting that is safe to flip back, with the readback below.

Rollback of steps 2 to 4 restores the two backups.  Rollback of an apply is the backup the apply named, written back with a second apply or by hand, then `simple-chat daemon reload`.

## Checks

- `cd scripts && python3 -m unittest simple_tools.tests.test_listener_settings_edit -v`:  a key that is not allowlisted is refused, a pin or a secret-looking key is refused whatever the allowlist says, a failed check-config leaves the file byte for byte, a refused reload rolls back byte for byte, and the plan's output equals the applied diff.  One class runs the real `check-config` on the image's sample.
- `python3 scripts/fleet-ops-control.py listener-settings plan --set seat.ZC.enabled=false --file <a copy of the sample>` prints the changed lines and writes nothing.
- `python3 scripts/test_fleet_ops_control.py` and `python3 scripts/check-ci-runner-policy.py` still pass.
- After activation, from the Mac:  `ssh fleet-ops-ctl -- server listener-settings server --plan --json` answers `result: read` with the allowlisted settings, and after an apply the listener's status shows every seat connected with `reload_refused` and `config_errors` empty.
