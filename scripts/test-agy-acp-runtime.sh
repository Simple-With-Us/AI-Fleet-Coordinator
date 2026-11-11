#!/bin/bash
# Contract tests for the agy-acp runtime fail-closed wrapper.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
RT="${ROOT}/agy-acp-runtime"
START="${RT}/start.sh"
TURBO="${RT}/agy-acp-turbo.sh"
SHIM="${RT}/bind-loopback.cjs"

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

[[ -x "$START" ]] || fail "start.sh must be executable"
[[ -x "$TURBO" ]] || fail "agy-acp-turbo.sh must be executable"
[[ -f "$SHIM" ]] || fail "bind-loopback.cjs missing"

grep -q -- '--grace-period 300' "$START" || fail "start.sh grace must be 300s"
if grep -q -- '604800' "$START"; then
  fail "start.sh must not keep the 7-day grace period"
fi
grep -q 'agy-acp-turbo.sh' "$START" || fail "start.sh must exec turbo.sh"
if grep -q 'agy-acp-list-wrapper' "$START"; then
  fail "start.sh must not spawn the session/list wrapper"
fi
if grep -E -q -- '/usr/local/bin/agy-acp[[:space:]]*$' "$START"; then
  fail "start.sh must not spawn vanilla agy-acp"
fi
grep -q -- '-r "$BIND_SHIM"' "$START" || fail "start.sh must node -r the bind shim"
if grep -q 'NODE_OPTIONS' "$START"; then
  fail "start.sh must not export NODE_OPTIONS (child would inherit it)"
fi

grep -q -- '--dangerously-skip-permissions --mode accept-edits' "$TURBO" \
  || fail "turbo.sh must set AGY_EXTRA_ARGS turbo policy"
grep -q -- '--skip-naration' "$TURBO" || fail "turbo.sh must pass --skip-naration"
grep -q 'AGY_EXTRA_ARGS' "$TURBO" || fail "turbo.sh must export AGY_EXTRA_ARGS"

# Bind reject happens before exec, so this works off-Mac.
out="$(AGY_ACP_BIND=0.0.0.0 bash "$START" 2>&1 || true)"
if [[ "$out" != *"AGY_ACP_BIND must be loopback"* ]]; then
  fail "wildcard bind must fail-closed, got: $out"
fi
out6="$(AGY_ACP_BIND=:: bash "$START" 2>&1 || true)"
if [[ "$out6" != *"AGY_ACP_BIND must be loopback"* ]]; then
  fail "IPv6 wildcard bind must fail-closed, got: $out6"
fi

AGY_ACP_BIND=127.0.0.1 node -r "$SHIM" -e '
const http = require("http");
const server = http.createServer((_, res) => res.end("ok"));
server.listen(0, () => {
  const addr = server.address();
  server.close();
  if (!addr || addr.address !== "127.0.0.1") {
    console.error("FAIL: listen bound", addr);
    process.exit(1);
  }
});
'

if AGY_ACP_BIND=0.0.0.0 node -r "$SHIM" -e 'require("http").createServer().listen(0)' \
  >/tmp/agy-bind-wild.out 2>/tmp/agy-bind-wild.err; then
  fail "shim must refuse wildcard AGY_ACP_BIND"
fi
if ! grep -q "AGY_ACP_BIND must be loopback" /tmp/agy-bind-wild.err; then
  fail "shim wildcard error missing, got: $(cat /tmp/agy-bind-wild.err)"
fi

echo "ok"
