# Eavesdropper Settings Editor On The Server

AFC-710, work package OP-2 of the Operator plan.  Every Eavesdropper setting lives in one file, `/data/listener.toml` inside the server's `eavesdropper` container, and until now only a hand edit could change it.  This adds the one writer a tool may use, and the helper verb that reaches it.  It is the kernel a later Muster control builds on;  it adds no button and changes nothing live.

**Not working yet, on purpose:**  apply is off in the allowlist, the installed helper on the server does not have the verb or the allowlist, and the running Eavesdropper image does not have the editor.  Each of those is an owner-gated step listed under Activation.

## Names

The component is the Eavesdropper (owner rename, Wed, Nov 4, 2026), so the verb, the editor, the allowlist and this page carry that name, as `eavesdropper-status` already does.  The file it edits is still called `listener.toml`, and its state folder `listener/`:  those names belong to the daemon (`scripts/simple_tools/config.py` and the image), which this change does not touch.  The editor takes the file's path from `SIMPLE_TOOLS_CONFIG`, so it follows a rename of the file with no change here.  The config tables keep their own names too (`[listener_api]`).

## What It Is

- `scripts/simple_tools/server/eavesdropper_settings_edit.py` is the editor.  It uses only the standard library, so the container runs it with `python3 -I` and the helper can load it from its bytes.
- `scripts/fleet-ops-eavesdropper-settings.json` is its one allowlist:  which settings may change, and the type and range of each.
- `scripts/fleet-ops-control.py` gains one verb, `eavesdropper-settings`, in two forms.

| Form | Where | What it does |
|---|---|---|
| `eavesdropper-settings get\|plan\|apply [--set KEY=VALUE ...] [--remove KEY.SUBKEY ...] --file PATH [--expect SHA256] [--yes] [--json]` | a command line, never ssh | Runs the editor in this process on that file (a copy, a fixture). |
| `server eavesdropper-settings server --plan\|--yes [--json]` | the `fleet-ops-ctl` forced command, or a shell on the server | Runs the editor inside the container on the live file.  The request is one JSON object on standard input. |

Over ssh, `--plan` with nothing on standard input is a get;  `--plan` with `{"set": {"seat.ZC.enabled": false}}` is a plan;  `--yes` with the same object is the apply, and may carry the plan's hash as `expectPlanSHA256`.  `--file` and `--set` are not offered over ssh, and the `--from-muster` route does not offer the verb at all.

A get is "standard input ended with nothing on it", so the caller has to close it:  `ssh -n fleet-ops-ctl -- ...`, or `</dev/null`.  A plain `ssh` from a terminal keeps the remote input open.  The helper then waits its five seconds and refuses the request (`usage`), and never takes it for a get.  A caller that sends a request closes its input after the JSON object.

## What May Change

| Section | Keys |
|---|---|
| `[seat.X]` | `enabled`, `wake` (`inbox` or `http` only), `routine`, `budget`, `live` |
| `[daemon]` | `coalesce_seconds`, `coalesce_max_seconds`, `owner_coalesce_seconds`, `stale_after_minutes`, `held_alert_minutes`, `held_back_catch_up`, `peer_broadcast_reminder_minutes`, `presence_topics`, `host_wake_load_per_core` |
| `[linear_comments]`, `[linear_projects]`, `[notes_queue]`, `[listener_api]` | `enabled` |

A setting is named `table.key` or `seat.SEAT.key` and is set whole:  a `budget` or a `routine` is one value.  The editor changes settings;  it does not create a seat, and it does not remove a key.

Because a table is set whole, a new value that leaves out a sub-key the file has would drop it:  a cap, a pause (`wakes_per_day = 0`), a timeout.  That is never silent.  Each sub-key the new value drops must be named (`--remove seat.JET.budget.wakes_per_day`, or a `remove` list on standard input), or the request is refused (`would_remove_keys`).  The plan lists what goes as `removes`, and a name the request does not drop is refused too.

A setting is rewritten only where the file has it as a plain `key = value` line under its own `[table]` header, which is how the image's sample writes every one.  A file edited by hand can hold the same setting as a sub-table header (`[seat.JET.budget]`), a dotted key (`budget.wakes_per_day = 0`) or a quoted key.  The editor refuses that setting with `form_not_editable` and writes nothing.  The live file is not broken when it says so:  rewrite that one setting by hand as an inline value, and the editor takes it from then on.

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
6. The Eavesdropper is told to reload (SIGHUP) and its `status.json` is read back.  A refused reload, or one that could not be sent, puts the old bytes back the same atomic way and reloads again, so the file on disk and the config the Eavesdropper runs agree.  A file step that fails after the rename (the folder sync) is handled the same way.

Three outcomes of step 6 need reading with care:

- `failed` with `rolledBack: true`:  the old bytes are back and the Eavesdropper was told again.  The answer names the backup, which is kept.
- `failed` with `rolledBack: false` and `replaced: true`:  the old bytes could not be put back (a full disk, say).  The new file is in place, the Eavesdropper was not told again and still runs the old config, and the answer's `backup` names the file that holds the old bytes.  This is the one outcome where disk and running config differ, and it needs a person.
- `applied` with the reload code `reload_unconfirmed`:  the SIGHUP was sent, but `status.json` did not show a reload inside the thirty-second readback.  The change is on disk and stands.  Nothing has confirmed that the Eavesdropper runs it, so read `eavesdropper-status` before relying on it.

