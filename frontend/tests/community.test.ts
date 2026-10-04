import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import App from '../src/App.vue'
import type { StoryPublicDetail } from '../src/story-contracts'
import { campaign, catalog, response, saved } from './fixtures'
import { story, storyServer } from './story-fixtures'
const wrappers: VueWrapper[] = []
const button = (w: VueWrapper, label: string) => w.findAll('button').find(b => b.text() === label)!
const detail = (id = 'public-a'): StoryPublicDetail => ({ story_id: id, story_version_id: 'version-1', ruleset_ref: 'mvp_v1', release_number: 1,
  visibility: 'public', lifecycle: 'active', metadata: story().draft.metadata, created_at: story().updated_at })
async function render(api = storyServer()) {
  vi.stubGlobal('fetch', api.fetch); const w = mount(App); wrappers.push(w); await flushPromises(); return { w, api }
}
beforeEach(() => {
  localStorage.clear(); sessionStorage.clear(); history.replaceState(null, '', '/')
  vi.spyOn(window, 'confirm').mockReturnValue(true)
  Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', { configurable: true, value: vi.fn() })
})
afterEach(() => { wrappers.splice(0).forEach(w => w.unmount()); vi.unstubAllGlobals() })

it('public search encodes filters, paginates with the same query, and only requests public projections', async () => {
  const { w, api } = await render(storyServer(path => path.startsWith('/public/stories?')
    ? response({ stories: [detail(path.includes('cursor=') ? 'public-b' : 'public-a')], next_cursor: path.includes('cursor=') ? null : 'next&cursor' })
    : path === '/public/stories/public-a' ? response(detail()) : undefined))
  await button(w, '公開作品を探す').trigger('click'); await flushPromises()
  await w.get('#story-search').setValue('海 & 空'); await w.get('#story-tag').setValue('短編')
  await w.get('.discovery-form').trigger('submit'); await flushPromises()
  await button(w, '次の作品を読み込む').trigger('click'); await flushPromises()
  const last = new URL(api.calls.filter(c => c.path.startsWith('/public/stories?')).at(-1)!.path, location.origin)
  expect(last.searchParams.get('q')).toBe('海 & 空'); expect(last.searchParams.get('tag')).toBe('短編'); expect(last.searchParams.get('cursor')).toBe('next&cursor')
  expect(w.findAll('[data-public-story]')).toHaveLength(2)
  await w.get('[data-public-story="public-a"]').trigger('click'); await flushPromises()
  expect(w.get('[aria-label="公開作品の詳細"]').text()).toContain('第1版')
  expect(api.calls.some(c => c.path.endsWith('/draft'))).toBe(false)
})

it('shows only own profile and quota, saves the display name, and hides them on logout', async () => {
  const { w, api } = await render(storyServer((path, init) => path.startsWith('/public/stories?') ? response({ stories: [], next_cursor: null })
    : path === '/profile' ? response({ principal_id: 'principal-a', display_name: init.method === 'PUT' ? JSON.parse(String(init.body)).display_name : '庭師' })
      : path === '/usage' ? response({ day: '2026-10-04', counts: { turn: 3 }, limits: { turn: 50 }, paused: false }) : undefined))
  await button(w, '公開作品を探す').trigger('click'); await flushPromises()
  await button(w, 'アカウント情報を取得').trigger('click'); await flushPromises()
  expect(w.get('[aria-label="自分の利用量"]').text()).toContain('3 / 50')
  await w.get('#profile-name').setValue('風の旅人')
  await w.get('.account-section form').trigger('submit'); await flushPromises()
  const call = api.puts().find(c => c.path === '/profile')!
  expect(JSON.parse(String(call.init.body))).toEqual({ display_name: '風の旅人' })
  expect(new Headers(call.init.headers).get('X-CSRF-Token')).toBe('memory-only-csrf')
  await button(w, 'ログアウト').trigger('click'); await flushPromises()
  expect(w.find('#profile-name').exists()).toBe(false); expect(w.text()).not.toContain('3 / 50')
})

it('report sends only reason and request ID and reuses the exact request after uncertain acceptance', async () => {
  let attempt = 0
  const { w, api } = await render(storyServer(path => {
    if (path.startsWith('/public/stories?')) return response({ stories: [detail()], next_cursor: null })
    if (path === '/public/stories/public-a') return response(detail())
    if (path.endsWith('/reports')) { if (!attempt++) throw new TypeError('lost response'); return response({ report_id: 'report-receipt', received_at: story().updated_at }) }
  }))
  await button(w, '公開作品を探す').trigger('click'); await flushPromises()
  await w.get('[data-public-story="public-a"]').trigger('click'); await flushPromises()
  await button(w, 'この作品を通報する').trigger('click')
  await w.get('#report-reason').setValue('注意事項に書かれていない内容がある')
  await w.get('[aria-label="公開作品の詳細"] form').trigger('submit'); await flushPromises()
  expect(button(w, '同じ通報を再送')).toBeDefined()
  await w.get('[aria-label="公開作品の詳細"] form').trigger('submit'); await flushPromises()
  const calls = api.posts().filter(c => c.path.endsWith('/reports'))
  expect(calls).toHaveLength(2); expect(calls[0]!.init.body).toBe(calls[1]!.init.body)
  expect(Object.keys(JSON.parse(String(calls[0]!.init.body))).sort()).toEqual(['reason', 'request_id'])
  expect(w.text()).toContain('受付番号: report-receipt')
})

