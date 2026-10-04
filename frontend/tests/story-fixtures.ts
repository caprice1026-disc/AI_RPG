import { clone, type AuthoringDraft, type StoryDraft, type StorySummary, type StoryTemplate, type ValidationReport } from '../src/story-contracts'
import { response, server } from './fixtures'

export function draft(): AuthoringDraft {
  return { authoring_schema_version: 1, metadata: { title: '風の庭', synopsis: '庭の種を探す', tags: [], content_warnings: [] }, field_policies: { '/scenario/objective': 'fixed' }, notes: '作者の秘密',
    scenario: { scenario_ref: 'story_unique', version: 1, schema_version: 2, ruleset_ref: 'mvp_v2', title: '風の庭', objective: '種を持ち帰る', required_capabilities: ['open_actions', 'protected_facts'],
      initialization: { characters: [{ ref: 'gardener', label: '庭師', kind: 'npc', max_hp: 6, defense: 9, attack_bonus: 0 }], items: [], placements: [] },
      world: { region_name: '庭', boundary: '庭の内側', goal_scene_ref: 'gate', goal_flag_ref: 'seed', outside_ending_ref: 'return', protected_facts: [{ fact_ref: 'secret', kind: 'rule', statement: '鍵は塔にある', visibility: 'secret' }] },
      scenes: [{ scene_ref: 'gate', sequence: 1, title: '庭の門', description: '花が揺れる', actions: [{ action_ref: 'pick', kind: 'scenario_action', label: '種を拾う', public_fact: '種を拾った', success: { add_flags: ['seed'], ending_ref: 'return' } }], open_flags: ['seed'], open_destinations: [] }],
      flags: [{ flag_ref: 'seed', public_fact: '種を入手した' }],
      endings: [{ ending_ref: 'return', title: '帰還', summary: '種を持って帰る', required_flags: ['seed'], reward: { tier: 'full', description: 'ありがとう' } }],
    } }
}
export function story(revision = 1): StoryDraft { return { story_id: 'story-a', revision, draft: draft(), updated_at: '2026-10-04T00:00:00Z' } }
export function summary(value = story()): StorySummary { return { story_id: value.story_id, revision: value.revision, metadata: value.draft.metadata,
  visibility: 'private', lifecycle: 'active', current_release_id: null, updated_at: value.updated_at } }
export function templates(): StoryTemplate[] { return ['garden', 'lighthouse'].map((id, i) => ({ template_id: id, title: i ? '灯台' : '庭', version: 1, description: '短編のテンプレート',
  required_capabilities: ['open_actions'], sections: ['metadata', 'world', 'scenes', 'initialization', 'flags', 'endings', 'field_policies'], initial_draft: draft() })) }
export function report(revision = 1): ValidationReport { return { id: 'report-a', story_id: 'story-a', draft_revision: revision, content_hash: 'hash', draft_hash: 'draft-hash', validator_version: '1',
  errors: [], warnings: [{ code: 'freeform_unverified', severity: 'warning', field_path: '/scenario/world', entity_ref: null, message: '自由行動は未検証です', suggestion: null }],
  coverage: { typed_definition: true, freeform: 'not_verified' }, created_at: story().updated_at } }
export function storyServer(handler: (path: string, init: RequestInit) => unknown = () => undefined) {
  let stored = story()
  const replays = new Map<string, { body: string; result: StoryDraft }>()
  const api = server(async (path, init) => {
    const intercepted = await handler(path, init)
    if (intercepted !== undefined) return intercepted
    if (path === '/stories/templates') return response({ templates: templates() })
    if (path === '/stories/mine') return response({ stories: [summary(stored)] })
    if (path.endsWith('/authoring-jobs') && init.method !== 'POST') return response({ jobs: [] })
    if (path === '/stories' && init.method === 'POST') return response(stored, 201)
    if (path === '/stories/story-a/draft' && init.method !== 'PUT') return response(stored)
    if (path === '/stories/story-a/draft') {
      const body = JSON.parse(String(init.body)), previous = replays.get(body.request_id)
      if (previous) return previous.body === init.body ? response(previous.result) : response({}, 409)
      if (body.expected_revision !== stored.revision) return response({}, 409)
      stored = { ...stored, revision: stored.revision + 1, draft: body.draft }
      replays.set(body.request_id, { body: String(init.body), result: clone(stored) })
      return response(stored)
    }
    if (path.endsWith('/validate')) return response(report(JSON.parse(String(init.body)).expected_revision))
    if (path.endsWith('/playtests')) return response({ campaign_id: 'playtest-a', actor_id: 'playtester-a', story_version_id: 'snapshot-a', content_hash: 'hash' }, 201)
    if (path.endsWith('/publish')) return response({ story_id: 'story-a', story_version_id: 'release-a', release_number: 1,
      metadata: stored.draft.metadata, visibility: 'unlisted', lifecycle: 'active', ruleset_ref: 'mvp_v2', created_at: stored.updated_at })
    if (path.endsWith('/revisions')) return response({ revisions: [{ ...story(), actor_id: 'owner-a', source: 'autosave' }] })
    if (path.endsWith('/restore')) return response({ ...story(), revision: stored.revision + 1 })
    if (path.endsWith('/duplicate')) return response({ ...stored, story_id: 'story-copy', revision: 1 })
    if (path.endsWith('/settings')) return response({ ...summary(stored), ...JSON.parse(String(init.body)) })
  })
  return { ...api, puts: () => api.calls.filter(c => c.init.method === 'PUT'), stored: () => clone(stored) }
}
