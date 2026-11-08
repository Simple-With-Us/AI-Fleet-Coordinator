# The Trust List Chain Verb: The Piece That Lets The Muster iPhone Route Hold A List

The Muster iPhone route (`fleet-ops-control.py --from-muster`, `scripts/muster_approval.py`, merged and inert) answers every approval `no_trust_list`, because nothing writes `/home/jay/.local/state/fleet-tools/muster-trust.json`.  This change adds the request that does, `muster trust-list chain --yes`, as `docs/muster-ios/server-route-spec.md` version 5.3 describes it (sections 5, 8, 12, 13 and 14, steps P3 and C1 to C5).  The sender splits a long history into consecutive requests of at most 32 lists, each starting at the version the helper holds plus one, and the helper keeps only the newest list it has verified.

Nothing here is deployed.  This change did not read or touch `/etc/muster`, the live state directory, any key, any service or the installed files, so every statement about the live server below comes from the earlier rollout notes or from the code and is marked as not checked now.

## 1. What The Verb Does

The command is `muster trust-list chain --yes`.  Its `--plan` form stays `mode_not_allowed`, there is no device approval and the clock is not checked.  Standard input is exactly `{"protocol":"muster-helper/1","chain":[ ... ]}`, read up to 65,536 bytes (4,096 for every other request), with 1 to 32 entries.  Each entry is a signed list with exactly `body`, `ownerSignature` and `countersignatures`.

| Step | What happens | If it fails |
| --- | --- | --- |
| P1 | `/etc/muster` is read once (the pin, the owner history and the reopen record). | `pin_unsafe` |
| P3 | The document is parsed strictly (UTF-8, one JSON object, no duplicate or unknown key, no float) and every field has its type and pattern.  Every key must be a P-256 key.  At most 64 devices and 64 countersignatures.  A list whose canonical bytes would exceed the stored file's limit is refused too. | `input_too_large` or `bad_input`, before the rate step, so these are never counted |
| C1 | The `chain` rate class, key `chain`: one request per 10 seconds and 30 an hour, under the rate lock alone. | `rate_limited` |
| C2 | The stored list is read without a lock.  If its version is the `toVersion` of a history line, its body hash must be that line's `toSHA256`. | `state_unsafe` |
| C3 | Every entry is checked against the list before it, in order, and the first failure ends the walk (all or none). | `chain_refused` with a `reason` |
| C4 | `ops-control.lock` is taken and the stored list is read again.  It must be the list C2 read (the same body hash, and a list held exactly when C2 found one). | `action_busy`, `trust_list_changed` or `state_unsafe` |
| C5 | Under the lock: temporary files that a crashed write left are swept, the newest entry is written durably, then one audit row is appended, then the signed acknowledgement is answered. | `internal_error` |

The checks of C3 are section 8's, for each entry: 1 the version is 1 when nothing is held and otherwise the previous plus one (`version_gap`); 2 `previousSHA256` is null when nothing is held and otherwise the previous body hash (`first_rule`, `previous_hash`); 3 `ownerKey` is the key the owner history assigns to that version, and when the version is the `toVersion` of a history line the body hash is that line's `toSHA256` (`owner_key`, `history_hash`); 4 the owner signature verifies with the key the history assigns, never the entry's own `ownerKey` (`owner_signature`); 5 no two devices share an id (`duplicate_device`); 6 no two countersignatures name one device (`countersignature`); 7 `closed` is true exactly when there is no device (`closed_rule`); 8 the one rule that applies: First (nothing held), A (a normal change, countersigned by devices of the previous list, keeping a server key if the previous list had one) or B (a close).  Rule C (a reopen) is refused `reopen_unavailable` once checks 1 to 7 have passed.

What is stored is the canonical bytes of the newest accepted entry, the whole signed object, with no trailing line feed, so the SHA-256 of `muster-trust.json` is that entry's `entrySHA256`.  It is written through a temporary file in the same directory (`O_WRONLY|O_CREAT|O_EXCL|O_NOFOLLOW|O_CLOEXEC`, mode 0600, a random name beginning `.muster-trust.json.`), synced, renamed and the directory synced, and the temporary file is removed on any fault before the rename (not only an I/O error).  A kill leaves it behind, so the next chain, under the lock and before it makes its own, removes every file named exactly `.muster-trust.json.` plus 16 lowercase hex characters plus `.tmp` that is a regular file of the account with one link, and nothing else (a link is never followed, and any error is ignored).  The stored list is read without a lock, so a reader may open the old file just before a chain renames over it: that reader sees the old file with no link left, and the read of the stored list accepts that (it is still the whole old list).  Nothing else does:  the helper key, the ledger, the rate file and the lock keep exactly one link, and a handle that can write is refused with none whoever asks.  A reader accepts any formatting, and holds the file to exactly one link, 1 to 65,536 bytes and all of section 8's list-local checks (a chain never stores a list that fails them).

