---
name: service-tokens
description: >-
  Create and manage narrowly scoped Infisical service tokens for fleet consumers, store each value in its consumer's host secret store, verify the grant, register nonsecret metadata in the Fleet Reference sheet, and retire tokens safely.  Load before issuing, rotating, widening or revoking a service token, or editing its registry record.
---

# Service Tokens

> **Runtime fork (Grok).**  Mac Grok TUI / CLI is `[GROK]`, and so is Grok Build: one seat (owner 2026-10-08), and everything is signed `GROK`.  Its Zulip display name is `Grok`.  GROK followed by BUILD, any letter case, with any non-letter, non-digit symbols between or after (GROK-BUILD, Grok (Build), grok_build), is an alias for GROK (owner 2026-10-10): the tools accept it as a seat name and sign GROK, so do not write it as a tag, a branch prefix or a lane folder.  Old `grok-build/` branches stay readable.  Grok Bot (Cursor cloud) uses `[GB-<NAME>]` role tags, not this pack and not `[GROK-BOT]`.  Never `[MONET]`.


Give each consumer its own token scoped to exactly the project, environment and folder it needs, read-only unless write access is proven necessary, with an expiry.  Never create or use retired machine identities, borrow another consumer's token, or fall back to `fleet-secrets`.  If the issuer session, consumer, scope or approved registry access is uncertain, stop before issuance or write.

## Inventory Before Issuance

Establish the exact consumer and runtime location, Infisical project and project ID, one exact folder in `prod`, read/write need, expiry, purpose, owner and secure-storage destination.  Check the Fleet Reference registry for an exact existing entry before minting.  Folder scope is exact: do not assume a parent grants descendants or `/` means every folder.  Never widen a token shared by another consumer.  Review the names already in the proposed folder: an exact folder grant may still expose credentials this consumer does not need.  Use named reads in the consumer, rather than exporting the folder.  If unrelated credentials share that folder, obtain the required authority for a coordinated narrower folder migration; preserve existing consumers and values until replacement acceptance.

## Issue With The Authorized Infisical User Session

Use the existing authorized Infisical user CLI issuer session.  The Mac GUI login/keychain may provide the user's encryption credentials; a server session may lack them.  Missing user credentials are a stop condition, never a reason to try machine auth or another person's session.

Check `infisical service-token create --help` in that session.  Pass explicit profile and project selections, exact environment/folder scope, access level, expiry, and a consumer-specific name.  Use only flags confirmed by the installed CLI.  This example captures both streams privately before invocation; it never displays a value:

```sh
set +x
umask 077
stdout_file="$(mktemp "${TMPDIR:-/tmp}/infisical-token.stdout.XXXXXX")"
stderr_file="$(mktemp "${TMPDIR:-/tmp}/infisical-token.stderr.XXXXXX")"
chmod 600 "$stdout_file" "$stderr_file"
if infisical service-token create \
  --profile "<authorized-user-profile>" \
  --projectId "<approved-project-id>" \
  --scope "prod:<exact-folder>" \
  --access-level read \
  --expiry-seconds "<approved-seconds>" \
  --name "<consumer>-ro" \
  --token-only >"$stdout_file" 2>"$stderr_file"; then
  issue_status=0
else
  issue_status=$?
  printf '%s\n' 'Issuance outcome unknown; preserve private captures and reconcile before retrying.' >&2
  exit "$issue_status"
fi
```

Keep shell tracing off.  Never print either capture, copy its value to a command argument, broad shell export or diagnostic dump, or let it reach an error, transcript, metadata file, source or registry.  Process-local variables and a narrowly scoped child-process environment binding are allowed when the consumer requires them; do not persist or log them.  After confirmed installation and verification, delete both captures.  If issuance is ambiguous, preserve them and reconcile with the authorized issuer before any retry.  If the value reaches a transcript or log, stop, report it and arrange authorized revocation before issuing a replacement.

## Verify The Issued Grant

Use the newly issued token as a Bearer credential to call `GET /api/v2/service-token` (no token ID in the path).  Parse the response in memory and retain only these nonsecret fields: `id`, `name`, `projectId` or `workspace`, `permissions`, `scopes`, `createdAt` and `expiresAt`.  Confirm returned project, environment, exact folder scopes, permissions and expiry match the inventory.  Record verification time and evidence summary; do not infer actual scope from requested CLI options.

