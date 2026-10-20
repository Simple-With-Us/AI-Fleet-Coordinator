# Lark MM Bridge

The deployment plist is a template, not an install-ready job.  Prepare a private copy with the authorized home directory, bridge script location, sender open ID and bot open ID substituted for its placeholders.  Keep the resulting local inventory outside public source.  The existing installed job is independent of this template; changing source does not install, load or restart it.

The bridge uses a separate ACP context per chat and one global FIFO worker, intentionally limiting model concurrency to one.  This preserves ordering and resource limits across chats.  An empty sender allowlist is rejected, and headless ACP permission requests are declined rather than granting unattended access.

ACP notifications accept known flat and nested message updates while rejecting malformed sections.  Only agent message chunks are replies; thought chunks and echoed user chunks remain excluded.  Replies are scanned before sending, and uncertain delivery outcomes are retained without automatic replay.

Verify the source with the synthetic bridge test script using Python 3.9 or newer.  It launches no real model or Lark session.  Live authentication, authorized sender values and activation remain separate deployment steps, with an existing-job registration and a real receipt required before claiming live delivery.


Permission requests are cancelled automatically; offered allow options and a full-access model configuration do not establish a trusted owner decision.  There is no approval grant path in this bridge.

The bridge imports the canonical `agent_sync.secretscan` module.  Deploy the script together with its `agent_sync` package from the same reviewed revision; a missing scanner prevents startup.  Incoming job content, model output, dry-run output and ACP stderr use that scanner before persistence or publication.

Safe queued jobs and generated replies are persisted in the private state file before execution or delivery.  Reply retries reuse the generated answer and never call the model again.  HTTP 429 and provably unsent failures use bounded backoff and a persisted attempt budget; unknown delivery outcomes never replay.  During shutdown the active worker gets a bounded join before sessions close; unstarted queued jobs and cached replies remain durable for restart.  A crash during model execution leaves a running job held for explicit reconciliation, because automatically repeating that model turn could repeat tool side effects.  Legacy pending rows without a safe durable job are also held rather than rerun.

No live bridge, launchd job or model session was started or restarted to validate these changes; synthetic tests are the evidence available before activation.
