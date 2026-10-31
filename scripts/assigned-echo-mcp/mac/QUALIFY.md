# Mac native qualification gate

The request-owning Mac helper replaces the retired env-export/exec helper: the fixed
signed binary reads its own fixed ECHO Keychain item and performs the bounded HTTPS
session itself.  The bearer exists only in the helper's native memory;  there is no
subprocess, no token environment and no selectors.  The CLI sends the same three
newline-delimited JSON records (initialize id 0, initialized notification, call id 1)
and reads the same hello + answer lines, so the ingress framing is preserved.

Current gate status:  source and synthetic behavior only.  On the actual Mac, strict
clang -Wall -Wextra -Werror compiles of the core, framing, transport and wrapper pass,
and the nonsensitive fixtures pass under ASan/UBSan:  mac-fixture 66 checks,
mac-input-fixture 29 checks, mac-transport-fixture 28 checks with an NSURLProtocol
stub that intercepts every request so no network is reachable.  compile-only.sh
reproduces exactly that, including the sanitizer runs.  The hard process bound is
qualified, not assumed:  input reads and in-flight requests hold the 180s deadline,
and alarm() caps the whole process, including a stalled stdout writer, at 195s.
Because an inherited SIGALRM ignore disposition or blocked mask would silently
defeat alarm(), the wrapper resets the disposition and unblocks the signal at
entry.  The mechanism is qualified by the isolated alarm fixture
(mac-alarm-fixture-driver.c):  a child using the exact reset/unblock/alarm
sequence still dies with SIGALRM under inherited ignore, inherited block, and
both.  The actual 195s production bound against a blocked stdout writer is NOT
qualified end-to-end;  it inherits the mechanism qualification only.
The cancel-barrier path where the terminal callback never arrives is covered by
code review only;  this harness cannot force it deterministically.

Custody contract, unchanged from the reviewed design:  fixed path
/usr/local/libexec/assigned-echo-mcp/mac-request, UID 501, root-owned nonwritable
code with every ancestor held open through use, self-check against anchor apple
generic + identifier "assigned-echo-mcp-request" + team CC8UTF7ATG, fixed login
Keychain item service assigned-hosted-mcp account ECHO, Keychain interaction
disabled, token shape validated, buffers wiped.  Redirects are never followed;  the
credential is never forwarded to one.

Ingress preservation (SOURCE ONLY, never executed until owner approval):  the retired
helper's owner-mode ingress contract is implemented verbatim in mac-request.m as the
"ingress" argv mode - private stdin read, refusal of ANY existing item,
generic-password creation with a trusted-app ACL containing only the fixed helper
itself (SecTrustedApplicationCreateFromPath(NULL) is self), buffer cleared on every
path.  It never serves requests.  mac-acl-fixture.c restores the reviewed synthetic
ACL fixture source unchanged;  acl-fixture-plan.sh remains the print-only plan:
isolated disposable nonsecret test Keychain, two distinctly signed fixture binaries,
first binary reads, second binary plus /usr/bin/security plus a generic interpreter
must all fail noninteractively, ACL inspected explicitly, lock/refusal verified,
fixture Keychain deleted.  No production service, account, bearer or Keychain is
referenced by any fixture.

AFTER source review and actual owner storage/signing permission, Echo alone:
1. Compiles, signs with the existing Developer ID Application identity using the
   exact identifier above, installs fixed root-owned code and verifies signature,
   designated requirement, code ownership and path.
2. Runs the synthetic ACL fixtures in an isolated test Keychain first.
3. Only then provisions the one fixed ECHO item through the preserved ingress flow.
