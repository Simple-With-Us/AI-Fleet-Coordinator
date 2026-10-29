# Unattached Git tree and commit creation

Two default-off own-App contents:write commands create unattached objects, never
update refs.  Tree creation requires a base tree and1-20 regular text files with
40KiB cumulative content, no workflow paths, symlink/submodule mode, duplicate or
prefix-overlapping paths, raw blob SHA or deletion.  Text is secret-scanned.
Commit creation takes exact tree and1-2 distinct parent SHAs, bounded nonblank
message; no root commit, caller author/committer/signature or URL input.
Strict receipts and durable UNKNOWN/no-replay remain.  App/repo/action allowlists
apply, no configured grants and no live object created for this source test.

This does not prove arbitrary existing tree/parent object contents safe.  Any later
branch/ref mutation still needs its own execution authority and GitHub permissions.
Raw tree modes, deletes, binary object writes and root/octopus commits remain
unimplemented.  Object existence is enforced by GitHub, not by a new live proof loop.
Schema: https://raw.githubusercontent.com/github/rest-api-description/main/descriptions/api.github.com/api.github.com.json
