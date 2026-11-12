// gr_workflow_dispatch advertises the flat scalar inputs contract the relay and
// the worker already enforce (dispatch-inputs schema repair):  at most 32 keys,
// values string <=1024 or number or boolean.  Lives in its own file because
// gr-front.test.mjs carries a pre-existing bearer-shaped literal the write-time
// scan refuses on any full-file write (same pattern as
// gr-refused-secret-code.test.mjs);  no credential-shaped literals here.

import test from "node:test";
import assert from "node:assert/strict";
import { GR_TOOLS } from "../src/gr-tools.js";

test("gr_workflow_dispatch advertises the enforced flat scalar inputs contract", () => {
  const tool = GR_TOOLS.find((t) => t.name === "gr_workflow_dispatch");
  assert.deepEqual(tool.inputSchema.properties.inputs, {
    type: "object",
    maxProperties: 32,
    additionalProperties: {
      anyOf: [{ type: "string", maxLength: 1024 }, { type: "number" }, { type: "boolean" }],
    },
  });
});
