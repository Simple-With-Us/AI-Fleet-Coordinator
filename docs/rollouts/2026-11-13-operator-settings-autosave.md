# Operator Settings Autosave

Refs AFC-789.  The existing server editor becomes the one shared autosave writer for every exposed nonsecret Operator setting.  Infisical holds the canonical document; the listener TOML remains a necessary synchronized runtime copy and retains unrelated structure/comments.

## Wire Contract

The existing owner-authenticated forced-command route remains `fleet-ops-control.py server eavesdropper-settings server --yes`.  One request travels on standard input:

```json
{"action":"save","set":{"daemon.coalesce_seconds":20.0},"expectBaseSHA256":"<previous canonical revision>","requestID":"<fresh canonical UUID>","requestCreatedAt":1794556800000}
```

The ordinary `--plan` request with empty input is still GET.  A configured canonical GET returns `source=infisical`, `revision` and `baseSHA256`, the complete typed settings map, and separate runtime-file metadata.  Before a reader is configured, a legacy GET explicitly says `source=runtime-copy` and has false canonical persistence/readback flags; it is not an Infisical receipt.

The example creation time is illustrative; generate the current integer epoch milliseconds with each fresh UUID.  Retain that exact identity for one coalesced intent, including transport uncertainty.  Reusing an ID with a different typed patch, removals, base or creation time is refused.  A new intent uses a fresh ID even when it intentionally returns to a prior value.

Only `ok=true`, `result=saved`, `outcome=ok`, the exact original `requestID`, `autosaveEnabled=true`, `persisted=true`, `readback=true` and `runtimeApplied=true`, with matching returned requested values and a valid revision, permits Saved in the UI.  The reload receipt also carries the exact accepted canonical revision.  Source control, CLI exit status and a reload timestamp alone do not prove that receipt.

| Outcome | Canonical Persistence | Runtime | UI Meaning |
| --- | --- | --- | --- |
| `version_conflict` | No writer replay | Unchanged | Failed; read current revision |
| `readback_conflict` or `readback_failed` | Unverified outcome | No copy written | Failed; inspect current state before retry |
| `runtime_pending` with persistence/readback true | Exact canonical reread succeeded | Copy/reload incomplete | Failed with the canonical revision retained; do not replay blindly |
| `saved` with every flag true | Exact canonical reread succeeded | Matching revision accepted | Saved |

Reconcile through the same `--yes` route with only `action=reconcile`, `requestID` and `requestCreatedAt`; it never writes to Infisical, copies TOML or reloads the daemon.  A successful save reconciliation returns `action=reconcile`, `operation=save`, `result=saved` and the same complete proof.  Initialization reconciliation uses `operation=initialize`, `result=initialized` and keeps runtime/autosave false, preserving initialization before reader activation.

The existing protected Operator nonce ledger stores only fixed metadata and typed digests/revisions, never settings or credentials.  Terminal `ok`/`refused` receipts last 24 hours, capacity is 1,000 requests, and pending/unknown/replay records never expire or get evicted.  Old write identities are refused after 24 hours, so expiry cannot rearm a delayed retry.  All platforms share this one purpose and folder lock.  A known successful request later superseded by another edit remains terminal `outcome=ok`, but returns `ok=false` and `request_superseded`; it is not current Saved proof.

Before either provider mutation, a Sending checkpoint is durably committed under the folder lock.  Reconciliation of an absent ID writes a non-rearming cancellation tombstone, fencing a late original.  An abandoned Prepared record can likewise be cancelled because it never entered mutation.  Sending/persisted unknown outcomes stay held until exact target and accepted runtime proof; current base values or age never establish refusal.  Any unsettled request blocks all newer IDs, including no-op edits, until the original is terminal.  A new edit then needs a fresh GET and fresh identity.

Provider diagnostics contain only fixed codes, numeric status and retryability/possible-send flags.  A real HTTP 429 and an unavailable pre-write grant verification are definite unsent, retryable failures; permanent scope, permission and expiry refusals are not retryable.  Timeouts after mutation entry, HTTP 408/5xx and unknown transport outcomes remain possibly sent.  Retryability never rearms a terminal request ID or authorizes replaying a mutation; an explicit retry needs a fresh canonical GET and fresh identity.  The entire operation shares a 150-second I/O deadline below the existing 180-second helper limit, including both local checks and runtime reload; the provider phase receives a fresh bounded budget after local validation.