The acknowledgement answers for the list just written (`helperTrustListVersion`, `trustListSHA256` the body hash, `closed`, `signedByCurrentKey`, `finalRefusals` always empty).  A `chain_refused` answer names the refused entry as `{"version", "entrySHA256", "final": false}`, carries the version C2 read (null when nothing is held), and writes no audit row.  The audit row of an accepted chain has verb `muster trust-list`, target `chain`, result `accepted`, and the fields `route`, `via`, `trustListVersion`, `trustListSHA256` and `fromVersion`.

## 2. What It Does Not Do

- **Rule C (a reopen) is not built.**  Every rule C entry is refused `chain_refused` with reason `reopen_unavailable`, final false, after checks 1 to 7 pass.  Nothing is recorded as final and `finalRefusals` is always `[]` (section 17A is open).
- **No rotation.**  `muster-agent repin` and `trust-status` are not in this repository.  A chain accepted under a rotated history says `signedByCurrentKey: false` until a list signed by the new key arrives, which is the proof section 10 asks the phone to wait for.
- **No device-id binding.**  Nothing yet ties a device entry's id to its own approval key (section 17A, A7).  The spec puts that fix in rule C only.
- **No sender.**  `muster-exec` and `muster-pub` are not in this repository.

## 3. How An Installed Route Becomes Live

The route needs all of the following before the first chain can be delivered.  The state of each on the live server was not read for this change.  The Nov 8 rollout note `2026-11-08-helper-unit-status.md` recorded `/etc/muster` and `/usr/local/lib/muster-agent` as absent and the installed helper answering the unsigned `helper_key_missing`.  Check each again before relying on it.

| # | What | Who | Repository state |
| --- | --- | --- | --- |
| 1 | The two changed files go to `/usr/local/lib/fleet-resource-control` (section 4 below). | Whoever deploys the helper | This change |
| 2 | `/etc/muster/owner.pub` (one line, the base64url SPKI of the owner key, then a line feed) and `/etc/muster/owner-history.jsonl` (one canonical line `{"fromVersion":1,"ownerKey":"...","toSHA256":null,"toVersion":null}` and a line feed), root-owned, not writable by group or others, in a root-owned directory. Until they exist every request is `pin_unsafe`. | The owner, as root at the console, comparing the full fingerprint with Muster's | `muster-agent install` (the tool section 10 names) is not in this repository, so the files would be made by hand |
| 3 | The helper key `/home/jay/.local/state/fleet-tools/muster-helper.key`, an unencrypted PKCS#8 PEM P-256 key, mode 0600, in a state directory with no group or other bits.  Without it every request answers the unsigned `helper_key_missing`.  Its public half is `services.serverHelper` of the lists the owner signs. | The owner (section 13 says the Milestone 5 install makes it and prints its fingerprint) | The installer does not make it |
| 4 | The `muster-exec` key and its line in `/home/jay/.ssh/authorized_keys`: `restrict,from="127.0.0.1,::1",command="/usr/bin/python3 -I /usr/local/lib/fleet-resource-control/fleet-ops-control.py --from-muster" ssh-ed25519 AAAA... muster-exec@server` (section 3).  The earlier slice's note asked for `env -i` with `SSH_ORIGINAL_COMMAND` passed through by name; the spec does not say so yet, and neither form was run under a real sshd. | The owner | `muster-exec` is not in this repository |
| 5 | The first chain: version 1 under rule First, signed by the pinned owner key, with at least one device and the helper key as `serverHelper`.  After it, an approval by a device of that list is no longer `no_trust_list`. | Muster, through `muster-exec` | This change accepts it |

Do not send a chain to the live server as a test.  The first chain is the owner's own list, and the route is live from then on.

**Do NOT send a close (a list with no device and `closed` true) to the live server.**  Rule C (reopen) is not built, so a close is a one-way door: after it only more closes are accepted (every reopen is `reopen_unavailable`), and a closed list refuses every approval (`unknown_device`).

## 4. Deploying The Two Files

