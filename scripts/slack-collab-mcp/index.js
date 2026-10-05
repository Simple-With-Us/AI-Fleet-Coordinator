#!/usr/bin/env node

import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import {
  CallToolRequestSchema,
  ListToolsRequestSchema,
} from "@modelcontextprotocol/sdk/types.js";
import fs from "node:fs";
import path from "node:path";
import os from "node:os";
import { WebSocket } from "ws";

// ── Environment & Secrets Setup ─────────────────────────────────────────────
const ENV_FILE = process.env.AGENT_SYNC_ENV || path.join(os.homedir(), ".secrets", "agent-sync.env");

function loadSecrets() {
  if (!fs.existsSync(ENV_FILE)) return;
  try {
    const raw = fs.readFileSync(ENV_FILE, "utf8");
    for (const line of raw.split(/\r?\n/)) {
      const trimmed = line.trim();
      if (!trimmed || trimmed.startsWith("#") || !trimmed.includes("=")) continue;
      const [k, ...rest] = trimmed.split("=");
      const key = k.trim().replace(/^export\s+/, "");
      const val = rest.join("=").trim().replace(/^['"]|['"]$/g, "");
      if (!process.env[key]) {
        process.env[key] = val;
      }
    }
  } catch (_) {}
}

loadSecrets();

const BOT_TOKEN = process.env.SLACK_BOT_TOKEN || "";
const POST_TOKEN = process.env.AGENT_SYNC_POST_TOKEN || process.env.AGENT_SYNC_TOKEN || "";
const LOCAL_RELAY_URL = process.env.AGENT_SYNC_RELAY || "http://127.0.0.1:8787";
const LOCAL_WS_URL = process.env.AGENT_SYNC_WS || "ws://127.0.0.1:8787";

// ── Channel Mappings ────────────────────────────────────────────────────────
const CHANNEL_MAP = {
  "agent-sync": "C0BEZDJDNKV",
  "socratictrade": "C0BBPSEBNAW",
  "congresstrade": "C0BDJ7A74KZ",
  "usage-monitor": "C0C6LR70TLZ",
  "dealdex": "C0C63FVB3AT",
  "codecaps": "C0C6NFR5QRJ",
  "botfleet": "C0C6CJEQXV1",
  "hoghunter": "C0C6LPC8JPK",
  "ai-fleet-coordinator": "C0C6JNBRZNE",
  "fleet-ops": "C0C6DHY8D0D",
  "autorotate": "C0C63FTKWNB",
  "clutch": "C0C6NH6A17W",
  "contactlogo": "C0C6JNDDX9Q",
  "cts": "C0C63FY812T",
  "congress-trading-shared": "C0C63FY812T",
  "fleetlink": "C0C6DJ05ZPX",
  "personal-site": "C0C6GU8B222",
  "simple-with-us": "C0C7D8CT9EU",
  "random": "C0BB8C4D8DD",
  "general": "C0BBRMR439P",
  "coding": "C0BDH8UKS1L",
  // Aliases
  "st": "C0BBPSEBNAW",
  "st-sync": "C0BBPSEBNAW",
  "socratic-trade": "C0BBPSEBNAW",
  "ct": "C0BDJ7A74KZ",
  "ct-sync": "C0BDJ7A74KZ",
  "congress-trade": "C0BDJ7A74KZ",
  "um": "C0C6LR70TLZ",
  "um-sync": "C0C6LR70TLZ",
  "hh": "C0C6LPC8JPK",
  "hh-sync": "C0C6LPC8JPK",
  "bf": "C0C6CJEQXV1",
  "afc": "C0C6JNBRZNE",
  "ops": "C0C6DHY8D0D",
  "sync-botfleet": "C0C6CJEQXV1",
  "sync-codecaps": "C0C6NFR5QRJ",
  "sync-hoghunter": "C0C6LPC8JPK",
  "sync-congress-trade": "C0BDJ7A74KZ",
  "sync-dealdex": "C0C63FVB3AT",
  "sync-socratic-trade": "C0BBPSEBNAW",
  "sync-usage-monitor": "C0C6LR70TLZ",
};

const CHANNEL_ID_TO_NAME = Object.fromEntries(
  Object.entries(CHANNEL_MAP).filter(([k, v]) => !k.includes("sync-") && !["st", "ct", "um", "hh", "bf", "afc", "ops"].includes(k)).map(([k, v]) => [v, k])
);

function resolveChannel(input) {
  if (!input) return "C0BEZDJDNKV"; // Default #agent-sync
  const clean = input.trim().toLowerCase().replace(/^#/, "");
  if (CHANNEL_MAP[clean]) return CHANNEL_MAP[clean];
  if (/^C[A-Z0-9]{8,12}$/i.test(input.trim())) return input.trim().toUpperCase();
  return "C0BEZDJDNKV";
}

// ── Slack API Helper ────────────────────────────────────────────────────────
async function slackApi(endpoint, params = {}, method = "GET") {
  if (!BOT_TOKEN) {
    throw new Error("SLACK_BOT_TOKEN not configured in environment or ~/.secrets/agent-sync.env");
  }
  const url = new URL(`https://slack.com/api/${endpoint}`);
  let options = {
    headers: {
      Authorization: `Bearer ${BOT_TOKEN}`,
    },
  };

  if (method === "GET") {
    for (const [k, v] of Object.entries(params)) {
      if (v !== undefined && v !== null) {
        url.searchParams.set(k, String(v));
      }
    }
  } else {
    options.method = "POST";
    options.headers["Content-Type"] = "application/json; charset=utf-8";
    options.body = JSON.stringify(params);
  }

  const res = await fetch(url.toString(), options);
  const data = await res.json().catch(() => ({}));
  if (!res.ok || !data.ok) {
    throw new Error(data.error || `Slack API error ${res.status}`);
  }
  return data;
}

// ── MCP Server Definition ───────────────────────────────────────────────────
const server = new Server(
  {
    name: "slack-collab",
    version: "1.0.0",
  },
  {
    capabilities: {
      tools: {},
    },
  }
);

// ── Register Tool List ──────────────────────────────────────────────────────
server.setRequestHandler(ListToolsRequestSchema, async () => {
  return {
    tools: [
      {
        name: "slack_post_message",
        description:
          "Post a coordination message, blocker, claim, or turn to any Slack channel or thread. Supports custom agent display name (e.g. [AG], [CURSOR], [CODEX], [MM]) and thread replies.",
        inputSchema: {
          type: "object",
          properties: {
            text: {
              type: "string",
              description: "The message text to send to Slack.",
            },
            channel: {
              type: "string",
              description:
                "Target channel name (e.g. 'agent-sync', 'socratictrade', 'fleetlink', 'codecaps') or channel ID (e.g. 'C0BEZDJDNKV'). Defaults to 'agent-sync'.",
            },
            thread_ts: {
              type: "string",
              description:
                "Optional thread timestamp (e.g. '1791181956.778789'). If specified, replies inside that thread.",
            },
            reply_broadcast: {
              type: "boolean",
              description:
                "If true and thread_ts is specified, also broadcast the reply to the main channel.",
            },
            username: {
              type: "string",
              description:
                "Agent tag/seat name to post as (e.g. 'AG', 'CURSOR', 'CODEX', 'MINIMAX'). Defaults to AGENT_TAG or 'AG'.",
            },
          },
          required: ["text"],
        },
      },
      {
        name: "slack_read_thread",
        description:
          "Read all messages from an active Slack thread in chronological order. Use for multi-agent pair programming and following collaboration discussions.",
        inputSchema: {
          type: "object",
          properties: {
            channel: {
              type: "string",
              description: "Channel name (e.g. 'agent-sync', 'socratictrade') or channel ID.",
            },
            thread_ts: {
              type: "string",
              description: "The root message timestamp of the thread to read.",
            },
            limit: {
              type: "number",
              description: "Maximum number of messages to return (default 50, max 100).",
            },
          },
          required: ["channel", "thread_ts"],
        },
      },
      {
        name: "slack_read_channel",
        description:
          "Read the latest messages from any Slack channel. Use to inspect ambient activity, verify previous posts, or check recent claims.",
        inputSchema: {
          type: "object",
          properties: {
            channel: {
              type: "string",
              description: "Channel name or channel ID.",
            },
            limit: {
              type: "number",
              description: "Number of messages to retrieve (default 20, max 100).",
            },
            since_ts: {
              type: "string",
              description: "Optional oldest timestamp (only return messages newer than this).",
            },
          },
          required: ["channel"],
        },
      },
      {
        name: "slack_await_reply",
        description:
          "Wait reactively for a reply in a channel or thread from a specific agent or human peer. Sub-second response without aggressive polling.",
        inputSchema: {
          type: "object",
          properties: {
            channel: {
              type: "string",
              description: "Channel name or ID to monitor.",
            },
            thread_ts: {
              type: "string",
              description: "Optional thread timestamp to wait for replies inside.",
            },
            target_sender: {
              type: "string",
              description: "Optional agent tag or username to wait for (e.g. 'CURSOR', 'CODEX', 'JAY').",
            },
            timeout_seconds: {
              type: "number",
              description: "Maximum seconds to wait before returning (default 45, max 180).",
            },
          },
          required: ["channel"],
        },
      },
      {
        name: "slack_list_channels",
        description:
          "List all available team channels in the workspace with IDs, active purposes, topics, and member counts.",
        inputSchema: {
          type: "object",
          properties: {},
        },
      },
      {
        name: "slack_add_reaction",
        description:
          "Add an emoji reaction (e.g. 'white_check_mark', 'eyes', 'thumbsup') to a message for lightweight ACK signaling.",
        inputSchema: {
          type: "object",
          properties: {
            channel: {
              type: "string",
              description: "Channel name or channel ID.",
            },
            timestamp: {
              type: "string",
              description: "Timestamp of the message to react to.",
            },
            name: {
              type: "string",
              description: "Reaction emoji name without colons (e.g. 'white_check_mark', 'eyes', 'rocket').",
            },
          },
          required: ["channel", "timestamp", "name"],
        },
      },
    ],
  };
});

// ── Tool Execution Handler ──────────────────────────────────────────────────
server.setRequestHandler(CallToolRequestSchema, async (request) => {
  const { name, arguments: args } = request.params;

  try {
    switch (name) {
      case "slack_post_message": {
        const text = (args.text || "").trim();
        if (!text) throw new Error("text is required");
        const targetChannel = resolveChannel(args.channel);
        const username = (args.username || process.env.AGENT_TAG || "AG").trim().toUpperCase();
        const thread_ts = args.thread_ts ? String(args.thread_ts).trim() : undefined;
        const reply_broadcast = Boolean(args.reply_broadcast);

        // Try local relay endpoint first for fast push fan-out
        let postedViaRelay = false;
        let postResult = null;

        if (POST_TOKEN) {
          try {
            const relayRes = await fetch(`${LOCAL_RELAY_URL}/post`, {
              method: "POST",
              headers: {
                Authorization: `Bearer ${POST_TOKEN}`,
                "Content-Type": "application/json",
              },
              body: JSON.stringify({
                channel: targetChannel,
                text,
                username,
                thread_ts,
                reply_broadcast,
              }),
            });
            if (relayRes.ok) {
              postResult = await relayRes.json();
              postedViaRelay = true;
            }
          } catch (_) {
            // Relay unreachable, fallback to direct Slack API
          }
        }

        if (!postedViaRelay) {
          const direct = await slackApi("chat.postMessage", {
            channel: targetChannel,
            text,
            username,
            ...(thread_ts ? { thread_ts } : {}),
            ...(reply_broadcast ? { reply_broadcast: true } : {}),
            unfurl_links: false,
            unfurl_media: false,
          }, "POST");
          postResult = {
            ok: true,
            channel: direct.channel,
            ts: direct.ts,
            thread_ts: direct.message?.thread_ts || thread_ts || null,
          };
        }

        const chDisplay = CHANNEL_ID_TO_NAME[postResult.channel] || postResult.channel;
        return {
          content: [
            {
              type: "text",
              text: JSON.stringify({
                ok: true,
                message: `Posted to #${chDisplay}${postResult.thread_ts ? ` (thread: ${postResult.thread_ts})` : ""} as [${username}]`,
                channel: postResult.channel,
                channel_name: chDisplay,
                ts: postResult.ts,
                thread_ts: postResult.thread_ts,
              }, null, 2),
            },
          ],
        };
      }

      case "slack_read_thread": {
        const targetChannel = resolveChannel(args.channel);
        const thread_ts = String(args.thread_ts).trim();
        const limit = Math.min(Math.max(parseInt(args.limit || 50, 10), 1), 100);

        const data = await slackApi("conversations.replies", {
          channel: targetChannel,
          ts: thread_ts,
          limit,
        });

        const messages = (data.messages || []).map((m) => ({
          ts: m.ts,
          user: m.user,
          username: m.username || m.user,
          text: m.text,
          thread_ts: m.thread_ts,
          reply_count: m.reply_count || 0,
        }));

        return {
          content: [
            {
              type: "text",
              text: JSON.stringify({
                ok: true,
                channel: targetChannel,
                thread_ts,
                count: messages.length,
                messages,
              }, null, 2),
            },
          ],
        };
      }

      case "slack_read_channel": {
        const targetChannel = resolveChannel(args.channel);
        const limit = Math.min(Math.max(parseInt(args.limit || 20, 10), 1), 100);
        const params = {
          channel: targetChannel,
          limit,
        };
        if (args.since_ts) {
          params.oldest = String(args.since_ts);
        }

        const data = await slackApi("conversations.history", params);
        const messages = (data.messages || []).map((m) => ({
          ts: m.ts,
          user: m.user,
          username: m.username || m.user,
          text: m.text,
          thread_ts: m.thread_ts || null,
          reply_count: m.reply_count || 0,
        }));

        return {
          content: [
            {
              type: "text",
              text: JSON.stringify({
                ok: true,
                channel: targetChannel,
                channel_name: CHANNEL_ID_TO_NAME[targetChannel] || targetChannel,
                count: messages.length,
                messages,
              }, null, 2),
            },
          ],
        };
      }

      case "slack_await_reply": {
        const targetChannel = resolveChannel(args.channel);
        const targetThread = args.thread_ts ? String(args.thread_ts).trim() : null;
        const targetSender = args.target_sender ? String(args.target_sender).trim().toUpperCase() : null;
        const timeoutMs = Math.min(Math.max((args.timeout_seconds || 45) * 1000, 5000), 180000);

        const startTime = Date.now();
        const startTs = (startTime / 1000).toFixed(6);

        // Use local WebSocket fanout if available for zero-latency reactive wakeup
        const reply = await new Promise((resolve) => {
          let timer = null;
          let ws = null;
          let settled = false;

          const done = (val) => {
            if (settled) return;
            settled = true;
            if (timer) clearTimeout(timer);
            if (ws) {
              try { ws.close(); } catch (_) {}
            }
            resolve(val);
          };

          timer = setTimeout(() => {
            done({ ok: false, timed_out: true, message: `No reply received within ${timeoutMs / 1000}s` });
          }, timeoutMs);

          try {
            const headers = POST_TOKEN ? { Authorization: `Bearer ${POST_TOKEN}` } : {};
            ws = new WebSocket(LOCAL_WS_URL, { headers });

            ws.on("message", (raw) => {
              try {
                const rec = JSON.parse(raw.toString());
                if (!rec || !rec.ts || Number(rec.ts) < Number(startTs)) return;
                if (rec.channel !== targetChannel) return;
                if (targetThread && rec.thread_ts !== targetThread) return;
                if (targetSender) {
                  const sender = (rec.username || rec.user || "").toUpperCase();
                  if (!sender.includes(targetSender)) return;
                }
                done({
                  ok: true,
                  matched: true,
                  channel: rec.channel,
                  ts: rec.ts,
                  thread_ts: rec.thread_ts,
                  username: rec.username || rec.user,
                  text: rec.text,
                });
              } catch (_) {}
            });

            ws.on("error", () => {
              // Fallback to polling history if WS fails
              pollFallback(targetChannel, targetThread, targetSender, startTs, done);
            });
          } catch (_) {
            pollFallback(targetChannel, targetThread, targetSender, startTs, done);
          }
        });

        return {
          content: [
            {
              type: "text",
              text: JSON.stringify(reply, null, 2),
            },
          ],
        };
      }

      case "slack_list_channels": {
        const convs = await slackApi("conversations.list", {
          types: "public_channel",
          limit: 100,
          exclude_archived: "true",
        });

        const channels = (convs.channels || []).map((c) => ({
          id: c.id,
          name: c.name,
          alias: CHANNEL_MAP[c.name] ? c.name : undefined,
          is_member: c.is_member,
          num_members: c.num_members,
          topic: c.topic?.value || "",
          purpose: c.purpose?.value || "",
        }));

        return {
          content: [
            {
              type: "text",
              text: JSON.stringify({
                ok: true,
                count: channels.length,
                channels,
              }, null, 2),
            },
          ],
        };
      }

      case "slack_add_reaction": {
        const targetChannel = resolveChannel(args.channel);
        const timestamp = String(args.timestamp).trim();
        const reactionName = String(args.name).trim().replace(/^:|:$/g, "");

        await slackApi("reactions.add", {
          channel: targetChannel,
          timestamp,
          name: reactionName,
        }, "POST");

        return {
          content: [
            {
              type: "text",
              text: JSON.stringify({
                ok: true,
                message: `Added :${reactionName}: reaction to message ${timestamp}`,
              }, null, 2),
            },
          ],
        };
      }

      default:
        throw new Error(`Unknown tool: ${name}`);
    }
  } catch (err) {
    return {
      isError: true,
      content: [
        {
          type: "text",
          text: `Error executing ${name}: ${err.message}`,
        },
      ],
    };
  }
});

async function pollFallback(targetChannel, targetThread, targetSender, startTs, done) {
  const interval = setInterval(async () => {
    try {
      let messages = [];
      if (targetThread) {
        const rep = await slackApi("conversations.replies", { channel: targetChannel, ts: targetThread, oldest: startTs });
        messages = rep.messages || [];
      } else {
        const hist = await slackApi("conversations.history", { channel: targetChannel, oldest: startTs, limit: 10 });
        messages = hist.messages || [];
      }

      for (const m of messages) {
        if (Number(m.ts) > Number(startTs)) {
          if (targetSender) {
            const sender = (m.username || m.user || "").toUpperCase();
            if (!sender.includes(targetSender)) continue;
          }
          clearInterval(interval);
          done({
            ok: true,
            matched: true,
            channel: targetChannel,
            ts: m.ts,
            thread_ts: m.thread_ts,
            username: m.username || m.user,
            text: m.text,
          });
          return;
        }
      }
    } catch (_) {}
  }, 3000);
}

// ── Server Start ────────────────────────────────────────────────────────────
async function run() {
  const transport = new StdioServerTransport();
  await server.connect(transport);
}

run().catch((err) => {
  console.error("FATAL in slack-collab MCP server:", err);
  process.exit(1);
});
