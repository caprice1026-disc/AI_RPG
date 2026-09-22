import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createGame } from '../src/game'
import type { Pending } from '../src/contracts'
import { campaign, deferred, Events, flush, response, saved, server, session, turn, waiting } from './fixtures'

const games: ReturnType<typeof createGame>[] = []
function app(api = server()) {
  const game = createGame({ fetch: api.fetch, storage: localStorage, events: (url: string) => new Events(url) })
  games.push(game)
  return game
}
async function playing(api = server()) {
  const g = app(api); expect(typeof g.boot).toBe('function'); await g.boot(); await g.selectAdventure(saved); return g
}
const start = { scenario_ref: 'chapel', preset_ref: 'scout', player_name: '  葵  ' }
const text = { kind: 'text' as const, text: '調べる' }
beforeEach(() => { localStorage.clear(); Events.instances = [] })
afterEach(() => { games.splice(0).forEach(g => g.dispose?.()); vi.useRealTimers() })

describe('session and public home', () => {
  it.each([401, 503])('distinguishes session HTTP %s without private requests', async status => {
    const api = server(() => response({}, status)); const g = app(api)
    expect(typeof g.boot).toBe('function'); await g.boot()
    expect(g.state.auth).toBe(status === 401 ? 'signed-out' : 'unavailable')
    expect(g.state.session).toBeNull()
    expect(api.calls.map(c => c.path)).toEqual(['/auth/session'])
  })
  it.each(['session', 'development', 'bearer'])('loads registered %s session and resume list', async mode => {
    const api = server(path => path === '/auth/session' ? response({ ...session, mode }) : undefined)
    const g = app(api); expect(typeof g.boot).toBe('function'); await g.boot()
    expect(g.state.session!.mode).toBe(mode)
    expect(g.state.adventures[0].title).toBe('廃礼拝堂の依頼')
    expect(api.calls.every(c => c.init.credentials === 'same-origin')).toBe(true)
    expect(JSON.stringify(localStorage)).not.toContain('memory-only-csrf')
  })
})

