const fs=require('fs'),acorn=require('./vendor/acorn/dist/acorn.js'),crypto=require('crypto');
const sourcePath=process.argv[2];if(!sourcePath)throw Error('usage: node extract_source_schemas.cjs PATH_TO_PINNED_SERVER_CORE [OUTPUT_DIRECTORY]');const outDir=process.argv[3]||__dirname;
const s=fs.readFileSync(sourcePath,'utf8');if(crypto.createHash('sha256').update(s).digest('hex')!=='ab42d8c5fb2085a1539876a5c40da54e67e2abd71a222ed5bd31b622898984c0')throw Error('hash');
const ast=acorn.parse(s,{ecmaVersion:'latest',sourceType:'module'}),defs=new Map(),imports=new Map();
for(const n of ast.body){if(n.type==='VariableDeclaration')for(const d of n.declarations)if(d.id.type==='Identifier')defs.set(d.id.name,{start:n.start,end:n.end,node:n});if(n.type==='FunctionDeclaration'||n.type==='ClassDeclaration')defs.set(n.id.name,{start:n.start,end:n.end,node:n});if(n.type==='ImportDeclaration')for(const sp of n.specifiers)imports.set(sp.local.name,[n.source.value,sp.imported?.name]);}
console.log([...defs.keys()].filter(n=>/conversation.*FrameSchema|conversation.*Topic|topic.*FrameSchema/i.test(n)));
const names=['conversationSnapshotSchema','conversationDeltaSchema','createTopicWireFrameSchema','conversationTopicFrameSchema','helloMessageSchema2','clientHelloSchema','v4ConversationSubscribeResultSchema','commandAckSchema','commandsQueryResultSchema'];let pending=[...names],used=new Map(),ext=new Map();
function walk(n,parent,key){if(!n||typeof n!=='object')return;if(n.type==='Identifier'){
 if(parent&&((parent.type==='MemberExpression'&&key==='property'&&!parent.computed)||(parent.type==='Property'&&key==='key'&&!parent.computed)||(parent.type==='VariableDeclarator'&&key==='id')||(['FunctionDeclaration','ClassDeclaration'].includes(parent.type)&&key==='id')))return;
 if(defs.has(n.name)&&!used.has(n.name))pending.push(n.name);if(imports.has(n.name))ext.set(n.name,imports.get(n.name));
 }for(const [k,v]of Object.entries(n)){if(['start','end','type'].includes(k))continue;if(Array.isArray(v))v.forEach(x=>walk(x,n,k));else if(v&&typeof v==='object')walk(v,n,k)}}
while(pending.length){const name=pending.pop();if(used.has(name))continue;const d=defs.get(name);if(!d)throw Error(name);used.set(name,d);walk(d.node)}
console.log('defs',used.size,'imports',JSON.stringify([...ext]));
const out=[...new Map([...used.values()].map(d=>[d.start,d])).values()].sort((a,b)=>a.start-b.start).map(d=>s.slice(d.start,d.end));
const prefix="'use strict';\n"+[...ext].map(([name,[mod,imp]])=>mod==='zod'?`const ${name}=require('./offline_node_modules/zod');`:`const ${name}=require(${JSON.stringify(mod)})[${JSON.stringify(imp)}];`).join('\n');
const generated=prefix+'\n'+out.join('\n')+'\nmodule.exports={conversationSnapshotSchema,conversationDeltaSchema,createTopicWireFrameSchema,conversationTopicFrameSchema,helloMessageSchema2,clientHelloSchema,v4ConversationSubscribeResultSchema,commandAckSchema,commandsQueryResultSchema};\n';
fs.writeFileSync(require('path').join(outDir,'native_source_schemas.cjs'),generated);
const manifest={source_sha256:crypto.createHash('sha256').update(s).digest('hex'),offset_units:'JavaScript UTF16 code units, not UTF8 byte offsets',generated_sha256:crypto.createHash('sha256').update(generated).digest('hex'),declarations:[...used].map(([name,d])=>({name,start:d.start,end:d.end,sha256:crypto.createHash('sha256').update(s.slice(d.start,d.end)).digest('hex')}))};
fs.writeFileSync(require('path').join(outDir,'schema-provenance.json'),JSON.stringify(manifest,null,2)+'\n');
