import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import App from '../src/App.vue'
import { campaign, catalog, deferred, Events, response, saved, server, session, turn, waiting } from './fixtures'

const wrappers: VueWrapper[] = []
async function render(api = server()) {
  vi.stubGlobal('fetch', api.fetch)
  const wrapper = mount(App); wrappers.push(wrapper); await flushPromises(); return wrapper
}
const button = (w: VueWrapper, label: string) => w.findAll('button').find(b => b.text() === label)!
beforeEach(() => {
  localStorage.clear(); history.replaceState(null, '', '/')
  Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', { configurable: true, writable: true, value: vi.fn() })
})
afterEach(() => { wrappers.splice(0).forEach(w => w.unmount()); vi.unstubAllGlobals() })

it('signed-out landing shows art and provider link, no credential inputs', async () => {
  const w = await render(server(() => response({}, 401)))
  expect(w.text()).toContain('あなたの言葉で、物語が動く。')
  expect(w.find('a[href="/auth/login"]').exists()).toBe(true)
  expect(w.find('img').attributes('src')).toBe('/static/vue/art/ruined-chapel.png')
  expect(w.findAll('input')).toHaveLength(0)
})
it('503 offers retry and does not mislabel service failure as signed-out', async () => {
  const w = await render(server(() => response({}, 503)))
  expect(w.text()).toContain('認証サービス'); expect(button(w, '接続を再確認')).toBeDefined()
  expect(w.find('a[href="/auth/login"]').exists()).toBe(false)
})
it.each(['development', 'bearer'])('%s mode has no browser logout affordance', async mode => {
  const w = await render(server(path => path === '/auth/session' ? response({ ...session, mode, csrf_token: null }) : undefined))
  expect(button(w, 'ログアウト')).toBeUndefined()
  if (mode === 'development') expect(w.text()).toContain('開発モード')
})
it.each(['LOGIN_REJECTED', 'IDENTITY_NOT_REGISTERED', 'AUTHENTICATION_UNAVAILABLE', '<script>unsafe</script>'])(
  'login error %s is fixed copy and removed from address', async value => {
    history.replaceState(null, '', `/?login_error=${encodeURIComponent(value)}`)
    const w = await render(server(() => response({}, 401)))
    expect(location.search).toBe(''); expect(w.text()).not.toContain(value)
    expect(w.find('[role="alert"]').exists()).toBe(true)
  })
