---
name: service-tokens
description: >-
  Create and manage narrowly scoped Infisical service tokens for fleet consumers, store each value in its consumer's host secret store, verify the grant, register nonsecret metadata in the Fleet Reference sheet, and retire tokens safely.  Load before issuing, rotating, widening or revoking a service token, or editing its registry record.
---

# Service Tokens

> **Runtime fork (Grok).**  Mac Grok TUI / CLI is `[GROK]`, and so is Grok Build: one seat (owner 2026-10-08), and everything is signed `GROK`.  Its Zulip display name is `Grok`.  GROK followed by BUILD, any letter case, with any non-letter, non-digit symbols between or after (GROK-BUILD, Grok (Build), grok_build), is an alias for GROK (owner 2026-10-10): the tools accept it as a seat name and sign GROK, so do not write it as a tag, a branch prefix or a lane folder.  Old `grok-build/` branches stay readable.  Grok Bot (Cursor cloud) uses `[GB-<NAME>]` role tags, not this pack and not `[GROK-BOT]`.  Never `[MONET]`.


Give each consumer its own token scoped to exactly the project, environment and folder it needs, read-only unless write access is proven necessary, with an expiry.  Never create or use retired machine identities, borrow another consumer's token, or fall back to `fleet-secrets`.  If the issuer session, consumer, scope or approved registry access is uncertain, stop before issuance or write.

## Inventory before issuance

Establish the exact consumer and runtime location, Infisical project and project ID, environment, folder path(s), read/write need, expiry, purpose, owner and secure-storage destination.  Check the Fleet Reference registry for an exact existing entry before minting.  Folder scope is exact: do not assume a parent grants descendants or `/` means every folder.  Never widen a token shared by another consumer.

## Issue with the authorized Infisical user session

Use the existing authorized Infisical user CLI issuer session.  The Mac GUI login/keychain may provide the user's encryption credentials; a server session may lack them.  Missing user credentials are a stop condition, never a reason to try machine auth or another person's session.

Check `infisical service-token create --help` in that session.  Pass explicit profile and project selections, exact environment/folder scope, access level, expiry, and a consumer-specific name.  Use only flags confirmed by the installed CLI.  This example captures both streams privately before invocation; it never displays a value:

```sh
umask 077
stdout_file="$(mktemp "${TMPDIR:-/tmp}/infisical-token.stdout.XXXXXX")"
stderr_file="$(mktemp "${TMPDIR:-/tmp}/infisical-token.stderr.XXXXXX")"
chmod 600 "$stdout_file" "$stderr_file"
if infisical service-token create \
  --profile "<authorized-user-profile>" \
  --projectId "<approved-project-id>" \
  --scope "<environment>:<exact-folder>" \
  --access-level read \
  --expiry-seconds "<approved-seconds>" \
  --name "<consumer>-ro" \
  --token-only >"$stdout_file" 2>"$stderr_file"; then
  issue_status=0
else
  issue_status=$?
fi
```

Keep shell tracing off.  Never print either capture, copy its value to a command argument, broad shell export or diagnostic dump, or let it reach an error, transcript, metadata file, source or registry.  Process-local variables and a narrowly scoped child-process environment binding are allowed when the consumer requires them; do not persist or log them.  After confirmed installation and verification, delete both captures.  If issuance is ambiguous, preserve them and reconcile with the authorized issuer before any retry.  If the value reaches a transcript or log, stop, report it and arrange authorized revocation before issuing a replacement.

## Verify the issued grant

Use the newly issued token as a Bearer credential to call `GET /api/v2/service-token` (no token ID in the path).  Parse the response in memory and retain only these nonsecret fields: `id`, `name`, `projectId` or `workspace`, `permissions`, `scopes`, `createdAt` and `expiresAt`.  Confirm returned project, environment, exact folder scopes, permissions and expiry match the inventory.  Record verification time and evidence summary; do not infer actual scope from requested CLI options.

The complete CLI value may have the legacy four-part form `st.<id>.<secret>.<encryption-key>`.  For the metadata request's Bearer header, remove only the final encryption-key segment to form `st.<id>.<secret>`.  Preserve the complete CLI value when installing it for the consumer.  Never print the response or retain unallowlisted fields.

A successful in-scope read is positive evidence that access works.  A zero-item result or HTTP 200 outside the requested path does not prove permission denial; the raw endpoint may project the allowed scope regardless of the requested path.  Use a known outside-key control only if the authorized issuer confirms it is available independently and its use is approved.  Otherwise record that the boundary is unverified.  Claim a 403 only when one was actually observed.  Do not probe arbitrary keys or issue a write probe for a read-only grant.

## Store the value on the consumer host

The value lives only in the consumer host's approved secret store.

