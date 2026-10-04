import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import App from '../src/App.vue'
import type { AuthoringJob } from '../src/story-ai-contracts'
import { deferred, response } from './fixtures'
import { report, story, storyServer, summary } from './story-fixtures'
const wrappers: VueWrapper[] = []
const button = (w: VueWrapper, label: string) => w.findAll('button').find(b => b.text() === label)!
function job(overrides: Partial<AuthoringJob> = {}): AuthoringJob {
  return { id: 'job-a', story_id: 'story-a', base_revision: 1, snapshot_hash: 'hash', kind: 'fill', state: 'succeeded', model_id: 'configured', actual_model: null,
    attempts: 1, physical_requests: 1, input_tokens: 100, output_tokens: 50, usage_complete: true, error_code: null,
    created_at: story().updated_at, updated_at: story().updated_at, outline: null, outline_revision: 0, approved_outline_revision: null,
    proposal: { id: 'proposal-a', job_id: 'job-a', story_id: 'story-a', base_revision: 1, decision: 'pending', applied_revision: null, adopted_change_ids: [], findings: [], validation: report(),
      changes: [{ id: 'change-a', field_path: '/metadata/title', operation: 'set', before_exists: true, before: '風の庭', after: '空の庭', reason: '舞台に合う名称', policy: 'fillable' },
        { id: 'change-b', field_path: '/metadata/synopsis', operation: 'set', before_exists: true, before: '', after: '空の上の冒険', reason: '紹介文の補完', policy: 'undecided' }] },
    ...overrides }
}
async function render(api = storyServer()) {
  vi.stubGlobal('fetch', api.fetch)
  const w = mount(App); wrappers.push(w); await flushPromises()
  await button(w, 'マイ作品').trigger('click'); await flushPromises()
  await w.get('[data-story="story-a"]').trigger('click'); await flushPromises()
  return { w, api }
}
beforeEach(() => {
  localStorage.clear(); sessionStorage.clear(); history.replaceState(null, '', '/')
  vi.spyOn(window, 'confirm').mockReturnValue(true)
  Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', { configurable: true, value: vi.fn() })
})
afterEach(() => { wrappers.splice(0).forEach(w => w.unmount()); vi.useRealTimers(); vi.unstubAllGlobals() })

it('autosave does not invoke AI; explicit check saves first and pins the base revision', async () => {
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
  const { w, api } = await render(storyServer((path, init) => path.endsWith('/authoring-jobs') && init.method === 'POST' ? response(job({ kind: 'check', base_revision: JSON.parse(String(init.body)).base_revision })) : undefined))
  await w.get('[id="/metadata/title"]').setValue('編集中の題名')
  await vi.advanceTimersByTimeAsync(2000); await flushPromises()
  expect(api.puts()).toHaveLength(1)
  expect(api.posts().some(c => c.path.endsWith('/authoring-jobs'))).toBe(false)
  await w.get('#ai-instructions').setValue('結末の条件を確認して')
  await button(w, '整合性をチェック').trigger('click'); await flushPromises()
  expect(JSON.parse(String(api.posts().find(c => c.path.endsWith('/authoring-jobs'))!.init.body))).toMatchObject({ kind: 'check', base_revision: 2, instructions: '結末の条件を確認して' })
})

it('AI proposals require explicit selection, apply only selected IDs, and invalidate old validation', async () => {
  const value = story(2); value.draft.metadata.title = '空の庭'
  const { w, api } = await render(storyServer((path, init) => path.endsWith('/authoring-jobs') && init.method === 'POST' ? response(job()) : path.endsWith('/apply') ? response(value) : undefined))
  await button(w, '原稿を検証').trigger('click'); await flushPromises()
  await button(w, '不足部分の補完を提案').trigger('click'); await flushPromises()
  expect(button(w, '選択した提案を採用').attributes('disabled')).toBeDefined()
  expect(w.text()).toContain('変更前'); expect(w.text()).toContain('未決定の項目')
  expect((w.get('[id="/metadata/title"]').element as HTMLInputElement).value).toBe('風の庭')
  await w.get('input[value="change-a"]').setValue(true)
  await button(w, '選択した提案を採用').trigger('click'); await flushPromises()
  const call = api.posts().find(c => c.path.endsWith('/apply'))!
  expect(JSON.parse(String(call.init.body))).toMatchObject({ expected_revision: 1, change_ids: ['change-a'] })
  expect((w.get('[id="/metadata/title"]').element as HTMLInputElement).value).toBe('空の庭')
  expect(w.text()).toContain('下書き 2 に採用済み')
  expect(w.get('#author-completion').attributes('disabled')).toBeDefined()
})

