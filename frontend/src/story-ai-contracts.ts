import type { ValidationFinding, ValidationReport } from './story-contracts'
export interface StoryOutline { title: string; premise: string; scenes: string[]; characters: string[]; endings: string[]; undecided: string[] }
export interface StoryProposal {
  id: string; job_id: string; story_id: string; base_revision: number
  changes: { id: string; field_path: string; operation: 'set' | 'remove'; before_exists: boolean; before: unknown; after: unknown; reason: string; policy: 'fillable' | 'undecided' }[]
  findings: ValidationFinding[]; validation: ValidationReport; decision: 'pending' | 'applied'; applied_revision: number | null; adopted_change_ids: string[]
}
export interface AuthoringJob {
  id: string; story_id: string; base_revision: number; snapshot_hash: string
  kind: 'check' | 'fill' | 'outline' | 'concretize'; state: 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled'
  model_id: string; actual_model: string | null; attempts: number; physical_requests: number; input_tokens: number; output_tokens: number
  usage_complete: boolean; error_code: string | null; created_at: string; updated_at: string
  proposal: StoryProposal | null; outline: StoryOutline | null; outline_revision: number; approved_outline_revision: number | null
}
