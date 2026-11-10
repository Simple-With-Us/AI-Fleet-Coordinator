"""Reviewed, exact-content attestations for outbound security-test fixtures.

This is trusted service code, not seat-supplied configuration. Each entry needs
normal code review of the full fixture, provenance and intended destination.
No prefix/path exemption, caller override, baseline inference or diff-only scan.
"""

from hashlib import sha256

from .secret_scan import find_secret

# Source of truth: the corrected rename checkpoint
# 81d8bb0cd4f5a63d3dc330b8f42be4a3e93e87a3 (tree
# b548e1a81d4fd18d72206f8c1f22cb41a6d19bcf, built on main
# ac6d5d44500937274aeddfc794a05fb427158f8c); all 13 triples verify byte-exact
# against that tree. The first checkpoint
# 4977e6a7970dc0a25638181600137fe06167dcec is retained as original evidence
# only, never as a shipping target. Ten rows are unchanged from that first
# re-attestation; three rows (test_install_mcp.py, integration.workerd.mjs,
# test_infisical_api_route.py) are refreshed from the corrected checkpoint.
# The
# rename-affected destinations are future targets: absent on main, matching
# nothing and failing closed until the exact attested bytes land.
# Only these exact bytes at these exact repository destinations are attested
# synthetic fixtures.
# The source Git blob ids and review procedure are recorded in the README.
# The Infisical API-route row is separately refreshed for the current rename
# reconciliation tree550beb61fb7cd372b3504d3b4f023130d7d730ec. Its immutable
# main27801add source, five safe rename/import/env edits and whole credential-
# line origin proof are recorded in preservation-pending-manifest.json. All
# credential-shaped spans are unchanged; provider/loaded guards are NOT waived.
_REVIEWED_FIXTURES = frozenset(
    {
        (
            "Simple-With-Us/AI-Fleet-Coordinator",
            "scripts/fleet_lanes/tests/test_install_mcp.py",
            "a0b944f012a3fa8c27dfe52cab1c53ef8893ec4f945e6b62eb6c297f42e8853b",
        ),
        (
            "Simple-With-Us/AI-Fleet-Coordinator",
            "scripts/simple-tools-mcp/DEPLOY-PHASE0.md",
            "5f85629a6a6979a763063ad3b79fd56b5d77ab93d2cd0e6da4c200485debca3f",
        ),
        (
            "Simple-With-Us/AI-Fleet-Coordinator",
            "scripts/simple-tools-mcp/test/gr-assert.test.mjs",
            "ecfa713b33754e1331a8fee0e07c67271fc186908a52830839f1b530a246dad8",
        ),
        (
            "Simple-With-Us/AI-Fleet-Coordinator",
            "scripts/simple-tools-mcp/test/gr-front.test.mjs",
            "9de759b53f8cb987b16ade0a9cf99ff69d999659519a620b292f4aa748eee3b9",
        ),
        (
            "Simple-With-Us/AI-Fleet-Coordinator",
            "scripts/simple-tools-mcp/test/gr-workflow-v1.test.mjs",
            "8a0cf816252c39f32202429861483d0d128d4a30a7ff92e6cbbbad988ee7dad2",
        ),
        (
            "Simple-With-Us/AI-Fleet-Coordinator",
            "scripts/simple-tools-mcp/test/integration.workerd.mjs",
            "2b76ceeed6f6fdf66f6e04523d05cc9652e00377e82bcb55268ceaf143e10f8c",
        ),
        (
            "Simple-With-Us/AI-Fleet-Coordinator",
            "scripts/simple_tools/mcp/fixtures.jsonl",
            "bea6ca376aefdce65ce0295dbc72cef4a396b051c8add3e6d0174fc917c81829",
        ),
        (
            "Simple-With-Us/AI-Fleet-Coordinator",
            "scripts/simple_tools/tests/test_infisical_api_route.py",
            "3ee7aac6e82c0f22bbadc29ba915cc05054c7d5e3c0962f9c84f9db5b8f94d88",
        ),
        (
            "Simple-With-Us/AI-Fleet-Coordinator",
            "scripts/simple_tools/tests/test_mcp_extras.py",
            "d31c6f0a7f84f17dd0d480714a5d0be9f05fad97d3a3ef5d519da68b304bc95a",
        ),
        (
            "Simple-With-Us/AI-Fleet-Coordinator",
            "scripts/simple_tools/tests/test_operator_github.py",
            "ef10944dc053c2f4bf664f1ee796257d0deb4d64f1bb786be9ad58ec3ea4b442",
        ),
        (
            "Simple-With-Us/AI-Fleet-Coordinator",
            "scripts/simple_tools/tests/test_operator_send.py",
            "4353cf8e69c53151e3340c4a1bed374e380bac3dfa076e22d8c08eb575963873",
        ),
        (
            "Simple-With-Us/AI-Fleet-Coordinator",
            "scripts/test-lark-mm-bridge.py",
            "f94d6ecb794baab74b2d43c97b7cf5efd8aab0e76bc42e2a6224eb2f4ddb5535",
        ),
        (
            "Simple-With-Us/AI-Fleet-Coordinator",
            "scripts/test_operator_provenance.py",
            "a72e105b054617eecebdd2011dfca341e08dbc9377e378dccc8733aa7fea2c1b",
        ),
    }
)


def unreviewed_file_secret(repo: str, path: str, raw: bytes) -> str | None:
    """Scan all bytes; accept a match only for an exact reviewed attestation.

    The ordinary scanner is unchanged for messages, titles, binary assets and
    every unattested file. A byte change, new secret, different repo or moved
    destination invalidates the attestation and is refused before any write.
    """
    category = find_secret(raw.decode("utf-8", "replace"))
    if category is None:
        return None
    if (repo, path, sha256(raw).hexdigest()) in _REVIEWED_FIXTURES:
        return None
    return category
