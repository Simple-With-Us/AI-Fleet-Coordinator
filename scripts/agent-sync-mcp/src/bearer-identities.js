// Canonical bearer identities are independent of OAuth client hosting.
// Inclusion names an identity only. It grants no relay repo/action, signer,
// OAuth client, delegation, or MCP consent. Missing credentials fail closed.
import { KNOWN_SEATS, SEAT_SECRETS } from "./config.js";
import { resolveSecret, infisicalMode } from "./infisical.js";

export const BEARER_ONLY_SEATS = Object.freeze({
  AG: Object.freeze({ prefix: "AG", email: "ag-bot@simplewithus.zulipchat.com" }),
  CLUTCH: Object.freeze({ prefix: "CLUTCH", email: "clutch-bot@simplewithus.zulipchat.com" }),
  FX: Object.freeze({ prefix: "FX", email: "fx-bot@simplewithus.zulipchat.com" }),
  GROK: Object.freeze({ prefix: "GROK_BUILD", email: "grok-build-bot@simplewithus.zulipchat.com" }),
  MC: Object.freeze({ prefix: "MC", email: "mc-bot@simplewithus.zulipchat.com" }),
  MM: Object.freeze({ prefix: "MM", email: "mm-bot@simplewithus.zulipchat.com" }),
  OPENCODE: Object.freeze({ prefix: "OPENCODE", email: "opencode-bot@simplewithus.zulipchat.com" }),
});
// Explicit BotFleet roles: BotFleet server/launch-identity.ts at
// 79348fc2778a8ca31ce69c612c2f9822c21652ac, cross-checked against active
// realm bot metadata on 2026-10-26. No wildcard or caller-supplied role.
export const BF_BEARER_SEATS = Object.freeze({
  "BF-BUILDER": Object.freeze({ prefix: "BF_BUILDER", email: "bf-builder-bot@simplewithus.zulipchat.com" }),
  "BF-COMPILER": Object.freeze({ prefix: "BF_COMPILER", email: "bf-compiler-bot@simplewithus.zulipchat.com" }),
  "BF-DEPLOYER": Object.freeze({ prefix: "BF_DEPLOYER", email: "bf-deployer-bot@simplewithus.zulipchat.com" }),
  "BF-DESIGNER": Object.freeze({ prefix: "BF_DESIGNER", email: "bf-designer-bot@simplewithus.zulipchat.com" }),
  "BF-DIRECTOR": Object.freeze({ prefix: "BF_DIRECTOR", email: "bf-director-bot@simplewithus.zulipchat.com" }),
  "BF-ENGINEER": Object.freeze({ prefix: "BF_ENGINEER", email: "bf-engineer-bot@simplewithus.zulipchat.com" }),
  "BF-TRADER": Object.freeze({ prefix: "BF_TRADER", email: "bf-trader-bot@simplewithus.zulipchat.com" }),
  "BF-FIXER": Object.freeze({ prefix: "BF_FIXER", email: "bf-fixer-bot@simplewithus.zulipchat.com" }),
  "BF-HOUSEKEEPER": Object.freeze({ prefix: "BF_HOUSEKEEPER", email: "bf-housekeeper-bot@simplewithus.zulipchat.com" }),
  "BF-MONITOR": Object.freeze({ prefix: "BF_MONITOR", email: "bf-monitor-bot@simplewithus.zulipchat.com" }),
  "BF-ORACLE": Object.freeze({ prefix: "BF_ORACLE", email: "bf-oracle-bot@simplewithus.zulipchat.com" }),
  "BF-PLUMBER": Object.freeze({ prefix: "BF_PLUMBER", email: "bf-plumber-bot@simplewithus.zulipchat.com" }),
  "BF-PUBLISHER": Object.freeze({ prefix: "BF_PUBLISHER", email: "bf-publisher-bot@simplewithus.zulipchat.com" }),
});
export const ADDITIONAL_BEARER_SEATS = Object.freeze({ ...BEARER_ONLY_SEATS, ...BF_BEARER_SEATS });
export const BEARER_SEATS = Object.freeze([...KNOWN_SEATS, ...Object.keys(ADDITIONAL_BEARER_SEATS)]);

// Existing OAuth seats keep their deployed hosting/key/email checks. Additional
// bearer-only seats use their own canonical Infisical records, never env keys or
// another seat's bundle. Off remains disabled; transitional fallback mode never
// permits an environment credential for these identities.
export async function resolveBearerSeatCredentials(env, config, identity) {
  // Retired Builder must not gain mintability from the fallback readiness fix.
  if (identity === "BF-BUILDER" && infisicalMode(env) !== "required") return null;
  // Unhosted CODEX and MA resolve their own Infisical /zulip records without an
  // OAuth client. A seat still listed in hostedSeats must keep the
  // deployed env-binding / botEmails path below, or minting and verify break.
  if ((identity === "CODEX" || identity === "MA") && !config.hostedSeats.includes(identity)) {
    if (infisicalMode(env) === "off") return null;
    const ownSource = { ...env, INFISICAL_MODE: "required" };
    const [key, email] = await Promise.all([
      resolveSecret(ownSource, { name: `${identity}_ZULIP_KEY`, path: "/zulip", optional: true }),
      resolveSecret(ownSource, { name: `${identity}_ZULIP_EMAIL`, path: "/zulip", optional: true }),
    ]);
    const canonicalEmail = identity === "MA" ? "muse-assist-bot@simplewithus.zulipchat.com" : "codex-bot@simplewithus.zulipchat.com";
    const nativeEmail = String(email ?? "").trim().toLowerCase();
    if (!key || nativeEmail !== canonicalEmail) return null;
    return { key, email: nativeEmail };
  }
  const row = Object.hasOwn(ADDITIONAL_BEARER_SEATS, identity) ? ADDITIONAL_BEARER_SEATS[identity] : null;
  if (row) {
    if (infisicalMode(env) === "off") return null;
    const ownSource = { ...env, INFISICAL_MODE: "required" };
    const [key, email] = await Promise.all([
      resolveSecret(ownSource, { name: `${row.prefix}_ZULIP_KEY`, path: "/zulip", optional: true }),
      resolveSecret(ownSource, { name: `${row.prefix}_ZULIP_EMAIL`, path: "/zulip", optional: true }),
    ]);
    if (!key || String(email).trim().toLowerCase() !== row.email) return null;
    return { key, email: row.email };
  }
  const names = Object.hasOwn(SEAT_SECRETS, identity) ? SEAT_SECRETS[identity] : null;
  if (!names || !config.hostedSeats.includes(identity)) return null;
  const key = String(await resolveSecret(env, { name: names.infisical.name, path: names.infisical.path, envName: names.key, optional: true }) ?? "").trim();
  const email = config.botEmails[identity];
  return key && email ? { key, email } : null;
}
