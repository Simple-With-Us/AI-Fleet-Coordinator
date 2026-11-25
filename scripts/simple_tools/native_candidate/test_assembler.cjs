'use strict';
const assert=require('assert'),{TopicWireFrameAssembler,crc32WireBytes,PROTOCOL_V4_LIMITS}=require('./native_assembler.cjs');
const {conversationTopicFrameSchema,createTopicWireFrameSchema}=require('./native_source_schemas.cjs');
const snap=require('./snapshot_fixture.json'),schema=createTopicWireFrameSchema(conversationTopicFrameSchema);
const frame={topic:'conversation/sess-fixture',subscriptionId:'own-fixture',fromSeq:0,toSeq:0,sentAt:1,payload:{kind:'snapshot',snapshot:snap}};
const base={wireVersion:3,deliveryKind:'initial',logicalFrameId:'frame-1',logicalFrameOrdinal:1,topic:frame.topic,subscriptionId:frame.subscriptionId};
let n=0;function one(name,fn){fn();console.log('PASS assembler',name);n++}
function assembler(options){return new TopicWireFrameAssembler(conversationTopicFrameSchema,options)}
function fragments(f=frame){const bytes=Buffer.from(JSON.stringify(f)),mid=Math.floor(bytes.length/2);return [bytes.subarray(0,mid),bytes.subarray(mid)].map((b,i)=>({...base,kind:'fragment',fragmentIndex:i,fragmentCount:2,logicalBytes:bytes.length,checksum:{algorithm:'crc32',value:crc32WireBytes(bytes)},dataBase64:b.toString('base64')}))}
const full={...base,kind:'complete',frame};
one('complete full source schema',()=>{schema.parse(full);assert.equal(assembler().accept(full)[0].kind,'complete')});
one('out of order fragments',()=>{const a=assembler(),f=fragments();assert.deepEqual(a.accept(f[1]),[]);assert.equal(a.accept(f[0])[0].kind,'complete');assert.equal(a.getStats().stagedDecodedBytes,0)});
one('identical fragment repeat',()=>{const a=assembler(),f=fragments();a.accept(f[0]);assert.deepEqual(a.accept(f[0]),[]);assert.equal(a.accept(f[1])[0].kind,'complete')});
one('conflicting fragment',()=>{const a=assembler(),f=fragments();a.accept(f[0]);assert.equal(a.accept({...f[0],dataBase64:Buffer.from('wrong').toString('base64')})[0].kind,'fault');assert.deepEqual(a.getStats(),{assemblies:0,stagedDecodedBytes:0})});
one('checksum mismatch',()=>{const a=assembler(),f=fragments().map(x=>({...x,checksum:{algorithm:'crc32',value:'00000000'}}));a.accept(f[0]);assert.equal(a.accept(f[1])[0].fault.reasonCode,'proto.frameAssemblyChecksumMismatch')});
one('malformed base64',()=>{assert.equal(assembler().accept({...fragments()[0],dataBase64:'!!!!'})[0].kind,'fault')});
one('timeout cleanup and tombstone',()=>{const a=assembler({timeoutMs:5}),f=fragments();a.accept(f[0],0);assert.equal(a.expire(6)[0].kind,'fault');assert.deepEqual(a.accept(f[1],7),[]);assert.equal(a.getStats().stagedDecodedBytes,0)});
one('ordinal replay and conflict',()=>{const a=assembler();a.accept(full);assert.deepEqual(a.accept(full),[]);assert.equal(a.accept({...full,logicalFrameId:'other'})[0].kind,'fault')});
one('superseding assembly fault',()=>{const a=assembler();a.accept(fragments()[0]);assert(a.accept({...full,logicalFrameOrdinal:2,logicalFrameId:'next'}).some(x=>x.kind==='fault'))});
one('recovery can replace incomplete assembly',()=>{const a=assembler();a.accept(fragments()[0]);const e=a.accept({...full,logicalFrameOrdinal:2,logicalFrameId:'next',deliveryKind:'recovery'});assert.equal(e.length,1);assert.equal(e[0].kind,'complete')});
one('budget cleanup',()=>{const a=assembler({maxStagedDecodedBytes:1});assert.equal(a.accept(fragments()[0])[0].kind,'fault');assert.equal(a.getStats().stagedDecodedBytes,0)});
one('full snapshot mandatory fields',()=>{const f=structuredClone(full);delete f.frame.payload.snapshot.control;assert.throws(()=>schema.parse(f));assert.equal(assembler().accept(f)[0].kind,'fault')});
one('null readiness invalid',()=>{const f=structuredClone(full);f.frame.payload.snapshot.runtimeInitialization=null;assert.throws(()=>schema.parse(f))});
one('invalid readiness attempt',()=>{const f=structuredClone(full);f.frame.payload.snapshot.runtimeInitialization={status:'ready'};assert.throws(()=>schema.parse(f))});
one('clear all retained state',()=>{const a=assembler();a.accept(fragments()[0]);a.clear();assert.equal(a.getStats().assemblies,0);assert.equal(a.nextExpiryAt,null)});
one('source limits',()=>{assert.equal(PROTOCOL_V4_LIMITS.maxFrameBytes,1048576);assert.equal(PROTOCOL_V4_LIMITS.logicalFrameAssemblyMaxBytes,16777216)});
console.log('TOTAL',n,'assembler tests');
