import { LINEAR_MCP_BY_UPSTREAM_NAME } from "./linear-mcp-specs.js";

export const LINEAR_MCP_URL = "https://mcp.linear.app/mcp";
export const LINEAR_MCP_PROTOCOL_VERSION = "2025-03-26";
export const LINEAR_MCP_MAX_RESPONSE_BYTES = 8 * 1024 * 1024;

const JSON_RPC_VERSION = "2.0";
const ACCEPT = "application/json, text/event-stream";
const SAFE_ERROR_KINDS = new Set([
  "unauthorized", "forbidden", "rate_limited", "timeout", "network",
  "redirect", "too_large", "protocol", "upstream_error", "mint", "status",
]);

/** Safe, structured failure from the fixed official Linear MCP transport. */
export class OfficialLinearMcpError extends Error {
  constructor(kind, { status = null, retryAfterS = null, uncertain = false, phase = "transport", rpcCode = null } = {}) {
    const safeKind = SAFE_ERROR_KINDS.has(kind) ? kind : "upstream_error";
    super(`Linear MCP ${safeKind}`);
    this.name = "OfficialLinearMcpError";
    this.kind = safeKind;
    this.status = Number.isInteger(status) && status >= 100 && status <= 599 ? status : null;
    this.retryAfterS = Number.isFinite(retryAfterS) ? Math.max(1, Math.min(3600, Math.ceil(retryAfterS))) : null;
    this.uncertain = uncertain === true;
    this.phase = ["token", "initialize", "initialized", "tools/call"].includes(phase) ? phase : "transport";
    this.rpcCode = Number.isInteger(rpcCode) ? rpcCode : null;
  }
}

function retryAfterSeconds(headers) {
  const value = headers?.get?.("retry-after");
  if (!value) return null;
  const seconds = Number(value);
  if (Number.isFinite(seconds)) return Math.max(1, Math.min(3600, seconds));
  const dateMs = Date.parse(value);
  return Number.isFinite(dateMs) ? Math.max(1, Math.min(3600, (dateMs - Date.now()) / 1000)) : null;
}

function statusKind(status) {
  if (status === 401) return "unauthorized";
  if (status === 403) return "forbidden";
  if (status === 429) return "rate_limited";
  if (status >= 300 && status < 400) return "redirect";
  return "status";
}

function timeoutSignal(timeoutMs) {
  const bounded = Number.isFinite(timeoutMs) ? Math.max(1000, Math.min(120000, timeoutMs)) : 30000;
  return AbortSignal.timeout(bounded);
}

async function readBounded(response, maxBytes) {
  const declared = Number(response.headers?.get?.("content-length"));
  if (Number.isFinite(declared) && declared > maxBytes) {
    try { await response.body?.cancel?.(); } catch { /* best effort */ }
    throw new OfficialLinearMcpError("too_large");
  }
  const reader = response.body?.getReader?.();
  if (reader) {
    const chunks = [];
    let size = 0;
    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        size += value.byteLength;
        if (size > maxBytes) {
          await reader.cancel();
          throw new OfficialLinearMcpError("too_large");
        }
        chunks.push(value);
      }
    } catch (error) {
      if (error instanceof OfficialLinearMcpError) throw error;
      throw new OfficialLinearMcpError("network");
    }
    const bytes = new Uint8Array(size);
    let offset = 0;
    for (const chunk of chunks) {
      bytes.set(chunk, offset);
      offset += chunk.byteLength;
    }
    return new TextDecoder("utf-8", { fatal: false }).decode(bytes);
  }
  try {
    const bytes = new Uint8Array(await response.arrayBuffer());
    if (bytes.byteLength > maxBytes) throw new OfficialLinearMcpError("too_large");
    return new TextDecoder("utf-8", { fatal: false }).decode(bytes);
  } catch (error) {
    if (error instanceof OfficialLinearMcpError) throw error;
    throw new OfficialLinearMcpError("network");
  }
}

function parseSse(text) {
  const messages = [];
  for (const block of text.split(/\r?\n\r?\n/)) {
    const data = block.split(/\r?\n/)
      .filter((line) => line.startsWith("data:"))
      .map((line) => line.slice(5).replace(/^ /, ""))
      .join("\n");
    if (!data || data === "[DONE]") continue;
    try { messages.push(JSON.parse(data)); } catch { /* ignore non-JSON SSE events */ }
  }
  return messages;
}

