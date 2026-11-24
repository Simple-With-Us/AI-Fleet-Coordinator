import json,re
from pathlib import Path
root=Path(__file__).resolve().parent
def read(n):return json.loads((root/n).read_text())
a=read('wire-assembler-exact1045.json')['source'].replace('import "zod";','')
h=read('wire-helpers1046.json');d=read('wire-dependencies1046.json');f=read('schema-full1056.json')
limits=f[0]['text'];limits=limits[:limits.index('\nvar v4ColdResumeWorkspaceSchema')].replace('VIDEO_INPUT_MAX_BYTES','(30*1024*1024)')
base=d[3]['text'].split('function crc32WireBytes')[0].replace('z35.','z.')
crc=h[0]['text'].split('// ../shared/src/web-remote-control-rpc-transport.ts')[0]
measure=d[0]['text'].split('function measureTopicNotificationEnvelopeBytes')[0]+h[2]['text'].split('function encodeJson')[0]
(root/'native_assembler.cjs').write_text("'use strict';\nconst z=require('./offline_node_modules/zod');\n"+limits+'\n'+base+crc+measure+'\nconst WIRE_FAULT_INVALID_PAYLOAD="proto.frameAssemblyInvalidPayload";\n'+a+'\nmodule.exports={TopicWireFrameAssembler,PROTOCOL_V4_LIMITS,crc32WireBytes,encodeWireBytesBase64,measureTopicNotificationEnvelopeBytes};\n')