The complete CLI value may have the legacy four-part form `st.<id>.<secret>.<encryption-key>`.  For the metadata request's Bearer header, remove only the final encryption-key segment to form `st.<id>.<secret>`.  Preserve the complete CLI value when installing it for the consumer.  Never print the response or retain unallowlisted fields.

A successful in-scope read is positive evidence that access works.  A zero-item result or HTTP 200 outside the requested path does not prove permission denial; the raw endpoint may project the allowed scope regardless of the requested path.  Use a known outside-key control only if the authorized issuer confirms it is available independently and its use is approved.  Otherwise record that the boundary is unverified.  Claim a 403 only when one was actually observed.  Do not probe arbitrary keys or issue a write probe for a read-only grant.

## Store The Value On The Consumer Host

The value lives only in the consumer host's approved secret store.

- On the Mac, use the owner's login keychain, one generic-password item per token.  Set service to `infisical-service-token` and account to the registry token name.  A reader can retrieve it with `security find-generic-password -s infisical-service-token -a <name> -w`.
- A specifically owner-approved Mac file store is an exception for the named consumer, path and grant only: consumer-owned 0600 file, parent 0700.  Preserve an approved working wrapper until reviewed source and actual consumer acceptance justify replacing it; do not extend the exception to other consumers.
- On a server or container, use that consumer host's approved store.  A file owned by the consumer account with mode 0600 inside a parent directory mode 0700 is an allowed explicit choice when that host's policy permits it; Infisical or another approved host store may also be used.  A server consumer never reaches for the Mac keychain; a Mac consumer never reaches for a server-side file.
- The metadata sidecar `~/.config/infisical/<consumer>.json` preserves token ID, project, environment, folder, access level, `createdAt` and expiry, never the value.  Keep the original creation timestamp through status updates and mirror synchronization.
- Never pass the token as a command-line argument.  Read it from the store inside the process that uses it; pass a Mac keychain write over stdin.
- Without that explicit store exception, a 0600 token file under `~/.config/infisical/` is staging only and must be deleted after installation.  A file in a repository is a published secret when pushed.
- The Mac login keychain is reachable only from the owner's GUI session.  If a consumer cannot access its approved store, fail closed and identify the missing item or scope.

## Register Metadata In The Fleet Reference Sheet

The source is <https://fleetlink.online/references>, tab `Infisical Service Tokens`.  Use only the shared Fleet-OPS module `scripts/fleet-sheet/sa.mjs` and its exported `api(path, init)`.  The module pins the private workbook configuration, reads `GOOGLE_SHEETS_SA` in memory from Infisical Fleet-OPS prod `/google-sheets-sa`, and handles Google Sheets authentication.  Normal use relies on the approved prebound `INFISICAL_TOKEN` and explicit Fleet-OPS project selection.  If either is missing, stop; do not mint or borrow another consumer's token for the sheet client.  An authorized GUI issuer operation may bootstrap the approved binding, never machine auth or another identity.  Do not print or write `GOOGLE_SHEETS_SA` JSON to disk or create a competing client.

Save only the nonsecret token metadata below as JSON.  Designate one registrar for this token across all hosts in its existing work topic before running the recipe, and preserve that ownership through reconciliation.  From an authorized Fleet-OPS checkout, that registrar runs the following with the metadata file as its sole argument.  It reads the live header, maps exact column names, checks ID and stable content, appends once, and fully reads back `updatedRange` before claiming success.  It prints only fixed outcomes and allowlisted variable, source or column names, never metadata values or provider errors.

Required metadata JSON fields: `id`, `name`, `project`, `projectId`, `scopes` (exactly one `{environment, folder, permissions}` for one folder in `prod`, with unique `read` and/or `write` permissions), `purpose`, `owner`, `consumer`, `status`, `createdAt`, `expiresAt`, `verifiedAt`, `evidence` and `secureStorageReference`.  Optional fields are `auditCoverage`, `screenshotComparison`, `discrepancyEvidenceLimits` and `createdBy`; omitted values become empty cells.  The current 19-column sheet has no creation-timestamp column: retain `createdAt` in the nonsecret sidecar.  The private mirror schema accepts an optional `createdAt` from verified Infisical metadata, but a new row derived only from the sheet does not supply it.  Do not claim that sheet synchronization captured the creation time.  Do not add a remote column as part of registration, include secret response fields, or flatten several grants into one row.

