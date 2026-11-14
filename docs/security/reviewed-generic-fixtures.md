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

## Congress.Trade origin-lock test candidate, source-owner review November 14, 2026

PROPOSAL ONLY. This record grants no publication, installation or admission authority.

- Repository: `Simple-With-Us/Congress.Trade`, live repository ID 1275616664.
- Path: `app/src/security/__tests__/originLock.test.ts`.
- Candidate full-file SHA-256: `cc1f0a0d5565309e9268ecd4e37d15d09c5c2e85f4de3cd54638be87bee555c5`.
- Size: 13,250 UTF-8 bytes, 363 lines.
- Source base: `63c22bb447e74e15c68f6b2fed814ba7545c04c4`.
- Flagged declaration provenance: `c862d5c0f61e521368ea47af76eb9f06d2bbda45`.

The CT source owner reports local complete-file structural/import/call review,
literal provenance and dataflow review against the unchanged base. Its one raw
generic assigned-secret hit is a quoted SECRET declaration at line 21, length 23.
The value is deliberately absent from this record. It is used only in constructed
Request headers and OriginLockConfig.secret acceptance/rejection tests, fake Env
configuration and in-process Hono.request calls. No credential read, provider or
network-derived dataflow was found. Framework/local imports and two source-wiring
filesystem reads were inspected. The repair changes wiring assertions at lines
357-361, not this declaration. Pre-existence alone is not a waiver.

The source owner reports exhaustive installed-pattern enumeration: one generic
assigned-secret hit, no other generic, dedicated-provider, private-key, JWT,
bearer-header or Zulip-shaped hits. Own WISE caller bearer was absent; no other
credentials were loaded, so this is not a claim about other live credentials.
The four-file packet remains frozen and unmodified. This proposal does not grant
clearance to that packet or correct its original wrong-target arguments.

Only the stated intended repository/path/full digest is proposed for generic
assignment review. `Simple-With-Us/Congress-Trade` is not admitted. Changed bytes,
other paths/repos, prose and unrelated tools gain no admission. Dedicated shapes,
Zulip-shaped keys and loaded values remain absolute. Client and relay must remain
byte-identical and pass normal review/check gates before any later qualified
release. No live registry is changed by preparing this source proposal. Independent source-owner local candidate checks passed with network denied,
no main/installation and own WISE caller bearer in the known set: exact intended
repo/path/content returned None; original wrong Congress-Trade repo and a
one-byte-different in-memory input both remained assigned-secret refusals. The
actual candidate digest was unchanged. Verdicts only were returned. This
preparer has not received, read or scanned the frozen file or its value.
