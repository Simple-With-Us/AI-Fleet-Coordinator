// The `guide` tool on the hosted server (AFC-331, docs/protocols/per-call-identity.md section 7).  The text is
// docs/protocols/agent-sync-tools-guide.md, bundled as src/guide-text.js (scripts/export-guide.mjs writes it;
// test/guide.test.mjs fails when it is stale).  Sections and lookup follow scripts/agent_sync/mcp/guide.py.

import { z } from "zod";
import { ToolError, okResult } from "./contract.js";
import { parseArgs } from "./args.js";

/** The guide tool's arguments:  an optional section name, nothing else. */
export const GuideArgs = z.strictObject({ section: z.string().max(100).optional() });

export const GUIDE_URL = "https://fleetlink.online/agent-sync-tools-guide/";

/** [[heading, text]] for every `## ` section, from its heading line to the next one;  fenced blocks never start one. */
export function splitSections(text) {
  const sections = [];
  let fenced = false;
  for (const line of text.split("\n")) {
    if (line.trimStart().startsWith("```")) fenced = !fenced;
    if (!fenced && line.startsWith("## ")) sections.push([line.slice(3).trim(), [line]]);
    else if (sections.length) sections.at(-1)[1].push(line);
  }
  return sections.map(([name, lines]) => [name, `${lines.join("\n").trimEnd()}\n`]);
}

export const sectionNames = (text) => splitSections(text).map(([name]) => name);

const key = (value) => [...value.toLowerCase()].filter((ch) => /[\p{L}\p{N}]/u.test(ch)).join("");

/** The whole guide, or one section matched without regard to case, spaces and punctuation. */
export function guideResult(text, args) {
  const names = sectionNames(text);
  const section = parseArgs(GuideArgs, args).section ?? "";
  if (!section.trim()) {
    return okResult(text, { section: null, sections: names, bytes: new TextEncoder().encode(text).length });
  }
  const wanted = key(section);
  const found = wanted ? splitSections(text).find(([name]) => key(name) === wanted) : null;
  if (!found) {
    throw new ToolError("not_found", `the guide has no section "${section.slice(0, 60).replaceAll('"', "'")}";  its sections are:  ${names.join(";  ") || "none"}`, { extra: { sections: names } });
  }
  return okResult(found[1], { section: found[0], sections: names, bytes: new TextEncoder().encode(found[1]).length });
}
