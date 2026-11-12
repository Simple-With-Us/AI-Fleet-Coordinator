// Compact default for seat-mcp list/search read tools (AFC-756). Lossless when full=true.

export const COMPACT_DEFAULT_LIMIT = 10;
export const COMPACT_BODY_LIMIT = 280;
export const FULL_ZULIP_BODY_LIMIT = 4000;

const TEXT_FIELDS = new Set(["body", "content", "description", "summary", "text", "subtitle", "message", "markdown", "renderedBody"]);

export const LINEAR_LIST_SEARCH_TOOLS = new Set(["linear_list", "linear_query"]);
export const LINEAR_LIST_SEARCH_PREFIX = "linear_list_";
export const ZULIP_MESSAGE_READ_TOOLS = new Set([
  "zulip_read_topic",
  "zulip_inbox",
  "zulip_dm_read",
  "zulip_get_message",
  "zulip_search",
  "zulip_message_history",
]);
export const NOTES_LIST_TOOLS = new Set(["notes_list"]);

export function splitFullArg(args, rawArgs = null) {
  const work = { ...args };
  const full = work.full === true;
  delete work.full;
  const limitProvided =
    rawArgs != null &&
    typeof rawArgs === "object" &&
    !Array.isArray(rawArgs) &&
    Object.prototype.hasOwnProperty.call(rawArgs, "limit");
  return { args: work, full, limitProvided };
}

export function effectiveLimit(args, { schemaDefault, full, limitProvided = false }) {
  if (limitProvided) return args.limit;
  if (full) return schemaDefault;
  return Math.min(schemaDefault, COMPACT_DEFAULT_LIMIT);
}

export function truncateText(text, limit) {
  if (limit <= 0 || text.length <= limit) return [text, false];
  let cut = text.slice(0, limit).trimEnd();
  if (!cut.endsWith("...")) cut += "...";
  return [cut, true];
}

export function approximateTokens(value) {
  const encoded = JSON.stringify(value);
  return Math.max(1, Math.ceil((encoded.length + 3) / 4));
}

function compactStringFields(value, limit, depth = 0) {
  if (depth > 24) return value;
  if (typeof value === "string") return truncateText(value, limit)[0];
  if (Array.isArray(value)) return value.map((item) => compactStringFields(item, limit, depth + 1));
  if (value && typeof value === "object") {
    const out = {};
    for (const [key, child] of Object.entries(value)) {
      if (TEXT_FIELDS.has(key) && typeof child === "string") {
        const [text, truncated] = truncateText(child, limit);
        out[key] = text;
        if (truncated) out[`${key}_truncated`] = true;
      } else {
        out[key] = compactStringFields(child, limit, depth + 1);
      }
    }
    return out;
  }
  return value;
}

function sliceNodes(nodes, topN) {
  if (nodes.length <= topN) return [nodes, false];
  return [nodes.slice(0, topN), true];
}

export function compactLinearGraphql(data, topN = COMPACT_DEFAULT_LIMIT) {
  const meta = { compact: true };
  if (!data || typeof data !== "object") {
    return { data: compactStringFields(data, COMPACT_BODY_LIMIT), meta };
  }
  const walk = (value) => {
    if (Array.isArray(value)) {
      const [trimmed, cut] = sliceNodes(value, topN);
      if (cut) meta.truncated = true;
      return trimmed.map(walk);
    }
    if (value && typeof value === "object") {
      const out = {};
      for (const [key, child] of Object.entries(value)) {
        if (key === "nodes" && Array.isArray(child)) {
          const [trimmed, cut] = sliceNodes(child, topN);
          if (cut) {
            meta.truncated = true;
            out.nodes_omitted = child.length - trimmed.length;
          }
          out.nodes = trimmed.map(walk);
        } else if (key === "edges" && Array.isArray(child)) {
          const [trimmed, cut] = sliceNodes(child, topN);
          if (cut) {
            meta.truncated = true;
            out.edges_omitted = child.length - trimmed.length;
          }
          out.edges = trimmed.map(walk);
        } else {
          out[key] = walk(child);
        }
      }
      return compactStringFields(out, COMPACT_BODY_LIMIT);
    }
    return value;
  };
  return { data: walk(data), meta };
}

export function toolWantsCompact(name, full) {
  if (full) return false;
  if (ZULIP_MESSAGE_READ_TOOLS.has(name) || NOTES_LIST_TOOLS.has(name)) return true;
  if (LINEAR_LIST_SEARCH_TOOLS.has(name) || name.startsWith(LINEAR_LIST_SEARCH_PREFIX)) return true;
  if (name === "linear_search_documentation") return true;
  return false;
}

export function compactNotesListResult(result, topN = COMPACT_DEFAULT_LIMIT) {
  const out = { ...result };
  const notes = Array.isArray(out.notes) ? out.notes : [];
  const [trimmed, cut] = sliceNodes(notes, topN);
  out.notes = trimmed;
  if (cut) out.compact_truncated = true;
  return out;
}