it('registered start form sends actual scenario, preset and exact name; direct action keeps draft', async () => {
  const api = server((path, init) => init.method === 'POST' ? response(path === '/adventures' ? saved : turn()) : undefined)
  const w = await render(api)
  await w.get('#player-name').setValue('  葵  ')
  await w.get('#preset').setValue('scout')
  await w.get('#start-form').trigger('submit'); await flushPromises()
  expect(JSON.parse(String(api.posts()[0]!.init.body))).toMatchObject({ scenario_ref: 'chapel', scenario_version: 2, preset_ref: 'scout', player_name: '  葵  ' })
  expect(w.text()).toContain('9 / 12'); expect(w.text()).toContain('回復ポーション'); expect(w.text()).toContain('扉は閉ざされている')
  expect(w.text()).toContain('4 / 7'); expect(w.text()).toContain('戦闘中')
  await w.get('#action-text').setValue('unfinished draft')
  await button(w, '周囲を見る').trigger('click'); await flushPromises()
  expect(JSON.parse(String(api.posts()[1]!.init.body)).content).toEqual({ kind: 'scenario_action', action_ref: 'look' })
  expect((w.get('#action-text').element as HTMLTextAreaElement).value).toBe('unfinished draft')
})
it('v3 start requires two points and a specialty and sends the saved build', async () => {
  const v3 = { ...catalog, scenarios: [{ scenario_ref: 'chapel', scenario_version: 3,
    title: '廃礼拝堂', objective: '聖印を探す', character_creation: {
      abilities: ['strength', 'agility', 'insight', 'presence'], points: 2,
      specialties: ['athletics', 'acrobatics', 'perception', 'stealth', 'persuasion'],
    } }], presets: catalog.presets.map(p => ({ ...p, base_abilities: {
    strength: 0, agility: 1, insight: 1, presence: 0,
  } })) }
  const api = server(path => path === '/adventures/catalog' ? response(v3) : undefined)
  const w = await render(api)
  await w.get('#player-name').setValue('葵')
  expect((button(w, '冒険を始める').element as HTMLButtonElement).disabled).toBe(true)
  await w.get('#ability-agility').setValue('1')
  await w.get('#ability-insight').setValue('1')
  await w.get('#specialty').setValue('perception')
  expect((button(w, '冒険を始める').element as HTMLButtonElement).disabled).toBe(false)
  await w.get('#start-form').trigger('submit'); await flushPromises()
  expect(JSON.parse(String(api.posts()[0]!.init.body))).toMatchObject({ scenario_version: 3,
    ability_points: { strength: 0, agility: 1, insight: 1, presence: 0 }, specialty_skill: 'perception' })
})
it('risk preview can be cancelled locally or confirmed without rerunning free text', async () => {
  const risky = { ...turn(), resolution_status: 'not_applied' as const, committed_state_version: null,
    risk_preview: { proposal_id: 'proposal-a', risk_text: '離れると冒険が終わります。' } }
  const api = server((path, init) => path.endsWith('/state') ? response(campaign({ latest_turn: risky }))
    : init.method === 'POST' ? response(waiting('confirm-turn')) : undefined)
  const w = await render(api)
  await w.get('[data-adventure="campaign-a"]').trigger('click'); await flushPromises()
  expect(w.text()).toContain('離れると冒険が終わります。')
  await button(w, 'やめる').trigger('click'); await flushPromises()
  expect(api.posts()).toHaveLength(0)
  expect(button(w, 'この行動を実行')).toBeUndefined()
  await w.get('[data-adventure="campaign-a"]').trigger('click'); await flushPromises()
  await button(w, 'この行動を実行').trigger('click'); await flushPromises()
  expect(JSON.parse(String(api.posts()[0]!.init.body)).content).toEqual({
    kind: 'confirm_action', proposal_id: 'proposal-a',
  })
})
it('development note, resume, separate enemy reactions and safe text rendering', async () => {
  const result = { kind: 'applied' as const, outcome: 'success' as const, facts: ['@hero は @goblin を攻撃した。'], dice: [] }
  const t = { ...turn(), narration: '<img src=x onerror=alert(1)>', action_results: [{ action_id: 'a', ordinal: 1, result }],
    enemy_reactions: [{ reaction_id: 'r', actor_id: 'enemy', target_id: 'actor-a', result: { ...result, facts: ['反撃で3のダメージ。'] } }] }
  const api = server(path => path === '/auth/session' ? response({ ...session, mode: 'development' }) : path.endsWith('/state') ? response(campaign({ latest_turn: t })) : undefined)
  const w = await render(api); expect(w.text()).toContain('開発モード')
  await w.get('[data-adventure="campaign-a"]').trigger('click'); await flushPromises()
  expect(w.text()).toContain('行動の結果'); expect(w.text()).toContain('敵の反撃'); expect(w.text()).toContain('葵 は ゴブリン')
  expect(w.text().match(/反撃で3のダメージ。/g)).toHaveLength(1)
  expect(w.text()).toContain('<img src=x onerror=alert(1)>'); expect(w.find('img[src="x"]').exists()).toBe(false)
  expect(result.facts).toEqual(['@hero は @goblin を攻撃した。'])
})
it.each(['victory', 'defeat', 'withdrawal'])('%s ending blocks actions, permits restart and feedback', async ending => {
  const c = campaign(); c.adventure!.status = 'completed'; c.adventure!.ending = { ending_ref: ending, title: ending, summary: '記録された結末' }
  const w = await render(server(path => path.endsWith('/state') ? response(c) : undefined))
  await w.get('[data-adventure="campaign-a"]').trigger('click'); await flushPromises()
  expect(w.text()).toContain('記録された結末')
  expect(w.find('#action-text').exists()).toBe(false)
  expect(w.get('.composer').text()).toContain('この冒険は完了しました')
  expect(button(w, '送信')).toBeUndefined()
  expect(button(w, '回復する行動を入力')).toBeUndefined()
  expect((button(w, '周囲を見る').element as HTMLButtonElement).disabled).toBe(true)
  await button(w, '新しい冒険へ').trigger('click'); await flushPromises()
  expect(w.find('#start-form').exists()).toBe(true)
})
it('fallback says state is saved without re-execution affordance', async () => {
  const t = { ...turn(), narration_status: 'fallback' as const, recovery: { fallback: true, reason: 'MODEL_TIMEOUT' } }
  const w = await render(server(path => path.endsWith('/state') ? response(campaign({ latest_turn: t })) : undefined))
  await w.get('[data-adventure="campaign-a"]').trigger('click'); await flushPromises()
  expect(w.text()).toContain('状態は保存済み'); expect(button(w, '同じ要求を再送')).toBeUndefined()
})
it('narration humanizes known public refs as text without changing the API value', async () => {
  const t = { ...turn(), narration: '@heroは@goblinを見た。@hero は <img src=x onerror=alert(1)> を読んだ。@unknown @hero_other' }
  const original = t.narration
  const w = await render(server(path => path.endsWith('/state') ? response(campaign({ latest_turn: t })) : undefined))
  await w.get('[data-adventure="campaign-a"]').trigger('click'); await flushPromises()
  expect(w.get('.narration').text()).toBe('葵はゴブリンを見た。葵 は <img src=x onerror=alert(1)> を読んだ。@unknown @hero_other')
  expect(w.find('img[src="x"]').exists()).toBe(false)
  expect(t.narration).toBe(original)
})
it.each([
  ['success', '技能判定：成功（合計19）'],
  ['failure', '技能判定：失敗（合計19）'],
] as const)('renders legacy %s skill facts in Japanese without rewriting saved text', async (outcome, displayed) => {
  const fact = `技能判定は${outcome}(合計19)`
  const t = { ...turn(), narration: 'successという文字を見つけた。', action_results: [
    { action_id: 'skill', ordinal: 1, result: { kind: 'applied' as const, outcome,
      facts: [fact, '@hero は success と書かれた紙を見た。'], dice: [] } },
  ] }
  const w = await render(server(path => path.endsWith('/state') ? response(campaign({ latest_turn: t })) : undefined))
  await w.get('[data-adventure="campaign-a"]').trigger('click'); await flushPromises()
  expect(w.get('.result-block').text()).toContain(displayed)
  expect(w.get('.result-block').text()).toContain('葵 は success と書かれた紙を見た。')
  expect(w.get('.narration').text()).toBe('successという文字を見つけた。')
  expect(t.action_results[0]!.result.facts[0]).toBe(fact)
})
it('keeps observed enemy names in history when the scene leaves combat', async () => {
  let completed = false
  const t = { ...turn(), narration: '@heroは@goblinから離れた。' }
  const api = server((path, init) => {
    if (init.method === 'POST') { completed = true; return response(t) }
    if (path.endsWith('/state') && completed) {
      const c = campaign({ state_version: 1, latest_turn: t }); c.adventure!.combat = null; return response(c)
    }
  })
  const w = await render(api); await w.get('[data-adventure="campaign-a"]').trigger('click'); await flushPromises()
  await button(w, '周囲を見る').trigger('click'); await flushPromises()
  expect(w.get('.narration').text()).toBe('葵はゴブリンから離れた。')
  expect(w.find('.combat').exists()).toBe(false)
})
it('inactive full-HP combat uses neutral wording rather than claiming the battle ended', async () => {
  const c = campaign(); c.adventure!.combat = { ...c.adventure!.combat!, active: false, current_hp: 10, max_hp: 10 }
  const w = await render(server(path => path.endsWith('/state') ? response(c) : undefined))
  await w.get('[data-adventure="campaign-a"]').trigger('click'); await flushPromises()
  expect(w.get('.combat').text()).toContain('戦闘なし')
  expect(w.get('.combat').text()).not.toContain('戦闘終了')
  expect(w.get('.combat').text()).toContain('10 / 10')
})
it('generated item choices display public names but emit original label and choice id', async () => {
  const original = '@iron_swordと@healing_potionを確認する'
  const c = campaign({ latest_turn: { ...turn('old', 0), choices: [{ id: 'item-choice', label: original }] } })
  c.player!.inventory = [
    { item_id: 'sword', item_ref: 'iron_sword', name: '鉄の剣', quantity: 1, equipped: true },
    { item_id: 'potion', item_ref: 'healing_potion', name: '回復ポーション', quantity: 1, equipped: false },
  ]
  const api = server((path, init) => path.endsWith('/state') ? response(c) : init.method === 'POST' ? response(turn('new', 0)) : undefined)
  const w = await render(api); await w.get('[data-adventure="campaign-a"]').trigger('click'); await flushPromises()
  const choice = button(w, '鉄の剣と回復ポーションを確認する')
  expect(choice).toBeDefined(); await choice.trigger('click'); await flushPromises()
  expect(JSON.parse(String(api.posts()[0]!.init.body)).content).toEqual({ kind: 'choice', choice_id: 'item-choice' })
  const events = w.findAllComponents({ name: 'TurnRecord' }).flatMap(r => r.emitted('choice') ?? [])
  expect(events).toEqual([['item-choice', original]])
  expect(c.latest_turn!.choices[0]!.label).toBe(original)
})
it('latest duplicate choices prefer registered actions while historical choices remain intact', async () => {
  const choices = [{ id: 'generated-look', label: '周囲を見る' }]
  const latest = { ...turn('latest', 0), choices }
  const older = { ...turn('older', 0), choices: [{ id: 'historical-look', label: '周囲を見る' }] }
  let current = campaign({ latest_turn: latest })
  const api = server((path, init) => {
    if (path.endsWith('/state')) return response(current)
    if (path.includes('/history?')) return response({ items: [
      { created_at: '2026-09-22T00:00:00Z', player_input: '以前の質問', turn: older },
      { created_at: '2026-09-22T00:01:00Z', player_input: '背景の質問', turn: latest },
    ], next_before_turn_id: null })
    if (init.method === 'POST') {
      current = campaign({ state_version: 1, latest_turn: turn('next', 1) })
      return response(current.latest_turn)
    }
  })
  const w = await render(api); await w.get('[data-adventure="campaign-a"]').trigger('click'); await flushPromises()
  expect(w.get('[data-turn-id="latest"]').find('.choices').exists()).toBe(false)
  const historical = w.get('[data-turn-id="older"] .choices button')
  expect(historical.text()).toBe('周囲を見る'); expect((historical.element as HTMLButtonElement).disabled).toBe(true)
  const record = w.findAllComponents({ name: 'TurnRecord' }).find(r => r.props('record').turn.turn_id === 'latest')!
  expect(record.props('record').turn.choices).toEqual([{ id: 'generated-look', label: '周囲を見る' }])
  await w.get('.registered-actions button').trigger('click'); await flushPromises()
  expect(JSON.parse(String(api.posts()[0]!.init.body)).content).toEqual({ kind: 'scenario_action', action_ref: 'look' })
  expect(record.emitted('choice')).toBeUndefined()
})
it('nonduplicate latest choices retain exact IDs and labels without trimming', async () => {
  const label = ' 周囲を見る'
  const latest = { ...turn('latest', 0), choices: [
    { id: 'duplicate', label: '周囲を見る' }, { id: 'distinct-choice', label },
  ] }
  const api = server((path, init) => path.endsWith('/state') ? response(campaign({ latest_turn: latest }))
    : init.method === 'POST' ? response(turn('next', 0)) : undefined)
  const w = await render(api); await w.get('[data-adventure="campaign-a"]').trigger('click'); await flushPromises()
  const choices = w.get('[data-turn-id="latest"]').findAll('.choices button')
  expect(choices).toHaveLength(1)
  await choices[0]!.trigger('click'); await flushPromises()
  expect(JSON.parse(String(api.posts()[0]!.init.body)).content).toEqual({ kind: 'choice', choice_id: 'distinct-choice' })
  const events = w.findAllComponents({ name: 'TurnRecord' }).flatMap(r => r.emitted('choice') ?? [])
  expect(events).toEqual([['distinct-choice', ' 周囲を見る']])
})
it('feedback downloads only optional entered text as UTF-8, with no network submission', async () => {
  const api = server(); const w = await render(api)
  const blobs: Blob[] = []
  vi.stubGlobal('URL', class extends URL { static createObjectURL(blob: Blob) { blobs.push(blob); return 'blob:feedback' }; static revokeObjectURL() {} })
  const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
  await w.get('#feedback-lost').setValue('入口で迷った')
  await w.get('.feedback form').trigger('submit')
  expect(blobs).toHaveLength(1); expect(blobs[0]!.type).toBe('text/plain;charset=utf-8')
  const content = await new Promise<string>(resolve => { const reader = new FileReader(); reader.onload = () => resolve(String(reader.result)); reader.readAsText(blobs[0]!) })
  expect(content).toContain('入口で迷った'); expect(content).not.toContain('principal-a'); expect(content).not.toContain('memory-only-csrf')
  expect(api.posts()).toHaveLength(0); expect(click).toHaveBeenCalledTimes(1)
  expect(w.text()).toContain('自動送信されません')
})
it('detached registered button cannot act on a different selected campaign', async () => {
  const api = server((path, init) => path === '/adventures' && init.method !== 'POST'
    ? response({ adventures: [saved, { ...saved, campaign_id: 'campaign-b', actor_id: 'actor-b' }] })
    : init.method === 'POST' ? response(turn()) : undefined)
  const w = await render(api)
  await w.get('[data-adventure="campaign-a"]').trigger('click'); await flushPromises()
  const oldButton = button(w, '周囲を見る')
  await w.get('[data-adventure="campaign-b"]').trigger('click'); await flushPromises()
  await oldButton.trigger('click'); await flushPromises()
  expect(api.posts()).toHaveLength(0)
})
it('reveals selected and new latest records, never jumps just because older history prepends', async () => {
  let latest = turn('recent', 0)
  const api = server((path, init) => {
    if (path.endsWith('/state')) return response(campaign({ latest_turn: latest, state_version: latest.committed_state_version! }))
    if (init.method === 'POST') { latest = turn('new', 1); return response(latest) }
    if (path.includes('/history?')) return response({ items: [{ created_at: '2026-09-22', player_input: '記録',
      turn: turn(path.includes('before_turn_id') ? 'older' : 'recent', 0) }], next_before_turn_id: path.includes('before_turn_id') ? null : 'recent' })
  })
  const w = await render(api); const scroll = vi.mocked(HTMLElement.prototype.scrollIntoView)
  await w.get('[data-adventure="campaign-a"]').trigger('click'); await flushPromises()
  expect(scroll).toHaveBeenCalled()
  expect((scroll.mock.contexts.at(-1) as HTMLElement).dataset.turnId).toBe('recent')
  scroll.mockClear()
  await button(w, '以前の履歴を読む').trigger('click'); await flushPromises()
  expect(scroll).not.toHaveBeenCalled()
  await button(w, '周囲を見る').trigger('click'); await flushPromises()
  expect((scroll.mock.contexts.at(-1) as HTMLElement).dataset.turnId).toBe('new')
  scroll.mockClear(); await button(w, '最新の結果へ').trigger('click')
  expect(scroll).toHaveBeenCalledTimes(1)
})
it('reveals the same latest record when narration arrives over SSE', async () => {
  Events.instances = []; vi.stubGlobal('EventSource', Events)
  let latest = waiting('live')
  const w = await render(server(path => path.endsWith('/state') ? response(campaign({ latest_turn: latest, state_version: latest.committed_state_version ?? 0 })) : undefined))
  await w.get('[data-adventure="campaign-a"]').trigger('click'); await flushPromises()
  const scroll = vi.mocked(HTMLElement.prototype.scrollIntoView); scroll.mockClear()
  latest = turn('live', 1); Events.instances[0]!.emit(latest); await flushPromises()
  expect(scroll).toHaveBeenCalled()
  expect((scroll.mock.contexts.at(-1) as HTMLElement).dataset.turnId).toBe('live')
  expect(w.get('.narration').text()).toBe('扉が開いた。')
})
it('reveals the latest result again after the initial history arrives on resume', async () => {
  const page = deferred<Response>(), latest = turn('recent', 1)
  const w = await render(server(path => path.endsWith('/state')
    ? response(campaign({ latest_turn: latest, state_version: 1 }))
    : path.includes('/history?') ? page.promise : undefined))
  await w.get('[data-adventure="campaign-a"]').trigger('click'); await flushPromises()
  const scroll = vi.mocked(HTMLElement.prototype.scrollIntoView)
  expect(scroll).toHaveBeenCalled(); scroll.mockClear()
  page.resolve(response({ items: [
    { created_at: '2026-09-22', player_input: '古い記録', turn: turn('older', 0) },
    { created_at: '2026-09-23', player_input: '最新の入力', turn: latest },
  ], next_before_turn_id: null }))
  await flushPromises()
  expect(w.findAll('[data-turn-id]').map(r => r.attributes('data-turn-id'))).toEqual(['older', 'recent'])
  expect(scroll).toHaveBeenCalledTimes(1)
  expect((scroll.mock.contexts.at(-1) as HTMLElement).dataset.turnId).toBe('recent')
})