it('editing after generation prevents adopting a stale proposal and keeps both inputs visible', async () => {
  const { w, api } = await render(storyServer((path, init) => path.endsWith('/authoring-jobs') && init.method === 'POST' ? response(job()) : undefined))
  await button(w, '不足部分の補完を提案').trigger('click'); await flushPromises()
  await w.get('input[value="change-a"]').setValue(true)
  await w.get('[id="/metadata/title"]').setValue('作者による新しい入力')
  expect(button(w, '選択した提案を採用').attributes('disabled')).toBeDefined()
  expect(w.text()).toContain('生成元から原稿が変わっています')
  expect(w.text()).toContain('空の庭')
  expect(api.posts().some(c => c.path.endsWith('/apply'))).toBe(false)
  await button(w, '今すぐ保存').trigger('click'); await flushPromises()
  expect(button(w, '選択した提案を採用').attributes('disabled')).toBeDefined()
})

it('outline edits require saving and explicit approval before concretization', async () => {
  let current = job({ kind: 'outline', proposal: null, outline_revision: 1,
    outline: { title: '庭', premise: '庭の謎', scenes: ['門', '塔'], characters: ['庭師'], endings: ['帰還'], undecided: ['鍵の所在'] } })
  const { w, api } = await render(storyServer((path, init) => {
    if (path.endsWith('/authoring-jobs') && init.method === 'POST') return response(JSON.parse(String(init.body)).kind === 'concretize' ? job({ id: 'concrete-a', kind: 'concretize' }) : current)
    if (path.endsWith('/outline') && init.method === 'PUT') { current = { ...current, outline: JSON.parse(String(init.body)).outline, outline_revision: 2, approved_outline_revision: null }; return response(current) }
    if (path.endsWith('/outline/approve')) { current = { ...current, approved_outline_revision: 2 }; return response(current) }
  }))
  await button(w, '条件から構成案を作る').trigger('click'); await flushPromises()
  expect(button(w, '承認した構成案を具体化').attributes('disabled')).toBeDefined()
  await w.get('[id="/ai-outline/premise"]').setValue('作者が決めた庭の謎')
  expect(button(w, 'この構成案を承認').attributes('disabled')).toBeDefined()
  await button(w, '構成案の修正を保存').trigger('click'); await flushPromises()
  expect(JSON.parse(String(api.puts()[0]!.init.body))).toMatchObject({ expected_outline_revision: 1, outline: { premise: '作者が決めた庭の謎' } })
  await button(w, 'この構成案を承認').trigger('click'); await flushPromises()
  expect(JSON.parse(String(api.posts().find(c => c.path.endsWith('/approve'))!.init.body)).expected_outline_revision).toBe(2)
  await button(w, '承認した構成案を具体化').trigger('click'); await flushPromises()
  expect(JSON.parse(String(api.posts().filter(c => c.path.endsWith('/authoring-jobs'))[1]!.init.body))).toMatchObject({ kind: 'concretize', base_revision: 1, outline_job_id: 'job-a', approved_outline_revision: 2 })
})

it.each(['success', 'unknown'])('shows outline-save latency as in-flight, retaining exact replay only for an %s outcome', async outcome => {
  const first = deferred<Response>(), retry = deferred<Response>()
  const current = job({ kind: 'outline', proposal: null, outline_revision: 1,
    outline: { title: '庭', premise: '庭の謎', scenes: ['門'], characters: [], endings: [], undecided: [] } })
  const saved = { ...current, outline_revision: 2, outline: { ...current.outline!, title: '作者が決めた庭' } }
  let attempts = 0
  const { w, api } = await render(storyServer((path, init) => {
    if (path.endsWith('/authoring-jobs')) return response({ jobs: [current] })
    if (path.endsWith('/outline') && init.method === 'PUT') return ++attempts === 1 ? first.promise : retry.promise
  }))
  await w.get('[id="/ai-outline/title"]').setValue('作者が決めた庭')
  const saveButton = button(w, '構成案の修正を保存')
  await saveButton.trigger('click'); await flushPromises()
  expect(api.puts()).toHaveLength(1)
  expect(button(w, '同じAI要求を再試行')).toBeUndefined()
  expect(w.get('[aria-label="AI作成補助"]').text()).toContain('AI操作中です')
  expect(w.get('.outline-form').attributes('disabled')).toBeDefined()
  expect(w.get('#ai-job-history').attributes('disabled')).toBeDefined()
  await saveButton.trigger('click'); await flushPromises()
  expect(api.puts()).toHaveLength(1)
  expect((w.get('[id="/ai-outline/title"]').element as HTMLInputElement).value).toBe('作者が決めた庭')
  first.resolve(outcome === 'unknown' ? response({}, 503) : response(saved)); await flushPromises()
  if (outcome === 'unknown') {
    expect(w.get('[aria-label="AI作成補助"] [role="alert"]').text()).toContain('結果が未確認')
    expect(button(w, '同じAI要求を再試行').attributes('disabled')).toBeUndefined()
    await button(w, '同じAI要求を再試行').trigger('click'); await flushPromises()
    expect(api.puts()[1]!.init.body).toBe(api.puts()[0]!.init.body)
    expect(button(w, '同じAI要求を再試行')).toBeUndefined()
    retry.resolve(response(saved)); await flushPromises()
  }
  expect(w.get('.outline-form legend').text()).toContain('第2版')
  expect(w.get('.outline-form').text()).not.toContain('未保存')
  expect(w.get('[aria-label="AI作成補助"]').text()).not.toContain('AI操作中です')
  expect(button(w, '同じAI要求を再試行')).toBeUndefined()
  expect(button(w, 'この構成案を承認').attributes('disabled')).toBeUndefined()
})

