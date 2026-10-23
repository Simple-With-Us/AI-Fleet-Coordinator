# Lark MM Bridge

The deployment plist is a template, not an install-ready job.  Prepare a private copy with the authorized home directory, bridge script location, sender open ID and bot open ID substituted for its placeholders.  Keep the resulting local inventory outside public source.  The existing installed job is independent of this template; changing source does not install, load or restart it.

The bridge uses a separate ACP context per chat and one global FIFO worker, intentionally limiting model concurrency to one.  This preserves ordering and resource limits across chats.  An empty sender allowlist is rejected, and headless ACP permission requests are declined rather than granting unattended access.

The owner marks a message with the eyes emoji at its very first character to say that the seat must not be woken for it.  A marked message is read and dropped:  no turn, no reply, no delivery row, and its text never reaches the secret scan or the log.  The marker counts only at the start of the trimmed text, so a message that carries the emoji later in the line is an ordinary message and is answered.

The Operator (the agent-sync listener) can put a message into the Lark chat.  It runs the bridge with `--signal` and the text, which leaves a JSON file in the signal directory under the state root; the bridge, which is the only process holding the Lark bot identity, picks the file up, sends the text into the chat with an idempotency key built from the signal's own id, runs the seat turn on it, and puts the answer back in the same chat.  A signal that names no chat of its own goes to the chat the owner last wrote in, and that chat is remembered across restarts.  A signal with credential-shaped text, with no text, or with nowhere to go is refused and filed with its reason, and a signal that could not be sent is recorded as unknown rather than replayed.

The signal file is also the done-set:  it records the steps that are already done, so a retry after a crash neither sends the message into the chat a second time nor queues the turn a second time.  The send reports one of three outcomes, and the three cannot share a handler:  a rate limit or a failure that provably never reached Lark leaves the signal pending with a backoff, a refusal by Lark files it as refused, and a lost answer files it as unknown, which is never replayed on the bridge's own initiative.  Every record is written atomically, the done directory keeps only its newest records, and the signal directories refuse a symlink.  A signal turn is claimed before it is queued, so the durable state keeps it and a restart resumes it, and the sender travels with the turn:  the seat inbox and the Zulip mirror attribute the Operator's signal to the Operator, so it does not wake the seat as if the owner had asked.

ACP notifications accept known flat and nested message updates while rejecting malformed sections.  Only agent message chunks are replies; thought chunks and echoed user chunks remain excluded.  Replies are scanned before sending, and uncertain delivery outcomes are retained without automatic replay.

Verify the source with the synthetic bridge test script using Python 3.9 or newer.  It launches no real model or Lark session.  Live authentication, authorized sender values and activation remain separate deployment steps, with an existing-job registration and a real receipt required before claiming live delivery.


Permission requests are cancelled automatically; offered allow options and a full-access model configuration do not establish a trusted owner decision.  There is no approval grant path in this bridge.

The bridge imports the canonical `agent_sync.secretscan` module.  Deploy the script together with its `agent_sync` package from the same reviewed revision; a missing scanner prevents startup.  Incoming job content and model output use that scanner before persistence or publication.  ACP stderr is discarded after a fixed diagnostic identifying only its source field; dry-run output contains no event body.

Safe queued jobs and generated replies are persisted in the private state file before execution or delivery.  Reply retries reuse the generated answer and never call the model again.  HTTP 429 and provably unsent failures use bounded backoff and a persisted attempt budget; unknown delivery outcomes never replay.  During shutdown the active worker gets a bounded join before sessions close; unstarted queued jobs and cached replies remain durable for restart.  A crash during model execution leaves a running job held for explicit reconciliation, because automatically repeating that model turn could repeat tool side effects.  Legacy pending rows without a safe durable job are also held rather than rerun.

No live bridge, launchd job or model session was started or restarted to validate these changes; synthetic tests are the evidence available before activation.


The retry budget is bounded within each process.  Restart restores a safe pending cached reply with a fresh bounded delivery budget, never another model turn; unknown and running jobs remain held.  Dry run uses an event-only preview path that neither constructs a Bridge nor loads or writes its delivery ledger, starts a model, or sends a reply.
