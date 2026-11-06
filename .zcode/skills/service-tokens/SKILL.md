---
name: service-tokens
description: >-
  Scoped Infisical service tokens for fleet consumers: mint one narrow grant, store it in the consumer host's own secret store, verify its exact scope in both directions, register the row in the fleet references registry, and retire it on time.  Load BEFORE issuing, rotating, widening or revoking any Infisical service token, before a consumer asks for a key no approved token covers, and before writing anything into a token registry.
---

# Service Tokens

> **Runtime (Z Code).**  Z Code desktop IDE (`/Applications/ZCode.app`) with config in `~/.zcode`.  Skills installed here (`~/.zcode/skills`) are loaded by Z Code.  Start Z Code inside a lane (`~/apps/lanes/<Repo>/zcode-<slug>`).  A seat a launcher assigned beats the ZC default.


## Why this exists

A shared credential that many consumers read is a credential none of them owns.  It cannot be revoked without breaking someone else, its scope drifts until it covers the whole estate, and nobody can say who reads it.  One consumer gets one token, scoped to exactly what it reads.

Two failures make this a written rule rather than a habit.  A token was minted and printed into a transcript, so it had to be revoked by hand and the leak was in the transcript forever.  Another consumer asked for a key no approved token covered, and the tempting answer was to reuse the shared Automation identity, which would have granted far more than the consumer needed.

## The rule

Every consumer gets its own scoped service token: one project, one environment, one folder, the narrowest access level that works, and an expiry.  The shared Automation identity is never a consumer's credential, and it is never a fallback.  A consumer that cannot resolve its own token stops and says so.

## Before you mint one

1. Inventory first.  Read the registry: the Fleet Reference sheet is the source of truth and `Fleet-OPS/docs/infisical-service-tokens.json` is its mirror.  Check whether a token already covers this consumer and this folder.
2. Reuse beats minting.  A token whose scope already matches the consumer exactly is the right answer, and the registry row already names its consumer.
3. Never widen.  If the consumer needs more than an existing token grants, mint a new token for the consumer.  Editing an existing grant changes what every other holder of it can read.
4. One consumer, one row.  Two consumers sharing a token means neither can be rotated alone.

## Minting it

The Infisical CLI reads the login keychain, so a plain shell over SSH cannot authenticate as the owner.  Run the CLI in the owner's GUI session, and treat the output as a secret.

```sh
infisical service-token create \
  --projectId <project-id> \
  --scope <environment>:<folder> \
  --access-level read \
  --expiry-seconds 7776000 \
  --name <consumer>-ro
```

The CLI prints the token on stderr.  Capture it straight into a 0600 file and never echo it, never let it reach a log, and never let it reach a transcript.  That file is staging, not a home: install the value into the consumer host's secret store, verify it there, and delete the file.  A token that was printed is a token that must be revoked and re-minted, not a token that may be reused.

## Where the token lives

The value lives in the consumer host's own secret store, and nowhere else.

- On the Mac: the owner's login keychain, one generic-password item per token.  The service is `infisical-service-token` and the account is the token's registry name, so a reader holding the registry row can find the item by that name: `security find-generic-password -s infisical-service-token -a <name> -w`.
- On a server or a container: that host's own store, which for a fleet consumer is Infisical itself or the environment the host injects the value into.  A server-side consumer never reaches for the Mac keychain, and a Mac consumer never reaches for a server-side file.
- The metadata sidecar `~/.config/infisical/<consumer>.json` carries the token id, the project, the environment, the folder, the access level and the expiry.  It never carries the value.
- The token is never passed as a command line argument, because a command line is visible in `ps` and in the process log.  It is read from the store inside the process that uses it, and the Mac's keychain write takes the value on stdin rather than in the argument list.
- A 0600 file under `~/.config/infisical/` is a staging step on the way in, never the resting place: the CLI prints the value on stderr, so it has to be captured somewhere before it can be installed.  Once the value is in the keychain, the staging file is deleted.  A file in a repo is a published secret the moment the repo is pushed.
- The Mac login keychain is reachable only from the owner's GUI session.  A plain shell over SSH cannot read or write it, so the install and the consumer both run in that session, and a consumer outside it fails closed with the reason.

## Verifying the scope

A scope that was never checked is a scope that was never bound.

1. Read one secret from the folder with the token, and confirm the value is the one the consumer expects.  A token that returns nothing is a token with no access, not a consumer with no secrets.
2. Read a secret outside the folder, and expect a refusal.  If that read succeeds, the token is wider than its grant: revoke it and mint a narrower one.
3. Check the access level.  A consumer that only reads is issued a read-only token, even when the same folder also holds a write path.

## Registering the row

A token that is not registered cannot be rotated by anybody but the seat that minted it, and it cannot be revoked in an emergency by a human who does not know it exists.  Every token gets a row in both places:

- The Fleet Reference sheet, which is the source of truth.  The sheet has a seat that owns it, so the row goes to that seat to write; ask for it with the fields below, and do not treat the token as registered until the sheet row exists.
- The mirror `Fleet-OPS/docs/infisical-service-tokens.json`, one object per token.

The row carries: `id` (the token id), `name`, `project`, `projectId`, `scopes`, `purpose`, `owner`, `consumer`, `status`, `createdAt`, `expiresAt`, `verifiedAt` and `evidence`.

- `consumer` is the program or host that reads the token, named exactly as it appears in its configuration, so a reader can find the item.
- `evidence` is what proved the scope: the verification that was run and when.
- `status` is `active`, `revoked` or `expired`.  A revoked token keeps its row, because the history of a grant is part of the registry.

## Rotating and revoking

Rotation is never a metadata edit.

1. Mint the replacement with the same scope and a fresh expiry, and install it on the consumer.
2. Verify the replacement in both directions.
3. Update the registry row: the new id, the new expiry, the old row's status `revoked`.
4. Revoke the old token through the API with an owner session: `DELETE https://app.infisical.com/api/v2/service-token/<token-id>`.

Revoke a token when it is expired, when its consumer is retired, when it was printed or logged, or when its scope turns out to be wider than its grant.  Never revoke or broaden a token from a metadata row alone: a row is a claim about a token, and the API is the fact about it.

## Failing closed

- A missing item, a locked keychain or an unreadable secret store means the consumer stops with a clear error naming the item and the missing scope.
- A refused API call means the consumer stops.  It does not fall back to another token, does not read a flat key file, does not reuse the Automation identity, and does not cache a value it managed to read once.
- A grant that no approved token covers is an owner request, not a technical obstacle.  Ask for the narrow grant, and say which folder and which access level it needs.

## Before you call it done

- One project, one environment, one folder, read-only unless write is genuinely required.
- An expiry is set, and it is short enough to be meaningful.
- The value is in the consumer host's secret store, the Mac login keychain for a Mac consumer, and appears in no log, no transcript, no command line and no pull request.
- The scope was verified in both directions: it reads inside the folder and it is refused outside it.
- The registry row exists in the sheet and in the mirror, with the verification recorded.
- Every temporary issuance artifact is gone: the issue script, the launchd job that ran it, the log that held the value, and the staging file the value was captured in.

## Related

- `secret-handoff` for how a credential reaches a consumer in the first place.
- `secret-safety` for the handling rules that apply to every secret, token included.
- `housekeeper` for a consumer that posts to a webhook from the Mac.