it.each([true, false])('offers opt-in registration only when enabled=%s', async registration_enabled => {
  const { w } = await render(storyServer(path => path === '/auth/options' ? response({ registration_enabled }) : path === '/auth/session' ? response({}, 401) : undefined))
  expect(w.find('a[href="/auth/login?join=true"]').exists()).toBe(registration_enabled)
  expect(w.find('a[href="/auth/login"]').exists()).toBe(true)
})

it('an unlisted v2 link needs ability allocation, starts by version only, and survives a lost start response on reload', async () => {
  history.replaceState(null, '', '/?story=shared-a')
  let attempt = 0
  const api = storyServer((path, init) => {
    if (path === '/stories/shared-a') return response({ ...detail('shared-a'), visibility: 'unlisted', ruleset_ref: 'mvp_v2' })
    if (path === '/adventures/catalog') return response({ ...catalog, presets: catalog.presets.map(p => ({ ...p, base_abilities: { strength: 0, agility: 1, insight: 1, presence: 0 } })) })
    if (path === '/adventures' && init.method === 'POST') { if (!attempt++) throw new TypeError('lost response'); return response(saved) }
  })
  const { w } = await render(api)
  await w.get('#player-name').setValue('旅人')
  expect(button(w, '冒険を始める').attributes('disabled')).toBeDefined()
  await w.get('#ability-agility').setValue('1'); await w.get('#ability-insight').setValue('1'); await w.get('#specialty').setValue('perception')
  await w.get('#start-form').trigger('submit'); await flushPromises()
  const first = api.posts().find(c => c.path === '/adventures')!
  const body = JSON.parse(String(first.init.body))
  expect(body).toMatchObject({ story_version_id: 'version-1', specialty_skill: 'perception' }); expect(body).not.toHaveProperty('scenario_ref')
  w.unmount(); const again = await render(api)
  await button(again.w, '同じ要求を再送').trigger('click'); await flushPromises()
  expect(api.posts().filter(c => c.path === '/adventures')[1]!.init.body).toBe(first.init.body)
})

it('catalog refresh never silently switches a displayed version before start', async () => {
  let version = 'version-1'
  const { w, api } = await render(storyServer((path, init) => path === '/adventures/catalog'
    ? response({ ...catalog, scenarios: [{ ...catalog.scenarios[0], story_version_id: version, story_id: 'story-a' }] })
    : path === '/adventures' && init.method === 'POST' ? response({}, 409) : undefined))
  version = 'version-2'
  await button(w, '一覧を更新').trigger('click'); await flushPromises()
  expect(w.text()).toContain('表示中の版は自動で切り替わりません')
  await w.get('#player-name').setValue('旅人'); await w.get('#start-form').trigger('submit'); await flushPromises()
  expect(JSON.parse(String(api.posts()[0]!.init.body)).story_version_id).toBe('version-1')
  await button(w, '更新された版を選び直す').trigger('click')
  await w.get('#start-form').trigger('submit'); await flushPromises()
  expect(JSON.parse(String(api.posts()[1]!.init.body)).story_version_id).toBe('version-2')
})

it('debug is opt-in and limited to the created author playtest, never a normal run', async () => {
  const { w, api } = await render(storyServer(path => path.endsWith('/debug') ? response({ campaign_id: 'playtest-a', story_version_id: 'snapshot-a', current_scene_ref: 'gate', status: 'active', flags: ['secret_flag'],
    actions: [{ definition: { label: '種を拾う' }, missing_flags: ['seed'], blocking_flags: [] }], endings: [{ definition: { title: '秘密の結末', summary: '作者だけが知る内容' }, missing_flags: ['seed'] }] }) : undefined))
  await w.get('[data-adventure="campaign-a"]').trigger('click'); await flushPromises()
  expect(w.find('.author-debug').exists()).toBe(false)
  await button(w, 'マイ作品').trigger('click'); await flushPromises()
  await w.get('[data-story="story-a"]').trigger('click'); await flushPromises()
  await button(w, '保存して最新原稿を試遊').trigger('click'); await flushPromises()
  expect(api.calls.some(c => c.path.endsWith('/debug'))).toBe(false)
  await button(w, 'マイ作品').trigger('click'); await flushPromises()
  expect(w.get('.author-debug').attributes('open')).toBeUndefined()
  await button(w, '試遊情報を再取得').trigger('click'); await flushPromises()
  expect(w.text()).toContain('秘密の結末')
  const call = api.calls.find(c => c.path.endsWith('/debug'))!
  expect(call.path).toBe('/stories/story-a/playtests/playtest-a/debug'); expect(call.init.method).toBeUndefined()
  await button(w, 'ログアウト').trigger('click'); await flushPromises()
  expect(w.html()).not.toContain('秘密の結末'); expect(w.html()).not.toContain('secret_flag')
})
