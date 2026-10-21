# Fleet-Config Design:  The Contract For The Muster Read And Write Engine

Mon, Oct 19, 2026.  Issues AFC-384, AFC-385 and AFC-386.  Project Muster:  Fleet Admin Mac App, team AFC.  This document is the shared contract for Phase 2.  An implementer who has read it should need no further design answers.  Where a choice was forced, the rejected alternative is named next to it.

## 1. Purpose And Scope

Muster is a native Mac SwiftUI admin app for monitoring and controlling every fleet seat's tooling config.  Phase 2 builds the engine under it:  a `fleet-config` CLI in `scripts/fleet_config/`, stdlib only, in the style of `scripts/fleet_knobs/`.  The CLI answers one question in machine-readable form:  does each seat's installed tooling config match the desired state in the manifest, and how strongly was that proved.  A later phase adds the plan and apply side on the same contract, and the SwiftUI app renders the JSON envelope defined here.

In scope for the contract:  the check vocabulary, the proof levels, the `status --json` envelope, the per-seat desired-state manifest, the boundary with AFC-375, and the module ownership map.  Out of scope for Phase 2:  the SwiftUI app, the Infisical knob panel (it reads `fleet_knobs`, and this engine never redefines a knob value), live fleet status transport, and the compliance rule registry UI.  Those phases consume this contract and do not amend it silently.

## 2. Boundary With AFC-375 (Fleet GitOps G7)

AFC-375 plans `deploy/mac/manifest.toml` describing Mac LaunchAgents and pm2 jobs.  The two manifests describe disjoint surfaces, and neither may describe the other's rows.

| Surface | Owner |
|---|---|
| Per-seat harness tooling config inside harness config dirs:  instruction files, hook registrations, MCP server registrations, skills, sub-agents, permissions and credential file stat | `fleet-config`, manifest `fleet-seats.json` (this design) |
| Mac LaunchAgents and pm2 jobs:  labels, plists, programs, keepalive, run intervals, working directories | AFC-375, `deploy/mac/manifest.toml` |
| Runtime modes, caps and flags read by jobs | Infisical through `scripts/fleet_knobs/`;  neither manifest copies a knob value |
| Git checkout health and lane safety | `scripts/fleet_lanes/doctor.py`, folded in as a consumer later;  `fleet-config` is not a third doctor |

The edge case is a hook that is also a daemon.  The registration of a hook in a harness settings file, and the hook script's presence and mode, belong to `fleet-config`.  A LaunchAgent or pm2 job that runs a long-lived process belongs to AFC-375, even when the process is the same script.  If a surface seems to fit both, it goes to AFC-375 when it is a process and to `fleet-config` when it is harness config.  Describing a LaunchAgent in `fleet-seats.json` is a review blocker, because the two engines would then fight over one row.

## 3. Fixed Vocabulary

### 3.1 Check States

Exactly six, spelled as in `types.py` `CheckState`:  `PASS`, `DRIFT`, `MISSING`, `UNPROVEN`, `UNSUPPORTED`, `UNKNOWN`.  `PASS` is the only healthy state.  `DRIFT` means present but not matching.  `MISSING` means the target is absent.  `UNPROVEN` means the config matches at the achieved proof level but the required proof was not run or is stale.  `UNSUPPORTED` means this platform or collector cannot be checked for this kind in this phase.  `UNKNOWN` means the check could not produce a trustworthy answer, and it must never render as healthy in any consumer.

### 3.2 Proof Levels

Exactly three, spelled `L0`, `L1`, `L2` (`types.py` `ProofLevel`, ordered by `rank`).

| Level | Meaning | When it runs |
|---|---|---|
| L0 | Config present and matching by hash, or by the name-set and mode comparison its check type defines | Every `status` run |
| L1 | A synthetic PreToolUse payload that attempts a write into a sentinel lane is piped into the installed hook, and the hook must deny it | Every `status` run, for `hooks` rules that require it |
| L2 | A headless platform canary (such as `claude -p` or `codex exec`) runs in a throwaway lane with a read-only prompt, showing the platform actually loads the hook | Only `status --prove l2`, or a daily job;  it costs a model turn, so it never runs by default |

The L1 payload carries the tool name the platform runtime really delivers, and the sentinel set covers the file tools as well as the shell tools (AFC-416).  A Claude-flavoured sentinel that names only `Bash` lets a broken matcher pass while the seat is unguarded (MiniMax delivers `bash` on `mcode exec` and `Bash` in the Desktop app), so the probe sends every native shell spelling for its platform and every native file tool (`Write`/`Edit` with `file_path`, `write`/`edit` with `path`), and all of them must deny.  A guard that ignores file payloads fails open (ALLOW), which the probe correctly folds into DRIFT.

Two invariants keep `UNPROVEN` rare by construction.  Only `hooks` rules may require `L1` or `L2` in Phase 2, and the manifest loader's validation rejects a higher requirement on any other kind.  An `L2` rule must set `ttlSeconds` to at least 86400, so a daily canary can stay fresh between runs.

### 3.3 Config Kinds, Collectors And Platforms

The six config kinds (`types.py` `ConfigKind`) are `instructions`, `hooks`, `mcp`, `skills`, `subagents` and `permissions`.  No check module invents a seventh kind.

A `collector` says where a check runs:  `mac` now, with `vm`, `hetzner` and `api` reserved.  A rule with a reserved collector yields one `UNSUPPORTED` result per named platform in Phase 2, with the reason naming the collector.  Reserving the values now means a later phase adds collectors with no schema change.

