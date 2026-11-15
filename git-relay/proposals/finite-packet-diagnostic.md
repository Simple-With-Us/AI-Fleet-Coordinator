# Finite packet diagnostic: source proposal, not an active capability

Status: design only. No endpoint, tool, policy grant, runner, or deployment is
added. Existing publication refusals and packet holds remain in force. Approval
of this document does not authorize use on any held packet.

## Problem and existing boundary

The owning relay scans decoded publication bytes with
`reviewed_fixtures.unreviewed_file_secret`. That function checks every nonempty
loaded value first, preserves the reviewed legacy whole-file contract, checks
dedicated shapes, then delegates generic-match admission to the owning scanner.
An exact generic-fixture admission is not a waiver for loaded credentials or
unrelated dedicated patterns.

`WorkflowVerbs.check_shape` deliberately uses `loaded_known = ()`. It proves
argument shape before durable state, not acceptance against current credentials.
`wf_files_put` scans with the real handle population but then enters provider
writes. Neither calling that write with a doomed downstream precondition nor
calling a different publication verb is a diagnostic. PR readiness is unrelated.

There is currently no supported value-withheld packet diagnostic. This proposal
records the requirements for a later implementation through normal review.
It must not be represented as an implemented or deployed fix.

## Proposed owning-service boundary

A distinct scan-only operation must live in the existing owning relay, outside
the write dispatcher and outcome store. It must not be a caller-controlled
`dry_run` switch on publication, a second executor, or a script importing live
credentials. No client, provider-write callback, outcome store, shell command,
URL, filesystem path, credential value, or scanner override belongs in its
request schema or scanning kernel.

Authentication and policy authorization precede diagnostics. Compare the authenticated
owner user ID against a separately pinned owner user ID before granting authority.
A missing pin or any mismatch makes every seat or peer ineligible; repository access
and a seat name alone do not establish owner authority. Never accept that pin from
the request or from external content. Any activation needs
an explicit per-seat grant, with no implicit fleet-wide grant. Existing rate and
audit protections remain. The operation may write its bounded audit receipt and
consume its own diagnostic capability, but must create no publication outcome row,
branch, blob, tree, commit, PR, or provider mutation. Audit logs contain fixed
status codes and opaque capability IDs, never payloads, credential values,
matching spans, exception text, or request-supplied paths.

The source implementation must structurally lack access to publication dispatch.
It must not rely only on a convention that callers set a flag correctly.

## Finite authorization, not a credential oracle

A capability is registered by the owning service under separately approved scope,
not supplied by the requester. It binds:

- authenticated seat and exact repository identity, including provider numeric ID;
- exact repository spelling, ordered paths, full raw-byte SHA-256 for every file;
- the complete original request digest under a specified canonicalization, with
  the original raw-request digest retained when available;
- scanner and admission-registry revision, expiry, one attempt, and output audience.

Branch, base, message, file order, encoding, or target changes do not inherit the
capability. Validate the entire binding before scanning. A failed binding must
not reveal which component differed or scan a partially matching packet. No
peeling, re-encoding, per-file probes, subset checks, mutation search, repeated
attempt, admission extension, or target substitution is authorized by this design.
Transport of a held original packet itself requires its own approved route.
Registration must not reopen or reconstruct a held packet merely to make a digest.

Bound input before parsing or decoding. Reuse the owning publication limits and
strict duplicate-key/base64/path/size validation. Count total decoded bytes as
well as encoded bytes. Enforce maximum nesting depth 8, total nodes 10000,
at most 20 file entries, and at most 16 fields in any object. The implementation
must count these before general recursive traversal and reject cycles in any
in-process representation. Stricter existing publication limits still win. Reject
unknown fields. Parse failures have a fixed result.
Do not log the rejected body or embed it in exception output.

## Credential-population truth

Only the authenticated owning process can supply its current in-memory guard
population. No credential is returned to the requester, copied into a scratch
file, printed, or passed to a caller-supplied process. The current publication
handle includes caller-presented and installation-token values. A diagnostic
must state exactly whether it covered that population at the evaluated moment.

