# Fleet RAG write path: contribute lessons, scan chats for policy only — 2026-09-02

Historical identifiers have been redacted. These records do not certify current deployment state.

**Seat:** GROK.  **Worktree:** `~/apps/fleet-grok-rag-mine` @ `grok/rag-memory-policy`.

Owner ruling after DeepSeek's audit and Antigravity's reply: agent `recall_contribute` is the
highest-yield memory source.  Chat transcript review is done sparingly for infrastructure and
policy drift, not as a lesson miner.  DeepSeek's read-only corpus advice is rejected.

## What changed

- `ingest --all` uses `NIGHTLY_SOURCES` and **skips `chat-log`**.  Pass `--source chat-log` for
  a rare policy scan.
- `iter_chat_logs` yields only **user** turns that look like an owner ruling / infra policy
  (seat `OWNER`, category `preference` or `infrastructure`).  Full sessions are not Docs.
- fleet-recall skill + [retired identifier] contribute every reusable lesson; do not treat contribute as
  last-resort after board/Notes.
- Staged `~/apps/fleet-rag/mined/chat/*.jsonl` (~50k session dumps) is **not** ingested into
  `fleet-agents`.  Extra markdown staging can still ride the expanded `doc` walker.

## Verify

```
cd ~/apps/fleet-grok-rag-mine/scripts
python3 -m unittest fleet_rag.tests.test_sources fleet_rag.tests.test_ingest fleet_rag.tests.test_recall
```

## Follow-ups (DeepSeek items we still agree with)

Doc diet (standing tokens per session must drop).  Golden-set growth from real re-derivations.
Read-only Qdrant key restart.  Dedicated box so fleet-agents does not ride ST Qdrant forever.
TEI CPU is still the ingest ceiling; do not raise it mid-run.
