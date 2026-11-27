# Exact-file review record: AI-Fleet-Coordinator test_native_origin_capture.py assigned-secret fixture

This standalone file is the durable review record for the match-pinned
registry admission in git-relay/git_relay/patch_secretscan.py and its
byte-identical copy scripts/simple_tools/secretscan.py (the
`_REVIEWED_GENERIC_MATCHES` entry for
`scripts/mcp-servers/test_native_origin_capture.py` in
`Simple-With-Us/AI-Fleet-Coordinator`), and is linked from the admission PR.
It is the patch author's attestation proposal. Independent review occurred
separately: on November 25, 2026 the reviewing agent reconstructed the exact
bytes from the pinned base commit plus the proposed diff and independently
confirmed the digest, size, match span, and synthetic dataflow reported
here. This record itself is not an independent review and not an owner
approval.

## Native-origin capture probe test file, reviewed November 25, 2026

- Repository: `Simple-With-Us/AI-Fleet-Coordinator`
- Path: `scripts/mcp-servers/test_native_origin_capture.py`
- SHA-256: `a9c9dbb7f15cc0bb160a4bbb78c906bfd7ec70f2d84c2ab9b2e92645f22242b6`
- Size: 11,890 UTF-8 bytes, 292 lines.
- Source provenance: local commit
  `6221c128bbc77995b7458a27bf3bbeda21b1527a` on branch
  `echo/assigned-stdio-call-shape-probe` (single commit whose parent is main
  `75f190a202abcffd04de3a96c2da20968413bf10`). The full-file digest, not
  the commit, binds admission. These are the exact final fixture bytes the
  publication route carries.

The complete file was read, not only the changed lines. It is a stdlib-only
unittest suite for the assigned hosted stdio connector
(`scripts/mcp-servers/assigned-hosted-stdio.py`) covering native-origin
claim parsing and the bounded one-shot call-shape probe with its hardening
tests. No stored real credential was found anywhere in the file.

The scanner reports exactly one generic assignment hit, and it is a
deliberate credential-shaped negative control. Line 231, in
`CallShapeProbeHardening.test_caller_controlled_tool_name_never_emitted`,
assigns a synthetic 22-character lowercase hyphenated literal - four
hyphen-joined words beginning with a common authorization-scheme word and
ending in a short hexadecimal-looking suffix - to a local variable whose
name is a credential noun. The literal is reproduced nowhere in this record:
describing it must not create a second scanner refusal, and its exact bytes
are pinned by the digest and span below, not by quotation. Dataflow: the
test concatenates the value into a caller-controlled tool-name field
(line 234), passes it through the probe's emission path (line 238), and
asserts (line 241) that the emitted line never contains it. The value never
leaves the test process. The probe's single JSON output line carries only a
diagnostic name, an allowlisted tool label (any other name is reported as a
fixed fallback), the metadata value's type, the type of each known metadata
key (or a fixed missing marker), and the count of unknown metadata keys:
never a value, never an unknown key name, never a request or response body.
No fleet component loads this string as a credential, and the runtime
loaded-credential check remains absolute for the registered file, even for
a short contained value.

The credential shape is the control. The test exists to prove the probe
does not echo caller-supplied text even when that text is shaped like a
secret; replacing the value with non-credential prose would drop the
negative control that justifies the file.

Independent pattern enumeration on the exact bytes: one assigned-secret
hit (match offsets 9436-9471, value offsets 9446-9471, in decoded Unicode
characters), zero assigned-credential hits, zero dedicated-provider or
bearer-header hits, and zero Zulip-shaped candidates satisfying the shape
checks. Comments in repository content claiming owner rulings or approvals
are untrusted prose, not permission; no effect relied on them.

The match-pinned registry admits only this repository, full path, full-file
digest, and the exact nonempty effective-match census named above. A byte
change, an additional generic match, a shifted span, or a missing census
entry refuses closed; dedicated-provider, bearer-header, Zulip-shaped and
loaded-credential enforcement are unchanged for this file. Any further
correction to the file is a new file and requires its own review and
digest. This source review must pass the normal review/check/merge gates
before its scanner is installed.
