import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import App from '../src/App.vue'
import { catalog, response } from './fixtures'
import { story, storyServer, templates } from './story-fixtures'
import { node, rows } from '../src/story-contracts'

const wrappers: VueWrapper[] = []
const button = (w: VueWrapper, label: string) => w.findAll('button').find(b => b.text() === label)!
async function render(api = storyServer()) {
  vi.stubGlobal('fetch', api.fetch)
  const w = mount(App); wrappers.push(w); await flushPromises()
  return { w, api }
}
async function author(w: VueWrapper) {
  await button(w, 'マイ作品').trigger('click'); await flushPromises()
  await w.get('[data-story="story-a"]').trigger('click'); await flushPromises()
}
beforeEach(() => {
  localStorage.clear(); sessionStorage.clear(); history.replaceState(null, '', '/')
  vi.spyOn(window, 'confirm').mockReturnValue(true)
  Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', { configurable: true, writable: true, value: vi.fn() })
})
afterEach(() => { wrappers.splice(0).forEach(w => w.unmount()); vi.unstubAllGlobals(); vi.useRealTimers() })

it('offers both registry templates, escapes authored text, and hides all author content on logout', async () => {
  const malicious = '<img src=x onerror=alert(1)>'
  const value = story(); value.draft.metadata.title = malicious
  const { w } = await render(storyServer((path, init) => path.endsWith('/draft') && init.method !== 'PUT' ? response(value) : undefined))
  await button(w, 'マイ作品').trigger('click'); await flushPromises()
  expect(w.findAll('[data-template]')).toHaveLength(2)
  await w.get('[data-story="story-a"]').trigger('click'); await flushPromises()
  expect(w.text()).toContain(malicious)
  expect(w.find('.author-page img').exists()).toBe(false)
  expect((w.get('#author-notes').element as HTMLTextAreaElement).value).toBe('作者の秘密')
  await button(w, 'ログアウト').trigger('click'); await flushPromises()
  expect(w.find('.author-page').exists()).toBe(false)
  expect(w.html()).not.toContain('作者の秘密'); expect(w.html()).not.toContain('鍵は塔にある')
})

it('forms edit all core fields using labels and selectors, with no raw ref or JSON input', async () => {
  const { w, api } = await render(); await author(w)
  await w.get('[id="/metadata/title"]').setValue('海辺の庭')
  await w.get('[id="/metadata/synopsis"]').setValue('海の風を感じる短編')
  await w.get('[id="/scenario/objective"]').setValue('庭師を助ける')
  await w.get('[id="/scenario/world/boundary"]').setValue('庭と浜辺')
  await w.get('[id="/scenario/scenes/0/description"]').setValue('波が聞こえる')
  await w.get('[id="/scenario/initialization/characters/0/label"]').setValue('海の庭師')
  await w.get('[id="/scenario/flags/0/public_fact"]').setValue('種をもらった')
  await w.get('[id="/scenario/endings/0/summary"]').setValue('庭師に感謝される')
  await w.get('[id="policy-/scenario/objective"]').setValue('undecided')
  const destinations = w.get('[id="/scenario/world/goal_scene_ref"]')
  expect(destinations.text()).toContain('庭の門')
  expect(w.find('input[id$="scene_ref"]').exists()).toBe(false)
  expect(w.find('textarea[id*="json"]').exists()).toBe(false)
  await button(w, '今すぐ保存').trigger('click'); await flushPromises()
  const sent = JSON.parse(String(api.puts()[0]!.init.body)).draft
  expect(sent.metadata).toMatchObject({ title: '海辺の庭', synopsis: '海の風を感じる短編' })
  expect(sent.scenario.world.goal_scene_ref).toBe('gate')
  expect(sent.scenario.scenes[0].description).toBe('波が聞こえる')
  expect(sent.scenario.initialization.characters[0].label).toBe('海の庭師')
  expect(sent.field_policies['/scenario/objective']).toBe('undecided')
})