it('unknown AI acceptance retries the exact request and running jobs can be cancelled', async () => {
  let attempt = 0
  const { w, api } = await render(storyServer((path, init) => {
    if (path.endsWith('/authoring-jobs') && init.method === 'POST') { if (!attempt++) throw new TypeError('lost response'); return response(job({ state: 'queued', proposal: null })) }
    if (path.endsWith('/cancel')) return response(job({ state: 'cancelled', proposal: null }))
  }))
  await button(w, '不足部分の補完を提案').trigger('click'); await flushPromises()
  await button(w, '同じAI要求を再試行').trigger('click'); await flushPromises()
  const calls = api.posts().filter(c => c.path.endsWith('/authoring-jobs'))
  expect(calls[0]!.init.body).toBe(calls[1]!.init.body)
  expect(w.text()).toContain('順番待ち')
  await button(w, 'AI処理をキャンセル').trigger('click'); await flushPromises()
  expect(w.text()).toContain('キャンセル済み')
})

it('AI state survives a visit to play and is removed on logout', async () => {
  const { w } = await render(storyServer((path, init) => path.endsWith('/authoring-jobs') && init.method === 'POST' ? response(job()) : undefined))
  await button(w, '不足部分の補完を提案').trigger('click'); await flushPromises()
  await button(w, '＋ 新しい冒険').trigger('click'); await flushPromises()
  expect(w.find('[aria-label="AIの変更提案"]').exists()).toBe(false)
  await button(w, 'マイ作品').trigger('click'); await flushPromises()
  expect(w.text()).toContain('空の庭')
  await button(w, 'ログアウト').trigger('click'); await flushPromises()
  expect(w.html()).not.toContain('空の庭'); expect(w.find('[aria-label="AI作成補助"]').exists()).toBe(false)
})

it('recovers a running job from server history and resumes polling without creating another job', async () => {
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
  const { w, api } = await render(storyServer(path => path.endsWith('/authoring-jobs')
    ? response({ jobs: [job({ state: 'running', proposal: null })] }) : path === '/authoring-jobs/job-a' ? response(job()) : undefined))
  expect(w.text()).toContain('不足部分の補完: 生成中')
  expect(button(w, '不足部分の補完を提案').attributes('disabled')).toBeDefined()
  await vi.advanceTimersByTimeAsync(2000); await flushPromises()
  expect(api.calls.some(call => call.path === '/authoring-jobs/job-a')).toBe(true)
  expect(w.text()).toContain('不足部分の補完: 完了'); expect(w.text()).toContain('空の庭')
  expect(api.posts()).toHaveLength(0)
  expect(localStorage.length).toBe(0); expect(sessionStorage.length).toBe(0)
})

it.each([1, 2])('recovers completed proposals after remount with draft revision %s and keeps stale proposals read-only', async revision => {
  const api = storyServer(path => path.endsWith('/authoring-jobs') ? response({ jobs: [job()] })
    : path.endsWith('/draft') ? response(story(revision)) : undefined)
  const initial = await render(api)
  expect(initial.w.text()).toContain('空の庭')
  initial.w.unmount()
  const { w } = await render(api)
  expect((w.get('#ai-job-history').element as HTMLSelectElement).value).toBe('job-a')
  expect(w.text()).toContain('空の庭')
  if (revision === 2) {
    expect(w.text()).toContain('生成元から原稿が変わっています')
    expect(w.get('input[value="change-a"]').attributes('disabled')).toBeDefined()
  } else {
    await w.get('input[value="change-a"]').setValue(true)
    expect(button(w, '選択した提案を採用').attributes('disabled')).toBeUndefined()
  }
  expect(api.posts()).toHaveLength(0)
})

