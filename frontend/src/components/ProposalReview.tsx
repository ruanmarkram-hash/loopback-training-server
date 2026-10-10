import { useMutation, useQuery } from '@tanstack/react-query'
import { useEffect, useState } from 'react'
import { ErrorNote, Modal } from './ui'
import { ForecastList, PrescriptionList } from './ProgramForecast'
import { ProposalExplanation } from './ProposalExplanation'
import { ProposalEvidence } from './ProposalEvidence'
import { api } from '../lib/api'
import { coaching } from '../lib/coaching'
import type { Proposal } from '../lib/coaching'
import { formatPrescriptionValue } from '../lib/coach-format'
export function ProposalReview({proposal, onClose, refresh}: {proposal: Proposal; onClose: () => void; refresh: () => Promise<void>}) {
 const plan = useQuery({queryKey: ['coaching','revision',proposal.plan_id], queryFn: () => api.get<{revision: number}>(`/api/plans/${proposal.plan_id}`), retry: false})
 const [now,setNow] = useState(Date.now)
 useEffect(() => {const timer = window.setInterval(()=>setNow(Date.now()),1000);return ()=>window.clearInterval(timer)},[])
 const expired = new Date(proposal.expires_at).getTime() <= now
 const stale = plan.data && plan.data.revision !== proposal.base_revision
 const accept = useMutation({mutationFn: () => coaching.accept(proposal, plan.data!.revision), onSuccess: async () => {await refresh(); onClose()}})
 const dismiss = useMutation({mutationFn: () => coaching.dismiss(proposal.id), onSuccess: async () => {await refresh(); onClose()}})
 const busy = accept.isPending || dismiss.isPending
 return <Modal label="Review coaching proposal" onClose={onClose} closeDisabled={busy} focusDialog width={740}>
  <h2>Review coaching proposal</h2><p>{proposal.reason}</p><p className="coach-muted">Expires {new Date(proposal.expires_at).toLocaleString()} · Plan revision {proposal.base_revision}</p>
  <ProposalEvidence proposal={proposal}/>
  {proposal.previewJobId&&<ProposalExplanation previewJobId={proposal.previewJobId}/>}
  {proposal.changes.map((change, index) => <div className="coach-change" key={`${change.workoutId}-${index}`}><strong>{{distance_meters:'Distance',duration_seconds:'Duration',pace_seconds_per_km:'Pace'}[change.field] || change.field.replaceAll('_',' ')}</strong><p>{formatPrescriptionValue(change.field,change.oldValue)} → {formatPrescriptionValue(change.field,change.value)}</p><p>{change.reason}</p></div>)}
  {!!proposal.structuralChanges?.length && <section aria-label="Prescription changes"><h3>Complete prescription changes</h3>{proposal.structuralChanges.map((change,index)=><div className="coach-change" key={`${change.workoutId}-${index}`}><h4>{change.title || 'Session change'}</h4>{(['before','after'] as const).map(side=>{const snapshot=change[side];return <section key={side} aria-label={side==='before'?'Current prescription':'Proposed prescription'}><strong>{side==='before'?'Current prescription':'Proposed prescription'}</strong>{snapshot ? <><p>{snapshot.title || 'Title unavailable'} · {snapshot.scheduled_date || snapshot.scheduledDate || snapshot.date || 'Date unavailable'}</p>{snapshot.description && <p>{snapshot.description}</p>}{snapshot.composition ? <PrescriptionList composition={snapshot.composition}/>:<p>Prescription details unavailable.</p>}</>:<p>{side==='before'?'New session, no previous prescription.':'Session retired from the future forecast.'}</p>}</section>})}</div>)}</section>}
  {!!proposal.limitations?.length && <section aria-label="Proposal limitations"><h3>Limitations</h3><ul>{proposal.limitations.map((text,index)=><li key={index}>{text}</li>)}</ul></section>}
  {proposal.protectedSessionIds && <p>{proposal.protectedSessionIds.length} protected sessions remain unchanged.</p>}{proposal.retiredWorkoutIds && <p>{proposal.retiredWorkoutIds.length} future sessions would be retired.</p>}{proposal.excludedWorkoutIds && <p>{proposal.excludedWorkoutIds.length} prescriptions excluded from undo.</p>}
  {!!proposal.forecast?.length && <><h3>Estimated full program</h3><ForecastList sessions={proposal.forecast} /></>}
  {proposal.validation.length ? <section aria-label="Proposal validation findings"><h3>Validation findings</h3><ul>{proposal.validation.map((finding,index) => {const item = typeof finding === 'object' && finding !== null ? finding as Record<string,unknown> : {};const text = typeof finding === 'string' ? finding : typeof item.explanation === 'string' ? item.explanation : typeof item.message === 'string' ? item.message : 'No explanation supplied. Request a fresh review before approving.';return <li key={index}><strong>{typeof item.severity === 'string' ? item.severity : 'Severity unknown'}</strong>: {text}{typeof item.code === 'string' && <span className="coach-muted"> · {item.code.replaceAll('_',' ')}</span>}{typeof item.week === 'string' && <span className="coach-muted"> · Week {item.week}</span>}</li>})}</ul></section> : <p>Server validation reported no findings.</p>}
  {expired && <p role="alert">This proposal has expired. Request a fresh review.</p>}{stale && <p role="alert">The plan changed. Request a fresh review before approving.</p>}
  {(plan.error || accept.error || dismiss.error) && <ErrorNote error={plan.error || accept.error || dismiss.error} />}
  <div className="coach-actions"><button className="btn-ghost" disabled={busy} onClick={onClose}>Close without changes</button><button className="btn-ghost" disabled={busy} onClick={() => dismiss.mutate()}>Dismiss proposal</button><button className="btn-accent" disabled={busy || plan.isPending || !!plan.error || !!stale || expired || proposal.status !== 'awaiting_approval'} onClick={() => accept.mutate()}>{accept.isPending ? 'Approving…' : 'Approve changes'}</button></div>
 </Modal>
}
