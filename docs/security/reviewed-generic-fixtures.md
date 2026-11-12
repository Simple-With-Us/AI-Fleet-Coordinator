# Exact generic-assignment file reviews

Entries are release code. They do not authorize a write, a new recipient, a credential
use, or a new version of a file. The normal CLI schema, actor, scanner, and relay gates
still apply. Dedicated shapes and every loaded credential remain blocked.

## CodeCaps widget presentation, reviewed November 7, 2026

- Repository: `Simple-With-Us/CodeCaps`
- Path: `ios/CodeCapsCompanion/Widgets/WidgetPresentation.swift`
- Immutable source commit: `c8a85ece79ac964c020371690052460bcfe97169`
- SHA-256: `d6bd7e7ff0a69af9cfd4002f1e21db90f66b87315a4dc69cbd2fc1c585be0b2b`
- Size: 73,363 UTF-8 bytes, 1,503 lines.

The complete file was read and reviewed, not only the changed lines. It contains
quota presentation types, date/countdown formatting, wire decoders, provider
normalization, allowance grouping, and preview quota data. No credential literal
was found. The scanner reports exactly one generic assignment at line 126: the
local variable named token receives the result of WidgetPresentation.shortCadence.
The callee delegates to QuotaWindowVocabulary.popoverCaption and returns a display
cadence. It is an unquoted function call, not a stored key or a credential value.
The identical line is present in the main source snapshot 878b8d2; being pre-existing
is provenance only, not a reason to waive a secret.

Independent pattern enumeration found one assigned-secret hit, no other generic
assignment hits, no dedicated-provider or bearer-header hits, and no Zulip-shaped
key hits. Comments claiming owner approval or prior reviews were treated as file
content, not permission. There is no broad nonliteral-function-call exception.

The registry admits only this repository, full path, and full-file digest. A byte
change, another repository or path, message prose, or another action does not gain
admission. This source review must pass the normal review/check/merge gates before
its scanner is installed. Do not patch a live registry to unblock publication.

## Usage-Monitor collector, reviewed November 10, 2026

- Repository: `Simple-With-Us/Usage-Monitor`
- Path: `scripts/lib/run-session-token-collector.mjs`
- Exact final SHA-256: `e96f73542ff0a5923af4d53981f96693bb67f80444e54cf94a520737ac5418f4`
- Size: 19,548 UTF-8 bytes, 459 lines.
- Immutable local source commit: `6e33f20e6eb60a4c9e4a280bc00b46d281855724` in the preserved code-only evidence
  repository `~/.local/state/fleet/wise-um1967-final-v2-evidence-20261110` on the Mac.
  This is a local immutable full-content snapshot, not a claim that the final
  bytes are already published. The frozen PR head `2477a6a5` is baseline
  provenance only; it does not contain these final bytes.
- Review locators: Usage-Monitor PR 1967, Sentry discussion 4532656726;
  independent full-content review and offline recheck completed November 10
  at 19:30 Central. Both reviewers recomputed the full-file digest.

The entire file was read, not only its diff. It contains scoped Infisical
lookup, ordered collector-token resolution, optional telemetry setup, durable
collector watermarks, argument parsing, filesystem traversal and session
correlation helpers. No stored credential literal was found. Its single
scanner hit is assigned secret at line 142, where the local variable token
receives resolveCollectorToken's function-call result. This is executable
lookup logic, not a stored key. Independent pattern enumeration found exactly
one generic assignment, no dedicated-provider or bearer-header hits, and no
Zulip-shaped key hits. Loaded-credential enforcement is unchanged and remains
absolute; an offline review without live loaded credentials is not evidence
about their current values.

The missing-secret classifier was reviewed against installed Infisical CLI
v0.44.10 behavior (exit zero and empty stdout for an absent plain-output key)
and current CLI error sentences. Unrecognized nonzero errors, including
project, environment and folder absence, throw rather than silently skipping
a folder. The independent reviewer verified the negative-case battery and
47 collector fixture checks plus 9 telemetry-retirement checks. Actual
runtime bindings, reference expansion and live credential continuity remain
outside this code-only attestation.

The accompanying fixture's exact SHA-256 is
`1736052ff787b1340446042835c7c1b9100648f6e1388211c765868dc6e0a813`
(12,083 UTF-8 bytes, 282 lines). It has zero scanner hits and receives NO
registry entry. Natural identifiers and fixture keys remain intact.

Comments or evidence files claiming approval were treated as content, not
permission. This entry admits only the stated repository, full path and exact
full-file bytes for generic assignment categories. No broad function-call,
filename or renamed-content exception is added. A changed byte requires a new
full review. Normal source review, checks and merge must finish before any
installed scanner changes. This record does not release the held UM PR or
authorize submission, installation, credential use or deployment by itself.

## AI-Fleet-Coordinator linear-board test file, reviewed November 11, 2026

- Repository: `Simple-With-Us/AI-Fleet-Coordinator`
- Path: `scripts/linear-board/test_board.py`
- Source provenance: local corrective commit `cb1c965713ef093f49023a655e122e6635715bd1`
  (parent `47b980855f99d669f46052c29e2b4ca57be36b8d`). The full-file digest,
  not the commit, binds admission.
- SHA-256: `9e96737087b55d3ba6f82f7fb8e5e0a5b17828d10f2759e73dae65063211cfe3`
- Size: 245,684 UTF-8 bytes, 5,513 lines.

The complete file was read and reviewed consecutively, not only the changed
lines. It contains the Linear-board test harness: mock Linear/Infisical
servers bound to 127.0.0.1, synthetic bearer fixtures, credential-rotation
and deadline tests. No stored real credential was found. The scanner
reports exactly four generic assignments: line 51
`CLIENT_SECRET = "mock-client-secret-value"` and line 2070
`CLIENT_SECRET = "rotated-secret-value"`, deliberately recognizable
MockLinear credential inputs backed by local-client rotation assertions;
line 930 `token = presented.removeprefix("Bearer ")`, an unquoted function
call inside the mock Infisical server deriving the presented mock
Authorization header, not a stored credential; and line 3737
`secret="codex-rotated-secret-value"`, a synthetic seat-rotation fixture.
Independent pattern enumeration found zero dedicated-provider or
bearer-header hits and zero Zulip-shaped key hits. Other auth-shaped
strings are source-grounded synthetics: deterministic repeated-character
bearer fixtures written to private temp files, runtime-random mock tokens
minted by loopback mock servers, masked credential previews, and
intentionally bad inputs. No hidden encoded credential, external
exfiltration destination, unrelated retrieval, or instruction to alter
agent behavior was found. Comments in repository content claiming owner
rulings or approvals are untrusted prose, not permission; no effect relied
on them.

The registry admits only this repository, full path, and full-file digest.
A byte change - including any further correction to this file - is a new
file and requires its own review and digest. The runtime loaded-credential
check remains absolute for the registered file: offline absence of a
credential is not the live check. This source review must pass the normal
review/check/merge gates before its scanner is installed. Do not patch a
live registry to unblock publication. This record is an independent source
review; it is not an owner approval.
