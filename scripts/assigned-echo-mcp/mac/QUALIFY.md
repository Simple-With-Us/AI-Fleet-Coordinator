# Mac native qualification gate

UNCOMPILED SOURCE until Echo's single existing Mac host lane runs compile-only.sh.
That harness compiles and inspects an unsigned temporary binary only, then removes
it. It neither signs nor installs, opens the Keychain, provisions an item or reads
a secret. Native APIs are deprecated but supported legacy login-Keychain APIs;
actual availability and ABI must be checked with the installed SDK.

The helper is fixed to UID501, root-owned /usr/local/libexec/assigned-echo-mcp,
service assigned-hosted-mcp, account ECHO and UID501's explicit login Keychain.
Its self check requires fixed path, root ownership/nonwritable code and a valid
Developer ID signature with identifier assigned-echo-mcp and team CC8UTF7ATG.
No user-supplied account, seat, item path, executable or password argument exists.
The only owner mode ingress reads private stdin, refuses ANY existing item,
creates a generic password with a trusted-app ACL containing only the fixed
helper and clears its buffer. Runtime disables Keychain interaction, reads only
that item and execs the fixed adapter with PATH and assigned bearer in memory.
No security-w argument, -A, generic interpreter ACL or plaintext token file.

AFTER source review and actual owner storage/signing permission, Echo alone:
1. Compiles, signs with the existing Developer ID Application identity using the
   exact identifier above, installs fixed root-owned code and verifies signature,
   designated requirement, code ownership, path and adapter reviewed bytes.
2. Uses NONSENSITIVE synthetic fixtures in an isolated test Keychain first, with
   the same Security.framework ACL construction and separately signed test code.
   The trusted helper must read successfully and a DIFFERENT binary, including
   /usr/bin/security and a generic interpreter, must fail noninteractively.
   Inspect ACL entries and ensure no allow-all/partition-list widening. Test
   locked-Keychain refusal, wrong signature, writable path and existing-item
   refusal. This source does not claim those results or supply an ACL bypass.
3. If and only if native signed ACL checks pass, obtains its own authorized ECHO
   bearer via supported private custody and sends it to fixed helper ingress on
   stdin. No value is printed, logged, stored in JSON or placed on argv.
4. Qualifies initialize, tools/list, own-ECHO whoami and cross-seat denial; checks
   required families/GR and no inherited Infisical/CF secrets in the child.

A root/shared-jay adversary and other broad host credentials remain outside this
connector's isolation claim. Existing access is not revoked by this source. No
WISE instance or cross-account custody access is included. If ACL or code identity
cannot meet this gate, do not provision and report the native platform blocker.
