# Fleet-Wide Compliance Rules

Sat, Oct 24, 2026.  Linear:  AFC-397 (the compliance rule registry) and AFC-400 (the starter rules).  Owner of this document:  the fleet, and the seat that owns the registry.

A per-seat rule asks "does this seat's tooling config match the desired state?".  A compliance rule asks a question about the fleet instead:  is a certificate about to expire,  is a credential sitting in a flat key file,  did a backup stop,  has a repo's Kody rules drifted from the canonical copy.  Both live in one registry (`fleet-seats.json`),  so the fleet has one place to read what it expects and one place to look when the answer is wrong.

The engine lives in `scripts/fleet_config/`.  The contract is `docs/plans/2026-10-19-fleet-config-design.md` (sections 3.3, 5, 6.3, 7 and 8).  This document is the how.

## Where A Rule Lives

The compliance family is the top-level `complianceRules` list in `fleet-seats.json`,  beside the `seats` object.  It is additive:  a reader that does not know the key ignores it,  so `schemaVersion` stays at 2 and the Muster app keeps reading the manifest it reads today.

```json
{
  "id": "compliance-secrets-hygiene",
  "kind": "compliance",
  "scope": ["mac"],
  "check": {"type": "secrets_hygiene", "target": "~/.secrets", "detail": ""},
  "expected": {
    "description": "No credential is written as a flat key file,  and no published file names an internal attack-map asset.",
    "names": ["published:.github/workflows", "published:github-workflows-template"]
  },
  "proofLevel": "L0",
  "ownerSeat": "CLAUDE",
  "autofixAllowed": false,
  "collector": "mac",
  "ttlSeconds": 3600,
  "description": "The retired flat key directory must hold no credential,  and the published workflows must name no internal asset.",
  "source": "OPS-818;  Kody rules 37 and 60",
  "threshold": {"knob": "COMPLIANCE_SECRETS_FINDINGS_MAX", "minimum": 0, "maximum": 10, "unit": "findings"},
  "implemented": true
}
```

The manifest is generated:  `scripts/fleet_config/seed_manifest.py` builds it and `fleet-seats.json` is its output,  so the file and the generator never drift.  A new rule is added to the seed,  not by hand to the JSON.

## The Vocabulary

| Field | What it says |
|---|---|
| `id` | Unique across the whole manifest,  seat rules and compliance rules together.  Lowercase letters,  digits and hyphens. |
| `kind` | Always `compliance` in this list. |
| `scope` | The hosts the rule is about:  `mac`, `vm`, `hetzner`, `cloud` or `fleet`.  It is not `platforms`:  a platform is a harness config surface,  and a fleet rule is about a host. |
| `check` | `{type, target, detail}`.  Three check types exist:  `command`,  `secrets_hygiene` and `certificate_expiry`. |
| `expected` | The desired state,  and the rule's declared list in `names`.  Every entry in `names` says what it is:  `published:<path>`,  `keychain:<name>`,  `profiles:<glob>`. |
| `proofLevel` | Always `L0`.  The proof machinery is about a seat's hook,  and a fleet rule has no hook. |
| `ownerSeat` | The seat that answers for this rule's drift.  It is a registry tag,  and the loader refuses one the registry does not hold live. |
| `autofixAllowed` | Always `false`.  No installer writes a fleet rule,  so a permission nobody can use is a promise the apply side cannot keep. |
| `collector` | Where the check runs:  `mac` today,  `vm`, `hetzner` and `api` reserved. |
| `ttlSeconds` | How long a result stays fresh.  A daily rule uses 86400. |
| `threshold` | The knob the rule's number comes from,  with the range of values the rule accepts.  The number is never in the manifest. |
| `implemented` | Whether the engine has a check for this rule's check type. |
| `implementationReason` | Why the rule cannot run yet.  Required when `implemented` is false. |
| `source` | Provenance:  the directive or ticket the rule came from. |

## How A Rule Is Written

1. **Name the rule and its owner.**  The owner seat is the one that answers when the rule drifts.  Every compliance rule needs one,  and the loader refuses a rule without an owner or with an owner the registry does not hold live.