A `platform` is a harness config surface id from `scripts/fleet_lanes/install_rules.py` `PLATFORMS` (such as `claude`, `codex`, `grok`, `antigravity`, `minimax`, `cursor`, `clutch`, `kimi`, `opencode`, `muse-code`).  Platform ids are passed into `fleet_config` as data, never hardcoded in this package and never imported from `fleet_lanes` at runtime.  The shared `home-agents` file is not a seat platform and must not appear in a manifest.  A platform with no file for a kind (per the installer tables) yields `UNSUPPORTED` for that kind, not an invented path.

## 4. Fail-Closed Semantics

A check that is stale (past its TTL), errors, times out or produces unparseable output counts as failing, never as passing.  The mapping is total, and the aggregator applies it in this order per rule and platform:

| Step | Condition | State | Reason code | Failing |
|---|---|---|---|---|
| 1 | Collector is not `mac` in Phase 2 | `UNSUPPORTED` | `unsupported` | No, but never healthy |
| 2 | The platform has no such config surface | `UNSUPPORTED` | `unsupported` | No, but never healthy |
| 3 | The target is absent | `MISSING` | `missing` | Yes |
| 4 | The check raised, timed out or returned unparseable output | `UNKNOWN` | `error`, `timeout` or `unparseable` | Yes |
| 5 | The observed state does not match the expected state at L0 | `DRIFT` | `drift` | Yes |
| 6 | L0 matches but the required L1 or L2 proof was not run | `UNPROVEN` | `proof-not-run` | No, but never healthy |
| 7 | L1 or L2 ran and the hook or platform did not deny the sentinel write | `DRIFT` | `proof-failed` | Yes |
| 8 | The required proof is achieved and fresh | `PASS` | `ok` | No |

Staleness is applied last, and it wins.  A result read after its `expiresAt` is re-labelled before it reaches the envelope:  state `UNKNOWN`, reason code `stale`, `stale` true, and failing, whatever state the rule itself produced.  It keeps its original `checkedAt` as evidence.  The envelope builder does not do this relabelling;  the aggregator owns the clock.  So the one condition of stale evidence has one answer everywhere:  a stale result is `UNKNOWN` with reason code `stale`, and `is_failing` counts it as failing.  It is never `UNPROVEN` with reason code `proof-not-run`, because `proof-not-run` says a proof was never attempted, while a result whose evidence has expired was attempted and is now out of date.  The two conditions are different and they do not overlap.

A status run has an overall budget as well as a per-check timeout.  When the budget is spent, the rules that have not run yet are reported as `UNKNOWN` with reason code `timeout` instead of being run, so one slow hook probe cannot turn a run into a queue of probes.  The budget adds no vocabulary:  the result it produces is step 4 of the table above.

The verdict math lives in `contract.py` as pure functions, so every consumer shares it.  `is_failing` is true for `DRIFT` and `MISSING`, and for any result whose reason code is `stale`, `error`, `timeout`, `unparseable` or `proof-failed`.  `overall_verdict` is `FAIL` when any result is failing, else `PASS` when at least one result exists and every result is `PASS`, else `UNKNOWN`.  An empty run is `UNKNOWN`, never `PASS`.

Finished CLI exit codes are pinned now:  0 for verdict `PASS`, 1 for verdict `FAIL`, 3 for verdict `UNKNOWN` (the `fleet_knobs` precedent for an unreachable source), 2 for a refused run (a bad manifest, a bad registry, a declined Touch ID confirmation, or an unimplemented verb in the skeleton), and 64 for a usage error.  The skeleton in `__main__.py` returns 2 for its stub verbs, which is a placeholder, not the finished mapping.  A usage error is a mistake in the request and never shares its code with a refusal:  an unknown verb, an unknown flag, or a `--seat` value that is not a registry tag exits 64, so a caller can tell "the command line is wrong" (64, and the caller fixes it) from "the run was refused" (2, and the owner approves or fixes the machine).  The parser raises instead of exiting, so argparse's own exit status 2 never leaks out of the CLI.

## 5. Check Types Per Kind

`check.type` is one of `types.py` `CheckType`.  `check.target` is a path for file checks and a name (never a value) for a credential reference.  `check.detail` names the part inside the target.  A credential reference's `detail` is the reference NAME, such as `ZULIP_CLAUDE`, and never a sentence:  a manifest and a reason carry names, so a name is the only thing a reference can be, and the prose about it belongs in `expected.description`.  The allowed pairings are fixed:

| Kind | Check type | Target and detail | Expected fields used |
|---|---|---|---|
| `instructions` | `marked_block_hash` | File path;  detail is the marked block name (the `fleet_lanes/marked_block.py` marker family) | `sha256`, `present` |
| `instructions` | `file_hash` | File path;  detail empty | `sha256`, `present` |
| `hooks` | `hook_registered` | Harness settings file;  detail is the event, Phase 2 only `PreToolUse` | `names` (hook ids), `timeoutSeconds`, `present` |
| `mcp` | `mcp_servers_registered` | Harness MCP config file | `names` (server names only) |
| `skills` | `skills_installed` | Skills directory for the platform | `names` (skill directory names) |
| `subagents` | `subagents_installed` | Sub-agent definitions directory for the platform | `names` (file stems) |
| `permissions` | `permissions_match` | Harness settings file | `names` (permission identifiers), `mode` |
| `permissions` | `credential_stat` | Credential file path;  detail is the credential reference name | `present`, `mode` |
| `permissions` | `credential_key_present` | Secret-store folder path, such as `/zulip`;  detail is the credential reference name | `names` (secret-store key names only), `present` |

