"""agent-sync over MCP (docs/protocols/agent-sync-mcp.md).

  tools.json      the tool contract:  names, descriptions, input and output schemas, annotations.
                  Every transport serves it unchanged; tests assert tools/list deep-equals it.
  fixtures.jsonl  golden cases every transport's tests run (fencing, secret scan, mentions,
                  schema rejections, error mapping).
  tools           the handlers, the untrusted envelope, the schema check and the error model.
  stdio           `agent-sync mcp`:  newline-delimited JSON-RPC 2.0 on stdin and stdout, both MCP
                  eras (the 2026-07-28 `server/discover` era and the `initialize` handshake).

Python 3.11+, standard library only.
"""
