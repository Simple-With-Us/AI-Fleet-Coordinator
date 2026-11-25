'use strict';
const fs=require('fs'),path=require('path'),crypto=require('crypto'),acorn=require('./vendor/acorn/dist/acorn.js');
const input=process.argv[2],dir=process.argv[3]||__dirname;
if(!input)throw Error('usage: node extract_scope.cjs PATH_TO_PINNED_SERVER_CORE [OUTPUT_DIRECTORY]');
const s=fs.readFileSync(input,'utf8'),hash=x=>crypto.createHash('sha256').update(x).digest('hex');
if(hash(s)!=='ab42d8c5fb2085a1539876a5c40da54e67e2abd71a222ed5bd31b622898984c0')throw Error('source-hash');
const ast=acorn.parse(s,{ecmaVersion:'latest',sourceType:'module'});
const groups={
 'installed_connection_scope.js':['createZCodeAgentConnectionScope','readTrustedZCodeAgentV4UnsubscribeRoute','readTrustedZCodeAgentV4Connection','withTrustedConnection','withTrustedUnsubscribeRoute','carriesExtensionInputSource','workspaceKey','workspaceTarget','MAX_PENDING_OWNERSHIP_FRAMES','MAX_PENDING_OWNERSHIP_BYTES','MAX_DOWNSTREAM_CONNECTION_ID_LENGTH','assertConnectionId','namespaceRelayConnectionId','frameBytes','deliveryProfileFor','TRUSTED_CONNECTION_FIELD','TRUSTED_UNSUBSCRIBE_ROUTE_FIELD'],
 'installed_socket_scope.js':['exposeWebSocket']};
const outputs={};
for(const [file,names]of Object.entries(groups)){
 const wanted=new Set(names),decls=[];
 for(const n of ast.body){const ids=n.type==='VariableDeclaration'?n.declarations.map(d=>d.id.name):[n.id?.name];const matched=ids.filter(x=>wanted.has(x));if(matched.length)decls.push({names:matched,start:n.start,end:n.end,sha256:hash(s.slice(n.start,n.end))})}
 if(new Set(decls.flatMap(d=>d.names)).size!==wanted.size)throw Error('missing-declaration');
 const generated="// Exact pinned source declarations. Fixture injects external dependencies.\n"+decls.map(d=>s.slice(d.start,d.end)).join('\n')+(file==='installed_socket_scope.js'?'\nglobalThis.expose=exposeWebSocket;\n':'\nglobalThis.makeScope=createZCodeAgentConnectionScope;\n');
 fs.writeFileSync(path.join(dir,file),generated);outputs[file]={generated_sha256:hash(generated),declarations:decls};
}
fs.writeFileSync(path.join(dir,'scope-provenance.json'),JSON.stringify({source_sha256:hash(s),offset_units:'JavaScript UTF16 code units',outputs},null,2)+'\n');