`credential_key_present` is how the fleet reaches a credential now that the flat credential directory is retired (AFC-359).  It asks the `fleet-secrets` CLI for the KEY NAMES in one secret-store folder, using the `names` verb, which prints names and never values, so a secret cannot reach a result, a reason or a log by construction.  A key that is absent is step 3 (`MISSING`);  a secret store that cannot be reached, because the CLI is missing, the machine identity is gone, the call timed out or the output was unreadable, is step 4 (`UNKNOWN` with a failing reason code).  The listing is cached on the run context, so one status run asks the secret store once per folder rather than once per rule.  A rule reports only the names it asked for, so one seat's rule never carries another seat's key names into a result, a reason or a log.

The example above uses a placeholder for the retired file's path on purpose, and the placeholder is a stand-in, not a path.  The real target is generated by `scripts/fleet_config/seed_manifest.py`, which builds it from the registry row's `zulipFileCode`;  the generated manifest is the only place the concrete path appears.  A rule that carries the literal placeholder passes vacuously, because no file of that name exists, so the placeholder must never be copied into a manifest anyone means to run.  A concrete home-local path in a public document is both a disclosure and a stale literal the moment the file is retired.

The `credential_stat` check keeps its own job:  it reports the stat data of a file, and it is what a rule that says a RETIRED file must be GONE uses, because `expected.present: false` inverts it.

Hash rules are exact, so two implementations agree byte for byte.  `file_hash` is the lowercase hex SHA-256 of the raw file bytes.  `marked_block_hash` is the lowercase hex SHA-256 of the marked block body as `fleet_lanes.marked_block.block_body` returns it (CR stripped, markers excluded), encoded as the lines joined by `\n` with one trailing `\n`, in UTF-8.  A file with malformed markers is step 4 (`UNKNOWN`, `unparseable`), never a guessed hash.  This package reimplements that body extraction rather than importing `fleet_lanes`, for the packaging reason in section 12, and a test must pin the two implementations to the same digest on a shared fixture.

Name-set comparisons are per check type, and the check type decides which one applies.  A subset test asks only whether every expected name is present;  a set-equality test also asks whether the machine holds a name the rule did not expect.

| Check type | Comparison | A name the machine holds that the rule did not expect |
|---|---|---|
| `permissions_match` | Set equality | Drift |
| `hook_registered` | Subset | Noted in the reason text, never drift |
| `mcp_servers_registered` | Subset | Noted in the reason text, never drift |
| `skills_installed` | Subset | Noted in the reason text, never drift |
| `subagents_installed` | Subset | Noted in the reason text, never drift |
| `credential_key_present` | Subset | Not drift;  the folder holds every seat's keys |

`credential_key_present` has to be a subset test.  The check asks the secret store for the KEY NAMES in one folder, and that folder holds every seat's keys, so the observed set is the whole folder and can never equal one seat's three names.  The rule passes when every name it expects is among them, and it is `MISSING` when one of them is not;  the names it reports are the ones it expected, never the rest of the folder.  The envelope lists expected names in manifest order and observed names sorted, so a diff is stable to read.  A credential file is checked by `stat` only:  existence, mode, size and mtime.  Its contents are never opened, hashed or compared, and no field in `types.py` can hold them.

## 6. The Manifest:  `fleet-seats.json`

The manifest is a new file `fleet-seats.json` at the repo root, a sibling of `fleet-apps.json`.  It is not an edit to `fleet-apps.json`.  The top level is an object with `schemaVersion` (the integer 1, pinned by `manifest.py` `MANIFEST_SCHEMA_VERSION`) and `seats`, an object keyed by registry seat tag.

One seat entry, excerpted (a real file must carry every live seat, per section 7):