- On the Mac, use the owner's login keychain, one generic-password item per token.  Set service to `infisical-service-token` and account to the registry token name.  A reader can retrieve it with `security find-generic-password -s infisical-service-token -a <name> -w`.
- On a server or container, use that consumer host's approved store.  A file owned by the consumer account with mode 0600 inside a parent directory mode 0700 is an allowed explicit choice when that host's policy permits it; Infisical or another approved host store may also be used.  A server consumer never reaches for the Mac keychain; a Mac consumer never reaches for a server-side file.
- The metadata sidecar `~/.config/infisical/<consumer>.json` may contain token ID, project, environment, folder, access level and expiry, never the value.
- Never pass the token as a command-line argument.  Read it from the store inside the process that uses it; pass a Mac keychain write over stdin.
- A 0600 file under `~/.config/infisical/` is staging only and must be deleted after installation.  A file in a repository is a published secret when pushed.
- The Mac login keychain is reachable only from the owner's GUI session.  If a consumer cannot access its approved store, fail closed and identify the missing item or scope.

## Register metadata in the Fleet Reference sheet

The source is <https://fleetlink.online/references>, tab `Infisical Service Tokens`.  Use only the shared Fleet-OPS module `scripts/fleet-sheet/sa.mjs` and its exported `api(path, init)`.  The module pins the private workbook configuration, reads `GOOGLE_SHEETS_SA` in memory from Infisical Fleet-OPS prod `/google-sheets-sa`, and handles Google Sheets authentication.  Normal use relies on the approved prebound `INFISICAL_TOKEN` and explicit Fleet-OPS project selection.  If either is missing, stop; do not mint or borrow another consumer's token for the sheet client.  An authorized GUI issuer operation may bootstrap the approved binding, never machine auth or another identity.  Do not print or write `GOOGLE_SHEETS_SA` JSON to disk or create a competing client.

Save only the nonsecret token metadata below as JSON.  From an authorized Fleet-OPS checkout, run the following with that metadata file as its sole argument.  The example reads the live header, maps by exact column names, checks for an existing token ID, appends once, and fully reads back `updatedRange` before claiming success.  It prints no metadata, values, errors or provider responses.

Required metadata JSON fields: `id`, `name`, `project`, `projectId`, `scopes` (array of `{environment, folder, permissions}`, where `permissions` is a nonempty array), `purpose`, `owner`, `consumer`, `status`, `createdAt`, `expiresAt`, `verifiedAt`, `evidence` and `secureStorageReference`.  Optional fields are `auditCoverage`, `screenshotComparison`, `discrepancyEvidenceLimits` and `createdBy`; omitted values become empty cells.  Do not include the token value or secret response fields.