describe('durable operations', () => {
  it.each(['loss', '503', 'json'])('unknown start %s survives reload with exact explicit retry', async failure => {
    let attempts = 0
    const api = server((path, init) => {
      if (path === '/adventures' && init.method === 'POST') {
        if (++attempts === 1) {
          if (failure === 'loss') throw new Error('lost')
          return failure === '503' ? response({}, 503) : new Response('{', { status: 201 })
        }
        return response(saved, 201)
      }
    })
    const a = app(api); expect(typeof a.boot).toBe('function'); await a.boot(); await a.startAdventure(start)
    const original = api.posts()[0]!.init.body
    a.dispose(); const b = app(api); await b.boot()
    expect(attempts).toBe(1); expect(b.canStart.value).toBe(false)
    await b.retry(b.state.pending)
    expect(api.posts()[1]!.init.body).toBe(original)
    expect(JSON.parse(String(original))).toMatchObject({ ...start, scenario_version: 2 })
    expect(new Headers(api.posts()[0]!.init.headers).get('X-CSRF-Token')).toBe('memory-only-csrf')
    expect(b.state.campaign!.player!.current_hp).toBe(9); expect(b.state.pending).toBeNull()
  })
  it.each(['loss', '503', 'json'])('unknown turn %s reloads original action, id and version', async failure => {
    let attempts = 0
    const api = server((_path, init) => {
      if (init.method === 'POST') {
        if (++attempts === 1) {
          if (failure === 'loss') throw new Error('lost')
          return failure === '503' ? response({}, 503) : new Response('{', { status: 202 })
        }
        return response(turn())
      }
    })
    const a = await playing(api)
    await a.submit({ kind: 'scenario_action', action_ref: 'look' }, '周囲を見る')
    const original = api.posts()[0]!.init.body
    a.dispose(); const b = app(api); await b.boot()
    expect(attempts).toBe(1); expect(b.canAct.value).toBe(false)
    await b.startAdventure(start); await b.submit(text, text.text); expect(attempts).toBe(1)
    await b.retry(b.state.pending)
    expect(api.posts()[1]!.init.body).toBe(original)
    expect(JSON.parse(String(original))).toMatchObject({ expected_state_version: 0, actor_id: 'actor-a', content: { kind: 'scenario_action', action_ref: 'look' } })
    expect(b.state.records).toHaveLength(1); expect(b.state.records[0].player_input).toBe('周囲を見る')
  })
  it('accepted start with failed state GET recovers by GET only', async () => {
    let fail = true
    const api = server((path, init) => init.method === 'POST' ? response(saved) : path.endsWith('/state') && fail ? response({}, 503) : undefined)
    const g = app(api); expect(typeof g.boot).toBe('function'); await g.boot(); await g.startAdventure(start)
    expect(g.state.pending).toMatchObject({ kind: 'start', adventure: { campaign_id: 'campaign-a' } })
    fail = false; await g.retry(g.state.pending)
    expect(api.posts()).toHaveLength(1); expect(g.state.pending).toBeNull()
  })
  it.each(['getItem', 'setItem'] as const)('storage %s failure prevents POST', async method => {
    vi.spyOn(Storage.prototype, method).mockImplementation(() => { throw new Error('denied') })
    const api = server(); const g = await playing(api)
    await g.startAdventure(start); await g.submit(text, text.text)
    expect(api.posts()).toHaveLength(0); expect(g.state.error).toContain('保存')
  })
  it('duplicate clicks send only one request', async () => {
    const accepted = deferred<Response>()
    const api = server((_path, init) => init.method === 'POST' ? accepted.promise : undefined)
    const g = await playing(api)
    const first = g.submit(text, text.text); await flush(); await g.submit(text, text.text)
    expect(api.posts()).toHaveLength(1)
    accepted.resolve(response(turn())); await first
  })
  it('401 clears private UI but retains the operation for same-principal reauthentication', async () => {
    let reject = true
    const api = server((_path, init) => init.method === 'POST' ? reject ? response({}, 401) : response(turn()) : undefined)
    const g = await playing(api); await g.submit(text, text.text)
    const original = api.posts()[0]!.init.body
    expect(g.state.auth).toBe('signed-out'); expect(g.state.campaign).toBeNull(); expect(g.state.records).toEqual([])
    reject = false; await g.boot(); expect(g.state.pending).not.toBeNull()
    await g.retry(g.state.pending); expect(api.posts()[1]!.init.body).toBe(original)
  })
  it('another principal and anonymous legacy storage never resume another operation', async () => {
    let principal = 'principal-a'
    const api = server((path, init) => path === '/auth/session' ? response({ ...session, principal_id: principal }) : init.method === 'POST' ? response({}, 503) : undefined)
    const a = await playing(api); await a.submit(text, text.text); a.dispose()
    localStorage.setItem('ai-rpg-pending-turn', JSON.stringify({ secret: 'old-anonymous' }))
    principal = 'principal-b'; const b = app(api); await b.boot()
    expect(b.state.pending).toBeNull(); expect(b.state.selected).toBeNull(); expect(api.posts()).toHaveLength(1)
    b.dispose(); principal = 'principal-a'; const c = app(api); await c.boot()
    expect(c.state.pending).toMatchObject({ kind: 'turn', body: { content: text } })
  })
  it('historical retry cannot replay or clear a later unknown operation', async () => {
    let attempts = 0
    const api = server((path, init) => path.endsWith('/state') ? response(campaign({ state_version: attempts >= 2 ? 1 : 0 }))
      : init.method === 'POST' ? ++attempts === 2 ? response(turn()) : response({}, 503) : undefined)
    const g = await playing(api); await g.submit(text, text.text)
    const old = JSON.parse(JSON.stringify(g.state.pending))
    await g.retry(old); await g.submit({ kind: 'text', text: '次の行動' }, '次の行動')
    const latest = JSON.stringify(g.state.pending)
    await g.retry(old)
    expect(attempts).toBe(3); expect(JSON.stringify(g.state.pending)).toBe(latest)
  })
  it.each([202, 409, 422])('late %s cannot mutate a replacement pending slot', async status => {
    const accepted = deferred<Response>()
    const api = server((_path, init) => init.method === 'POST' ? accepted.promise : undefined)
    const g = await playing(api); const sending = g.submit(text, text.text); await flush()
    const key = Object.keys(localStorage).find(k => k.endsWith(':pending'))!
    const replacement: Pending = { ...JSON.parse(localStorage.getItem(key)!), body: { ...JSON.parse(localStorage.getItem(key)!).body, request_id: 'replacement' } }
    localStorage.setItem(key, JSON.stringify(replacement))
    accepted.resolve(response(status === 202 ? turn() : {}, status)); await sending
    expect(JSON.parse(localStorage.getItem(key)!)).toEqual(replacement)
    expect(g.state.records.some((r: any) => r.turn.turn_id === 'turn-1')).toBe(false)
  })
})