```json
{
  "schemaVersion": 1,
  "seats": {
    "CLAUDE": {
      "platforms": ["claude"],
      "rules": [
        {
          "id": "claude-instructions-lane-map",
          "kind": "instructions",
          "platforms": ["claude"],
          "check": {"type": "marked_block_hash", "target": "<claude-instructions-path>", "detail": "fleet-lane-map"},
          "expected": {"description": "Lane Map block matches the installer source", "present": true, "sha256": "<64 lowercase hex>"},
          "proofLevel": "L0",
          "ownerSeat": "CLAUDE",
          "autofixAllowed": false,
          "collector": "mac",
          "ttlSeconds": 900
        },
        {
          "id": "claude-hooks-lane-guard",
          "kind": "hooks",
          "platforms": ["claude"],
          "check": {"type": "hook_registered", "target": "<claude-settings-path>", "detail": "PreToolUse"},
          "expected": {"description": "Lane guard hook registered with its timeout", "present": true, "names": ["lane-guard"], "timeoutSeconds": 10},
          "proofLevel": "L1",
          "ownerSeat": "CLAUDE",
          "autofixAllowed": false,
          "collector": "mac",
          "ttlSeconds": 900
        },
        {
          "id": "claude-mcp-fleet-recall",
          "kind": "mcp",
          "platforms": ["claude"],
          "check": {"type": "mcp_servers_registered", "target": "<claude-mcp-config-path>", "detail": ""},
          "expected": {"description": "Fleet MCP servers registered by name", "present": true, "names": ["fleet-recall"]},
          "proofLevel": "L0",
          "ownerSeat": "CLAUDE",
          "autofixAllowed": false,
          "collector": "mac",
          "ttlSeconds": 900
        },
        {
          "id": "claude-skills-fleet",
          "kind": "skills",
          "platforms": ["claude"],
          "check": {"type": "skills_installed", "target": "<claude-skills-dir>", "detail": ""},
          "expected": {"description": "Fleet skills installed by name", "present": true, "names": ["fleet-mode"]},
          "proofLevel": "L0",
          "ownerSeat": "CLAUDE",
          "autofixAllowed": false,
          "collector": "mac",
          "ttlSeconds": 900
        },
        {
          "id": "claude-subagents-fleet",
          "kind": "subagents",
          "platforms": ["claude"],
          "check": {"type": "subagents_installed", "target": "<claude-agents-dir>", "detail": ""},
          "expected": {"description": "Fleet sub-agent definitions installed by name", "present": true, "names": ["code-scout"]},
          "proofLevel": "L0",
          "ownerSeat": "CLAUDE",
          "autofixAllowed": false,
          "collector": "mac",
          "ttlSeconds": 900
        },
        {
          "id": "claude-permissions-zulip-key",
          "kind": "permissions",
          "platforms": ["claude"],
          "check": {"type": "credential_key_present", "target": "/zulip", "detail": "ZULIP_CLAUDE"},
          "expected": {"description": "The CLAUDE Zulip credential is reachable in the secret store at /zulip", "present": true, "names": ["CLAUDE_ZULIP_EMAIL", "CLAUDE_ZULIP_KEY", "CLAUDE_ZULIP_SITE"]},
          "proofLevel": "L0",
          "ownerSeat": "CLAUDE",
          "autofixAllowed": false,
          "collector": "mac",
          "ttlSeconds": 900
        },
        {
          "id": "claude-permissions-zulip-retired-file",
          "kind": "permissions",
          "platforms": ["claude"],
          "check": {"type": "credential_stat", "target": "<retired-flat-credential-file>", "detail": "ZULIP_CLAUDE"},
          "expected": {"description": "The retired flat Zulip credential file is gone, stat only", "present": false},
          "proofLevel": "L0",
          "ownerSeat": "CLAUDE",
          "autofixAllowed": false,
          "collector": "mac",
          "ttlSeconds": 900
        }
      ],
      "notApplicable": []
    }
  }
}
```

Every path in the example is a placeholder, written as an angle-bracket token such as `<claude-settings-path>`.  The placeholder names a thing, never a location:  the concrete path on a seat's machine lives in the generated manifest and nowhere else, because a home-local path in a public document is a disclosure and a stale literal the moment the file moves.  A rule built from a placeholder path is a rule that checks nothing, so a manifest is never written by hand from this example.

### 6.1 Rule Fields