Keep the sidecar private.  The registrar's existing `~/.config/infisical` directory must be owned by its account with mode 0700; the recipe refuses a missing, symlinked or less private directory.  Its `registry-attempts` subdirectory contains only local attempt outcomes keyed by a token-ID digest, never token values or token-value hashes.

```sh
export FLEET_OPS_ROOT="$(pwd -P)"  # Run from the owned Fleet-OPS checkout root.
metadata_file="$HOME/.config/infisical/service-token-metadata.json"
node --input-type=module - "$metadata_file" <<'JS'
import { open, mkdir, lstat, rename, unlink, rmdir } from 'node:fs/promises';
import { constants } from 'node:fs';
import { createHash, randomUUID } from 'node:crypto';
import { homedir } from 'node:os';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';

const MAX_BYTES = 64 * 1024;
const safeErrors = new WeakMap();
const refuse = (code, source) => {
  const error = new Error();
  safeErrors.set(error, { code, source });
  throw error;
};
const boundedJSON = async (path, source) => {
  const file = await open(path, constants.O_RDONLY | constants.O_NOFOLLOW);
  try {
    if (!(await file.stat()).isFile()) refuse('invalid_metadata', source);
    const raw = Buffer.alloc(MAX_BYTES + 1);
    let used = 0;
    while (used < raw.length) {
      const { bytesRead } = await file.read(raw, used, raw.length - used, null);
      if (!bytesRead) break;
      used += bytesRead;
    }
    if (used > MAX_BYTES) refuse('metadata_too_large', source);
    const value = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(raw.subarray(0, used)));
    const stack = [[value, 0]];
    let nodes = 0;
    while (stack.length) {
      const [item, depth] = stack.pop();
      if (++nodes > 1000 || depth > 16) refuse('metadata_complexity', source);
      if (item && typeof item === 'object') {
        for (const child of Object.values(item)) stack.push([child, depth + 1]);
      }
    }
    return value;
  } finally { await file.close(); }
};
let phase = 'pre-append';
let source = 'configuration';
let lock;
let lockOwned = false;
let stateDir;
let marker;
let attempt;
const saveAttempt = async outcome => {
  const temp = marker + '.' + randomUUID();
  const file = await open(temp, 'wx', 0o600);
  try {
    await file.writeFile(JSON.stringify({ version: 1, outcome }));
    await file.sync();
  } finally { await file.close(); }
  try {
    await rename(temp, marker);
    const directory = await open(stateDir, 'r');
    try { await directory.sync(); } finally { await directory.close(); }
  } finally { await unlink(temp).catch(() => {}); }
};
try {
  const root = process.env.FLEET_OPS_ROOT;
  if (!root) refuse('missing_configuration', 'FLEET_OPS_ROOT');
  if (process.argv.length !== 3) refuse('invalid_arguments', 'metadata-file');
  for (const name of ['INFISICAL_TOKEN', 'INFISICAL_FLEET_OPS_PROJECT_ID']) {
    if (!process.env[name]?.trim()) refuse('missing_configuration', name);
  }

  source = 'metadata-file';
  const input = await boundedJSON(process.argv[2], source);
  const required = ['id', 'name', 'project', 'projectId', 'scopes', 'purpose', 'owner', 'consumer', 'status', 'createdAt', 'expiresAt', 'verifiedAt', 'evidence', 'secureStorageReference'];
  const optional = ['auditCoverage', 'screenshotComparison', 'discrepancyEvidenceLimits', 'createdBy'];
  if (!input || typeof input !== 'object' || Array.isArray(input)) refuse('invalid_metadata', source);
  if (Object.keys(input).some(key => !required.includes(key) && !optional.includes(key))) refuse('invalid_metadata', source);
  for (const key of optional) input[key] ??= '';
  if (required.filter(key => key !== 'scopes').some(key => typeof input[key] !== 'string' || !input[key].trim())) refuse('invalid_metadata', source);
  if (optional.some(key => typeof input[key] !== 'string')) refuse('invalid_metadata', source);
  if (!Array.isArray(input.scopes) || input.scopes.length !== 1) refuse('one_scope_required', source);
  const scope = input.scopes[0];
  if (!scope || typeof scope !== 'object' || Array.isArray(scope) || Object.keys(scope).some(key => !['environment', 'folder', 'permissions'].includes(key))) refuse('invalid_scope', source);
  if (scope.environment !== 'prod' || typeof scope.folder !== 'string' || !scope.folder.startsWith('/') || /[\s,]/.test(scope.folder)) refuse('invalid_scope', source);
  if (!Array.isArray(scope.permissions) || !scope.permissions.length || scope.permissions.some(value => !['read', 'write'].includes(value)) || new Set(scope.permissions).size !== scope.permissions.length) refuse('invalid_permission', source);
  for (const key of ['createdAt', 'expiresAt', 'verifiedAt']) {
    if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,3})?(?:Z|[+-]\d{2}:\d{2})$/.test(input[key]) || !Number.isFinite(Date.parse(input[key]))) refuse('invalid_timestamp', source);
  }
  delete process.env.GOOGLE_SHEETS_SA; // Require the shared client's Infisical read.
  source = 'Fleet-OPS/scripts/fleet-sheet/sa.mjs';
  const { api } = await import(pathToFileURL(join(root, 'scripts/fleet-sheet/sa.mjs')).href);
  const sheet = 'Infisical Service Tokens';
  const readPath = range => '/values/' + encodeURIComponent(range) + '?majorDimension=ROWS';
  const read = range => api(readPath(range));
  source = 'attempt-state';
  const stateParent = join(homedir(), '.config/infisical');
  const parentInfo = await lstat(stateParent);
  if (!parentInfo.isDirectory() || parentInfo.uid !== process.getuid() || (parentInfo.mode & 0o777) !== 0o700) refuse('unsafe_state_directory', source);
  stateDir = join(stateParent, 'registry-attempts');
  try { await mkdir(stateDir, { mode: 0o700 }); }
  catch (error) { if (error.code !== 'EEXIST') throw error; }
  const stateInfo = await lstat(stateDir);
  if (!stateInfo.isDirectory() || stateInfo.uid !== process.getuid() || (stateInfo.mode & 0o777) !== 0o700) refuse('unsafe_state_directory', source);
  const parentDirectory = await open(stateParent, 'r');
  try { await parentDirectory.sync(); } finally { await parentDirectory.close(); }
  const key = createHash('sha256').update(input.id).digest('hex'); // Token ID, never token value.
  marker = join(stateDir, key + '.json');
  lock = join(stateDir, key + '.lock');
  try { await mkdir(lock, { mode: 0o700 }); lockOwned = true; }
  catch { refuse('local_lock_unavailable', source); }
  try {
    const info = await lstat(marker);
    if (!info.isFile() || info.uid !== process.getuid() || (info.mode & 0o777) !== 0o600) refuse('unsafe_attempt_state', source);
    attempt = await boundedJSON(marker, source);
    if (attempt?.version !== 1 || !['ok', 'unknown'].includes(attempt.outcome)) refuse('invalid_attempt_state', source);
  } catch (error) { if (error.code !== 'ENOENT') throw error; }
  source = 'Infisical Service Tokens/read';
  const current = await read(sheet);
  const [headers, ...rows] = current.values ??  [];
  const needed = ['Token', 'Token ID', 'Project', 'Project ID', 'Environment', 'Folders', 'Permission', 'Purpose', 'Owner', 'Runtime consumer', 'Status', 'Expires (UTC)', 'Metadata verified (UTC)', 'Evidence', 'Audit coverage', 'Secure storage reference', 'Screenshot comparison 2026-11-03', 'Discrepancy / evidence limits', 'Created By'];
  if (!headers || headers.length !== needed.length || needed.some(name => headers.filter(value => value === name).length !== 1)) throw new Error();
  const values = {
    'Token': input.name, 'Token ID': input.id, 'Project': input.project, 'Project ID': input.projectId,
    'Environment': scope.environment,
    'Folders': input.scopes.map(value => value.folder).join('\n'),
    'Permission': scope.permissions.length === 2 ? 'read/write' : scope.permissions[0],
    'Purpose': input.purpose, 'Owner': input.owner, 'Runtime consumer': input.consumer,
    'Status': input.status, 'Expires (UTC)': input.expiresAt, 'Metadata verified (UTC)': input.verifiedAt,
    'Evidence': input.evidence, 'Audit coverage': input.auditCoverage,
    'Secure storage reference': input.secureStorageReference,
    'Screenshot comparison 2026-11-03': input.screenshotComparison,
    'Discrepancy / evidence limits': input.discrepancyEvidenceLimits, 'Created By': input.createdBy,
  };
  const row = headers.map(header => values[header] ??  '');
  const secretPattern = /(?:\bst\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+|\bgh[pousr]_[A-Za-z0-9_]+|\bgithub_pat_[A-Za-z0-9_]+|\bsk-(?:proj-|ant-)?[A-Za-z0-9_-]+|\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]+|\bxox[baprs]-[A-Za-z0-9-]+|\b(?:AKIA|ASIA)[A-Z0-9]{16}\b|\bAIza[A-Za-z0-9_-]{30,}|\beyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+|-----BEGIN [A-Z ]*PRIVATE KEY-----|"(?:private_key|encryptedKey|secret|token|iv|tag)"\s*:)/i;
  for (let index = 0; index < row.length; index++) {
    if (typeof row[index] !== 'string' || secretPattern.test(row[index])) refuse('secret_like_metadata', headers[index]);
  }
  const idColumn = headers.indexOf('Token ID');
  const matches = rows.filter(existingRow => existingRow[idColumn] === input.id);
  if (matches.length > 1) throw new Error();
  const existing = matches[0];
  if (!existing) {
    const stable = ['Token', 'Project ID', 'Environment', 'Folders', 'Runtime consumer'];
    if (rows.some(candidate => stable.every(header => String(candidate[headers.indexOf(header)] ?? '') === values[header]))) refuse('content_match_requires_reconciliation', 'Infisical Service Tokens');
  }
  if (existing) {
    if (needed.some(header => String(existing[headers.indexOf(header)] ?? '') !== String(row[headers.indexOf(header)] ?? ''))) {
      process.stdout.write('Token ID exists with different metadata; stop and reconcile.\n');
      process.exitCode = 2;
    } else {
      if (attempt) await saveAttempt('ok'); // Exact-row reconciliation sends no mutation.
      process.stdout.write('Token ID already registered; no row added.\n');
    }
  } else {
    if (attempt) {
      process.stdout.write('outcome_unknown phase=pre-append source=attempt-state; reconcile without appending.\n');
      process.exitCode = 3;
      // The persistent marker blocks replay even when no row is visible yet.
    } else {
      source = 'attempt-state';
      await saveAttempt('unknown'); // Durable before the first possibly sent mutation.
      phase = 'mutation-started';
      source = 'Infisical Service Tokens/append';
      const appended = await api('/values/' + encodeURIComponent(sheet + '!A:S') + ':append?valueInputOption=RAW&insertDataOption=INSERT_ROWS', {
        method: 'POST', body: JSON.stringify({ majorDimension: 'ROWS', values: [row] }),
      });
      const range = appended.updates?.updatedRange;
      const matchedRange = typeof range === 'string' && /^'?Infisical Service Tokens'?!A([1-9]\d*):S\1$/.exec(range);
      if (appended.updates?.updatedRows !== 1 || !matchedRange || Number(matchedRange[1]) < 2) throw new Error();
      phase = 'post-append';
      source = 'Infisical Service Tokens/readback';
      const readback = await read(range);
      const actual = readback.values ?? [];
      if (actual.length !== 1 || !Array.isArray(actual[0]) || actual[0].length > row.length) throw new Error();
      const normalized = Array.from({ length: row.length }, (_, index) => actual[0][index] ?? '');
      if (JSON.stringify([normalized]) !== JSON.stringify([row])) throw new Error();
      source = 'Infisical Service Tokens/final-readback';
      const finalTab = await read(sheet);
      const finalValues = finalTab.values;
      if (!Array.isArray(finalValues) || !Array.isArray(finalValues[0]) || JSON.stringify(finalValues[0]) !== JSON.stringify(headers)) throw new Error();
      const finalRows = finalValues.slice(1);
      if (finalRows.some(entry => !Array.isArray(entry) || entry.length > row.length)) throw new Error();
      const finalMatches = finalRows.filter(entry => entry[idColumn] === input.id);
      if (finalMatches.length !== 1) throw new Error();
      const confirmed = Array.from({ length: row.length }, (_, index) => finalMatches[0][index] ?? '');
      if (JSON.stringify(confirmed) !== JSON.stringify(row)) throw new Error();
      await saveAttempt('ok'); // Exact range and unique full-tab readback commit success.
      process.stdout.write('Registry row appended and read back exactly.\n');
    }
  }
} catch (error) {
  // The shared api throws provider text, not trusted status/send metadata. Never echo it.
  const safe = safeErrors.get(error) ?? { code: 'source_unavailable', source };
  const unknown = phase !== 'pre-append' || attempt?.outcome === 'unknown';
  process.stderr.write(`${unknown ? 'outcome_unknown' : safe.code} phase=${phase} source=${safe.source}; stop and reconcile before retrying.\n`);
  process.exitCode = unknown ? 3 : 1;
} finally {
  if (lockOwned) {
    try { await rmdir(lock); }
    catch { process.stderr.write('local_lock_cleanup_failed source=attempt-state\n'); process.exitCode ||= 1; }
  }
}
JS
```