Two installed files change, `fleet-ops-control.py` (three new audit field names and docstrings) and `muster_approval.py` (the verb).  `muster_trust_files.py`, the allowlist and every other file are byte-for-byte what `main` had.  Read the installed hashes first and replace nothing if they differ from the files the last deploy wrote (this change did not read them).  Take a hash-verified backup of each, write each new file beside its target as a candidate, check its sha256 against the repository file and rename it into place, the way `2026-11-08-helper-unit-status.md` did.

**Helper first, module second.**  Every state between the two renames works, and the order decides which one is harmless:

- New helper with the old module: `chain --yes` is still `bad_command`, as today.  The helper only knows three more audit names.
- Old helper with the new module: a chain is accepted and stored, but the helper's audit function drops the three fields it does not know, so the row says `accepted` without the version and hash.  Nothing is lost on disk, but the row is poorer, so the helper goes first.

Rollback restores the module first (the verb is off at once), then the helper.  A list that a chain has stored stays stored, and the old module reads it as it reads any list, so approvals keep working against it.

## 5. Checks

- `python3 scripts/test_muster_approval.py`: 523 tests (389 before).  The chain tests drive every vector of section 16 from the fixture's `frozenExpected` through the module, the helper in process and the helper as a real process (all 24 rows, the rule C rows as `reopen_unavailable`), the T2 and T3 acknowledgements and the T7 refusal byte for byte, the stored bytes and their hash, hostile input, the rate limits, the stored list in every unsafe state, an unlocked reader against the chain's own writer, the race between C2 and C4, a fault at every step of C5, a kill -9 at every step of the write and the sweep of what it leaves, a trickle on standard input, and chains at once.
- The other suites stay green: `test_fleet_ops_control.py` (271 tests), `test_fleet_resource_control.py` (44), `test_fleet_cli_release.py` (78), `test_fleet_skill_identity.py` (106), the installer test at umask 022 (13), `(cd docs/muster-ios && python3 -I check_vectors_v5.py)` (0 failures) and `python3 scripts/make_muster_vectors.py --check` (the fixture is what the generator makes).  The fixture needed no change.
- Mutation check: 167 edits of the new code and of the helper's audit names, made in scratch copies and never in the lane, each caught (a run stops at its first failing test), and one control edit that changes nothing, which left the suite green.  They include the survivors of the two independent reviews (the owner signature and the countersignature under the wrong prefix, check 7 in one direction, an old version naming the current pin, the integer text limit, C4 by version, the stdin deadline per read, cleanup on OSError only, a FIFO opened blocking) and each way of loosening the unlocked-reader and sweep rules.  The tests that only assert a constant of the module were left out of the run, so that a changed limit has to be caught by behaviour.  The edits that need real processes (the action lock not taken, the second read of the list dropped, the audit row before the write, the list written in place) were also run against the multi-process tests alone and caught there.  The real race test fails on the old link check (about 250 reads in 4,700) and passes on the fix.
- After deployment, read-only: with the pin and the key in place, `muster trust-list current --plan` answers a signed acknowledgement with `helperTrustListVersion` null before the first chain, and with the first list's version and body hash after it.  `sha256sum` of `muster-trust.json` equals the first list's entry hash, which Muster prints.

## 6. Where The Build Is Stricter Than The Reference Checker

`docs/muster-ios/check_vectors_v5.py` and `make_vectors_v5.py` accept some lists that the helper refuses.  Each of these is a designed, fail-closed reading of the spec, found by differential testing against both checkers (about 168,000 cases, no divergence on a well-formed list).  The risk is liveness, not safety: if the owner side ever signs a list that one of them refuses, the helper refuses it for good, and every later list hashes over it.  Each needs one spec sentence, a vector and a Swift-side test, or the helper should be loosened to the reference.

- **Keys are P-256 and nothing else, as the 91-byte uncompressed SPKI.**  A compressed or hybrid point, another curve or any other length is `bad_input` at P3.  Section 8 says "base64url, 1 to 200 characters"; section 2 says P-256.
- **Base64url is canonical.**  A key with stray trailing bits is `bad_input`; in a signature field it is a signature that does not verify (`owner_signature` or `countersignature`).
- **`issuedAt` is a real calendar time.**  `:60` seconds, `02-30` and year `0000` are `bad_input`.
- **`body.contract` equals `muster-trust-list/1`**, or the list is `bad_input`.
- **The stored-list reader enforces checks 5, 6 and 7.**  A stored file with two devices of one id, two countersignatures naming one device, or `closed` against `devices` is `state_unsafe` (section 11 says only "§5's rules").  A chain never stores such a list, and a hand-repaired file must pass them too.
- **An entry whose canonical form exceeds 65,536 bytes is `bad_input`.**  Defence in depth: a maximal valid entry is about 55 KB, so no list that passes the other checks reaches it today.

