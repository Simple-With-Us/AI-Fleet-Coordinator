import test from "node:test";
import assert from "node:assert/strict";
import { readBoundedBody, BodyLimitError } from "./body.js";
test("bounds actual bytes, not characters or declared length", async () => {
  const request = body => new Request("https://test", {method:"POST", body});
  assert.equal(new TextDecoder().decode(await readBoundedBody(request("éé"),4)), "éé");
  await assert.rejects(readBoundedBody(request("ééé"),4), BodyLimitError);
  await assert.rejects(readBoundedBody(new Request("https://test", {method:"POST",headers:{"content-length":"1"},body:"abcdef"}),4),BodyLimitError);
});
test("oversized streams stop and cancel before draining", async () => {
  let pulled=0, canceled=false;
  const stream=new ReadableStream({pull(c){pulled++;c.enqueue(new Uint8Array(3));},cancel(){canceled=true;}},{highWaterMark:0});
  await assert.rejects(readBoundedBody(new Request("https://test", {method:"POST",body:stream,duplex:"half"}),4),BodyLimitError);
  assert.equal(pulled,2);assert.equal(canceled,true);
});
test("declared oversize never reads body", async () => {
  let pulled=0;
  const stream=new ReadableStream({pull(){pulled++;}},{highWaterMark:0});
  await assert.rejects(readBoundedBody(new Request("https://test", {method:"POST",headers:{"content-length":"5"},body:stream,duplex:"half"}),4),BodyLimitError);
  assert.equal(pulled,0);
});
