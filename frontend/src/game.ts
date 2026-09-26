import { computed, reactive } from 'vue'
import type { AdventureId, AdventureSummary, CampaignState, Catalog, Content, HistoryPage, Pending, Session, StartBody, StoryRecord, Turn } from './contracts'

type EventStream = {
  onopen: ((event: Event) => void) | null; onerror: ((event: Event) => void) | null
  addEventListener(name: string, listener: (event: MessageEvent) => void): void
  close(): void
}
type Options = { fetch?: typeof fetch; storage?: Storage; events?: (url: string) => EventStream }
export const terminal = (turn: Turn) => ['completed', 'fallback'].includes(turn.narration_status)
  && ['committed', 'not_applied', 'failed'].includes(turn.resolution_status)
const storageMessage = '送信内容をブラウザーに保存できません。保存を許可してから送信してください。'
const conflictMessage = '冒険の状態が更新されました。行動内容を確認してから、改めて選んでください。'
class HttpError extends Error {
  constructor(public status: number) { super(status === 409 ? conflictMessage : status === 403
    ? 'この操作は許可されていません。ログイン状態を確認してください。'
    : status === 422 ? '入力内容を確認してください。'
    : '通信を確認できませんでした。時間をおいて再試行してください。') }
}
const same = (a: Pending | null, b: Pending | null) => a === null || b === null ? a === b
  : a.kind === b.kind && JSON.stringify(a.body) === JSON.stringify(b.body)
    && (a.kind !== 'turn' || b.kind !== 'turn' || a.campaignId === b.campaignId)
const isId = (v: unknown): v is AdventureId => !!v && typeof v === 'object'
  && 'campaign_id' in v && typeof v.campaign_id === 'string' && 'actor_id' in v && typeof v.actor_id === 'string'
function validPending(v: unknown): v is Pending {
  if (!v || typeof v !== 'object' || !('kind' in v) || !('body' in v)) return false
  const p = v as Pending, b = p.body
  if (!b || typeof b.request_id !== 'string') return false
  if (p.kind === 'start') return typeof p.body.scenario_ref === 'string' && Number.isInteger(p.body.scenario_version)
    && typeof p.body.preset_ref === 'string' && typeof p.body.player_name === 'string' && (p.adventure === null || isId(p.adventure))
    && (p.body.ability_points === undefined || ['strength', 'agility', 'insight', 'presence']
      .every(key => Number.isInteger(p.body.ability_points?.[key as keyof typeof p.body.ability_points])))
    && (p.body.specialty_skill === undefined || typeof p.body.specialty_skill === 'string')
  if (p.kind !== 'turn' || typeof p.campaignId !== 'string' || typeof p.displayText !== 'string'
    || !(p.turnId === null || typeof p.turnId === 'string') || !Number.isInteger(p.body.expected_state_version)
    || typeof p.body.actor_id !== 'string') return false
  const c = p.body.content
  return !!c && (c.kind === 'text' ? typeof c.text === 'string' : c.kind === 'choice'
    ? typeof c.choice_id === 'string' : c.kind === 'scenario_action' ? typeof c.action_ref === 'string'
      : c.kind === 'confirm_action' && typeof c.proposal_id === 'string')
}