it('selects durable history entries while protecting unsaved outline edits', async () => {
  const outline = job({ id: 'outline-a', kind: 'outline', proposal: null, outline_revision: 1,
    outline: { title: '庭', premise: '保存した構成案', scenes: [], characters: [], endings: [], undecided: [] } })
  const { w, api } = await render(storyServer(path => path.endsWith('/authoring-jobs') ? response({ jobs: [outline, job()] })
    : path === '/authoring-jobs/job-a' ? response(job()) : undefined))
  await w.get('[id="/ai-outline/premise"]').setValue('未保存の構成案')
  vi.mocked(window.confirm).mockReturnValueOnce(false)
  await w.get('#ai-job-history').setValue('job-a'); await flushPromises()
  expect((w.get('#ai-job-history').element as HTMLSelectElement).value).toBe('outline-a')
  expect((w.get('[id="/ai-outline/premise"]').element as HTMLTextAreaElement).value).toBe('未保存の構成案')
  await button(w, 'AI履歴を更新').trigger('click'); await flushPromises()
  expect((w.get('[id="/ai-outline/premise"]').element as HTMLTextAreaElement).value).toBe('未保存の構成案')
  await w.get('#ai-job-history').setValue('job-a'); await flushPromises()
  expect(w.text()).toContain('空の庭'); expect(w.find('[id="/ai-outline/premise"]').exists()).toBe(false)
  expect(api.posts()).toHaveLength(0)
})

it.each(['history', 'status'])('ignores delayed %s results from a previous story and filters mismatched jobs', async source => {
  const late = deferred<Response>(), other = { ...story(), story_id: 'story-b' }
  other.draft.metadata.title = '次の作品'
  const { w } = await render(storyServer(path => {
    if (path === '/stories/mine') return response({ stories: [summary(story()), summary(other)] })
    if (path === '/stories/story-a/authoring-jobs') return source === 'history' ? late.promise : response({ jobs: [job({ state: 'running', proposal: null })] })
    if (path === '/authoring-jobs/job-a') return late.promise
    if (path === '/stories/story-b/draft') return response(other)
    if (path === '/stories/story-b/authoring-jobs') return response({ jobs: [job(), job({ id: 'job-b', story_id: 'story-b', kind: 'check', proposal: null })] })
  }))
  if (source === 'status') { await button(w, 'AIの状態を再取得').trigger('click'); await flushPromises() }
  await w.get('[data-story="story-b"]').trigger('click'); await flushPromises()
  late.resolve(response(source === 'history' ? { jobs: [job()] } : job())); await flushPromises()
  expect((w.get('#ai-job-history').element as HTMLSelectElement).value).toBe('job-b')
  expect(w.find('#ai-job-history option[value="job-a"]').exists()).toBe(false)
  expect(w.text()).toContain('整合性チェック: 完了'); expect(w.text()).not.toContain('空の庭')
})

it('retries history reads and restores jobs without sending an AI mutation', async () => {
  let reads = 0
  const { w, api } = await render(storyServer(path => {
    if (path.endsWith('/authoring-jobs')) return ++reads === 1 ? response({}, 503) : response({ jobs: [job()] })
  }))
  expect(w.text()).toContain('AI処理の履歴を取得できませんでした')
  await button(w, 'AI履歴を更新').trigger('click'); await flushPromises()
  expect(w.text()).toContain('空の庭'); expect(api.posts()).toHaveLength(0)
})

it('creates a blank draft without JSON input and can request an outline from a natural-language premise', async () => {
  const blank = { ...story(), story_id: 'blank-a', draft: { authoring_schema_version: 1, scenario: {},
    metadata: { title: '', synopsis: '', tags: [], content_warnings: [] }, field_policies: {}, notes: '' } }
  const { w, api } = await render(storyServer((path, init) => {
    if (path === '/stories' && init.method === 'POST') return response(blank, 201)
    if (path.endsWith('/authoring-jobs') && init.method === 'POST') return response(job({ story_id: 'blank-a', kind: 'outline', state: 'queued', proposal: null }))
  }))
  await button(w, '空の原稿から作る').trigger('click'); await flushPromises()
  expect(JSON.parse(String(api.posts()[0]!.init.body))).toEqual({ request_id: expect.any(String), draft: {} })
  expect((w.get('[id="/metadata/title"]').element as HTMLInputElement).value).toBe('')
  expect(w.text()).toContain('未完成でも保存できます')
  expect(button(w, '条件から構成案を作る').attributes('disabled')).toBeUndefined()
  expect(api.puts()).toHaveLength(0)
  await w.get('#ai-instructions').setValue('砂漠を旅する配達人の物語を作りたい')
  await button(w, '条件から構成案を作る').trigger('click'); await flushPromises()
  const call = api.posts().find(call => call.path === '/stories/blank-a/authoring-jobs')!
  expect(JSON.parse(String(call.init.body))).toMatchObject({ kind: 'outline', base_revision: 1, instructions: '砂漠を旅する配達人の物語を作りたい' })
})
