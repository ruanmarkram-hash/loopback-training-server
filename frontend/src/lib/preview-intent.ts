import type {Proposal} from './coaching'
/** Store only a body fingerprint and request identity, scoped to the authenticated UUID and plan. */
export type PreviewIntent = {slot:string;key:string}
export async function sha256(value:string) {const bytes=await crypto.subtle.digest('SHA-256',new TextEncoder().encode(value));return Array.from(new Uint8Array(bytes),byte=>byte.toString(16).padStart(2,'0')).join('')}
export async function preparePreviewIntent(actorId:string,kind:'initial'|'undo'|'rebuild',planId:string,body:unknown,terminalHashes:ReadonlySet<string>=new Set()):Promise<PreviewIntent> {
 if(!actorId)throw new Error('The authenticated account is unavailable. Sign in again before previewing.')
 const fingerprint=await sha256(JSON.stringify(body)),slot=`loopback.preview-intent:${actorId}:${kind}:${planId}:${fingerprint}`
 let stored:{key?:unknown}|null=null
 try{stored=JSON.parse(sessionStorage.getItem(slot)||'null')}catch{throw new Error('Could not read a safe preview retry identity. Review existing proposals before trying again.')}
 let key=typeof stored?.key==='string'?stored.key:null
 const legacySlot=`loopback.program-preview-intent:${actorId}`
 if(!key&&kind==='initial'){try{const legacy=JSON.parse(sessionStorage.getItem(legacySlot)||'null');if(legacy?.digest===fingerprint&&typeof legacy.key==='string')key=`program-ui:${legacy.key}`}catch{throw new Error('Could not recover the existing preview request identity.')}}
 if(!key||terminalHashes.has(await sha256(key)))key=`${kind}-ui:${crypto.randomUUID()}`
 try{sessionStorage.setItem(slot,JSON.stringify({...stored,key}));if(kind==='initial')sessionStorage.removeItem(legacySlot)}catch{throw new Error('Could not preserve a safe preview retry identity. Enable session storage before previewing.')}
 return {slot,key}
}
export function recordPreviewProposal(intent:PreviewIntent,proposalId:string){
 // Receiving a draft does not end its retry identity. Terminal status is matched by owned request binding.
 try{sessionStorage.setItem(intent.slot,JSON.stringify({key:intent.key,proposalId}))}catch{/* The request key was persisted before sending; keep the visible draft even if optional metadata fails. */}
}

export function terminalIntentHashes(proposals:Proposal[],actorId:string,requestType:string,planId?:string):Set<string> {
 return new Set(proposals.filter(proposal=>proposal.ownerId===actorId&&proposal.requestType===requestType&&(!planId||proposal.requestPlanId===planId)&&typeof proposal.clientRequestKeyHash==='string'&&/^[a-f0-9]{64}$/.test(proposal.clientRequestKeyHash)&&(['applied','dismissed','superseded','expired'].includes(proposal.status)||new Date(proposal.expires_at).getTime()<=Date.now())).map(proposal=>proposal.clientRequestKeyHash!))
}
