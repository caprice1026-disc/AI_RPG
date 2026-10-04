import { computed, reactive, watch } from 'vue'
import { HttpError, type Game } from './game'
import { clone, node, type AuthoringDraft, type SaveDraft, type StoryDraft, type StoryPlaytest,
  type StoryPublicDetail, type StoryRevision, type StorySummary, type StoryTemplate, type ValidationReport,
  type Visibility, type Lifecycle } from './story-contracts'

type Operation = { path: string; body: string; method: string; kind: 'draft' | 'playtest' | 'publish' | 'settings' }
export function createStories(game: Game, onPlaytest: () => void = () => {}) {
  const state = reactive({
    stories: [] as StorySummary[], templates: [] as StoryTemplate[], current: null as StoryDraft | null,
    revisions: [] as StoryRevision[], remote: null as StoryDraft | null, report: null as ValidationReport | null,
    published: null as StoryPublicDetail | null, playtest: null as StoryPlaytest | null,
    saveStatus: 'saved' as 'saved' | 'waiting' | 'saving' | 'failed' | 'conflict',
    busy: false, loading: false, error: '', saveError: '', baseline: '', retryExact: false,
    acknowledged: false, warningCodes: [] as string[], visibility: 'unlisted' as Visibility,
    pendingOperation: null as Operation | null, externalDirty: false, externalPending: false,
  })
  let generation = 0, disposed = false, loadingGeneration = 0
  let timer: ReturnType<typeof setTimeout> | undefined
  let pendingSave: { body: SaveDraft; targets: { target: Record<string, unknown>; path: string[] }[] } | null = null
  let saving: Promise<boolean> | null = null
  const dirty = computed(() => !!state.current && JSON.stringify(state.current.draft) !== state.baseline)
  const unsaved = computed(() => dirty.value || !!pendingSave || !!state.pendingOperation || state.busy || state.externalDirty || state.externalPending)
  const reportCurrent = computed(() => !!state.report && !dirty.value && state.report.draft_revision === state.current?.revision)
  const canPublish = computed(() => reportCurrent.value && !state.report!.errors.length && state.acknowledged
    && state.report!.warnings.every(w => state.warningCodes.includes(w.code)) && !state.busy && !state.pendingOperation && !state.externalPending)
  const active = () => {
    const g = generation, principal = game.state.session?.principal_id
    return () => !disposed && g === generation && game.state.auth === 'ready' && principal === game.state.session?.principal_id
  }
  const path = (suffix = '') => `/stories/${encodeURIComponent(state.current!.story_id)}${suffix}`
  const message = (e: unknown) => e instanceof HttpError && e.status === 409
    ? '保存された版や公開状態が変わりました。最新の状態を確認してください。'
    : e instanceof HttpError && e.status === 422 ? '実行できませんでした。検証結果と入力内容、同じ内容での試遊完了を確認してください。'
      : '通信を確認できませんでした。入力を保持しています。再試行してください。'
  function invalidateReport() { state.report = null; state.acknowledged = false; state.warningCodes = [] }
  function reset() {
    ++generation; ++loadingGeneration; clearTimeout(timer); pendingSave = null; saving = null
    state.current = state.remote = null; state.stories = []; state.templates = []; state.revisions = []
    state.published = null; state.playtest = null; state.pendingOperation = null
    state.baseline = ''; state.error = state.saveError = ''; state.busy = state.loading = false
    state.saveStatus = 'saved'; state.retryExact = false; invalidateReport()
    state.externalDirty = state.externalPending = false
  }
  const stopSession = watch(() => [game.state.auth, game.state.session?.principal_id], reset, { flush: 'sync' })
  function install(value: StoryDraft) {
    clearTimeout(timer); pendingSave = null; state.baseline = JSON.stringify(value.draft)
    state.current = clone(value); state.remote = null; state.saveStatus = 'saved'; state.saveError = ''; state.retryExact = false
    state.revisions = []; state.published = null; state.playtest = null; invalidateReport()
  }
  async function load() {
    const valid = active(), g = ++loadingGeneration
    state.loading = true; state.error = ''
    const result = await Promise.allSettled([
      game.request<{ stories: StorySummary[] }>('/stories/mine', {}, false),
      game.request<{ templates: StoryTemplate[] }>('/stories/templates', {}, false),
    ])
    if (!valid() || g !== loadingGeneration) return
    if (result[0].status === 'fulfilled') state.stories = result[0].value.stories
    if (result[1].status === 'fulfilled') state.templates = result[1].value.templates
    if (result.some(r => r.status === 'rejected')) state.error = 'マイ作品を読み込めませんでした。もう一度読み込んでください。'
    state.loading = false
  }
  async function open(id: string) {
    if (state.busy || state.pendingOperation || state.externalPending || !confirmLeave()) return
    const valid = active(); clearTimeout(timer); state.busy = true; state.error = ''
    try { const value = await game.request<StoryDraft>(`/stories/${encodeURIComponent(id)}/draft`, {}, false); if (valid()) install(value) }
    catch (e) { if (valid()) state.error = message(e) }
    finally { if (valid()) state.busy = false }
  }
  function schedule() {
    clearTimeout(timer)
    if (!dirty.value && !pendingSave && !saving) { state.saveStatus = 'saved'; return }
    if (!dirty.value || state.busy || saving || state.pendingOperation || ['failed', 'conflict'].includes(state.saveStatus)) return
    state.saveStatus = 'waiting'; timer = setTimeout(() => { void save() }, 2000)
  }
  const stopDraft = watch(() => state.current?.draft, () => { invalidateReport(); schedule() }, { deep: true, flush: 'sync' })
  function captureTargets(value: unknown, targetPath: string[] = [], targets: NonNullable<typeof pendingSave>['targets'] = []) {
    if (value && typeof value === 'object') {
      if (!Array.isArray(value)) targets.push({ target: value as Record<string, unknown>, path: targetPath })
      Object.entries(value).forEach(([key, child]) => captureTargets(child, [...targetPath, key], targets))
    }
    return targets
  }
  async function sendSave(): Promise<boolean> {
    if (!state.current || state.saveStatus === 'conflict') return false
    if (!dirty.value && !pendingSave) return true
    const valid = active(), current = state.current
    pendingSave ??= { body: { request_id: crypto.randomUUID(), expected_revision: current.revision, draft: clone(current.draft) },
      targets: captureTargets(current.draft) }
    const pending = pendingSave
    state.saveStatus = 'saving'; state.saveError = ''
    try {
      const result = await game.request<StoryDraft>(path('/draft'), { method: 'PUT', body: JSON.stringify(pending.body) }, false)
      if (!valid() || current !== state.current) return false
      const unchanged = JSON.stringify(current.draft) === JSON.stringify(pending.body.draft)
      // Merge only server-assigned identities into objects that may have been edited/moved while saving.
      for (const { target, path: parts } of pending.targets) {
        let saved: unknown = result.draft, submitted: unknown = pending.body.draft
        for (const part of parts) {
          saved = node(saved)[part] ?? (Array.isArray(saved) ? saved[Number(part)] : undefined)
          submitted = node(submitted)[part] ?? (Array.isArray(submitted) ? submitted[Number(part)] : undefined)
        }
        for (const [key, value] of Object.entries(node(saved))) {
          if (['ref', 'scene_ref', 'action_ref', 'flag_ref', 'ending_ref', 'fact_ref'].includes(key)
            && !node(submitted)[key] && !target[key] && typeof value === 'string') target[key] = value
        }
      }
      state.baseline = JSON.stringify(result.draft); current.revision = result.revision; current.updated_at = result.updated_at
      if (unchanged) current.draft = clone(result.draft)
      pendingSave = null; state.retryExact = false; state.saveStatus = dirty.value ? 'waiting' : 'saved'
      const summary = state.stories.find(s => s.story_id === current.story_id)
      if (summary) { summary.revision = result.revision; summary.metadata = clone(result.draft.metadata); summary.updated_at = result.updated_at }
      return true
    } catch (e) {
      if (valid() && current === state.current) {
        if (e instanceof HttpError && e.status >= 400 && e.status < 500 && ![408, 409, 429].includes(e.status)) pendingSave = null
        state.retryExact = !!pendingSave
        state.saveStatus = e instanceof HttpError && e.status === 409 ? 'conflict' : 'failed'
        state.saveError = state.saveStatus === 'conflict'
          ? '別の画面で更新されています。この画面の入力は保持しています。保存済みの原稿と比較してください。'
          : state.retryExact ? '保存を確認できませんでした。入力は保持しています。同じ保存要求を再試行してください。'
            : '保存が拒否されました。入力を修正して保存し直してください。入力は保持しています。'
      }
      return false
    }
  }
  function save(): Promise<boolean> {
    clearTimeout(timer)
    if (saving) return saving
    const task = sendSave(); saving = task
    void task.finally(() => { if (saving === task) { saving = null; schedule() } })
    return task
  }
  async function flushSave() {
    if (!state.current || state.pendingOperation || state.externalPending) return false
    do { if (!await save()) return false } while (dirty.value)
    return true
  }
  async function compareRemote() {
    if (!state.current || state.busy) return
    const valid = active(); state.busy = true
    try { const value = await game.request<StoryDraft>(path('/draft'), {}, false); if (valid()) state.remote = value }
    catch (e) { if (valid()) state.error = message(e) }
    finally { if (valid()) state.busy = false }
  }
  function useRemote() {
    if (state.remote && window.confirm('この画面の未保存の入力を破棄し、保存済みの原稿に置き換えますか？')) install(state.remote)
  }
  async function keepLocal() {
    if (!state.current || !state.remote || state.busy) return
    if (!window.confirm('比較した保存済みの原稿を、この画面の入力で更新しますか？履歴から復元できます。')) return
    state.current.revision = state.remote.revision; state.baseline = JSON.stringify(state.remote.draft)
    pendingSave = null; state.remote = null; state.saveStatus = 'waiting'; await save()
  }
  async function history() {
    if (!state.current || state.busy) return
    const valid = active(), id = state.current.story_id; state.busy = true
    try { const result = await game.request<{ revisions: StoryRevision[] }>(path('/revisions'), {}, false)
      if (valid() && state.current?.story_id === id) state.revisions = result.revisions }
    catch (e) { if (valid()) state.error = message(e) }
    finally { if (valid()) { state.busy = false; schedule() } }
  }
  async function resumePlaytest() {
    if (!state.playtest || game.state.pending || game.state.busy) { state.error = '冒険の未確認の要求を先に確認してください。'; return }
    const valid = active(), result = state.playtest
    await game.refreshHome()
    if (!valid()) return
    if (await game.selectAdventure(result) && valid()) onPlaytest()
    else if (valid()) state.error = '試遊は作成済みです。試遊を開き直してください。'
  }
  async function runOperation() {
    const operation = state.pendingOperation
    if (!operation || state.busy) return
    const valid = active(); clearTimeout(timer); state.busy = true; state.error = ''
    try {
      const result = await game.request<StoryDraft | StoryPublicDetail | StoryPlaytest | StorySummary>(operation.path,
        { method: operation.method, body: operation.body }, false)
      if (!valid()) return
      if (operation.kind === 'draft') install(result as StoryDraft)
      if (operation.kind === 'publish') state.published = result as StoryPublicDetail
      if (operation.kind === 'settings') {
        const summary = result as StorySummary, i = state.stories.findIndex(s => s.story_id === summary.story_id)
        if (i >= 0) state.stories[i] = summary
        if (state.published) { state.published.visibility = summary.visibility; state.published.lifecycle = summary.lifecycle }
      }
      if (operation.kind === 'playtest') {
        const playtest = result as StoryPlaytest
        if (!playtest.campaign_id || !playtest.actor_id) throw new Error('Playtest campaign is missing')
        state.playtest = playtest; await resumePlaytest()
      }
      state.pendingOperation = null
      if (valid()) await load()
    } catch (e) {
      if (valid()) {
        state.error = message(e)
        if (e instanceof HttpError && e.status >= 400 && e.status < 500 && ![408, 429].includes(e.status)) state.pendingOperation = null
      }
    } finally { if (valid()) { state.busy = false; schedule() } }
  }
  async function operation(kind: Operation['kind'], suffix: string, body: Record<string, unknown>, method = 'POST', absolute = false) {
    if (state.busy || state.pendingOperation || state.externalPending) return
    state.pendingOperation = { kind, path: absolute ? suffix : path(suffix), body: JSON.stringify({ request_id: crypto.randomUUID(), ...body }), method }
    await runOperation()
  }
  async function create(template_id?: string) {
    if (!confirmLeave() || state.busy || state.pendingOperation || state.externalPending) return
    await operation('draft', '/stories', template_id ? { template_id } : { draft: {} }, 'POST', true)
  }
  async function duplicate() { if (await flushSave()) await operation('draft', '/duplicate', {}) }
  async function restore(revision: number) {
    if (!window.confirm(`履歴 ${revision} を新しい下書きとして復元しますか？`) || !await flushSave()) return
    await operation('draft', '/restore', { expected_revision: state.current!.revision, revision })
  }
  async function validate() {
    if (state.busy || !await flushSave()) return
    const valid = active(); state.busy = true; state.error = ''; invalidateReport()
    try { const report = await game.request<ValidationReport>(path('/validate'),
      { method: 'POST', body: JSON.stringify({ expected_revision: state.current!.revision }) }, false)
      if (valid()) state.report = report }
    catch (e) { if (valid()) state.error = message(e) }
    finally { if (valid()) { state.busy = false; schedule() } }
  }
  async function playtest() {
    if (game.state.pending || game.state.busy) { state.error = '冒険の未確認の要求を先に確認してください。'; return }
    if (await flushSave()) await operation('playtest', '/playtests', { expected_revision: state.current!.revision, preset_ref: 'scout', player_name: '作者',
      ...(state.current!.draft.scenario.ruleset_ref === 'mvp_v2' ? { ability_points: { strength: 0, agility: 1, insight: 1, presence: 0 }, specialty_skill: 'perception' } : {}) })
  }
  async function publish() {
    if (!canPublish.value || !await flushSave() || !canPublish.value) return
    await operation('publish', '/publish', { expected_revision: state.current!.revision, validation_report_id: state.report!.id,
      acknowledged_warning_codes: [...new Set(state.warningCodes)], author_playtest_acknowledged: state.acknowledged, visibility: state.visibility })
  }
  async function settings(visibility: Visibility, lifecycle: Exclude<Lifecycle, 'blocked'>) {
    if (await flushSave()) await operation('settings', '/settings', { visibility, lifecycle }, 'PUT')
  }
  function confirmLeave() { return !unsaved.value || window.confirm('未保存の入力、または結果未確認の操作があります。この画面を離れますか？') }
  function beforeUnload(event: BeforeUnloadEvent) { if (unsaved.value) { event.preventDefault(); event.returnValue = '' } }
  window.addEventListener('beforeunload', beforeUnload)
  function dispose() { disposed = true; stopDraft(); stopSession(); reset(); window.removeEventListener('beforeunload', beforeUnload) }
  return { state, dirty, unsaved, reportCurrent, canPublish, load, open, save, flushSave, compareRemote, useRemote, keepLocal,
    history, create, duplicate, restore, validate, playtest, resumePlaytest, publish, settings, retryOperation: runOperation, confirmLeave, acceptDraft: install, dispose }
}
export type Stories = ReturnType<typeof createStories>
