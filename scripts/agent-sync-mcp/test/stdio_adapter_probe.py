"""Run the real assigned-hosted-stdio adapter on JSON-RPC frames and print what it would send.

Read by test/stdio-adapter-headers.test.mjs:  frames (a JSON list) on stdin, a JSON list on stdout with, for each frame,
the lower-cased request headers (the bearer and the user agent left out) and the body text.  No network:  the adapter's
opener is a stand-in that answers every request with an empty tool result.  The bearer is synthetic.
"""

import importlib.util
import io
import json
import sys
from pathlib import Path

ADAPTER = Path(__file__).resolve().parents[2] / "mcp-servers" / "assigned-hosted-stdio.py"
TOKEN = "sbt_claude_" + "A" * 43

spec = importlib.util.spec_from_file_location("adapter", ADAPTER)
adapter_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(adapter_module)


class Reply(io.BytesIO):
    status = 200

    def __init__(self, frame_id):
        super().__init__(json.dumps({"jsonrpc": "2.0", "id": frame_id, "result": {"content": []}}).encode())
        self.headers = {"Content-Type": "application/json"}


sent = []


def opener(request, timeout):
    sent.append(request)
    return Reply(json.loads(request.data).get("id"))


out = []
for frame in json.load(sys.stdin):
    sent.clear()
    adapter_module.Adapter(TOKEN, opener).call(frame)
    request = sent[-1]
    headers = {key.lower(): value for key, value in request.header_items() if key.lower() not in ("authorization", "user-agent")}
    out.append({"headers": headers, "body": request.data.decode("utf-8")})
print(json.dumps(out))
