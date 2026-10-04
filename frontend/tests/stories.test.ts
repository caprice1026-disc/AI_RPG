import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { createGame } from '../src/game'
import { createStories, type Stories } from '../src/stories'
import { clone, node, rows } from '../src/story-contracts'
import { deferred, flush, response } from './fixtures'
import { story, storyServer } from './story-fixtures'

const cleanup: (() => void)[] = []
async function setup(api = storyServer()) {
  const game = createGame({ fetch: api.fetch }); await game.boot()
  const played = vi.fn(), editor = createStories(game, played)
  cleanup.push(() => { editor.dispose(); game.dispose() })
  await editor.load(); await editor.open('story-a')
  return { game, editor, played, api }
}
beforeEach(() => { vi.useFakeTimers(); localStorage.clear(); vi.spyOn(window, 'confirm').mockReturnValue(true) })
afterEach(() => { cleanup.splice(0).forEach(fn => fn()); vi.useRealTimers() })

it('debounces all authoring changes for two seconds and sends revision, request ID, and CSRF', async () => {
  const { editor, api } = await setup()
  editor.state.current!.draft.metadata.title = '新しいタイトル'
  await vi.advanceTimersByTimeAsync(1500)
  editor.state.current!.draft.notes = '追加した秘密'
  await vi.advanceTimersByTimeAsync(1999)
  expect(api.puts()).toHaveLength(0)
  await vi.advanceTimersByTimeAsync(1)
  expect(api.puts()).toHaveLength(1)
  const call = api.puts()[0]!, body = JSON.parse(String(call.init.body))
  expect(call.path).toBe('/stories/story-a/draft')
  expect(body).toMatchObject({ expected_revision: 1, draft: { metadata: { title: '新しいタイトル' }, notes: '追加した秘密' } })
  expect(body.request_id).toMatch(/^[\da-f-]{36}$/)
  expect(new Headers(call.init.headers).get('X-CSRF-Token')).toBe('memory-only-csrf')
  expect(editor.state.current!.revision).toBe(2)
  expect(editor.state.saveStatus).toBe('saved'); expect(editor.dirty.value).toBe(false)
})

it('two editors use CAS; the losing editor retains input until explicitly comparing and resolving', async () => {
  const api = storyServer(), first = await setup(api), second = await setup(api)
  first.editor.state.current!.draft.metadata.title = '保存されたタイトル'
  await first.editor.save()
  second.editor.state.current!.draft.metadata.title = '別タブの入力'
  await second.editor.save()
  expect(second.editor.state.saveStatus).toBe('conflict')
  expect(second.editor.state.current!.draft.metadata.title).toBe('別タブの入力')
  await vi.advanceTimersByTimeAsync(5000)
  expect(api.puts()).toHaveLength(2)
  await second.editor.compareRemote()
  expect(second.editor.state.remote!.draft.metadata.title).toBe('保存されたタイトル')
  expect(second.editor.state.current!.revision).toBe(1)
  await second.editor.keepLocal()
  expect(JSON.parse(String(api.puts()[2]!.init.body)).expected_revision).toBe(2)
  expect(api.stored().draft.metadata.title).toBe('別タブの入力')
  expect(second.editor.state.current!.revision).toBe(3)
})

it('a lost save response is retried byte for byte before saving newer input', async () => {
  let lost = true
  const underlying = storyServer()
  const fetcher = async (input: RequestInfo | URL, init: RequestInit = {}) => {
    const result = await underlying.fetch(input, init)
    if (init.method === 'PUT' && lost) { lost = false; throw new TypeError('response lost after commit') }
    return result
  }
  const api = { ...underlying, fetch: fetcher }, { editor } = await setup(api)
  editor.state.current!.draft.metadata.title = '保存済みだが応答を失った入力'
  await editor.save()
  expect(underlying.stored().revision).toBe(2); expect(editor.state.saveStatus).toBe('failed')
  editor.state.current!.draft.metadata.title = '応答喪失後の追加入力'
  await vi.advanceTimersByTimeAsync(3000)
  expect(api.puts()).toHaveLength(1)
  await editor.save()
  expect(api.puts()[1]!.init.body).toBe(api.puts()[0]!.init.body)
  expect(editor.state.current!.draft.metadata.title).toBe('応答喪失後の追加入力')
  expect(editor.state.current!.revision).toBe(2)
  await vi.advanceTimersByTimeAsync(2000)
  const last = JSON.parse(String(api.puts()[2]!.init.body))
  expect(last.expected_revision).toBe(2); expect(last.request_id).not.toBe(JSON.parse(String(api.puts()[0]!.init.body)).request_id)
  expect(editor.state.current!.revision).toBe(3); expect(editor.dirty.value).toBe(false)
})

