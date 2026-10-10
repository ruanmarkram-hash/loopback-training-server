import {mkdir,open,readFile,rename,stat,unlink} from 'node:fs/promises';
import {createHash} from 'node:crypto';
import path from 'node:path';
import {CliCoach,validateRequest,validateOutput,acquireHostLock,restrictTaskResult} from './adapter.mjs';
import {IdentityGuard} from './identity.mjs';
const sha=value=>createHash('sha256').update(value).digest('hex');
const exact=(o,keys)=>o&&typeof o==='object'&&!Array.isArray(o)&&Object.keys(o).length===keys.length&&keys.every(k=>Object.hasOwn(o,k));
const uuid=/^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i;
const failures=new Set(['busy','idempotency_conflict','interrupted','budget_exceeded','auth_expired','quota','malformed','policy_rejected','transient','timeout','cancelled','output_limit','capability_violation','termination_failed','evidence_failed']);
const halt=new Set(['idempotency_conflict','budget_exceeded','auth_expired','quota','malformed','capability_violation','termination_failed','evidence_failed','cancelled']);
export async function privateJson(file,max=65536){const info=await stat(file);if(!info.isFile()||info.size>max||(info.mode&0o077)!==0||(process.getuid&&info.uid!==process.getuid()))throw new Error('unsafe_private_file');return JSON.parse(await readFile(file,'utf8'));}
async function durableJson(file,value){const temporary=file+'.tmp';const handle=await open(temporary,'w',0o600);try{await handle.writeFile(JSON.stringify(value));await handle.sync();}finally{await handle.close();}await rename(temporary,file);const parent=await open(path.dirname(file),'r');try{await parent.sync();}finally{await parent.close();}}
async function secureDirectory(dir){await mkdir(dir,{recursive:true,mode:0o700});const info=await stat(dir);if(!info.isDirectory()||(info.mode&0o077)!==0||(process.getuid&&info.uid!==process.getuid()))throw new Error('unsafe_state_directory');}
export function normalizeClaim(body,now=Date.now()){
 if(!exact(body,['job']))throw new Error('invalid_claim');if(body.job===null)return null;const job=body.job;
 if(!exact(job,['id','leaseToken','request'])||!uuid.test(job.id)||typeof job.id!=='string'||typeof job.leaseToken!=='string'||!/^[A-Za-z0-9_-]{20,200}$/.test(job.leaseToken))throw new Error('invalid_claim');
 const r=job.request;if(!exact(r,['version','correlationId','idempotencyKey','context'])||r.version!==1||r.correlationId!==job.id||typeof r.idempotencyKey!=='string'||r.idempotencyKey.length<1||r.idempotencyKey.length>1000)throw new Error('invalid_claim');
 const request={...r,idempotencyKey:sha(r.idempotencyKey+'\0'+job.leaseToken)};validateRequest(request);
 const leaseSeconds=r.context?.policy?.lease_seconds??180;if(!Number.isSafeInteger(leaseSeconds)||leaseSeconds<120||leaseSeconds>600)throw new Error('invalid_lease_policy');
 return {version:1,jobId:job.id,leaseToken:job.leaseToken,request,claimedAt:now,leaseSeconds};
}
export function transportResult(result,correlationId,context){
 result=restrictTaskResult(result,context);
 if(!result||typeof result!=='object'||(!failures.has(result.status)&&result.status!=='ok')||(result.status==='ok'&&!validateOutput(result.output)))return {status:'malformed',correlationId};
 const output={status:result.status,correlationId};if(result.status==='ok')output.output=result.output;
 if(result.usage&&['input_tokens','output_tokens'].every(k=>Number.isSafeInteger(result.usage[k])&&result.usage[k]>=0))output.usage=Object.fromEntries(['input_tokens','output_tokens','cached_input_tokens','cache_write_input_tokens','reasoning_output_tokens'].filter(k=>Number.isSafeInteger(result.usage[k])&&result.usage[k]>=0).map(k=>[k,result.usage[k]]));
 for(const key of ['eventHash','diagnosticHash'])if(typeof result[key]==='string'&&/^[a-f0-9]{64}$/.test(result[key]))output[key]=result[key];
 if(result.exitCode===null||Number.isSafeInteger(result.exitCode))output.exitCode=result.exitCode;
 if(result.exitSignal===null||(typeof result.exitSignal==='string'&&/^SIG[A-Z]{1,12}$/.test(result.exitSignal)))output.exitSignal=result.exitSignal;
 if(Array.isArray(result.diagnostics))output.diagnostics=result.diagnostics.filter(s=>typeof s==='string'&&/^[a-z_]{1,64}$/.test(s)).slice(0,16);
 return output;
}
const sleep=(ms,signal)=>new Promise(resolve=>{if(signal?.aborted)return resolve();const done=()=>{clearTimeout(timer);signal?.removeEventListener('abort',done);resolve();};const timer=setTimeout(done,ms);signal?.addEventListener('abort',done,{once:true});});
export class HostWorker{
 constructor({baseUrl,tokenFile,stateDir,pins,killWrapper,authHome,cliLimits={},httpTimeoutMs=5000,maxHttpAttempts=3,pollMs=10000,fetchImpl=fetch,adapter,identityGuard,clock=()=>Date.now(),wait=sleep}={}){
  const url=new URL(baseUrl);if(url.username||url.password||url.search||url.hash||url.pathname!=='/'||(url.protocol!=='https:'&&!(url.protocol==='http:'&&['127.0.0.1','localhost','[::1]'].includes(url.hostname))))throw new Error('unsafe_backend_origin');
  if(![tokenFile,stateDir,killWrapper,authHome].every(p=>typeof p==='string'&&path.isAbsolute(p)))throw new Error('absolute_private_paths_required');
  if(!Number.isSafeInteger(httpTimeoutMs)||httpTimeoutMs<20||httpTimeoutMs>10000||!Number.isSafeInteger(maxHttpAttempts)||maxHttpAttempts<1||maxHttpAttempts>3||!Number.isSafeInteger(pollMs)||pollMs<20||pollMs>60000)throw new Error('invalid_worker_limits');
  for(const key of Object.keys(cliLimits))if(!['timeoutMs','maxDailyCalls','maxDailyTokens','tokenReservation','maxEventBytes'].includes(key))throw new Error('invalid_cli_limit');
  if(cliLimits.timeoutMs!==undefined&&cliLimits.timeoutMs>90000)throw new Error('lease_unsafe_cli_timeout');
  Object.assign(this,{baseUrl:url.origin,tokenFile,stateDir,killWrapper,authHome,cliLimits,httpTimeoutMs,maxHttpAttempts,pollMs,fetchImpl,clock,wait});
  this.guard=identityGuard??new IdentityGuard(pins);this.adapter=adapter??new CliCoach({executable:pins.native.path,stateDir:path.join(stateDir,'adapter'),killWrapper,authHome,...cliLimits});
  this.journal=path.join(stateDir,'pending.json');this.prepared=false;
 }
 async prepare(signal){await secureDirectory(this.stateDir);await secureDirectory(path.join(this.stateDir,'adapter'));await this.guard.verify();await this.token();
  if(!this.prepared){const probe=new CliCoach({executable:this.adapter.executable,stateDir:path.join(this.stateDir,'adapter'),killWrapper:this.killWrapper,authHome:this.authHome,timeoutMs:5000});const version=await probe.run('',signal,'version');if(version.status==='busy')return {status:'busy'};if(version.status==='termination_failed')return {status:'blocked',modelStatus:'termination_failed'};if(version.status!=='version_verified')throw new Error('unsupported_cli_version');this.prepared=true;}
 }
 async token(){const info=await stat(this.tokenFile);if(!info.isFile()||info.size>4096||(info.mode&0o077)!==0||(process.getuid&&info.uid!==process.getuid()))throw new Error('unsafe_token_file');const token=(await readFile(this.tokenFile,'utf8')).trim();if(!/^[A-Za-z0-9._~-]{20,2048}$/.test(token))throw new Error('invalid_worker_token');return token;}
 async post(route,body,signal){
  if(route!=='/api/coaching/worker/claim'&&!/^\/api\/coaching\/worker\/jobs\/[a-f0-9-]{36}\/result$/i.test(route))throw new Error('forbidden_worker_route');
  const ctrl=new AbortController();const timer=setTimeout(()=>ctrl.abort(),this.httpTimeoutMs);const abort=()=>ctrl.abort();signal?.addEventListener('abort',abort,{once:true});if(signal?.aborted)ctrl.abort();
  try{const response=await this.fetchImpl(this.baseUrl+route,{method:'POST',redirect:'error',headers:{'Authorization':'Bearer '+await this.token(),'Content-Type':'application/json'},body:JSON.stringify(body),signal:ctrl.signal});
   if(response.status===401||response.status===403)return {kind:'http_auth',code:response.status};if(response.status===409)return {kind:'lease_expired',code:409};if(response.status===429||response.status>=500)return {kind:'http_transient',code:response.status};if(!response.ok)return {kind:'http_rejected',code:response.status};
   let bytes=0;const chunks=[];if(!response.body)return {kind:'http_rejected'};for await(const chunk of response.body){bytes+=chunk.length;if(bytes>65536){return {kind:'http_rejected',reason:'response_too_large'};}chunks.push(chunk);}
   try{return {kind:'ok',body:JSON.parse(Buffer.concat(chunks).toString('utf8'))};}catch{return {kind:'http_rejected',reason:'response_not_json'};}
  }catch(error){return {kind:signal?.aborted?'cancelled':['unsafe_token_file','invalid_worker_token'].includes(error.message)?'http_auth':'http_transient'};}finally{ctrl.abort();clearTimeout(timer);signal?.removeEventListener('abort',abort);}
 }
 requiredInferenceLeaseMs(){
  const inferenceMs=this.adapter.timeoutMs??this.cliLimits.timeoutMs??90000;
  // Auth and inference may each require the adapter's 3.5s termination checkpoint.
  // Reserve all bounded delivery attempts/backoffs plus filesystem/identity margin.
  const authMs=Math.min(inferenceMs,5000);
  const deliveryMs=this.maxHttpAttempts*this.httpTimeoutMs+Array.from({length:this.maxHttpAttempts-1},(_,i)=>Math.min(1000*2**i,4000)).reduce((a,b)=>a+b,0);
  return inferenceMs+authMs+7000+deliveryMs+5000;
 }
 async tick({signal}={}){
  if(signal?.aborted)return {status:'cancelled'};const readiness=await this.prepare(signal);if(readiness)return readiness;
  const owned=await acquireHostLock(path.join(this.stateDir,'host-worker.lock'));if(!owned)return {status:'busy'};
  try{
   let pending;try{pending=await privateJson(this.journal,131072);}catch(e){if(e.code!=='ENOENT')throw e;}
   if(pending){if(pending.version!==1||typeof pending.jobId!=='string'||!uuid.test(pending.jobId)||typeof pending.leaseToken!=='string'||!/^[A-Za-z0-9_-]{20,200}$/.test(pending.leaseToken)||!Number.isFinite(pending.claimedAt)||!Number.isSafeInteger(pending.leaseSeconds)||pending.leaseSeconds<120||pending.leaseSeconds>600)throw new Error('invalid_pending_journal');validateRequest(pending.request);}
   else{const claimStartedAt=this.clock();const claimed=await this.post('/api/coaching/worker/claim',{},signal);if(claimed.kind!=='ok')return {status:claimed.kind,httpCode:claimed.code};try{pending=normalizeClaim(claimed.body,claimStartedAt);}catch{const job=claimed.body?.job;if(!job||typeof job.id!=='string'||!uuid.test(job.id)||typeof job.leaseToken!=='string'||!/^[A-Za-z0-9_-]{20,200}$/.test(job.leaseToken))return {status:'http_rejected',reason:'invalid_claim'};pending={version:1,jobId:job.id,leaseToken:job.leaseToken,request:{version:1,correlationId:job.id,idempotencyKey:sha(job.id+'\0'+job.leaseToken),context:{}},claimedAt:claimStartedAt,leaseSeconds:180,result:{status:'malformed',correlationId:job.id,diagnostics:['invalid_claim_request']}};}if(!pending)return {status:'idle'};await durableJson(this.journal,pending);}
   if(!pending.result){
    // An old lease is never a reason to spend new inference. Backend alone reclaims leases.
    const enoughLease=()=>pending.leaseSeconds*1000-(this.clock()-pending.claimedAt)>this.requiredInferenceLeaseMs();
    const abandon=()=>({status:'interrupted',correlationId:pending.jobId,diagnostics:['insufficient_remaining_lease']});
    const cached=typeof this.adapter.lookup==='function'?await this.adapter.lookup(pending.request,{signal}):null;
    if(cached?.status==='busy'){if(this.clock()-pending.claimedAt>=pending.leaseSeconds*1000){await unlink(this.journal);return {status:'lease_expired',jobId:pending.jobId};}return {status:'busy',jobId:pending.jobId};}
    if(cached)pending.result=transportResult(cached,pending.jobId,pending.request.context);
    else if(!enoughLease())pending.result=abandon();
    else{await this.guard.verify();pending.result=enoughLease()?transportResult(await this.adapter.analyze(pending.request,{signal}),pending.jobId,pending.request.context):abandon();}
    if(pending.result?.status==='busy'){delete pending.result;return {status:'busy',jobId:pending.jobId};}
    await durableJson(this.journal,pending);
   }else pending.result=transportResult(pending.result,pending.jobId,pending.request.context);
   for(let attempt=0;attempt<this.maxHttpAttempts;attempt++){
    const delivered=await this.post(`/api/coaching/worker/jobs/${pending.jobId}/result`,{leaseToken:pending.leaseToken,result:pending.result});
    if(delivered.kind==='ok'&&(delivered.body?.id!==pending.jobId||!['completed','pending','dead_letter'].includes(delivered.body?.status)))return {status:'http_rejected',jobId:pending.jobId,reason:'invalid_result_receipt'};
    if(delivered.kind==='ok'||delivered.kind==='lease_expired'){
     await unlink(this.journal);const jobStatus=typeof delivered.body?.status==='string'?delivered.body.status:undefined;
     const backendFailure=failures.has(delivered.body?.data?.failure)?delivered.body.data.failure:undefined;
     return {status:delivered.kind==='lease_expired'?'lease_expired':jobStatus==='dead_letter'||halt.has(pending.result.status)?'blocked':'submitted',jobId:pending.jobId,modelStatus:pending.result.status,jobStatus,backendFailure};
    }
    if(delivered.kind!=='http_transient'||attempt===this.maxHttpAttempts-1)return {status:delivered.kind,jobId:pending.jobId,modelStatus:pending.result.status,httpCode:delivered.code};
    if(this.clock()-pending.claimedAt>(pending.leaseSeconds-10)*1000)return {status:'http_transient',jobId:pending.jobId,modelStatus:pending.result.status};
    await this.wait(Math.min(1000*2**attempt,4000),signal);
   }
  }finally{await owned.handle.close();await unlink(path.join(this.stateDir,'host-worker.lock'));}
 }
 async status({signal}={}){
  const readiness=await this.prepare(signal);if(readiness)return {...readiness,backendAuth:'not_checked',inferenceCalls:0};const probe=new CliCoach({executable:this.adapter.executable,stateDir:path.join(this.stateDir,'adapter'),killWrapper:this.killWrapper,authHome:this.authHome,timeoutMs:5000});const auth=await probe.run('',signal,true);
  let pending,budget;try{pending=await privateJson(this.journal,131072);}catch(e){if(e.code!=='ENOENT')throw e;}
  try{budget=await privateJson(path.join(this.stateDir,'adapter','budget.json'),8388608);}catch(e){if(e.code!=='ENOENT')throw e;}
  const accounting=budget?Object.fromEntries(['day','calls','reservedTokens','usageTokens'].map(k=>[k,budget[k]])):null;
  if(accounting&&(!/^\d{4}-\d{2}-\d{2}$/.test(accounting.day)||!['calls','reservedTokens','usageTokens'].every(k=>Number.isSafeInteger(accounting[k])&&accounting[k]>=0)))throw new Error('budget_state_invalid');
  if(pending&&(typeof pending.jobId!=='string'||!uuid.test(pending.jobId)||!Number.isFinite(pending.claimedAt)))throw new Error('invalid_pending_journal');
  return {status:auth.status==='authenticated'?'local_ready':'blocked',cliVersion:this.guard.pins?.version??'test_fixture',authStatus:auth.status,backendAuth:'not_checked',inferenceCalls:0,pending:pending?{jobId:pending.jobId,ageSeconds:Math.max(0,Math.floor((this.clock()-pending.claimedAt)/1000)),hasResult:Boolean(pending.result),modelStatus:pending.result?transportResult(pending.result,pending.jobId).status:undefined}:null,budget:accounting};
 }
 async run({signal,onStatus=()=>{}}={}){let last,failures=0;while(!signal?.aborted){const status=await this.tick({signal});const signature=JSON.stringify(status);if(signature!==last&&status.status!=='idle'){onStatus(status);last=signature;}if(['blocked','http_auth','http_rejected','cancelled'].includes(status.status))return status;failures=status.status==='http_transient'?Math.min(failures+1,3):0;await this.wait(Math.min(this.pollMs*2**failures,60000),signal);}return {status:'cancelled'};}
}
