# Unattached Git tree and commit creation

Two default-off own-App contents:write commands create unattached objects, never
update refs.  Tree creation requires a base tree and1-20 regular text files with
40KiB cumulative content, no workflow paths, symlink/submodule mode, duplicate or
prefix-overlapping paths, raw blob SHA or deletion.  Text is secret-scanned.

Opt-in larger-text class: allow_large_text exactly true raises blob text and tree
cumulative content bounds to 100000 UTF-8 bytes; false or absent selects the base
caps, and any other value is refused.  The HTTP envelope allowance for these two
actions is 256KiB of SERIALIZED request bytes, so heavily escaped text near the
decoded cap can exceed the allowance and be refused: the class raises the decoded
bounds, it does not guarantee arbitrary 100000-byte transport.  The secret-scan
budget covers the full admitted body, including a marker at its very end.
Commit creation takes exact tree and1-2 distinct parent SHAs, bounded nonblank
message; no root commit, caller author/committer/signature or URL input.
Strict receipts and durable UNKNOWN/no-replay remain.  App/repo/action allowlists
apply, no configured grants and no live object created for this source test.

This does not prove arbitrary existing tree/parent object contents safe.  Any later
branch/ref mutation still needs its own execution authority and GitHub permissions.
Raw tree modes, deletes, binary object writes and root/octopus commits remain
unimplemented.  Object existence is enforced by GitHub, not by a new live proof loop.
Schema: https://raw.githubusercontent.com/github/rest-api-description/main/descriptions/api.github.com/api.github.com.json
