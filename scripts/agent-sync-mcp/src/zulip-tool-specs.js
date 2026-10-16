// Public names for the hosted Zulip tools.  The stdio tools.json remains the shared transport
// contract and, since AFC-331, already names every Zulip tool zulip_<verb>:  the hosted server
// serves exactly those names.  The old unprefixed names were removed, not aliased (owner
// 2026-10-16);  renamed-tools.json maps each to its replacement for the unknown_tool answer.
// Inside HostedTools a tool still runs as tool_<verb> (ZULIP_LEGACY_NAMES), which keeps the
// handlers, idempotency rows and audit wording unchanged.
import { TOOLS } from "./contract.js";
import { HOSTED_INBOX_DESCRIPTION } from "./dm.js";

const PREFIX = "zulip_";
// The ten tools the hosted server served first (whoami and nine Zulip tools), in their old order.
const ORIGINAL_VERBS = Object.freeze(["topics", "read_topic", "inbox", "post", "reply", "react", "dm_list", "dm_read", "dm_send"]);

// zulip_<verb> -> <verb>, for every Zulip tool in tools.json.
export const ZULIP_LEGACY_NAMES = Object.freeze(Object.fromEntries(
  TOOLS.tools.filter((tool) => tool.name.startsWith(PREFIX)).map((tool) => [tool.name, tool.name.slice(PREFIX.length)]),
));
// AFC-330:  every Zulip tool after the original nine, by its public name -> verb.
export const ZULIP_FULL_NAMES = Object.freeze(Object.fromEntries(
  Object.entries(ZULIP_LEGACY_NAMES).filter(([, verb]) => !ORIGINAL_VERBS.includes(verb)),
));

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
  "Show the identity a call acts as (the identity it names, or this connection's default) and the configured service " +
  "connections:  the seat, its verified Zulip bot identity and posting tag, the seat the OAuth grant belongs to, plus " +
  "whether Linear, fleet recall and FleetLink are configured.  It never returns credentials.";

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

function publicSpec(spec) {
  const verb = ZULIP_LEGACY_NAMES[spec.name];
  const isWhoami = spec.name === "whoami";
  const outputSchema = spec.outputSchema && isWhoami
    ? {
      ...spec.outputSchema,
      required: [...new Set([...(spec.outputSchema.required ?? []), "integrations"])],
      properties: { ...spec.outputSchema.properties, authenticated_seat: { type: "string" }, integrations: WHOAMI_INTEGRATIONS },
    }
    : spec.outputSchema;
  return {
    ...spec,
    title: isWhoami ? "Who Am I" : `Zulip ${TITLE_WORDS[verb] ?? spec.title}`,
    description: isWhoami ? WHOAMI_DESCRIPTION : (verb === "inbox" ? HOSTED_INBOX_DESCRIPTION : spec.description),
    ...(outputSchema ? { outputSchema } : {}),
  };
}

const original = ["whoami", ...ORIGINAL_VERBS.map((verb) => `${PREFIX}${verb}`)];
export const ZULIP_PUBLIC_TOOLS = Object.freeze(original.map((name) => publicSpec(TOOLS.tools.find((tool) => tool.name === name))));
// Listed after the Linear tools, so the ten original Zulip tools keep their place in tools/list.
export const ZULIP_FULL_PUBLIC_TOOLS = Object.freeze(TOOLS.tools.filter((tool) => Object.hasOwn(ZULIP_FULL_NAMES, tool.name)).map(publicSpec));
export const ZULIP_PUBLIC_BY_NAME = Object.freeze(Object.fromEntries([...ZULIP_PUBLIC_TOOLS, ...ZULIP_FULL_PUBLIC_TOOLS].map((tool) => [tool.name, tool])));
// The shared `identities` spec (tools.json);  the per-connection router serves it, not HostedTools.
export const IDENTITIES_SPEC = Object.freeze(TOOLS.tools.find((tool) => tool.name === "identities"));
