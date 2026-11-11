"use strict";

// Owned stdio-to-ws bind persist.  Packaged WebSocketServer({ port }) listens
// on ::: / 0.0.0.0.  Load with `node -r` (not NODE_OPTIONS) so the turbo child
// does not inherit the hook.  npm i cannot restore a wildcard listen.

const net = require("net");

const LOOPBACK = new Set(["127.0.0.1", "localhost", "::1"]);

function forcedHost() {
  const host = process.env.AGY_ACP_BIND || "127.0.0.1";
  if (!LOOPBACK.has(host)) {
    throw new Error(`agy-acp: AGY_ACP_BIND must be loopback, got ${host}`);
  }
  return host;
}

const origListen = net.Server.prototype.listen;
net.Server.prototype.listen = function agyBindListen(...args) {
  const host = forcedHost();
  if (typeof args[0] === "object" && args[0] !== null) {
    const opts = Object.assign({}, args[0]);
    if (!LOOPBACK.has(String(opts.host || ""))) {
      opts.host = host;
    }
    args[0] = opts;
  } else if (typeof args[0] === "number") {
    if (typeof args[1] !== "string") {
      args.splice(1, 0, host);
    } else if (!LOOPBACK.has(args[1])) {
      args[1] = host;
    }
  }
  return origListen.apply(this, args);
};