A `failed` answer with `replaced: false` and no backup means a file step failed before the live file was replaced, and the file is unchanged.

A plan does steps 2 to 4 with the candidate in a private temporary folder, and writes nothing beside the file.  It answers the settings that would change, the changed lines only (no context lines, so a pin or a variable name is never repeated), the check's verdict and a `planSHA256`.  An apply that names that hash does exactly what the plan showed, or is refused (`plan_mismatch`).

## The Other Writer Of The Same File

`scripts/simple_tools/server/apply_live_config.py --apply` also writes `/data/listener.toml`, and this change does not touch it.  It is run by hand inside the container when an image refresh changes the sample.  Two things follow, and both are open:

- **It overwrites seat sections whole.**  It copies the sample's `[seat.JET]` and every `[seat.GB-*]` and `[seat.BF-*]` section over the live one wherever they differ.  A setting this editor changed in one of those sections (a Grok Bot seat turned off with `enabled = false`, a budget) is put back to the sample's value by the next `--apply`, with no word about it beyond its own diff.  Read that diff before every `--apply`, and make a change that has to outlast an image refresh in the sample too.
- **It takes no lock.**  This editor locks the file's folder;  `apply_live_config.py` does not, so the two can interleave.  The editor's re-read just before its rename (`changed_underneath`) narrows the window but does not close it.  Do not run the two at the same time.

Whether `apply_live_config.py` should take the same folder lock, and whether it should keep the allowlisted settings of a section it replaces, is a question for the seat that owns that script, not something this change decides.

## Privilege And Audit

The container call is one fixed argv, run without a shell and without sudo:  `docker exec -i --user simpletools eavesdropper /usr/local/bin/python3 -I /app/scripts/simple_tools/server/eavesdropper_settings_edit.py --request-stdin`.  No setting, value or path from a request is a word of that argv.  The environment is the helper's pinned docker one, and docker's own words are only classified, never kept.  The file that is edited is the one the container's `SIMPLE_TOOLS_CONFIG` names;  a request cannot name a file.

An apply holds the helper's one action lock and writes one row to `ops-audit.jsonl` with the target `eavesdropper settings`.  The row names no setting and no value.  A get or a plan takes no lock and writes no row.

Anyone who can already run `docker exec` as that account can edit the file by hand.  The boundary for the ssh key is the forced command, the allowlist and the audit log, as for every other verb.

## Activation (Owner-Gated, Not Done By This Change)

1. **A ruling** that Muster may write the live `listener.toml` through the `fleet-ops-ctl` route, rather than a signed HTTPS endpoint.
2. **Install on the server**, as root, the way the eavesdropper-status verb was (`2026-11-07-eavesdropper-status-verb.md`, Deployment):  a hash-verified backup, then `fleet-ops-eavesdropper-settings.json` (root-owned, mode 0644) and the new `fleet-ops-control.py` into `/usr/local/lib/fleet-resource-control`.  The allowlist goes in first.  `scripts/hetzner/install-resource-control.sh` does not name the allowlist in its payload yet;  that is a follow-up.  Until this step, the server verb answers `unknown server verb`.
3. **Redeploy the Eavesdropper** from an image that holds this change, in a window the seat that runs the Eavesdropper agrees.  Until then the verb answers `failed` with `editor_unavailable`.
4. **Turn apply on**:  set `"applyEnabled": true` in the allowlist through a reviewed change, then install that file.  Until then every apply answers `refused` with `apply_disabled`.
5. **The first live apply**, on a setting that is safe to flip back, with the readback below.

Rollback of steps 2 to 4 restores the two backups.  Rollback of an apply is the backup the apply named, written back with a second apply or by hand, then `simple-chat daemon reload`.  Before step 5, read The Other Writer Of The Same File above:  an `apply_live_config.py --apply` after it can undo a seat setting.

## Checks

- `cd scripts && python3 -m unittest simple_tools.tests.test_eavesdropper_settings_edit -v`:  a key that is not allowlisted is refused, a pin or a secret-looking key is refused whatever the allowlist says, a failed check-config leaves the file byte for byte, a refused reload rolls back byte for byte, a file step that fails after the replace rolls back and names the backup, a setting written as a sub-table or a dotted key is refused as `form_not_editable`, a whole-table value never drops a sub-key unless the request names it, and the plan's output equals the applied diff.  One class runs the real `check-config` on the image's sample.
- `python3 scripts/fleet-ops-control.py eavesdropper-settings plan --set seat.ZC.enabled=false --file <a copy of the sample>` prints the changed lines and writes nothing.
- `python3 scripts/test_fleet_ops_control.py` and `python3 scripts/check-ci-runner-policy.py` still pass.
- After activation, from the Mac:  `ssh -n fleet-ops-ctl -- server eavesdropper-settings server --plan --json` answers `result: read` with the allowlisted settings, and after an apply the Eavesdropper's status shows every seat connected with `reload_refused` and `config_errors` empty.
