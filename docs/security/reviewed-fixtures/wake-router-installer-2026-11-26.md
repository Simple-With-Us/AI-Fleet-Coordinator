# Exact-file review record: the wake router installer assigned-secret fixture

This standalone file is the durable review record for the match-pinned
registry admission in git-relay/git_relay/patch_secretscan.py and its
byte-identical copy scripts/simple_tools/secretscan.py (the
`_REVIEWED_GENERIC_MATCHES` entry for
`scripts/simple_tools/server/install-wake-router.sh` in
`Simple-With-Us/AI-Fleet-Coordinator`), and is linked from the admission
pull request.  It is the source owner's attestation proposal;  it is not an
independent review and not an owner approval.

## Wake router installer, reviewed November 26, 2026

- Repository: `Simple-With-Us/AI-Fleet-Coordinator`
- Path: `scripts/simple_tools/server/install-wake-router.sh`
- SHA-256: `80c5933a23534b5b9ed0030dafcfcb90b0e21a6fd25f94c72b16734edae6bad6`
- Size: 48,068 UTF-8 bytes, 975 lines.
- Source provenance: main commit
  `1b8d1ca008443c59c75e577968e5bda046582469`.  The full-file digest, not
  the commit, binds admission.

The complete file was read, not only the changed lines.  It is a bash
wrapper whose body is an embedded Python program, and that program carries a
second embedded Python program as a string constant.  Both were read
through.  The file installs the one shared wake router:  it stages the
read-only Infisical service token, checks the router's knobs with a child
process, writes the release tree and the systemd units, and refuses a
running program that is not its own release entry point.  No stored
credential was found anywhere in it.

The scanner reports exactly two generic assignment hits, and the review
enumeration found the same two and nothing else.  Line 109 binds the
module-level constant that names the Infisical environment variable the
installer reads;  it names a credential slot, it does not hold one, and the
value is a 27-character uppercase-underscore environment variable name.
Line 130 binds the local name that carries the credential inside the
embedded knob-check program to the result of a call that reads one line
from standard input;  it receives a computed value, and the captured text is
the dotted expression of that call, not a secret.  Independent pattern
enumeration found no assigned-credential hits, no dedicated-provider hits,
no bearer-header hits and no Zulip-shaped key hits.  The other
credential-noun bindings in the file receive None or a call result.

The credential itself never lives in this file.  It is read at run time
from a 0600 file owned by the caller, passed to the child process on
standard input, placed in that process's environment and written to a 0600
environment file.  That is runtime handling, not material stored in the
source.

The registry admits only this repository, full path and exact full-file
digest for the generic-assignment categories, with the exact match census
pinned.  A changed byte, another match or a removed match refuses;  the
loaded-credential check and every dedicated shape stay absolute for the
registered file.  This source review must pass the normal review, check
and merge gates before any installed scanner changes.  Do not patch a live
registry to unblock publication.
