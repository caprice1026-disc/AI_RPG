export interface Session { principal_id: string; mode: 'session' | 'development' | 'bearer'; csrf_token: string | null; expires_at: string | null }
export interface Catalog {
  scenarios: { scenario_ref: string; scenario_version: number; title: string; objective: string }[]
  presets: { preset_ref: string; name: string; description: string; max_hp: number }[]
}
export interface AdventureId { campaign_id: string; actor_id: string }
export interface AdventureSummary extends AdventureId {
  player_name: string; scenario_ref: string; scenario_version: number; title: string
  status: 'active' | 'completed'; state_version: number; created_at: string
}
export type Content = { kind: 'text'; text: string } | { kind: 'scenario_action'; action_ref: string } | { kind: 'choice'; choice_id: string }
export interface StartBody { request_id: string; scenario_ref: string; scenario_version: number; preset_ref: string; player_name: string }
export interface TurnBody { request_id: string; expected_state_version: number; actor_id: string; content: Content }
export type Pending = { kind: 'start'; body: StartBody; adventure: AdventureId | null }
  | { kind: 'turn'; body: TurnBody; campaignId: string; displayText: string; turnId: string | null }
export type Result = { kind: 'not_applicable'; reason: 'target_unavailable' | 'resource_unavailable' | 'rule_precondition' }
  | { kind: 'applied'; outcome: 'success' | 'failure' | 'neutral'; facts: string[];
      dice: { expression: string; rolls: number[]; modifier: number; total: number }[] }
export interface Turn {
  turn_id: string; route: 'narrative' | 'mechanical' | null
  resolution_status: 'pending' | 'resolving' | 'committed' | 'not_applied' | 'failed'
  narration_status: 'pending' | 'generating' | 'completed' | 'fallback'
  committed_state_version: number | null; narration: string | null
  choices: { id: string; label: string }[]
  action_results: { action_id: string; ordinal: number; result: Result }[]
  enemy_reactions: { reaction_id: string; actor_id: string; target_id: string; result: Result }[]
  recovery: { fallback: boolean; reason: string | null }
}
export interface CampaignState {
  state_version: number; latest_turn: Turn | null
  player: { actor_id: string; name: string; current_hp: number; max_hp: number;
    inventory: { item_id: string; item_ref: string | null; name: string; quantity: number; equipped: boolean }[] } | null
  adventure: { scenario_ref: string; title: string; objective: string; status: 'active' | 'completed';
    current_scene: { scene_ref: string; title: string; description: string } | null
    discovered_facts: string[]; available_actions: { action_ref: string; label: string }[]
    ending: { ending_ref: string; title: string; summary: string } | null
    combat: { enemy_ref: string; enemy_name: string; current_hp: number; max_hp: number; active: boolean } | null } | null
}
export interface StoryRecord { turn: Turn; player_input?: string; created_at?: string }
export interface HistoryPage { items: (StoryRecord & { player_input: string; created_at: string })[]; next_before_turn_id: string | null }
