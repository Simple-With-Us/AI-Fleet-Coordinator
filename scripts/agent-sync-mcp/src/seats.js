// Seat classification constants, kept in a leaf module (no imports) so the
// static page renderer (src/pages.js) can classify seats without pulling in
// src/config.js.  config.js imports ./events.js, which imports
// ./wa/contract.js, which imports zod - fine in the Worker and in the unit
// suite (both install scripts/agent-sync-mcp dependencies), but the visual
// tests run `npm ci` only in visual-tests/ and load pages.js directly, so any
// pages.js -> config.js edge breaks module resolution there.
export const EVENTS_ONLY_SEATS = Object.freeze(["JET"]);