```sh
export FLEET_OPS_ROOT="$(pwd -P)"  # Run from the owned Fleet-OPS checkout root.
metadata_file="$HOME/.config/infisical/service-token-metadata.json"
node --input-type=module - "$metadata_file" <<'JS'
import { readFile } from 'node:fs/promises';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';

const fail = () => { process.exitCode = 1; };
try {
  const root = process.env.FLEET_OPS_ROOT;
  if (!root || process.argv.length !== 3) throw new Error();
  if (!process.env.INFISICAL_TOKEN || !process.env.INFISICAL_FLEET_OPS_PROJECT_ID) throw new Error();

  const input = JSON.parse(await readFile(process.argv[2], 'utf8'));
  const required = ['id', 'name', 'project', 'projectId', 'scopes', 'purpose', 'owner', 'consumer', 'status', 'createdAt', 'expiresAt', 'verifiedAt', 'evidence', 'secureStorageReference'];
  const optional = ['auditCoverage', 'screenshotComparison', 'discrepancyEvidenceLimits', 'createdBy'];
  if (Object.keys(input).some(key => !required.includes(key) && !optional.includes(key))) throw new Error();
  for (const key of optional) input[key] ??= '';
  if (required.filter(key => key !== 'scopes').some(key => typeof input[key] !== 'string' || !input[key].trim())) throw new Error();
  if (optional.some(key => typeof input[key] !== 'string')) throw new Error();
  if (!Array.isArray(input.scopes) || input.scopes.length === 0) throw new Error();
  if (input.scopes.some(scope => typeof scope.environment !== 'string' || typeof scope.folder !== 'string' || !scope.environment || !scope.folder || !Array.isArray(scope.permissions) || scope.permissions.length === 0 || scope.permissions.some(value => typeof value !== 'string' || !value))) throw new Error();
  delete process.env.GOOGLE_SHEETS_SA; // Require the shared client's Infisical read.
  const { api } = await import(pathToFileURL(join(root, 'scripts/fleet-sheet/sa.mjs')).href);
  const sheet = 'Infisical Service Tokens';
  const readPath = range => '/values/' + encodeURIComponent(range) + '?majorDimension=ROWS';
  const read = range => api(readPath(range));
  const current = await read(sheet);
  const [headers, ...rows] = current.values ??  [];
  const needed = ['Token', 'Token ID', 'Project', 'Project ID', 'Environment', 'Folders', 'Permission', 'Purpose', 'Owner', 'Runtime consumer', 'Status', 'Expires (UTC)', 'Metadata verified (UTC)', 'Evidence', 'Audit coverage', 'Secure storage reference', 'Screenshot comparison 2026-11-03', 'Discrepancy / evidence limits', 'Created By'];
  if (!headers || headers.length !== needed.length || needed.some(name => headers.filter(value => value === name).length !== 1)) throw new Error();
  const values = {
    'Token': input.name, 'Token ID': input.id, 'Project': input.project, 'Project ID': input.projectId,
    'Environment': [...new Set(input.scopes.map(scope => scope.environment))].join(', '),
    'Folders': input.scopes.map(scope => scope.folder).join(', '),
    'Permission': [...new Set(input.scopes.flatMap(scope => Array.isArray(scope.permissions) ? scope.permissions : [scope.permissions]))].join(', '),
    'Purpose': input.purpose, 'Owner': input.owner, 'Runtime consumer': input.consumer,
    'Status': input.status, 'Expires (UTC)': input.expiresAt, 'Metadata verified (UTC)': input.verifiedAt,
    'Evidence': input.evidence, 'Audit coverage': input.auditCoverage,
    'Secure storage reference': input.secureStorageReference,
    'Screenshot comparison 2026-11-03': input.screenshotComparison,
    'Discrepancy / evidence limits': input.discrepancyEvidenceLimits, 'Created By': input.createdBy,
  };
  const row = headers.map(header => values[header] ??  '');
  const secretPattern = /(?:\bst\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+|-----BEGIN [A-Z ]*PRIVATE KEY-----|"private_key"\s*:|"(?:encryptedKey|secret|token)"\s*:)/i;
  if (row.some(value => typeof value !== 'string' || secretPattern.test(value))) throw new Error();
  const idColumn = headers.indexOf('Token ID');
  const matches = rows.filter(existingRow => existingRow[idColumn] === input.id);
  if (matches.length > 1) throw new Error();
  const existing = matches[0];
  if (existing) {
    if (needed.some(header => String(existing[headers.indexOf(header)] ?? '') !== String(row[headers.indexOf(header)] ?? ''))) {
      process.stdout.write('Token ID exists with different metadata; stop and reconcile.\n');
      process.exitCode = 2;
    } else {
      process.stdout.write('Token ID already registered; no row added.\n');
    }
  } else {
    const appended = await api('/values/' + encodeURIComponent(sheet + '!A:S') + ':append?valueInputOption=RAW&insertDataOption=INSERT_ROWS', {
      method: 'POST', body: JSON.stringify({ majorDimension: 'ROWS', values: [row] }),
    });
    if (appended.updates?.updatedRows !== 1 || !appended.updates?.updatedRange) throw new Error();
    const readback = await read(appended.updates.updatedRange);
    const actual = readback.values ??  [];
    if (actual.length !== 1 || actual[0].length > row.length) throw new Error();
    const normalized = Array.from({ length: row.length }, (_, index) => actual[0][index] ?? '');
    if (JSON.stringify([normalized]) !== JSON.stringify([row])) throw new Error();
    process.stdout.write('Registry row appended and read back exactly.\n');
  }
} catch {
  process.stdout.write('Registration stopped; inspect privately and reconcile by Token ID before retrying.\n');
  fail();
}
JS
```

Coordinate one writer for each token before registration; Google Sheets has no token-ID uniqueness constraint, so the read-before-append check is not a concurrent-writer lock.  The exact headers are embedded above and the live header is checked on every run.  The row contains metadata only: never include token value, value hash, `encryptedKey`, `iv`, `tag` or service-account JSON.  If an append outcome is uncertain, the next run rereads by token ID and must reconcile; never blindly append twice.

After verifying the sheet row, the Fleet-OPS metadata mirror may be synchronized from an owned Fleet-OPS lane with `node scripts/infisical-service-tokens/sync-from-sheet.mjs --write`.  Inspect the diff and confirm only the matching metadata changed.  The sheet remains the source of truth; never hand-edit the mirror as a substitute.

## Rotate, revoke and close out

Rotation is not a metadata edit.  Issue a replacement with the intended scope and fresh expiry, install it, verify the grant and consumer store, update the registry with the new token metadata and mark the old row revoked, then revoke the old token through the authorized user session.  Never revoke or broaden a token based on a metadata row alone; verify actual token metadata and consumer state first.

A missing item, locked keychain, unreadable store, refused API call or uncertain write means stop and report the observed boundary.  Do not retry as another identity or silently change stores.  A token is complete when the consumer can read its own store entry without printing it, its exact metadata is verified, the sheet row is confirmed by readback, and any mirror sync is verified.  Record outside-scope uncertainty accurately rather than describing it as a denial.

## Related

- `secret-handoff` for moving a credential to its consumer.
- `secret-safety` for handling any secret value.
- `housekeeper` for a consumer that posts to a webhook from the Mac.
