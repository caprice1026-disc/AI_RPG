import type { CampaignState, Catalog, Session, Turn } from '../src/contracts'

export const session: Session = { principal_id: 'principal-a', mode: 'session', csrf_token: 'memory-only-csrf', expires_at: null }
export const catalog: Catalog = {
  scenarios: [{ scenario_ref: 'chapel', scenario_version: 2, title: '廃礼拝堂の依頼', objective: '鐘を探す' }],
  presets: [
    { preset_ref: 'scout', name: '斥候', description: '身軽な旅人', max_hp: 12 },
    { preset_ref: 'guard', name: '守り手', description: '丈夫な旅人', max_hp: 18 },
  ],
}
export const saved = { campaign_id: 'campaign-a', actor_id: 'actor-a', player_name: '葵',
  scenario_ref: 'chapel', scenario_version: 2, title: '廃礼拝堂の依頼', status: 'active' as const,
  state_version: 0, created_at: '2026-09-22T00:00:00Z' }
export function turn(id = 'turn-1', version = 1): Turn {
  return { turn_id: id, route: 'mechanical', resolution_status: 'committed', narration_status: 'completed',
    committed_state_version: version, narration: '扉が開いた。', choices: [], action_results: [], enemy_reactions: [],
    recovery: { fallback: false, reason: null } }
}
export function waiting(id = 'turn-1'): Turn {
  return { ...turn(id), route: null, resolution_status: 'resolving', narration_status: 'pending',
    committed_state_version: null, narration: null }
}
export function campaign(overrides: Partial<CampaignState> = {}): CampaignState {
  return { state_version: 0, latest_turn: null,
    player: { actor_id: 'actor-a', name: '葵', current_hp: 9, max_hp: 12,
      inventory: [{ item_id: 'potion', item_ref: 'potion', name: '回復ポーション', quantity: 2, equipped: false }] },
    adventure: { scenario_ref: 'chapel', title: '廃礼拝堂の依頼', objective: '鐘を探す', status: 'active',
      current_scene: { scene_ref: 'gate', title: '廃礼拝堂の入口', description: '霧の中に門がある。' },
      discovered_facts: ['扉は閉ざされている'], available_actions: [{ action_ref: 'look', label: '周囲を見る' }],
      ending: null, combat: { enemy_ref: 'goblin', enemy_name: 'ゴブリン', current_hp: 4, max_hp: 7, active: true } },
    ...overrides }
}
export function response(data: unknown, status = 200) {
  return new Response(status === 204 ? null : JSON.stringify(data), { status, headers: { 'Content-Type': 'application/json' } })
}
export function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>(r => { resolve = r })
  return { promise, resolve }
}
export async function flush() { for (let i = 0; i < 40; i++) await Promise.resolve() }

// Only HTTP is faked: the actual persistence, state module and Vue view run in tests.
export function server(handler: (path: string, init: RequestInit) => unknown = () => undefined) {
  const calls: { path: string; init: RequestInit }[] = []
  const fetch = async (input: RequestInfo | URL, init: RequestInit = {}): Promise<Response> => {
    const path = String(input)
    calls.push({ path, init })
    const result = await handler(path, init)
    if (result !== undefined) return result as Response
    if (path === '/auth/session') return response(session)
    if (path === '/auth/logout') return response(null, 204)
    if (path === '/adventures/catalog') return response(catalog)
    if (path === '/adventures' && init.method !== 'POST') return response({ adventures: [saved] })
    if (path.endsWith('/state')) return response(campaign())
    if (path.includes('/history?')) return response({ items: [], next_before_turn_id: null })
    throw new Error(`Unexpected request: ${path}`)
  }
  return { fetch, calls, posts: () => calls.filter(c => c.init.method === 'POST') }
}
export class Events {
  static instances: Events[] = []
  onopen: (() => void) | null = null
  onerror: (() => void) | null = null
  listener: ((event: MessageEvent) => void) | null = null
  closed = false
  constructor(public url: string) { Events.instances.push(this) }
  addEventListener(_name: string, listener: (event: MessageEvent) => void) { this.listener = listener }
  close() { this.closed = true }
  emit(value: Turn) { this.listener?.({ data: JSON.stringify({ payload: { turn: value } }) } as MessageEvent) }
}
