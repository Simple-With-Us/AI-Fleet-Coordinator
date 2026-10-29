// Shared by OAuth and both bearer routes. Unrelated accepted sizes stay 256 KiB.
// The bounded read/parse budget for rejected non-file requests is intentionally
// larger too: the tool name can legally follow arguments in a JSON object.
import { readBodyBytes } from './wake.js';
export const MCP_DEFAULT_BYTES = 262144;
export const FILE_ARGS_BYTES = 6 * 1024 * 1024;
export const MCP_FILE_BYTES = FILE_ARGS_BYTES + 65536;
const enc = new TextEncoder();
export async function boundedMcpRequest(request) {
  if (request.method !== 'POST') return request;
  const bytes = await readBodyBytes(request, MCP_FILE_BYTES);
  if (bytes === null) return new Response('Payload Too Large', {status:413});
  if (bytes.byteLength > MCP_DEFAULT_BYTES) {
    let body;
    try { body = JSON.parse(new TextDecoder('utf-8',{fatal:true}).decode(bytes)); }
    catch { return new Response('Payload Too Large',{status:413}); }
    const object = v => v !== null && typeof v === 'object' && !Array.isArray(v);
    if (!object(body) || body.jsonrpc !== '2.0' || body.method !== 'tools/call' ||
        !object(body.params) || !['gr_wf_file_put','gr_wf_files_put'].includes(body.params.name) ||
        !object(body.params.arguments) ||
        Object.keys(body).some(k=>!['jsonrpc','id','method','params'].includes(k)) ||
        Object.keys(body.params).some(k=>!['name','arguments','_meta'].includes(k)))
      return new Response('Payload Too Large',{status:413});
    let argsBytes;
    try { argsBytes=enc.encode(JSON.stringify(body.params.arguments)).byteLength; }
    catch { return new Response('Payload Too Large',{status:413}); }
    if(argsBytes > FILE_ARGS_BYTES || bytes.byteLength-argsBytes>65536)
      return new Response('Payload Too Large',{status:413});
  }
  return new Request(request.url,{method:'POST',headers:request.headers,body:bytes,cf:request.cf});
}