An early failure cannot report an unverified canonical source, revision or autosave capability.  If the journal has already durably refused the exact request ID, the editor returns `errorCode=request_refused`, `outcome=refused` and its fixed original `refusalCode`, without fetching a document after the failure.  The current UI retains its prior verified baseline, requires a fresh canonical GET and preserves drafts before allowing a new explicit intent; absent availability metadata is never a claim that the feature was disabled.  Pre-journal failures and possibly sent operations do not receive this terminal normalization.

The server folder lock and revision checks serialize editor clients.  They do not provide provider-level CAS against an unrelated direct Infisical writer.  A canonical reread mismatch is explicitly a conflict, never a patched cache success.

Whole `budget`, `routine` and `live` values retain the existing explicit-removal contract: a dropped subkey must also appear in `remove`, for example `"remove":["seat.GROK.budget.wakes_per_day"]`.  This distinguishes returning a cap to unset/unlimited from zero, which pauses wakes.  Missing, extra or unrelated removal names are refused.  A verified canonical document then owns those allowed whole-table values when synchronizing the runtime copy; unrelated settings and TOML structure remain intact.

## Editable Data And Fixed Status

Every field in the existing allowlist is editable: daemon timings, feature switches, presence pairs, seat routing, routine reference names/protocol/timing/cost, and budget/live tables.  Whole-table edits preserve siblings.  The daemon's own validation still refuses inconsistent candidates or reference names belonging to another seat.  Presence pairs preserve channel and topic case; optional `channelNames` choices come from the daemon's existing Zulip metadata cache, with no new credential read and no invented title-casing.  Dollar-cap persistence does not claim accounting enforcement that the current runtime lacks.

Seat instance/credential/bot/bearer/administrator pins and daemon owner/eligible-client/outgoing-webhook identity pins are internal fixed status, not fake editable controls.  Raw credential values, paths and endpoints never enter the document.

## Deployment Gates

1. Merge/review the shared client foundation and this backend source, and prepare/review the matching Muster UI while preserving its draft hold until step 5's backend live acceptance succeeds.  Ship the editor, canonical module, allowlist and `fleet_infisical.py` together; the container build context includes the manifest.
2. Read the current service-token registry and a names-only inventory of the proposed `prod:/operator-settings` folder.  Verify existing grants before asking for or issuing any new purpose.  No empty-folder or grant-compliance claim is made by this source.
3. Use this consumer's own finite-expiry read-only and read/write grants only.  The exact bindings are recorded in INFISICAL.md.  Do not borrow Notes, letter, seat, deployment or ambient credentials.  Store/token registration and actual installation are separate rollout work.
4. Install the reviewed source while retaining the working TOML source and leaving the new daemon reader binding inactive.  Verify the actual installed forced-command helper and schema, container module dependencies and persistent protected ledger, rather than inferring installation from a checkout.  The daemon must report a fresh, error-free snapshot revision for the existing TOML.  Use the existing forced-command route's explicit `initialize` action with the runtime-file hash and stable request identity, through this consumer's own writer binding.  It validates that accepted snapshot and the daemon checker, rechecks both after validation, reads the named key for absence, and makes one create-only POST.  It never PATCHes an existing or concurrently initialized document and never replays an uncertain create.  The exact canonical readback proves initialization; it does not activate a new runtime source.
5. Enable the daemon reader binding only after initialization and its own read-only grant are verified.  Verify GET reports the canonical source/revision, perform the authorized presence-channel casing canary below, and verify exact provider reread, runtime-file state and accepted daemon revision.  Verify stale revisions, invalid grants and a refused reload give the documented failed/partial outcomes.  This document does not claim any of those live actions occurred.

## Initialization And Casing Canary