Every rule carries exactly these fields (camelCase in JSON, snake_case in `types.py` `Rule`):  `id`, `kind`, `platforms`, `check`, `expected`, `proofLevel`, `ownerSeat`, `autofixAllowed`, `collector` and `ttlSeconds`, plus an optional `description` and an optional `source` (AFC-416).  This is the Muse Assist rule shape (`id`, `platforms`, `check`, expected state, owner seat, autofix allowed) with three Phase 2 additions:  `proofLevel`, `collector` and `source`.  `source` is provenance only and never affects the verdict:  a standing directive plus date, or a Zulip topic message id citing the seat reply the rule came from (AFC-384 requires the review to cite MM's answers at 633125365 and 633197144, the delta at 633215586, and the sharpened answers at 633216022).

| Field | Meaning |
|---|---|
| `id` | Unique across the whole manifest;  lowercase letters, digits and hyphens;  the apply side addresses rules by it (`apply --only`) |
| `kind` | One of the six config kinds |
| `platforms` | Non-empty list of platform ids this rule checks;  each produces one result |
| `check` | `{type, target, detail}` per section 5;  never holds a secret value |
| `expected` | `{description, present, sha256, names, mode, timeoutSeconds}`;  only the fields its check type uses;  names and booleans only where a secret could stand |
| `proofLevel` | The proof the rule needs:  `L0`, `L1` or `L2` |
| `ownerSeat` | The registry tag of the seat that answers for this rule's drift;  may differ from the checked seat |
| `autofixAllowed` | The only permission the apply side has;  false means report only, forever, until a human edits the manifest |
| `collector` | `mac` in Phase 2;  `vm`, `hetzner` and `api` reserved |
| `ttlSeconds` | How long a result stays fresh;  default 900;  at least 86400 for an `L2` rule |
| `source` | Provenance only:  standing directive plus date, or Zulip message id;  empty when uncited |

A file that fits none of the six kinds (AFC-416 Finding 3:  MiniMax channel and identity YAMLs, `mcp-runtime-names.json`, `review-links.json`, unbounded `background-tasks/` state) is not a seventh kind.  It is named in the seat's `uncovered` list, as `{surface, reason}`,  never by silence and never by forcing it into a kind it does not fit;  a later phase that grows the kinds can move a surface out of the list and into a rule.  The list is its own field because a `notApplicable` cover carries a kind,  and a cover for a surface that fits no kind would have to name one anyway.  The app renders the list,  so the surface is visible as knowingly blind rather than accidentally unchecked,  and the list produces no result and no verdict:  it is the honest statement that the surface is not checked,  and the reason says why.

### 6.2 Where A Seat Entry Comes From

A seat with no Mac config surface (a hosted or cloud seat) still gets an entry.  It lists `platforms` as an empty list and carries one seat-wide `notApplicable` entry per kind, each with a human reason, for example `"hosted seat;  no Mac harness config surface in Phase 2"`.  Not-applicable entries produce no check results.  The app renders them from the manifest, so the seat is visible without inventing a check nobody can run.

## 7. Registry Source Of Truth And Coverage

Seat names come from the fleet registry, never hand-typed.  The source of truth is `fleet-apps.json` at the repo root, key `seats`, field `tag`, parsed canonically by `scripts/fleet_lanes/layout.py` `load_registry()` into `Registry.seats[].name`.  The live roster is every row that is not retired and carries no `aliasOf`.  Pattern families in `seatApps.patterns` (`BF-*`, `GB-*`) and `seatApps.extraSeats` (`JET`) are not roster seats and get no manifest entries until the registry gives them their own `seats` rows.

The manifest references the registry by key, not by literal string in code:  the `seats` object is keyed by tag, `manifest.py` `registry_seat_tags()` reads the same file as JSON and returns the live tags in registry order, and `load_manifest()` refuses an unknown key, a retired or alias key, and a live seat with no entry.  No module in this package hardcodes a seat tag.  The loader reads the registry as JSON instead of importing `fleet_lanes`, because a stable copy of this package run with `python3 -I` carries just this package (section 12);  the JSON read mirrors `load_registry()` semantics for the three fields it needs (`tag`, `retired`, `aliasOf`), and a test must pin the two to the same roster.

Coverage is a validation rule, enforced by `manifest.py` `validate_coverage()`:

1. For each seat entry, for each platform in its `platforms`, for each of the six kinds:  some rule of that seat names the kind and the platform, or a `notApplicable` entry covers it (matching platform, or empty `platforms` for a seat-wide cover).  Otherwise the manifest is rejected, naming the uncovered triple.
2. A seat with an empty `platforms` list needs one seat-wide `notApplicable` entry per kind, each with a non-empty `reason`.
3. Every `notApplicable` entry needs a non-empty `reason`.  Silence is never a cover.
4. Rule ids are unique across the manifest (enforced in `load_manifest()`).
5. Only `hooks` rules may require `L1` or `L2`, and an `L2` rule needs `ttlSeconds` of at least 86400.
6. Every platform named anywhere is in the known platform list passed to `validate_platforms()` from the installer tables at the call site.

## 8. The `status --json` Envelope

`contract.py` `SCHEMA_VERSION` is the integer 1, pinned by a contract test.  A breaking envelope change raises it deliberately and updates this document in the same commit;  a version never changes in place.  The envelope carries a flat `uncovered` list of `{seat, surface, reason}` beside the flat `results` list,  for the same reason the results list is flat:  two flat lists cannot disagree with each other.  Adding that list did not raise the version,  because the consumer that exists  (the Muster app's decoder)  ignores unknown fields,  so a new top-level key is additive for it and not a breaking change;  the version moves when a field's meaning changes,  or when a consumer has to be told.  A breaking envelope change raises it deliberately and updates this document in the same commit.  The envelope is one flat `results` list;  consumers group by seat, platform and kind.  It carries no second grouped copy that could disagree with the flat list.

```json
{
  "schemaVersion": 1,
  "tool": "fleet-config",
  "command": "status",
  "generatedAt": "2026-10-19T14:30:00Z",
  "summary": {
    "total": 2,
    "byState": {"PASS": 1, "DRIFT": 1, "MISSING": 0, "UNPROVEN": 0, "UNSUPPORTED": 0, "UNKNOWN": 0},
    "overallVerdict": "FAIL",
    "failing": 1
  },
  "results": [
    {
      "ruleId": "claude-hooks-lane-guard",
      "seat": "CLAUDE",
      "platform": "claude",
      "kind": "hooks",
      "state": "PASS",
      "reason": "Hook lane-guard is registered and denied the sentinel write at L1.",
      "reasonCode": "ok",
      "expected": {"description": "Lane guard hook registered with its timeout", "present": true, "names": ["lane-guard"], "timeoutSeconds": 10},
      "observed": {"present": true, "names": ["lane-guard"], "timeoutSeconds": 10},
      "proofLevelRequired": "L1",
      "proofLevelAchieved": "L1",
      "collector": "mac",
      "checkType": "hook_registered",
      "checkedAt": "2026-10-19T14:30:00Z",
      "expiresAt": "2026-10-19T14:45:00Z",
      "ttlSeconds": 900,
      "stale": false,
      "failing": false
    },
    {
      "ruleId": "claude-skills-fleet",
      "seat": "CLAUDE",
      "platform": "claude",
      "kind": "skills",
      "state": "DRIFT",
      "reason": "Skill set differs:  fleet-mode is installed, sentence-gap is not.",
      "reasonCode": "drift",
      "expected": {"description": "Fleet skills installed by name", "present": true, "names": ["fleet-mode", "sentence-gap"]},
      "observed": {"present": true, "names": ["fleet-mode"]},
      "proofLevelRequired": "L0",
      "proofLevelAchieved": null,
      "collector": "mac",
      "checkType": "skills_installed",
      "checkedAt": "2026-10-19T14:30:00Z",
      "expiresAt": "2026-10-19T14:45:00Z",
      "ttlSeconds": 900,
      "stale": false,
      "failing": true
    }
  ]
}
```

Field rules for every result:  `ruleId`, `seat`, `platform`, `kind`, `expected` versus `observed`, `proofLevelRequired`, `proofLevelAchieved` (null when no level was achieved), `state`, a human-readable `reason`, a machine `reasonCode`, plus `collector`, `checkType`, `checkedAt`, `expiresAt`, `ttlSeconds`, `stale` and `failing`.  The top level carries `schemaVersion`, `tool`, `command`, `generatedAt` (ISO 8601 UTC with a `Z` suffix, one clock for the whole run) and `summary` with `total`, `byState` (all six state keys, always), `overallVerdict` and `failing`.  It never carries a secret value:  names, booleans, modes, sizes and hashes of non-secret config only, and `stat` fields only for a credential file.

The human (non-JSON) `status` output is a rendering of the same report, one line per result, worst state first.  It adds no facts the envelope lacks.

## 9. Proof Machinery

L0 lives inside each per-kind check module:  hash or name-set comparison against `expected`.  L1 and L2 live in `proof.py` and nowhere else.

L1 is a pure probe.  `proof.py` builds a synthetic PreToolUse payload per platform adapter whose tool input attempts a write to a sentinel path under a throwaway sentinel lane (a path string only;  nothing creates the lane and nothing writes the file), pipes it to the installed hook's stdin, and reads the platform's deny signal (exit code and output shape per adapter).  A deny achieves L1.  An allow is step 7 (`DRIFT`, `proof-failed`).  A hook that crashes, is missing or times out is step 4, never a pass.  Payload shapes differ per platform, so each adapter is one small function in `proof.py` with its own fixture test.  The hook under test is the installed one named by the rule, resolved through the platform's settings file, never a copy from the repo.

L2 is a canary with a cost guard.  `proof.py` creates a throwaway lane, runs the platform's headless command (`claude -p`, `codex exec`, per adapter) with a read-only prompt that asks the model to attempt the sentinel write, and looks for deny evidence in the transcript.  Deny evidence with a clean exit achieves L2.  No deny evidence is step 7.  The lane is removed afterwards, pass or fail.  L2 runs only under `--prove l2` or from the daily job, and it is the only part of this engine that may spend a model turn.  A `status` run without `--prove l2` neither spends a model turn nor reads a per-rule L2 evidence cache:  an L2 rule whose proof was not requested reads `UNPROVEN` with reason code `proof-not-run`, which is never healthy but does not accuse the seat of drift.  That is the whole of the not-run case.  It is not the stale case, and the two must not be confused:  a result whose own TTL has passed, including one served from the last-known-good copy, is re-labelled `UNKNOWN` with reason code `stale` by the aggregator, and it is failing, exactly as section 4 says.  A stale result is never reported as `UNPROVEN`.

The seam between the aggregator and the proof machinery is one function, `proof.prove_rule(rule, platform, ctx, prove_l2)`.  The aggregator decides WHICH rule needs a proof and this function decides HOW to get one, so neither side has to know the other's internals.  The outcome carries `level` as the level that was ACHIEVED, so a proof that ran and failed reports None and the aggregator folds it into step 7 (`DRIFT`, `proof-failed`) or step 4 (`UNKNOWN`, `error`), both of which are failing.  A proof that did not run at all reports `proof-not-run` and the rule reads `UNPROVEN`, which is never healthy but does not accuse the seat of drift.  The aggregator can be given a `proof_runner` instead, which is how a test drives this path without a real hook.

## 10. Module Ownership Map

Every module in `scripts/fleet_config/`, with one owner lane each, so four parallel implementers never edit the same file.  Flat modules, matching `fleet_knobs`.

| Module | Responsibility | Owner lane |
|---|---|---|
| `__init__.py` | Package docstring and the public re-exports;  no logic | Kernel (done) |
| `types.py` | The fixed vocabulary:  states, proof levels, kinds, collectors, check types, reason codes and every dataclass | Kernel (done) |
| `contract.py` | `SCHEMA_VERSION`, the envelope builder and the pure verdict math (`is_failing`, `overall_verdict`, `summarize`) | Kernel (done);  changes only with a schema version bump |
| `manifest.py` | Manifest loading, registry roster reading, coverage and platform validation | Implementer 1 (manifest) |
| `seed_manifest.py` | Generates `fleet-seats.json` from the registry:  one entry per live seat, the expected hashes from the installer source, and the credential targets from the registry's `zulipFileCode` | Implementer 1 (manifest) |
| `checks.py` | The shared check context (`CheckContext`, including the per-run `cache`), `build_result`, and the one allowlisted runner every child process starts through | Implementer 2 (checks) |
| `checks_instructions.py` | `instructions` checks:  marked block hash and file hash, including the marked-block body extraction that mirrors `fleet_lanes/marked_block.py` | Implementer 2 (checks) |
| `checks_hooks.py` | `hooks` checks at L0:  registration, hook ids and timeouts in harness settings files | Implementer 2 (checks) |
| `checks_mcp.py` | `mcp` checks:  registered server names per platform config format | Implementer 2 (checks) |
| `checks_skills.py` | `skills` checks:  installed skill names per platform | Implementer 2 (checks) |
| `checks_subagents.py` | `subagents` checks:  installed sub-agent definition names per platform | Implementer 2 (checks) |
| `checks_permissions.py` | `permissions` checks:  permission identifiers, file modes and credential `stat` (never contents) | Implementer 2 (checks) |
| `proof.py` | L1 sentinel probes and L2 headless canaries, per-platform adapters, TTL evidence handling | Implementer 3 (proof) |
| `collectors.py` | Collector dispatch:  `mac` runs locally;  reserved collectors return the pinned `UNSUPPORTED` result | Implementer 3 (proof) |
| `aggregator.py` | Runs every rule through collectors, checks and proof;  applies the section 4 mapping, the clock, staleness and timeouts;  builds the `StatusReport` | Implementer 4 (engine) |
| `cache.py` | The last-known-good copy of the latest report, mirroring `fleet_knobs/lkg.py` trust rules (0700 dir, 0600 file, atomic rename, age cap) | Implementer 4 (engine) |
| `plan.py` | The read-only plan side:  desired versus observed per rule, with the exact change apply would make | Implementer 4 (engine) |
| `apply.py` | The write side:  only rules with `autofixAllowed` true, only via the existing installer engines, backup then atomic replace, never a hand-rolled file edit | Implementer 4 (engine), after plan |
| `__main__.py` | Argument parsing and verb dispatch only;  no check logic | Implementer 4 (engine) |
| `touchid.py` | The Touch ID gate in front of persistence-class writes:  it runs a signed helper and refuses the write when the confirmation does not succeed | Implementer 4 (engine) |
| `touchid_helper/` | The small signed helper binary the Touch ID gate runs;  built by `build.sh`, never resolved from the environment | Implementer 4 (engine) |
| `tests/` | Unit and contract tests per module, mirroring `fleet_knobs/tests/` | Each implementer for their module |

Coordination rules:  `types.py` and `contract.py` change only through the design, never inside a check module's lane.  A check module returns `CheckResult` values and never prints, never exits and never reads the clock (the aggregator passes `checked_at` in).  `apply.py` calls the existing engines under `scripts/fleet_lanes/` as subprocesses, never as libraries, so the isolated copy keeps working (section 12), and it adds no third installer.  Nobody edits `scripts/fleet_lanes/`, `scripts/fleet_knobs/`, `fleet-apps.json` or `scripts/check-fleet-registry.py` from a `fleet_config` lane.

## 11. CLI Surface

Run from `scripts/`, as `python3 -m fleet_config` (packaged later as `fleet-config`):

```
python3 -m fleet_config status [--json] [--seat TAG] [--platform ID] [--prove l2]
python3 -m fleet_config plan   [--json] [--seat TAG] [--platform ID]
python3 -m fleet_config apply  [--json] --only RULE-ID [...]
```

`status` is read-only and runs L0 plus L1.  `plan` is read-only and shows what `apply` would change.  `apply` requires at least one `--only` rule id, refuses a rule whose `autofixAllowed` is false, and refuses to run without a fresh `status` or `plan` to act on.  `--seat` takes a registry tag and `--platform` a platform id;  both filter, and an unknown value is a usage error, never an empty pass.

A CLI that folds the plan into the apply side serves the same contract with `apply --plan`, which prints the plan and changes nothing, and addresses the work by seat and kind instead of by rule id.  That is one surface and one behaviour, not two:  the plan is always available and never changes anything, and the apply side never touches a rule whose `autofixAllowed` is false.  Which spelling ships is a decision for the owner, because the two spellings differ in how a caller names the work, and the envelope and the manifest do not care.

## 12. Style, Packaging And Safety Rules

Stdlib only, Python 3.9 safe, type hints, a docstring on every public symbol, and two spaces between sentences in every docstring, comment and document.  No `Any` where a precise type is obvious.

Like `fleet_knobs`, this package must work as a stable copy run with `python3 -I` carrying just `scripts/fleet_config/`.  That is why it imports nothing outside the stdlib and never imports `fleet_lanes` at runtime:  shared facts (the registry roster, the platform id list, the marked-block hash) are read as files or passed in as data, and a parity test pins each mirror to its source.  The alternative, importing `fleet_lanes.layout` and `fleet_lanes.marked_block` directly, is simpler today and is rejected because it silently breaks the isolated copy that hooks and LaunchAgents run.

Safety rules, non-negotiable:  no secret value is ever read into a result, a log or a plan (credential files are `stat` only);  `status` and `plan` write nothing outside `cache.py`'s state dir;  `apply` writes only through the existing installer engines, with their backup and atomic-replace discipline;  and every subprocess this package starts goes through one allowlisted runner with a timeout, on the `fleet_lanes/doctor.py` precedent, so a hung harness cannot hang the run.

That runner covers the apply side too, not only the checks:  the MAC level doctors, the secret-store listing and the installer engines the apply side drives all start through it, so a hung installer is a failed step rather than a blocked command, and a command that is not on the allowlist is refused before any process exists.

## 13. Testing

Each implementer lands tests with their module, under `scripts/fleet_config/tests/`, in the `fleet_knobs` style (temp dirs, fake executables on `PATH`, no network, no real home).  Four contract-level tests are pinned by this design and are not optional,  and they live together in `scripts/fleet_config/tests/test_contract_pins.py` so that a reader who greps for a pinned name finds it:

1. `test_schema_version_is_pinned` asserts `contract.SCHEMA_VERSION` is exactly 1 and that a built envelope carries it.
2. `test_envelope_never_carries_a_secret` builds a report over a credential rule and asserts the serialized envelope holds the mode, size and mtime, and no file contents (the fixture writes a known sentinel string as contents and asserts its absence).
3. `test_fail_closed_mapping` asserts stale, error, timeout and unparseable results are failing and that an all-`UNKNOWN` run and an empty run both verdict `UNKNOWN`, never `PASS`.
4. `test_marked_block_hash_parity` asserts `checks_instructions` and `fleet_lanes.marked_block` give the same digest for the same fixture file (this test may import `fleet_lanes`;  the package under test may not).

The names are the pin, not a suggestion:  a test that asserts the same thing under another name does not satisfy it, because the pinned name is what a reader greps for and what a future change has to find.  Test 4 in particular is the only thing that keeps the two marked-block implementations agreeing, and nothing else in the suite does it, so it is the one most easily lost.

## 14. Decisions And Rejected Alternatives

| # | Decision | Why | Rejected alternative |
|---|---|---|---|
| D1 | The manifest is a sibling file `fleet-seats.json`, keyed by registry tag | `fleet-apps.json` is the identity registry with its own owner and checker;  desired state churns at a different rate | Extending `fleet-apps.json` with a per-seat config block, rejected because two workers would own one file and the registry checker would need to learn config semantics |
| D2 | `schemaVersion` and `SCHEMA_VERSION` are the integer 1 | Matches `fleet_knobs` (`schema: 1`) and the lane doctor (`SCHEMA = 2`);  an exact integer is trivially pinned by a contract test | A semver string such as `"1.0"`, rejected because nothing in the fleet versions JSON contracts that way and a string invites range matching that hides a breaking change |
| D3 | The envelope is one flat `results` list;  grouping is a consumer view | One canonical list cannot disagree with itself;  the SwiftUI app groups by seat, platform and kind for free | Nested seats-then-platforms-then-results, rejected because summary counts and per-rule lookups then need a second traversal and a second source of truth |
| D4 | Stale, error, timeout and unparseable map to `UNKNOWN` with a failing reason code, not to a seventh state | The six-state vocabulary is fixed by the task and by the app;  the reason code carries the failing distinction without widening it | Reusing `DRIFT` for a failed check, rejected because drift is a positive finding about config, and a broken checker must not accuse a seat of drift it did not observe |
| D5 | A seat with no Mac surface gets `notApplicable` covers and no results, not `UNSUPPORTED` results | It is honest (nothing is checkable yet), it keeps the daily verdict clean, and the app renders the covers from the manifest | One `UNSUPPORTED` result per kind for cloud seats, rejected because a permanent wall of unsupported rows trains everyone to ignore the state that a genuinely unsupported Mac platform needs |
| D6 | Only `hooks` rules may require `L1` or `L2` in Phase 2 | L1 and L2 are hook proofs;  a higher requirement anywhere else would make `UNPROVEN` the permanent state of five kinds | Allowing any kind to require any level, rejected because no proof machinery exists for the other kinds and the manifest would promise what the engine cannot run |
| D7 | This package mirrors `fleet_lanes` semantics (roster, marked-block hash) instead of importing it | The isolated stable copy under `python3 -I` carries just this package, as `fleet_knobs` established | Direct imports of `fleet_lanes.layout` and `fleet_lanes.marked_block`, rejected because they break that copy silently, at hook time, on the worst possible day |
| D8 | Credential checks never read a secret's contents:  `credential_stat` reports `stat` data only, and `credential_key_present` asks the secret store for key NAMES only | A hash of contents is a fingerprint of a secret and needs the file opened;  mode plus presence, or the presence of a key name, is the compliance fact Muster needs | Hashing the credential file to detect rotation, rejected because presence and mode answer the admin question and rotation is the credential owner's business, not a config drift |

## 15. Risks And Blockers For Other Workers

Read this before starting a lane.  These are the places where the contract meets facts this kernel did not own.

1. **No registry maps a seat tag to a platform id.**  `fleet-apps.json` `seatApps.bySeat` uses app slugs (`claude-code`, `grok-terminal`, `minimax-code`) that are a different vocabulary from the `install_rules.py` platform ids (`claude`, `grok`, `minimax`).  The seed manifest therefore hand-maps each seat to its platform ids once, in `fleet-seats.json` itself, and the loader can only validate that the ids are known, not that the pairing is right.  Whoever seeds the manifest must verify each pairing against the harness the seat actually runs, seat by seat.
2. **Expected hashes must come from the installer source, not from installed files.**  If the seed manifest snapshots whatever hash is on this Mac today, drift detection is circular and proves nothing.  The `instructions` seed hashes must be computed from what `fleet_lanes.install_rules` composes (its rules sources), and the seeding task needs a small hash helper from `checks_instructions.py` to do it.  Until that helper lands, the manifest cannot be seeded honestly.
3. **`fleet-seats.json` does not exist yet, and this kernel does not create it.**  `load_manifest()` correctly refuses without it.  Seeding it (every live seat, six kinds each, per section 7) is a separate task that needs the seat-to-platform mapping from risk 1 and the hashes from risk 2.  The Muster app phase must not hardcode a seat list while it waits.
4. **Hook deny signals differ per platform and are not yet catalogued in one place.**  L1 needs, per platform, the hook invocation shape and the exact deny signal (exit code and output).  `proof.py`'s implementer must extract these from each platform's installed hook config and pin them as fixtures.  Any platform whose deny signal cannot be determined gets `UNSUPPORTED` for L1 (its hooks rules then cap at `UNPROVEN` if they require L1, so such a rule should require `L0` until the adapter lands).
5. **Reserved collectors produce `UNSUPPORTED`, and there is no `api` collector design yet.**  A later phase that wants cloud seats (risk:  they are `notApplicable` today) must design the `api` collector and replace those covers with rules.  Nothing in Phase 2 should pre-build that transport.
6. **AFC-375's `deploy/mac/manifest.toml` is not in this repo yet.**  The section 2 boundary is written against its planned shape.  When AFC-375 lands, one reviewer must check both manifests for a doubly described row, because neither loader can see the other's file today.