it('server-assigned IDs merge into the edited objects without overwriting in-flight edits', async () => {
  const pending = deferred<Response>()
  const api = storyServer((path, init) => path.endsWith('/draft') && init.method === 'PUT' ? pending.promise : undefined)
  const { editor } = await setup(api)
  const entries = rows(editor.state.current!.draft.scenario.scenes)
  entries.push({ title: '新しい場所', sequence: 2, description: '', actions: [] })
  const save = editor.save(); await flush()
  entries[1]!.title = '保存中に変えた名前'
  node(editor.state.current!.draft.scenario.world).goal_scene_ref = ''
  const reply = clone(story(2)); reply.draft = JSON.parse(String(api.puts()[0]!.init.body)).draft
  rows(reply.draft.scenario.scenes)[1]!.scene_ref = 'server-assigned-scene'
  pending.resolve(response(reply)); await save
  expect(entries[1]).toMatchObject({ title: '保存中に変えた名前', scene_ref: 'server-assigned-scene' })
  expect(node(editor.state.current!.draft.scenario.world).goal_scene_ref).toBe('')
  expect(editor.dirty.value).toBe(true)
})

it('warns on unsaved navigation and unload, and stops warning after successful save', async () => {
  const { editor } = await setup()
  editor.state.current!.draft.notes = '未保存'
  vi.mocked(window.confirm).mockReturnValue(false)
  expect(editor.confirmLeave()).toBe(false)
  const event = new Event('beforeunload', { cancelable: true }); window.dispatchEvent(event)
  expect(event.defaultPrevented).toBe(true)
  await editor.save()
  expect(editor.confirmLeave()).toBe(true)
  const after = new Event('beforeunload', { cancelable: true }); window.dispatchEvent(after)
  expect(after.defaultPrevented).toBe(false)
})

it('flushes the latest revision before snapshot playtest, refreshes the list, and selects its adventure directly', async () => {
  const { editor, game, api, played } = await setup()
  editor.state.current!.draft.metadata.title = '試遊前の編集'
  await editor.playtest()
  const call = api.posts().find(c => c.path.endsWith('/playtests'))!
  expect(JSON.parse(String(call.init.body))).toMatchObject({ expected_revision: 2, preset_ref: 'scout', player_name: '作者', specialty_skill: 'perception' })
  expect(api.posts().filter(c => c.path === '/adventures')).toHaveLength(0)
  const playIndex = api.calls.indexOf(call), home = api.calls.findIndex((c, i) => i > playIndex && c.path === '/adventures')
  const stateIndex = api.calls.findIndex((c, i) => i > playIndex && c.path === '/campaigns/playtest-a/state')
  expect(home).toBeGreaterThan(playIndex); expect(stateIndex).toBeGreaterThan(home)
  expect(game.state.selected).toEqual({ campaign_id: 'playtest-a', actor_id: 'playtester-a' })
  expect(editor.state.playtest!.story_version_id).toBe('snapshot-a'); expect(played).toHaveBeenCalledOnce()
})

it('does not start a playtest when draft saving conflicts', async () => {
  const { editor, api } = await setup(storyServer((path, init) => path.endsWith('/draft') && init.method === 'PUT' ? response({}, 409) : undefined))
  editor.state.current!.draft.notes = '未保存'
  await editor.playtest()
  expect(api.posts().some(c => c.path.endsWith('/playtests'))).toBe(false)
  expect(editor.state.current!.draft.notes).toBe('未保存')
})

