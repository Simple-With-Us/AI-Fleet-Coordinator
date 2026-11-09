# Muster Server Route:  The `--from-muster` Entry

**Spec version 5.6.**  Version 5.6 changes no rule, answer, byte or vector:  it rewords every passage that still described `muster trust-list chain --yes` as unbuilt, listed in the first table below.  BB's chain verb, merged in PR 1543, is on `main` and was deployed to the server on November 8, 2026 at 10:06 AM, so C1 to C5 run as specified;  only the owner's setup steps, which are not done yet, keep the route inert (§4).  Version 5.5 wrote down two things that BB's chain verb already does, listed in the second table below:  the sweep of stale temporary files at C5, and the size guard at P3.  Neither changes what any request gets back, a byte the helper sends, or a vector:  no list within the other limits reaches the size guard, and the sweep removes only files that nothing reads.  Version 5.4 wrote down the strict parsing and the refusal shapes of BB's build of the trust-list chain slice (then not on `main`), from AA's rulings on BB's seven questions and from two independent reviews of that build, listed in the third table below:  every key the 91-byte uncompressed P-256 `SubjectPublicKeyInfo`, canonical base64url, real calendar times and the exact `contract` at P3, which the reference checkers now apply too;  `claimed` and `chain` in a chain refusal, by step;  a signing fault at C5 after the rename;  the chain audit row as best effort;  the trust file's open flags and link count;  and new harness cases.  Each made the spec say what the built helper does where it is stricter or fails closed, which AA allowed in version 5.4.  Vectors T26 and T27 are new, and no earlier vector changes.  Version 5.3 closed, before the trust-list chain slice was built, the gaps that a review for its builder found, listed in the fourth table below:  a reason for a history hash mismatch, the stored list's bytes and file rules, the C4 comparison, the C5 order and its faults, the chain audit row, and a note for the retired T9.  It also settled §17A's A8, said that A7 stays inside rule C, and listed `unit-status` as open.  Two of these added to frozen rules (a reason in §8's check 3 and a condition in rule A), which AA allowed in version 5.3 because the chain slice that applies them was not built then:  the chain verb answered `bad_command`.  Version 5.2 added the requests from EE's Swift-side read of BB's helper PR 1505, relayed by BB, listed in the fifth table below.  They change no rule the helper follows and no byte it sends:  they state the phone's side of rules already frozen (the canonical encoder and parser in Swift, the approval's own times, and what the phone does after each refusal).  Version 5.1 added the settled rulings from the independent review of BB's helper build and the spec requests from BB's final build, listed in the sixth table;  it changed frozen parts only where those rulings directed (the `--yes` step order among them).  Written by the CLAUDE seat (session AA) on November 7, 2026 (versions 5.3 to 5.6 on November 8), from revision 9 of `design.md`, EE's eighth look and BB's answers from building the helper.  Versions 1 to 4 are kept beside it in the scratch folder.  **Frozen:**  everything except the parts marked OPEN.  **OPEN, gating Milestone 5:**  rule C (§8's reopen rule and its rule at non-server verifiers), the reopen record and recovery (§9), the final chain refusal store (§11), `finalRefusals` (§13) and the chain-refusal audit row (§14).  Section 17A lists what is wrong with them, unsolved.  Milestones 1 to 4 build only on frozen parts.  Any other change to a frozen part is version 6 and needs a new review.  Where this spec and the design differ, this spec wins.

## Changes In Version 5.6

From BB's chain verb as merged in PR 1543 (`scripts/muster_approval.py` on `main`) and deployed to the server on November 8, 2026 at 10:06 AM, written down by AA on November 8, 2026.  Neither row changes a rule, what any request gets back, a byte the helper sends, or a vector.

| # | Change | Where |
| --- | --- | --- |
| 1 | The chain verb is built:  the passages that said `muster trust-list chain --yes` answers `bad_command` at P2 "until the chain slice lands", or that §5's chain framing, C1 to C5, the chain refusal body and the chain audit row take effect "once" it lands, now say what the helper on `main` does.  §16's harness case for that `bad_command` becomes the misspelt chain commands the helper refuses `bad_command`, and its chain vectors say that the helper's own tests run them.  | §0, §4, §5, §12, §13, §14, §15, §16 |
| 2 | What keeps the route inert today, stated once:  the owner's setup steps are not done (no `/etc/muster` pin files, no helper key, and no `muster-exec` key with its forced command), so nothing reaches the route over SSH, and a `--from-muster` request started any other way answers the unsigned `helper_key_missing` at P0.  | §4 |

## Changes In Version 5.5

From BB's chain verb as merged in PR 1543 (`scripts/muster_approval.py` on `main`), written down by AA on November 8, 2026.  Both rows say what that helper already does;  neither changes what any request gets back, a byte the helper sends, or a vector.  With PR 1543 the chain slice has landed:  where this spec says what holds "until the chain slice lands" (`chain --yes` answering `bad_command` at P2, in §0, §4, §5 and §12 to §16), it describes the helper before PR 1543, and what it says takes effect "when" or "once" the slice lands (§5's chain framing, C1 to C5, the chain refusal body and the chain audit row) is what `main` does now.  Rewording those passages for the landed slice is left to a later version;  it changes no rule.

| # | Change | Where |
| --- | --- | --- |
| 1 | The sweep at C5:  under `ops-control.lock` and before the temporary file is made, the helper removes stale `.muster-trust.json.<16 lowercase hex>.tmp` entries in the state directory that are regular files of its account with one link, ignoring every error and touching nothing else.  | §8, §12, §16 |
| 2 | The size guard at P3:  an entry whose canonical bytes exceed the 65,536-byte stored-file limit is `bad_input`;  no list within the other limits (64 devices, 64 countersignatures) can reach it, so it is defence in depth.  The reference checkers apply it too.  | §5, both scripts |

## Changes In Version 5.4

From BB's build of the trust-list chain slice (BB's lane `trust-chain`, not on `main` yet), the seven questions BB asked from it, and two independent reviews of that build, settled by AA on November 8, 2026.  Every row makes the spec say what the built helper does where it is stricter or fails closed.  Nothing here needs a change to the helper on `main`, whose chain verb still answers `bad_command` (§4):  nothing there writes `muster-trust.json`, so its readers never meet a replaced file.

| # | Change | Where |
| --- | --- | --- |
| 1 | P3's strictness, stated, and applied by the reference checkers so that a list they print is never one the helper refuses:  (a) every key is the 91-byte DER `SubjectPublicKeyInfo` of an uncompressed P-256 point, so a compressed or hybrid point is `bad_input`;  (b) base64url is canonical, with no padding and no stray trailing bits:  in a key `bad_input`, in an owner signature `owner_signature`, in a countersignature `countersignature`, and in an approval's signature `bad_signature`;  (c) every time is a real calendar time, so `23:59:60Z`, February 30 and year `0000` are `bad_input`;  (d) `body.contract` is exactly `muster-trust-list/1`, else `bad_input`.  Vectors T26 and T27 print the two signature cases;  the others are harness cases, which `make_vectors_v5.py` and `check_vectors_v5.py` each assert.  | §2, §5, §8, §16, both scripts |
| 2 | A chain refusal at P1 or P2 (`pin_unsafe`, `bad_command`, and `mode_not_allowed` for `chain --plan`) carries `claimed` `{}` and no `chain`;  from P3 on it carries `chain` (`null` except at C3) and no `claimed`.  | §13, §16 |
| 3 | A signing fault at C5 after the rename:  if it repeats when the refusal is signed, the answer is the unsigned `helper_key_missing`, because the refusal needs the same key;  a fault that does not repeat gives the signed `internal_error`.  Either way the list is held.  | §12, §15, §16 |
| 4 | The chain audit row is best effort:  a row means the list was durable, not the reverse.  There is no row when only the directory `fsync` fails or the process dies between the rename and the append, and `current --plan` is authoritative for what is held.  | §12, §14, §16 |
| 5 | The state files, `muster-trust.json` included, are opened with `O_NONBLOCK`, so a FIFO in a file's place cannot hang the open;  the temporary file, made new by `O_CREAT` with `O_EXCL`, needs none.  Every state file needs exactly one link, except that the read of the trust file also accepts none, because an unlocked reader can `fstat` the file just after a chain's rename replaced it;  the helper key, which is never replaced by a rename, keeps exactly one.  This changes v5.3's "exactly one link" for that one read only.  | §8, §11, §16 |
| 6 | A first list with `closed` false and `devices` empty is refused `closed_rule` (check 7:  `closed` is true exactly when there is no device), as a harness case.  | §16 |
| 7 | A held list that fails its history line at C2 is `state_unsafe` with `helperTrustListVersion` `null`, as a list that could not be read is.  | §12, §13, §15 |

## Changes In Version 5.3

From a review of the chain slice's readiness for its builder (gaps G1 to G6) and §17A's A7 and A8, settled by AA on November 8, 2026, and one open item from BB.  The helper as built changes in nothing:  each rule here is the chain slice's, which is not built yet, or wording.

| # | Change | Where |
| --- | --- | --- |
| 1 | Check 3 also requires, when an entry's version is a history line's `toVersion`, that the entry's body hash equals that line's `toSHA256`;  a mismatch is the new `chain_refused` reason `history_hash`.  At C2, a held list that fails the same comparison is `state_unsafe`.  Vector T24, under a rotated history.  | §8, §10, §12, §15, §16 |
| 2 | What `chain --yes` stores:  the canonical bytes of the newest accepted entry (the whole signed object) with no trailing line feed, so the file's SHA-256 is that entry's `entrySHA256`;  the temporary file's open flags, mode, name and cleanup;  readers keep accepting any formatting.  §11's read checks name exactly one link.  The storage paragraph's "OPEN-5" label says it was settled in version 3.  | §8, §11 |
| 3 | C4 refuses `trust_list_changed` unless the stored list is the one C2 read:  the same body hash, or none when C2 found none.  Y7 is worded to match, as the helper already does it.  | §12 |
| 4 | C5's order and faults:  the entry is written durably, then the audit row is appended, then the signed `ack` is answered.  A failure before the rename is `internal_error` and nothing is stored;  a failure after it is `internal_error` with the list held, so the same chain sent again is `version_gap`;  a failed audit append after the rename is not a fault.  | §12, §14, §15 |
| 5 | The chain audit row's verb, target, result and fields are named.  | §14 |
| 6 | The chain table says that T9 was retired and that a vector's number is never reused.  | §16 |
| 7 | §17A's A8 is settled:  rule A refuses a list that names neither server key when the previous list named one (`server_keys_removed`), as rule B does.  Vector T25.  A7's fix, when §17A is settled, goes into rule C only.  | §8, §16, §17A |
| 8 | `unit-status` on the `--from-muster` route is listed as open (from BB).  | §17B |
| 9 | §16's cases without fixed bytes gain the chain slice's storage, C2, C4 and C5 cases.  The test fixture now carries every printed list and, for each chain vector, its held list, chain, history, whether it is a rule C row, the answer a Milestone 1 to 4 helper must give, and the proposal's answer.  | §16 |

## Changes In Version 5.2

Every row is from EE's Swift-side read of BB's helper PR 1505 (the `--from-muster` entry), relayed by BB, and is made normative here.  None changes what the helper does or what it sends.