## 7. Interpretations And Open Items For The Spec Owner

The spec is silent or ambiguous on these, and the build chose as follows.  None is a defect that the reviews found; each wants a spec sentence.

1. **The refusal shape of the chain command.**  Section 13's chain row has no `claimed`.  A refusal of the chain command after the command line has parsed (P3 on) carries `chain` (null unless C3 refused an entry) and no `claimed`.  A refusal before that (`pin_unsafe`, `bad_command`, `mode_not_allowed`) is the generic shape with `claimed` empty and no `chain`.
2. **A held list that fails the history hash at C2** is `state_unsafe` with a null `helperTrustListVersion`, because the helper cannot vouch for what it read.  (Reporting the version it did read would also fit section 13, and would let a sender spot a fork.)
3. **A chain that does not ascend** is `version_gap` at C3 (check 1), not `bad_input` at P3.
4. **No audit row when only the directory sync fails**, or when the process dies between the rename and the audit append.  The list is held, `trust-list current --plan` is authoritative, and the audit tail is best effort: a row implies that the list was durable, not the reverse.  (A row for a list that may not be durable would be wrong.)
5. **A signing fault after the rename that repeats** gives the unsigned `helper_key_missing`, because the refusal that would say `internal_error` is signed with the same key.  A fault that happens once gives a signed `internal_error`.  In both the list is held and its row is written.
6. **The forced command.**  Section 3 still prints the plain form of the `command=` line.  The earlier slice asked for `env -i` with `SSH_ORIGINAL_COMMAND` passed through by name; neither form was run under a real sshd.
7. **The strictness in section 6 above.**
8. **The link count of the stored list (a change to section 11 of 5.3, found by the review and by Kody on this PR).**  Section 11 says every state file has exactly one link.  A reader that holds no lock can open `muster-trust.json` just before a chain renames over it and then see its old inode with no link left (about 5 percent of reads in a race test), which is no fault because the file only ever changes by rename.  The build therefore accepts a link count of 0 or 1 for the read of the stored list and for nothing else:  the helper key, the ledger, the rate file and the lock still need exactly one, and a handle that can write needs one whatever the caller asks.  Spec 5.4 (AA, PR 1542) states the rule;  it should say "the trust file" and not "the trust file and the helper key".

Not interpretations, because the spec says them: a concurrent chain can meet `action_busy`, `trust_list_changed` or `version_gap` (section 12, C4; section 15), and C4 compares the body hash only, so a stored list re-signed over the same body is not a change.  Also by the design of section 12: C1 spends one of the 30 hourly chain slots before the chain is judged, and C2 does not check the held list's owner signature (a chain can repair a held list whose signature is broken, which is safe because the new entry is owner-signed over `previousSHA256`).

## Deployed

Deployed by hand on November 8, 2026 at 10:06 AM from main `0c83f9592` (PR 1543), the helper first and the module second, as section 4 says.  Each file was replaced through a candidate beside it, hash-checked against the repository file, and renamed into place.

| File | Before | After |
| --- | --- | --- |
| `fleet-ops-control.py` | `ead2d0927153…` | `bf83a9e2be38…` |
| `muster_approval.py` | `fbe9db7d437c…` | `fbd212aec173…` |

Both old files were backed up first, each hash-checked against the installed file it copied, in the server's backup folder `fleet-ops-control-trust-chain-20261108T160614Z` (root, mode 0700).  Rollback restores the module first and then the helper from that folder, each through a hash-checked candidate.  No other installed file changed, and the allowlist, `fleet_resource_control.py` and `muster_trust_files.py` were byte for byte main's before and after.

Read after the swap:

- The installed module imports on its own and has the chain parse and the temporary-file sweep.
- `--from-muster` on the server still answers the fixed unsigned `helper_key_missing`, as it must:  there is no helper key and no `/etc/muster` yet, so the route is inert.
- Through the Mac route, `unit-status`, `logs`, `eavesdropper-status` (29 seats running) and `resource-status` still answer.

Nothing was sent to the chain verb on the live server.  The route goes live only when the owner's steps in section 3 exist and the owner delivers a first chain.
