# Lark MM Bridge

The deployment plist is a template, not an install-ready job.  Prepare a private copy with the authorized home directory, bridge script location, sender open ID and bot open ID substituted for its placeholders.  Keep the resulting local inventory outside public source.  The existing installed job is independent of this template; changing source does not install, load or restart it.

The bridge uses a separate ACP context per chat and one global FIFO worker, intentionally limiting model concurrency to one.  This preserves ordering and resource limits across chats.  An empty sender allowlist is rejected, and headless ACP permission requests are declined rather than granting unattended access.

ACP notifications accept known flat and nested message updates while rejecting malformed sections.  Only agent message chunks are replies; thought chunks and echoed user chunks remain excluded.  Replies are scanned before sending, and uncertain delivery outcomes are retained without automatic replay.

Verify the source with the synthetic bridge test script using Python 3.9 or newer.  It launches no real model or Lark session.  Live authentication, authorized sender values and activation remain separate deployment steps, with an existing-job registration and a real receipt required before claiming live delivery.
