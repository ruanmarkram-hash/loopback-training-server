import type { WorkoutComposition } from './types'
import { api } from './api'
import type {AuthCommand} from './api'
export type PublicationReview={status:'compatible'|'review_required';reason:string|null;restrictions:string[];availableDays:string[]}
export type ForecastSession = { id: string; date: string; title: string; status: string; prescription_committed?: boolean; composition: WorkoutComposition; phase?: string; session_type?: string; effort?: string; constraints?: string[]; dose?: {distance_meters: number|null; estimated_duration_seconds: number|null; running_seconds: number|null; duration_estimated: boolean; event_excluded_from_training_load: boolean}; benchmark_evidence?: {source: string; observed_at: string; confidence: number|null} }
export type PrescriptionSnapshot = {title?: string; scheduled_date?: string; scheduledDate?: string; date?: string; description?: string|null; composition?: WorkoutComposition}
export type StructuralChange = {workoutId: string; title?: string; before?: PrescriptionSnapshot|null; after?: PrescriptionSnapshot|null}
export type RebuildResult = {status: 'forecast_ready'|'review_required'|'goal_infeasible'; previewJobId?:string; proposal: Proposal|null; forecast: ForecastSession[]|null; preserved_forecast: ForecastSession[]; end_date: string|null; limitations: string[]; options: string[]}
export type EvidenceAssessment = {paceStatus?:string;paceDirection?:string|null;independentEligibleDays?:number;minimumIndependentDays?:number;paceReason?:string;explicitMissCount?:number;elapsedGapDays?:number|null;gapCoverage?:string;interruptionReview?:boolean;interruptionPolicy?:string;partialWeekTreatment?:string}
export type RecordedWeek = {partial_week:boolean;session_count:number;distance_meters:number;duration_seconds:number;unknown_distance_sessions:number;unknown_duration_sessions:number;longest_run_meters:number;longest_run_share:number|null;hard_session_count:number;hard_session_evidence?:string;planned_session_count:number;planned_distance_meters:number;planned_duration_seconds:number;planned_estimated:boolean;actual_vs_planned_distance_ratio:number|null;comparison_status?:string}
export type EvidenceMetrics = {actual_distance_meters?:number;actual_duration_seconds?:number;session_count?:number;planned_future_session_count?:number;timezone?:string;weeks?:Record<string,RecordedWeek>}
export type RequestBinding = {ownerId?:string;clientRequestKeyHash?:string|null;requestType?:string|null;requestPlanId?:string|null}
export type Proposal = RequestBinding & { initialFacts?:{idempotencyKey?:string}; previewJobId?:string; evidenceSummary?:EvidenceAssessment;coverage?:{history?:string;ingestionCompleteness?:string};metrics?:EvidenceMetrics; id: string; plan_id: string; base_revision: number; status: string; type: string; reason: string; expires_at: string; validation: unknown[]; changes: {workoutId: string; field: string; oldValue: number; value: number; reason: string}[]; forecast?: ForecastSession[]; structuralChanges?: StructuralChange[]; limitations?: string[]; protectedSessionIds?: string[]; retiredWorkoutIds?: string[]; excludedWorkoutIds?: string[] }
export type Job = RequestBinding & {id: string; status: string; attempts: number; data: {reason?: string; trigger?: string; failureReason?: string; failure?: string; previewJobId?:string; explanation?:{source?:string;reason?:string;status?:string;untrusted_annotation?:boolean}; inference?: {status?: string}}; created_at: string}
export type CoachProfile = {goal?: {type: string; race_date?: string; target_seconds?: number}; available_days: string[]; timezone: string; restrictions?: string[]; benchmark?: {level?: string; pace_seconds_per_km?: number; weekly_distance_meters?: number; long_run_meters?: number; source?: string; observed_at?: string; confidence?: number}}
export type CoachStatus = {profile?: CoachProfile|null;coverage: {history: string}; metrics: {actual_distance_meters: number; session_count: number; planned_future_session_count: number}; jobs: Job[]; proposals: Proposal[]}
export const coaching = {
 undo: (id: string, body: {targetRevision: number; idempotencyKey: string},command?:AuthCommand) => api.post<{proposal: Proposal;previewJobId?:string}>(`/api/coaching/plans/${id}/undo-preview`,body,{command}),
 rebuild: (id: string, body: unknown,command?:AuthCommand) => api.post<RebuildResult>(`/api/coaching/programs/${id}/rebuild`,body,{command}),
 explanation: (id:string,idempotencyKey:string) => api.post<Job>(`/api/coaching/jobs/${id}/explanation`,{idempotencyKey}),
 status: () => api.get<CoachStatus>('/api/coaching/status'),
 program: (body: unknown,command?:AuthCommand) => api.post<{proposal: Proposal;previewJobId?:string}>('/api/coaching/program', body,{command}),
 review: () => api.post('/api/coaching/reviews', {idempotencyKey: crypto.randomUUID()}),
 accept: (proposal: Proposal, revision: number) => api.post(`/api/coaching/proposals/${proposal.id}/accept`, {expectedRevision: revision}),
 dismiss: (id: string) => api.post(`/api/coaching/proposals/${id}/dismiss`, {reason: 'Athlete dismissed in coaching review'}),
 job: (id: string, action: 'retry'|'cancel') => api.post(`/api/coaching/jobs/${id}/${action}`),
}
