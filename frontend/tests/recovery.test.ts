import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { createGame, type Game } from '../src/game'
import { campaign, deferred, Events, flush, response, saved, server, session, turn, waiting } from './fixtures'

const games: Game[] = []
function make(api = server()) {
  const g = createGame({ fetch: api.fetch, storage: localStorage, events: url => new Events(url) }); games.push(g); return g
}
async function playing(api = server()) { const g = make(api); await g.boot(); await g.selectAdventure(saved); return g }
const text = { kind: 'text' as const, text: 'original input' }
const start = { scenario_ref: 'chapel', preset_ref: 'scout', player_name: '葵' }
beforeEach(() => { localStorage.clear(); Events.instances = [] })
afterEach(() => { games.splice(0).forEach(g => g.dispose()); vi.useRealTimers() })

it('accepted turn reload uses GET only and retains exact input', async () => {
  const api = server((path, init) => init.method === 'POST' ? response(waiting('known')) : path.includes('/turns/') ? response(turn('known', 0)) : undefined)
  const first = await playing(api); await first.submit(text, text.text); const original = JSON.stringify(first.state.pending?.body)
  expect(first.state.pending).toMatchObject({ turnId: 'known' }); first.dispose()
  const reloaded = make(api); await reloaded.boot()
  expect(api.posts()).toHaveLength(1); expect(reloaded.state.pending).toBeNull()
  expect(reloaded.state.records.map(r => r.player_input)).toEqual(['original input'])
  expect(String(api.posts()[0]!.init.body)).toBe(original)
})
it('tracking failure retains accepted id and retry never posts again', async () => {
  let gets = 0
  const api = server((path, init) => init.method === 'POST' ? response(waiting('known')) : path.includes('/turns/')
    ? ++gets === 1 ? response({}, 503) : response(turn('known', 0)) : undefined)
  const g = await playing(api); await g.submit(text, text.text)
  const body = JSON.stringify(g.state.pending?.body)
  Events.instances[0]!.onerror?.(); await flush()
  expect(g.state.trackingError).not.toBe(''); expect(JSON.stringify(g.state.pending?.body)).toBe(body)
  await g.retry(g.state.pending)
  expect(api.posts()).toHaveLength(1); expect(gets).toBe(2); expect(g.state.pending).toBeNull()
  expect(g.state.trackingError).toBe('')
})
it('completed turn with failed state refresh recovers by GET only', async () => {
  let failState = false
  const api = server((path, init) => {
    if (init.method === 'POST') { failState = true; return response(turn('done', 0)) }
    if (path.endsWith('/state') && failState) return response({}, 503)
  })
  const g = await playing(api); await g.submit(text, text.text)
  expect(g.state.pending).toBeNull(); expect(g.canAct.value).toBe(false)
  failState = false; await g.recover()
  expect(api.posts()).toHaveLength(1); expect(g.canAct.value).toBe(true)
})
it.each(['start', 'turn'])('422 %s releases only rejected request and corrected input gets a new id', async kind => {
  const api = server((_path, init) => init.method === 'POST' ? response({}, 422) : undefined)
  const g = await playing(api)
  for (let i = 0; i < 2; i++) {
    if (kind === 'start') await g.startAdventure(start); else await g.submit(text, text.text)
    expect(g.state.pending).toBeNull()
  }
  expect(api.posts()).toHaveLength(2)
  expect(JSON.parse(String(api.posts()[0]!.init.body)).request_id).not.toBe(JSON.parse(String(api.posts()[1]!.init.body)).request_id)
})
it('late start acceptance is durable without replacing a newer selected campaign', async () => {
  const delayed = deferred<Response>()
  const api = server((_path, init) => init.method === 'POST' ? delayed.promise : undefined)
  const g = await playing(api); const starting = g.startAdventure(start); await flush()
  await g.selectAdventure({ campaign_id: 'campaign-b', actor_id: 'actor-b' })
  delayed.resolve(response(saved)); await starting
  expect(g.state.selected?.campaign_id).toBe('campaign-b')
  expect(g.state.pending).toMatchObject({ kind: 'start', adventure: { campaign_id: 'campaign-a' } })
  await g.retry(g.state.pending); expect(g.state.selected?.campaign_id).toBe('campaign-a'); expect(api.posts()).toHaveLength(1)
})
it.each([503, 401])('late old-view HTTP %s cannot clear or show errors on newer selection', async status => {
  const delayed = deferred<Response>()
  const api = server(path => path === '/campaigns/campaign-a/state' ? delayed.promise : undefined)
  const g = make(api); await g.boot(); const old = g.selectAdventure(saved); await flush()
  await g.selectAdventure({ campaign_id: 'campaign-b', actor_id: 'actor-b' })
  delayed.resolve(response({}, status)); await old
  expect(g.state.auth).toBe('ready'); expect(g.state.selected?.campaign_id).toBe('campaign-b'); expect(g.state.error).toBe('')
  expect(api.calls.find(c => c.path === '/campaigns/campaign-a/state')!.init.signal!.aborted).toBe(true)
})
it('late history is inert after switching and cannot change pagination', async () => {
  const delayed = deferred<Response>()
  const api = server(path => path.startsWith('/campaigns/campaign-a/history?') ? delayed.promise : undefined)
  const g = make(api); await g.boot(); const old = g.selectAdventure(saved); await flush()
  await g.selectAdventure({ campaign_id: 'campaign-b', actor_id: 'actor-b' })
  delayed.resolve(response({ items: [{ created_at: '2026-09-22', player_input: 'old', turn: turn() }], next_before_turn_id: 'old' })); await old
  expect(g.state.records).toEqual([]); expect(g.state.historyCursor).toBeNull()
})
it('old session response cannot restore identity or private data after a new login', async () => {
  const delayed = deferred<Response>(); let calls = 0
  const api = server(path => path === '/auth/session' ? ++calls === 1 ? delayed.promise : response({ ...session, principal_id: 'other' }) : undefined)
  const g = make(api); const first = g.boot(); await flush(); await g.boot()
  delayed.resolve(response(session)); await first
  expect(g.state.session?.principal_id).toBe('other')
})
it('late polling and SSE cannot overwrite a new campaign; all view requests abort', async () => {
  const delayed = deferred<Response>()
  const api = server(path => path === '/campaigns/campaign-a/state' ? response(campaign({ latest_turn: waiting('old') }))
    : path.includes('/turns/old') ? delayed.promise : undefined)
  const g = await playing(api); const stream = Events.instances[0]!
  stream.onerror?.(); await flush(); await g.selectAdventure({ campaign_id: 'campaign-b', actor_id: 'actor-b' })
  delayed.resolve(response(turn('old', 99))); stream.emit(turn('old', 99)); await flush()
  expect(g.state.records).toEqual([]); expect(g.state.campaign?.state_version).toBe(0); expect(stream.closed).toBe(true)
  expect(api.calls.find(c => c.path.endsWith('/turns/old'))!.init.signal!.aborted).toBe(true)
})
it('state version comes from current campaign, never its older latest turn', async () => {
  const api = server((path, init) => path.endsWith('/state') ? response(campaign({ state_version: 7, latest_turn: turn('old', 3) })) : init.method === 'POST' ? response(turn('new', 7)) : undefined)
  const g = await playing(api); await g.submit(text, text.text)
  expect(JSON.parse(String(api.posts()[0]!.init.body)).expected_state_version).toBe(7)
})
it('delayed unfinished history cannot erase completed output and its choices', async () => {
  const delayed = deferred<Response>(); let calls = 0, latest = turn('old', 0)
  const done = { ...turn('live', 1), choices: [{ id: 'current-choice', label: '続く' }] }
  const api = server((path, init) => {
    if (path.endsWith('/state')) return response(campaign({ state_version: latest.committed_state_version!, latest_turn: latest }))
    if (path.includes('/history?')) return ++calls === 1 ? response({}, 503) : delayed.promise
    if (init.method === 'POST') return response(waiting('live'))
  })
  const g = await playing(api); await g.submit(text, text.text); const loading = g.loadHistory(); await flush()
  latest = done; Events.instances[0]!.emit(done); await flush()
  delayed.resolve(response({ items: [
    { created_at: '2026-09-22T00:00:00Z', player_input: 'old input', turn: turn('old', 0) },
    { created_at: '2026-09-22T00:01:00Z', player_input: text.text, turn: waiting('live') },
  ], next_before_turn_id: null })); await loading
  expect(g.state.records.map(r => [r.turn.turn_id, r.player_input, r.turn.narration])).toEqual([
    ['old', 'old input', '扉が開いた。'], ['live', 'original input', '扉が開いた。'],
  ])
  expect(g.state.records[1]!.turn.choices[0]!.id).toBe('current-choice'); expect(g.state.pending).toBeNull()
})
it('history reconciliation preserves unfinished server input and unaccepted local input', async () => {
  let calls = 0
  const api = server((path, init) => {
    if (path.endsWith('/state')) return response(campaign({ latest_turn: turn('old', 0) }))
    if (path.includes('/history?')) return ++calls === 1 ? response({}, 503) : response({ items: [
      { created_at: '2026-09-22T00:00:00Z', player_input: 'old', turn: turn('old', 0) },
      { created_at: '2026-09-22T00:01:00Z', player_input: 'unfinished', turn: waiting('unfinished') },
    ], next_before_turn_id: null })
    if (init.method === 'POST') return response({}, 503)
  })
  const g = await playing(api); await g.submit(text, text.text); const pending = JSON.stringify(g.state.pending); await g.loadHistory()
  expect(g.state.records.map(r => r.player_input)).toEqual(['old', 'unfinished']); expect(JSON.stringify(g.state.pending)).toBe(pending)
})
it('valid latest choice sends choice_id instead of free text', async () => {
  const api = server((path, init) => path.endsWith('/state') ? response(campaign({ latest_turn: { ...turn('old', 0), choices: [{ id: 'choice-id', label: '中へ' }] } }))
    : init.method === 'POST' ? response(turn('new', 0)) : undefined)
  const g = await playing(api); await g.submit({ kind: 'choice', choice_id: 'choice-id' }, '中へ')
  expect(JSON.parse(String(api.posts()[0]!.init.body)).content).toEqual({ kind: 'choice', choice_id: 'choice-id' })
})
it('lost terminal event is recovered when an open SSE stream goes silent', async () => {
  vi.useFakeTimers()
  let done = false
  const api = server(path => path.endsWith('/state') ? response(campaign({ latest_turn: done ? turn('server', 0) : waiting('server') }))
    : path.endsWith('/turns/server') ? (done = true, response(turn('server', 0))) : undefined)
  const g = await playing(api); Events.instances[0]!.onopen?.(); await vi.advanceTimersByTimeAsync(16000)
  expect(g.state.tracking).toBe(false); expect(g.state.records[0]!.turn.narration).toBe('扉が開いた。')
  expect(api.posts()).toHaveLength(0)
})
it('corrupt persisted pending fails closed and never gets replaced with a new POST', async () => {
  localStorage.setItem('ai-rpg:vue:principal-a:pending', '{corrupt')
  const api = server(); const g = await playing(api); await g.submit(text, text.text); await g.startAdventure(start)
  expect(api.posts()).toHaveLength(0); expect(g.state.storageBlocked).toBe(true)
  expect(localStorage.getItem('ai-rpg:vue:principal-a:pending')).toBe('{corrupt')
})
it('a lagging state response cannot replace the live latest turn or unlock stale-version actions', async () => {
  let version = 0
  const api = server((path, init) => path.endsWith('/state') ? response(campaign({ state_version: version, latest_turn: turn('old', 0) }))
    : init.method === 'POST' ? response({ ...turn('new', 1), choices: [{ id: 'new-choice', label: '新しい選択' }] }) : undefined)
  const g = await playing(api); await g.submit(text, text.text)
  expect(g.state.campaign?.latest_turn?.turn_id).toBe('new')
  expect(g.canAct.value).toBe(false)
  version = 1; await g.recover(); expect(g.canAct.value).toBe(true)
  expect(g.state.campaign?.latest_turn?.turn_id).toBe('new')
})
it('SSE completion keeps controls locked until authoritative state refresh completes', async () => {
  const delayed = deferred<Response>(); let calls = 0
  const api = server(path => path.endsWith('/state') ? ++calls === 1 ? response(campaign({ latest_turn: waiting() })) : delayed.promise : undefined)
  const g = await playing(api); Events.instances[0]!.emit(turn()); await flush()
  expect(g.canAct.value).toBe(false)
  delayed.resolve(response(campaign({ state_version: 1, latest_turn: turn() }))); await flush()
  expect(g.canAct.value).toBe(true)
})
it.each(['recover', 'submit'])('a newer server pending turn supersedes a completed turn during %s', async entry => {
  let current = campaign()
  const completed = turn('completed-a', 1)
  const api = server((path, init) => {
    if (path.endsWith('/state')) return response(current)
    if (init.method === 'POST') {
      current = campaign({ state_version: 1, latest_turn: completed })
      return response(completed)
    }
  })
  const g = await playing(api); await g.submit(text, text.text)
  expect(g.state.pending).toBeNull(); expect(g.canAct.value).toBe(true)

  current = campaign({ state_version: 1, latest_turn: waiting('server-b') })
  if (entry === 'recover') await g.recover(); else await g.submit(text, text.text)
  expect(g.state.campaign?.latest_turn?.turn_id).toBe('server-b')
  expect(g.state.tracking).toBe(true); expect(g.canAct.value).toBe(false)
  expect(api.posts()).toHaveLength(1); expect(Events.instances).toHaveLength(1)
  expect(g.state.records.find(r => r.turn.turn_id === 'completed-a')?.turn.narration).toBe('扉が開いた。')

  current = campaign({ state_version: 1, latest_turn: completed })
  await g.refreshState()
  expect(g.state.campaign?.latest_turn?.turn_id).toBe('server-b')
  expect(g.canAct.value).toBe(false)

  current = campaign({ state_version: 2, latest_turn: turn('server-b', 2) })
  Events.instances[0]!.emit(current.latest_turn!); await flush()
  expect(g.state.campaign?.latest_turn?.turn_id).toBe('server-b')
  expect(g.state.tracking).toBe(false); expect(g.canAct.value).toBe(true)
  expect(g.state.records.map(r => r.turn.turn_id)).toEqual(['completed-a', 'server-b'])
  expect(api.posts()).toHaveLength(1)
})
it('successful state recovery clears a tracking warning only after completion is confirmed', async () => {
  let current = campaign({ latest_turn: waiting('server') }), failState = false
  const api = server(path => path.endsWith('/state') ? failState ? response({}, 503) : response(current)
    : path.includes('/turns/') ? response({}, 503) : undefined)
  const g = await playing(api); Events.instances[0]!.onerror?.(); await flush()
  expect(g.state.trackingError).not.toBe('')
  failState = true; await g.recover()
  expect(g.state.trackingError).not.toBe(''); expect(g.canAct.value).toBe(false)

  failState = false; current = campaign({ state_version: 1, latest_turn: turn('server', 1) })
  await g.recover()
  expect(g.canAct.value).toBe(true); expect(g.state.trackingError).toBe('')
  expect(api.posts()).toHaveLength(0)
})
