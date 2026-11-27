'use strict';
// Authorized pinned installed source is required. Never runs application code.
const fs=require('fs'),path=require('path'),cp=require('child_process'),crypto=require('crypto');
const source=process.argv[2];if(!source)throw Error('usage: node prepare_candidate.cjs PATH_TO_AUTHORIZED_PINNED_SERVER_CORE');
const root=__dirname,hash=x=>crypto.createHash('sha256').update(x).digest('hex');
if(hash(fs.readFileSync(source))!=='ab42d8c5fb2085a1539876a5c40da54e67e2abd71a222ed5bd31b622898984c0')throw Error('source-hash');
cp.execFileSync(process.execPath,[path.join(root,'extract_source_schemas.cjs'),path.resolve(source),root],{stdio:'inherit'});
cp.execFileSync(process.execPath,[path.join(root,'verify_provenance.cjs'),path.resolve(source)],{stdio:'inherit'});
console.log('Generated candidate schemas/provenance from qualified source. No native connection or activation.');