export function createGame(options: Options = {}) {
  const fetcher = options.fetch ?? ((...args) => fetch(...args))
  const storage = () => options.storage ?? window.localStorage
  const state = reactive({
    auth: 'checking' as 'checking' | 'signed-out' | 'ready' | 'unavailable', session: null as Session | null,
    catalog: { scenarios: [], presets: [] } as Catalog, adventures: [] as AdventureSummary[],
    selected: null as AdventureId | null, campaign: null as CampaignState | null,
    records: [] as StoryRecord[], pending: null as Pending | null, draft: '', names: {} as Record<string, string>,
    error: '', homeError: '', historyError: '', trackingError: '', notice: '',
    loading: false, homeLoading: false, busy: false, tracking: false, historyBusy: false,
    historyCursor: null as string | null, historyLoaded: false, stateReady: false, storageBlocked: false,
  })
  let sessionGeneration = 0, viewGeneration = 0, refreshGeneration = 0, homeGeneration = 0
  let disposed = false, stopTracking = () => {}, activeOperation: string | null = null
  let observedTurn: Turn | null = null
  const requests = new Map<AbortController, boolean>()
  const key = (suffix: string) => `ai-rpg:vue:${state.session!.principal_id}:${suffix}`
  const canStart = computed(() => state.auth === 'ready' && !state.pending && !state.busy && !state.storageBlocked
    && !!state.catalog.scenarios.length && !!state.catalog.presets.length)
  const canAct = computed(() => state.auth === 'ready' && state.stateReady && !state.loading && !state.busy
    && !state.pending && !state.tracking && !state.storageBlocked && state.campaign?.adventure?.status === 'active'
    && (!state.campaign.latest_turn || terminal(state.campaign.latest_turn)))
  function context() {
    const s = sessionGeneration, v = viewGeneration
    return () => !disposed && s === sessionGeneration && v === viewGeneration && state.auth === 'ready'
  }
  function abort(viewOnly = false) {
    for (const [controller, view] of requests) if (!viewOnly || view) { controller.abort(); requests.delete(controller) }
  }
  function resetView() {
    ++viewGeneration; ++refreshGeneration; stopTracking(); abort(true); activeOperation = null
    observedTurn = null
    state.selected = null; state.campaign = null; state.records = []; state.draft = ''; state.names = {}
    state.loading = false; state.busy = false; state.stateReady = false
    state.error = state.storageBlocked ? storageMessage : ''; state.notice = ''; state.trackingError = ''; state.historyError = ''
    state.historyCursor = null; state.historyLoaded = false; state.historyBusy = false
  }
  function clearPrivate(auth: typeof state.auth) {
    ++sessionGeneration; resetView(); abort(); state.auth = auth; state.session = null
    state.pending = null; state.catalog = { scenarios: [], presets: [] }; state.adventures = []
    state.homeError = ''; state.homeLoading = false; state.storageBlocked = false
  }
  async function request<T>(path: string, init: RequestInit = {}, view = true): Promise<T> {
    const s = sessionGeneration, v = viewGeneration
    const controller = new AbortController(); requests.set(controller, view)
    const timeout = setTimeout(() => controller.abort(), 20000)
    const headers = new Headers(init.headers)
    if (init.method === 'POST') {
      headers.set('Content-Type', 'application/json')
      if (state.session?.csrf_token) headers.set('X-CSRF-Token', state.session.csrf_token)
    }
    try {
      const result = await fetcher(path, { ...init, headers, credentials: 'same-origin', signal: controller.signal })
      if (result.status === 401 && s === sessionGeneration && (!view || v === viewGeneration)) {
        const expired = state.auth === 'ready'
        clearPrivate('signed-out')
        if (expired) state.notice = 'ログインの有効期限が切れました。同じアカウントでログインすると、未確認の要求を再開できます。'
      }
      if (!result.ok) throw new HttpError(result.status)
      return result.status === 204 ? undefined as T : await result.json() as T
    } finally { clearTimeout(timeout); requests.delete(controller) }
  }
  function readPending(): Pending | null {
    const raw = storage().getItem(key('pending'))
    if (raw === null) return null
    const value: unknown = JSON.parse(raw)
    if (!validPending(value)) throw new Error(storageMessage)
    return value
  }
  function storageFailure() { state.storageBlocked = true; state.error = storageMessage }
  function ownsPending(expected: Pending) {
    try {
      const actual = readPending()
      if (!same(actual, expected)) { state.pending = actual; return false }
      return true
    } catch { storageFailure(); return false }
  }
  function persist(next: Pending | null, expected: Pending | null = null) {
    try {
      const actual = readPending()
      if (!same(actual, expected) || (expected && next && !same(next, expected))) { state.pending = actual; return false }
      if (next) storage().setItem(key('pending'), JSON.stringify(next)); else storage().removeItem(key('pending'))
      state.pending = next; return true
    } catch { storageFailure(); return false }
  }
  function merge(record: StoryRecord) {
    const previous = state.records.find(r => r.turn.turn_id === record.turn.turn_id)
    if (previous) {
      const rank = (t: Turn) => terminal(t) ? 3 : t.resolution_status === 'committed' ? 2 : 1
      if (rank(record.turn) >= rank(previous.turn)) previous.turn = record.turn
      if (record.player_input !== undefined) previous.player_input = record.player_input
      if (record.created_at !== undefined) previous.created_at = record.created_at
    } else state.records.push(record)
  }
  async function refreshHome() {
    if (state.auth !== 'ready') return
    const s = sessionGeneration, h = ++homeGeneration
    const active = () => !disposed && s === sessionGeneration && h === homeGeneration && state.auth === 'ready'
    state.homeLoading = true; state.homeError = ''
    const results = await Promise.allSettled([request<Catalog>('/adventures/catalog', {}, false), request<{ adventures: AdventureSummary[] }>('/adventures', {}, false)])
    if (!active()) return
    if (results[0].status === 'fulfilled') state.catalog = results[0].value
    if (results[1].status === 'fulfilled') state.adventures = results[1].value.adventures
    if (results.some(r => r.status === 'rejected')) state.homeError = '冒険の一覧を読み込めませんでした。もう一度読み込んでください。'
    state.homeLoading = false
  }
  async function boot() {
    disposed = false; clearPrivate('checking')
    const s = sessionGeneration
    try {
      const session = await request<Session>('/auth/session', {}, false)
      if (disposed || s !== sessionGeneration) return
      state.session = session; state.auth = 'ready'
      let selected: AdventureId | null = null
      try {
        state.pending = readPending()
        const value: unknown = JSON.parse(storage().getItem(key('selected')) ?? 'null')
        if (isId(value)) selected = value
      } catch { storageFailure() }
      await refreshHome()
      if (disposed || s !== sessionGeneration) return
      if (state.pending?.kind === 'turn') selected = { campaign_id: state.pending.campaignId, actor_id: state.pending.body.actor_id }
      else if (state.pending?.kind === 'start') selected = state.pending.adventure
      if (selected) await selectAdventure(selected)
    } catch (error) {
      if (!disposed && s === sessionGeneration) {
        state.auth = error instanceof HttpError && error.status === 401 ? 'signed-out' : 'unavailable'
        state.notice = state.auth === 'unavailable' ? '認証サービスを利用できません。時間をおいて再確認してください。' : ''
      }
    }
  }
  async function refreshState(): Promise<CampaignState | null> {
    if (!state.selected || state.auth !== 'ready') return null
    const active = context(), generation = ++refreshGeneration, selected = state.selected
    try {
      const result = await request<CampaignState>(`/campaigns/${encodeURIComponent(selected.campaign_id)}/state`)
      if (!active() || generation !== refreshGeneration || result.state_version < (state.campaign?.state_version ?? -1)) return null
      if (result.latest_turn) merge({ turn: result.latest_turn })
      // History and live records establish turn order by identity. A different
      // pending turn has no committed version; that does not make it older.
      const observedIndex = state.records.findIndex(r => r.turn.turn_id === observedTurn?.turn_id)
      const latestIndex = state.records.findIndex(r => r.turn.turn_id === result.latest_turn?.turn_id)
      if (observedTurn && (latestIndex < observedIndex || result.state_version < (observedTurn.committed_state_version ?? 0))) {
        result.latest_turn = state.records[observedIndex]?.turn ?? observedTurn
      } else if (latestIndex >= 0) result.latest_turn = state.records[latestIndex]!.turn
      observedTurn = result.latest_turn
      state.campaign = result
      if (result.player) state.names.hero = result.player.name
      for (const item of result.player?.inventory ?? []) if (item.item_ref) state.names[item.item_ref] = item.name
      if (result.adventure?.combat) state.names[result.adventure.combat.enemy_ref.replace(/^@/, '')] = result.adventure.combat.enemy_name
      state.stateReady = result.state_version >= (observedTurn?.committed_state_version ?? 0)
      if (!state.stateReady) state.error = '保存済みの結果に状態表示を合わせています。状態を再取得してください。'
      if (state.stateReady && result.latest_turn && terminal(result.latest_turn)) state.trackingError = ''
      return result
    } catch {
      if (active() && generation === refreshGeneration) {
        state.stateReady = false; state.error = '冒険の状態を取得できませんでした。状態を再取得してください。'
      }
      return null
    }
  }
  async function loadHistory() {
    if (!state.selected || state.historyBusy || state.auth !== 'ready') return
    const active = context(), id = state.selected.campaign_id
    state.historyBusy = true; state.historyError = ''
    try {
      const suffix = state.historyCursor ? `&before_turn_id=${encodeURIComponent(state.historyCursor)}` : ''
      const page = await request<HistoryPage>(`/campaigns/${encodeURIComponent(id)}/history?limit=50${suffix}`)
      if (!active()) return
      page.items.forEach(merge)
      const ids = page.items.map(r => r.turn.turn_id), byId = new Map(state.records.map(r => [r.turn.turn_id, r]))
      state.records = [...ids.map(id => byId.get(id)!), ...state.records.filter(r => !ids.includes(r.turn.turn_id))]
      state.historyCursor = page.next_before_turn_id; state.historyLoaded = true
    } catch {
      if (active()) state.historyError = '履歴を読み込めませんでした。再取得すると保存済みの記録を確認できます。'
    } finally { if (active()) state.historyBusy = false }
  }
  async function selectAdventure(selected: AdventureId) {
    if (state.auth !== 'ready') return false
    resetView(); state.selected = { campaign_id: selected.campaign_id, actor_id: selected.actor_id }; state.loading = true
    const active = context()
    try { storage().setItem(key('selected'), JSON.stringify(state.selected)) } catch { storageFailure() }
    const result = await refreshState()
    if (!active()) return false
    state.loading = false
    if (!result) return false
    const pending = state.pending
    if (pending?.kind === 'start' && pending.adventure?.campaign_id === selected.campaign_id) persist(null, pending)
    const history = loadHistory()
    if (pending?.kind === 'turn' && pending.campaignId === selected.campaign_id && pending.turnId) await retry(pending)
    else if (result.latest_turn && !terminal(result.latest_turn)) track(result.latest_turn)
    await history
    return active()
  }
  async function acceptTurn(turn: Turn, pending?: Pending) {
    const active = context()
    if (pending && !ownsPending(pending)) return
    merge({ turn, ...(pending?.kind === 'turn' && state.records.find(r => r.turn.turn_id === turn.turn_id)?.player_input === undefined
      ? { player_input: pending.displayText } : {}) })
    observedTurn = state.records.find(r => r.turn.turn_id === turn.turn_id)!.turn
    if (state.campaign) state.campaign.latest_turn = state.records.find(r => r.turn.turn_id === turn.turn_id)!.turn
    state.notice = terminal(turn) ? '行動を保存しました。' : turn.resolution_status === 'committed'
      ? '判定と状態は保存済みです。物語の描写を準備しています…' : '行動を受け付けました。判定しています…'
    if (terminal(turn) && pending) {
      if (!persist(null, pending)) return
      if (pending.kind === 'turn' && pending.body.content.kind === 'text' && state.draft === pending.body.content.text) state.draft = ''
    }
    if (terminal(turn) || turn.resolution_status === 'committed') {
      state.stateReady = false
      await refreshState()
      if (!active()) return
      const current = state.records.find(r => r.turn.turn_id === turn.turn_id)!.turn
      if (state.campaign && (!state.campaign.latest_turn || state.campaign.latest_turn.turn_id === turn.turn_id)) state.campaign.latest_turn = current
      const latest = state.campaign?.latest_turn
      // A replay or completion event may reveal another device's next turn.
      if (latest && latest.turn_id !== turn.turn_id && !terminal(latest)) track(latest)
      if (terminal(turn)) void refreshHome()
    }
  }
  function track(initial: Turn) {
    stopTracking()
    if (!state.selected || terminal(initial)) return
    const activeView = context(), id = state.selected.campaign_id, turnId = initial.turn_id
    let stopped = false, polling = false, polls = 0, events: EventStream | null = null
    let timer: ReturnType<typeof setTimeout> | undefined
    const active = () => !stopped && activeView()
    state.tracking = true; state.trackingError = ''
    const cancel = () => {
      stopped = true; clearTimeout(timer); events?.close(); events = null
      if (stopTracking === cancel) { state.tracking = false; stopTracking = () => {} }
    }
    stopTracking = cancel
    const fail = () => {
      if (!active()) return
      cancel(); state.trackingError = '結果の確認を中断しました。保存済みの結果を再確認してください。'
    }
    async function observe(turn: Turn) {
      if (!active() || turn.turn_id !== turnId) return
      const pending = state.pending?.kind === 'turn' && state.pending.turnId === turnId ? state.pending : undefined
      if (pending && !ownsPending(pending)) { cancel(); return }
      if (terminal(turn)) cancel()
      await acceptTurn(turn, pending)
    }
    async function poll() {
      if (!active()) return
      if (++polls > 30) { fail(); return }
      try {
        const turn = await request<Turn>(`/campaigns/${encodeURIComponent(id)}/turns/${encodeURIComponent(turnId)}`)
        if (!active()) return
        await observe(turn)
        if (active()) timer = setTimeout(poll, 2000)
      } catch { fail() }
    }
    function fallback() {
      if (!active() || polling) return
      polling = true; clearTimeout(timer); events?.close(); events = null
      state.notice = '接続を確認しながら、保存済みの結果を定期的に取得しています…'
      void poll()
    }
    try {
      events = options.events ? options.events(`/campaigns/${encodeURIComponent(id)}/events`)
        : new EventSource(`/campaigns/${encodeURIComponent(id)}/events`, { withCredentials: true })
      events.addEventListener('turn.updated', event => {
        if (!active() || polling) return
        try {
          const t = JSON.parse(event.data).payload?.turn as Turn | undefined
          if (t && t.turn_id === turnId && typeof t.resolution_status === 'string' && typeof t.narration_status === 'string') void observe(t)
        } catch { /* Malformed public events leave polling available. */ }
      })
      events.onopen = () => {
        if (!active() || polling) return
        clearTimeout(timer)
        // The stream can open after the final event: a silence watchdog closes that gap.
        timer = setTimeout(fallback, 15000)
      }
      events.onerror = fallback
      timer = setTimeout(fallback, 1500)
    } catch { fallback() }
  }
  async function retry(expected: Pending | null = state.pending) {
    if (!expected || state.auth !== 'ready' || state.busy || !ownsPending(expected)) return
    if (expected.kind === 'turn' && state.selected?.campaign_id !== expected.campaignId) {
      await selectAdventure({ campaign_id: expected.campaignId, actor_id: expected.body.actor_id }); return
    }
    let pending = readPending()!
    const active = context(), s = sessionGeneration, operation = pending.body.request_id
    const owns = () => !disposed && s === sessionGeneration && state.auth === 'ready' && ownsPending(pending)
    state.busy = true; state.error = ''; activeOperation = operation
    try {
      if (pending.kind === 'start') {
        if (!pending.adventure) {
          const accepted = await request<AdventureId>('/adventures', { method: 'POST', body: JSON.stringify(pending.body) }, false)
          if (!owns()) return
          if (!isId(accepted)) throw new Error('Invalid acceptance')
          const updated = { ...pending, adventure: { campaign_id: accepted.campaign_id, actor_id: accepted.actor_id } }
          if (!persist(updated, pending)) return
          pending = updated
        }
        if (!active()) return
        await selectAdventure(pending.adventure!); if (s === sessionGeneration) void refreshHome()
      } else {
        const base = `/campaigns/${encodeURIComponent(pending.campaignId)}/turns`
        const accepted = await request<Turn>(pending.turnId ? `${base}/${encodeURIComponent(pending.turnId)}` : base,
          pending.turnId ? {} : { method: 'POST', body: JSON.stringify(pending.body) })
        if (!active() || !owns()) return
        if (!accepted || typeof accepted.turn_id !== 'string' || !accepted.resolution_status) throw new Error('Invalid acceptance')
        const updated = { ...pending, turnId: accepted.turn_id }
        if (!persist(updated, pending)) return
        pending = updated
        await acceptTurn(accepted, pending)
        if (active() && !terminal(accepted) && state.campaign?.latest_turn?.turn_id === accepted.turn_id) track(accepted)
      }
    } catch (error) {
      if (!owns() || !active()) return
      // A 401 hides the view in request(), preserving this principal's exact operation.
      const rejected = error instanceof HttpError && error.status >= 400 && error.status < 500
        && error.status !== 401 && error.status !== 408 && error.status !== 429
        && (pending.kind === 'start' ? !pending.adventure : !pending.turnId)
      if (rejected) {
        persist(null, pending)
        if (error.status === 409 && pending.kind === 'turn') {
          const current = await refreshState()
          if (active() && current?.latest_turn && !terminal(current.latest_turn)) track(current.latest_turn)
        }
        if (active()) state.error = error.message
      } else state.error = '要求の結果を確認できませんでした。同じ要求を再送して確認してください。'
    } finally {
      if (active() && activeOperation === operation) { state.busy = false; activeOperation = null }
    }
  }
  async function startAdventure(input: Omit<StartBody, 'request_id' | 'scenario_version'>) {
    if (!canStart.value) return
    const scenario = state.catalog.scenarios.find(s => s.scenario_ref === input.scenario_ref)
    if (!scenario || !state.catalog.presets.some(p => p.preset_ref === input.preset_ref) || !input.player_name.trim()
      || [...input.player_name].length > 40 || /[\p{C}\p{Zl}\p{Zp}]/u.test(input.player_name)) {
      state.error = 'シナリオ、冒険者のタイプと1〜40文字の名前を確認してください。'; return
    }
    const preset = state.catalog.presets.find(p => p.preset_ref === input.preset_ref)!
    if (scenario.character_creation) {
      const points = input.ability_points, base = preset.base_abilities
      const abilities = scenario.character_creation.abilities
      if (!points || !base || !input.specialty_skill || !scenario.character_creation.specialties.includes(input.specialty_skill)
        || abilities.reduce((sum, key) => sum + points[key], 0) !== scenario.character_creation.points
        || abilities.some(key => !Number.isInteger(points[key]) || points[key] < 0 || points[key] > 2 || base[key] + points[key] > 3)) {
        state.error = '能力ポイントをすべて配分し、得意技能を選んでください。'; return
      }
    }
    const pending: Pending = { kind: 'start', body: { request_id: crypto.randomUUID(), scenario_ref: input.scenario_ref,
      scenario_version: scenario.scenario_version, preset_ref: input.preset_ref, player_name: input.player_name,
      ...(scenario.character_creation ? { ability_points: input.ability_points, specialty_skill: input.specialty_skill } : {}) }, adventure: null }
    if (persist(pending)) await retry(pending)
  }
  async function submit(content: Content, displayText: string) {
    if (!canAct.value || !state.selected || !state.campaign || !displayText.trim()) return
    const active = context(), selected = state.selected, knownVersion = state.campaign.state_version
    state.busy = true; state.error = ''
    const current = await refreshState()
    if (!active()) return
    state.busy = false
    if (!current || !state.stateReady) return
    if (current.latest_turn && !terminal(current.latest_turn)) { track(current.latest_turn); return }
    if (current.adventure?.status !== 'active') return
    if (current.state_version !== knownVersion) { state.error = conflictMessage; return }
    if (content.kind === 'scenario_action' && !current.adventure.available_actions.some(a => a.action_ref === content.action_ref)
      || content.kind === 'choice' && !current.latest_turn?.choices.some(c => c.id === content.choice_id)
      || content.kind === 'confirm_action' && current.latest_turn?.risk_preview?.proposal_id !== content.proposal_id) {
      state.error = 'その行動は現在利用できません。最新の候補を選んでください。'; return
    }
    const pending: Pending = { kind: 'turn', campaignId: selected.campaign_id, displayText, turnId: null,
      body: { request_id: crypto.randomUUID(), expected_state_version: current.state_version, actor_id: selected.actor_id, content } }
    if (persist(pending)) await retry(pending)
  }
  async function recover() {
    const active = context(), current = await refreshState()
    if (!active() || !current) return
    if (state.stateReady) state.error = ''
    if (state.pending?.kind === 'turn' && state.pending.turnId) await retry(state.pending)
    else if (current.latest_turn && !terminal(current.latest_turn)) track(current.latest_turn)
  }
  function goHome() { resetView() }
  function bindAction(content: Content, label: string) {
    const active = context(), version = state.campaign?.state_version
    return () => {
      if (active() && version === state.campaign?.state_version) return submit(content, label)
    }
  }
  async function logout() {
    if (state.session && state.session.mode !== 'session') return
    let csrf = state.session?.csrf_token
    clearPrivate('signed-out')
    const s = sessionGeneration
    try {
      if (!csrf) {
        const session = await request<Session>('/auth/session', {}, false)
        if (disposed || s !== sessionGeneration) return
        if (session.mode !== 'session') return
        csrf = session.csrf_token
      }
      await request('/auth/logout', { method: 'POST', headers: csrf ? { 'X-CSRF-Token': csrf } : {} }, false)
    } catch (error) {
      if (error instanceof HttpError && error.status === 401) return
      if (s === sessionGeneration) { state.auth = 'unavailable'; state.notice = 'ログアウトを確認できませんでした。もう一度ログアウトしてください。' }
    }
  }
  function dispose() { disposed = true; clearPrivate('signed-out') }
  return { state, canStart, canAct, boot, refreshHome, refreshState, loadHistory, selectAdventure,
    startAdventure, submit, bindAction, retry, recover, goHome, logout, dispose }
}
export type Game = ReturnType<typeof createGame>