it('requires explicit author completion and every warning code; editing invalidates the report and acknowledgements', async () => {
  const { editor, api } = await setup()
  await editor.validate()
  expect(editor.canPublish.value).toBe(false)
  await editor.publish(); expect(api.posts().some(c => c.path.endsWith('/publish'))).toBe(false)
  editor.state.acknowledged = true
  expect(editor.canPublish.value).toBe(false)
  editor.state.warningCodes = ['freeform_unverified']
  expect(editor.canPublish.value).toBe(true)
  await editor.publish()
  expect(JSON.parse(String(api.posts().find(c => c.path.endsWith('/publish'))!.init.body))).toMatchObject({
    expected_revision: 1, validation_report_id: 'report-a', author_playtest_acknowledged: true, acknowledged_warning_codes: ['freeform_unverified'], visibility: 'unlisted',
  })
  expect(editor.state.published!.story_version_id).toBe('release-a')
  editor.state.current!.draft.scenario.objective = '違う目的'
  expect(editor.state.report).toBeNull(); expect(editor.state.acknowledged).toBe(false); expect(editor.canPublish.value).toBe(false)
})

it('restores as a new revision and duplicates into a new story after saving pending input', async () => {
  const { editor, api } = await setup()
  await editor.history(); expect(editor.state.revisions).toHaveLength(1)
  editor.state.current!.draft.notes = '現在の入力'
  await editor.restore(1)
  expect(JSON.parse(String(api.posts().find(c => c.path.endsWith('/restore'))!.init.body))).toMatchObject({ expected_revision: 2, revision: 1 })
  expect(editor.state.current!.revision).toBe(3)
  await editor.duplicate()
  expect(editor.state.current!.story_id).toBe('story-copy')
})

it('retries an uncertain mutation with its original request ID and payload', async () => {
  let attempts = 0
  const { editor, api } = await setup(storyServer((path) => {
    if (path.endsWith('/duplicate') && !attempts++) throw new TypeError('lost response')
  }))
  await editor.duplicate(); expect(editor.state.pendingOperation).not.toBeNull()
  await editor.retryOperation()
  const calls = api.posts().filter(c => c.path.endsWith('/duplicate'))
  expect(calls).toHaveLength(2); expect(calls[1]!.init.body).toBe(calls[0]!.init.body)
  expect(editor.state.pendingOperation).toBeNull()
})

it('logout and late responses cannot reveal author content or autosave into a different session', async () => {
  const pending = deferred<Response>()
  const { editor, game, api } = await setup(storyServer((path, init) => path.endsWith('/draft') && init.method === 'PUT' ? pending.promise : undefined))
  editor.state.current!.draft.notes = '隠す必要がある秘密'
  const save = editor.save(); await flush(); await game.logout()
  expect(editor.state.current).toBeNull(); expect(editor.state.stories).toHaveLength(0)
  pending.resolve(response(story(2))); await save; await vi.advanceTimersByTimeAsync(3000)
  expect(editor.state.current).toBeNull(); expect(api.puts()).toHaveLength(1)
  expect(localStorage.getItem('ai-rpg:vue:principal-a:draft')).toBeNull()
})

it('rejected save input can be corrected without reusing a definitively rejected request', async () => {
  let rejected = true
  const { editor, api } = await setup(storyServer((path, init) => path.endsWith('/draft') && init.method === 'PUT' && rejected ? response({}, 422) : undefined))
  editor.state.current!.draft.notes = '不正な入力'; await editor.save()
  expect(editor.state.retryExact).toBe(false)
  rejected = false; editor.state.current!.draft.notes = '修正済み'; await editor.save()
  expect(JSON.parse(String(api.puts()[1]!.init.body)).request_id).not.toBe(JSON.parse(String(api.puts()[0]!.init.body)).request_id)
  expect(editor.state.current!.revision).toBe(2)
})
