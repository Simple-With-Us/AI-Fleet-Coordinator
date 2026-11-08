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