it('refuses unsupported templates and changing action kind removes incompatible fields', async () => {
  const available = templates(); available.push({ ...available[0]!, template_id: 'future', required_capabilities: ['custom_javascript'] })
  const { w, api } = await render(storyServer(path => path === '/stories/templates' ? response({ templates: available }) : undefined))
  await author(w)
  expect(w.get('[data-template="future"]').attributes('disabled')).toBeDefined()
  await w.get('#action-kind-0-0').setValue('skill_check')
  await button(w, '今すぐ保存').trigger('click'); await flushPromises()
  const action = JSON.parse(String(api.puts()[0]!.init.body)).draft.scenario.scenes[0].actions[0]
  expect(action.kind).toBe('skill_check'); expect(action).not.toHaveProperty('public_fact')
  expect(action).toMatchObject({ action_ref: 'pick', check_ref: 'normal', skill_ref: 'perception', success: {}, failure: {} })
})

it('playtest from the actual author view opens the existing gameplay with a snapshot response', async () => {
  const { w, api } = await render(); await author(w)
  await w.get('[id="/metadata/title"]').setValue('試遊の直前に変更')
  await button(w, '保存して最新原稿を試遊').trigger('click'); await flushPromises()
  expect(w.find('.author-page').exists()).toBe(false)
  expect(w.find('[aria-label="冒険の物語"]').exists()).toBe(true)
  expect(api.posts().filter(c => c.path.endsWith('/playtests'))).toHaveLength(1)
  expect(api.calls.some(c => c.path === '/campaigns/playtest-a/state')).toBe(true)
  await button(w, 'マイ作品').trigger('click'); await flushPromises()
  expect((w.get('[id="/metadata/title"]').element as HTMLInputElement).value).toBe('試遊の直前に変更')
  expect(w.find('.character-panel').exists()).toBe(false)
})

it('blocks navigation cancellation, renders warning coverage and requires manual publish acknowledgement', async () => {
  const { w, api } = await render(); await author(w)
  await w.get('#author-notes').setValue('未保存の秘密')
  vi.mocked(window.confirm).mockReturnValue(false)
  await button(w, '＋ 新しい冒険').trigger('click')
  expect(w.find('.author-page').exists()).toBe(true)
  await button(w, '原稿を検証').trigger('click'); await flushPromises()
  expect(w.text()).toContain('未検証')
  expect(button(w, '確認した原稿を公開').attributes('disabled')).toBeDefined()
  await w.get('#author-completion').setValue(true)
  expect(button(w, '確認した原稿を公開').attributes('disabled')).toBeDefined()
  await w.get('input[type="checkbox"][value="freeform_unverified"]').setValue(true)
  await button(w, '確認した原稿を公開').trigger('click'); await flushPromises()
  expect(w.get('#story-share-link').attributes('readonly')).toBeDefined()
  expect((w.get('#story-share-link').element as HTMLInputElement).value).toContain('/?story=story-a')
  expect(w.text()).toContain('第1版')
  expect(api.posts().some(c => c.path.endsWith('/publish'))).toBe(true)
})

it('published catalog start retains legacy refs and pins the story version', async () => {
  const { w, api } = await render(storyServer(path => path === '/adventures/catalog'
    ? response({ ...catalog, scenarios: [{ ...catalog.scenarios[0], story_id: 'story-a', story_version_id: 'published-v1' }] }) : undefined))
  await w.get('#player-name').setValue('訪問者')
  await w.get('#start-form').trigger('submit'); await flushPromises()
  expect(JSON.parse(String(api.posts()[0]!.init.body))).toMatchObject({ scenario_ref: 'chapel', scenario_version: 2, story_version_id: 'published-v1' })
})

it('an unlisted link reads public detail only and starts the displayed immutable version', async () => {
  history.replaceState(null, '', '/?story=shared-story')
  const { w, api } = await render(storyServer(path => path === '/stories/shared-story'
    ? response({ story_id: 'shared-story', story_version_id: 'shared-v2', release_number: 2, visibility: 'unlisted', lifecycle: 'active',
      metadata: story().draft.metadata, created_at: story().updated_at, ruleset_ref: 'mvp_v1' }) : undefined))
  expect(w.get('[aria-label="共有された作品"]').text()).toContain('第2版')
  expect(api.calls.some(c => c.path.endsWith('/draft'))).toBe(false)
  await w.get('#player-name').setValue('訪問者')
  await w.get('#start-form').trigger('submit'); await flushPromises()
  const body = JSON.parse(String(api.posts()[0]!.init.body))
  expect(body).toMatchObject({ story_version_id: 'shared-v2' })
  expect(body).not.toHaveProperty('scenario_ref'); expect(body).not.toHaveProperty('ability_points')
})
