/**
 * Optional pm2 snippet for seat-mcp.  Do not `pm2 start` from a one-off
 * dsh/npx session.  Do not `pm2 save` unless the fleet dump is healthy.
 *
 * Local bind only:  127.0.0.1:8793.  No Cloudflare hostname in v1.
 *
 *   // drop this object into ~/apps/pm2-ecosystem.config.cjs apps[] when ready
 *   // then:  pm2 start /Users/jay/apps/pm2-ecosystem.config.cjs --only seat-mcp
 */
module.exports = {
  name: "seat-mcp",
  script: "/Users/jay/apps/seat-mcp/start.sh",
  interpreter: "bash",
  cwd: "/Users/jay/apps/seat-mcp",
  out_file: "/Users/jay/.pm2/logs/seat-mcp-out.log",
  error_file: "/Users/jay/.pm2/logs/seat-mcp-error.log",
};