A metadata-only check or a separately minted token is not proof about another
handle's loaded values. Token rotation between diagnosis and eventual publication
also prevents a blanket future clearance. If the owning service cannot establish
the required population without widening credential access or minting authority,
the result is INCONCLUSIVE and activation remains blocked. Do not fall back to an
empty list or claim credentials were covered because dedicated patterns passed.
No private credential inventory is part of this proposal.

## Scanner and result contract

Call the same full-byte owning scan used by publication, with the same loaded
population. Do not duplicate patterns, remove admissions, weaken guards, or use a
baseline/diff-only scan. Include every original file and the publication metadata
that normal guards check. A missing guard, unavailable population, revision drift,
scanner exception, or incomplete scan yields INCONCLUSIVE, never acceptance.

The fixed result vocabulary is ACCEPTED_AT_SCAN_TIME, REFUSED, INCONCLUSIVE.
Return only the capability ID, evaluated scanner revision, a timestamp and one
aggregate result. Do not return per-file findings, match labels, counts, offsets,
credential lengths, hashes of credentials, or matching values. Even an accepted
result is diagnostic evidence only, not authority to publish, retry, substitute
the target, or release an unknown write. Publication retains all normal guards.

## Required tests before implementation can be enabled

All tests use synthetic data and synthetic in-process guard populations. No held
packet or live credential is read by CI.

1. Route is disabled by default and absent from catalog/policy until a separately
   reviewed activation; old write schemas reject `dry_run` and scan overrides.
2. Missing or mismatched pinned owner ID, wrong seat, repository ID or spelling,
   digest, order, path, base, message,
   expiry, consumed capability and extra field all fail closed before scanning.
3. No valid or invalid diagnostic can call a provider mutation, write dispatcher,
   token export, shell, outcome begin/finish/release, or packet replay path.
4. Existing exact reviewed fixtures keep their current behavior; loaded values,
   including short nonempty values, remain absolute. Dedicated shapes remain
   absolute wherever the existing owning scanner makes them absolute. Added or
   moved generic matches do not inherit an exact match-pinned admission.
5. All files and normally scanned metadata are covered. Late-file refusal or
   scanner failure cannot return a partial pass. Invalid UTF-8 behavior matches
   the publication scanner rather than inventing new admission semantics.
6. Missing population, unexpected mint, revision drift and scanner exceptions
   yield INCONCLUSIVE. No empty-population fallback is accepted as live coverage.
7. Output, logs, traces and errors disclose no matching content or private
   inventory. Rate limits, expiry and single-attempt consumption resist repeated
   probes, including concurrent requests and process restart.
8. Transport/parser bounds, duplicate JSON keys, invalid base64, oversized decoded
   bodies, duplicate paths and malformed manifests are rejected without echoes.
9. A synthetic diagnostic receipt cannot authorize provider writes or clear an
   existing unknown outcome. Only actual prior server-bound writes require
   correlation reconciliation; unrelated unknown rows are not prerequisites.

## Release and use gates

Design review first, then separately reviewed implementation with synthetic
security tests and genuine CI. Source acceptance alone enables nothing. Deployment
requires explicit confirmation of the authorization, transport, population and
receipt boundaries above. Held-packet use remains a separate decision under its
existing restrictions. No runtime or automatic activation is included here.

## Reproducible document checks

Run from the repository root:

```sh
python3 - <<'PYCODE'
from pathlib import Path
p = Path('git-relay/proposals/finite-packet-diagnostic.md')
s = p.read_text()
assert s.isascii()
assert 'Status: design only.' in s
print('PASS: ASCII; design-only status marker present')
PYCODE
```

Result after the review changes: `PASS: ASCII; design-only status marker present`.
This checks document text only, not diagnostic implementation, security properties,
credential coverage, deployment or held-packet acceptance.