| # | Change | Where |
| --- | --- | --- |
| 1 | Swift writes canonical bytes with a hand-written canonical encoder (keys sorted by byte value, so `B` before `a` and `a10` before `a9`), never `JSONEncoder` or `.sortedKeys`, and reads with a strict parser that refuses duplicate keys, floating-point numbers, a boolean where a number belongs (or the reverse) and non-ASCII strings.  Open item 4 is settled.  | §2, §13, §17B |
| 2 | The approval's `issuedAt` is not tied to the signed plan's `issuedAt`:  the helper compares it only with its own clock, so the phone sets it from its estimate of the server's clock (its own clock plus the offset seen at the plan answer) and sets `expiresAt` at most 120 seconds later.  Copying the plan's `issuedAt` would make any Plan Review longer than 120 seconds sign an approval that has already expired.  | §7, §13 |
| 3 | What The Phone Does Next, a new table:  a verified `plan_mismatch` has spent its nonce (plan again and sign again);  `expired`, `not_yet_valid` and `window_too_long` mean sign a new approval;  a verified `state_unsafe` or `internal_error` is treated as maybe spent, because the answer does not name its step and an `internal_error` from Y8c may leave the nonce on disk.  | §6, §15 |
| 4 | A ledger over 4 MiB is `ledger_corrupt`, not 2 MiB:  twice the most a genuine ledger can hold, so a later change to the cap or the retention cannot make it read as corrupt (BB's build).  | §11, §16 |
| 5 | A failed close of the ledger after its `fsync`, and a failed prune rewrite, are not faults:  the append is durable and the prune is retried by the next approval (BB's build).  | §12 |

## Changes In Version 5.1

From the independent review of BB's build, which used version 5 on `main` as the authority.  The rulings are settled.  Rows 8 to 12 are BB's spec requests from his final build (head `ee55ce811`), made normative here;  his other requests were already in this version (rows 1, 4, 5 and 7).

| # | Change | Where |
| --- | --- | --- |
| 1 | An acknowledgement with nothing held answers `closed: false`, `signedByCurrentKey: false` and both nulls;  every verifier branches on a null `helperTrustListVersion` first, and the rotation proof needs that version to equal K.  | §10, §13 |
| 2 | A refusal's `helperTrustListVersion` is informational, never coverage:  `no_trust_list` with version K means a list exists and is not usable.  | §13 |
| 3 | A stale resource-policy revision is `plan_refused` with reason `revision_stale`, not `internal_error`.  | §4, §12, §15 |
| 4 | The `yes` rate step moves from Y1 to Y8b, under `ops-control.lock`, between the ledger read (Y8a) and the nonce append (Y8c), so only authenticated approvals that spend a nonce use the `yes` budget.  | §11, §12, §14, §15 |
| 5 | Outcome `unknown` covers a Coolify GET sent without an observed answer;  the muster plan's redeploy `auditTarget` turns every code point outside printable ASCII into one `?`.  | §4, §13 |
| 6 | The chain verb is not built yet:  `chain --yes` answers the existing `bad_command` (as BB's final build does;  no new code is added), so the helper holds no list and every approval answers `no_trust_list`.  | §0, §4, §5, §12, §14, §15, §16 |
| 7 | The rate clamp the build already does is written down:  an event dated after `now` counts as made at `now`, so a clock stepped back never holds a key.  The rate file's checks gain the 128 KiB size limit, the 300-character key limit and known classes.  | §12 |
| 8 | The ledger's retention:  a line may be dropped only when it is beyond the last 4,096 lines and its `expiresAt` is more than seven days before `now`, so the clock alone never makes a nonce forgettable;  a ledger over 2 MiB is `ledger_corrupt`;  the key is the `approvalNonce` alone.  | §11, §16 |
| 9 | The `plan_refused` reasons are a closed list:  the planner's own `error_code`, which gains `update_route_changed`, `receipt_context_mismatch` and `receipt_context_unavailable`, else `allowlist_miss`, `report_stale` (every way the status report can fail, named) or `revision_stale`.  | §4, §15 |
| 10 | A fault after Y8c is `unknown` when the action had begun or may have, and `failed` only when it provably never began or ended in a definite failure;  its audit row carries the same word, and its `detail` is one of three fixed words.  | §12, §13, §14, §16 |
| 11 | A refusal's `helperTrustListVersion` is the version this request actually read (Y2, R3 or K2) and nothing more, so a `plan_refused` at R2 carries `null`;  the acknowledgement with nothing held carries `finalRefusals: []`.  | §13 |
| 12 | A high-S signature is accepted;  it cannot be used twice, because the nonce is spent.  The phone need not make low-S signatures.  | §6 |

## 0.  What Changes For BB's Helper Slice

Since version 4, from BB's answers and EE's eighth look:

| # | Change | Where |
| --- | --- | --- |
| 1 | The exact plan commands and `auditTarget` per verb, as `plan_request` on `main` builds them;  `argv[0]` is resolved on the host, so the app signs the server's own plan hash.  | §4 |
| 2 | Plan-time refusals have a code, `plan_refused`, with a reason.  | §4, §12 |
| 3 | An approval fails closed with `clock_unsynced` when `adjtimex(modes=0)` fails, returns `TIME_ERROR`, reports `STA_UNSYNC`, or reports `maxerror` above 1 second;  `--plan` is unaffected.  | §7, §12 |
| 4 | A fault before the nonce is spent is `internal_error`;  after it, a fault is a signed result with outcome `failed` (v5.1:  `failed` or `unknown`, §12 Faults).  Outcome `unknown` is added.  | §12, §13 |
| 5 | The window is compared exactly on the float clock, with no grace.  | §7 |
| 6 | Rate-limit keys are `verb target`;  the boundaries are stated.  | §12 |
| 7 | The state directory is checked at P0.  | §11 |
| 8 | Audit fields named, including `updateRoute` and the ledger-repair row.  | §14 |
| 9 | Root readers of state files check the file's expected owner, not their own uid.  | §10, §11 |
| 10 | An empty reopen record is accepted as no lines;  how the shared module is held equal is stated.  | §9, §10 |
| 11 | The rule table's "When" column is exclusive;  §15 no longer lists `trust_list_changed` at C3;  refusals carry `helperTrustListVersion` or `null`.  | §8, §13, §15 |
| 12 | **Wait for §17A before building:**  rule C's record conditions, the final-refusal store, `finalRefusals` and the chain-refusal audit row.  Until then the chain verb (built, v5.6) answers a rule C entry `chain_refused` with reason `reopen_unavailable` (non-final), records no refusal, and every acknowledgement carries `"finalRefusals":[]`.  | §8, §11, §13 |

Unchanged:  the command line, the verbs and modes, the approval text and its fields, the ledger, OPEN-1 to OPEN-5 as settled in version 3, and the check order.  The approval vectors keep their meaning, but their bytes change because the illustrative plan now has `argv[0]` as the helper resolves it on this server (`/usr/bin/systemctl`, for the user-scope `fleet-server-status.timer`).

## Changes From Version 4

1. **Recovery machinery marked OPEN** (§8 rule C, §9, §11, §13, §14), with EE's eighth-look majors and minors listed unsolved in §17A.  Milestones 1 to 4 do not use it.
2. **BB's build answers made normative:**  plan commands (§4), audit fields (§14), the clock (§7, §12), `plan_refused`, `internal_error`, outcome `unknown`, rate keys and boundaries, the exact window, the state directory, the empty record, and module equality.
3. **Root readers** (`repin`, `trust-status`) check each file's expected owner (§10, §11), and Muster signs nothing with the old key during a rotation (§10).
4. **Rule table** made exclusive (§8);  **§15** typo fixed;  **refusal bodies** say what `helperTrustListVersion` is when nothing is held (§13).
5. **Vectors:**  the illustrative plan uses `/usr/bin/systemctl`, so every approval vector was re-signed with the same outcomes;  the rule C and recovery vectors are kept as illustrations of the OPEN proposal.

## 1.  Scope

This spec covers what the root-owned helper `/usr/local/lib/fleet-resource-control/fleet-ops-control.py` does when started as `--from-muster`, and the trust-list rules every verifier shares.  It does not change the `--from-ssh` entry, its parser or its behaviour.

## 2.  Conventions

| Item | Rule |
| --- | --- |
| Times on the wire | UTC, `YYYY-MM-DDTHH:MM:SSZ` exactly.  |
| Binary in text | base64url (RFC 4648 section 5), no padding, and canonical:  no stray trailing bits, so re-encoding the decoded bytes gives back the same text (v5.4, §5).  |
| Public keys | P-256 public keys as base64url of the DER `SubjectPublicKeyInfo` of the uncompressed point:  91 bytes, so 122 characters (v5.4, §5).  |
| Signatures | ECDSA over P-256, SHA-256 as the message hash, DER `ECDSA-Sig-Value`, then base64url.  |
| Fingerprints | SHA-256 of the DER `SubjectPublicKeyInfo`, 64 lowercase hex characters in 16 groups of 4, compared in full.  |
| Library | `cryptography` from the root-owned system package `python3-cryptography` (46.0.5, verified to sign and verify P-256 under `/usr/bin/python3 -I`, Python 3.14.4), imported only inside the `--from-muster` branch.  |

**Canonical bytes,** defined without reference to any library:

- Values are objects, arrays, strings, integers, `true`, `false` and `null`.  No floating-point numbers.
- Every string, keys included, contains only printable ASCII (`0x20` to `0x7E`).
- Inside a string, `"` is written `\"` and `\` is written `\\`.  Nothing else is escaped;  `/` is written as `/`.
- Integers are base 10, `-` when negative, no leading zeros, no exponent, magnitude at most 2^53 − 1.
- Object members are sorted by key, comparing keys as byte strings, so `B` sorts before `a` and `a10` before `a9`.  Keys are unique.
- A member whose value is `null` is written, never left out.
- No whitespace;  `,` between members and elements;  `:` between a key and its value.

In Python this is `json.dumps(v, sort_keys=True, separators=(",", ":"), ensure_ascii=True)` after checking that every string is printable ASCII.  **In Swift (v5.2),** every producer and verifier writes canonical bytes with a hand-written canonical encoder that writes each member itself, `null` ones included, with object keys sorted by byte value (so `B` before `a` and `a10` before `a9`), never with `JSONEncoder` or `.sortedKeys`, whose key order is not a byte sort.  It reads every signed or hashed object with a strict parser that refuses duplicate keys, floating-point numbers and any string that is not printable ASCII, and keeps `true` and `false` apart from numbers, so a boolean where a number belongs (or the reverse) is refused.  It never reads them with `JSONSerialization`, which reads booleans as numbers, turns integers into doubles and keeps the last of two duplicate keys.  The encoder must reproduce §16's canonical-form vector byte for byte.

**Entry hash.**  `entrySHA256` of a signed list is SHA-256 of the canonical bytes of the whole signed object (`body`, `ownerSignature` and `countersignatures`).  The body hash covers only `body`.  Two entries can share a body hash and differ in their signatures (T15), and ECDSA signatures are malleable, so an entry hash is not a stable name for an owner-signed list (§17A).

Free text that may hold other characters (log lines, read documents) travels as `outputB64`, the base64url of its UTF-8 bytes.  `detail` and `name` are printable ASCII;  the helper replaces any other character in `detail` with `?`.

**Message prefixes:**

| Object | Bytes Signed | Signed By |
| --- | --- | --- |
| Approval | The approval text (§6) | The device's approval key |
| Trust list | `muster-trust-list/1\n` + canonical body | The owner key the history assigns to that version (§10) |
| Countersignature | `muster-trust-countersign/1\n` + the same canonical body | A device's approval key |
| Signed plan | `muster-signed-plan/1\n` + canonical body | The helper key |
| Signed result | `muster-signed-result/1\n` + canonical body | The helper key |
| Signed refusal | `muster-signed-refusal/1\n` + canonical body | The helper key |
| Acknowledgement | `muster-signed-ack/1\n` + canonical body | The helper key (`muster-pub`'s heartbeat uses the same body under its own key) |

## 3.  The Forced Command And The Command Line

```
restrict,from="127.0.0.1,::1",command="/usr/bin/python3 -I /usr/local/lib/fleet-resource-control/fleet-ops-control.py --from-muster" ssh-ed25519 AAAA... muster-exec@server
```

This line goes in `/home/jay/.ssh/authorized_keys`, beside `fleet-ops-ctl`'s.  `--from-muster` must be the only argument.  The helper reads `SSH_ORIGINAL_COMMAND` under the existing `SSH_COMMAND_MAX` (256) and `SSH_SAFE_RE` rules and splits it on single spaces into exactly four words, `SCOPE VERB TARGET MODE`, where `MODE` is `--plan` or `--yes`.  The answer is always JSON.  The branch sets an explicit `require_approval = True` that every shared function it calls must honour;  a shared function called without it behaves exactly as today.

## 4.  Verbs

| Scope | Verb | Target | `--plan` | `--yes` |
| --- | --- | --- | --- | --- |
| `server` | `start`, `stop`, `restart` | A unit the allowlist names | Signed plan | Needs an approval |
| `server` | `logs` | A unit the allowlist names | Signed plan | Needs an approval;  the result carries the log text |
| `server` | `update` | An update the allowlist names | Signed plan | Needs an approval |
| `server` | `redeploy` | A Coolify application uuid the helper's own `server-status.json` lists | Signed plan naming the uuid and the application | Needs an approval |
| `server` | `resource-policy` | The existing `field.value.revision` grammar | Signed plan (takes `broker_lock`, one Infisical read) | Needs an approval |
| `server` | `resource-status` | `coding` | Signed result with the read | `mode_not_allowed` |
| `server` | `seat-inventory` | A platform the module knows | Signed result with the read | `mode_not_allowed` |
| `server` | `update-status` | The existing receipt id | Signed result with the read | `mode_not_allowed` |
| `muster` | `trust-list` | `current` | Signed acknowledgement | `mode_not_allowed` |
| `muster` | `trust-list` | `chain` | `mode_not_allowed` | Accepts a chain (§8, §12's C1 to C5);  no device approval (below) |

A scope, verb or target word not in this table (including the `mac` and `pm2` scopes and `server eavesdropper-status`) is `bad_command`;  a known verb with a mode the table does not allow is `mode_not_allowed`.

**The chain verb is built (v5.6).**  BB's chain verb, merged in PR 1543, is on `main` and was deployed to the server on November 8, 2026.  `muster trust-list chain --yes` reads its chain under §5's framing at P3, then runs C1 to C5 (§12) as specified, and it is the only thing that writes `muster-trust.json`.  `muster trust-list chain --plan` answers `mode_not_allowed`, as the table says, and any other spelling of the command (`muster trust-list chain` with no mode, `--YES`, a fifth word, or a doubled, leading or trailing space) is `bad_command` at P2, before standard input is read, so it is not counted and writes no audit row.  Until a chain is accepted the helper holds no list:  every approval answers `no_trust_list` at Y2, and `muster trust-list current --plan` answers the acknowledgement with nothing held (§13).

**What keeps the route inert today (v5.6).**  Only the owner's setup steps, which are not done yet:  there are no `/etc/muster` pin files (§10), no helper key (§11), and no `muster-exec` key with its forced command (§3).  So nothing reaches the route over SSH, and a `--from-muster` request started any other way stops at P0 with the unsigned `helper_key_missing` (§13), before the pin, the command line or standard input is read.  Once those steps are done, the helper still holds no list until its first chain is accepted.

**Plan commands (from `scripts/fleet-ops-control.py` on `main`, which is authoritative).**  The `--from-muster` branch calls the same `plan_request(ctx, config, scope, action, target)` and hashes what it returns, so these are that function's outputs, read from `main` at `96a7d33f3` (BB confirmed this on November 7).  Numbers in an argv are strings.

| Verb | `commands` | `auditTarget` |
| --- | --- | --- |
| `start`, `stop`, `restart` | `[[SYSTEMCTL] + ["--user"] (only when the allowlist entry's `scope` is `user`) + [ACTION, UNIT]]` (`_systemctl_argv`) | The unit |
| `logs` | `[[JOURNALCTL] + ["--user"] (only for `scope` `user`) + ["-u", UNIT, "-n", "200", "--no-pager"]]`;  200 is `LOG_MAX_LINES` (`server_logs_commands`) | The unit |
| `update`, system scope | `[["/usr/bin/sudo", "-n", "/usr/bin/systemctl", "start", "--no-block", "fleet-cli-git-update.service"]]`:  `update_command` allows only the update named `fleet-cli` with that unit, uses the fixed `UPDATE_SYSTEMCTL` and `SUDO` paths, adds `sudo -n` only when the helper is not running as uid 0, and first probes `sudo -n -l -- <that argv>` | The update's name |
| `update`, user scope | `[[SYSTEMCTL, "--user", "start", "--no-block", UNIT]]`, `UNIT` being the update's unit (`_systemctl_argv`) | The update's name |
| `redeploy` | `[["coolify", "GET", "/api/v1/deploy?uuid=UUID"]]`, the query built with `urllib.parse.urlencode`.  `coolify` is a literal pseudo-command:  `execute()` never runs it, and sends an HTTP GET to the Coolify base URL plus `commands[0][2]`, with the token read at run time and never in the plan | The application's name from the report row, with a trailing ` (SERVICE)` removed when the row names that service, capped at 120 characters;  `coolify resource` when the row has no name;  then, in the muster plan object, made printable ASCII (below) |
| `resource-policy` | Not `fleet-ops-plan/1`:  the `fleet-resource-plan/1` object of §13, with `old` and `new` from `fleet_resource_control`'s `before` and `after` | The field name |

`SYSTEMCTL` is the first of `/usr/bin/systemctl` and `/bin/systemctl` that exists as a file (`_systemctl`), and `JOURNALCTL` the first of `/usr/bin/journalctl` and `/bin/journalctl` (`_journalctl`);  if neither exists, the first is used.  **So `argv[0]` is resolved on the host, and the plan hash is server-local:  the app signs the `planSHA256` that the server's own `--plan` returned (§13's plan check), and never a hash it computed itself.**

**The redeploy `auditTarget` in the muster plan (v5.1).**  The plan's canonical bytes allow only printable ASCII (§2), and an application name can hold other characters.  So when the helper builds the muster plan object for `redeploy`, it starts from the value above (the application name from the report row, a trailing ` (SERVICE)` removed when the row names that service, capped at 120 characters, or `coolify resource` when the row has no name), and then replaces every code point outside `0x20` to `0x7E` with one `?`:  one `?` per code point, with no collapsing of runs and no trimming.  The cap comes first, so the result is never longer than 120 characters.  For example, `Café App` becomes `Caf? App`.  This applies only when the muster plan object is built;  the existing `--from-ssh` route's `audit_target` is unchanged.

**Plan-time refusals.**  The shared planner refuses with `plan_refused`.  Its `reason` is the planner's own `error_code`, passed through unchanged, when it gives one;  on BB's final build (v5.1) that is one of `update_busy`, `authorization_unavailable`, `update_route_unavailable`, `update_executable_unavailable`, `unit_status_unavailable`, `unit_status_invalid`, `unit_unavailable`, `unit_type_unsupported`, `update_route_changed` (the update's route changed before it ran), `receipt_context_mismatch` (for `update-status`, the update receipt does not match its recorded baseline) and `receipt_context_unavailable` (for `update-status`, the captured receipt context cannot be read).  Otherwise the reason is `allowlist_miss` (the target, its action or the redeploy permission is not on the helper's root-owned allowlist), `report_stale` (for `redeploy`, `server-status.json` is missing, unsafe, not JSON, not written by `fleet-server-status`, older than 45 minutes (`STATUS_MAX_AGE_SECONDS`), does not list the uuid, or lists a row that cannot be redeployed), or `revision_stale` (for `resource-policy`, the saved policy's revision is no longer the one the target names, because the policy changed since it was read;  `fleet_resource_control` refuses this without an error code, so before v5.1 it would have been `internal_error`).  No other reason is used.

## 5.  Standard Input

**Framing.**  Read to end of file.  The limit is 4,096 bytes, or 65,536 bytes for `muster trust-list chain --yes`;  the helper reads at most limit + 1 bytes and refuses `input_too_large` beyond the limit.  Children keep `stdin=DEVNULL`.  A sender splits a chain into consecutive requests, each starting at the version the helper holds plus one.

**Parsing.**  Strict UTF-8, exactly one JSON object, `NaN` and infinities refused, duplicate keys refused, unknown keys refused, every value of the stated type and pattern, every string printable ASCII.  For a chain, every entry must have exactly the `body`, `ownerSignature` and `countersignatures` keys and every body field of §8 with its type;  structural checks on entries happen here.  Any failure is `bad_input`.

**Strictness (v5.4).**  These rules hold at P3, as BB's build of the chain slice applies them.  The reference checkers (`make_vectors_v5.py` and `check_vectors_v5.py`) apply the same rules, so no list they print is one the helper refuses.

- **Keys.**  Every public key in a list (`ownerKey`, the four keys of each device, and each `services` value that is not `null`) is the base64url of the 91-byte DER `SubjectPublicKeyInfo` of an uncompressed P-256 point:  the fixed P-256 prefix, then `04` and the two 32-byte coordinates, of a point on the curve.  A compressed point (`02` or `03`, in a 59-byte `SubjectPublicKeyInfo`), a hybrid point (`06` or `07`), another curve or anything else is `bad_input`.
- **Base64url** is canonical:  only the alphabet of RFC 4648 section 5, no `=` padding, a length that a whole number of bytes can have, and no stray trailing bits, so re-encoding the decoded bytes gives back exactly the same text.  A key is decoded here, at P3, and any failure is `bad_input`.  A signature is held only to §6's pattern at P3 (so `=` in one is `bad_input`);  its base64url and its DER are judged when it is verified, so a signature with stray trailing bits fails its verification:  in an owner signature the reason is `owner_signature` (check 4), in a countersignature it is `countersignature` (rule A, or rule C's proposal), and in an approval's `signature` the code is `bad_signature` (Y5).  Vectors T26 and T27 print the two list cases.
- **Times.**  Every time (a list's `issuedAt`, and an approval's `issuedAt` and `expiresAt`) has §2's form and is a real calendar time:  `2026-11-07T23:59:60Z` (no leap second), `2026-02-30T20:00:00Z` and year `0000` are `bad_input`.
- **The contract.**  A list's `body.contract` is exactly `muster-trust-list/1`;  any other value is `bad_input`.
- **Size (v5.5).**  An entry whose canonical bytes (§2) exceed 65,536 bytes, the most the stored file may hold (§11), is `bad_input` at P3, so a list the helper accepts can always be read back.  No list within the other limits (64 devices and 64 countersignatures, every field at its longest) can reach it:  the largest is about 55 KB.  So this is defence in depth, which keeps that invariant if a limit is ever raised.  The reference checkers apply it too (`size`), as they apply the rules above.
- **The whole request.**  P3 parses the whole document before C1, so one malformed entry makes the request `bad_input` wherever it stands in the chain, and nothing is counted.  **Formatting and key order of the input do not matter:**  the helper parses the document and works from the values, and canonical bytes are computed by the helper wherever a hash or signature needs them.

| Request | Document |
| --- | --- |
| `server … --plan` | `{"protocol":"muster-helper/1","job":{"jobId","jobNonce","deviceId"}}` |
| `server … --yes` | `{"protocol":"muster-helper/1","approval":{…§6…}}` |
| `muster trust-list current --plan` | `{"protocol":"muster-helper/1"}` |
| `muster trust-list chain --yes` | `{"protocol":"muster-helper/1","chain":[signed list, …]}`, ascending, 1 to 32 entries |

## 6.  The Approval

| Field | Parse Pattern | Checked Later |
| --- | --- | --- |
| `jobId` | `[a-z0-9]{1,26}` | |
| `jobNonce` | `[0-9a-f]{32}` | Must differ from `approvalNonce` (`bad_input`) |
| `approvalNonce` | `[0-9a-f]{32}` | Ledger, Y8a and Y8c |
| `host` | `[A-Za-z0-9._@-]{1,128}` | Must equal `server`, Y3 (`field_mismatch`) |
| `action` | `[A-Za-z0-9._@-]{1,128}` | Must equal the command's verb, Y3 |
| `target` | `[A-Za-z0-9._@-]{1,128}` | Must equal the command's target, Y3 |
| `planSHA256` | `[0-9a-f]{64}` | Y9 |
| `deviceId` | `d-[0-9a-f]{24}` (first 24 hex of SHA-256 of the device's approval key DER) | Y4 |
| `issuedAt`, `expiresAt` | §2 time format | Y6 |
| `signature` | `[A-Za-z0-9_-]{1,100}` | Y5 |

**Approval text.**  The eleven values below, joined by a single line feed, no trailing line feed, ASCII:

```
muster-approve/1
<jobId>
<jobNonce>
<approvalNonce>
<host>
<action>
<target>
<planSHA256>
<deviceId>
<issuedAt>
<expiresAt>
```

**Which key.**  Only the `approvalKey` of the device the approval names, on the stored list.  A job may carry more than one approval (a new one after `plan_mismatch` or `expired`, for example, as §15's What The Phone Does Next says);  each has its own `approvalNonce`.

**Signature form (v5.1).**  The `signature` is the DER `ECDSA-Sig-Value` of §2, and no verifier requires its `s` to be in the lower half of the curve order.  A high-S signature is accepted;  it cannot be used twice, because the nonce is spent (the ledger is keyed by `approvalNonce` alone, §11, so the other form of the same signature is `replayed_nonce`).  On the Swift side, low-S is not required:  the phone may send whichever form its signer produced.

## 7.  The Clock And The Two-Minute Window

**The clock.**  Before the window, the helper reads the kernel's clock state with `adjtimex` in read-only mode (`modes = 0`), a privilege-free call made through `ctypes` from the standard library, which works under `/usr/bin/python3 -I`.  It refuses the approval `clock_unsynced` when any of these holds:  the call fails;  it returns `TIME_ERROR`;  `STA_UNSYNC` is set in `status`;  or `maxerror` is above 1 second (1,000,000 microseconds).  `--plan`, reads and the trust-list verbs do not check it.  On this server, as BB measured on November 7, chrony runs, `timedatectl` reports `NTPSynchronized=yes`, and the kernel reports `TIME_OK`, `maxerror` 175 ms and `esterror` 2.6 ms, so the check passes today.  Two points are not verified (§17B, item 3).

**The window.**  `now` is read once at Y6 as the system clock's float seconds since the epoch (UTC), and compared exactly with the approval's whole-second times, with no grace:  `expiresAt` must be later than `issuedAt` and at most 120 seconds after it (`window_too_long`);  `issuedAt` must be at most 60 seconds after `now` (`not_yet_valid`);  `now` must not be later than `expiresAt` (`expired`).  `now` equal to `expiresAt` is accepted, and so is `issuedAt` equal to `now` + 60;  `now` = `expiresAt` + 0.001 is `expired`.

**The approval's own times (v5.2).**  The approval's `issuedAt` and `expiresAt` are the phone's, tied to no other time:  the helper compares them only with its own clock at Y6, never with the signed plan's `issuedAt` or any other answer's time.  So the phone never copies the plan's `issuedAt` into the approval, because any Plan Review longer than 120 seconds would then sign an approval that is already `expired`.  It sets `issuedAt` to its estimate of the server's clock at the moment it signs, which is its own clock plus the offset it saw at the plan answer (the plan's `issuedAt` minus its own clock when that answer arrived), rounded down to the whole second, and sets `expiresAt` at most 120 seconds after `issuedAt`.  The estimate errs early by the plan answer's time in transit, which only shortens the window the approval has left when it reaches the helper;  unless the phone's own clock is changed in between, it cannot make the approval `not_yet_valid`.

## 8.  The Trust List

```json
{
  "body": {
    "contract": "muster-trust-list/1",
    "version": 1,
    "previousSHA256": null,
    "issuedAt": "YYYY-MM-DDTHH:MM:SSZ",
    "ownerKey": "<base64url SPKI DER>",
    "closed": false,
    "devices": [{"deviceId": "d-…", "name": "…", "approvalKey": "…", "requestKey": "…", "readingKey": "…", "notificationKey": "…"}],
    "services": {"macPublisher": …, "macExecutor": …, "macAgreement": …, "serverPublisher": …, "serverExecutor": …, "serverExecutorAgreement": …, "serverHelper": …},
    "reopenTag": null
  },
  "ownerSignature": "<base64url DER>",
  "countersignatures": [{"deviceId": "d-…", "signature": "<base64url DER>"}]
}
```

Field patterns (checked at P3):  `contract` exactly `muster-trust-list/1` (v5.4);  `version` an integer from 1;  `previousSHA256` and `reopenTag` 64 lowercase hex or `null`;  `issuedAt` §2, and a real calendar time (v5.4);  every key the canonical base64url of the 91-byte `SubjectPublicKeyInfo` of an uncompressed P-256 point, so exactly 122 characters (v5.4, replacing "base64url, 1 to 200 characters":  a compressed or hybrid point, or any other key, is `bad_input`);  `name` 1 to 40 printable ASCII;  the seven `services` keys always present, each a key or `null`;  signatures as in §6 (the pattern only:  a signature's base64url and DER are judged when check 4 or rule A verifies it);  at most 64 devices and 64 countersignatures.  §5's Strictness gives the reasons.

**The checks, in order.**  Every verifier runs these checks on each entry, in this order, and stops at the first that fails;  that check's reason is the answer.  "Previous" is the list the verifier holds, or the entry before this one in the chain.

| # | Check | Reason |
| --- | --- | --- |
| 1 | `version` is 1 when nothing is held, and otherwise the previous version plus one.  | `version_gap` |
| 2 | When nothing is held, `previousSHA256` is `null`;  otherwise it equals the previous body hash.  | `first_rule`, `previous_hash` |
| 3 | `ownerKey` equals the key the owner history (§10) assigns to this version;  and, when this version is the `toVersion` of a history line, the entry's body hash equals that line's `toSHA256` (v5.3).  | `owner_key`, `history_hash` |
| 4 | `ownerSignature` verifies with **the key the history assigns** (never with the entry's own `ownerKey` field).  | `owner_signature` |
| 5 | No two devices share a `deviceId`.  | `duplicate_device` |
| 6 | No two countersignatures name one device.  | `countersignature` |
| 7 | `closed` is true exactly when `devices` is empty.  | `closed_rule` |
| 8 | The one rule below that applies holds, its conditions checked in the order written.  | As the rule says |

Exactly one rule applies to each entry:

| Rule | Applies When | Conditions, In Order |
| --- | --- | --- |
| First | Nothing is held | Not closed, no countersignatures, `reopenTag` null (`first_rule`).  |
| A.  Normal change | Something is held, the previous list is not closed, and this one is not closed | `reopenTag` null (`first_rule`);  at least one countersignature, every one verifying with the `approvalKey` of a device on the previous list (`countersignature`);  if the previous list named `serverHelper` or `serverPublisher`, this one still names at least one of them (`server_keys_removed`, v5.3, §17A A8).  |
| B.  Close | Something is held and this list is closed | No countersignatures and `reopenTag` null (`closed_rule`);  every `services` value equal to the previous one or `null` (`services_changed`);  if the previous list named `serverHelper` or `serverPublisher`, this one still names at least one of them (`server_keys_removed`).  |
| C.  Reopen (**OPEN**) | Something is held, the previous list is closed, and this one is not | Proposal, not settled (§17A):  `services` as in B;  the countersigners are exactly this list's devices, each verifying with its own `approvalKey` (`countersignature`);  `reopenTag` not null and a record line for this version exists (`reopen_unrecorded`);  the tag checks against the newest line (`reopen_tag`);  `devices` is exactly the one device that line names (`reopen_device`).  Until §17A is settled, every verifier refuses a rule C entry with `reopen_unavailable`, a non-final reason.  |

**Final and non-final reasons (OPEN).**  Version 4 called `owner_signature`, `duplicate_device`, `countersignature`, `closed_rule`, `first_rule`, `services_changed`, `server_keys_removed`, `reopen_tag` and `reopen_device` final, and recorded final refusals by entry hash.  EE's eighth look shows that final refusals can be minted without the owner key, and that a passing tag can later fail (§17A).  Until that is settled, no verifier records or reports a refusal as final.

**Rule C at verifiers that cannot read the record (OPEN).**  The version 4 proposal:  the phone and Muster apply a rule C entry only when a verified acknowledgement from the right key of the newest list they have applied (`serverHelper` if listed, else `serverPublisher`) covers it, and hold it pending until then.  §17A lists what breaks.

The helper accepts the whole request or none of it.

**Storage (OPEN-5, settled in version 3).**  The newest accepted signed list, in `/home/jay/.local/state/fleet-tools/muster-trust.json` (0600), always written by a temporary file in the same directory, `fsync`, `rename` and a directory `fsync`, under `ops-control.lock`.  **What is written (v5.3):**  the canonical bytes (§2) of the newest entry of the accepted chain, the whole signed object (`body`, `ownerSignature` and `countersignatures`), with no trailing line feed, so the file's SHA-256 is that entry's `entrySHA256`.  The temporary file is opened `O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC`, mode 0600, with a random name beginning `.muster-trust.json.`, and is unlinked if any step before the rename fails.  It needs no `O_NONBLOCK` (v5.4):  `O_CREAT | O_EXCL` only ever makes a new file, so the open cannot meet a FIFO.  **The sweep (v5.5).**  At C5, under `ops-control.lock` and before the temporary file is made, the helper removes the stale temporary files of earlier writes (a process killed between the open and the rename leaves one, and nothing ever reads it):  each entry in the state directory named exactly `.muster-trust.json.`, 16 lowercase hex characters and `.tmp`, that is a regular file (looked at without following a symbolic link) owned by the helper's account with one link.  It ignores every error, since the write that follows does not depend on it, and touches nothing else;  a chain refused before C5 sweeps nothing.  Readers keep accepting any formatting that parses under §5's rules, so a list written another way is still read.  It is never created empty.  Absent means nothing is held;  anything that fails §11's read checks is `state_unsafe`.

## 9.  The Reopen Record And Recovery (OPEN, gates Milestone 5)

Everything in this section is the version 4 proposal, kept so the problems in §17A have a text to point at.  It is not settled and nothing builds on it before Milestone 5.

| Item | Proposal |
| --- | --- |
| Path | `/etc/muster/reopen-codes.jsonl` |
| Owner and mode | `root:root`, `0644`, a regular file in root-owned `/etc/muster` (§10's checks apply) |
| Writer | Only `muster-agent reopen --version N --device d-…`, run as root at the console.  It appends one line and `fsync`s, after truncating a final fragment that has no line feed (it is the only writer).  Nothing else removes or edits a line.  |
| Line format | The canonical JSON of `{"code","createdAt","deviceId","version"}` and a line feed.  |
| Which line counts | Only the newest line for a version (§17A, item A1).  |
| Display | The code in eight groups of four and a check group:  the first 4 hex of SHA-256 of `muster-reopen-check/1\n<version>\n<deviceId>\n<code>`.  |
| Readers | The helper, `muster-pub` and `muster-exec`, through the shared module (§10), afresh for every verification.  |

**Settled now, because P1 reads the file in every milestone:**  an absent or empty record is accepted as no lines;  a line that is not canonical JSON with exactly those four keys and patterns (`code` 32 lowercase hex, `createdAt` §2, `deviceId` `d-` and 24 hex, `version` an integer from 1), or a final fragment without a line feed, is `pin_unsafe`.

**The tag (proposal).**  `reopenTag` is the lowercase hex HMAC-SHA256, keyed with the ASCII bytes of `code`, over the canonical body with `reopenTag` set to `null`.

**Recovery (proposal).**  Muster stores every entry it signs before it sends it.  For a pending entry at version v, a verified acknowledgement from the required key:  (1) naming v, applies it when the hash matches and otherwise adopts the server's chain;  (2) naming a later version, makes Muster fetch and walk the server's chain;  (3) naming an earlier version, discards the pending entry only if `finalRefusals` holds its exact `entrySHA256`.  Coverage is checked first.  Vectors R1 to R5 illustrate it.

**Contradiction (proposal).**  An acknowledgement from the required key that contradicts an applied version makes the verifier stop, fetch the server's chain and adopt it if it walks.  §17A, item A3, shows it has no exit.  **Settled for Milestones 1 to 4:**  no verifier holds a pending entry before Milestone 5, and Muster signs one entry per version, so a contradicting acknowledgement can come only from a forged or broken signer;  the phone and Muster then show a warning and keep working, and never stop.

## 10.  The Owner Key Pin And History

| File | Format | Writer |
| --- | --- | --- |
| `/etc/muster/owner.pub` | One line:  base64url DER `SubjectPublicKeyInfo` of the current owner key, then a line feed;  at most 1,024 bytes | `muster-agent install`, `repin`, as root |
| `/etc/muster/owner-history.jsonl` | One canonical JSON line per owner key:  `{"fromVersion","ownerKey","toSHA256","toVersion"}`;  `toVersion` and `toSHA256` (the body hash of version `toVersion`) are `null` for the current key | The same commands |

**Checks (P1, and the same in `muster-pub` and `muster-exec`).**  One module, `muster_trust_files.py`, `lstat`s `/etc/muster`, `owner.pub`, `owner-history.jsonl` and, if it exists, `reopen-codes.jsonl`;  refuses any that is missing (the record excepted), a symbolic link, of the wrong type, not owned by uid 0, or writable by group or others;  refuses an `owner.pub` that is not one P-256 key, a history that is not contiguous from version 1 or has other than one open entry, an open entry whose key differs from `owner.pub`, and a record that fails §9's settled format;  and returns the bytes it read.  **Every caller calls it afresh for each verification:**  the helper once per request at P1;  `muster-pub` and `muster-exec` for every chain they verify, every acknowledgement or heartbeat they sign, and at least once a minute.  The helper answers `pin_unsafe`;  `muster-pub` and `muster-exec` stop publishing and executing and report it in their heartbeats.  While walking, every verifier also requires the body hash of each history line's `toVersion` to equal its `toSHA256`:  that is §8's check 3, and a mismatch is `history_hash` (v5.3).  The helper compares the list it holds at C2 the same way, and a held list that fails is `state_unsafe` (§12).

**How the two copies stay equal.**  The repository holds one source file, `scripts/muster_agent/muster_trust_files.py`.  The helper's installer and `muster-agent`'s installer both copy that one file, to `/usr/local/lib/fleet-resource-control/` and `/usr/local/lib/muster-agent/`;  a repository test checks that both installers name that one source path and that no second copy exists in the repository.  On the server each installer, when the other copy is present, compares their SHA-256 after copying and fails on a difference, and `muster-agent trust-status` prints both hashes.

**Install.**  `muster-agent install` writes `owner.pub` and the line `{"fromVersion":1,"ownerKey":…,"toSHA256":null,"toVersion":null}` and prints the full fingerprint.  It refuses when `owner.pub` exists, unless no stored list exists anywhere on the server, in which case it replaces both after the same fingerprint comparison.

**Status.**  `muster-agent trust-status` prints, for each stored list that exists, its version, its full body hash and whether its owner signature verifies under the current history;  the two installed module hashes;  and the newest record line's version and device.  It changes nothing.

**Root readers.**  `install`, `repin` and `trust-status` run as root and read the stored lists with §11's checks, except that each file must be owned by its expected account rather than by the reader:  `muster-trust.json` by `jay`, `muster-pub`'s stored list by `muster-pub`.

**Rotation.**  `muster-agent repin --from-version N`, as root at the console after a full-fingerprint comparison of the new key:

1. Reads every stored list that exists (the helper's `muster-trust.json` and `muster-pub`'s stored list), as a root reader.  It refuses, printing what it found, unless at least one exists, all hold the same version K with the same body hash, and N equals K + 1.  If the server lags, the owner delivers the chain first;  `repin` never guesses.
2. Builds the new history:  the open line closed with `toVersion` K and `toSHA256` the body hash from step 1, and a new open line from N.
3. Writes it to a temporary file in `/etc/muster` (`O_CREAT | O_EXCL | O_NOFOLLOW`, 0644), `fsync`s it, renames it over the history, and `fsync`s `/etc/muster`.  Then the same for `owner.pub`.  Each rename is durable before the next begins.
4. `muster-agent repin --resume` handles the state where the history's open entry names a key other than `owner.pub`'s:  it prints that key's full fingerprint, asks the owner to confirm it is the new key, and writes `owner.pub` from that entry as in step 3.  In any other state it changes nothing and says so.

**Muster's side.**  Muster offers a rotation only when the server has acknowledged every version it signed, then shows N = K + 1 and from that moment signs nothing with the old key.  It signs version N with the new key only after an acknowledgement shows `signedByCurrentKey: false` with `helperTrustListVersion` equal to K, which proves `repin` has run.  An acknowledgement whose `helperTrustListVersion` is `null` (nothing held, §13) never counts as that proof, whatever its `signedByCurrentKey` says.

On every approval (Y2) and in every acknowledgement, the stored list must be signed by the current key;  a pre-rotation list gives `no_trust_list` to approvals and `signedByCurrentKey: false` in acknowledgements until a list of version N or later arrives.

## 11.  The Nonce Ledger And State Files

**The state directory.**  `/home/jay/.local/state/fleet-tools/`, checked at P0 with `lstat`:  a directory, not a link, owned by the effective uid, with no group or other bits.  Anything else is `helper_key_missing` (the key lives there, so no refusal can be signed).

**Ledger.**  `muster-nonces.jsonl` in that directory, 0600.  One canonical JSON line per used approval nonce, `{"approvalNonce","deviceId","expiresAt","jobId","recordedAt"}`, then a line feed.  The job nonce is `muster-exec`'s.  For `resource-policy` the helper checks and appends before calling `fleet_resource_control`, which does not touch the ledger.

**Durability.**  Under `ops-control.lock` at Y8 (§12):  at Y8a, refuse `replayed_nonce` if the nonce is on any line;  at Y8b, take the `yes` rate step, which refuses `rate_limited` without spending the nonce;  only then, at Y8c, append, flush, `fsync` (and the directory if the file was just created), and only then continue.  The nonce is spent even if Y9 refuses or the run fails.

**The final line (OPEN-3).**  If the file does not end with a line feed, the helper looks at the fragment after the last line feed.  A fragment that parses as a whole ledger line was written in full:  the helper appends a line feed, `fsync`s, writes a ledger-repair row with result `completed`, and continues, so that nonce stays spent.  A fragment that does not parse is the remains of an append that never finished its `fsync`, so no action ran on it:  the helper truncates to the last line feed, `fsync`s, writes a ledger-repair row with result `repaired`, and continues.  Any other line that does not parse is `ledger_corrupt`:  every approval is refused, without a row, until the file is repaired in a shell.

**Retention (v5.1).**  Under the lock, the helper may rewrite the ledger without a line only when that line is beyond the last 4,096 lines and its `expiresAt` is more than 604,800 seconds (seven days) before `now`.  A line whose `expiresAt` is after `now` stays.  The clock alone never makes a nonce forgettable:  a clock stepped forward and back cannot push a nonce out, because only 4,096 newer genuine approvals can, and the `yes` cap is 60 an hour.  The rewrite goes through a temporary file, `fsync`, `rename` and a directory `fsync`.  A ledger over 4 MiB is `ledger_corrupt` (v5.2).  That is twice the most a genuine ledger can hold, rounded up to a whole MiB:  the larger of 4,096 lines and the `yes` cap over the retention (60 an hour for 169 hours, 10,140 lines), times the longest line (201 bytes), times two.  At 2 MiB the margin over a week of approvals was 3.4 percent, so a later change to the cap or the retention could have made every genuine ledger read as corrupt.  The key is the `approvalNonce` alone:  the same nonce from another device or another job is `replayed_nonce`.

**Final chain refusals (OPEN).**  Version 4's `muster-chain-refusals.jsonl` is not built until §17A is settled.

**Opening state files.**  The ledger, the rate file and the rate lock file are opened `O_NOFOLLOW | O_CLOEXEC | O_NONBLOCK` (`O_NONBLOCK` from v5.4, so that a FIFO put in a file's place cannot hang the open;  the `fstat` below refuses it), created with `O_CREAT | O_EXCL` and mode 0600 only when absent, then `fstat`ed:  a regular file, owned by the effective uid (or, for a root reader, the expected account, §10), no group or other bits, and the link count below;  anything else is `state_unsafe`.  **`muster-trust.json`** is opened `O_RDONLY | O_NOFOLLOW | O_CLOEXEC | O_NONBLOCK` and never created by a read;  absent means nothing is held;  otherwise it must pass the same `fstat` checks, be 1 to 65,536 bytes, and parse as one signed list under §5's rules, or it is `state_unsafe`.  **The link count (v5.4).**  Every state file needs exactly one link, except that the read of `muster-trust.json` also accepts none (an old file a chain's rename has just unlinked):  the ledger, the rate file, the rate lock file, the helper key and the trust file's temporary file (which `O_CREAT | O_EXCL` makes new) need exactly one, and an open that can write is refused at link count 0 whoever asks.  An unlocked reader (Y2, K2, C2) can open `muster-trust.json` just before a chain's atomic rename and `fstat` it just after;  its descriptor still reads the complete old file, so that reader is simply ordered before that chain.  A link count of 0 on an open that writes is refused, because an unlinked handle would write to a file nobody can find.  The regular-file, owner and mode checks are unchanged, on the same `fstat`.  This changes v5.3's "including exactly one link" for the read of the trust file only.  **The helper key (OPEN-2)** is opened like the ledger, read-only, but never created, and keeps exactly one link, since it is never replaced by a rename;  any failure is the unsigned `helper_key_missing` at P0.

## 12.  Check Orders

**Prelude, every request:**

- **P0.**  The state directory and the helper key (§11);  `helper_key_missing`, unsigned.
- **P1.**  `/etc/muster` (§10), the reopen record included when present, read once into memory for this request;  `pin_unsafe`.
- **P2.**  Command line (§3, §4);  `bad_command`, then `mode_not_allowed`.
- **P3.**  Standard input (§5);  `input_too_large`, then `bad_input`.

Requests refused at P0 to P3 are not counted by the rate limits;  each is refused before it can reach a lock, a signature check or an action.

**Rate limits.**  Counted at Y8b, R1, C1 and K1.  Before v5.1 the `yes` class was counted at Y1;  it now waits until the approval has verified and is about to spend its nonce.

| Class | Requests | Key | Spacing Per Key | Hourly Cap Per Class |
| --- | --- | --- | --- | --- |
| `plan` | `server … --plan` | `verb target`, one space between | 5 seconds | 120 |
| `yes` | `server … --yes` | `verb target`, one space between | 2 seconds | 60 |
| `chain` | `muster trust-list chain --yes` | `chain` | 10 seconds | 30 |
| `current` | `muster trust-list current --plan` | `current` | 2 seconds | 600 |

- **Times** are integer milliseconds since the epoch, from the same float clock as §7, rounded down.
- **Spacing:**  a request passes when at least the spacing has passed since the last counted request with its key (`now − last ≥ spacing`;  exactly 5 seconds passes).
- **The hour** is sliding and inclusive:  the cap counts the class's requests with `now − at ≤ 3,600,000`, so a request exactly 3,600 seconds old still counts.  The cap is per class, across every key.
- **The clamp:**  on every read of the rate file, each stored event dated after `now` is treated as made at `now` (its time becomes `min(at, now)`), so a clock stepped back never holds a key.  That clamped list is what the spacing and the cap count.  When the clamp changed any event and the request is refused, the clamped list is still written back (best effort:  a failed write is not an error), and the refused request is not recorded.  When the request is accepted, its event `[class, key, now]` is appended to the clamped list and written (durably for `yes`, Y8b).
- **What counts:**  every request that passes the rate step, whatever happens after it.  A request refused `rate_limited` is not counted.  For `yes` the rate step is Y8b, under `ops-control.lock`, so nothing before Y8b uses the `yes` budget:  `bad_signature`, `unknown_device`, `field_mismatch`, `no_trust_list`, `state_unsafe`, `clock_unsynced`, `window_too_long`, `expired`, `not_yet_valid`, `action_busy`, `trust_list_changed`, `ledger_corrupt`, `replayed_nonce` and `internal_error` before Y8b never count.  A captured approval replayed any number of times never counts (it is refused at Y8a or earlier), and junk can never lock the owner out.  Everything after Y8c counts, because it spent a nonce:  `plan_mismatch` at Y9 and a failed run at Y10 included.  A fault at Y8c is `internal_error` (Faults, below) and its Y8b event stays counted;  only an approval that verified can reach it.
- **The file:**  `/home/jay/.local/state/fleet-tools/muster-rate.json`, the canonical JSON of `{"contract":"muster-rate/1","events":[[class,key,milliseconds],…]}`, oldest first.  Every update drops events with `now − at > 3,600,000` and is written by a temporary file and `rename` (no `fsync`:  a lost update only forgets recent requests), except that a `yes` event is made durable at Y8b before its nonce is spent.  An empty file means no events.  A file larger than 128 KiB, with more than 810 events, with an event that is not `[known class, string key of at most 300 characters, integer time]`, with times not in non-decreasing order, or that fails §11's checks or does not parse, is `state_unsafe`, and stays so until repaired in a shell.
- **The lock:**  `/home/jay/.local/state/fleet-tools/muster-rate.lock`, an exclusive `flock` of its own (never `ops-control.lock` itself), and never held while taking another lock.  The lock order is the same everywhere:  `ops-control.lock`, then the rate lock.  Y8b takes the rate lock while holding `ops-control.lock`;  R1 and K1 (`plan` and `current`), and C1, only ever take the rate lock, and release it before any other lock is taken.  The helper waits for it at most 2 seconds, then answers `rate_limited` without counting (at Y8b, with the nonce unspent).  So an approval holds `ops-control.lock` through Y8b for at most that 2-second wait plus two `fsync`s.  The rate step stays under it on purpose:  the event and the nonce are written by one holder in that order, so no other approval runs between the check and the spend, and a nonce is never spent without its event.  `plan` and `current` hold the rate lock only to read and rewrite one file of at most 128 KiB, without `fsync`, so the 2-second wait is reached only when something has wedged.  An approval that meets the lock at Y7 answers `action_busy`, spends nothing, and may be retried inside its window.
- **Residual:**  `plan` and `current` requests are unsigned, so their rate steps cannot be authenticated:  anyone who can reach the route, an exploited `muster-exec` included, can use up the `plan` and `current` budgets, which delays the owner's own plans and acknowledgements (and the same holds for the `chain` budget at C1).  Nobody can use up the `yes` budget that way:  it counts only approvals that verified and are about to spend their nonce, and each nonce is spent once, so neither junk nor a replayed capture makes the owner's real approval `rate_limited`.  An exploited `muster-exec` can already withhold every request, so the `plan` and `current` exposure adds no new power.  A flood of unsigned or wrongly signed `yes` requests costs the server one helper start each, and the signature check adds little to that:  on this server one P-256 check took a median 0.084 ms, against 50 ms to start `python3 -I` with the helper's imports (measured Nov 7, 2026).  So the `yes` class has no unauthenticated gate before Y5:  such a gate would cut almost no work and would bring back the lockout this order removes.  The transport in front of `muster-exec` limits who can start the helper at all.

**`server … --yes`, an approval (OPEN-4):**

- **Y1.**  No rate step (v5.1):  the `yes` class is counted at Y8b, once the approval has verified, so nothing before Y8b uses its budget.  The step keeps its name so later steps keep theirs.
- **Y2.**  Read the stored list without a lock (it changes only by `rename`), noting its body hash (`state_unsafe`);  none, or not signed by the current owner key, is `no_trust_list`.
- **Y3.**  `host`, `action`, `target` equal `server`, the verb, the target;  `field_mismatch`.
- **Y4.**  `deviceId` names a device on the stored list, and the list is not closed;  `unknown_device`.
- **Y5.**  Signature by that device's `approvalKey`;  `bad_signature`.  From here the fields are verified.
- **Y6.**  Clock synchronised (`clock_unsynced`), then the window (§7).
- **Y7.**  Take `ops-control.lock` (for `logs` too;  `action_busy`), read the stored list again (`state_unsafe`), and refuse `trust_list_changed` if no list is held now or its body hash differs from Y2's (worded in v5.3 to match C4;  the helper already does this).
- **Y8.**  Under `ops-control.lock`, in this order:
  - **Y8a.**  Read the ledger (§11);  `ledger_corrupt`, then `replayed_nonce`.
  - **Y8b.**  Rate check and record for class `yes`, key `verb target`, taking the rate lock while holding `ops-control.lock` (Rate limits, above).  Over the hourly cap or inside the spacing is `rate_limited`, and the nonce is not spent:  the same approval may be retried inside its two-minute window.  Otherwise the event is recorded and made durable (the temporary file `fsync`ed, renamed, and the directory `fsync`ed) before Y8c.
  - **Y8c.**  Append the nonce and `fsync` it (§11).  From here the nonce is spent.
- **Y9.**  Build the plan with the shared function and compare its SHA-256 with `planSHA256`;  `plan_mismatch` and an audit row.  For `resource-policy` this and the revision comparison happen inside `fleet_resource_control` under `broker_lock`.
- **Y10.**  Run, write the audit row, answer with a signed result (exit 0 done or submitted, 1 failed or unknown).

**Faults.**  Any unexpected failure before Y8c has spent the nonce (an exception, a missing file the steps above do not name) is the refusal `internal_error`, and nothing ran.  After Y8c the nonce is spent, so a fault is never a refusal:  it is a signed result, with its one audit row (§14).  Its outcome (v5.1) is `unknown` when the action had begun or may have (a command killed at its limit, a Coolify request that may have been sent and was not answered, an exception once the run began), and `failed` only when the action provably never began or ended in a definite failure.  That covers a plan that cannot be built at Y9 (the planner refusing with a §4 reason, including `revision_stale` when the saved resource policy changed after the approved plan was built, a redeploy uuid gone, an Infisical read error), which never began, so its outcome is `failed`.  The fault's `detail` is exactly one of three fixed words:  `plan_refused: <reason>` (the planner refused with a §4 reason, at Y9 or inside the shared run function before it ran anything), `the helper stopped unexpectedly while running` (an exception out of the run at Y10), or `internal_error` (any other fault).  The outcome follows the rule above, not the word.  A failed close of the ledger after its `fsync`, and a failed prune rewrite, are not faults (v5.2):  the append is durable, the prune is retried by the next approval, and the run proceeds.

**`server … --plan`:**  R1 rate limit (`rate_limited`);  R2 build the plan or run the read, as today (`broker_lock` for `resource-policy` only;  `plan_refused` with its reason, or `internal_error`);  R3 read the stored list for the answer's `helperTrustListVersion` (§13), then the signed plan or signed result (exit 0).

**`muster trust-list chain --yes`:**

- **C1.**  Rate limit;  `rate_limited`.
- **C2.**  Read the stored list without a lock, noting its body hash (§11;  `state_unsafe`);  none means the chain must begin at version 1.  When the held list's version is the `toVersion` of a line in P1's copy of the history, its body hash must equal that line's `toSHA256`, or it is `state_unsafe` (v5.3, §10).  That refusal carries `helperTrustListVersion` `null` (v5.4), by §13's rule for a list the helper could not use:  it vouches for no version it read.
- **C3.**  Verify the whole chain (§8 to §10) against it and P1's copy of `/etc/muster`;  `chain_refused` with the reason, recording nothing (final-refusal recording is OPEN, §11).
- **C4.**  Take `ops-control.lock` (`action_busy`), read the stored list again (`state_unsafe`), and refuse `trust_list_changed` unless it is the list C2 read (v5.3):  that is, if its body hash differs from C2's, if a list is now held where C2 found none, or if none is held where C2 found one.
- **C5.**  Under the lock, in this order (v5.3):  write the newest entry (§8's Storage), durable before anything else;  then append the audit row (§14);  then answer with a signed `ack` of the list just written (exit 0).  A failure before the rename is `internal_error`, and nothing is stored.  A failure once the rename has happened (the directory `fsync` or the signing, for example) is still `internal_error`, but the list is held:  the sender learns that from `current --plan`, and the same chain sent again is refused `version_gap`.  A failed audit append after the rename is not a fault, as a failed close of the ledger or a failed prune is not (Faults, above):  the list is durable, and the signed `ack` is answered.  **A signing fault after the rename (v5.4).**  The `internal_error` refusal is signed with the same helper key as the `ack`.  So a signing fault that repeats when the refusal is signed gives the unsigned `helper_key_missing` (§13's one unsigned answer), and a fault that does not repeat gives the signed `internal_error`;  either way the list is held, and `current --plan` shows it.  **The audit row is best effort (v5.4).**  A row means the list was durable, but not the reverse:  there is no row when only the directory `fsync` fails (the answer is `internal_error`, and the list is held) or when the process dies between the rename and the append.  So `muster trust-list current --plan`, never the audit log, is authoritative for what is held.  **The sweep (v5.5).**  Before the write, still under the lock, the helper removes the stale temporary files of earlier writes, as §8's Storage says;  it ignores every error, so the sweep never changes the answer.

**`muster trust-list current --plan`:**  K1 rate limit (`rate_limited`);  K2 read the stored list without a lock (§11;  `state_unsafe`);  K3 answer with a signed `ack`.

**`resource-policy` interface.**  `fleet_resource_control.handle(ctx, action, target, mode, *, require_approval=False, approval=None, source="fleet-ops-ctl-owner-key")`.  With `require_approval=True` it requires `approval`, compares its `planSHA256` with the canonical plan it builds under `broker_lock` (`plan_mismatch`), writes its audit rows with `source="muster-ios"` and §14's fields, and refuses if any of these is missing.  The canonical plan's `old` and `new` are its `before` and `after`.

## 13.  Signed Answers

```json
{"protocol": "muster-helper/1", "kind": "plan" | "result" | "refusal" | "ack", "body": { … }, "keyId": "<16 hex>", "signature": "<base64url DER>"}
```

| Kind | Body Fields |
| --- | --- |
| `plan` | `contract` `muster-signed-plan/1`, `claimed` (`{"jobId","jobNonce","deviceId"}` as the request gave them), `host`, `action`, `target`, `plan` (the canonical plan bytes as a string), `planSHA256`, `helperTrustListVersion`, `issuedAt` |
| `result` | `contract` `muster-signed-result/1`, `jobId`, `jobNonce`, `approvalNonce`, `deviceId` (verified on `--yes`;  for a read, `jobId`, `jobNonce` and `deviceId` are under `claimed` and `approvalNonce` is absent), `host`, `action`, `target`, `planSHA256` (`null` for reads), `outcome`, `detail`, `outputB64` (or `null`), `auditId` (or `null`), `helperTrustListVersion`, `finishedAt` |
| `refusal`, approval | `contract` `muster-signed-refusal/1`, `verified` (`true` only once Y5 passed);  when verified, `jobId`, `jobNonce`, `approvalNonce`, `deviceId`, `host`, `action`, `target`, `planSHA256`;  otherwise `claimed` holding only those of them that passed their parse pattern (empty for a refusal at P1 to P3);  `errorCode`, `reason` (`null`), `helperTrustListVersion`, `refusedAt` |
| `refusal`, `--plan` or read | As above with `verified` always `false` and `claimed` holding only those of `jobId`, `jobNonce` and `deviceId` that passed their pattern;  `reason` is the §4 reason for `plan_refused`, else `null` |
| `refusal`, `current --plan` | `contract`, `verified` `false`, `claimed` `{}`, `errorCode`, `reason` `null`, `helperTrustListVersion`, `refusedAt` |
| `refusal`, chain | From P3 on (v5.4), once the command line has parsed as `muster trust-list chain --yes`:  `contract`, `verified` `false`, `chain` (`{"version","entrySHA256","final"}` for the refused entry at C3, with `final` always `false` until §17A is settled;  `null` for any other refusal, P3's `input_too_large` and `bad_input` included), `errorCode`, `reason` (the §8 reason for `chain_refused`, else `null`), `helperTrustListVersion`, `refusedAt`, and no `claimed`, since a chain carries no job.  A refusal at P1 or P2 (`pin_unsafe`, `bad_command`, and `mode_not_allowed` for `chain --plan`) comes before the command is known to be a chain, so it has no `chain` and carries `claimed` `{}`, as the approval row says for P1 to P3.  |
| `ack` | `contract` `muster-signed-ack/1`, `helperTrustListVersion` (or `null`), `closed`, `signedByCurrentKey`, `trustListSHA256` (or `null`), `finalRefusals` (OPEN;  always `[]` until §17A is settled), `at` |

**`helperTrustListVersion`** in every answer is the version of the stored list this request actually read, at Y2, R3, K2 or C2, and nothing more (v5.1).  A chain acknowledgement is the one exception:  it carries the version it just wrote at C5, as the T2 and T3 acknowledgements print (v5.3).  It is `null` when the helper holds no list, when it could not read it or could not use it (`state_unsafe`, including a held list that fails its history line's `toSHA256` at C2, v5.4), and when the request was answered before its read step:  a refusal at P1 to P3, at K1, at C1, or at R1 or R2 (a `plan_refused` at R2 carries `null`, because the list is read at R3).  An approval refusal at Y2 or later, Y8b's `rate_limited` included, carries the version read at Y2.  It reports the stored list's version even when that list is not signed by the current key.  A phone reads it as information, never as coverage:  a `no_trust_list` refusal that carries version K means a list of version K exists and is not usable (for example, it is signed by a key that is no longer current), not that version K covers the request.

**An acknowledgement with nothing held (v5.1).**  When the helper holds no stored list, an acknowledgement answers `helperTrustListVersion: null`, `trustListSHA256: null`, `closed: false`, `signedByCurrentKey: false` and `finalRefusals: []`.  Every verifier branches on `helperTrustListVersion == null` first, before it reads any other field of an acknowledgement:  a null version means only that nothing is held, so that acknowledgement's `closed` and `signedByCurrentKey` carry no information, and in particular its `signedByCurrentKey: false` is never evidence that `repin` ran (§10).  The same holds for `muster-pub`'s heartbeat, which has the same body.

**Outcomes:**  `done` (it ran and finished as planned);  `submitted` (it was handed to a service that finishes later, such as a Coolify deploy or a `--no-block` start);  `failed` (only when the action provably never began, as when the planner or the shared run function refused before running anything, a connection was refused or a name did not resolve, or when it ended in a definite failure);  `unknown` (the action had begun or may have, and its end could not be observed:  a command killed at its limit, a lost child status, a Coolify GET for `redeploy` that may have been sent and was not answered (a timeout, a reset or a cut answer), or an exception once the run began);  `planned` (a read or a plan, no change).  A fault after Y8c is `failed` or `unknown` by the same rule (§12 Faults, v5.1).  The phone reads `unknown` as "Started;  result not known".

`muster-pub`'s heartbeat has the same body as `ack`, signed with its own key under the same prefix.

**The phone's plan check.**  Before Plan Review, the phone verifies the plan's signature with the `serverHelper` key of its applied list;  checks that `claimed` equals the job it sent and `host`, `action` and `target` equal the job's;  checks that SHA-256 of the ASCII bytes of `plan` equals `planSHA256`;  and checks that `plan` parses as one of the canonical plan objects below and re-encodes to exactly the same bytes.  Any failure:  no Plan Review, and the job shows as refused by the phone.  It renders Plan Review from those bytes and signs the approval over that `planSHA256`.  It parses `plan`, like every answer, with §2's strict parser and re-encodes it with §2's canonical encoder (v5.2).  It keeps the offset between the plan's `issuedAt` and its own clock when the answer arrived, for the approval's times (§7);  the approval's `issuedAt` is never the plan's `issuedAt` itself.

Canonical plan objects:  `{"contract":"fleet-ops-plan/1","scope","action","target","commands","auditTarget"}` for the helper's verbs, with `commands` and `auditTarget` as in §4;  `{"contract":"fleet-resource-plan/1","field","old","new","revision","auditTarget"}` for `resource-policy`, `auditTarget` being the field name.

**The helper key.**  `/home/jay/.local/state/fleet-tools/muster-helper.key`, an unencrypted PKCS#8 PEM P-256 key, 0600, made by the helper's Milestone 5 install, which prints its full fingerprint.  Its public half is `services.serverHelper`.  `keyId` is the first 16 hex of SHA-256 of its DER public key.  Without it the only answer is `{"protocol":"muster-helper/1","kind":"refusal","body":{"contract":"muster-signed-refusal/1","errorCode":"helper_key_missing"},"keyId":null,"signature":null}`, which every verifier treats as unauthenticated.

## 14.  Audit Fields

Rows go to `/home/jay/.local/state/fleet-tools/ops-audit.jsonl`, always appended under `ops-control.lock`.

**Fields.**  The helper's existing fields:  `time`, `host`, `verb`, `target`, `result`, and optionally `id`, `lines`, `route`, `invocationID`, `verified`, `baselineStartMonotonic`.  A `--from-muster` row adds `route: "from-muster"`, `via: "muster-ios"`, `jobId`, `deviceId`, `approvalNonce`, `planSHA256`, and `errorCode` on a refusal.  Because `route` now holds `from-muster`, the update route that an `update` row keeps in `route` today moves, for these rows only, to a new field `updateRoute`.  `fleet_resource_control`'s rows, in its own `resource-policy-audit.jsonl`, add `source: "muster-ios"` and the same ids.  A ledger-repair row has verb `muster ledger-repair` and result `repaired` (a torn fragment truncated) or `completed` (a whole final line given its line feed).  A chain row (v5.3) has verb `muster trust-list`, target `chain`, result `accepted`, `route: "from-muster"`, `via: "muster-ios"`, `trustListVersion` (the version now held), `trustListSHA256` (its body hash) and `fromVersion` (the version C2 read, or `null` when nothing was held), and no `jobId`, `deviceId`, `approvalNonce` or `planSHA256`.

| Request | Row |
| --- | --- |
| Approval | One helper row once Y8c has appended its nonce:  the Y9 `plan_mismatch` refusal, the Y10 outcome, or a fault after Y8c (its `result` is `failed` or `unknown`, the outcome's word, §12 Faults).  This holds for `resource-policy` too;  `fleet_resource_control` writes its own rows in its own file as it does today.  Refusals before Y8c (including `clock_unsynced`, `expired`, `action_busy`, `trust_list_changed`, `ledger_corrupt` and `replayed_nonce` at Y8a, `rate_limited` at Y8b, and `internal_error`) write none;  an approval refused `rate_limited` and retried gets its one row only when a retry spends its nonce.  So each `approvalNonce` gets at most one helper row, ever, and replaying an approval adds no row.  |
| Chain | One row at C5 when accepted (with the new version and body hash, Fields above), appended after the stored list is durable;  a failed append is not a fault (§12, v5.3);  the row is best effort (v5.4):  a row means the list was durable, but a held list may have no row (after a failed directory `fsync`, or a process that died between the rename and the append), so `current --plan`, not the log, is authoritative for what is held.  A refused chain writes none until §17A is settled.  |
| Ledger repair | One row when a final fragment is completed or truncated (§11).  |
| `--plan`, `current`, reads | None.  |

**What this bounds.**  An exploited `muster-exec` can add at most one row per genuine approval it ever carried (rows the owner's own actions write anyway), and a refused chain adds none, so it cannot push the owner's rows out of the 50-row tail the phone reads.

## 15.  Error Codes

| Code | Where |
| --- | --- |
| `helper_key_missing` | P0, unsigned (includes an unsafe state directory);  also, unsigned, a later answer whose signing fails and fails again when its `internal_error` refusal is signed, as at C5 after the rename, where the list is held (v5.4, §12) |
| `pin_unsafe` | P1 only (includes a record that fails §9's settled format) |
| `bad_command`, `mode_not_allowed` | P2 (§4:  not counted, no input read, no audit row) |
| `input_too_large`, `bad_input` | P3 (`bad_input` includes equal nonces, non-ASCII strings and malformed chain entries) |
| `rate_limited` | Y8b (verified, the nonce not spent), R1, C1, K1, including a rate lock held more than 2 seconds |
| `state_unsafe` | Y2, C2 (including a held list whose body hash is not its history line's `toSHA256`, v5.3, with `helperTrustListVersion` `null`, v5.4), K2, or any state file (§11, §12);  verified only from Y7 (the stored list read again), Y8a (the ledger) or Y8b (the rate file), all before Y8c (v5.2) |
| `no_trust_list` | Y2 |
| `field_mismatch`, `unknown_device`, `bad_signature` | Y3, Y4, Y5 |
| `clock_unsynced`, `window_too_long`, `not_yet_valid`, `expired` | Y6 |
| `action_busy`, `trust_list_changed` | Y7, C4 |
| `ledger_corrupt`, `replayed_nonce` | Y8a |
| `plan_mismatch` | Y9, verified, the nonce spent at Y8c |
| `plan_refused` | R2, with `reason` exactly one of:  the planner's own `error_code` (§4), which is `update_busy`, `authorization_unavailable`, `update_route_unavailable`, `update_executable_unavailable`, `unit_status_unavailable`, `unit_status_invalid`, `unit_unavailable`, `unit_type_unsupported`, `update_route_changed`, `receipt_context_mismatch` or `receipt_context_unavailable`;  else `allowlist_miss`, `report_stale` (the status report is missing, unsafe, not JSON, not from `fleet-server-status`, older than 45 minutes, not listing the uuid, or not a redeployable row) or `revision_stale`.  After Y8c the same reason appears only as a result's `detail` `plan_refused: <reason>` (§12 Faults) |
| `internal_error` | Any step before Y8c spends the nonce, or R2, C1 to C5 (at C5, once the rename has happened, the list is held, §12, v5.3;  a signing fault there that repeats is `helper_key_missing`, v5.4), K1 to K3;  verified when it comes from Y6 to Y8c, and at Y8c the nonce's line may already be on disk (§12 Faults, v5.2) |
| `chain_refused` | C3, `reason` one of `version_gap`, `previous_hash`, `owner_key`, `history_hash` (v5.3), `owner_signature`, `duplicate_device`, `countersignature`, `closed_rule`, `first_rule`, `services_changed`, `server_keys_removed`, `reopen_unavailable` (until §17A is settled), and, in the OPEN proposal, `reopen_unrecorded`, `reopen_tag`, `reopen_device` |

Exit status:  0 planned, submitted, done or acknowledged;  1 failed or unknown;  2 refused.

**What The Phone Does Next (v5.2).**  After a refusal of an approval, the phone acts on the code and on `verified` (§13) as below.  A resend is the same approval document sent again.  A new approval has a new `approvalNonce`, new times (§7) and a new Face ID signature.  How a refusal is shown to the owner is the design's reading rule, unchanged:  every refusal except a verified `plan_mismatch` shows as not known whether it ran, until a signed result settles it, so before it signs anything new the phone asks for the job's result again.

| Refusal | The Approval's Nonce | What The Phone Does Next |
| --- | --- | --- |
| `plan_mismatch` (verified, Y9) | Spent at Y8c | Plan again (`--plan`), show a new Plan Review, and sign a new approval over the new `planSHA256`.  A resend of this approval can only be `replayed_nonce`.  |
| `expired` (verified, Y6) | Not spent by this request, but maybe spent by an earlier send | This request spent nothing, but the same approval may already have run on an earlier send whose answer was lost, and a retry after the window always meets `expired`.  So the phone asks for the job's result first (as above) and signs a new approval, with new times (§7), only when that result shows the action did not run;  until then it shows the job as not known whether it ran.  A new approval may name the same `planSHA256`, since Y9 compares it with a fresh plan anyway.  |
| `not_yet_valid`, `window_too_long` (verified, Y6) | Not spent | Sign a new approval with new times (§7);  an approval refused as not yet valid, or for a window that is too long, never ran.  It may name the same `planSHA256`, since Y9 compares it with a fresh plan anyway;  if the plan has changed since, the answer is `plan_mismatch`.  |
| `state_unsafe`, `internal_error` (verified) | Maybe spent | Treat as maybe spent:  never resend this approval (if its nonce was spent, a resend can only be `replayed_nonce`);  plan again and sign a new approval.  A verified `state_unsafe` comes from Y7 (the stored list read again), Y8a (the ledger) or Y8b (the rate file), all before Y8c, so by this spec its nonce is not spent.  A verified `internal_error` comes from any step from Y6 to Y8c, and at Y8c the nonce's line may have reached the disk before its write or `fsync` failed (§12 Faults).  The answer does not name its step, so the phone treats both codes alike.  A `state_unsafe` stays until the file is repaired in a shell (§11, §12), so a new approval meets the same refusal until then.  |
| `rate_limited` (verified, Y8b), `action_busy` (verified, Y7) | Not spent | Resend the same approval inside its window (§12);  once the window has passed, sign a new approval.  |
| `replayed_nonce` (verified, Y8a) | Spent by an earlier use | Never resend.  Ask for the job's result;  if the owner chooses to act again, plan again and sign a new approval.  |

For any other refusal the phone resends nothing and signs nothing new on its own.

## 16.  Test Vectors

Generated by `make_vectors_v5.py` with `cryptography` 46.0.5 under `/usr/bin/python3 -I` (Python 3.14.4).  Every vector was checked by the script's reference checker before it was written, and `check_vectors_v5.py` re-checks the printed vectors from this file alone.  **The rule C, reopen-record and recovery vectors (T4, T5's last step, T8, T15, T18, T20, T21, T22, R1 to R5) illustrate the OPEN proposal of §9 and will change when §17A is settled;  under version 5 as frozen, every rule C entry is refused `reopen_unavailable`.**  **Every key below is a test key derived from a fixed label (the whole bold heading, parenthesis included) and is used nowhere else.**  A harness resets the ledger and the rate file between approval vectors, because several reuse one approvalNonce, and one `verb target` at one clock.

### Test Keys

Each private key is the P-256 scalar `int(SHA-256("muster test key " + label)) mod (n - 1) + 1`, where the label is the bold heading below.  Only the public halves are printed:  a test harness derives each private key from its label, and private key blocks are kept out of the repository so its secret scanning stays meaningful.

**owner**  (fingerprint `2d0a d701 cd97 12e6 2fc0 0c40 08b8 1f44 2427 a5b1 c565 b548 1c97 70d5 1e88 aef2`)

```
public, base64url SPKI DER:  MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEVzkkcfsegaoVRZFzkDIoVF5bEqxUarf9x8v30K8pWB_JGjq4wKAmg96uVQ704aEAZtLFNi_PbZfc69WlOvJovw
```

**owner after rotation (T24 only)**  (fingerprint `f57d 7b3a ed19 d1cf 752b 3a29 f523 2a34 e7a0 fa9f c8b5 f0c8 2059 909a cdbd dc3d`)

```
public, base64url SPKI DER:  MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEupDAVWZ7zS6qwoDX4eW_6TvQjFea1VKKb5WgUXz_nASY71O6DavTImTpgwYibD3eVn_C_mgjpEsYh6Do7Crpsw
```

**rogue owner (not pinned)**  (fingerprint `40ed 8ebe 86c0 9c00 2acb d2d2 a44c ceba 08ee f9d8 4c30 f5a8 8883 ab92 c214 1d95`)

```
public, base64url SPKI DER:  MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEBeBfmdTfUbAQUH7Kn-LAXnK7MimR4Lg0OOCy8BI6RXyduxyJtlDpjqGgL6nSMQBY5bwmQsZ1g6QuSCxhz4I1aQ
```

**server helper**  (fingerprint `38fb d100 25f4 12d0 13b6 17d0 fcb5 c48b 6025 60e7 ffd5 b14e ac66 aa43 a42d 0f07`)

```
public, base64url SPKI DER:  MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAE7eq30mr1oP043ORJ4UfBGVRKXGDc655WHh8DvUh6Jdbp3jTGbZSbdaiTTFrK5bdAkMBRMt8uVfIQeyCAQv5OBg
```

**device A approval**  (fingerprint `ae94 28d6 95e4 14bc efc0 ef5b 14b2 1a69 ba27 49a3 a39b 628d 5583 c5c7 4472 1a68`)

```
public, base64url SPKI DER:  MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEgLxT0run_JYlj2CY2lEYFAhCfGMoh-1Q2HxSODhM4j0boQdGe-OA9Sa_WOPmi0p15BDqD_ZxBkiKyYljQyj0Lg
```

**device A request**  (fingerprint `7983 2322 8ddf 18cd 2b50 db29 0c96 4e69 75f7 0677 d1f5 24b1 d772 560f e5d8 3739`)

```
public, base64url SPKI DER:  MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAENKYBMrkHEF5vbsQDSgfcQUiD_12yOXThq2Nn06nVIeFabNxtAoJZBWoi4GSXcnf_CZ1eBGiAOp_GLzJJktVBUg
```

**device B approval**  (fingerprint `629e 6234 3180 ad88 3d8f 7a47 bd2a 2a15 3f95 2eac a128 0c3d 55da c659 5260 549a`)

```
public, base64url SPKI DER:  MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEuupAUkUqXTeWrCnK_xO-dZ-76OtdQ8e6ftZNWdPKGnllCdHfNA3pwHxj7I84TnMKeBWEGpILKbT_pJMyOr3urg
```

**device C approval**  (fingerprint `f4e3 96a6 7cee fe6a 551a bbc0 4252 4575 8b4f 9c81 e8ec 8814 f9d6 8ea3 ff16 05f4`)

```
public, base64url SPKI DER:  MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAETH7k5HURk_7DG-eMy6GVf66aD_dyBNIDRLa3uTE8mP92HxEXdfjyCUxsXHR34I0zbGhv7AWgXnX6fPjnCPKg6w
```

**device X approval (not on any list)**  (fingerprint `3777 a9e7 4253 6770 5d2e 9b4f 2325 6e85 ebdb 31b3 de5d b90d ca81 7bd3 b289 37cd`)

```
public, base64url SPKI DER:  MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAE7LCZF3FUIllAX5hkuQv0rHU7bl5AuQLJ5Pmd21vcQkK-_kfyZdqKoMeXhkaAzD2uOTeu7NLy9X-vzi6Nt6RZIw
```

**rogue helper (not enrolled)**  (fingerprint `34be 9604 70e0 e4e5 753a aa11 66d7 3b8a ef20 b05a b453 7467 54d5 2a05 4fb5 660c`)

```
public, base64url SPKI DER:  MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAESUQZoy7P6Eua62UCeJvTZF7LzCX2Jb4L9NN_raKYT6_yDrRqx-ud2pf-Ogw66b1Dsj8-Hfcb82GN-8sUCuqhHw
```

The reading and notification keys of devices A, B and C use the labels `device A reading`, `device A notification` and so on;  only their public halves appear, inside the lists.

### Canonical Form

This object, given here out of order and with spaces, has a null object member and keys that sort differently by bytes than by case or by number:

```
{"z": "fleet-ops-plan/1", "a10": ["/usr/bin/systemctl", 7, true, null], "B": null, "a9": {"y": "a\"b\\c", "b": -3}, "a": "x"}
```

Its canonical bytes, exactly:  `B` before `a`, `a10` before `a9`, the null member present, `/` not escaped:

```
{"B":null,"a":"x","a10":["/usr/bin/systemctl",7,true,null],"a9":{"b":-3,"y":"a\"b\\c"},"z":"fleet-ops-plan/1"}
```

SHA-256:  `ed2ca98a1b511830878f0ac78679fc93aa79617fa3cb99001328163f55705ce4`

### Pin, Owner History And Reopen Record

`/etc/muster/owner.pub`:

```
MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEVzkkcfsegaoVRZFzkDIoVF5bEqxUarf9x8v30K8pWB_JGjq4wKAmg96uVQ704aEAZtLFNi_PbZfc69WlOvJovw
```

`/etc/muster/owner-history.jsonl` (one line):

```
{"fromVersion":1,"ownerKey":"MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEVzkkcfsegaoVRZFzkDIoVF5bEqxUarf9x8v30K8pWB_JGjq4wKAmg96uVQ704aEAZtLFNi_PbZfc69WlOvJovw","toSHA256":null,"toVersion":null}
```

`/etc/muster/reopen-codes.jsonl` (two lines for version 4 and device C, `d-f4e396a67ceefe6a551abbc0`;  the first code was issued and then retired when the owner ran `reopen` again for version 4, and the second was used):

```
{"code":"9d0b3e51a7c2f4086e1d5b9a3c7f2e04","createdAt":"2026-11-07T20:30:00Z","deviceId":"d-f4e396a67ceefe6a551abbc0","version":4}
{"code":"4f2a9c0e7b1d6e3a8c5f0b2d9e7a1c4b","createdAt":"2026-11-07T20:45:00Z","deviceId":"d-f4e396a67ceefe6a551abbc0","version":4}
```

How `muster-agent reopen --version 4 --device d-f4e396a67ceefe6a551abbc0` prints the second code with its check group:  `4f2a-9c0e-7b1d-6e3a-8c5f-0b2d-9e7a-1c4b a780`.  The same code checked against version 5 gives `a121`, and against device B (`d-629e62343180ad883d8f7a47`) gives `dcab`.  The retired first code's check group for version 4 and device C was `f6c1`.

`/etc/muster/owner-history.jsonl` for T24 only (two lines, v5.3:  the owner key rotated after version 1, so the closed line names version 1's body hash, and `owner.pub` holds the `owner after rotation (T24 only)` key):

```
{"fromVersion":1,"ownerKey":"MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEVzkkcfsegaoVRZFzkDIoVF5bEqxUarf9x8v30K8pWB_JGjq4wKAmg96uVQ704aEAZtLFNi_PbZfc69WlOvJovw","toSHA256":"0dfd951c5317bb228f6e95931175b59d3e0a206e08b5e4c2fed06070b7ff934e","toVersion":1}
{"fromVersion":2,"ownerKey":"MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEupDAVWZ7zS6qwoDX4eW_6TvQjFea1VKKb5WgUXz_nASY71O6DavTImTpgwYibD3eVn_C_mgjpEsYh6Do7Crpsw","toSHA256":null,"toVersion":null}
```

### Trust Lists

**Version 1**, canonical body SHA-256 `0dfd951c5317bb228f6e95931175b59d3e0a206e08b5e4c2fed06070b7ff934e`, entry SHA-256 `0110cd45b99ad0ac33ae668c2f0e2bef60c5c82e677dd53b697c95d3313c7af0`:

```json
{
  "body": {
    "closed": false,
    "contract": "muster-trust-list/1",
    "devices": [
      {
        "approvalKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEgLxT0run_JYlj2CY2lEYFAhCfGMoh-1Q2HxSODhM4j0boQdGe-OA9Sa_WOPmi0p15BDqD_ZxBkiKyYljQyj0Lg",
        "deviceId": "d-ae9428d695e414bcefc0ef5b",
        "name": "Test Phone A",
        "notificationKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAETc9UlPQaEGmS86h02BDlWsQALVfu2oYjQHRbDkyhxmec9zFNnpDTkWOX-Ki1D1Ty3BKQIAkVU6cp0FyJ5AsZYQ",
        "readingKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEa_YIgLInEJOjngBXp3gB4F6BWt_SAEZMeOU0yDY-uyRpmrHR7-5x2LLCAEZY6QpIq_VcytQlySQQAoy9y86sIw",
        "requestKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAENKYBMrkHEF5vbsQDSgfcQUiD_12yOXThq2Nn06nVIeFabNxtAoJZBWoi4GSXcnf_CZ1eBGiAOp_GLzJJktVBUg"
      }
    ],
    "issuedAt": "2026-11-07T20:00:00Z",
    "ownerKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEVzkkcfsegaoVRZFzkDIoVF5bEqxUarf9x8v30K8pWB_JGjq4wKAmg96uVQ704aEAZtLFNi_PbZfc69WlOvJovw",
    "previousSHA256": null,
    "reopenTag": null,
    "services": {
      "macAgreement": null,
      "macExecutor": null,
      "macPublisher": null,
      "serverExecutor": null,
      "serverExecutorAgreement": null,
      "serverHelper": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAE7eq30mr1oP043ORJ4UfBGVRKXGDc655WHh8DvUh6Jdbp3jTGbZSbdaiTTFrK5bdAkMBRMt8uVfIQeyCAQv5OBg",
      "serverPublisher": null
    },
    "version": 1
  },
  "countersignatures": [],
  "ownerSignature": "MEQCIEoIHgh5tSTYaS0VmQK23YZB80t_8iCa2eeQXXNNoMSvAiBDN7gpqShrSE1rKRDffXhzpr6m-ogmVX9Ua-ZGyGXuWQ"
}
```

**Version 2 (rule A)**, canonical body SHA-256 `c2b6d904493faab638728e46b4e43adf939825611f70e3190671af90ee511024`, entry SHA-256 `87d086a0aafbddecca8c85e87c3ec2acf6c953604195dfb8974c8653b644f11c`:

```json
{
  "body": {
    "closed": false,
    "contract": "muster-trust-list/1",
    "devices": [
      {
        "approvalKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEgLxT0run_JYlj2CY2lEYFAhCfGMoh-1Q2HxSODhM4j0boQdGe-OA9Sa_WOPmi0p15BDqD_ZxBkiKyYljQyj0Lg",
        "deviceId": "d-ae9428d695e414bcefc0ef5b",
        "name": "Test Phone A",
        "notificationKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAETc9UlPQaEGmS86h02BDlWsQALVfu2oYjQHRbDkyhxmec9zFNnpDTkWOX-Ki1D1Ty3BKQIAkVU6cp0FyJ5AsZYQ",
        "readingKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEa_YIgLInEJOjngBXp3gB4F6BWt_SAEZMeOU0yDY-uyRpmrHR7-5x2LLCAEZY6QpIq_VcytQlySQQAoy9y86sIw",
        "requestKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAENKYBMrkHEF5vbsQDSgfcQUiD_12yOXThq2Nn06nVIeFabNxtAoJZBWoi4GSXcnf_CZ1eBGiAOp_GLzJJktVBUg"
      },
      {
        "approvalKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEuupAUkUqXTeWrCnK_xO-dZ-76OtdQ8e6ftZNWdPKGnllCdHfNA3pwHxj7I84TnMKeBWEGpILKbT_pJMyOr3urg",
        "deviceId": "d-629e62343180ad883d8f7a47",
        "name": "Test iPad B",
        "notificationKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAE_bA9-74nJfUWF1TD3uDhUMf_aVzkanRJaNlEVktoBFLNh8q5pVQGt60EPbZxxPKws9ZEa324ck7CIuKTOXKbMA",
        "readingKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAES3Gma5eyi9qXqF_Vm1wmgoCzO--_JXrGwLucGoRP8rz_pSI_p_W-AOULR1jsYIbIgRASoNX96Y1swYGCB12cpQ",
        "requestKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEVWOuw9sD6n17pCEXE7zqP_HsA7Unt3pigQFwB-BvnKq93d2MT8XODx-KN05M36ZZiyIDAlhHSWqmKp7XmrZMXw"
      }
    ],
    "issuedAt": "2026-11-07T20:10:00Z",
    "ownerKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEVzkkcfsegaoVRZFzkDIoVF5bEqxUarf9x8v30K8pWB_JGjq4wKAmg96uVQ704aEAZtLFNi_PbZfc69WlOvJovw",
    "previousSHA256": "0dfd951c5317bb228f6e95931175b59d3e0a206e08b5e4c2fed06070b7ff934e",
    "reopenTag": null,
    "services": {
      "macAgreement": null,
      "macExecutor": null,
      "macPublisher": null,
      "serverExecutor": null,
      "serverExecutorAgreement": null,
      "serverHelper": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAE7eq30mr1oP043ORJ4UfBGVRKXGDc655WHh8DvUh6Jdbp3jTGbZSbdaiTTFrK5bdAkMBRMt8uVfIQeyCAQv5OBg",
      "serverPublisher": null
    },
    "version": 2
  },
  "countersignatures": [
    {
      "deviceId": "d-ae9428d695e414bcefc0ef5b",
      "signature": "MEQCIDYTYV54V_tX9CKZhk9P7RG6H0eKLKEnZiq0o5SfQuuCAiAHP9hX8KctsDEFpKTknVzrWngPo_CawkTJHlVPXc06Xw"
    }
  ],
  "ownerSignature": "MEYCIQCFuKCTZ523TAkne09GVbLaFQbQwlDfVzbpRo2vpzRxBgIhAMczoI9kkV8YAyeBqh90KIEP6w2CVjs62H700g6iP-HR"
}
```

**Version 3 (rule B, closed)**, canonical body SHA-256 `5e06787afce950d2f803ac3716dc9ee0d033798f2803eb3393b6b3fa53e88ce8`, entry SHA-256 `92ed746bbc595f8b45e5d8f44398fcfba052e9d49dd3ee8b6a706e37c6e86966`:

```json
{
  "body": {
    "closed": true,
    "contract": "muster-trust-list/1",
    "devices": [],
    "issuedAt": "2026-11-07T20:20:00Z",
    "ownerKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEVzkkcfsegaoVRZFzkDIoVF5bEqxUarf9x8v30K8pWB_JGjq4wKAmg96uVQ704aEAZtLFNi_PbZfc69WlOvJovw",
    "previousSHA256": "c2b6d904493faab638728e46b4e43adf939825611f70e3190671af90ee511024",
    "reopenTag": null,
    "services": {
      "macAgreement": null,
      "macExecutor": null,
      "macPublisher": null,
      "serverExecutor": null,
      "serverExecutorAgreement": null,
      "serverHelper": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAE7eq30mr1oP043ORJ4UfBGVRKXGDc655WHh8DvUh6Jdbp3jTGbZSbdaiTTFrK5bdAkMBRMt8uVfIQeyCAQv5OBg",
      "serverPublisher": null
    },
    "version": 3
  },
  "countersignatures": [],
  "ownerSignature": "MEUCIQC4Cy7X1oppCMJBikG3expZKgokyrOVI6hjE158dIySXgIgaV_WSIz7tYYFLW38niZwFoSU2x2hf4rVKkjgIebLHDA"
}
```

**Version 4 (rule C, reopened)**, canonical body SHA-256 `5911cecb01736c03a12bd355b3d74ace6e0164b78d2dac4f0ee2cc81fa711e0f`, entry SHA-256 `31c2a7574fa5821520b2cdea5894ad056518be43c98b19fb208f8e4d8ae529d4`:

```json
{
  "body": {
    "closed": false,
    "contract": "muster-trust-list/1",
    "devices": [
      {
        "approvalKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAETH7k5HURk_7DG-eMy6GVf66aD_dyBNIDRLa3uTE8mP92HxEXdfjyCUxsXHR34I0zbGhv7AWgXnX6fPjnCPKg6w",
        "deviceId": "d-f4e396a67ceefe6a551abbc0",
        "name": "Test Phone C",
        "notificationKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEH-LOtR0pZ4orjdPxCYFfcdDQE-d2eXu_PdcNi1LBbnoqwMECfkwCwmFjjwClFRIBG0HSthRfgZDrEA_lasDHcg",
        "readingKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAED0rgDuFHSq-VF_k0617gyxsiHFtUDxsfckW3O1JTVER4MvMB_Ua3isHPgsjowUyL8_2wOhTroMi4mHR3bY_2tg",
        "requestKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEKCWVuPOr3TdZSendYJNllk98wOyLQ95mAdO7y4Hwqm5ZV-T4ISvA2sZy_tfact4OYzDc0pYxciaedztE5EKi8Q"
      }
    ],
    "issuedAt": "2026-11-07T20:50:00Z",
    "ownerKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEVzkkcfsegaoVRZFzkDIoVF5bEqxUarf9x8v30K8pWB_JGjq4wKAmg96uVQ704aEAZtLFNi_PbZfc69WlOvJovw",
    "previousSHA256": "5e06787afce950d2f803ac3716dc9ee0d033798f2803eb3393b6b3fa53e88ce8",
    "reopenTag": "98e5f483cf1d368013391fb0437be628a7105d78712382ddb9be9b4c82c9c8e9",
    "services": {
      "macAgreement": null,
      "macExecutor": null,
      "macPublisher": null,
      "serverExecutor": null,
      "serverExecutorAgreement": null,
      "serverHelper": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAE7eq30mr1oP043ORJ4UfBGVRKXGDc655WHh8DvUh6Jdbp3jTGbZSbdaiTTFrK5bdAkMBRMt8uVfIQeyCAQv5OBg",
      "serverPublisher": null
    },
    "version": 4
  },
  "countersignatures": [
    {
      "deviceId": "d-f4e396a67ceefe6a551abbc0",
      "signature": "MEQCIDB2JPuz7Pln2qYoYDN1KF4fELrTiirU5znbhd0d_cGKAiAy9McQ8yq6ntfFiBLWkXEFkyC1gF0np873UruEEDIKLw"
    }
  ],
  "ownerSignature": "MEUCIQDR0Lv-HcSFsAsR-39-qSn8MjL3mizHd8ltR8cy8LfP9wIgO2tuOksY4ScwjQLkFEZ88hCvzU9-tefjShWWEJUzP5Q"
}
```

**Version 4, signed again at 20:55 (T22, R5)**, canonical body SHA-256 `a477b60535bfe9d1427d4187d38cca29bf7065096d7a7ec7d809653b1be15b42`, entry SHA-256 `a59115d174c2b006b2d5fa6b529140516fd474da6afc73c5e2b4eb8bb99d1e98`:

```json
{
  "body": {
    "closed": false,
    "contract": "muster-trust-list/1",
    "devices": [
      {
        "approvalKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAETH7k5HURk_7DG-eMy6GVf66aD_dyBNIDRLa3uTE8mP92HxEXdfjyCUxsXHR34I0zbGhv7AWgXnX6fPjnCPKg6w",
        "deviceId": "d-f4e396a67ceefe6a551abbc0",
        "name": "Test Phone C",
        "notificationKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEH-LOtR0pZ4orjdPxCYFfcdDQE-d2eXu_PdcNi1LBbnoqwMECfkwCwmFjjwClFRIBG0HSthRfgZDrEA_lasDHcg",
        "readingKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAED0rgDuFHSq-VF_k0617gyxsiHFtUDxsfckW3O1JTVER4MvMB_Ua3isHPgsjowUyL8_2wOhTroMi4mHR3bY_2tg",
        "requestKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEKCWVuPOr3TdZSendYJNllk98wOyLQ95mAdO7y4Hwqm5ZV-T4ISvA2sZy_tfact4OYzDc0pYxciaedztE5EKi8Q"
      }
    ],
    "issuedAt": "2026-11-07T20:55:00Z",
    "ownerKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEVzkkcfsegaoVRZFzkDIoVF5bEqxUarf9x8v30K8pWB_JGjq4wKAmg96uVQ704aEAZtLFNi_PbZfc69WlOvJovw",
    "previousSHA256": "5e06787afce950d2f803ac3716dc9ee0d033798f2803eb3393b6b3fa53e88ce8",
    "reopenTag": "b199d430607e77d3e6a4ab354cf1a52a4faec626b2b994bb200c22d68fb5e1b0",
    "services": {
      "macAgreement": null,
      "macExecutor": null,
      "macPublisher": null,
      "serverExecutor": null,
      "serverExecutorAgreement": null,
      "serverHelper": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAE7eq30mr1oP043ORJ4UfBGVRKXGDc655WHh8DvUh6Jdbp3jTGbZSbdaiTTFrK5bdAkMBRMt8uVfIQeyCAQv5OBg",
      "serverPublisher": null
    },
    "version": 4
  },
  "countersignatures": [
    {
      "deviceId": "d-f4e396a67ceefe6a551abbc0",
      "signature": "MEUCIQDeWNKle0weRXipGwQTz8Q152R6UrqV6OM46oqejENyPwIgdF0IoYKJMOWLGzCc_yIvZSyjsVTRDoZTgOerNRbcfT0"
    }
  ],
  "ownerSignature": "MEUCIAFrdPeP4rX7Z4ThaoiyzZWFdZW1FWTmIHPY-vOPH1LzAiEAjt-3ef-sLqAHMWgP_KhdfsuO7DfyompJJa6qr8ytbeA"
}
```

### Bad Lists

**T6 list**, entry SHA-256 `1bd580e86a50cc2c8da11ec23d0ee7580a3c55624fdc9788a839ce4d2894bd1a`:

```json
{
  "body": {
    "closed": true,
    "contract": "muster-trust-list/1",
    "devices": [],
    "issuedAt": "2026-11-07T20:10:00Z",
    "ownerKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEVzkkcfsegaoVRZFzkDIoVF5bEqxUarf9x8v30K8pWB_JGjq4wKAmg96uVQ704aEAZtLFNi_PbZfc69WlOvJovw",
    "previousSHA256": "0dfd951c5317bb228f6e95931175b59d3e0a206e08b5e4c2fed06070b7ff934e",
    "reopenTag": null,
    "services": {
      "macAgreement": null,
      "macExecutor": null,
      "macPublisher": null,
      "serverExecutor": null,
      "serverExecutorAgreement": null,
      "serverHelper": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAESUQZoy7P6Eua62UCeJvTZF7LzCX2Jb4L9NN_raKYT6_yDrRqx-ud2pf-Ogw66b1Dsj8-Hfcb82GN-8sUCuqhHw",
      "serverPublisher": null
    },
    "version": 2
  },
  "countersignatures": [],
  "ownerSignature": "MEUCICW2-FeDJ-0Ue0wZxH_ZZQI7L6n7P9EBBei2vHpky9aFAiEAhmEBazsuNAGcyyowY2y6iKRhoCPAThUwwiwfycF5UlI"
}
```

**T7 list**, entry SHA-256 `540040efe15dcefb0c5f1d31749bafd66c22475e5630a327f4e817cdee5ececc`:

```json
{
  "body": {
    "closed": false,
    "contract": "muster-trust-list/1",
    "devices": [
      {
        "approvalKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEgLxT0run_JYlj2CY2lEYFAhCfGMoh-1Q2HxSODhM4j0boQdGe-OA9Sa_WOPmi0p15BDqD_ZxBkiKyYljQyj0Lg",
        "deviceId": "d-ae9428d695e414bcefc0ef5b",
        "name": "Test Phone A",
        "notificationKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAETc9UlPQaEGmS86h02BDlWsQALVfu2oYjQHRbDkyhxmec9zFNnpDTkWOX-Ki1D1Ty3BKQIAkVU6cp0FyJ5AsZYQ",
        "readingKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEa_YIgLInEJOjngBXp3gB4F6BWt_SAEZMeOU0yDY-uyRpmrHR7-5x2LLCAEZY6QpIq_VcytQlySQQAoy9y86sIw",
        "requestKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAENKYBMrkHEF5vbsQDSgfcQUiD_12yOXThq2Nn06nVIeFabNxtAoJZBWoi4GSXcnf_CZ1eBGiAOp_GLzJJktVBUg"
      },
      {
        "approvalKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEuupAUkUqXTeWrCnK_xO-dZ-76OtdQ8e6ftZNWdPKGnllCdHfNA3pwHxj7I84TnMKeBWEGpILKbT_pJMyOr3urg",
        "deviceId": "d-629e62343180ad883d8f7a47",
        "name": "Test iPad B",
        "notificationKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAE_bA9-74nJfUWF1TD3uDhUMf_aVzkanRJaNlEVktoBFLNh8q5pVQGt60EPbZxxPKws9ZEa324ck7CIuKTOXKbMA",
        "readingKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAES3Gma5eyi9qXqF_Vm1wmgoCzO--_JXrGwLucGoRP8rz_pSI_p_W-AOULR1jsYIbIgRASoNX96Y1swYGCB12cpQ",
        "requestKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEVWOuw9sD6n17pCEXE7zqP_HsA7Unt3pigQFwB-BvnKq93d2MT8XODx-KN05M36ZZiyIDAlhHSWqmKp7XmrZMXw"
      }
    ],
    "issuedAt": "2026-11-07T20:10:00Z",
    "ownerKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEVzkkcfsegaoVRZFzkDIoVF5bEqxUarf9x8v30K8pWB_JGjq4wKAmg96uVQ704aEAZtLFNi_PbZfc69WlOvJovw",
    "previousSHA256": "0000000000000000000000000000000000000000000000000000000000000000",
    "reopenTag": null,
    "services": {
      "macAgreement": null,
      "macExecutor": null,
      "macPublisher": null,
      "serverExecutor": null,
      "serverExecutorAgreement": null,
      "serverHelper": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAE7eq30mr1oP043ORJ4UfBGVRKXGDc655WHh8DvUh6Jdbp3jTGbZSbdaiTTFrK5bdAkMBRMt8uVfIQeyCAQv5OBg",
      "serverPublisher": null
    },
    "version": 2
  },
  "countersignatures": [
    {
      "deviceId": "d-ae9428d695e414bcefc0ef5b",
      "signature": "MEYCIQDmVC5PrE99Ih2d1s96ZkFcIEg07EP-jXGBuYGJ9nNxBwIhAKS0-hsnudSBFBzQL6r6OGrqljrQxEnf9_ZWUeYFyiSc"
    }
  ],
  "ownerSignature": "MEQCIGP6vYReSz9LDrop-FCOwedo9N8nsL2iBucXlzYBKLQIAiBLVa2gdZQrx-Q__XkOfrZ1WFSDgLWwCBCJJiwWUMUHcA"
}
```

**T8 list**, entry SHA-256 `bdd8258defb64bf1be890c630356d20166d5c5248352cd7ee7b07358f15cdfb4`:

```json
{
  "body": {
    "closed": false,
    "contract": "muster-trust-list/1",
    "devices": [
      {
        "approvalKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAETH7k5HURk_7DG-eMy6GVf66aD_dyBNIDRLa3uTE8mP92HxEXdfjyCUxsXHR34I0zbGhv7AWgXnX6fPjnCPKg6w",
        "deviceId": "d-f4e396a67ceefe6a551abbc0",
        "name": "Test Phone C",
        "notificationKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEH-LOtR0pZ4orjdPxCYFfcdDQE-d2eXu_PdcNi1LBbnoqwMECfkwCwmFjjwClFRIBG0HSthRfgZDrEA_lasDHcg",
        "readingKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAED0rgDuFHSq-VF_k0617gyxsiHFtUDxsfckW3O1JTVER4MvMB_Ua3isHPgsjowUyL8_2wOhTroMi4mHR3bY_2tg",
        "requestKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEKCWVuPOr3TdZSendYJNllk98wOyLQ95mAdO7y4Hwqm5ZV-T4ISvA2sZy_tfact4OYzDc0pYxciaedztE5EKi8Q"
      }
    ],
    "issuedAt": "2026-11-07T20:50:00Z",
    "ownerKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEVzkkcfsegaoVRZFzkDIoVF5bEqxUarf9x8v30K8pWB_JGjq4wKAmg96uVQ704aEAZtLFNi_PbZfc69WlOvJovw",
    "previousSHA256": "5e06787afce950d2f803ac3716dc9ee0d033798f2803eb3393b6b3fa53e88ce8",
    "reopenTag": "cf21cb1b1ae0a19198d139ae53228122f57996b742561545ad5dd49b3708c422",
    "services": {
      "macAgreement": null,
      "macExecutor": null,
      "macPublisher": null,
      "serverExecutor": null,
      "serverExecutorAgreement": null,
      "serverHelper": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAE7eq30mr1oP043ORJ4UfBGVRKXGDc655WHh8DvUh6Jdbp3jTGbZSbdaiTTFrK5bdAkMBRMt8uVfIQeyCAQv5OBg",
      "serverPublisher": null
    },
    "version": 4
  },
  "countersignatures": [
    {
      "deviceId": "d-f4e396a67ceefe6a551abbc0",
      "signature": "MEUCIQC-WFgJyZOzQXnTlGK5U92oXbe0ZBb-8ye19Gg9DyUMKwIgIqkRFWpQj02Huk3kwW4b6FquRS9YUluJtAZ61VBmqZY"
    }
  ],
  "ownerSignature": "MEQCIAvrD4BBNITvGdy2JfajuJtXVZsPnlIFXr9Lh1BX78e7AiAzi-24tJKko4Xj21x0UOYlAnl5lq4EoBunIKKob2cw6A"
}
```

**T10 list**, entry SHA-256 `147f7c9202174d50a420ee636a1727ef552687e0445e7f4cb2ca8ccf238333d9`:

```json
{
  "body": {
    "closed": false,
    "contract": "muster-trust-list/1",
    "devices": [
      {
        "approvalKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEgLxT0run_JYlj2CY2lEYFAhCfGMoh-1Q2HxSODhM4j0boQdGe-OA9Sa_WOPmi0p15BDqD_ZxBkiKyYljQyj0Lg",
        "deviceId": "d-ae9428d695e414bcefc0ef5b",
        "name": "Test Phone A",
        "notificationKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAETc9UlPQaEGmS86h02BDlWsQALVfu2oYjQHRbDkyhxmec9zFNnpDTkWOX-Ki1D1Ty3BKQIAkVU6cp0FyJ5AsZYQ",
        "readingKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEa_YIgLInEJOjngBXp3gB4F6BWt_SAEZMeOU0yDY-uyRpmrHR7-5x2LLCAEZY6QpIq_VcytQlySQQAoy9y86sIw",
        "requestKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAENKYBMrkHEF5vbsQDSgfcQUiD_12yOXThq2Nn06nVIeFabNxtAoJZBWoi4GSXcnf_CZ1eBGiAOp_GLzJJktVBUg"
      }
    ],
    "issuedAt": "2026-11-07T20:20:00Z",
    "ownerKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEVzkkcfsegaoVRZFzkDIoVF5bEqxUarf9x8v30K8pWB_JGjq4wKAmg96uVQ704aEAZtLFNi_PbZfc69WlOvJovw",
    "previousSHA256": "0dfd951c5317bb228f6e95931175b59d3e0a206e08b5e4c2fed06070b7ff934e",
    "reopenTag": null,
    "services": {
      "macAgreement": null,
      "macExecutor": null,
      "macPublisher": null,
      "serverExecutor": null,
      "serverExecutorAgreement": null,
      "serverHelper": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAE7eq30mr1oP043ORJ4UfBGVRKXGDc655WHh8DvUh6Jdbp3jTGbZSbdaiTTFrK5bdAkMBRMt8uVfIQeyCAQv5OBg",
      "serverPublisher": null
    },
    "version": 3
  },
  "countersignatures": [
    {
      "deviceId": "d-ae9428d695e414bcefc0ef5b",
      "signature": "MEQCIEJt50Efc30icup-j8t-i2xwChCOfLYbSsLbMR_qwZSZAiA9zP4HMFZr__d1bqCRhoDby3WKluPeKiY12v6LFgtfdg"
    }
  ],
  "ownerSignature": "MEQCIDqGiyKA85gxRP7Me9X5c9L3IeA7L1TBcU4RfYxOT8sQAiAkN_Os0Li9TAlsyRPUW9L4KW2DBdORkIDKq33zV5tp8w"
}
```

**T11 list**, entry SHA-256 `6d7968872364fd9c30c282339891b438efa80e61921d45cd752ae50a6107dafe`:

```json
{
  "body": {
    "closed": false,
    "contract": "muster-trust-list/1",
    "devices": [
      {
        "approvalKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEgLxT0run_JYlj2CY2lEYFAhCfGMoh-1Q2HxSODhM4j0boQdGe-OA9Sa_WOPmi0p15BDqD_ZxBkiKyYljQyj0Lg",
        "deviceId": "d-ae9428d695e414bcefc0ef5b",
        "name": "Test Phone A",
        "notificationKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAETc9UlPQaEGmS86h02BDlWsQALVfu2oYjQHRbDkyhxmec9zFNnpDTkWOX-Ki1D1Ty3BKQIAkVU6cp0FyJ5AsZYQ",
        "readingKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEa_YIgLInEJOjngBXp3gB4F6BWt_SAEZMeOU0yDY-uyRpmrHR7-5x2LLCAEZY6QpIq_VcytQlySQQAoy9y86sIw",
        "requestKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAENKYBMrkHEF5vbsQDSgfcQUiD_12yOXThq2Nn06nVIeFabNxtAoJZBWoi4GSXcnf_CZ1eBGiAOp_GLzJJktVBUg"
      },
      {
        "approvalKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEuupAUkUqXTeWrCnK_xO-dZ-76OtdQ8e6ftZNWdPKGnllCdHfNA3pwHxj7I84TnMKeBWEGpILKbT_pJMyOr3urg",
        "deviceId": "d-629e62343180ad883d8f7a47",
        "name": "Test iPad B",
        "notificationKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAE_bA9-74nJfUWF1TD3uDhUMf_aVzkanRJaNlEVktoBFLNh8q5pVQGt60EPbZxxPKws9ZEa324ck7CIuKTOXKbMA",
        "readingKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAES3Gma5eyi9qXqF_Vm1wmgoCzO--_JXrGwLucGoRP8rz_pSI_p_W-AOULR1jsYIbIgRASoNX96Y1swYGCB12cpQ",
        "requestKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEVWOuw9sD6n17pCEXE7zqP_HsA7Unt3pigQFwB-BvnKq93d2MT8XODx-KN05M36ZZiyIDAlhHSWqmKp7XmrZMXw"
      }
    ],
    "issuedAt": "2026-11-07T20:10:00Z",
    "ownerKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEBeBfmdTfUbAQUH7Kn-LAXnK7MimR4Lg0OOCy8BI6RXyduxyJtlDpjqGgL6nSMQBY5bwmQsZ1g6QuSCxhz4I1aQ",
    "previousSHA256": "0dfd951c5317bb228f6e95931175b59d3e0a206e08b5e4c2fed06070b7ff934e",
    "reopenTag": null,
    "services": {
      "macAgreement": null,
      "macExecutor": null,
      "macPublisher": null,
      "serverExecutor": null,
      "serverExecutorAgreement": null,
      "serverHelper": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAE7eq30mr1oP043ORJ4UfBGVRKXGDc655WHh8DvUh6Jdbp3jTGbZSbdaiTTFrK5bdAkMBRMt8uVfIQeyCAQv5OBg",
      "serverPublisher": null
    },
    "version": 2
  },
  "countersignatures": [
    {
      "deviceId": "d-ae9428d695e414bcefc0ef5b",
      "signature": "MEUCIGxzEqBy4gcndxdhjkuLAVSQpCi8T9ID4CAdxSQm0Q00AiEAupl85-d5QVtC-0lYpKZdM14IS2jLJFdCc0B16LkzcZY"
    }
  ],
  "ownerSignature": "MEYCIQDOtMgPn1kEBih9TlIWxxZC53sxRDdYPp5xf2QjI-DzqQIhAJu9u0-PRzj80sTKBKLBVXBhoFT9KPTlEQFCdDyV3Rat"
}
```

**T12 list**, entry SHA-256 `f0ad7a3756386268582e89869d27ed682f76794ec5707304bdd9a0e9ccbdec37`:

```json
{
  "body": {
    "closed": false,
    "contract": "muster-trust-list/1",
    "devices": [
      {
        "approvalKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEgLxT0run_JYlj2CY2lEYFAhCfGMoh-1Q2HxSODhM4j0boQdGe-OA9Sa_WOPmi0p15BDqD_ZxBkiKyYljQyj0Lg",
        "deviceId": "d-ae9428d695e414bcefc0ef5b",
        "name": "Test Phone A",
        "notificationKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAETc9UlPQaEGmS86h02BDlWsQALVfu2oYjQHRbDkyhxmec9zFNnpDTkWOX-Ki1D1Ty3BKQIAkVU6cp0FyJ5AsZYQ",
        "readingKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEa_YIgLInEJOjngBXp3gB4F6BWt_SAEZMeOU0yDY-uyRpmrHR7-5x2LLCAEZY6QpIq_VcytQlySQQAoy9y86sIw",
        "requestKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAENKYBMrkHEF5vbsQDSgfcQUiD_12yOXThq2Nn06nVIeFabNxtAoJZBWoi4GSXcnf_CZ1eBGiAOp_GLzJJktVBUg"
      },
      {
        "approvalKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEuupAUkUqXTeWrCnK_xO-dZ-76OtdQ8e6ftZNWdPKGnllCdHfNA3pwHxj7I84TnMKeBWEGpILKbT_pJMyOr3urg",
        "deviceId": "d-629e62343180ad883d8f7a47",
        "name": "Test iPad B",
        "notificationKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAE_bA9-74nJfUWF1TD3uDhUMf_aVzkanRJaNlEVktoBFLNh8q5pVQGt60EPbZxxPKws9ZEa324ck7CIuKTOXKbMA",
        "readingKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAES3Gma5eyi9qXqF_Vm1wmgoCzO--_JXrGwLucGoRP8rz_pSI_p_W-AOULR1jsYIbIgRASoNX96Y1swYGCB12cpQ",
        "requestKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEVWOuw9sD6n17pCEXE7zqP_HsA7Unt3pigQFwB-BvnKq93d2MT8XODx-KN05M36ZZiyIDAlhHSWqmKp7XmrZMXw"
      }
    ],
    "issuedAt": "2026-11-07T20:10:00Z",
    "ownerKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEVzkkcfsegaoVRZFzkDIoVF5bEqxUarf9x8v30K8pWB_JGjq4wKAmg96uVQ704aEAZtLFNi_PbZfc69WlOvJovw",
    "previousSHA256": "0dfd951c5317bb228f6e95931175b59d3e0a206e08b5e4c2fed06070b7ff934e",
    "reopenTag": null,
    "services": {
      "macAgreement": null,
      "macExecutor": null,
      "macPublisher": null,
      "serverExecutor": null,
      "serverExecutorAgreement": null,
      "serverHelper": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAE7eq30mr1oP043ORJ4UfBGVRKXGDc655WHh8DvUh6Jdbp3jTGbZSbdaiTTFrK5bdAkMBRMt8uVfIQeyCAQv5OBg",
      "serverPublisher": null
    },
    "version": 2
  },
  "countersignatures": [
    {
      "deviceId": "d-ae9428d695e414bcefc0ef5b",
      "signature": "MEQCIDYTYV54V_tX9CKZhk9P7RG6H0eKLKEnZiq0o5SfQuuCAiAHP9hX8KctsDEFpKTknVzrWngPo_CawkTJHlVPXc06Xw"
    }
  ],
  "ownerSignature": "MEUCIQDE30-GFX_rUidfw_YRShwgpye5ti6gQvTcoxxGsCpEAAIgA11DYHpzbNWNkK91ib5xaVrQ79aQR48eSxzsOsInupA"
}
```

**T13 list**, entry SHA-256 `a5d38d2a96000d72ea68a93ca17cd093049b56ea6cc2fb0e43c635ee69afdb99`:

```json
{
  "body": {
    "closed": false,
    "contract": "muster-trust-list/1",
    "devices": [
      {
        "approvalKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEgLxT0run_JYlj2CY2lEYFAhCfGMoh-1Q2HxSODhM4j0boQdGe-OA9Sa_WOPmi0p15BDqD_ZxBkiKyYljQyj0Lg",
        "deviceId": "d-ae9428d695e414bcefc0ef5b",
        "name": "Test Phone A",
        "notificationKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAETc9UlPQaEGmS86h02BDlWsQALVfu2oYjQHRbDkyhxmec9zFNnpDTkWOX-Ki1D1Ty3BKQIAkVU6cp0FyJ5AsZYQ",
        "readingKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEa_YIgLInEJOjngBXp3gB4F6BWt_SAEZMeOU0yDY-uyRpmrHR7-5x2LLCAEZY6QpIq_VcytQlySQQAoy9y86sIw",
        "requestKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAENKYBMrkHEF5vbsQDSgfcQUiD_12yOXThq2Nn06nVIeFabNxtAoJZBWoi4GSXcnf_CZ1eBGiAOp_GLzJJktVBUg"
      },
      {
        "approvalKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEgLxT0run_JYlj2CY2lEYFAhCfGMoh-1Q2HxSODhM4j0boQdGe-OA9Sa_WOPmi0p15BDqD_ZxBkiKyYljQyj0Lg",
        "deviceId": "d-ae9428d695e414bcefc0ef5b",
        "name": "Test Phone A",
        "notificationKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAETc9UlPQaEGmS86h02BDlWsQALVfu2oYjQHRbDkyhxmec9zFNnpDTkWOX-Ki1D1Ty3BKQIAkVU6cp0FyJ5AsZYQ",
        "readingKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEa_YIgLInEJOjngBXp3gB4F6BWt_SAEZMeOU0yDY-uyRpmrHR7-5x2LLCAEZY6QpIq_VcytQlySQQAoy9y86sIw",
        "requestKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAENKYBMrkHEF5vbsQDSgfcQUiD_12yOXThq2Nn06nVIeFabNxtAoJZBWoi4GSXcnf_CZ1eBGiAOp_GLzJJktVBUg"
      }
    ],
    "issuedAt": "2026-11-07T20:10:00Z",
    "ownerKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEVzkkcfsegaoVRZFzkDIoVF5bEqxUarf9x8v30K8pWB_JGjq4wKAmg96uVQ704aEAZtLFNi_PbZfc69WlOvJovw",
    "previousSHA256": "0dfd951c5317bb228f6e95931175b59d3e0a206e08b5e4c2fed06070b7ff934e",
    "reopenTag": null,
    "services": {
      "macAgreement": null,
      "macExecutor": null,
      "macPublisher": null,
      "serverExecutor": null,
      "serverExecutorAgreement": null,
      "serverHelper": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAE7eq30mr1oP043ORJ4UfBGVRKXGDc655WHh8DvUh6Jdbp3jTGbZSbdaiTTFrK5bdAkMBRMt8uVfIQeyCAQv5OBg",
      "serverPublisher": null
    },
    "version": 2
  },
  "countersignatures": [
    {
      "deviceId": "d-ae9428d695e414bcefc0ef5b",
      "signature": "MEYCIQDntVslIpf3b9voKlUjtgjvYcrOKR4nz3Q0hBfiGdTcqAIhAMeLVebXlVFzbjl5QZo1zgpNaQaU7skfYZYTd1A9mj2A"
    }
  ],
  "ownerSignature": "MEYCIQCHOxw8AzIXmUSyDNpBC77Gc-xI5WHx0T_SicTeDKqlrgIhAKVHXGQYaW-RbuHH504Q-qmtxiHZuILvMjzBaDt9X-Ce"
}
```

**T14 list**, entry SHA-256 `b1c7492e460872be0e86c18701be037509837552200f49c99f6c75167925f21c`:

```json
{
  "body": {
    "closed": false,
    "contract": "muster-trust-list/1",
    "devices": [
      {
        "approvalKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEgLxT0run_JYlj2CY2lEYFAhCfGMoh-1Q2HxSODhM4j0boQdGe-OA9Sa_WOPmi0p15BDqD_ZxBkiKyYljQyj0Lg",
        "deviceId": "d-ae9428d695e414bcefc0ef5b",
        "name": "Test Phone A",
        "notificationKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAETc9UlPQaEGmS86h02BDlWsQALVfu2oYjQHRbDkyhxmec9zFNnpDTkWOX-Ki1D1Ty3BKQIAkVU6cp0FyJ5AsZYQ",
        "readingKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEa_YIgLInEJOjngBXp3gB4F6BWt_SAEZMeOU0yDY-uyRpmrHR7-5x2LLCAEZY6QpIq_VcytQlySQQAoy9y86sIw",
        "requestKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAENKYBMrkHEF5vbsQDSgfcQUiD_12yOXThq2Nn06nVIeFabNxtAoJZBWoi4GSXcnf_CZ1eBGiAOp_GLzJJktVBUg"
      },
      {
        "approvalKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEuupAUkUqXTeWrCnK_xO-dZ-76OtdQ8e6ftZNWdPKGnllCdHfNA3pwHxj7I84TnMKeBWEGpILKbT_pJMyOr3urg",
        "deviceId": "d-629e62343180ad883d8f7a47",
        "name": "Test iPad B",
        "notificationKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAE_bA9-74nJfUWF1TD3uDhUMf_aVzkanRJaNlEVktoBFLNh8q5pVQGt60EPbZxxPKws9ZEa324ck7CIuKTOXKbMA",
        "readingKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAES3Gma5eyi9qXqF_Vm1wmgoCzO--_JXrGwLucGoRP8rz_pSI_p_W-AOULR1jsYIbIgRASoNX96Y1swYGCB12cpQ",
        "requestKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEVWOuw9sD6n17pCEXE7zqP_HsA7Unt3pigQFwB-BvnKq93d2MT8XODx-KN05M36ZZiyIDAlhHSWqmKp7XmrZMXw"
      }
    ],
    "issuedAt": "2026-11-07T20:10:00Z",
    "ownerKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEVzkkcfsegaoVRZFzkDIoVF5bEqxUarf9x8v30K8pWB_JGjq4wKAmg96uVQ704aEAZtLFNi_PbZfc69WlOvJovw",
    "previousSHA256": "0dfd951c5317bb228f6e95931175b59d3e0a206e08b5e4c2fed06070b7ff934e",
    "reopenTag": null,
    "services": {
      "macAgreement": null,
      "macExecutor": null,
      "macPublisher": null,
      "serverExecutor": null,
      "serverExecutorAgreement": null,
      "serverHelper": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAE7eq30mr1oP043ORJ4UfBGVRKXGDc655WHh8DvUh6Jdbp3jTGbZSbdaiTTFrK5bdAkMBRMt8uVfIQeyCAQv5OBg",
      "serverPublisher": null
    },
    "version": 2
  },
  "countersignatures": [
    {
      "deviceId": "d-629e62343180ad883d8f7a47",
      "signature": "MEQCIA822LZH4C8O1ZwblWQnBaD-XxFvo4DPnLDe43hdkJgQAiA308DQ9VxVtpnLP4IvEXTD_O9wv0CnlMJR-EIGbhZo9g"
    }
  ],
  "ownerSignature": "MEYCIQCFuKCTZ523TAkne09GVbLaFQbQwlDfVzbpRo2vpzRxBgIhAMczoI9kkV8YAyeBqh90KIEP6w2CVjs62H700g6iP-HR"
}
```

**T15 list**, entry SHA-256 `8ae6c261f7a4867b2526e909152d2f9bef76094427d52b886fe63d67fb56c346`:

```json
{
  "body": {
    "closed": false,
    "contract": "muster-trust-list/1",
    "devices": [
      {
        "approvalKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAETH7k5HURk_7DG-eMy6GVf66aD_dyBNIDRLa3uTE8mP92HxEXdfjyCUxsXHR34I0zbGhv7AWgXnX6fPjnCPKg6w",
        "deviceId": "d-f4e396a67ceefe6a551abbc0",
        "name": "Test Phone C",
        "notificationKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEH-LOtR0pZ4orjdPxCYFfcdDQE-d2eXu_PdcNi1LBbnoqwMECfkwCwmFjjwClFRIBG0HSthRfgZDrEA_lasDHcg",
        "readingKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAED0rgDuFHSq-VF_k0617gyxsiHFtUDxsfckW3O1JTVER4MvMB_Ua3isHPgsjowUyL8_2wOhTroMi4mHR3bY_2tg",
        "requestKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEKCWVuPOr3TdZSendYJNllk98wOyLQ95mAdO7y4Hwqm5ZV-T4ISvA2sZy_tfact4OYzDc0pYxciaedztE5EKi8Q"
      }
    ],
    "issuedAt": "2026-11-07T20:50:00Z",
    "ownerKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEVzkkcfsegaoVRZFzkDIoVF5bEqxUarf9x8v30K8pWB_JGjq4wKAmg96uVQ704aEAZtLFNi_PbZfc69WlOvJovw",
    "previousSHA256": "5e06787afce950d2f803ac3716dc9ee0d033798f2803eb3393b6b3fa53e88ce8",
    "reopenTag": "98e5f483cf1d368013391fb0437be628a7105d78712382ddb9be9b4c82c9c8e9",
    "services": {
      "macAgreement": null,
      "macExecutor": null,
      "macPublisher": null,
      "serverExecutor": null,
      "serverExecutorAgreement": null,
      "serverHelper": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAE7eq30mr1oP043ORJ4UfBGVRKXGDc655WHh8DvUh6Jdbp3jTGbZSbdaiTTFrK5bdAkMBRMt8uVfIQeyCAQv5OBg",
      "serverPublisher": null
    },
    "version": 4
  },
  "countersignatures": [
    {
      "deviceId": "d-ae9428d695e414bcefc0ef5b",
      "signature": "MEUCIE4y0Nw-usi5qjTr5LM5H1UD7wPsyjaEMQvvPeOAey9bAiEAwMlxVs09BAVXcFFsVq7OjkYsfbjW2YyW5i5vGTFlWhU"
    }
  ],
  "ownerSignature": "MEUCIQDR0Lv-HcSFsAsR-39-qSn8MjL3mizHd8ltR8cy8LfP9wIgO2tuOksY4ScwjQLkFEZ88hCvzU9-tefjShWWEJUzP5Q"
}
```

**T16 list**, entry SHA-256 `226778184ab6965ca94604efd830d57d1d19d6f95e962c556d7a81610c01007b`:

```json
{
  "body": {
    "closed": true,
    "contract": "muster-trust-list/1",
    "devices": [
      {
        "approvalKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEgLxT0run_JYlj2CY2lEYFAhCfGMoh-1Q2HxSODhM4j0boQdGe-OA9Sa_WOPmi0p15BDqD_ZxBkiKyYljQyj0Lg",
        "deviceId": "d-ae9428d695e414bcefc0ef5b",
        "name": "Test Phone A",
        "notificationKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAETc9UlPQaEGmS86h02BDlWsQALVfu2oYjQHRbDkyhxmec9zFNnpDTkWOX-Ki1D1Ty3BKQIAkVU6cp0FyJ5AsZYQ",
        "readingKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEa_YIgLInEJOjngBXp3gB4F6BWt_SAEZMeOU0yDY-uyRpmrHR7-5x2LLCAEZY6QpIq_VcytQlySQQAoy9y86sIw",
        "requestKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAENKYBMrkHEF5vbsQDSgfcQUiD_12yOXThq2Nn06nVIeFabNxtAoJZBWoi4GSXcnf_CZ1eBGiAOp_GLzJJktVBUg"
      }
    ],
    "issuedAt": "2026-11-07T20:10:00Z",
    "ownerKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEVzkkcfsegaoVRZFzkDIoVF5bEqxUarf9x8v30K8pWB_JGjq4wKAmg96uVQ704aEAZtLFNi_PbZfc69WlOvJovw",
    "previousSHA256": "0dfd951c5317bb228f6e95931175b59d3e0a206e08b5e4c2fed06070b7ff934e",
    "reopenTag": null,
    "services": {
      "macAgreement": null,
      "macExecutor": null,
      "macPublisher": null,
      "serverExecutor": null,
      "serverExecutorAgreement": null,
      "serverHelper": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAE7eq30mr1oP043ORJ4UfBGVRKXGDc655WHh8DvUh6Jdbp3jTGbZSbdaiTTFrK5bdAkMBRMt8uVfIQeyCAQv5OBg",
      "serverPublisher": null
    },
    "version": 2
  },
  "countersignatures": [],
  "ownerSignature": "MEUCIQCl1yGFLOWgYjpoCS0TuuyP7jtQZErHuBUKqlfYAec8qwIgQWfSPmscPnhTWhKYi1Hez13vmlHGe8ATXDVjDlZomSI"
}
```

**T17 list**, entry SHA-256 `df40f333ba7d7370128c2efaacb3c6acde8a9dd6af0d4390192eea3e41c8ec72`:

```json
{
  "body": {
    "closed": false,
    "contract": "muster-trust-list/1",
    "devices": [
      {
        "approvalKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEgLxT0run_JYlj2CY2lEYFAhCfGMoh-1Q2HxSODhM4j0boQdGe-OA9Sa_WOPmi0p15BDqD_ZxBkiKyYljQyj0Lg",
        "deviceId": "d-ae9428d695e414bcefc0ef5b",
        "name": "Test Phone A",
        "notificationKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAETc9UlPQaEGmS86h02BDlWsQALVfu2oYjQHRbDkyhxmec9zFNnpDTkWOX-Ki1D1Ty3BKQIAkVU6cp0FyJ5AsZYQ",
        "readingKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEa_YIgLInEJOjngBXp3gB4F6BWt_SAEZMeOU0yDY-uyRpmrHR7-5x2LLCAEZY6QpIq_VcytQlySQQAoy9y86sIw",
        "requestKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAENKYBMrkHEF5vbsQDSgfcQUiD_12yOXThq2Nn06nVIeFabNxtAoJZBWoi4GSXcnf_CZ1eBGiAOp_GLzJJktVBUg"
      }
    ],
    "issuedAt": "2026-11-07T20:00:00Z",
    "ownerKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEVzkkcfsegaoVRZFzkDIoVF5bEqxUarf9x8v30K8pWB_JGjq4wKAmg96uVQ704aEAZtLFNi_PbZfc69WlOvJovw",
    "previousSHA256": "0000000000000000000000000000000000000000000000000000000000000000",
    "reopenTag": null,
    "services": {
      "macAgreement": null,
      "macExecutor": null,
      "macPublisher": null,
      "serverExecutor": null,
      "serverExecutorAgreement": null,
      "serverHelper": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAE7eq30mr1oP043ORJ4UfBGVRKXGDc655WHh8DvUh6Jdbp3jTGbZSbdaiTTFrK5bdAkMBRMt8uVfIQeyCAQv5OBg",
      "serverPublisher": null
    },
    "version": 1
  },
  "countersignatures": [],
  "ownerSignature": "MEUCIQDABl1QK-EVORC8hjsoA5Gyeu4Kg7BdoA_yCxTKiQC7JwIgMSfNn3tcz63piNnI5lvUFCYRZdgEOszkWPWe4tsU018"
}
```

**T18 list**, entry SHA-256 `379aa0e81dfd6cb33670acd57650e3684d71c9d26128f9f73748abd929010a55`:

```json
{
  "body": {
    "closed": false,
    "contract": "muster-trust-list/1",
    "devices": [
      {
        "approvalKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAETH7k5HURk_7DG-eMy6GVf66aD_dyBNIDRLa3uTE8mP92HxEXdfjyCUxsXHR34I0zbGhv7AWgXnX6fPjnCPKg6w",
        "deviceId": "d-f4e396a67ceefe6a551abbc0",
        "name": "Test Phone C",
        "notificationKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEH-LOtR0pZ4orjdPxCYFfcdDQE-d2eXu_PdcNi1LBbnoqwMECfkwCwmFjjwClFRIBG0HSthRfgZDrEA_lasDHcg",
        "readingKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAED0rgDuFHSq-VF_k0617gyxsiHFtUDxsfckW3O1JTVER4MvMB_Ua3isHPgsjowUyL8_2wOhTroMi4mHR3bY_2tg",
        "requestKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEKCWVuPOr3TdZSendYJNllk98wOyLQ95mAdO7y4Hwqm5ZV-T4ISvA2sZy_tfact4OYzDc0pYxciaedztE5EKi8Q"
      }
    ],
    "issuedAt": "2026-11-07T20:50:00Z",
    "ownerKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEVzkkcfsegaoVRZFzkDIoVF5bEqxUarf9x8v30K8pWB_JGjq4wKAmg96uVQ704aEAZtLFNi_PbZfc69WlOvJovw",
    "previousSHA256": "5e06787afce950d2f803ac3716dc9ee0d033798f2803eb3393b6b3fa53e88ce8",
    "reopenTag": null,
    "services": {
      "macAgreement": null,
      "macExecutor": null,
      "macPublisher": null,
      "serverExecutor": null,
      "serverExecutorAgreement": null,
      "serverHelper": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAE7eq30mr1oP043ORJ4UfBGVRKXGDc655WHh8DvUh6Jdbp3jTGbZSbdaiTTFrK5bdAkMBRMt8uVfIQeyCAQv5OBg",
      "serverPublisher": null
    },
    "version": 4
  },
  "countersignatures": [
    {
      "deviceId": "d-f4e396a67ceefe6a551abbc0",
      "signature": "MEUCIQC1yKbBZ-jBIevame4bd36b27bBo4lPbEAF5wn5J7O3jQIgBcnw6YNncLx9iD86tOBLgG3prdUpkpzbfJo9qqg_lxA"
    }
  ],
  "ownerSignature": "MEUCIDIWWOpwHR_lf4OG5gb2MlYKKpk-U5VtxlCJiao8FAnAAiEAxTTd7M7sGnpwYhftRZfVlQ0dz6e7qzccMA-qOMM-bSE"
}
```

**T19 list**, entry SHA-256 `9a6b4f41ab72f5d89cd2e6bb84f6fc1a6ee4e9f03214f461c28a018614865aeb`:

```json
{
  "body": {
    "closed": true,
    "contract": "muster-trust-list/1",
    "devices": [],
    "issuedAt": "2026-11-07T20:10:00Z",
    "ownerKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEVzkkcfsegaoVRZFzkDIoVF5bEqxUarf9x8v30K8pWB_JGjq4wKAmg96uVQ704aEAZtLFNi_PbZfc69WlOvJovw",
    "previousSHA256": "0dfd951c5317bb228f6e95931175b59d3e0a206e08b5e4c2fed06070b7ff934e",
    "reopenTag": null,
    "services": {
      "macAgreement": null,
      "macExecutor": null,
      "macPublisher": null,
      "serverExecutor": null,
      "serverExecutorAgreement": null,
      "serverHelper": null,
      "serverPublisher": null
    },
    "version": 2
  },
  "countersignatures": [],
  "ownerSignature": "MEUCIDVjSg_TZTl_CW15iPrsjqA4pMz1wETx3xpbovVDt1uFAiEAgQa-v_ourVZUoFFUHbr9VXV8u8mQIn8-jXbIr0K_oTA"
}
```

**T20 list**, entry SHA-256 `78005061353b495f74ac02e2e422385945f6cb53886d6ccf253d3fa26215c536`:

```json
{
  "body": {
    "closed": false,
    "contract": "muster-trust-list/1",
    "devices": [
      {
        "approvalKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEuupAUkUqXTeWrCnK_xO-dZ-76OtdQ8e6ftZNWdPKGnllCdHfNA3pwHxj7I84TnMKeBWEGpILKbT_pJMyOr3urg",
        "deviceId": "d-629e62343180ad883d8f7a47",
        "name": "Test iPad B",
        "notificationKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAE_bA9-74nJfUWF1TD3uDhUMf_aVzkanRJaNlEVktoBFLNh8q5pVQGt60EPbZxxPKws9ZEa324ck7CIuKTOXKbMA",
        "readingKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAES3Gma5eyi9qXqF_Vm1wmgoCzO--_JXrGwLucGoRP8rz_pSI_p_W-AOULR1jsYIbIgRASoNX96Y1swYGCB12cpQ",
        "requestKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEVWOuw9sD6n17pCEXE7zqP_HsA7Unt3pigQFwB-BvnKq93d2MT8XODx-KN05M36ZZiyIDAlhHSWqmKp7XmrZMXw"
      }
    ],
    "issuedAt": "2026-11-07T20:50:00Z",
    "ownerKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEVzkkcfsegaoVRZFzkDIoVF5bEqxUarf9x8v30K8pWB_JGjq4wKAmg96uVQ704aEAZtLFNi_PbZfc69WlOvJovw",
    "previousSHA256": "5e06787afce950d2f803ac3716dc9ee0d033798f2803eb3393b6b3fa53e88ce8",
    "reopenTag": "368c3e1db040ba98f33cb202502b0961b3ba6288b715ceef789f361fa0d3800c",
    "services": {
      "macAgreement": null,
      "macExecutor": null,
      "macPublisher": null,
      "serverExecutor": null,
      "serverExecutorAgreement": null,
      "serverHelper": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAE7eq30mr1oP043ORJ4UfBGVRKXGDc655WHh8DvUh6Jdbp3jTGbZSbdaiTTFrK5bdAkMBRMt8uVfIQeyCAQv5OBg",
      "serverPublisher": null
    },
    "version": 4
  },
  "countersignatures": [
    {
      "deviceId": "d-629e62343180ad883d8f7a47",
      "signature": "MEQCIHOR4elOIfK3NP4PADHU9IY8gZiuzfSGZRLWPqsC9r1UAiBECgiS6uaf0KQp6sdQnNRjfflCbu5BUoJzxNhs8ftA6g"
    }
  ],
  "ownerSignature": "MEUCIQC4T3c8v712RIq-9BPip0IVX23xUS0VaCb2vBzs6sAjngIgDaKuK2UzR-55OPUC2asYYeLc4luv2ZJl4Dpu88Ya_Js"
}
```

**T21 list**, entry SHA-256 `45a1fab26e0fa2efec35b9a7178cee6f6ab384ca46f0cb91afe716405990a5f5`:

```json
{
  "body": {
    "closed": false,
    "contract": "muster-trust-list/1",
    "devices": [
      {
        "approvalKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAETH7k5HURk_7DG-eMy6GVf66aD_dyBNIDRLa3uTE8mP92HxEXdfjyCUxsXHR34I0zbGhv7AWgXnX6fPjnCPKg6w",
        "deviceId": "d-f4e396a67ceefe6a551abbc0",
        "name": "Test Phone C",
        "notificationKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEH-LOtR0pZ4orjdPxCYFfcdDQE-d2eXu_PdcNi1LBbnoqwMECfkwCwmFjjwClFRIBG0HSthRfgZDrEA_lasDHcg",
        "readingKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAED0rgDuFHSq-VF_k0617gyxsiHFtUDxsfckW3O1JTVER4MvMB_Ua3isHPgsjowUyL8_2wOhTroMi4mHR3bY_2tg",
        "requestKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEKCWVuPOr3TdZSendYJNllk98wOyLQ95mAdO7y4Hwqm5ZV-T4ISvA2sZy_tfact4OYzDc0pYxciaedztE5EKi8Q"
      }
    ],
    "issuedAt": "2026-11-07T20:50:00Z",
    "ownerKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEVzkkcfsegaoVRZFzkDIoVF5bEqxUarf9x8v30K8pWB_JGjq4wKAmg96uVQ704aEAZtLFNi_PbZfc69WlOvJovw",
    "previousSHA256": "5e06787afce950d2f803ac3716dc9ee0d033798f2803eb3393b6b3fa53e88ce8",
    "reopenTag": "9477dc539be54017c596289fc8859de13c6f1ab807958d3dad6413238fb97988",
    "services": {
      "macAgreement": null,
      "macExecutor": null,
      "macPublisher": null,
      "serverExecutor": null,
      "serverExecutorAgreement": null,
      "serverHelper": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAE7eq30mr1oP043ORJ4UfBGVRKXGDc655WHh8DvUh6Jdbp3jTGbZSbdaiTTFrK5bdAkMBRMt8uVfIQeyCAQv5OBg",
      "serverPublisher": null
    },
    "version": 4
  },
  "countersignatures": [
    {
      "deviceId": "d-f4e396a67ceefe6a551abbc0",
      "signature": "MEYCIQDnZIxoSIiTpEylaV6cA_fS8szQquS5LuXVPV1cfe1_7wIhAOWenYAoyLdN43eGgAePoAiT3lbvAyOs8-Yf2EOwDrOI"
    }
  ],
  "ownerSignature": "MEQCIBusjBYWAvZHntfKtH9kDWcOYroie-O2sdcWlnFvNCJhAiA--FEu69qrAVhKuE2UBrqotCXKi8SntkHIMwP946_mHw"
}
```

**T24 list**, entry SHA-256 `2775f7a6f73bbae98c8cdfc182d45c539037a8c39f21dc6987da800e822a765f`:

```json
{
  "body": {
    "closed": false,
    "contract": "muster-trust-list/1",
    "devices": [
      {
        "approvalKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEgLxT0run_JYlj2CY2lEYFAhCfGMoh-1Q2HxSODhM4j0boQdGe-OA9Sa_WOPmi0p15BDqD_ZxBkiKyYljQyj0Lg",
        "deviceId": "d-ae9428d695e414bcefc0ef5b",
        "name": "Test Phone A",
        "notificationKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAETc9UlPQaEGmS86h02BDlWsQALVfu2oYjQHRbDkyhxmec9zFNnpDTkWOX-Ki1D1Ty3BKQIAkVU6cp0FyJ5AsZYQ",
        "readingKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEa_YIgLInEJOjngBXp3gB4F6BWt_SAEZMeOU0yDY-uyRpmrHR7-5x2LLCAEZY6QpIq_VcytQlySQQAoy9y86sIw",
        "requestKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAENKYBMrkHEF5vbsQDSgfcQUiD_12yOXThq2Nn06nVIeFabNxtAoJZBWoi4GSXcnf_CZ1eBGiAOp_GLzJJktVBUg"
      }
    ],
    "issuedAt": "2026-11-07T20:05:00Z",
    "ownerKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEVzkkcfsegaoVRZFzkDIoVF5bEqxUarf9x8v30K8pWB_JGjq4wKAmg96uVQ704aEAZtLFNi_PbZfc69WlOvJovw",
    "previousSHA256": null,
    "reopenTag": null,
    "services": {
      "macAgreement": null,
      "macExecutor": null,
      "macPublisher": null,
      "serverExecutor": null,
      "serverExecutorAgreement": null,
      "serverHelper": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAE7eq30mr1oP043ORJ4UfBGVRKXGDc655WHh8DvUh6Jdbp3jTGbZSbdaiTTFrK5bdAkMBRMt8uVfIQeyCAQv5OBg",
      "serverPublisher": null
    },
    "version": 1
  },
  "countersignatures": [],
  "ownerSignature": "MEUCIAaMIGeuZ34v74Ad1Jdx-0rSzk0h3l8N40hqHv-IyD1QAiEAtRe6el3FtB6GNrGtwZR6f_E4yjIQvku_tQx898C7uPw"
}
```

**T25 list**, entry SHA-256 `d15cfdf2e9c45979b705ebd92baa98fd173acde35ce3bd23abcacbb4f6059b97`:

```json
{
  "body": {
    "closed": false,
    "contract": "muster-trust-list/1",
    "devices": [
      {
        "approvalKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEgLxT0run_JYlj2CY2lEYFAhCfGMoh-1Q2HxSODhM4j0boQdGe-OA9Sa_WOPmi0p15BDqD_ZxBkiKyYljQyj0Lg",
        "deviceId": "d-ae9428d695e414bcefc0ef5b",
        "name": "Test Phone A",
        "notificationKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAETc9UlPQaEGmS86h02BDlWsQALVfu2oYjQHRbDkyhxmec9zFNnpDTkWOX-Ki1D1Ty3BKQIAkVU6cp0FyJ5AsZYQ",
        "readingKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEa_YIgLInEJOjngBXp3gB4F6BWt_SAEZMeOU0yDY-uyRpmrHR7-5x2LLCAEZY6QpIq_VcytQlySQQAoy9y86sIw",
        "requestKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAENKYBMrkHEF5vbsQDSgfcQUiD_12yOXThq2Nn06nVIeFabNxtAoJZBWoi4GSXcnf_CZ1eBGiAOp_GLzJJktVBUg"
      },
      {
        "approvalKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEuupAUkUqXTeWrCnK_xO-dZ-76OtdQ8e6ftZNWdPKGnllCdHfNA3pwHxj7I84TnMKeBWEGpILKbT_pJMyOr3urg",
        "deviceId": "d-629e62343180ad883d8f7a47",
        "name": "Test iPad B",
        "notificationKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAE_bA9-74nJfUWF1TD3uDhUMf_aVzkanRJaNlEVktoBFLNh8q5pVQGt60EPbZxxPKws9ZEa324ck7CIuKTOXKbMA",
        "readingKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAES3Gma5eyi9qXqF_Vm1wmgoCzO--_JXrGwLucGoRP8rz_pSI_p_W-AOULR1jsYIbIgRASoNX96Y1swYGCB12cpQ",
        "requestKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEVWOuw9sD6n17pCEXE7zqP_HsA7Unt3pigQFwB-BvnKq93d2MT8XODx-KN05M36ZZiyIDAlhHSWqmKp7XmrZMXw"
      }
    ],
    "issuedAt": "2026-11-07T20:10:00Z",
    "ownerKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEVzkkcfsegaoVRZFzkDIoVF5bEqxUarf9x8v30K8pWB_JGjq4wKAmg96uVQ704aEAZtLFNi_PbZfc69WlOvJovw",
    "previousSHA256": "0dfd951c5317bb228f6e95931175b59d3e0a206e08b5e4c2fed06070b7ff934e",
    "reopenTag": null,
    "services": {
      "macAgreement": null,
      "macExecutor": null,
      "macPublisher": null,
      "serverExecutor": null,
      "serverExecutorAgreement": null,
      "serverHelper": null,
      "serverPublisher": null
    },
    "version": 2
  },
  "countersignatures": [
    {
      "deviceId": "d-ae9428d695e414bcefc0ef5b",
      "signature": "MEUCIQCAlGFqQxrq1Rqbcu9naVLFqkBqLWJvOkdJryDj6UY43wIgNcGJmVax4q-shuNZZgiuSW7OwoF4xJ0vqsNO-rzbk-c"
    }
  ],
  "ownerSignature": "MEYCIQDMlQQ6pt2Irt3n6EUuA9v9GbyPNESSxP8pD9sWr0lOnwIhAMM1Sjf7LBmWIihdYS4HraOj9VCvQuTHiL5fG8FBUHKs"
}
```

**T26 list**, entry SHA-256 `3d2ca57b5bce35eff4327f574fef48c7c8e09c86b44890dc3a334a3ac59620f6`:

```json
{
  "body": {
    "closed": false,
    "contract": "muster-trust-list/1",
    "devices": [
      {
        "approvalKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEgLxT0run_JYlj2CY2lEYFAhCfGMoh-1Q2HxSODhM4j0boQdGe-OA9Sa_WOPmi0p15BDqD_ZxBkiKyYljQyj0Lg",
        "deviceId": "d-ae9428d695e414bcefc0ef5b",
        "name": "Test Phone A",
        "notificationKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAETc9UlPQaEGmS86h02BDlWsQALVfu2oYjQHRbDkyhxmec9zFNnpDTkWOX-Ki1D1Ty3BKQIAkVU6cp0FyJ5AsZYQ",
        "readingKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEa_YIgLInEJOjngBXp3gB4F6BWt_SAEZMeOU0yDY-uyRpmrHR7-5x2LLCAEZY6QpIq_VcytQlySQQAoy9y86sIw",
        "requestKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAENKYBMrkHEF5vbsQDSgfcQUiD_12yOXThq2Nn06nVIeFabNxtAoJZBWoi4GSXcnf_CZ1eBGiAOp_GLzJJktVBUg"
      }
    ],
    "issuedAt": "2026-11-07T20:00:30Z",
    "ownerKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEVzkkcfsegaoVRZFzkDIoVF5bEqxUarf9x8v30K8pWB_JGjq4wKAmg96uVQ704aEAZtLFNi_PbZfc69WlOvJovw",
    "previousSHA256": null,
    "reopenTag": null,
    "services": {
      "macAgreement": null,
      "macExecutor": null,
      "macPublisher": null,
      "serverExecutor": null,
      "serverExecutorAgreement": null,
      "serverHelper": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAE7eq30mr1oP043ORJ4UfBGVRKXGDc655WHh8DvUh6Jdbp3jTGbZSbdaiTTFrK5bdAkMBRMt8uVfIQeyCAQv5OBg",
      "serverPublisher": null
    },
    "version": 1
  },
  "countersignatures": [],
  "ownerSignature": "MEUCIFxRVNQt6GBqHHkwEAGh_KWebrgR6Dwz8E45uOau4tzJAiEA21-xzkkpOZfV5fRofpVDL6_hjfrHOpbam3uhz0euF-x"
}
```

**T27 list**, entry SHA-256 `4c949c1c28376e08ac0da53b64c0243ec8bcacb7eb0dad1abc3cca4a2f05de40`:

```json
{
  "body": {
    "closed": false,
    "contract": "muster-trust-list/1",
    "devices": [
      {
        "approvalKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEgLxT0run_JYlj2CY2lEYFAhCfGMoh-1Q2HxSODhM4j0boQdGe-OA9Sa_WOPmi0p15BDqD_ZxBkiKyYljQyj0Lg",
        "deviceId": "d-ae9428d695e414bcefc0ef5b",
        "name": "Test Phone A",
        "notificationKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAETc9UlPQaEGmS86h02BDlWsQALVfu2oYjQHRbDkyhxmec9zFNnpDTkWOX-Ki1D1Ty3BKQIAkVU6cp0FyJ5AsZYQ",
        "readingKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEa_YIgLInEJOjngBXp3gB4F6BWt_SAEZMeOU0yDY-uyRpmrHR7-5x2LLCAEZY6QpIq_VcytQlySQQAoy9y86sIw",
        "requestKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAENKYBMrkHEF5vbsQDSgfcQUiD_12yOXThq2Nn06nVIeFabNxtAoJZBWoi4GSXcnf_CZ1eBGiAOp_GLzJJktVBUg"
      },
      {
        "approvalKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEuupAUkUqXTeWrCnK_xO-dZ-76OtdQ8e6ftZNWdPKGnllCdHfNA3pwHxj7I84TnMKeBWEGpILKbT_pJMyOr3urg",
        "deviceId": "d-629e62343180ad883d8f7a47",
        "name": "Test iPad B",
        "notificationKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAE_bA9-74nJfUWF1TD3uDhUMf_aVzkanRJaNlEVktoBFLNh8q5pVQGt60EPbZxxPKws9ZEa324ck7CIuKTOXKbMA",
        "readingKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAES3Gma5eyi9qXqF_Vm1wmgoCzO--_JXrGwLucGoRP8rz_pSI_p_W-AOULR1jsYIbIgRASoNX96Y1swYGCB12cpQ",
        "requestKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEVWOuw9sD6n17pCEXE7zqP_HsA7Unt3pigQFwB-BvnKq93d2MT8XODx-KN05M36ZZiyIDAlhHSWqmKp7XmrZMXw"
      }
    ],
    "issuedAt": "2026-11-07T20:10:30Z",
    "ownerKey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEVzkkcfsegaoVRZFzkDIoVF5bEqxUarf9x8v30K8pWB_JGjq4wKAmg96uVQ704aEAZtLFNi_PbZfc69WlOvJovw",
    "previousSHA256": "0dfd951c5317bb228f6e95931175b59d3e0a206e08b5e4c2fed06070b7ff934e",
    "reopenTag": null,
    "services": {
      "macAgreement": null,
      "macExecutor": null,
      "macPublisher": null,
      "serverExecutor": null,
      "serverExecutorAgreement": null,
      "serverHelper": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAE7eq30mr1oP043ORJ4UfBGVRKXGDc655WHh8DvUh6Jdbp3jTGbZSbdaiTTFrK5bdAkMBRMt8uVfIQeyCAQv5OBg",
      "serverPublisher": null
    },
    "version": 2
  },
  "countersignatures": [
    {
      "deviceId": "d-ae9428d695e414bcefc0ef5b",
      "signature": "MEUCIFeXjyOC1-n_pzvParjh67PwT_xep1ZsomUtDfTJQ66VAiEA85jz4X_BjVyAtUHe4h8l9B7BYBkkfN3f5AzdAK0uIxt"
    }
  ],
  "ownerSignature": "MEQCIH5KB1VmjnUM6G72-J_Nm4kEsbTMcwi3IjPnPdcdjqAlAiBrg1WySrVksuwSNd_W5EPhCgoadnFKZmFw16ev7egGjQ"
}
```

### Chain Vectors

Each is `muster trust-list chain --yes` with the lists named, under the pin, history and record above.  The reason is the first failing check in §8's numbered order;  each bad list fails exactly one check.  **The rule C rows (T4, T5, T8, T15, T18, T20, T21 and T22) give the OPEN proposal's outcomes.  A Milestone 1 to 4 builder skips them, or expects `chain_refused` with reason `reopen_unavailable` for each rule C entry (T5 is refused at its fourth entry), as T23 below shows.**  `Final` gives the version 4 proposal's classification, which is OPEN (§17A, A2);  version 5 records and reports nothing as final.  The helper on `main` (BB's chain verb, PR 1543) gives the Milestone 1 to 4 answer for every row, which is `Expected` for every row that is not a rule C row, and its tests (`ChainVectorTests` in `scripts/test_muster_approval.py`) run every row against it from the test fixture;  the other verifiers check them too.  T24 runs under the rotated history printed above (T24 only);  every other row runs under the one-line history.

| Vector | Held Before | Chain | Expected | Final | Why |
| --- | --- | --- | --- | --- | --- |
| T1 first list | nothing | v1 | ack |  | Holding nothing, version 1 under rule First. |
| T2 rule A | v1 | v2 | ack |  | Adds device B, countersigned by device A from version 1. |
| T3 rule B | v2 | v3 | ack |  | Closes the list:  no devices, services unchanged, owner signature only. |
| T4 rule C after an abandoned first code | v3 | v4 | ack |  | Reopens with device C.  Two record lines name version 4;  the list is tagged with the newer one, the only line that counts, and names exactly its device. |
| T5 whole history | nothing | v1, v2, v3, v4 | ack |  | A late verifier walks First, A, B and C from nothing. |
| T6 close that swaps a service key | v1 | T6 list | `chain_refused`, reason `services_changed` | yes | A close may only keep or remove service keys. |
| T7 wrong previous hash | v1 | T7 list | `chain_refused`, reason `previous_hash` | no | previousSHA256 is all zeros;  nothing else is wrong. |
| T8 reopen with an unrecorded code | v3 | T8 list | `chain_refused`, reason `reopen_tag` | yes | The newest line for version 4 does not make this tag (nor does any other line). |
| T10 gap | v1 | T10 list | `chain_refused`, reason `version_gap` | no | Version 3 held over version 1, with previousSHA256 naming version 1, so only the version check fails. |
| T11 wrong owner key | v1 | T11 list | `chain_refused`, reason `owner_key` | no | Names a key the history does not assign;  signed by the pinned key, so only check 3 fails. |
| T12 bad owner signature | v1 | T12 list | `chain_refused`, reason `owner_signature` | yes | Names the pinned key but is signed by another. |
| T13 duplicate device | v1 | T13 list | `chain_refused`, reason `duplicate_device` | yes | Device A listed twice. |
| T14 rule A countersigned by a newcomer | v1 | T14 list | `chain_refused`, reason `countersignature` | yes | Only device B countersigns, and B is not on version 1. |
| T15 tampered copy of version 4 | v3 | T15 list | `chain_refused`, reason `countersignature` | yes | v4's own body and owner signature, countersigned by A instead of C.  Same body hash as v4, different entry hash. |
| T16 closed flag with a device | v1 | T16 list | `chain_refused`, reason `closed_rule` | yes | closed is true while devices is not empty. |
| T17 first list naming a predecessor | nothing | T17 list | `chain_refused`, reason `first_rule` | yes | Version 1 with a non-null previousSHA256. |
| T18 reopen without a tag | v3 | T18 list | `chain_refused`, reason `reopen_unrecorded` | no | A rule C entry whose reopenTag is null. |
| T19 close that removes both server keys | v1 | T19 list | `chain_refused`, reason `server_keys_removed` | yes | serverHelper set to null, leaving no server key to acknowledge a later reopen. |
| T20 reopen with the wrong device | v3 | T20 list | `chain_refused`, reason `reopen_device` | yes | Tagged with the newest code for version 4, but that line names device C and the list names device B. |
| T21 reopen with a retired code | v3 | T21 list | `chain_refused`, reason `reopen_tag` | yes | Identical to v4 except that it is tagged with the first code, which the newer line for version 4 retired. |
| T22 a second valid version 4 | v3 | v4 at 20:55 | ack |  | Signed at 20:55 with the same code and device:  a server could hold this instead of v4 (R5). |
| T24 a version 1 the rotated history did not close | nothing | T24 list | `chain_refused`, reason `history_hash` | no | Under the rotated history, whose closed line names v1's body hash:  another version 1, signed by the key that history assigns to version 1, so only check 3's history hash fails (v5.3). |
| T25 rule A that removes both server keys | v1 | T25 list | `chain_refused`, reason `server_keys_removed` | yes | Adds device B, countersigned by device A, and sets serverHelper to null, leaving no server key (§17A A8, settled in v5.3). |
| T26 owner signature with a stray trailing bit | nothing | T26 list | `chain_refused`, reason `owner_signature` | yes | A first list whose ownerSignature has a stray trailing bit set in its last base64url character:  a lenient decoder reads the same valid DER, but the base64url is not canonical, so check 4 fails (v5.4). |
| T27 countersignature with a stray trailing bit | v1 | T27 list | `chain_refused`, reason `countersignature` | yes | Adds device B, countersigned by device A, but the countersignature has a stray trailing bit set in its last base64url character, so rule A's countersignature condition fails (v5.4). |

T9 was retired, and a vector's number is never reused, so the table has no T9.  T24 and T25 were added in version 5.3, and T26 and T27 in version 5.4;  none of them is a rule C row.  T26 and T27 carry the same DER signature bytes a lenient base64url decoder would read, so a verifier that does not hold base64url to its canonical form (§5, v5.4) accepts them, and the helper refuses them.

### Frozen Version 5 Vector

What every verifier in Milestones 1 to 4 must answer for a rule C entry.

| Vector | Held Before | Chain | Expected | Final | Why |
| --- | --- | --- | --- | --- | --- |
| T23 v4 under version 5 as frozen | v3 | v4 | `chain_refused`, reason `reopen_unavailable` | no | The same chain as T4, judged by a Milestone 1 to 4 verifier:  checks 1 to 7 pass, and rule C is not available. |

### Recovery Vectors

OPEN proposal (§9, §17A).  Muster holds an entry pending and sees one verified acknowledgement from the key §9 requires.  v4's entry SHA-256 is `31c2a7574fa5821520b2cdea5894ad056518be43c98b19fb208f8e4d8ae529d4` and its body SHA-256 `5911cecb01736c03a12bd355b3d74ace6e0164b78d2dac4f0ee2cc81fa711e0f`;  T15 has the same body hash but entry SHA-256 `8ae6c261f7a4867b2526e909152d2f9bef76094427d52b886fe63d67fb56c346`.  Hashes below are shortened to 16 characters;  the vectors above give them in full.

| Vector | Pending | Acknowledgement Says | Muster Must | Why |
| --- | --- | --- | --- | --- |
| R1 a final refusal of a tampered copy | v4 | holds version 3, body `5e06787afce950d2…`;  final refusal of version 4, entry `8ae6c261f7a4867b…`, `countersignature` | keep waiting | The refused entry is T15:  the same body hash as v4, a different entry hash. |
| R2 after a non-final refusal | v4 | holds version 3, body `5e06787afce950d2…` | keep waiting | The server refused v4 with version_gap (sent out of order);  non-final reasons are never recorded. |
| R3 coverage | v4 | holds version 4, body `5911cecb01736c03…` | apply | The acknowledgement names version 4 with v4's body hash. |
| R4 a final refusal of the pending entry itself | T8 list | holds version 3, body `5e06787afce950d2…`;  final refusal of version 4, entry `bdd8258defb64bf1…`, `reopen_tag` | discard | Pending entry T8 (as if the check group had been bypassed) was refused for a final reason;  it can never be accepted. |
| R5 the server holds a different version 4 | v4 | holds version 4, body `a477b60535bfe9d1…` | adopt the server's chain | Muster fetches the chain the acknowledgement covers and walks it under §8;  it never signs a redo. |

### Approval Vectors

Each runs `server restart fleet-server-status.timer --yes` under the pin and history above, holding the list each names.

#### V1 valid

Listed approval key, inside the window, fresh nonce, matching plan.  Audit row:  yes.

```
stored list:           version 1
helper clock (now):    2026-11-07T21:00:30Z
ledger nonces:         (none)
helper plan SHA-256:   f162187bfc6725679de05bd66a6a1c1ae68c2e99b5ad3263330f99ec4103be4d
expected outcome:      accepted
approval text bytes (hex):
6d75737465722d617070726f76652f310a6a6f62376b326d39713478310a30663165326433633462356136393738383739366135623463336432653166300a61316232633364346535663630373138323933613462356336643765386639300a7365727665720a726573746172740a666c6565742d7365727665722d7374617475732e74696d65720a663136323138376266633637323536373964653035626436366136613163316165363863326539396235616433323633333330663939656334313033626534640a642d6165393432386436393565343134626365666330656635620a323032362d31312d30375432313a30303a30305a0a323032362d31312d30375432313a30323a30305a
approval text SHA-256: 0db7a5bb4cf48a40b762ec8a6be018a1bb1d7f90d11428229a4ab0e0b4b9c49e
signature (base64url DER): MEQCIG7qzh_jgCU_aocqf4dSWekVrAeZPd6ZEjtzvQzibmgNAiBXnKFx8UCCkR2RtV0scxR7Ogm4lCg0uNSA6lOdQMY0jw
stdin bytes (533 bytes):
{"approval":{"action":"restart","approvalNonce":"a1b2c3d4e5f60718293a4b5c6d7e8f90","deviceId":"d-ae9428d695e414bcefc0ef5b","expiresAt":"2026-11-07T21:02:00Z","host":"server","issuedAt":"2026-11-07T21:00:00Z","jobId":"job7k2m9q4x1","jobNonce":"0f1e2d3c4b5a69788796a5b4c3d2e1f0","planSHA256":"f162187bfc6725679de05bd66a6a1c1ae68c2e99b5ad3263330f99ec4103be4d","signature":"MEQCIG7qzh_jgCU_aocqf4dSWekVrAeZPd6ZEjtzvQzibmgNAiBXnKFx8UCCkR2RtV0scxR7Ogm4lCg0uNSA6lOdQMY0jw","target":"fleet-server-status.timer"},"protocol":"muster-helper/1"}
```

#### V2 request key used as approval key

Signed by the device's request key.  Audit row:  no.

```
stored list:           version 1
helper clock (now):    2026-11-07T21:00:30Z
ledger nonces:         (none)
helper plan SHA-256:   f162187bfc6725679de05bd66a6a1c1ae68c2e99b5ad3263330f99ec4103be4d
expected outcome:      bad_signature
approval text bytes (hex):
6d75737465722d617070726f76652f310a6a6f62376b326d39713478310a30663165326433633462356136393738383739366135623463336432653166300a61316232633364346535663630373138323933613462356336643765386639300a7365727665720a726573746172740a666c6565742d7365727665722d7374617475732e74696d65720a663136323138376266633637323536373964653035626436366136613163316165363863326539396235616433323633333330663939656334313033626534640a642d6165393432386436393565343134626365666330656635620a323032362d31312d30375432313a30303a30305a0a323032362d31312d30375432313a30323a30305a
approval text SHA-256: 0db7a5bb4cf48a40b762ec8a6be018a1bb1d7f90d11428229a4ab0e0b4b9c49e
signature (base64url DER): MEUCIEPLzqSgt3VkbQ-9raF21RBgv73RGEDmKKH6rdb6eoDlAiEAqRjzimBrUlXuM8iaHaa8sFHgSHS4bduyzmvh2YZgZgw
stdin bytes (534 bytes):
{"approval":{"action":"restart","approvalNonce":"a1b2c3d4e5f60718293a4b5c6d7e8f90","deviceId":"d-ae9428d695e414bcefc0ef5b","expiresAt":"2026-11-07T21:02:00Z","host":"server","issuedAt":"2026-11-07T21:00:00Z","jobId":"job7k2m9q4x1","jobNonce":"0f1e2d3c4b5a69788796a5b4c3d2e1f0","planSHA256":"f162187bfc6725679de05bd66a6a1c1ae68c2e99b5ad3263330f99ec4103be4d","signature":"MEUCIEPLzqSgt3VkbQ-9raF21RBgv73RGEDmKKH6rdb6eoDlAiEAqRjzimBrUlXuM8iaHaa8sFHgSHS4bduyzmvh2YZgZgw","target":"fleet-server-status.timer"},"protocol":"muster-helper/1"}
```

#### V3 key not on the list

Signed by a key that is not the listed approval key.  Audit row:  no.

```
stored list:           version 1
helper clock (now):    2026-11-07T21:00:30Z
ledger nonces:         (none)
helper plan SHA-256:   f162187bfc6725679de05bd66a6a1c1ae68c2e99b5ad3263330f99ec4103be4d
expected outcome:      bad_signature
approval text bytes (hex):
6d75737465722d617070726f76652f310a6a6f62376b326d39713478310a30663165326433633462356136393738383739366135623463336432653166300a61316232633364346535663630373138323933613462356336643765386639300a7365727665720a726573746172740a666c6565742d7365727665722d7374617475732e74696d65720a663136323138376266633637323536373964653035626436366136613163316165363863326539396235616433323633333330663939656334313033626534640a642d6165393432386436393565343134626365666330656635620a323032362d31312d30375432313a30303a30305a0a323032362d31312d30375432313a30323a30305a
approval text SHA-256: 0db7a5bb4cf48a40b762ec8a6be018a1bb1d7f90d11428229a4ab0e0b4b9c49e
signature (base64url DER): MEYCIQCEwv0FE1Kg2y_uLHmZ7CV85_D2yUQRPGTmBVR3E1bMXAIhAPY9Xgdsj0J0PrC_pNwCVLT1mBpKAdhtjTBu9ZQ4aH5Z
stdin bytes (535 bytes):
{"approval":{"action":"restart","approvalNonce":"a1b2c3d4e5f60718293a4b5c6d7e8f90","deviceId":"d-ae9428d695e414bcefc0ef5b","expiresAt":"2026-11-07T21:02:00Z","host":"server","issuedAt":"2026-11-07T21:00:00Z","jobId":"job7k2m9q4x1","jobNonce":"0f1e2d3c4b5a69788796a5b4c3d2e1f0","planSHA256":"f162187bfc6725679de05bd66a6a1c1ae68c2e99b5ad3263330f99ec4103be4d","signature":"MEYCIQCEwv0FE1Kg2y_uLHmZ7CV85_D2yUQRPGTmBVR3E1bMXAIhAPY9Xgdsj0J0PrC_pNwCVLT1mBpKAdhtjTBu9ZQ4aH5Z","target":"fleet-server-status.timer"},"protocol":"muster-helper/1"}
```

#### V4 expired

One second after expiresAt.  The phone reads this as "Not known whether it ran".  Audit row:  no (refused before Y8).

```
stored list:           version 1
helper clock (now):    2026-11-07T21:02:01Z
ledger nonces:         (none)
helper plan SHA-256:   f162187bfc6725679de05bd66a6a1c1ae68c2e99b5ad3263330f99ec4103be4d
expected outcome:      expired
approval text bytes (hex):
6d75737465722d617070726f76652f310a6a6f62376b326d39713478310a30663165326433633462356136393738383739366135623463336432653166300a61316232633364346535663630373138323933613462356336643765386639300a7365727665720a726573746172740a666c6565742d7365727665722d7374617475732e74696d65720a663136323138376266633637323536373964653035626436366136613163316165363863326539396235616433323633333330663939656334313033626534640a642d6165393432386436393565343134626365666330656635620a323032362d31312d30375432313a30303a30305a0a323032362d31312d30375432313a30323a30305a
approval text SHA-256: 0db7a5bb4cf48a40b762ec8a6be018a1bb1d7f90d11428229a4ab0e0b4b9c49e
signature (base64url DER): MEQCIG7qzh_jgCU_aocqf4dSWekVrAeZPd6ZEjtzvQzibmgNAiBXnKFx8UCCkR2RtV0scxR7Ogm4lCg0uNSA6lOdQMY0jw
stdin bytes (533 bytes):
{"approval":{"action":"restart","approvalNonce":"a1b2c3d4e5f60718293a4b5c6d7e8f90","deviceId":"d-ae9428d695e414bcefc0ef5b","expiresAt":"2026-11-07T21:02:00Z","host":"server","issuedAt":"2026-11-07T21:00:00Z","jobId":"job7k2m9q4x1","jobNonce":"0f1e2d3c4b5a69788796a5b4c3d2e1f0","planSHA256":"f162187bfc6725679de05bd66a6a1c1ae68c2e99b5ad3263330f99ec4103be4d","signature":"MEQCIG7qzh_jgCU_aocqf4dSWekVrAeZPd6ZEjtzvQzibmgNAiBXnKFx8UCCkR2RtV0scxR7Ogm4lCg0uNSA6lOdQMY0jw","target":"fleet-server-status.timer"},"protocol":"muster-helper/1"}
```

#### V5 replayed nonce

The approvalNonce is already in the ledger.  Audit row:  no.

```
stored list:           version 1
helper clock (now):    2026-11-07T21:00:45Z
ledger nonces:         a1b2c3d4e5f60718293a4b5c6d7e8f90
helper plan SHA-256:   f162187bfc6725679de05bd66a6a1c1ae68c2e99b5ad3263330f99ec4103be4d
expected outcome:      replayed_nonce
approval text bytes (hex):
6d75737465722d617070726f76652f310a6a6f62376b326d39713478310a30663165326433633462356136393738383739366135623463336432653166300a61316232633364346535663630373138323933613462356336643765386639300a7365727665720a726573746172740a666c6565742d7365727665722d7374617475732e74696d65720a663136323138376266633637323536373964653035626436366136613163316165363863326539396235616433323633333330663939656334313033626534640a642d6165393432386436393565343134626365666330656635620a323032362d31312d30375432313a30303a30305a0a323032362d31312d30375432313a30323a30305a
approval text SHA-256: 0db7a5bb4cf48a40b762ec8a6be018a1bb1d7f90d11428229a4ab0e0b4b9c49e
signature (base64url DER): MEQCIG7qzh_jgCU_aocqf4dSWekVrAeZPd6ZEjtzvQzibmgNAiBXnKFx8UCCkR2RtV0scxR7Ogm4lCg0uNSA6lOdQMY0jw
stdin bytes (533 bytes):
{"approval":{"action":"restart","approvalNonce":"a1b2c3d4e5f60718293a4b5c6d7e8f90","deviceId":"d-ae9428d695e414bcefc0ef5b","expiresAt":"2026-11-07T21:02:00Z","host":"server","issuedAt":"2026-11-07T21:00:00Z","jobId":"job7k2m9q4x1","jobNonce":"0f1e2d3c4b5a69788796a5b4c3d2e1f0","planSHA256":"f162187bfc6725679de05bd66a6a1c1ae68c2e99b5ad3263330f99ec4103be4d","signature":"MEQCIG7qzh_jgCU_aocqf4dSWekVrAeZPd6ZEjtzvQzibmgNAiBXnKFx8UCCkR2RtV0scxR7Ogm4lCg0uNSA6lOdQMY0jw","target":"fleet-server-status.timer"},"protocol":"muster-helper/1"}
```

#### V6 plan hash mismatch

The helper's own plan hashes differently.  The only refusal the phone reads as "Refused, nothing ran".  Audit row:  yes (after Y8).

```
stored list:           version 1
helper clock (now):    2026-11-07T21:00:30Z
ledger nonces:         (none)
helper plan SHA-256:   7de3f43362de3ceacf6da1058c041ebbb53d48af20e862c8173474ec54fdc754
expected outcome:      plan_mismatch
approval text bytes (hex):
6d75737465722d617070726f76652f310a6a6f62376b326d39713478310a30663165326433633462356136393738383739366135623463336432653166300a61316232633364346535663630373138323933613462356336643765386639300a7365727665720a726573746172740a666c6565742d7365727665722d7374617475732e74696d65720a663136323138376266633637323536373964653035626436366136613163316165363863326539396235616433323633333330663939656334313033626534640a642d6165393432386436393565343134626365666330656635620a323032362d31312d30375432313a30303a30305a0a323032362d31312d30375432313a30323a30305a
approval text SHA-256: 0db7a5bb4cf48a40b762ec8a6be018a1bb1d7f90d11428229a4ab0e0b4b9c49e
signature (base64url DER): MEQCIG7qzh_jgCU_aocqf4dSWekVrAeZPd6ZEjtzvQzibmgNAiBXnKFx8UCCkR2RtV0scxR7Ogm4lCg0uNSA6lOdQMY0jw
stdin bytes (533 bytes):
{"approval":{"action":"restart","approvalNonce":"a1b2c3d4e5f60718293a4b5c6d7e8f90","deviceId":"d-ae9428d695e414bcefc0ef5b","expiresAt":"2026-11-07T21:02:00Z","host":"server","issuedAt":"2026-11-07T21:00:00Z","jobId":"job7k2m9q4x1","jobNonce":"0f1e2d3c4b5a69788796a5b4c3d2e1f0","planSHA256":"f162187bfc6725679de05bd66a6a1c1ae68c2e99b5ad3263330f99ec4103be4d","signature":"MEQCIG7qzh_jgCU_aocqf4dSWekVrAeZPd6ZEjtzvQzibmgNAiBXnKFx8UCCkR2RtV0scxR7Ogm4lCg0uNSA6lOdQMY0jw","target":"fleet-server-status.timer"},"protocol":"muster-helper/1"}
```

#### V7 wrong host

Well-formed and correctly signed, but host is mac.  It passes P3 and fails at Y3.  Audit row:  no.

```
stored list:           version 1
helper clock (now):    2026-11-07T21:00:30Z
ledger nonces:         (none)
helper plan SHA-256:   f162187bfc6725679de05bd66a6a1c1ae68c2e99b5ad3263330f99ec4103be4d
expected outcome:      field_mismatch
approval text bytes (hex):
6d75737465722d617070726f76652f310a6a6f62376b326d39713478310a30663165326433633462356136393738383739366135623463336432653166300a61316232633364346535663630373138323933613462356336643765386639300a6d61630a726573746172740a666c6565742d7365727665722d7374617475732e74696d65720a663136323138376266633637323536373964653035626436366136613163316165363863326539396235616433323633333330663939656334313033626534640a642d6165393432386436393565343134626365666330656635620a323032362d31312d30375432313a30303a30305a0a323032362d31312d30375432313a30323a30305a
approval text SHA-256: 103eb38172aa952cad070ead8358bb6cdc478e7ca9874ebbd8af0003f3fd64bc
signature (base64url DER): MEUCIBY1dSyQqhVJKsm1DQQ-PmrtYfV6cb0IF23r5i5DuyEPAiEA1GQXEnkpb93TejSa3w7-LLuOq6CIbZ2mp8_pLmDpM_0
stdin bytes (531 bytes):
{"approval":{"action":"restart","approvalNonce":"a1b2c3d4e5f60718293a4b5c6d7e8f90","deviceId":"d-ae9428d695e414bcefc0ef5b","expiresAt":"2026-11-07T21:02:00Z","host":"mac","issuedAt":"2026-11-07T21:00:00Z","jobId":"job7k2m9q4x1","jobNonce":"0f1e2d3c4b5a69788796a5b4c3d2e1f0","planSHA256":"f162187bfc6725679de05bd66a6a1c1ae68c2e99b5ad3263330f99ec4103be4d","signature":"MEUCIBY1dSyQqhVJKsm1DQQ-PmrtYfV6cb0IF23r5i5DuyEPAiEA1GQXEnkpb93TejSa3w7-LLuOq6CIbZ2mp8_pLmDpM_0","target":"fleet-server-status.timer"},"protocol":"muster-helper/1"}
```

#### V8 wrong target

Correctly signed for another unit than the command names.  Audit row:  no.

```
stored list:           version 1
helper clock (now):    2026-11-07T21:00:30Z
ledger nonces:         (none)
helper plan SHA-256:   f162187bfc6725679de05bd66a6a1c1ae68c2e99b5ad3263330f99ec4103be4d
expected outcome:      field_mismatch
approval text bytes (hex):
6d75737465722d617070726f76652f310a6a6f62376b326d39713478310a30663165326433633462356136393738383739366135623463336432653166300a61316232633364346535663630373138323933613462356336643765386639300a7365727665720a726573746172740a626f74666c6565742d7365727665722e736572766963650a663136323138376266633637323536373964653035626436366136613163316165363863326539396235616433323633333330663939656334313033626534640a642d6165393432386436393565343134626365666330656635620a323032362d31312d30375432313a30303a30305a0a323032362d31312d30375432313a30323a30305a
approval text SHA-256: 4ab5da35845a79668127f6811eb1c2aefea2bd84c87f4ddc7c6f9cc8ba21c696
signature (base64url DER): MEYCIQCccBFI1-7Es0YJ0doIAIkdPKhyGSvV0KHJ1oHRdePjWAIhAOinzhYSwDKQqrWahx3reGzvhMf-Ea1svJ2PiCUNTvyM
stdin bytes (533 bytes):
{"approval":{"action":"restart","approvalNonce":"a1b2c3d4e5f60718293a4b5c6d7e8f90","deviceId":"d-ae9428d695e414bcefc0ef5b","expiresAt":"2026-11-07T21:02:00Z","host":"server","issuedAt":"2026-11-07T21:00:00Z","jobId":"job7k2m9q4x1","jobNonce":"0f1e2d3c4b5a69788796a5b4c3d2e1f0","planSHA256":"f162187bfc6725679de05bd66a6a1c1ae68c2e99b5ad3263330f99ec4103be4d","signature":"MEYCIQCccBFI1-7Es0YJ0doIAIkdPKhyGSvV0KHJ1oHRdePjWAIhAOinzhYSwDKQqrWahx3reGzvhMf-Ea1svJ2PiCUNTvyM","target":"botfleet-server.service"},"protocol":"muster-helper/1"}
```

#### V9 window too long

expiresAt is 300 seconds after issuedAt.  Audit row:  no.

```
stored list:           version 1
helper clock (now):    2026-11-07T21:00:30Z
ledger nonces:         (none)
helper plan SHA-256:   f162187bfc6725679de05bd66a6a1c1ae68c2e99b5ad3263330f99ec4103be4d
expected outcome:      window_too_long
approval text bytes (hex):
6d75737465722d617070726f76652f310a6a6f62376b326d39713478310a30663165326433633462356136393738383739366135623463336432653166300a61316232633364346535663630373138323933613462356336643765386639300a7365727665720a726573746172740a666c6565742d7365727665722d7374617475732e74696d65720a663136323138376266633637323536373964653035626436366136613163316165363863326539396235616433323633333330663939656334313033626534640a642d6165393432386436393565343134626365666330656635620a323032362d31312d30375432313a30303a30305a0a323032362d31312d30375432313a30353a30305a
approval text SHA-256: a086801a6ffd7bf53066c29ba9c2724dca05e0875068828a958a58cdcc271e40
signature (base64url DER): MEUCIQCh-_RA-W_mewOnGvWhl2NLEqFTToKupXLA-L5GBj_6JwIgA74Lin1KKWIrEf3jKu6GExLsoYxEuw8VVmDELj8BD2Y
stdin bytes (534 bytes):
{"approval":{"action":"restart","approvalNonce":"a1b2c3d4e5f60718293a4b5c6d7e8f90","deviceId":"d-ae9428d695e414bcefc0ef5b","expiresAt":"2026-11-07T21:05:00Z","host":"server","issuedAt":"2026-11-07T21:00:00Z","jobId":"job7k2m9q4x1","jobNonce":"0f1e2d3c4b5a69788796a5b4c3d2e1f0","planSHA256":"f162187bfc6725679de05bd66a6a1c1ae68c2e99b5ad3263330f99ec4103be4d","signature":"MEUCIQCh-_RA-W_mewOnGvWhl2NLEqFTToKupXLA-L5GBj_6JwIgA74Lin1KKWIrEf3jKu6GExLsoYxEuw8VVmDELj8BD2Y","target":"fleet-server-status.timer"},"protocol":"muster-helper/1"}
```

#### V10 issued in the future

issuedAt is 570 seconds after the helper's clock.  Audit row:  no.

```
stored list:           version 1
helper clock (now):    2026-11-07T21:00:30Z
ledger nonces:         (none)
helper plan SHA-256:   f162187bfc6725679de05bd66a6a1c1ae68c2e99b5ad3263330f99ec4103be4d
expected outcome:      not_yet_valid
approval text bytes (hex):
6d75737465722d617070726f76652f310a6a6f62376b326d39713478310a30663165326433633462356136393738383739366135623463336432653166300a61316232633364346535663630373138323933613462356336643765386639300a7365727665720a726573746172740a666c6565742d7365727665722d7374617475732e74696d65720a663136323138376266633637323536373964653035626436366136613163316165363863326539396235616433323633333330663939656334313033626534640a642d6165393432386436393565343134626365666330656635620a323032362d31312d30375432313a31303a30305a0a323032362d31312d30375432313a31323a30305a
approval text SHA-256: 8e9f95b0bcc76ab0eb0207c31a9d80b37b4b35d6b583331d965840e713cff20e
signature (base64url DER): MEYCIQCzvvuLZp8ERNmZOAYXveP6DLfmclnzZCZ1YOVE07EveQIhAJRnYOyB5FJy93ceUxA8xkCcKPyxZDf9rEl3_wcMEtjE
stdin bytes (535 bytes):
{"approval":{"action":"restart","approvalNonce":"a1b2c3d4e5f60718293a4b5c6d7e8f90","deviceId":"d-ae9428d695e414bcefc0ef5b","expiresAt":"2026-11-07T21:12:00Z","host":"server","issuedAt":"2026-11-07T21:10:00Z","jobId":"job7k2m9q4x1","jobNonce":"0f1e2d3c4b5a69788796a5b4c3d2e1f0","planSHA256":"f162187bfc6725679de05bd66a6a1c1ae68c2e99b5ad3263330f99ec4103be4d","signature":"MEYCIQCzvvuLZp8ERNmZOAYXveP6DLfmclnzZCZ1YOVE07EveQIhAJRnYOyB5FJy93ceUxA8xkCcKPyxZDf9rEl3_wcMEtjE","target":"fleet-server-status.timer"},"protocol":"muster-helper/1"}
```

#### V11 checked exactly at expiresAt

now equals expiresAt.  Accepted, so it passes Y8 and runs.  Audit row:  yes.

```
stored list:           version 1
helper clock (now):    2026-11-07T21:02:00Z
ledger nonces:         (none)
helper plan SHA-256:   f162187bfc6725679de05bd66a6a1c1ae68c2e99b5ad3263330f99ec4103be4d
expected outcome:      accepted
approval text bytes (hex):
6d75737465722d617070726f76652f310a6a6f62376b326d39713478310a30663165326433633462356136393738383739366135623463336432653166300a61316232633364346535663630373138323933613462356336643765386639300a7365727665720a726573746172740a666c6565742d7365727665722d7374617475732e74696d65720a663136323138376266633637323536373964653035626436366136613163316165363863326539396235616433323633333330663939656334313033626534640a642d6165393432386436393565343134626365666330656635620a323032362d31312d30375432313a30303a30305a0a323032362d31312d30375432313a30323a30305a
approval text SHA-256: 0db7a5bb4cf48a40b762ec8a6be018a1bb1d7f90d11428229a4ab0e0b4b9c49e
signature (base64url DER): MEQCIG7qzh_jgCU_aocqf4dSWekVrAeZPd6ZEjtzvQzibmgNAiBXnKFx8UCCkR2RtV0scxR7Ogm4lCg0uNSA6lOdQMY0jw
stdin bytes (533 bytes):
{"approval":{"action":"restart","approvalNonce":"a1b2c3d4e5f60718293a4b5c6d7e8f90","deviceId":"d-ae9428d695e414bcefc0ef5b","expiresAt":"2026-11-07T21:02:00Z","host":"server","issuedAt":"2026-11-07T21:00:00Z","jobId":"job7k2m9q4x1","jobNonce":"0f1e2d3c4b5a69788796a5b4c3d2e1f0","planSHA256":"f162187bfc6725679de05bd66a6a1c1ae68c2e99b5ad3263330f99ec4103be4d","signature":"MEQCIG7qzh_jgCU_aocqf4dSWekVrAeZPd6ZEjtzvQzibmgNAiBXnKFx8UCCkR2RtV0scxR7Ogm4lCg0uNSA6lOdQMY0jw","target":"fleet-server-status.timer"},"protocol":"muster-helper/1"}
```

#### V12 issued exactly 60 seconds ahead

issuedAt equals now plus 60 seconds.  Accepted, so it passes Y8 and runs.  Audit row:  yes.

```
stored list:           version 1
helper clock (now):    2026-11-07T21:00:30Z
ledger nonces:         (none)
helper plan SHA-256:   f162187bfc6725679de05bd66a6a1c1ae68c2e99b5ad3263330f99ec4103be4d
expected outcome:      accepted
approval text bytes (hex):
6d75737465722d617070726f76652f310a6a6f62376b326d39713478310a30663165326433633462356136393738383739366135623463336432653166300a61316232633364346535663630373138323933613462356336643765386639300a7365727665720a726573746172740a666c6565742d7365727665722d7374617475732e74696d65720a663136323138376266633637323536373964653035626436366136613163316165363863326539396235616433323633333330663939656334313033626534640a642d6165393432386436393565343134626365666330656635620a323032362d31312d30375432313a30313a33305a0a323032362d31312d30375432313a30333a33305a
approval text SHA-256: ceba2fb07e294647904a78febc882bcd02fc6d3caffea2403f465b474d4463de
signature (base64url DER): MEUCIHP-S5jIjsKPv2Fq2H4DaZCfvFdQ5td1E2AejKxMvO7MAiEAmjm_yGb4_jCsR0kupusdRfd_jpXvo5x3aMi9xG3w4EA
stdin bytes (534 bytes):
{"approval":{"action":"restart","approvalNonce":"a1b2c3d4e5f60718293a4b5c6d7e8f90","deviceId":"d-ae9428d695e414bcefc0ef5b","expiresAt":"2026-11-07T21:03:30Z","host":"server","issuedAt":"2026-11-07T21:01:30Z","jobId":"job7k2m9q4x1","jobNonce":"0f1e2d3c4b5a69788796a5b4c3d2e1f0","planSHA256":"f162187bfc6725679de05bd66a6a1c1ae68c2e99b5ad3263330f99ec4103be4d","signature":"MEUCIHP-S5jIjsKPv2Fq2H4DaZCfvFdQ5td1E2AejKxMvO7MAiEAmjm_yGb4_jCsR0kupusdRfd_jpXvo5x3aMi9xG3w4EA","target":"fleet-server-status.timer"},"protocol":"muster-helper/1"}
```

#### V13 approval nonce equals job nonce

The two nonces must differ.  Audit row:  no.

```
stored list:           version 1
helper clock (now):    2026-11-07T21:00:30Z
ledger nonces:         (none)
helper plan SHA-256:   f162187bfc6725679de05bd66a6a1c1ae68c2e99b5ad3263330f99ec4103be4d
expected outcome:      bad_input
approval text bytes (hex):
6d75737465722d617070726f76652f310a6a6f62376b326d39713478310a30663165326433633462356136393738383739366135623463336432653166300a30663165326433633462356136393738383739366135623463336432653166300a7365727665720a726573746172740a666c6565742d7365727665722d7374617475732e74696d65720a663136323138376266633637323536373964653035626436366136613163316165363863326539396235616433323633333330663939656334313033626534640a642d6165393432386436393565343134626365666330656635620a323032362d31312d30375432313a30303a30305a0a323032362d31312d30375432313a30323a30305a
approval text SHA-256: f9788c39e9e4a6a63d8d9dacc58de3830cb942a8c6029578cca3d422d9e02c77
signature (base64url DER): MEYCIQDgUXlToXHbT5jma6Ht66GBfDMNRAXnrFkPBDceW_tpSAIhAJfwWqf6LgOTqboQLLP5vdrOk7iblY2t75nUEejOXyb_
stdin bytes (535 bytes):
{"approval":{"action":"restart","approvalNonce":"0f1e2d3c4b5a69788796a5b4c3d2e1f0","deviceId":"d-ae9428d695e414bcefc0ef5b","expiresAt":"2026-11-07T21:02:00Z","host":"server","issuedAt":"2026-11-07T21:00:00Z","jobId":"job7k2m9q4x1","jobNonce":"0f1e2d3c4b5a69788796a5b4c3d2e1f0","planSHA256":"f162187bfc6725679de05bd66a6a1c1ae68c2e99b5ad3263330f99ec4103be4d","signature":"MEYCIQDgUXlToXHbT5jma6Ht66GBfDMNRAXnrFkPBDceW_tpSAIhAJfwWqf6LgOTqboQLLP5vdrOk7iblY2t75nUEejOXyb_","target":"fleet-server-status.timer"},"protocol":"muster-helper/1"}
```

#### V14 device not on the stored list

Device B, correctly signed by its own approval key, while the helper holds version 1, which lists only A.  Audit row:  no.

```
stored list:           version 1
helper clock (now):    2026-11-07T21:00:30Z
ledger nonces:         (none)
helper plan SHA-256:   f162187bfc6725679de05bd66a6a1c1ae68c2e99b5ad3263330f99ec4103be4d
expected outcome:      unknown_device
approval text bytes (hex):
6d75737465722d617070726f76652f310a6a6f62376b326d39713478310a30663165326433633462356136393738383739366135623463336432653166300a61316232633364346535663630373138323933613462356336643765386639300a7365727665720a726573746172740a666c6565742d7365727665722d7374617475732e74696d65720a663136323138376266633637323536373964653035626436366136613163316165363863326539396235616433323633333330663939656334313033626534640a642d3632396536323334333138306164383833643866376134370a323032362d31312d30375432313a30303a30305a0a323032362d31312d30375432313a30323a30305a
approval text SHA-256: 1bd383b4a02688723ef5442f779725525df92a99c1be3ce17ac73b2923c9c226
signature (base64url DER): MEUCIQC-nAdU5Sa-aMvEAM9RDVlHaTDckgz1h7MLMPQ5evjXcAIgdq4TXtrcUOXv8BhIqxdq1U11kfk-Ne08gSw-y5URHsk
stdin bytes (534 bytes):
{"approval":{"action":"restart","approvalNonce":"a1b2c3d4e5f60718293a4b5c6d7e8f90","deviceId":"d-629e62343180ad883d8f7a47","expiresAt":"2026-11-07T21:02:00Z","host":"server","issuedAt":"2026-11-07T21:00:00Z","jobId":"job7k2m9q4x1","jobNonce":"0f1e2d3c4b5a69788796a5b4c3d2e1f0","planSHA256":"f162187bfc6725679de05bd66a6a1c1ae68c2e99b5ad3263330f99ec4103be4d","signature":"MEUCIQC-nAdU5Sa-aMvEAM9RDVlHaTDckgz1h7MLMPQ5evjXcAIgdq4TXtrcUOXv8BhIqxdq1U11kfk-Ne08gSw-y5URHsk","target":"fleet-server-status.timer"},"protocol":"muster-helper/1"}
```

#### V15 stored list closed

V1's approval while the helper holds version 3, which is closed.  Audit row:  no.

```
stored list:           version 3 (closed)
helper clock (now):    2026-11-07T21:00:30Z
ledger nonces:         (none)
helper plan SHA-256:   f162187bfc6725679de05bd66a6a1c1ae68c2e99b5ad3263330f99ec4103be4d
expected outcome:      unknown_device
approval text bytes (hex):
6d75737465722d617070726f76652f310a6a6f62376b326d39713478310a30663165326433633462356136393738383739366135623463336432653166300a61316232633364346535663630373138323933613462356336643765386639300a7365727665720a726573746172740a666c6565742d7365727665722d7374617475732e74696d65720a663136323138376266633637323536373964653035626436366136613163316165363863326539396235616433323633333330663939656334313033626534640a642d6165393432386436393565343134626365666330656635620a323032362d31312d30375432313a30303a30305a0a323032362d31312d30375432313a30323a30305a
approval text SHA-256: 0db7a5bb4cf48a40b762ec8a6be018a1bb1d7f90d11428229a4ab0e0b4b9c49e
signature (base64url DER): MEQCIG7qzh_jgCU_aocqf4dSWekVrAeZPd6ZEjtzvQzibmgNAiBXnKFx8UCCkR2RtV0scxR7Ogm4lCg0uNSA6lOdQMY0jw
stdin bytes (533 bytes):
{"approval":{"action":"restart","approvalNonce":"a1b2c3d4e5f60718293a4b5c6d7e8f90","deviceId":"d-ae9428d695e414bcefc0ef5b","expiresAt":"2026-11-07T21:02:00Z","host":"server","issuedAt":"2026-11-07T21:00:00Z","jobId":"job7k2m9q4x1","jobNonce":"0f1e2d3c4b5a69788796a5b4c3d2e1f0","planSHA256":"f162187bfc6725679de05bd66a6a1c1ae68c2e99b5ad3263330f99ec4103be4d","signature":"MEQCIG7qzh_jgCU_aocqf4dSWekVrAeZPd6ZEjtzvQzibmgNAiBXnKFx8UCCkR2RtV0scxR7Ogm4lCg0uNSA6lOdQMY0jw","target":"fleet-server-status.timer"},"protocol":"muster-helper/1"}
```

#### V16 nothing stored

V1's approval while the helper holds no list.  Audit row:  no.

```
stored list:           nothing
helper clock (now):    2026-11-07T21:00:30Z
ledger nonces:         (none)
helper plan SHA-256:   f162187bfc6725679de05bd66a6a1c1ae68c2e99b5ad3263330f99ec4103be4d
expected outcome:      no_trust_list
approval text bytes (hex):
6d75737465722d617070726f76652f310a6a6f62376b326d39713478310a30663165326433633462356136393738383739366135623463336432653166300a61316232633364346535663630373138323933613462356336643765386639300a7365727665720a726573746172740a666c6565742d7365727665722d7374617475732e74696d65720a663136323138376266633637323536373964653035626436366136613163316165363863326539396235616433323633333330663939656334313033626534640a642d6165393432386436393565343134626365666330656635620a323032362d31312d30375432313a30303a30305a0a323032362d31312d30375432313a30323a30305a
approval text SHA-256: 0db7a5bb4cf48a40b762ec8a6be018a1bb1d7f90d11428229a4ab0e0b4b9c49e
signature (base64url DER): MEQCIG7qzh_jgCU_aocqf4dSWekVrAeZPd6ZEjtzvQzibmgNAiBXnKFx8UCCkR2RtV0scxR7Ogm4lCg0uNSA6lOdQMY0jw
stdin bytes (533 bytes):
{"approval":{"action":"restart","approvalNonce":"a1b2c3d4e5f60718293a4b5c6d7e8f90","deviceId":"d-ae9428d695e414bcefc0ef5b","expiresAt":"2026-11-07T21:02:00Z","host":"server","issuedAt":"2026-11-07T21:00:00Z","jobId":"job7k2m9q4x1","jobNonce":"0f1e2d3c4b5a69788796a5b4c3d2e1f0","planSHA256":"f162187bfc6725679de05bd66a6a1c1ae68c2e99b5ad3263330f99ec4103be4d","signature":"MEQCIG7qzh_jgCU_aocqf4dSWekVrAeZPd6ZEjtzvQzibmgNAiBXnKFx8UCCkR2RtV0scxR7Ogm4lCg0uNSA6lOdQMY0jw","target":"fleet-server-status.timer"},"protocol":"muster-helper/1"}
```

### Illustrative Plan

```
{"action":"restart","auditTarget":"fleet-server-status.timer","commands":[["/usr/bin/systemctl","--user","restart","fleet-server-status.timer"]],"contract":"fleet-ops-plan/1","scope":"server","target":"fleet-server-status.timer"}
```

`planSHA256` = `f162187bfc6725679de05bd66a6a1c1ae68c2e99b5ad3263330f99ec4103be4d`.  The helper builds the real plan;  tests inject the helper's plan hash as each vector says.

### Signed Answers

**A signed plan for V1's job (the phone checks SHA-256 of plan equals planSHA256)**, canonical body SHA-256 `e5b96a09d1ced478758f712c44d03de5ab448b179615eee4574c745c72444d8c`:

```json
{
  "body": {
    "action": "restart",
    "claimed": {
      "deviceId": "d-ae9428d695e414bcefc0ef5b",
      "jobId": "job7k2m9q4x1",
      "jobNonce": "0f1e2d3c4b5a69788796a5b4c3d2e1f0"
    },
    "contract": "muster-signed-plan/1",
    "helperTrustListVersion": 1,
    "host": "server",
    "issuedAt": "2026-11-07T20:59:50Z",
    "plan": "{\"action\":\"restart\",\"auditTarget\":\"fleet-server-status.timer\",\"commands\":[[\"/usr/bin/systemctl\",\"--user\",\"restart\",\"fleet-server-status.timer\"]],\"contract\":\"fleet-ops-plan/1\",\"scope\":\"server\",\"target\":\"fleet-server-status.timer\"}",
    "planSHA256": "f162187bfc6725679de05bd66a6a1c1ae68c2e99b5ad3263330f99ec4103be4d",
    "target": "fleet-server-status.timer"
  },
  "keyId": "38fbd10025f412d0",
  "kind": "plan",
  "protocol": "muster-helper/1",
  "signature": "MEQCIBI-WzxlGZY8xxVBbDfsF3MHFzjcQtzIuF8oDmr4gdIwAiAjElcSer1SSIzSYv4nfUINKMoEJcdiRUrWTF6DR3Hipg"
}
```

**A done result for V1**, canonical body SHA-256 `fb00db21378ae4d8e6a8bf31a04d5d221ea1533f6461fdf4e08432187971a134`:

```json
{
  "body": {
    "action": "restart",
    "approvalNonce": "a1b2c3d4e5f60718293a4b5c6d7e8f90",
    "auditId": "a1b2c3d4",
    "contract": "muster-signed-result/1",
    "detail": "restarted",
    "deviceId": "d-ae9428d695e414bcefc0ef5b",
    "finishedAt": "2026-11-07T21:00:33Z",
    "helperTrustListVersion": 1,
    "host": "server",
    "jobId": "job7k2m9q4x1",
    "jobNonce": "0f1e2d3c4b5a69788796a5b4c3d2e1f0",
    "outcome": "done",
    "outputB64": null,
    "planSHA256": "f162187bfc6725679de05bd66a6a1c1ae68c2e99b5ad3263330f99ec4103be4d",
    "target": "fleet-server-status.timer"
  },
  "keyId": "38fbd10025f412d0",
  "kind": "result",
  "protocol": "muster-helper/1",
  "signature": "MEUCIQDFqIlO-vpC-wNdOSG5Ur01GaDAX4Dsx-rnrD4dPtbt5AIgCQNAsiX-Gogq0eugYSG74vfOHC-VsbzPPJrq7yCyXTw"
}
```

**The refusal for V3 (unverified:  the fields are only claimed)**, canonical body SHA-256 `f4a7c0ea905cc4bcfc14a5b8c783043f4e15dca1801fc87deeb573bad46564f1`:

```json
{
  "body": {
    "claimed": {
      "action": "restart",
      "approvalNonce": "a1b2c3d4e5f60718293a4b5c6d7e8f90",
      "deviceId": "d-ae9428d695e414bcefc0ef5b",
      "host": "server",
      "jobId": "job7k2m9q4x1",
      "jobNonce": "0f1e2d3c4b5a69788796a5b4c3d2e1f0",
      "planSHA256": "f162187bfc6725679de05bd66a6a1c1ae68c2e99b5ad3263330f99ec4103be4d",
      "target": "fleet-server-status.timer"
    },
    "contract": "muster-signed-refusal/1",
    "errorCode": "bad_signature",
    "helperTrustListVersion": 1,
    "reason": null,
    "refusedAt": "2026-11-07T21:00:31Z",
    "verified": false
  },
  "keyId": "38fbd10025f412d0",
  "kind": "refusal",
  "protocol": "muster-helper/1",
  "signature": "MEUCIHAe62OgP73rmOwN7Ky9mjZBR1uwWXunD7OjUTDP1fkBAiEA2rNL6Vvp4fWWXms2xUSou9fmOGl1xjPkpHT6fSstli0"
}
```

**The refusal for V6 (verified)**, canonical body SHA-256 `113a684901cfd1c87f6a7cd0585bcc3cb568ebb1fc668ee31e3e43fee036c067`:

```json
{
  "body": {
    "action": "restart",
    "approvalNonce": "a1b2c3d4e5f60718293a4b5c6d7e8f90",
    "contract": "muster-signed-refusal/1",
    "deviceId": "d-ae9428d695e414bcefc0ef5b",
    "errorCode": "plan_mismatch",
    "helperTrustListVersion": 1,
    "host": "server",
    "jobId": "job7k2m9q4x1",
    "jobNonce": "0f1e2d3c4b5a69788796a5b4c3d2e1f0",
    "planSHA256": "f162187bfc6725679de05bd66a6a1c1ae68c2e99b5ad3263330f99ec4103be4d",
    "reason": null,
    "refusedAt": "2026-11-07T21:00:31Z",
    "target": "fleet-server-status.timer",
    "verified": true
  },
  "keyId": "38fbd10025f412d0",
  "kind": "refusal",
  "protocol": "muster-helper/1",
  "signature": "MEQCIB_8NHvQdv48xg8TgovOU637BcoC9lqMwR_kYuEYfdgnAiBV4XQMbyQdqc5Yv4mA14m58zWHOspy6MTf_aP1I6EE6w"
}
```

**A chain refusal for T7 (not final)**, canonical body SHA-256 `b2d1e8effab6fdd11c54ab286de0e74543ae33f19812f2a7132c0631b6edca6d`:

```json
{
  "body": {
    "chain": {
      "entrySHA256": "540040efe15dcefb0c5f1d31749bafd66c22475e5630a327f4e817cdee5ececc",
      "final": false,
      "version": 2
    },
    "contract": "muster-signed-refusal/1",
    "errorCode": "chain_refused",
    "helperTrustListVersion": 1,
    "reason": "previous_hash",
    "refusedAt": "2026-11-07T20:11:00Z",
    "verified": false
  },
  "keyId": "38fbd10025f412d0",
  "kind": "refusal",
  "protocol": "muster-helper/1",
  "signature": "MEUCIBeOMeZWVmAl0KKbKsOg5NFa11knxMAv2wgHs9jWvXIHAiEA4V8NGSNeUWv2i7EnvxnbqepllmVpOAiQzmyts4DOXzg"
}
```

**The acknowledgement after T2, holding version 2**, canonical body SHA-256 `9d298b221e08f88f8f2579f7cd9d9576625e3866f561e15721ddc83fddc215dd`:

```json
{
  "body": {
    "at": "2026-11-07T20:12:02Z",
    "closed": false,
    "contract": "muster-signed-ack/1",
    "finalRefusals": [],
    "helperTrustListVersion": 2,
    "signedByCurrentKey": true,
    "trustListSHA256": "c2b6d904493faab638728e46b4e43adf939825611f70e3190671af90ee511024"
  },
  "keyId": "38fbd10025f412d0",
  "kind": "ack",
  "protocol": "muster-helper/1",
  "signature": "MEQCIFhV5Qz7or-RV1MPPcYCbJPOcjDNBLkFG9Upx3cklnWWAiAuc9m4lzy6f_xgbI8dHGLZlMr9bRngI3Rdx2A0xwPfCA"
}
```

**The acknowledgement after T3, holding the closed version 3**, canonical body SHA-256 `4980ff6b3ac2e8aba483bb620c9d497564a3906459c53f6e4a73fe614f1db543`:

```json
{
  "body": {
    "at": "2026-11-07T20:22:02Z",
    "closed": true,
    "contract": "muster-signed-ack/1",
    "finalRefusals": [],
    "helperTrustListVersion": 3,
    "signedByCurrentKey": true,
    "trustListSHA256": "5e06787afce950d2f803ac3716dc9ee0d033798f2803eb3393b6b3fa53e88ce8"
  },
  "keyId": "38fbd10025f412d0",
  "kind": "ack",
  "protocol": "muster-helper/1",
  "signature": "MEQCIBz42pls5KK5Y2NElye4f8_6lUYfGxJ6aWlyws0a_cNzAiAGyw3Y7S0mJmpVzpWJ2oUC-ITrsIVLTzmxdT_n6E2tFQ"
}
```

### Cases Without Fixed Bytes

A harness builds these:  `pin_unsafe` (owner.pub group-writable, owned by jay, or a symbolic link;  the same for the reopen record when it exists, or a record line that does not match its pattern);  `helper_key_missing` (the key file absent, a symbolic link, mode 0644, or unparseable);  `input_too_large` (4,097 bytes);  `bad_input` (a duplicate key, `NaN`, a non-ASCII string, or a chain entry missing a body field);  a valid approval whose stdin keys are in another order with extra spaces (accepted, since formatting does not matter);  `state_unsafe` (`muster-nonces.jsonl` mode 0644, `muster-trust.json` a symbolic link or empty, or an unparseable rate file);  a torn final ledger fragment (truncated, then the approval proceeds);  a complete final ledger line without a line feed (a line feed is appended, and that nonce stays spent);  a corrupt middle line (`ledger_corrupt`);  `rate_limited` (two `plan` requests for one `verb target` within the spacing, two different approvals for one `verb target` within 2 seconds, the class's hourly cap reached, or `muster-rate.lock` held elsewhere for more than 2 seconds);  a `yes` approval refused `rate_limited` at Y8b (its nonce not in the ledger afterwards, and no audit row), then retried inside its window and accepted;  60 approvals with bad signatures, then V1 (accepted:  none of the 60 used the `yes` budget);  V1 replayed 60 times after it ran (`replayed_nonce` each time, and a fresh approval for the same `verb target` 2 seconds after V1 is accepted);  `bad_command` for a misspelt chain command (`muster trust-list chain` with no mode, `--YES`, `Chain`, or a doubled, leading or trailing space), before its input is read, not counted, and with no audit row;  the acknowledgement with nothing held (`closed` false, `signedByCurrentKey` false, `helperTrustListVersion` and `trustListSHA256` null);  `plan_refused` with `revision_stale` (a `resource-policy` target naming a revision older than the saved policy's);  a redeploy row named `Café App` (the muster plan's `auditTarget` is `Caf? App`, and the `--from-ssh` route's `audit_target` is unchanged);  `trust_list_changed` (the stored list replaced between Y2 and Y7);  a replay of V1 long after its window (`expired` at Y6, before the ledger is read, and no audit row);  V1 replayed inside its window after the clock was stepped forward more than seven days and back (`replayed_nonce`:  the ledger kept its nonce, which is within the last 4,096 lines);  a ledger of more than 4,096 lines rewritten under the lock (only lines beyond the last 4,096 whose `expiresAt` is more than seven days before `now` are dropped, a line whose `expiresAt` is after `now` stays, and the rewrite is `fsync`ed, renamed and its directory `fsync`ed);  a ledger over 4 MiB (`ledger_corrupt`);  V1's `approvalNonce` in another approval from another device or another job (`replayed_nonce`);  V1's high-S twin sent after V1 ran (`replayed_nonce`);  `clock_unsynced` (V1 with the clock reported unsynchronised, and no audit row);  V1 at `now` = 21:02:00.001 (`expired`);  rate boundaries (a second `plan` request exactly 5 seconds after the first passes, at 4.999 seconds is `rate_limited`;  an event exactly 3,600 seconds old still counts;  a rate file with 811 events is `state_unsafe`);  `plan_refused` (`update_busy` with the update's unit activating;  `report_stale` with a 46-minute-old report;  each with `helperTrustListVersion` `null`, since R2 comes before the list is read);  an unsafe state directory (mode 0755:  `helper_key_missing`);  a fault after Y8c before the action began (a signed result with outcome `failed`, `detail` `plan_refused: <reason>` or `internal_error`, and one audit row whose `result` is `failed`);  a fault once the run began (outcome `unknown`, `detail` `the helper stopped unexpectedly while running`, and one audit row whose `result` is `unknown`);  a command killed at its limit, and a Coolify GET that timed out, was reset or got a cut answer (`unknown` each);  a refused connection to Coolify (`failed`);  an empty reopen record (accepted as no lines);  a record whose last line has no line feed (`pin_unsafe`);  from version 5.3:  after T1, `muster-trust.json` holds v1's canonical bytes with no line feed, and its SHA-256 is v1's entry SHA-256;  a stored list written by someone else with spaces and a line feed (accepted, since readers take any formatting);  the T24 list held under the rotated history, whose closed line names another body hash for version 1 (`state_unsafe` at C2);  a list stored after C2 where none was held, and a list removed after C2 (`trust_list_changed` at C4 each);  a fault at C5 before the rename (`internal_error`, nothing stored, and the temporary file gone);  a fault at C5 after the rename (`internal_error`, the list held, and the same chain sent again is `version_gap`);  a failed audit append at C5 after the rename (the signed `ack` is answered and the list is held);  from version 5.4, each at P3 and `bad_input`, never a §8 reason, and not counted, with the list re-signed so that only its one fault remains (`make_vectors_v5.py` asserts each):  an `ownerKey`, a device key or a `services` key that is a compressed point (a 33-byte point in a 59-byte SubjectPublicKeyInfo) or a hybrid point (the 91-byte form with `06` or `07` where `04` belongs);  a key whose last base64url character has a stray trailing bit set, or that ends in `=` padding;  an `issuedAt` of `2026-11-07T23:59:60Z`, `2026-02-30T20:00:00Z` or `0000-11-07T20:00:00Z`;  a body whose `contract` is `muster-trust-list/2`;  and any of these in the second entry of a chain whose first entry is valid (the whole request is `bad_input`);  also from version 5.4:  an approval whose `signature` has a stray trailing bit set (`bad_signature` at Y5);  a first list with `closed` false and `devices` empty (`chain_refused`, reason `closed_rule`:  check 7 holds `closed` true exactly when there is no device);  a reader that opens `muster-trust.json` just before a chain's rename and `fstat`s it just after (link count 0:  accepted, and it reads the old list);  a second hard link to `muster-trust.json` (`state_unsafe`);  a FIFO in place of `muster-trust.json`, the ledger or the rate file (the open does not hang, and the answer is `state_unsafe`);  `pin_unsafe` for a chain, `bad_command` for `muster trust-list chain --yes` with a fifth word, and `mode_not_allowed` for `muster trust-list chain --plan` (each with `claimed` `{}` and no `chain`), against `bad_input` and `rate_limited` for a chain (`chain` `null` and no `claimed`);  the T24 list held under the rotated history (`state_unsafe` at C2 with `helperTrustListVersion` `null`);  a signing fault at C5 after the rename that repeats when the refusal is signed (the unsigned `helper_key_missing`, and the list held) and one that does not (the signed `internal_error`, and the list held);  and a failed directory `fsync` at C5 (`internal_error`, the list held, and no audit row);  from version 5.5:  a stale temporary file named `.muster-trust.json.`, 16 lowercase hex characters and `.tmp`, a regular file of the helper's account with one link, is gone after an accepted chain;  a file of another name (15 or 17 hex characters, upper-case hex, or another state file's prefix), a symbolic link or a directory of that name, a file of that name with two links, and one owned by another account are left alone;  a chain refused before C5 (at C3, or `action_busy` at C4) sweeps nothing;  and a sweep whose listing, `stat` or `unlink` fails still lets the chain be accepted.

## 17.  Open

### 17A.  The Recovery Machinery (OPEN, Gates Milestone 5)

Rule C (§8), the reopen record and recovery (§9), the final chain refusal store (§11), `finalRefusals` (§13) and the chain-refusal audit row (§14).  Owner:  AA for the design, BB for the helper's half.  Gates Milestone 5.  EE's eighth look raised these;  they are listed here, not solved.

| # | Problem | Kind |
| --- | --- | --- |
| A1 | **Retiring reopen codes by line order breaks late walkers.**  Only the newest line counts, and `reopen` never checks the stored lists, so acceptance of a rule C entry depends on when it was checked.  A second `reopen` for a version some verifier already accepted (a mistyped version, an owner re-running it while the hub withholds acknowledgements, or a new phone whose key died while pending) retires the accepted code:  the Milestone 5 helper's bootstrap, a reinstalled `muster-pub` and any walk from version 1 then refuse it with `reopen_tag`, and nothing clears it.  A hostile hub can also split `muster-pub` from the helper for good this way.  | Major |
| A2 | **Final chain refusals can be minted without the owner key.**  `owner_signature` and the check-2 `first_rule` need no owner signature;  any countersignature bytes added to one genuine owner-signed body give new entry hashes refused `countersignature` or `closed_rule`;  ECDSA s → n − s malleability gives more.  Each would add a refusal line and an audit row.  EE's proposed key:  (version, body hash, reason), recorded only when check 4 verified.  | Major |
| A3 | **A forged `muster-pub` heartbeat freezes Muster with no exit.**  While the applied list names no `serverHelper`, one heartbeat naming the applied version with another hash stops Muster and the phones;  the chain cannot walk, no exit is defined, and while stopped Muster cannot sign the close that revokes a stolen phone.  | Major |
| A4 | **The newest-8 window versus first-line-per-entry.**  The store keeps one line per entry hash and never rewrites, while the acknowledgement carries only the newest 8 lines, so junk refusals at the pending version push the pending entry's own refusal out for good and wedge a reopen.  | Major |
| A5 | `muster-pub`'s refusal store has no rate limit or size cap, so the hub alone can grow it on the server's root filesystem.  | Minor |
| A6 | A close signed while a reopen is pending at the same version can be silently undone when the server takes the reopen.  | Minor |
| A7 | The device binding checks only a `deviceId` string:  nothing requires a device entry's `deviceId` to equal `d-` and the first 24 hex of SHA-256 of its own `approvalKey`.  **v5.3:**  its fix goes into rule C only, when §17A is settled;  it adds no check to checks 1 to 7 and no condition to rules First, A or B, so the chain slice built on them does not change.  | Minor |
| A8 | Rule A may remove both server keys, after which a close and reopen can never be acknowledged.  **Settled in v5.3:**  rule A refuses such a list `server_keys_removed`, as rule B does (§8, vector T25).  | Minor (settled in v5.3) |
| A9 | `finalRefusals` is undefined when `helperTrustListVersion` is `null` (settled in v5.1:  it is `[]`, §13);  the chain refusal's `chain` field for `action_busy` and `trust_list_changed`, and Muster's discard step, should name `errorCode` `chain_refused`.  | Minor |
| A10 | A one-device owner has no stated way to leave a pending entry whose device can no longer be used.  | Minor |
| A11 | The design said "the list names `serverHelper`" where this spec says the applied list;  the design now follows the spec.  | Minor (wording, fixed in the design) |

### 17B.  Other Open Items

| # | Item | Owner | Gates |
| --- | --- | --- | --- |
| 3 | Two points about `clock_unsynced` (§7) that are not verified:  how the kernel's `maxerror` and `STA_UNSYNC` behave after a long chrony outage, so whether the check refuses approvals then;  and whether anything other than chrony steps this server's clock.  | BB | Milestone 5 |
| 5 | How a phone accepts an owner-key rotation (the old key's statement and the Face ID step).  | AA | Milestone 1 |
| 6 | The paths, in `muster-pub`'s home, of its stored list, and `muster-agent trust-status`'s exact output.  | AA | Milestone 1 |
| 7 | `unit-status` on the `--from-muster` route (v5.3, from BB at EE's request):  a read-only verb `server unit-status <unit> --plan` (`ActiveState`, `SubState`, `Result`, the last exit time, the next elapse for a timer, and `checkedAt`), built on the `--from-ssh` route first (a PR from BB's lane `helper-unit-status`).  The phone does not need it until its read paths are designed, so it is not in §4's verb table yet.  | BB | Nothing yet (the phone's read paths) |

Items 1 (plan commands) and 2 (audit field names) are settled by BB's answers (§4, §14).  Item 4 (whether Swift's `JSONEncoder` with `.sortedKeys` reproduces the canonical-form vector) is settled in v5.2 (§2):  Swift uses a hand-written canonical encoder, never `JSONEncoder` or `.sortedKeys`, and a strict parser, and the encoder must reproduce the vector byte for byte.

## 18.  Review Response

EE's eighth look, as it touches this spec, and BB's answers.

| Item | Outcome |
| --- | --- |
| Majors:  retired codes break late walkers;  minted final refusals;  heartbeat freeze;  newest-8 window | OPEN (§17A, A1 to A4), with the whole recovery machinery, gating Milestone 5, as the coordinator directed.  Milestones 1 to 4 do not use it:  rule C entries are refused `reopen_unavailable`, nothing is recorded as final, `finalRefusals` is empty, and a contradicting heartbeat only warns.  |
| Minors inside the machinery | OPEN (§17A, A5 to A10).  |
| `repin`:  root fails the owner check;  no signing freeze during rotation | Fixed in v5 (§10):  root readers check the expected account;  Muster signs nothing with the old key from showing N until `signedByCurrentKey: false` proves `repin` ran.  |
| Rule table overlaps (First and B when nothing is held) | Fixed in v5 (§8):  "Applies When" is exclusive.  |
| §15 lists `trust_list_changed` at C3 | Fixed in v5.  |
| Refusal `helperTrustListVersion` undefined when nothing is held | Fixed in v5 (§13).  |
| Rate-limit boundaries;  over-810 file;  P0 to P3 not counted | Fixed in v5 (§12):  inclusive spacing and hour, `state_unsafe` over 810, and the design's claim narrowed to requests that parse.  |
| `resource-policy` audit rows;  a plan that fails after Y8 | Fixed in v5 from BB's answers (§12 Faults, §14).  |
| BB's answers:  plan commands, audit fields, unsynced clock, planner refusals, fault code, `unknown`, rate key, exact window, state directory, empty record, module equality | Made normative (§4, §7, §9, §10, §11, §12, §13, §14).  |
| Corrections before merge (EE's check of PR 1484, and BB) | The `clock_unsynced` test is normative (§7:  `adjtimex(modes=0)`, `TIME_ERROR`, `STA_UNSYNC`, `maxerror` above 1 second, or a failed call), with two unverified points left in 17B.3;  §16 says Milestone 1 to 4 builders skip the rule C rows or expect `reopen_unavailable`, and adds vector T23 for it;  V11 to V16 state their audit rows, and V11 and V12, being accepted, get one.  The version stays 5:  these settle text the review found unclear, and change no frozen rule.  |
| Sentry on PR 1484:  plan commands diverge from `scripts/fleet-ops-control.py` | Confirmed against `main` at `96a7d33f3`, and BB confirmed `main` is authoritative:  `--user` only for user-scope units, `SYSTEMCTL` and `JOURNALCTL` resolved on the host, redeploy as `["coolify","GET",…]`, and the update argv exactly as `update_command` builds it.  §4 now quotes `main`, adds that the app signs the server's own plan hash, and takes `plan_refused` reasons from the planner's own error codes.  The illustrative plan's bytes did not change (the timer is user-scope and `/usr/bin/systemctl` exists here).  |
| Every vector | EE's v4 run held.  v5's approval vectors are re-signed over the new plan with the same outcomes, and `check_vectors_v5.py` re-checks everything.  |
| Independent review of BB's build:  an acknowledgement with nothing held left its fields open, and a null-version acknowledgement could pass as proof that `repin` ran | Fixed in v5.1 (§13, §10):  nothing held answers `closed: false`, `signedByCurrentKey: false` and both nulls;  every verifier branches on a null `helperTrustListVersion` first;  the rotation proof needs that version to equal K.  |
| Independent review of BB's build:  a refusal's `helperTrustListVersion` could be read as coverage | Fixed in v5.1 (§13):  the version is still reported, as information only;  `no_trust_list` with version K means a list exists and is not usable.  |
| Independent review of BB's build:  a stale resource-policy revision fell through to `internal_error` | Fixed in v5.1 (§4, §12, §15):  `plan_refused` with reason `revision_stale`;  BB's final build names three more planner words (below).  |
| Independent review of BB's build (security):  the `yes` rate step ran before the signature check, so 60 wrongly signed requests could make the owner's real approval `rate_limited` for an hour | Fixed in v5.1 (§12, §11, §14, §15):  the `yes` class moves to Y8b, under `ops-control.lock`, after the ledger read (Y8a) and before the nonce append (Y8c);  a rate-limited approval keeps its nonce unspent;  the rate parameters and the lock order are unchanged, and the residual note says plainly that only the unsigned `plan` and `current` budgets can be used up by others.  |
| Independent review of BB's build:  outcome `unknown` for an unanswered Coolify GET, and a redeploy `auditTarget` that could break canonical bytes | Fixed in v5.1 (§13, §4):  `unknown` covers a GET sent without an observed answer;  the muster plan's `auditTarget` turns each code point outside `0x20` to `0x7E` into one `?`, after the 120-character cap, leaving the `--from-ssh` route unchanged.  |
| Independent review of BB's build:  the spec described `trust-list chain` as available before its slice is built | Fixed in v5.1 (§0, §4, §5, §12, §14, §15, §16):  `chain --plan` is `mode_not_allowed` and `chain --yes` is the existing `bad_command` at P2, as BB's final build answers (no new code), so the helper holds no list and every approval answers `no_trust_list`.  |
| BB's final build (head `ee55ce811`), request 2:  the ledger forgot a nonce 180 seconds after expiry, so a clock stepped forward and back could make an approval replayable | Fixed in v5.1 (§11, §16):  a line may go only when it is beyond the last 4,096 lines and its `expiresAt` is more than seven days before `now`;  a line whose `expiresAt` is after `now` stays;  the clock alone never makes a nonce forgettable;  the rewrite is `fsync`ed, renamed and its directory `fsync`ed;  a ledger over 2 MiB is `ledger_corrupt`;  the key is the `approvalNonce` alone.  §16's harness case for a pruned nonce now replays V1 long after its window (`expired` at Y6) and adds the stepped clock (`replayed_nonce`).  |
| BB's final build, request 3:  the `plan_refused` reasons | Fixed in v5.1 (§4, §15):  the planner's own `error_code` from a closed list, which gains `update_route_changed`, `receipt_context_mismatch` and `receipt_context_unavailable`;  else `allowlist_miss`, `report_stale` (every way the report can fail, named) or `revision_stale`.  |
| BB's final build, request 4:  a fault after Y8c was always `failed` | Fixed in v5.1 (§12, §13, §14, §16):  `unknown` when the action had begun or may have, `failed` only when it provably never began or ended in a definite failure;  the audit row's `result` is the outcome's word;  the `detail` is `plan_refused: <reason>`, `internal_error` or `the helper stopped unexpectedly while running`.  |
| BB's final build, request 5:  `chain --yes` until the chain slice lands | Fixed in v5.1 (§0, §4, §5, §12, §14, §15, §16):  `bad_command`, replacing the `not_built` an earlier draft of 5.1 named, so no new code is added.  |
| BB's final build, request 7:  when a refusal's `helperTrustListVersion` is `null` | Fixed in v5.1 (§13, §12):  it is the version this request actually read (Y2, R3 or K2) and nothing more, so a `plan_refused` at R2 carries `null`;  the acknowledgement with nothing held carries `finalRefusals: []`.  |
| BB's final build, request 9:  high-S signatures | Fixed in v5.1 (§6):  a high-S signature is accepted;  it cannot be used twice, because the nonce is spent;  the phone need not make low-S signatures.  |
| BB's final build, requests 1, 6 and 8 | Already in v5.1 (§12, §4):  the `yes` rate step at Y8a to Y8c (BB's single "Y8, in this order" says the same), the redeploy `auditTarget` rule, the rate clamp and the empty rate file.  |
| EE's Swift-side read of BB's helper PR 1505, relayed by BB:  §2 and §17B item 4 still named `JSONEncoder` with `.sortedKeys`, whose key order is not a byte sort, and Swift's own parser is not strict | Fixed in v5.2 (§2, §13, §17B):  a hand-written canonical encoder (keys sorted by byte value, so `B` before `a` and `a10` before `a9`), never `JSONEncoder` or `.sortedKeys`, and a strict parser that refuses duplicate keys, floating-point numbers, a boolean where a number belongs (or the reverse) and non-ASCII strings;  item 4 is settled.  |
| EE's Swift-side read of PR 1505:  copying the plan's `issuedAt` into the approval fails whenever Plan Review takes longer than 120 seconds | Fixed in v5.2 (§7, §13):  the approval's times are tied to no other time, since the helper compares them only with its own clock;  the phone uses its estimate of the server's clock (its own clock plus the offset seen at the plan answer) and sets `expiresAt` at most 120 seconds later.  |
| EE's Swift-side read of PR 1505:  the phone's retry list left out a verified `plan_mismatch`, the window refusals, and a verified `state_unsafe` or `internal_error` | Fixed in v5.2 (§15, §6):  What The Phone Does Next says a verified `plan_mismatch` has spent its nonce (plan again and sign again);  `expired`, `not_yet_valid` and `window_too_long` mean sign a new approval;  a verified `state_unsafe` (Y7, Y8a or Y8b) or `internal_error` (Y6 to Y8c, the nonce maybe on disk at Y8c) is treated as maybe spent.  |
| BB's build:  the ledger size limit | Fixed in v5.2 (§11, §16):  4 MiB, derived as twice the most a genuine ledger can hold, replacing 5.1's 2 MiB, whose margin was 3.4 percent.  |
| BB's build:  a failed close or prune after the append | Fixed in v5.2 (§12):  neither is a fault, because the append is durable and the prune is retried.  |
| Review of the chain slice's readiness for its builder (gaps G1 to G6), and §17A's A7 and A8 | Fixed in v5.3 (§8, §10, §11, §12, §14, §15, §16, §17A), as AA ruled:  check 3's `history_hash`, and `state_unsafe` for such a held list at C2;  the stored list is the newest entry's canonical bytes, so its SHA-256 is the `entrySHA256`, with the temporary file's flags, name and cleanup, and one link at read;  C4 compares the body hash and whether a list is held;  C5 writes, then appends its audit row, then answers, with its faults stated;  the chain audit row is named;  T9's retirement is noted;  rule A gains `server_keys_removed` (A8, vector T25), and A7 stays inside rule C.  |
| BB's build of the chain slice (lane `trust-chain`, not on `main` yet), BB's seven questions from it, and two independent reviews of that build | Fixed in v5.4 (§2, §5, §8, §11, §12, §13, §14, §15, §16), as AA ruled:  P3's strictness (uncompressed 91-byte keys, canonical base64url, real calendar times and the exact `contract`) is stated and moved into the reference checkers, with vectors T26 and T27;  a chain refusal's `claimed` and `chain` follow its step;  a signing fault after the rename that repeats is the unsigned `helper_key_missing`, and one that does not is the signed `internal_error`, the list held either way;  the chain audit row is best effort;  state files open with `O_NONBLOCK`, and only the read of the trust file accepts a link count of 0;  an open first list with no device is `closed_rule`;  C2's history mismatch carries a null version.  |
| BB's chain verb as merged in PR 1543 (`scripts/muster_approval.py` on `main`) | Written down in v5.5 (§5, §8, §12, §16), as the helper already does:  at C5, under `ops-control.lock` and before the temporary file is made, stale `.muster-trust.json.<16 lowercase hex>.tmp` regular files of the helper's account with one link are swept, every error ignored and nothing else touched;  at P3, an entry whose canonical bytes exceed the stored file's 65,536 bytes is `bad_input`, which no list within the other limits reaches.  |
| BB's chain verb on `main` (PR 1543, deployed to the server on November 8, 2026), and the passages that still called it unbuilt | Reworded in v5.6 (§0, §4, §5, §12, §13, §14, §15, §16), changing no rule:  each now says what the helper on `main` does;  §4 states once that only the owner's setup steps (the `/etc/muster` pin files, the helper key, and the `muster-exec` key with its forced command) keep the route inert, so a request started today answers the unsigned `helper_key_missing` at P0;  §16's harness case is the misspelt chain commands that are `bad_command`.  |