The local lock and fsynced attempt marker serialize this host and prevent its crash/retry from blindly appending.  They do not coordinate other hosts or replace the designated registrar.  Google Sheets has no token-ID uniqueness constraint; this recipe is not a shared transactional idempotency service.  The final full-tab read checks unchanged headers, one exact ID match and all 19 cells before recording success; it detects observed duplicates without making the read atomic with another host’s writes.  Do not run another host's registrar or delete its markers to retry.  An existing `unknown` or `ok` marker with no exact matching row blocks mutation; a full exact row match can reconcile it to `ok` using reads only.  After a crash, retain the lock and marker until the designated registrar establishes that the original invocation has ended and reconciles the live rows; no age-based unlocking or automatic unknown replay.

Every exception after mutation starts, including authentication, timeout, response parsing, readback or local completion-record failure, is conservatively `outcome_unknown` with exit 3.  The shared client supplies no trustworthy structured status or sent flag, so do not infer a definite refusal from its error text or release the attempt for a blind retry.  Pre-append failures exit 1; an existing ID with different metadata exits 2.  No provider body is printed.  The exact headers are checked on every run; metadata only is allowed, never token value, value hash, `encryptedKey`, `iv`, `tag` or service-account JSON.  Pattern scanning supplements allowlisted metadata and private review; it cannot prove arbitrary text secret-free.

