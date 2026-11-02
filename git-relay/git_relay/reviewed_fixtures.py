"""Reviewed, exact-content attestations for outbound security-test fixtures.

This is trusted service code, not seat-supplied configuration. Each entry needs
normal code review of the full fixture, provenance and intended destination.
No prefix/path exemption, caller override, baseline inference or diff-only scan.
"""

from hashlib import sha256

from .secret_scan import find_secret

# Source c8c2a471fac215957cc81ec3b2e5e765e5908443. Only these exact
# bytes at these exact repository destinations are attested synthetic fixtures.
# The source Git blob ids and review procedure are recorded in the README.
_REVIEWED_FIXTURES = frozenset({
    ("Simple-With-Us/AI-Fleet-Coordinator", "scripts/simple_tools/mcp/fixtures.jsonl",
     "06389862d783117572042eaef6cfd645c196a74648ad926c1f38a2ff14afe50a"),
    ("Simple-With-Us/AI-Fleet-Coordinator", "scripts/simple_tools/tests/test_operator_github.py",
     "ef10944dc053c2f4bf664f1ee796257d0deb4d64f1bb786be9ad58ec3ea4b442"),
    ("Simple-With-Us/AI-Fleet-Coordinator", "scripts/simple_tools/tests/test_operator_send.py",
     "d2f235ea96b7cd5bbc2dff026e3db178e051d23775532e7855bc1de019c594a7"),
    ("Simple-With-Us/AI-Fleet-Coordinator", "scripts/test-lark-mm-bridge.py",
     "f94d6ecb794baab74b2d43c97b7cf5efd8aab0e76bc42e2a6224eb2f4ddb5535"),
    ("Simple-With-Us/AI-Fleet-Coordinator", "scripts/test_operator_provenance.py",
     "a72e105b054617eecebdd2011dfca341e08dbc9377e378dccc8733aa7fea2c1b"),
})


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