describe('authoritative state and history', () => {
  it('409 refreshes state and preserves draft without replacement submission', async () => {
    let version = 0
    const api = server((path, init) => {
      if (init.method === 'POST') { version = 1; return response({ detail: { code: 'STATE_VERSION_CONFLICT' } }, 409) }
      if (path.endsWith('/state')) return response(campaign({ state_version: version }))
    })
    const g = await playing(api); g.state.draft = '調べる'; await g.submit(text, text.text)
    expect(g.state.pending).toBeNull(); expect(g.state.campaign!.state_version).toBe(1)
    expect(g.state.draft).toBe('調べる'); expect(g.state.error).toContain('確認'); expect(api.posts()).toHaveLength(1)
  })
  it('refreshed version or removed registered action requires reconsideration before POST', async () => {
    let version = 0
    const api = server(path => path.endsWith('/state') ? response(campaign({ state_version: version })) : undefined)
    const g = await playing(api); version = 1
    await g.submit({ kind: 'scenario_action', action_ref: 'look' }, '周囲を見る')
    await g.submit({ kind: 'scenario_action', action_ref: 'unknown' }, '未知')
    expect(api.posts()).toHaveLength(0)
  })
  it('new selection wins against delayed state success and history', async () => {
    const delayed = deferred<Response>()
    const api = server(path => path === '/campaigns/campaign-a/state' ? delayed.promise : undefined)
    const g = app(api); expect(typeof g.boot).toBe('function'); await g.boot()
    const old = g.selectAdventure(saved); await flush()
    await g.selectAdventure({ campaign_id: 'campaign-b', actor_id: 'actor-b' })
    delayed.resolve(response(campaign({ state_version: 9 }))); await old
    expect(g.state.selected!.campaign_id).toBe('campaign-b'); expect(g.state.campaign!.state_version).toBe(0)
  })
  it('same-campaign late refresh and lower versions cannot roll state back', async () => {
    let calls = 0; const delayed = deferred<Response>()
    const api = server(path => path.endsWith('/state') ? ++calls === 2 ? delayed.promise : response(campaign({ state_version: calls === 1 ? 2 : 4 })) : undefined)
    const g = await playing(api); const old = g.refreshState(); await flush(); await g.refreshState()
    delayed.resolve(response(campaign({ state_version: 1, latest_turn: turn('obsolete', 1) }))); await old
    expect(g.state.campaign!.state_version).toBe(4); expect(g.state.records).toEqual([])
  })
  it('history initial failure reconciles complete input/output pairs after live play', async () => {
    let historyCalls = 0; let latest = turn('old', 0)
    const api = server((path, init) => {
      if (path.endsWith('/state')) return response(campaign({ latest_turn: latest }))
      if (init.method === 'POST') { latest = turn('new', 0); return response(latest) }
      if (path.includes('/history?')) return ++historyCalls === 1 ? response({}, 503) : response({ items: [
        { created_at: '2026-09-22T00:00:00Z', player_input: 'old input', turn: turn('old', 0) },
        { created_at: '2026-09-22T00:01:00Z', player_input: 'new input', turn: turn('new', 0) },
      ], next_before_turn_id: null })
    })
    const g = await playing(api); await g.submit({ kind: 'text', text: 'new input' }, 'new input'); await g.loadHistory()
    expect(g.state.records.map((r: any) => [r.turn.turn_id, r.player_input])).toEqual([['old', 'old input'], ['new', 'new input']])
    expect(g.state.historyError).toBe('')
  })
  it('history pagination uses before_turn_id and old choices stay invalid', async () => {
    const api = server(path => {
      if (path.endsWith('/state')) return response(campaign({ latest_turn: { ...turn('recent', 0), choices: [{ id: 'current', label: '続く' }] } }))
      if (path.includes('/history?')) return response({ items: [{ created_at: path.includes('before_turn_id') ? '2026-09-21' : '2026-09-22', player_input: path.includes('before_turn_id') ? 'old' : 'recent', turn: turn(path.includes('before_turn_id') ? 'old' : 'recent', 0) }], next_before_turn_id: path.includes('before_turn_id') ? null : 'recent' })
    })
    const g = await playing(api); await g.loadHistory()
    expect(api.calls.at(-1)!.path).toContain('before_turn_id=recent')
    expect(g.state.records.map((r: any) => r.turn.turn_id)).toEqual(['old', 'recent'])
    await g.submit({ kind: 'choice', choice_id: 'old' }, '昔'); expect(api.posts()).toHaveLength(0)
  })
})