The normal `scripts/hetzner/install-resource-control.sh` remains credential-free.  Its explicit local-only `--receive-operator-settings-bootstrap` mode delegates to the already installed helper, using the control account's database home.  Neither forced SSH route nor remote request JSON can select that mode.  It accepts one private stdin document with exactly `protocol=operator-settings-install/1`, a separately qualified immutable `imageID`, and `bootstrap` containing only `INFISICAL_OPERATOR_SETTINGS_PROJECT_ID` and `OPERATOR_SETTINGS_INFISICAL_WRITE_TOKEN`.  No value belongs in argv, a shell variable assignment, a message or a log.

The new own-purpose writer store is `~/.config/fleet-tools/operator-settings.env`, separate from the Coolify control credential.  Its folder is owner-0700 and its single-link file owner-0600, reached through protected nonsymlink directory descriptors.  Create-only publication never overwrites an existing or incomplete handoff.  The actual stored writer grant must pass the adjacent trusted canonical verifier for exact project, prod `/operator-settings`, read/write permissions and finite expiry no more than 90 days away.  Only then is `operator-settings-image.json` published with the qualified image ID and store inode.  Failure leaves no ready image metadata; preserve incomplete material for reconciliation rather than minting or overwriting again.

Writer requests check that metadata and the running image before opening the writer file, capture the full container ID and exec that same ID after a second identity check.  A replaced name, stopped container or rollback image fails closed.  Only `save`, `initialize` and `reconcile` use the private host-to-editor stdin envelope; the original UI wire stays unchanged, and no bootstrap enters its ledger, audit or signed response.  The existing editor clears private context on exit, and checker/reload children receive no settings writer token.

The read-only grant remains a separate runtime bootstrap binding.  Its exact existing private provisioning route and the new image must be qualified before issuance/activation; this implementation supplies no Coolify environment writer and does not escalate its read-only control token.  Never put RW in container configuration or the Mac UI.  Keep RO inactive through initialization, and remove the settings RO/project runtime binding before starting an incompatible rollback image.  These source changes install or issue nothing by themselves.

Run these source commands only from the reviewed deployed tool tree, after the preceding review, custody and grant gates.  Bindings arrive from the consumer's own approved bootstrap; these commands neither contain token values nor install them.  The JSON files contain only allowlisted nonsecret settings and belong in private rollout scratch, outside committed source.

First read the still-active TOML source through the same owner-authenticated editor route, then create the one canonical document from that accepted snapshot:

```sh
python3 scripts/fleet-ops-control.py server eavesdropper-settings server --plan --json </dev/null > operator-settings-before.json
python3 -c 'import json,time,uuid; d=json.load(open("operator-settings-before.json"))["eavesdropperSettings"]; assert d["source"] == "runtime-copy"; print(json.dumps({"action":"initialize","expectBaseSHA256":d["baseSHA256"],"requestID":str(uuid.uuid4()),"requestCreatedAt":int(time.time()*1000)}))' > operator-settings-initialize.json
python3 scripts/fleet-ops-control.py server eavesdropper-settings server --yes --json < operator-settings-initialize.json > operator-settings-initialized.json
python3 -c 'import json; r=json.load(open("operator-settings-initialize.json")); d=json.load(open("operator-settings-initialized.json"))["eavesdropperSettings"]; assert d["requestID"] == r["requestID"] and d["outcome"] == "ok"; assert d["ok"] and d["result"] == "initialized" and d["persisted"] and d["readback"] and not d["runtimeApplied"] and not d["autosaveEnabled"]; print("Canonical initialization read back; runtime source remains unchanged")'
```

After the separately gated reader installation/restart, read again.  Build a patch changing only the spelling of the three known presence channels to `Work-In-Progress`, `Builds` and `Alerts`, with every topic preserved byte for byte.  A pre-existing canonical spelling refuses the canary rather than claiming a no-op as live-write proof:

```sh
python3 scripts/fleet-ops-control.py server eavesdropper-settings server --plan --json </dev/null > operator-settings-canonical.json
python3 - <<'PY'
import json
import time
import uuid
d = json.load(open("operator-settings-canonical.json"))["eavesdropperSettings"]
assert d["source"] == "infisical" and d["persisted"] and d["readback"]
names = {"work-in-progress": "Work-In-Progress", "builds": "Builds", "alerts": "Alerts"}
old = d["settings"]["daemon.presence_topics"]
new = [[names.get(channel.casefold(), channel), topic] for channel, topic in old]
assert new != old, "No casing change remains; this is not a write canary"
assert [topic for _, topic in new] == [topic for _, topic in old]
request = {"action": "save", "set": {"daemon.presence_topics": new}, "expectBaseSHA256": d["revision"],
           "requestID": str(uuid.uuid4()), "requestCreatedAt": int(time.time() * 1000)}
with open("operator-settings-canary.json", "w") as handle:
    json.dump(request, handle)
PY
python3 scripts/fleet-ops-control.py server eavesdropper-settings server --yes --json < operator-settings-canary.json > operator-settings-canary-receipt.json
python3 scripts/fleet-ops-control.py server eavesdropper-settings server --plan --json </dev/null > operator-settings-after.json
python3 - <<'PY'
import hashlib
import json
before = json.load(open("operator-settings-canonical.json"))["eavesdropperSettings"]
request = json.load(open("operator-settings-canary.json"))
saved = json.load(open("operator-settings-canary-receipt.json"))["eavesdropperSettings"]
after = json.load(open("operator-settings-after.json"))["eavesdropperSettings"]
expected = dict(before["settings"], **request["set"])
expected_document = {"schemaVersion": 1, "settings": expected}
expected_revision = hashlib.sha256(json.dumps(expected_document, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
assert saved["ok"] and saved["result"] == "saved"
assert saved["requestID"] == request["requestID"] and saved["outcome"] == "ok"
assert all(saved[key] is True for key in ("persisted", "readback", "runtimeApplied", "autosaveEnabled"))
assert saved["source"] == after["source"] == "infisical"
assert saved["settings"] == after["settings"] == expected
assert saved["revision"] == after["revision"] == saved["reload"]["revision"] == expected_revision
assert saved["revision"] != before["revision"]
print("Verified canonical write, exact reread and matching daemon revision; only channel casing changed")
PY
```

The current presence matcher casefolds channel and topic when matching.  This spelling-only canary leaves routing semantics, topics, caps and spend unchanged.  Do not substitute another knob if no authorized casing change remains.  On a `runtime_pending`, timeout or readback conflict, retain the exact request file and reconcile its identity read-only before any new edit; a fresh GET alone never settles an unknown write:

```sh
python3 -c 'import json; r=json.load(open("operator-settings-canary.json")); print(json.dumps({"action":"reconcile","requestID":r["requestID"],"requestCreatedAt":r["requestCreatedAt"]}))' > operator-settings-reconcile.json
python3 scripts/fleet-ops-control.py server eavesdropper-settings server --yes --json < operator-settings-reconcile.json > operator-settings-reconciled.json
```

## Verified Runtime Copy

Only the active daemon holding its existing state-directory lock may record an accepted Infisical document.  Construction, check-config, refused reloads and waiting/competing instances do not write that copy.  Its metadata binds the exact project, `prod:/operator-settings`, key `OPERATOR_SETTINGS`, schema version and canonical revision to the document, plus a verification timestamp.  A fallback is explicitly `source=verified-copy`, may be at most 48 hours old, and rejects timestamps more than five minutes in the future.  A failed refresh never renews the timestamp.

The copy is a mode-0600, single-link regular file owned by the consumer in a protected directory.  Reads/writes traverse its parents by descriptors without following symbolic links, reject writable parents, and replace a protected temporary file atomically.  Wrong-home, wrong-schema, stale, linked or unsafe-custody copies are refused; none is described as current canonical state.

## Rollback

Preserve the working code/configuration until the replacement source and its own grant work.  Keep the previous image and pre-change TOML backup.  If runtime synchronization fails after canonical persistence, retain the returned revision and investigate before another write; the provider document remains authoritative.  A rollback of a canonical value is another explicitly fenced owner edit.  Reverting a TOML file alone does not override the canonical source on startup/reload.  Revoke a new binding only after its consumer is no longer using it; never silently fall back to another credential.
