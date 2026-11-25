'use strict';
// Complete restricted schemas for this route only, not general app schemas.
// Optional native features not used by the adapter are deliberately refused.
function schemas(z){
 const id=z.string().min(1).max(512),command=z.string().regex(/^app-control-review-alerts-[a-f0-9]{64}$/),client=z.literal('review-alerts-router');
 const init=z.discriminatedUnion('status',[
  z.object({status:z.literal('initializing'),attemptId:id}).strict(),
  z.object({status:z.literal('ready'),attemptId:id}).strict(),
  z.object({status:z.literal('failed'),attemptId:id,reasonCode:z.string().max(256),message:z.string().max(4096)}).strict()
 ]);
 const payload=z.object({text:z.string().min(1).refine(s=>Buffer.byteLength(s)<=16000),requestedDelivery:z.literal('queue')}).strict();
 const envelope=z.object({commandId:command,clientId:client,sessionId:id,type:z.literal('sendText'),payload,issuedAt:z.number().finite()}).strict();
 const binding=z.object({host:z.literal('server'),local:z.literal(true),session_id:id,workspace:z.string().min(1).max(4096).refine(s=>s.startsWith('/')),generation:z.number().int().positive().safe()}).strict();
 const result=z.discriminatedUnion('type',[
  z.object({type:z.literal('inputAccepted'),delivery:z.enum(['startNow','queue','guide']),inputId:command,messageId:id.optional()}).strict(),
  z.object({type:z.literal('inputDisposition'),delivery:z.enum(['startNow','queue','guide'])}).strict()
 ]);
 const ack=z.object({commandId:command,status:z.enum(['accepted','rejected','stale','duplicate','noop','failed']),revisionAtDecision:z.number().int().nonnegative().safe(),reasonCode:z.string().regex(/^(fault|guard|proto)\.[A-Za-z0-9_.-]{1,120}$/).optional(),message:z.string().max(4096).optional(),result:result.optional(),memoryEnabled:z.boolean().optional(),ttftExcluded:z.literal('capacity').optional()}).strict();
 const key=z.object({sessionId:id,commandId:command}).strict();
 const query=z.object({results:z.array(z.object({key,result:z.union([ack,z.literal('unknown')]),turn:z.object({turnId:id,state:z.string().min(1).max(128)}).strict().optional()}).strict()).length(1)}).strict();
 const routing=z.object({workspace_identity:z.string().min(1).max(4096).refine(s=>s===s.trim()&&!/[\x00-\x1f]/.test(s)).nullable()}).strict();
 return {routing,binding,envelope,payload,ack,query,initialization:init};
}
module.exports={schemas};