describe('turn tracking lifecycle', () => {
  it('logout response loss reacquires CSRF without restoring private data before retry', async () => {
    let logoutCalls = 0, sessionCalls = 0
    const api = server((path, init) => {
      if (path === '/auth/session') return response({ ...session, csrf_token: ++sessionCalls === 1 ? 'first-csrf' : 'fresh-csrf' })
      if (path === '/auth/logout') {
        expect(g.state.campaign).toBeNull()
        if (++logoutCalls === 1) throw new Error('response lost')
        return new Headers(init.headers).get('X-CSRF-Token') === 'fresh-csrf' ? response(null, 204) : response({}, 403)
      }
    })
    const g = await playing(api); await g.logout()
    expect(g.state.auth).toBe('unavailable'); expect(g.state.session).toBeNull()
    await g.logout()
    expect(g.state.auth).toBe('signed-out'); expect(sessionCalls).toBe(2)
    expect(api.posts()).toHaveLength(2); expect(g.state.adventures).toEqual([])
  })
  it('logout response loss with already-ended session confirms success without another POST', async () => {
    let loggedOut = false
    const api = server(path => {
      if (path === '/auth/session' && loggedOut) return response({}, 401)
      if (path === '/auth/logout') { loggedOut = true; throw new Error('lost after logout') }
    })
    const g = await playing(api); await g.logout(); await g.logout()
    expect(g.state.auth).toBe('signed-out'); expect(api.posts()).toHaveLength(1)
  })
  it('server pending turn resumes without browser storage; healthy SSE avoids polling', async () => {
    vi.useFakeTimers()
    const api = server(path => path.endsWith('/state') ? response(campaign({ latest_turn: waiting() })) : undefined)
    const g = await playing(api); expect(g.canAct.value).toBe(false)
    expect(Events.instances).toHaveLength(1); Events.instances[0]!.onopen?.()
    await vi.advanceTimersByTimeAsync(5000)
    expect(api.calls.filter(c => c.path.includes('/turns/'))).toHaveLength(0)
    expect(api.posts()).toHaveLength(0)
    g.dispose(); expect(Events.instances[0]!.closed).toBe(true)
  })
  it('committed SSE updates HP while narration is pending and preserves final result', async () => {
    let current = campaign({ latest_turn: waiting() })
    const api = server(path => path.endsWith('/state') ? response(current) : undefined)
    const g = await playing(api)
    const committed = { ...turn(), narration_status: 'generating' as const, narration: null }
    current = campaign({ state_version: 1, latest_turn: committed, player: { ...campaign().player!, current_hp: 3 } })
    Events.instances[0]!.emit(committed); await flush()
    expect(g.state.campaign!.player!.current_hp).toBe(3); expect(g.canAct.value).toBe(false)
    current = { ...current, latest_turn: turn() }; Events.instances[0]!.emit(turn()); await flush()
    expect(g.state.records).toHaveLength(1); expect(g.state.records[0].turn.narration).toBe('扉が開いた。')
    expect(Events.instances[0]!.closed).toBe(true)
  })
  it('disconnected SSE falls back to bounded GETs and stops on switch', async () => {
    vi.useFakeTimers()
    const api = server(path => path.endsWith('/state') ? response(campaign({ latest_turn: waiting() })) : path.includes('/turns/') ? response(waiting()) : undefined)
    const g = await playing(api); Events.instances[0]!.onerror?.(); await flush()
    expect(Events.instances[0]!.closed).toBe(true)
    await vi.advanceTimersByTimeAsync(180000)
    const gets = api.calls.filter(c => c.path.includes('/turns/')).length
    expect(gets).toBeGreaterThan(0); expect(gets).toBeLessThanOrEqual(30)
    expect(g.state.tracking).toBe(false); expect(g.state.trackingError).not.toBe('')
    await vi.advanceTimersByTimeAsync(60000); expect(api.calls.filter(c => c.path.includes('/turns/'))).toHaveLength(gets)
    g.goHome(); Events.instances[0]!.emit(turn()); await flush(); expect(g.state.records).toEqual([])
  })
  it('logout closes tracking, clears private data immediately, and sends CSRF', async () => {
    const api = server(path => path.endsWith('/state') ? response(campaign({ latest_turn: waiting() })) : undefined)
    const g = await playing(api); await g.logout()
    expect(Events.instances[0]!.closed).toBe(true); expect(g.state.campaign).toBeNull()
    expect(g.state.adventures).toEqual([]); expect(g.state.session).toBeNull()
    expect(new Headers(api.posts()[0]!.init.headers).get('X-CSRF-Token')).toBe('memory-only-csrf')
  })
})
