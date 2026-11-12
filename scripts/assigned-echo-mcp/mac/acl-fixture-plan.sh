#!/bin/bash
# PRINT-ONLY plan until explicit source review + host owner storage/signing permission.
# No commands printed below are executed by this file.
set -euo pipefail
cat <<'PLAN'
Gate: host owner chooses an isolated disposable NONSECRET test Keychain, never login.keychain.
Compile acl-fixture.c into TWO different fixed test executable paths.
Sign each with DISTINCT identifiers using existing approved Developer ID; do not trust a generic interpreter.
Create/unlock isolated test Keychain with a NONSECRET disposable fixture password via private stdin/native API, never production custody.
FIRST binary: create ISOLATED_KEYCHAIN FIRST_BINARY_PATH
FIRST binary: read ISOLATED_KEYCHAIN FIRST_BINARY_PATH -> must exit0/status0.
SECOND binary: read ISOLATED_KEYCHAIN FIRST_BINARY_PATH -> must fail noninteractively/status-denied.
Use same fixture to verify /usr/bin/security and generic interpreter deny noninteractively; no allow-all/-A/partition-list widening.
Inspect resulting ACL explicitly, preserving only expected fixed trusted binary identity.
Lock test Keychain and verify first binary fails, then delete only the isolated fixture Keychain/test binaries.
Do not continue to own ECHO ingress unless signed positive AND different-binary denial AND lock/refusal checks actually pass.
No production service/account, bearer or Keychain is referenced by the fixture binary.
PLAN