A validation refusal before mutation creates no new attempt marker and leaves any existing marker intact.  Local outcomes do not prove a provider refusal and are not a cross-host idempotency ledger.

The same designated registrar owns approved same-ID status changes.  This append recipe deliberately refuses different metadata on an existing ID.  For a sanctioned update, read the entire live tab, require exactly one Token ID match, capture its exact 19-cell before-image privately, and establish the approved field changes from observed revocation evidence.  Re-read that row immediately before updating and stop if any cell changed.  Through the same shared `api`, update only its exact row range with `PUT` and `valueInputOption=RAW`; preserve ID, creation sidecar and unaffected cells.  Apply the same column mapping, secret scan and persistent unknown-before-mutation guard, fully read back all 19 cells, and mark `ok` only on exact equality.  Preserve registrar ownership and an `unknown` operation record on any ambiguous result; reconcile by ID and full row without replay.  This is a supervised writer procedure, not an additional runnable updater or a compare-and-swap guarantee; use an existing reviewed updater if available, otherwise prepare and review the specific update before execution.

After verifying the sheet row, the Fleet-OPS metadata mirror may be synchronized from an owned Fleet-OPS lane with `node scripts/infisical-service-tokens/sync-from-sheet.mjs --write`.  Inspect the diff and confirm only the matching metadata changed.  The sheet remains the source of truth; never hand-edit the mirror as a substitute.

## Rotate, Revoke And Close Out

Rotation is not a metadata edit.  Issue the replacement with the intended scope and fresh expiry, install it, verify its grant and store, and register the new metadata row.  Keep the old grant live until the actual consumer successfully reads through the replacement; source edits or a stored file alone are insufficient.  After the required owner authorization and consumer acceptance, revoke the old grant through the authorized user session, verify the result, then mark its registry row revoked.  An uncertain revoke result must be reconciled before any retry or revoked-status claim.  Never revoke or broaden a token based on a metadata row alone.

A missing item, locked keychain, unreadable store, refused API call or uncertain write means stop and report the observed boundary.  Do not retry as another identity or silently change stores.  A token is complete when the consumer can read its own store entry without printing it, its exact metadata is verified, the sheet row is confirmed by readback, and any mirror sync is verified.  Record outside-scope uncertainty accurately rather than describing it as a denial.

## Related

- `secret-handoff` for moving a credential to its consumer.
- `secret-safety` for handling any secret value.
- `housekeeper` for a consumer that posts to a webhook from the Mac.
