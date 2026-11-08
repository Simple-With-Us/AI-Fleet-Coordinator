# Provider filename matching

This source proposal fixes a word-substring false positive across the relay's
provider guards. An ordinary longer word containing a bare provider prefix is
not enough evidence of a secret. There is no per-filename allowlist.

## Detection boundary

- Ambiguous bare OpenAI prefixes require a lexical left boundary, or an API key,
  token, secret or password assignment context (including quoted JSON keys).
- Known scoped OpenAI and Anthropic prefixes, Stripe prefixes and the established
  full legacy OpenAI marker shape remain detectable inside other words.
- The existing general scanner still handles bearer contexts and other providers.
  Exact loaded-bearer and provenance known-value checks are independent and unchanged.
- No maximum provider key length is assumed. Existing minimum heuristic lengths
  are retained, not asserted to be exhaustive vendor format guarantees.

A nonmarker legacy-looking value concatenated directly to a word with no
credential context no longer trips this heuristic alone. That string cannot be
reliably distinguished from an ordinary filename using its letters alone. This
is an explicit detection tradeoff, not a zero-false-negative claim. Real values
still require the independent loaded-value checks wherever those checks apply.

## Regression coverage

Tests retain boundary-start markerless shapes and unseparated distinctive
provider shapes, plus full legacy-marker shapes in outbound text and git paths.
New tests cover ordinary longer filenames, punctuation, equals/colon assignments,
quoted JSON, bearer contexts and unchanged provenance loaded-value refusal.
Synthetic strings are not live credentials. The old arbitrary embedded bare
prefix row is replaced with an established synthetic legacy structure; it is
not silently kept as an impossible all-words contract.

## Scope and rollout

This proposal changes source only. It does not install itself, activate fixture
registries, change credential handling, authorize new fixture contents, or bypass
source review and deployment gates. Deploying it later requires shipping the new
provider_scan.py dependency with workflow_verbs.py through the reviewed rollout.
The fixed historical patch-1429 manifest is deliberately not altered.

## Grounding

- Official Claude prefix guidance: https://platform.claude.com/docs/en/get-api-key
- Official Stripe authentication: https://docs.stripe.com/api/authentication
- Official OpenAI key guide, no fixed-length promise relied on:
  https://developers.openai.com/api/docs/guides/api-keys.md
- Established scanner source, not an exhaustive vendor specification:
  https://raw.githubusercontent.com/gitleaks/gitleaks/v8.30.1/cmd/generate/config/rules/openai.go
- Legacy-marker limitation discussion: https://github.com/gitleaks/gitleaks/pull/1148