function parseRpcBody(text, contentType, id, phase) {
  let candidates;
  if (/text\/event-stream/i.test(contentType ?? "")) candidates = parseSse(text);
  else {
    try { candidates = [JSON.parse(text)]; }
    catch { throw new OfficialLinearMcpError("protocol", { phase, uncertain: phase === "tools/call" }); }
  }
  const message = candidates.find((entry) => entry && entry.jsonrpc === JSON_RPC_VERSION && entry.id === id);
  if (!message) throw new OfficialLinearMcpError("protocol", { phase, uncertain: phase === "tools/call" });
  if (message.error && typeof message.error === "object") {
    const code = Number.isInteger(message.error.code) ? message.error.code : null;
    const status = Number.isInteger(message.error.data?.status) ? message.error.data.status : null;
    throw new OfficialLinearMcpError(status ? statusKind(status) : "upstream_error", {
      phase,
      uncertain: phase === "tools/call",
      rpcCode: code,
      status,
      retryAfterS: Number.isFinite(message.error.data?.retryAfterS) ? message.error.data.retryAfterS : null,
    });
  }
  if (!message.result || typeof message.result !== "object" || Array.isArray(message.result)) {
    throw new OfficialLinearMcpError("protocol", { phase, uncertain: phase === "tools/call" });
  }
  return message.result;
}

function validateCallToolResult(value) {
  if (!value || typeof value !== "object" || Array.isArray(value) || !Array.isArray(value.content)) {
    throw new OfficialLinearMcpError("protocol", { phase: "tools/call", uncertain: true });
  }
  if (value.isError !== undefined && typeof value.isError !== "boolean") {
    throw new OfficialLinearMcpError("protocol", { phase: "tools/call", uncertain: true });
  }
  if (value.content.some((item) => !item || typeof item !== "object" || Array.isArray(item) || typeof item.type !== "string")) {
    throw new OfficialLinearMcpError("protocol", { phase: "tools/call", uncertain: true });
  }
  if (value.structuredContent !== undefined && (!value.structuredContent || typeof value.structuredContent !== "object" || Array.isArray(value.structuredContent))) {
    throw new OfficialLinearMcpError("protocol", { phase: "tools/call", uncertain: true });
  }
  return value;
}

async function post(fetchImpl, token, body, { phase, protocolVersion, sessionId, timeoutMs, write, id, maxBytes = LINEAR_MCP_MAX_RESPONSE_BYTES }) {
  let response;
  try {
    const headers = {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
      Accept: ACCEPT,
    };
    if (protocolVersion) headers["MCP-Protocol-Version"] = protocolVersion;
    if (sessionId) headers["Mcp-Session-Id"] = sessionId;
    response = await fetchImpl(LINEAR_MCP_URL, {
      method: "POST",
      headers,
      body: JSON.stringify(body),
      redirect: "manual",
      signal: timeoutSignal(timeoutMs),
    });
  } catch (error) {
    const kind = error?.name === "AbortError" || error?.name === "TimeoutError" ? "timeout" : "network";
    throw new OfficialLinearMcpError(kind, { phase, uncertain: phase === "tools/call" && write });
  }
  if (!response || response.status < 200 || response.status >= 300) {
    const status = Number.isInteger(response?.status) ? response.status : null;
    throw new OfficialLinearMcpError(status ? statusKind(status) : "network", {
      status,
      retryAfterS: retryAfterSeconds(response?.headers),
      phase,
      // Authentication and rate-limit HTTP rejections precede tool execution.
      // Tool-level errors are MCP results and remain uncertain for writes.
      uncertain: phase === "tools/call" && write && ![401, 403, 429].includes(status),
    });
  }
  let text;
  try {
    text = await readBounded(response, maxBytes);
  } catch (error) {
    if (error instanceof OfficialLinearMcpError) {
      throw new OfficialLinearMcpError(error.kind, {
        status: error.status,
        retryAfterS: error.retryAfterS,
        phase,
        uncertain: phase === "tools/call" && write,
      });
    }
    throw new OfficialLinearMcpError("network", { phase, uncertain: phase === "tools/call" && write });
  }
  return { result: parseRpcBody(text, response.headers?.get?.("content-type"), id, phase), response };
}

function asTokenError(error) {
  const kind = SAFE_ERROR_KINDS.has(error?.kind) ? error.kind : "mint";
  return new OfficialLinearMcpError(kind, {
    status: error?.status,
    retryAfterS: error?.retryAfterS,
    phase: "token",
  });
}

