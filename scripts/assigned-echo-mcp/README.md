# ECHO-only native custody, server first

SOURCE ONLY. This code has offline synthetic checks, not host qualification.
Do not provision or deploy until source review, host permission, and native
systemd 259 checks pass. The existing host owner owns every secret and service
effect. No WISE instance, other-seat item, or secret transfer is included.

The fixed server code directory is `/usr/local/libexec/assigned-echo-mcp`, root
owned and not writable by clients. After PR1026 is qualified and available in the checkout, the root-only
`install-linux.sh` installs its reviewed `assigned-hosted-stdio.py` and this
`linux-custody.py` there. It creates code only, never storage or a service. No code installer
or helper accepts a caller-supplied seat, token path, endpoint or executable.
The host owner verifies the installed code and root-only write permissions.
Both `/usr/local` and `/usr/local/libexec` must already exist as root-owned,
non-symlink, non-group/world-writable directories. The selected host was
qualified with root0755 ancestors. A generic root:staff2775 layout is deliberately
refused, not silently broadened or chmodded by this ECHO-specific installer.

After explicit storage approval, the owner may feed its existing ECHO bearer
through private stdin to `linux-custody.py ingress`. It validates ECHO format,
uses native `/usr/bin/systemd-creds encrypt --with-key=host` on stdin, and writes
only ciphertext to the fixed, previously absent
`/etc/credstore.encrypted/assigned-echo-mcp`, mode0600. The directory must already
be root0700. Native encryption may create the host key; that is an approved host
operation, never a source-test side effect. Existing ciphertext is never replaced.
No plaintext file, command argument, log, Infisical reader or fallback is used.

The root-controlled launcher starts a fixed transient `assigned-echo-mcp.service`
with DynamicUser, `LoadCredentialEncrypted`, hardening and stdio pipe. Its child
reads only the fixed systemd materialized service credential, checks own UID and
private file permissions, then execs the fixed adapter with only a fixed PATH and
ASSIGNED_MCP_BEARER in memory. No automation/CF credentials enter that child.
Concurrent fixed-unit launches fail rather than sharing a session or selecting a
new unit. No caller-controlled service or code override is accepted.

Host checks must cover real encryption/decryption, materialized credential
permissions, DynamicUser isolation, initialized MCP session, own ECHO whoami and
cross-seat denial, hosted inventory, stop/cleanup and no plaintext artifacts.
No actual secret is used in source tests. The caller's ability to launch a
root-controlled service must be authorized through the host's existing access
model; this code does not add sudo rules. Root/shared-jay access outside the
connector is a separate broader boundary and is not claimed isolated.

Mac Security.framework ingress and fixed signed-helper ACL are a separate native
platform leg. A signing identity being available is not signature or ACL proof.
Do not replace that leg with `security -w` arguments, `-A`, shared JSON or a token
readable by any generic interpreter. No Mac helper is claimed ready here.

## Native Linux credential representation

systemd259.5 on the qualified ECHO host materializes the fixed credential as root:root0440 and its directory as root:root0550, on the same read-only tmpfs.  The group bits are the POSIX ACL mask, not group-object access.  Actual same-unit metadata confirms a named service-UID read ACL (directory read/search), group-object and other permissions both zero.  The service UID is not the inode owner.  The earlier self-owned0400 check therefore refused valid native credentials before MCP startup.

The child now requires a non-root service UID, root-owned non-writable nonsymlink ancestors, a held fixed directory descriptor, root0550 directory and root0440 single-link regular file on the same read-only filesystem, and exact Linux POSIX ACL xattrs: root plus this service UID only, read/search for the directory and read for the file, group-object/other zero and the corresponding mask.  No missing-ACL, writable-mount, broad-group, arbitrary-owner or alternate-path fallback is accepted.  Unsupported systemd representations fail closed.  Existing ciphertext is preserved; this change requires no re-ingress, remint or rotation.

Upstream implementation: https://raw.githubusercontent.com/systemd/systemd/v259.5/src/core/exec-credential.c uses0400 then named-UID ACL read permission, falling back to ownership only when a read-only remount is safe.  Synthetic regression tests do not replace actual host startup, native custody and MCP qualification.
