import type { AdventureId } from './contracts'

export type DraftNode = Record<string, unknown>
export type FieldPolicy = 'fixed' | 'fillable' | 'undecided'
export type Visibility = 'private' | 'unlisted' | 'public'
export type Lifecycle = 'active' | 'withdrawn' | 'blocked' | 'archived'
export interface StoryMetadata { title: string; synopsis: string; tags: string[]; content_warnings: string[] }
export interface AuthoringDraft {
  authoring_schema_version: 1; scenario: DraftNode; metadata: StoryMetadata
  field_policies: Record<string, FieldPolicy>; notes: string
}
export interface TemplateQuestion { prompt: string; hint: string; target_section: string; field_path: string | null }
export interface StoryTemplate {
  template_id: string; version: number; title: string; description: string
  required_capabilities: string[]; sections: string[]; initial_draft: AuthoringDraft
  questions?: TemplateQuestion[]; recommended_structure?: string[]
}
export interface StoryDraft { story_id: string; revision: number; draft: AuthoringDraft; updated_at: string }
export interface StoryRevision extends StoryDraft { actor_id: string; source: string }
export interface StorySummary {
  story_id: string; revision: number; metadata: StoryMetadata; visibility: Visibility
  lifecycle: Lifecycle; current_release_id: string | null; updated_at: string
}
export interface ValidationFinding {
  code: string; severity: 'error' | 'warning'; field_path: string; entity_ref: string | null
  message: string; suggestion: string | null
}
export interface ValidationReport {
  id: string; story_id: string; draft_revision: number; content_hash: string | null; draft_hash: string
  validator_version: string; errors: ValidationFinding[]; warnings: ValidationFinding[]
  coverage: Record<string, unknown>; created_at: string
}
export interface StoryPublicDetail {
  story_id: string; story_version_id: string; release_number: number; visibility: Visibility
  lifecycle: Lifecycle; metadata: StoryMetadata; created_at: string; ruleset_ref: 'mvp_v1' | 'mvp_v2'
}
export interface StoryPlaytest extends AdventureId { story_version_id: string; content_hash: string }
export interface SaveDraft { request_id: string; expected_revision: number; draft: AuthoringDraft }

export const node = (value: unknown): DraftNode => value && typeof value === 'object' && !Array.isArray(value) ? value as DraftNode : {}
export const rows = (value: unknown): DraftNode[] => Array.isArray(value) ? value as DraftNode[] : []
export const clone = <T>(value: T): T => JSON.parse(JSON.stringify(value)) as T
export const capabilityNames: Record<string, string> = {
  skill_checks: '技能判定', combat: '戦闘', item_use: 'アイテム使用', open_actions: '自由行動', protected_facts: '固定設定',
}