/** Call one allowlisted official Linear MCP tool through the seat's OAuth token. */
export async function callOfficialLinear(client, name, args, { write = false, fetch: fetchImpl, timeoutMs } = {}) {
  const upstreamName = typeof name === "string" && name.startsWith("linear_") ? name.slice("linear_".length) : name;
  const spec = LINEAR_MCP_BY_UPSTREAM_NAME[upstreamName];
  if (!spec || typeof spec.name !== "string") throw new OfficialLinearMcpError("protocol", { phase: "tools/call" });
  const isWrite = spec.write === true || write === true;
  let argumentJson;
  try { argumentJson = JSON.stringify(args ?? {}); }
  catch { throw new OfficialLinearMcpError("too_large", { phase: "tools/call" }); }
  if (!args || typeof args !== "object" || Array.isArray(args) || new TextEncoder().encode(argumentJson).byteLength > 512 * 1024) {
    throw new OfficialLinearMcpError("too_large", { phase: "tools/call" });
  }
  if (!client || typeof client.token !== "function") throw new OfficialLinearMcpError("mint", { phase: "token" });
  const selectedFetch = fetchImpl ?? client.fetch;
  if (typeof selectedFetch !== "function") throw new OfficialLinearMcpError("network", { phase: "initialize" });

  let token;
  try { token = await client.token(); }
  catch (error) { throw asTokenError(error); }
  if (typeof token !== "string" || token.length === 0) throw new OfficialLinearMcpError("mint", { phase: "token" });

  const boundedTimeout = timeoutMs ?? client.timeoutMs;
  const initializeResponse = await post(selectedFetch, token, {
    jsonrpc: JSON_RPC_VERSION,
    id: 1,
    method: "initialize",
    params: {
      protocolVersion: LINEAR_MCP_PROTOCOL_VERSION,
      capabilities: {},
      clientInfo: { name: "agent-sync-mcp", version: "2.0.0" },
    },
  }, { phase: "initialize", timeoutMs: boundedTimeout, write: isWrite, id: 1 });
  const initialized = initializeResponse.result;
  const protocolVersion = typeof initialized.protocolVersion === "string" && initialized.protocolVersion.length <= 100
    ? initialized.protocolVersion : null;
  if (!protocolVersion) throw new OfficialLinearMcpError("protocol", { phase: "initialize" });
  const rawSessionId = initializeResponse.response.headers?.get?.("mcp-session-id") ?? "";
  if (rawSessionId && (rawSessionId.length > 200 || /[\u0000-\u001f\u007f]/.test(rawSessionId))) {
    throw new OfficialLinearMcpError("protocol", { phase: "initialize" });
  }
  const sessionId = rawSessionId || null;

  let notificationResponse;
  try {
    notificationResponse = await selectedFetch(LINEAR_MCP_URL, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${token}`,
        "Content-Type": "application/json",
        Accept: ACCEPT,
        "MCP-Protocol-Version": protocolVersion,
        ...(sessionId ? { "Mcp-Session-Id": sessionId } : {}),
      },
      body: JSON.stringify({ jsonrpc: JSON_RPC_VERSION, method: "notifications/initialized" }),
      redirect: "manual",
      signal: timeoutSignal(boundedTimeout),
    });
  } catch (error) {
    const kind = error?.name === "AbortError" || error?.name === "TimeoutError" ? "timeout" : "network";
    throw new OfficialLinearMcpError(kind, { phase: "initialized" });
  }
  if (!notificationResponse || ![200, 202, 204].includes(notificationResponse.status)) {
    const status = Number.isInteger(notificationResponse?.status) ? notificationResponse.status : null;
    throw new OfficialLinearMcpError(status ? statusKind(status) : "network", {
      status,
      retryAfterS: retryAfterSeconds(notificationResponse?.headers),
      phase: "initialized",
    });
  }
  try { await notificationResponse.body?.cancel?.(); } catch { /* notification has no response body */ }

  const callResponse = await post(selectedFetch, token, {
    jsonrpc: JSON_RPC_VERSION,
    id: 2,
    method: "tools/call",
    params: { name: upstreamName, arguments: args ?? {} },
  }, { phase: "tools/call", protocolVersion, sessionId, timeoutMs: boundedTimeout, write: isWrite, id: 2 });
  return validateCallToolResult(callResponse.result);
}
