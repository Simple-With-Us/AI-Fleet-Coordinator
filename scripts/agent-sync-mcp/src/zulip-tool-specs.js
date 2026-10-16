// Public names for the hosted Zulip tools.  The stdio tools.json remains the
// shared transport contract; this module only shapes the hosted tools/list view.
import { TOOLS } from "./contract.js";
import { DM_TOOLS, HOSTED_INBOX_DESCRIPTION } from "./dm.js";

const ZULIP_NAMES = Object.freeze({
  topics: "zulip_topics",
  read_topic: "zulip_read_topic",
  inbox: "zulip_inbox",
  post: "zulip_post",
  reply: "zulip_reply",
  react: "zulip_react",
  dm_list: "zulip_dm_list",
  dm_read: "zulip_dm_read",
  dm_send: "zulip_dm_send",
});

const LEGACY_NAMES = Object.freeze(Object.fromEntries(Object.entries(ZULIP_NAMES).map(([legacy, canonical]) => [legacy, canonical])));
const TITLE_WORDS = {
  topics: "Topics",
  read_topic: "Read Topic",
  inbox: "Inbox",
  post: "Post",
  reply: "Reply",
  react: "React",
  dm_list: "DM List",
  dm_read: "DM Read",
  dm_send: "DM Send",
};

const WHOAMI_DESCRIPTION =
  "Show the identity and configured service connections for this endpoint:  the seat, its existing verified Zulip bot " +
  "identity and posting tag, plus whether Linear, fleet recall and FleetLink are configured.  This is connection-wide identity " +
  "metadata; it never returns credentials.";

const INTEGRATION_STATE = {
  type: "object",
  additionalProperties: false,
  required: ["configured", "state"],
  properties: {
    configured: { type: "boolean" },
    state: { type: "string", enum: ["configured", "unconfigured", "unavailable"] },
  },
};

const WHOAMI_INTEGRATIONS = {
  type: "object",
  additionalProperties: false,
  required: ["zulip", "linear", "recall", "fleetlink"],
  properties: {
    zulip: { ...INTEGRATION_STATE, description: "The seat's verified Zulip identity and connection state." },
    linear: { ...INTEGRATION_STATE, description: "Whether the hosted Linear service is configured." },
    recall: { ...INTEGRATION_STATE, description: "Whether the fleet recall service is configured." },
    fleetlink: { ...INTEGRATION_STATE, description: "Whether FleetLink (fleetlink_* tools) is configured." },
  },
};

function replaceToolReferences(description) {
  return description
    .replace(/\b(dm_list|dm_read|dm_send)\b/g, (name) => ZULIP_NAMES[name])
    .replace(/\brules of post apply\b/g, "rules of zulip_post apply")
    .replace(/\buse post for\b/g, "use zulip_post for");
}

function publicSpec(spec) {
  const legacyName = spec.name;
  const name = ZULIP_NAMES[legacyName];
  const isWhoami = legacyName === "whoami";
  const outputSchema = spec.outputSchema && isWhoami
    ? {
      ...spec.outputSchema,
      required: [...new Set([...(spec.outputSchema.required ?? []), "integrations"])],
      properties: { ...spec.outputSchema.properties, integrations: WHOAMI_INTEGRATIONS },
    }
    : spec.outputSchema;
  return {
    ...spec,
    name: name ?? legacyName,
    title: isWhoami ? "Who Am I" : `Zulip ${TITLE_WORDS[legacyName]}`,
    description: isWhoami ? WHOAMI_DESCRIPTION : (legacyName === "inbox" ? HOSTED_INBOX_DESCRIPTION : replaceToolReferences(spec.description)),
    ...(outputSchema ? { outputSchema } : {}),
  };
}

const hostedSpecs = [
  ...TOOLS.tools.filter((tool) => tool.name === "whoami" || Object.hasOwn(ZULIP_NAMES, tool.name)),
  ...DM_TOOLS,
].map(publicSpec);

export const ZULIP_PUBLIC_TOOLS = Object.freeze(hostedSpecs);
export const ZULIP_PUBLIC_BY_NAME = Object.freeze(Object.fromEntries(ZULIP_PUBLIC_TOOLS.map((tool) => [tool.name, tool])));
// Incoming legacy names remain dispatchable, but callers advertise only the public names above.
export const ZULIP_TOOL_ALIASES = Object.freeze(LEGACY_NAMES);
