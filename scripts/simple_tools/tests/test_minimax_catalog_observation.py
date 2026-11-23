"""Hermetic catalog-observation contract and no-discovery-change tests."""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from simple_tools import minimax_catalog_observation as obs
from simple_tools import minimax_origin_bridge as bridge


class Observation(unittest.TestCase):
    def evaluate(self, body, env="1"):
        script = obs.HELPER + "\n" + body
        done = subprocess.run(["node", "--input-type=module"], input=script, text=True,
                              capture_output=True, check=True,
                              env={**os.environ, "AFC_MM_CATALOG_SHAPE": env})
        return done.stdout, [json.loads(x) for x in done.stderr.splitlines()]

    def test_default_off_reads_no_snapshot_and_does_no_output(self):
        stdout, lines = self.evaluate('afcMmCatalogShape("capture",new Proxy({}, {get(){throw Error("read")}}));', "")
        self.assertEqual((stdout, lines), ("", []))
        self.assertEqual(self.evaluate('afcMmCatalogShape("capture",null);'), ("", []))

    def test_publication_and_capture_are_separate_fixed_shape_records(self):
        stdout, lines = self.evaluate('''
const server={pluginName:"fleet-own-tools",server:{name:"simple-tools"}}, binding={kind:"mcp",pluginName:"fleet-own-tools",source:"simple-tools",mcpToolName:"identities",private:"DO_NOT_EMIT"};
const snap={revision:"plugin-snapshot-7",mcpServers:[server],diagnostics:[],turnCapabilities:{runtimeTools:[{}],runtimeToolBindings:[binding]}};
afcMmCatalogShape("publication",snap); afcMmCatalogShape("capture",snap);
''')
        self.assertEqual(stdout, "")
        self.assertEqual([x["phase"] for x in lines], ["publication", "capture"])
        wanted = {"event", "version", "phase", "snapshot_sequence", "total_runtime_tools",
                  "fleet_server_count", "identities_binding_count", "warming", "failed", "catalog_ready"}
        for item in lines:
            self.assertEqual(set(item), wanted)
            self.assertTrue(item["catalog_ready"])
            self.assertEqual(item["snapshot_sequence"], 7)
            self.assertNotIn("DO_NOT_EMIT", json.dumps(item))

    def test_missing_duplicate_warming_failed_never_ready(self):
        _, lines = self.evaluate('''
const server={pluginName:"fleet-own-tools",server:{name:"simple-tools"}}, binding={kind:"mcp",pluginName:"fleet-own-tools",source:"simple-tools",mcpToolName:"identities"};
for(const [servers,bindings,code] of [[[],[],null],[[server],[],null],[[server,server],[binding],null],[[server],[binding,binding],null],[[server],[binding],"MCP_SERVER_WARMING"],[[server],[binding],"MCP_SERVER_LIST_FAILED"]]) {
 afcMmCatalogShape("capture",{revision:"PRIVATE_UNEXPECTED",mcpServers:servers,diagnostics:code?[{pluginName:"fleet-own-tools",capabilityName:"simple-tools",code}]:[],turnCapabilities:{runtimeTools:[],runtimeToolBindings:bindings}});
}
''')
        self.assertEqual(len(lines), 6)
        self.assertTrue(all(not x["catalog_ready"] for x in lines))
        self.assertTrue(all(x["snapshot_sequence"] is None for x in lines))

    def test_capture_returns_identical_projection_and_no_tool_changes(self):
        body = '''
let events=[]; const tIe=x=>(events.push("project"),x); class Runtime {
assertUsable(){events.push("assert")}
CAPTURE
}
const runtime=new Runtime();runtime.snapshot={revision:"plugin-snapshot-1",mcpServers:[],diagnostics:[],turnCapabilities:{runtimeTools:[],runtimeToolBindings:[]}};
console.log(JSON.stringify({same:runtime.captureTurnCapabilities()===runtime.snapshot.turnCapabilities,events}));
'''.replace("CAPTURE", obs.CAPTURE_NEW)
        stdout, lines = self.evaluate(body)
        self.assertEqual(json.loads(stdout), {"same": True, "events": ["assert", "project"]})
        self.assertEqual(lines[0]["phase"], "capture")

    def test_installer_exact_base_idempotent_drift_and_rollback(self):
        base = (obs.HELPER_ANCHOR + "}\n" + obs.CAPTURE_OLD + "\n" + obs.PUBLISH_OLD).encode()
        with tempfile.TemporaryDirectory() as temp, mock.patch.object(obs, "BASE_SHA256", bridge.digest(base)):
            root = Path(temp)
            (root / "chunks").mkdir()
            (root / "package.json").write_text(json.dumps({"name": "@minimax-ai/code", "version": "0.7.4"}))
            path = root / bridge.CHUNK
            path.write_bytes(base)
            obs.run(root)
            self.assertEqual(path.read_bytes(), base)
            obs.run(root, apply=True)
            expected = path.read_bytes()
            obs.run(root, apply=True)
            self.assertEqual(path.read_bytes(), expected)
            path.write_bytes(expected + b"drift")
            with self.assertRaises(ValueError):
                obs.run(root, rollback=True, apply=True)
            path.write_bytes(expected)
            obs.run(root, rollback=True, apply=True)
            self.assertEqual(path.read_bytes(), base)
            obs.run(root, rollback=True, apply=True)

    def test_hash_and_anchor_drift_refused(self):
        with self.assertRaises(ValueError):
            obs.patched(b"anything")
        data = b"no anchors"
        with mock.patch.object(obs, "BASE_SHA256", bridge.digest(data)):
            with self.assertRaises(ValueError):
                obs.patched(data)


if __name__ == "__main__":
    unittest.main()