2. **Declare the check.**  A rule that asks a question the engine cannot ask is a registry row with `implemented: false` and a reason that says what is missing.  That is honest,  and it keeps the gap visible in the registry instead of only in a review.  A rule that is not implemented produces no result,  so it never makes the run's verdict unknown forever.

3. **Declare the list the rule reads.**  A rule that reads a list declares it in `expected.names`,  and every entry says what it is.  A rule that declares no list has nothing to check,  and reads `UNKNOWN`:  an empty list is not a clean bill of health.

4. **Take the number from a knob.**  A threshold is a knob in `scripts/fleet_knobs/registry.py`,  read from Infisical.  Add the knob there,  then run `cd scripts && python3 -m fleet_knobs inventory --write ../INFISICAL.md` and commit both.  A rule whose knob is unset,  or whose value is outside the range the rule accepts,  reads `UNKNOWN` and never a pass.  A threshold nobody set is not a threshold,  so the knob's registry default is the value to set,  not one the rule may use.

5. **Never print a value.**  A rule reports names,  shapes and counts.  A reason is a short sentence about those,  and a secret value never reaches a result,  a reason,  the envelope or a log.

## Who Owns What

The owner seat answers for the rule's drift:  it is the seat that gets the finding and fixes what the rule found.  The seeded starter rules name these owners:

| Rule | Owner | What it asks |
|---|---|---|
| `compliance-secrets-hygiene` | CLAUDE | No credential in the retired flat key directory,  and no published file naming an internal asset (OPS-818,  Kody rules 37 and 60) |
| `compliance-apple-cert-expiry` | CODEX | Every Apple signing identity and provisioning profile the fleet uses is valid for the threshold window |
| `compliance-kody-rule-sync` | CODEX | The Kody rules of every repo match the canonical copy |
| `compliance-seat-wake-config` | MM | Every seat's wake configuration on this Mac is sane |
| `compliance-backup-heartbeat` | CLAUDE | The fleet backup job is fresh and healthy |
| `compliance-litestream-heartbeat` | CODEX | The litestream replication on the server is fresh |
| `compliance-testflight-group` | AG | Every app's TestFlight group assignments are the ones the fleet intends |
| `compliance-vercel-git-link` | MC | Every Vercel project is linked to the repository and branch it should be |
| `compliance-connector-credential-health` | FX | Every connector credential is live,  and none answers with an authentication failure |

These owner seats are the seed's proposal.  The registry holds a seat tag and nothing about what a seat is responsible for,  so the owner of each rule is the fleet owner's call,  and changing one is a one-line edit to the seed.

## How A Rule Reaches Muster's Grid

1. The engine runs the rule.  `python3 -m fleet_config status` (from `scripts/`) runs every rule and prints one line per result;  `--json` prints the pinned envelope.  A compliance result carries the kind `compliance`,  an empty seat,  and the scope in the platform field.

2. The envelope carries the results in its flat `results` list,  and the fleet-wide rules the engine cannot run yet in its flat `notImplemented` list.  Both keys are additive:  the app's decoder ignores a key it does not know.

3. The Muster app reads the manifest for the roster and the rule shapes,  and the envelope for the states.  The app's model already decodes the optional `complianceRules` key,  so a manifest with the family and a manifest without it both read.  The grid's compliance column is a later phase:  until then the compliance results and the `notImplemented` rows are visible in the JSON envelope and in the app's data,  and the app shows them when that phase lands.

4. Nothing is automatic in the other direction either:  a compliance rule never changes anything on this Mac.  `autofixAllowed` is always false for this family,  and the apply side has no installer for it.

## What A Rule May Never Do

- Read a credential value.  `stat` and names only.  The retired flat key directory is counted by name;  the files in it are never opened.
- Carry a value of the fleet's own inventory into the repository.  The leak patterns match shapes,  never the values the attack map holds.
- Ask for proof above `L0`,  allow an automatic fix,  or name a scope the engine does not know.
- Decide on a threshold nobody set.  An unset knob or an out-of-range value is `UNKNOWN`,  and `UNKNOWN` never reads as a pass.
