import {useMutation,useQuery} from '@tanstack/react-query'
import {useRef} from 'react'
import {api} from '../lib/api'
import {coaching} from '../lib/coaching'
import type {Job} from '../lib/coaching'
import {ErrorNote} from './ui'
export function ProposalExplanation({previewJobId}:{previewJobId:string}) {
 const key=useRef(crypto.randomUUID())
 const jobs=useQuery({queryKey:['coaching','explanations',previewJobId],queryFn:()=>api.get<Job[]>('/api/coaching/jobs'),retry:false,refetchInterval:5000})
 const request=useMutation({mutationFn:()=>coaching.explanation(previewJobId,key.current),onSuccess:()=>jobs.refetch()})
 const linked=jobs.data?.find(job=>job.data.previewJobId===previewJobId&&job.data.trigger==='structural_explanation')
 return <section aria-label="Optional coaching explanation"><h3>Optional coaching explanation</h3><p>This preview is deterministic. A separately requested CLI explanation is an untrusted annotation. It cannot change or approve prescriptions.</p><button className="btn-ghost" disabled={request.isPending||!!linked||jobs.isPending||!!jobs.error} onClick={()=>request.mutate()}>{request.isPending?'Requesting explanation…':'Request CLI explanation'}</button>{(jobs.error||request.error)&&<ErrorNote error={jobs.error||request.error}/>}<button className="btn-ghost" disabled={jobs.isFetching} onClick={()=>void jobs.refetch()}>Refresh explanation</button>{linked&&<><p>Explanation job: {linked.status.replaceAll('_',' ')} · Attempts {linked.attempts}.</p>{linked.data.explanation?.reason ? <><p><strong>Untrusted CLI annotation</strong></p><p>{linked.data.explanation.reason}</p></>:<p>No CLI explanation has been received.</p>}</>}</section>
}
