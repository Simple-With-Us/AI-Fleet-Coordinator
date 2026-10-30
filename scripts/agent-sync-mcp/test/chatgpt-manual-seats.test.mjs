import test from "node:test";
import assert from "node:assert/strict";
import { loadConfig } from "../src/config.js";
import { preGateAuthorize, postGateAuthRequest, resolveArmedSeat, clientBoundToSeat } from "../src/policy.js";
import { testEnv, authorizeParams, CHATGPT_CLIENT, CHATGPT_REDIRECT, RESOURCE, CHALLENGE } from "./helpers.mjs";

function config(extra = {}) {
  return loadConfig(testEnv({ HOSTED_SEATS: "JET,CODEX", ZULIP_EMAIL_CODEX: "codex-bot@simplewithus.zulipchat.com", SEATS: {
    JET: { redirect_uris: [CHATGPT_REDIRECT], cimd_client_ids: [CHATGPT_CLIENT] },
    CODEX: { redirect_uris: [CHATGPT_REDIRECT], cimd_client_ids: [] },
  }, ...extra }));
}
const own = { clientId: "CodeXManual123456", clientName: "Codex (manual form, CODEX)" };
const oldJet = { clientId: CHATGPT_CLIENT, clientName: "ChatGPT" };

test("only the exact JET/CODEX HTTPS callback pair can share, independent of seat order", () => {
  const cfg = config();
  assert.deepEqual(cfg.redirectSeats.get(CHATGPT_REDIRECT), ["JET", "CODEX"]);
  assert.equal(cfg.redirectOwner.has(CHATGPT_REDIRECT), false);
  assert.equal(cfg.cimdOwner.get(CHATGPT_CLIENT), "JET");
  const seats = Object.fromEntries(Object.entries({CODEX: {redirect_uris:[CHATGPT_REDIRECT],cimd_client_ids:[]}, JET: {redirect_uris:[CHATGPT_REDIRECT],cimd_client_ids:[CHATGPT_CLIENT]}}));
  assert.deepEqual(config({SEATS:seats}).redirectSeats.get(CHATGPT_REDIRECT), ["CODEX", "JET"]);
  for (const uri of ["https://chatgpt.com/other", "https://chatgpt.com/connector_platform_oauth_redirect/", "https://evil.example/callback"]) {
    assert.throws(() => config({SEATS:{JET:{redirect_uris:[uri]},CODEX:{redirect_uris:[uri]}}}), /two seats/);
  }
  for (const foreign of ["JET-CHAT", "ECHO", "WISE", "GROK-WEB"]) {
    assert.throws(() => config({SEATS:{JET:{redirect_uris:[CHATGPT_REDIRECT]},CODEX:{redirect_uris:[CHATGPT_REDIRECT]},[foreign]:{redirect_uris:[CHATGPT_REDIRECT]}}}), /two seats/);
  }
  assert.throws(() => config({SEATS:{JET:{redirect_uris:[CHATGPT_REDIRECT],cimd_client_ids:[CHATGPT_CLIENT]},CODEX:{redirect_uris:[CHATGPT_REDIRECT],cimd_client_ids:[CHATGPT_CLIENT]}}}), /CIMD client id.*two seats/);
});

test("shared HTTPS keeps exactly-one-armed and own-client boundaries", () => {
  const cfg = config();
  const params = authorizeParams({ client_id: own.clientId, redirect_uri: CHATGPT_REDIRECT });
  const pre = preGateAuthorize(params, cfg);
  assert.equal(pre.ok, true); assert.deepEqual(pre.seats, ["JET", "CODEX"]);
  assert.equal(resolveArmedSeat(pre.seats, new Set()).reason, "not_armed");
  assert.equal(resolveArmedSeat(pre.seats, new Set(["JET", "CODEX"])).reason, "ambiguous_armed_seats");
  assert.deepEqual(resolveArmedSeat(pre.seats, new Set(["CODEX"])), {ok:true,seat:"CODEX"});
  assert.equal(clientBoundToSeat(own, "CODEX", CHATGPT_REDIRECT, cfg), true);
  assert.equal(clientBoundToSeat(own, "JET", CHATGPT_REDIRECT, cfg), false);
  assert.equal(clientBoundToSeat(oldJet, "JET", CHATGPT_REDIRECT, cfg), true);
  assert.equal(clientBoundToSeat(oldJet, "CODEX", CHATGPT_REDIRECT, cfg), false);
  assert.equal(clientBoundToSeat({...own,clientName:"untagged"}, "CODEX", CHATGPT_REDIRECT, cfg), false);
  assert.equal(clientBoundToSeat({...oldJet,clientName:own.clientName}, "CODEX", CHATGPT_REDIRECT, cfg), false);
  const request={clientId:own.clientId,redirectUri:CHATGPT_REDIRECT,codeChallenge:CHALLENGE,codeChallengeMethod:"S256",resource:RESOURCE,scope:["zulip:read"]};
  assert.equal(postGateAuthRequest(request,cfg,"CODEX"),true);
  assert.equal(postGateAuthRequest({...request,clientId:CHATGPT_CLIENT},cfg,"CODEX"),false);
  assert.equal(postGateAuthRequest({...request,resource:"https://evil.example/mcp"},cfg,"CODEX"),false);
  assert.equal(postGateAuthRequest({...request,codeChallengeMethod:"plain"},cfg,"CODEX"),false);
  assert.equal(postGateAuthRequest({...request,redirectUri:"https://chatgpt.com/other"},cfg,"CODEX"),false);
  assert.equal(postGateAuthRequest({...request,scope:["unknown"]},cfg,"CODEX"),false);
});
