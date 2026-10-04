<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { createGame } from './game'
import TurnRecord from './TurnRecord.vue'
import Feedback from './Feedback.vue'
import StoryAuthor from './StoryAuthor.vue'
import Community from './Community.vue'
import { createStories } from './stories'
import type { AdventureId, Catalog } from './contracts'
import type { StoryPublicDetail } from './story-contracts'

const game = createGame(), s = game.state
const authoring = ref(false)
const communityOpen = ref(false), registrationEnabled = ref(false)
const sharedStart = ref(false)
let publicGeneration = 0
const optionsController = new AbortController()
const editor = createStories(game, () => { authoring.value = false })
const sharedStory = ref<StoryPublicDetail | null>(null)
const sharedError = ref('')
function goHome() { if (!authoring.value || editor.confirmLeave()) { ++publicGeneration; authoring.value = false; communityOpen.value = false; sharedStart.value = false; game.goHome(); navOpen.value = false } }
function showCommunity() { if (!authoring.value || editor.confirmLeave()) { ++publicGeneration; authoring.value = false; communityOpen.value = true; navOpen.value = false } }
function showAuthor() { ++publicGeneration; communityOpen.value = false; authoring.value = true; navOpen.value = false }
function selectAdventure(id: AdventureId) {
  if (authoring.value && !editor.confirmLeave()) return
  ++publicGeneration; authoring.value = false; communityOpen.value = false; dismissedRiskId.value = null; navOpen.value = false; void game.selectAdventure(id)
}
function logout() { if (!authoring.value || editor.confirmLeave()) void game.logout() }
async function loadSharedStory(id: string) {
  const principal = s.session?.principal_id, generation = ++publicGeneration
  sharedError.value = ''
  try {
    const detail = await game.request<StoryPublicDetail>(`/stories/${encodeURIComponent(id)}`, {}, false)
    if (s.auth !== 'ready' || s.session?.principal_id !== principal || generation !== publicGeneration) return
    sharedStory.value = detail
    game.goHome(); authoring.value = false; communityOpen.value = false; sharedStart.value = true
    if (!['mvp_v1', 'mvp_v2'].includes(detail.ruleset_ref)) sharedError.value = 'この作品のルールには対応していません。'
  } catch { if (s.auth === 'ready' && s.session?.principal_id === principal && generation === publicGeneration) { game.goHome(); sharedError.value = '作品を表示できませんでした。共有範囲や公開状態を確認してください。' } }
}
function rememberSharedStory(id: string) { try { sessionStorage.setItem('ai-rpg:shared-story', id) } catch { /* The link remains usable when storage is unavailable. */ } }
function loginToStory(id: string) { rememberSharedStory(id); location.assign('/auth/login') }
const { canAct } = game
const canStart = computed(() => sharedStart.value ? game.canStartVersion.value && !!sharedStory.value && !sharedError.value : game.canStart.value)
const art = '/static/vue/art/ruined-chapel.png'
const navOpen = ref(false), stateOpen = ref(false)
const timeline = ref<HTMLOListElement>()
const waitingVisual = ref<HTMLElement>()
const start = reactive({ scenario_ref: '', preset_ref: '', player_name: '',
  ability_points: { strength: 0, agility: 0, insight: 0, presence: 0 }, specialty_skill: '' })
const scenario = ref<Catalog['scenarios'][number]>()
const catalogUpdated = computed(() => {
  const latest = s.catalog.scenarios.find(item => item.scenario_ref === scenario.value?.scenario_ref)
  return !sharedStart.value && latest && scenario.value && (latest.story_version_id !== scenario.value.story_version_id || latest.scenario_version !== scenario.value.scenario_version)
})
function useLatestCatalog() { scenario.value = s.catalog.scenarios.find(item => item.scenario_ref === start.scenario_ref) }
watch(() => start.scenario_ref, value => { scenario.value = s.catalog.scenarios.find(x => x.scenario_ref === value) }, { flush: 'sync' })
const preset = computed(() => s.catalog.presets.find(x => x.preset_ref === start.preset_ref))
const abilityNames = { strength: '筋力', agility: '器用さ', insight: '洞察', presence: '対話力' }
const skillNames: Record<string, string> = {
  athletics: '運動', acrobatics: '身のこなし', perception: '観察',
  stealth: '隠密', persuasion: '説得',
}
const characterCreation = computed(() => sharedStart.value ? sharedStory.value?.ruleset_ref === 'mvp_v2'
  ? { abilities: ['strength', 'agility', 'insight', 'presence'] as const, points: 2, specialties: Object.keys(skillNames) } : null
  : scenario.value?.character_creation)
function startSelected() {
  if (sharedStart.value && sharedStory.value && !sharedError.value) return game.startPublishedAdventure({ ...start, story_version_id: sharedStory.value.story_version_id }, sharedStory.value.ruleset_ref)
  return game.startAdventure(start, scenario.value)
}
const pointsSpent = computed(() => Object.values(start.ability_points).reduce((a, b) => a + b, 0))
const buildReady = computed(() => !characterCreation.value || (pointsSpent.value === characterCreation.value.points
  && !!start.specialty_skill && Object.entries(start.ability_points).every(([key, value]) =>
    value >= 0 && value <= 2 && (preset.value?.base_abilities?.[key as keyof typeof start.ability_points] ?? 0) + value <= 3)))
const adventure = computed(() => s.campaign?.adventure), player = computed(() => s.campaign?.player)
const dismissedRiskId = ref<string | null>(null)
const riskPreview = computed(() => {
  const preview = s.campaign?.latest_turn?.risk_preview
  return preview && preview.proposal_id !== dismissedRiskId.value && adventure.value?.status === 'active' ? preview : null
})
const actions = computed(() => (adventure.value?.available_actions ?? []).map(action => ({ ...action,
  run: game.bindAction({ kind: 'scenario_action', action_ref: action.action_ref }, action.label),
})))
const inventory = computed(() => (player.value?.inventory ?? []).map(item => {
  const effect = item.effect_ref === undefined ? item.item_ref : item.effect_ref
  if (effect !== 'healing_potion') return { ...item, use: null }
  const reason = adventure.value?.status === 'completed' ? 'この冒険は完了しました。'
    : s.busy || s.tracking ? '行動を処理中です。'
    : !s.stateReady || s.loading ? '冒険の状態を確認してください。'
    : s.pending ? '前の要求の結果を確認してください。'
    : !canAct.value ? (s.error || '現在は使用できません。')
    : item.quantity <= 0 ? '残りがありません。'
    : player.value!.current_hp >= player.value!.max_hp ? 'HPは満タンです。' : ''
  const text = `${item.name}を使って、自分の傷を回復する。`
  return { ...item, use: { reason, run: game.bindAction({ kind: 'text', text }, text) } }
}))
const waitingDice = computed(() => {
  const turn = s.campaign?.latest_turn
  return turn ? [...turn.action_results, ...turn.enemy_reactions]
    .flatMap(action => action.result.kind === 'applied' ? action.result.dice : []) : []
})
const waitingPhase = computed(() => {
  const turn = s.campaign?.latest_turn
  if (!s.tracking || !turn) return null
  if (turn.resolution_status === 'pending' || turn.resolution_status === 'resolving') return 'thinking'
  if (turn.resolution_status === 'committed' && turn.narration_status !== 'completed'
    && turn.narration_status !== 'fallback') return waitingDice.value.length ? 'roll' : 'narration'
  return null
})
const names = computed(() => s.names)
const loginError = ref('')
const loginMessages: Record<string, string> = {
  LOGIN_REJECTED: 'ログインを完了できませんでした。もう一度お試しください。',
  IDENTITY_NOT_REGISTERED: 'このアカウントは参加登録されていません。主催者に登録状況を確認してください。',
  AUTHENTICATION_UNAVAILABLE: '認証サービスを利用できません。時間をおいてお試しください。',
}
watch(() => s.catalog, () => {
  if (!s.catalog.scenarios.some(x => x.scenario_ref === start.scenario_ref)) start.scenario_ref = s.catalog.scenarios[0]?.scenario_ref ?? ''
  if (!scenario.value) scenario.value = s.catalog.scenarios.find(x => x.scenario_ref === start.scenario_ref)
  if (!s.catalog.presets.some(x => x.preset_ref === start.preset_ref)) start.preset_ref = s.catalog.presets[0]?.preset_ref ?? ''
})
watch(() => s.auth, auth => { if (auth !== 'ready') { ++publicGeneration; start.player_name = ''; start.scenario_ref = ''; scenario.value = undefined; navOpen.value = false; stateOpen.value = false; authoring.value = false; communityOpen.value = false; sharedStory.value = null; sharedStart.value = false; sharedError.value = '' } })
function revealLatest() {
  const turnId = s.campaign?.latest_turn?.turn_id
  const record = Array.from(timeline.value?.children ?? []).find(node => (node as HTMLElement).dataset.turnId === turnId)
  record?.scrollIntoView({ block: 'start', behavior: 'auto' })
}
watch([() => s.selected?.campaign_id, () => s.historyLoaded, () => s.campaign?.latest_turn?.turn_id,
  () => s.campaign?.latest_turn?.resolution_status, () => s.campaign?.latest_turn?.narration_status,
  () => s.campaign?.latest_turn?.narration], async () => {
  await nextTick()
  if (waitingPhase.value) waitingVisual.value?.scrollIntoView({ block: 'nearest', behavior: 'auto' })
  else revealLatest()
})
onMounted(() => {
  const query = new URLSearchParams(location.search)
  if (query.get('story')) rememberSharedStory(query.get('story')!)
  void fetch('/auth/options', { credentials: 'same-origin', signal: optionsController.signal })
    .then(async result => { if (result.ok && !optionsController.signal.aborted) registrationEnabled.value = (await result.json()).registration_enabled === true }).catch(() => {})
  if (query.has('login_error')) {
    loginError.value = loginMessages[query.get('login_error')!] ?? loginMessages.LOGIN_REJECTED!
    history.replaceState(history.state, '', location.pathname)
  }
  void game.boot().then(() => {
    let id = query.get('story')
    try { id ??= sessionStorage.getItem('ai-rpg:shared-story'); if (s.auth === 'ready') sessionStorage.removeItem('ai-rpg:shared-story') } catch { /* Login still works without storage. */ }
    if (s.auth === 'ready' && id) return loadSharedStory(id)
  })
})
onUnmounted(() => { optionsController.abort(); editor.dispose(); game.dispose() })
function sendText() { if (s.draft.trim()) void game.submit({ kind: 'text', text: s.draft }, s.draft) }
</script>

<template>
  <a class="skip-link" href="#main">本文へ移動</a>
  <header class="topbar">
    <button class="brand" aria-label="AI RPG ホーム" @click="goHome()">AI RPG</button>
    <span class="tagline">冒険の記録</span>
    <div class="account">
      <span v-if="s.session?.mode === 'development'" class="hint">開発モード</span>
      <button v-if="s.auth === 'ready' && s.session?.mode === 'session'" class="quiet" @click="logout()">ログアウト</button>
    </div>
  </header>

  <main v-if="s.auth !== 'ready' && communityOpen" id="main"><Community :game="game" @login="loginToStory" /></main>
  <main v-else-if="s.auth !== 'ready'" id="main" class="landing">
    <img :src="art" class="landing-art" alt="月明かりと灯火に照らされた、霧の中の廃礼拝堂">
    <div class="landing-copy">
      <h1>あなたの言葉で、物語が動く。</h1>
      <p class="lead">想像する。入力する。<br>そして、まだ見ぬ物語がはじまる。</p>
      <p v-if="loginError" role="alert" class="notice">{{ loginError }}</p>
      <p v-if="s.notice" role="status" class="notice">{{ s.notice }}</p>
      <p v-if="s.auth === 'checking'" role="status">ログイン状態を確認しています…</p>
      <template v-else-if="s.auth === 'unavailable'">
        <button class="primary" @click="game.boot()">接続を再確認</button>
        <button v-if="s.notice.includes('ログアウト')" @click="game.logout()">ログアウトを再試行</button>
      </template>
      <template v-else>
        <a class="primary button" href="/auth/login">ログインして冒険を始める</a>
        <a v-if="registrationEnabled" class="button" href="/auth/login?join=true">参加登録して始める</a>
        <p class="hint">登録済みのアカウントでログインしてください。</p>
        <button class="quiet" @click="showCommunity()">公開作品を探す</button>
        <button class="quiet" @click="game.boot()">ログイン状態を再確認</button>
      </template>
    </div>
  </main>

  <main v-else id="main" class="shell" :class="{ playing: s.selected && !authoring && !communityOpen }">
    <aside class="adventure-rail" :class="{ expanded: navOpen }" aria-label="冒険の記録">
      <button class="mobile-toggle" :aria-expanded="navOpen" aria-controls="adventure-nav" @click="navOpen = !navOpen">☰ 冒険の記録</button>
      <nav id="adventure-nav" aria-label="続きから">
        <h2>続きから</h2>
        <p v-if="s.homeLoading" class="hint" role="status">冒険を読み込んでいます…</p>
        <p v-else-if="!s.adventures.length" class="hint">まだ冒険の記録はありません。</p>
        <ul class="adventure-list">
          <li v-for="item in s.adventures" :key="item.campaign_id">
            <button :data-adventure="item.campaign_id" :aria-current="s.selected?.campaign_id === item.campaign_id ? 'page' : undefined"
              @click="selectAdventure(item)">
              {{ item.title }}<small>{{ item.player_name }} · {{ item.status === 'completed' ? '完了' : '冒険中' }}</small>
            </button>
          </li>
        </ul>
        <button class="new-adventure" @click="goHome()">＋ 新しい冒険</button>
        <button :aria-current="authoring ? 'page' : undefined" @click="showAuthor()">マイ作品</button>
        <button :aria-current="communityOpen ? 'page' : undefined" @click="showCommunity()">公開作品を探す</button>
        <p v-if="s.homeError" class="error" role="alert">{{ s.homeError }}</p>
        <button class="quiet" :disabled="s.homeLoading" @click="game.refreshHome()">一覧を更新</button>
        <div class="nav-support">
          <details class="guidance"><summary>遊び方と保存について</summary>
            <p>候補から選ぶか、自由に入力できます。受け付けられた行動は自動保存され、再読み込みしても「続きから」で再開できます。結果が未確認なら「同じ要求を再送」で確認してください。</p>
            <p>HPが減ったら持ち物の回復ポーションを使用できます。危険な場面では撤退も選べます。</p>
          </details>
          <Feedback />
        </div>
      </nav>
    </aside>

    <KeepAlive><StoryAuthor v-if="authoring" :editor="editor" :game="game" /></KeepAlive>
    <Community v-if="!authoring && communityOpen" :game="game" @select="story => loadSharedStory(story.story_id)" @login="loginToStory" />
    <section v-else-if="!authoring && !s.selected" class="start-page">
      <img v-if="!sharedStart && scenario?.scenario_ref === 'ruined_chapel'" :src="art" class="start-art" alt="霧の中にたたずむ廃礼拝堂">
      <div class="start-content">
        <h1>新しい物語を、ここから。</h1>
        <p class="hint">舞台と冒険者を選んで、最初の一歩を。</p>
        <article v-if="sharedStart && sharedStory" class="notice" aria-label="共有された作品">
          <h2>{{ sharedStory.metadata.title }}</h2><p>{{ sharedStory.metadata.synopsis }}</p>
          <p>第{{ sharedStory.release_number }}版</p>
          <p v-for="warning in sharedStory.metadata.content_warnings" :key="warning">注意: {{ warning }}</p>
          <button type="button" class="quiet" @click="loadSharedStory(sharedStory.story_id)">公開状態を再取得</button>
          <button type="button" class="quiet" @click="sharedStart = false">他の作品を選ぶ</button>
        </article>
        <p v-if="sharedError" class="error" role="alert">{{ sharedError }}</p>
        <form id="start-form" @submit.prevent="startSelected()">
          <template v-if="!sharedStart"><label for="scenario">シナリオ</label>
          <select id="scenario" v-model="start.scenario_ref" :disabled="!canStart" required aria-describedby="scenario-description">
            <option v-for="item in s.catalog.scenarios" :key="item.scenario_ref" :value="item.scenario_ref">{{ item.scenario_ref === scenario?.scenario_ref ? scenario.title : item.title }}</option>
          </select>
          <p id="scenario-description" class="hint">{{ scenario?.objective }}</p></template>
          <div v-if="catalogUpdated" class="notice" role="status"><p>選択中の作品の公開版が更新されました。表示中の版は自動で切り替わりません。</p><button type="button" @click="useLatestCatalog()">更新された版を選び直す</button></div>
          <label for="preset">冒険者のタイプ</label>
          <select id="preset" v-model="start.preset_ref" :disabled="!canStart" required aria-describedby="preset-description">
            <option v-for="item in s.catalog.presets" :key="item.preset_ref" :value="item.preset_ref">{{ item.name }}</option>
          </select>
          <p id="preset-description" class="hint">{{ preset?.description }}<template v-if="preset">（HP {{ preset.max_hp }}）</template></p>
          <fieldset v-if="characterCreation" class="character-build">
            <legend>能力ポイントを配分する（残り {{ characterCreation.points - pointsSpent }}）</legend>
            <label v-for="key in characterCreation.abilities" :key="key" :for="`ability-${key}`">
              {{ abilityNames[key] }} <small>基礎 {{ preset?.base_abilities?.[key] ?? 0 }} / 合計 {{ (preset?.base_abilities?.[key] ?? 0) + start.ability_points[key] }}</small>
              <select :id="`ability-${key}`" v-model.number="start.ability_points[key]" :disabled="!canStart">
                <option v-for="value in [0, 1, 2]" :key="value" :value="value">＋{{ value }}</option>
              </select>
            </label>
            <label for="specialty">得意技能</label>
            <select id="specialty" v-model="start.specialty_skill" :disabled="!canStart" required>
              <option value="" disabled>選択してください</option>
              <option v-for="skill in characterCreation.specialties" :key="skill" :value="skill">{{ skillNames[skill] ?? skill }}</option>
            </select>
          </fieldset>
          <label for="player-name">冒険者の名前</label>
          <input id="player-name" v-model="start.player_name" :disabled="!canStart" required maxlength="40" autocomplete="off" placeholder="1〜40文字">
          <button class="primary" type="submit" :disabled="!canStart || !start.player_name.trim() || !buildReady">{{ s.busy ? '冒険を準備しています…' : '冒険を始める' }}</button>
        </form>
        <div v-if="s.pending" class="notice" role="status">
          <p>前の要求の結果が未確認です。保存した内容で再開できます。</p>
          <button :disabled="s.busy || s.tracking" @click="game.retry(s.pending)">{{ s.pending.kind === 'start' && s.pending.adventure ? '作成済みの冒険を開く' : '同じ要求を再送' }}</button>
        </div>
        <p v-if="s.error" class="error" role="alert">{{ s.error }}</p>
      </div>
    </section>

    <section v-else-if="!authoring && s.selected" class="story" aria-label="冒険の物語">
      <div class="story-scroll">
        <header class="scene-heading">
          <p class="eyebrow">{{ adventure?.title || '冒険の記録' }}</p>
          <h1>{{ adventure?.current_scene?.title || (s.loading ? '冒険を読み込んでいます…' : '物語') }}</h1>
          <p v-if="adventure?.objective" class="objective">{{ adventure.objective }}</p>
        </header>
        <img v-if="adventure?.scenario_ref === 'ruined_chapel'" :src="art" class="scene-art" alt="月明かりに浮かぶ廃礼拝堂の情景">
        <div class="story-body">
          <p v-if="adventure?.current_scene" class="scene-description">{{ adventure.current_scene.description }}</p>
          <p v-if="s.historyError" class="error" role="alert">{{ s.historyError }}</p>
          <button v-if="s.historyError || s.historyCursor" :disabled="s.historyBusy" @click="game.loadHistory()">{{ s.historyError ? '履歴を再取得' : '以前の履歴を読む' }}</button>
          <ol ref="timeline" class="timeline" aria-label="物語の履歴">
            <TurnRecord v-for="record in s.records" :key="record.turn.turn_id" :record="record" :names="names"
              :enabled="canAct && record.turn.turn_id === s.campaign?.latest_turn?.turn_id"
              :hidden-choice-labels="record.turn.turn_id === s.campaign?.latest_turn?.turn_id ? actions.map(action => action.label) : []"
              @choice="(id, label) => game.submit({ kind: 'choice', choice_id: id }, label)" />
          </ol>
          <p v-if="waitingPhase === 'thinking'" ref="waitingVisual" class="thinking-wait" role="status">GMが行動を確認しています…</p>
          <div v-else-if="waitingPhase === 'roll'" ref="waitingVisual" class="dice-wait" role="status">
            <span class="die-spinner" aria-hidden="true">{{ waitingDice[0]?.rolls[0] ?? '?' }}</span>
            <span>ダイスの結果は確定しました。<br>GMが描写を準備しています…</span>
          </div>
          <p v-else-if="waitingPhase === 'narration'" ref="waitingVisual" class="narration-wait" role="status">行動は保存済みです。GMが描写を準備しています…</p>
          <p v-if="!s.records.length && s.historyLoaded && !s.loading" class="hint">まだ行動の記録はありません。最初の一歩を選んでみましょう。</p>
          <div v-if="s.pending?.kind === 'turn' && s.pending.campaignId === s.selected.campaign_id && !s.pending.turnId" class="player-input">
            <span class="speaker">確認待ち</span>{{ s.pending.displayText }}
          </div>
          <section v-if="adventure?.status === 'completed'" class="ending" aria-label="冒険の結末">
            <p class="eyebrow">冒険の結末</p><h2>{{ adventure.ending?.title || '冒険を終えました' }}</h2>
            <p>{{ adventure.ending?.summary || '保存された記録を振り返れます。' }}</p>
            <p v-if="adventure.ending?.reward" class="hint">報酬: {{ adventure.ending.reward }}</p>
            <button class="primary" @click="game.goHome()">新しい冒険へ</button>
          </section>
          <div v-if="adventure?.available_actions.length" class="registered-actions" aria-label="行動候補">
            <button v-for="action in actions" :key="`${s.selected.campaign_id}:${s.campaign?.state_version}:${action.action_ref}`" :disabled="!canAct"
              @click="action.run">{{ action.label }}</button>
          </div>
          <section v-if="riskPreview" class="risk-preview" aria-label="重大なリスクの確認">
            <h2>実行前の確認</h2><p>{{ riskPreview.risk_text }}</p>
            <p class="hint">確認しなければ実行されません。見送るとこの画面では隠れますが、再開すると再表示されます。</p>
            <button :disabled="!canAct" @click="game.submit({ kind: 'confirm_action', proposal_id: riskPreview.proposal_id }, '確認して実行する')">この行動を実行</button>
            <button class="quiet" @click="dismissedRiskId = riskPreview.proposal_id">今回は見送る</button>
          </section>
        </div>
      </div>
      <form class="composer" @submit.prevent="sendText">
        <button v-if="s.records.length" class="quiet latest-link" type="button" @click="revealLatest">最新の結果へ</button>
        <p v-if="s.notice || s.loading" class="progress" role="status">{{ s.loading ? '冒険を読み込んでいます…' : s.notice }}</p>
        <p v-if="s.error" class="error" role="alert">{{ s.error }}</p>
        <div v-if="s.pending && !s.busy && !s.tracking" class="recovery">
          <p>未確認の要求があります。保存した内容で再開できます。</p>
          <button type="button" @click="game.retry(s.pending)">{{ s.pending.kind === 'turn' && s.pending.turnId ? '結果を再確認' : s.pending.kind === 'start' && s.pending.adventure ? '作成済みの冒険を開く' : '同じ要求を再送' }}</button>
        </div>
        <div v-if="s.trackingError" class="recovery" role="status"><p>{{ s.trackingError }}</p><button type="button" @click="game.recover()">結果を再確認</button></div>
        <button v-if="!s.stateReady && !s.loading" type="button" @click="game.recover()">状態を再取得</button>
        <template v-if="adventure?.status !== 'completed'">
          <label class="visually-hidden" for="action-text">行動を入力</label>
          <div class="composer-row">
            <textarea id="action-text" v-model="s.draft" rows="2" maxlength="8000" :disabled="!canAct"
              placeholder="どのように行動しますか？" @keydown.ctrl.enter.prevent="sendText" />
            <button class="primary" type="submit" :disabled="!canAct || !s.draft.trim()">{{ s.busy || s.tracking ? '処理中…' : '送信' }}</button>
          </div>
          <p class="composer-hint">行動を選ぶか、自由に入力 · 受付後に自動保存</p>
        </template>
        <p v-else class="composer-hint">この冒険は完了しました。記録を読み返すか、「新しい冒険へ」から次の物語を始められます。</p>
      </form>
    </section>

    <aside v-if="s.selected && !authoring && !communityOpen" class="character-panel" :class="{ expanded: stateOpen }" aria-label="冒険者の状態">
      <button class="mobile-toggle" :aria-expanded="stateOpen" aria-controls="character-state" @click="stateOpen = !stateOpen">冒険者の状態<template v-if="player"> · HP {{ player.current_hp }} / {{ player.max_hp }}</template></button>
      <div id="character-state">
        <h2>冒険者</h2>
        <p class="player-name">{{ player?.name || '—' }}</p>
        <template v-if="player">
          <div class="hp-label"><span>HP</span><span>{{ player.current_hp }} / {{ player.max_hp }}</span></div>
          <meter :value="player.current_hp" min="0" :max="Math.max(1, player.max_hp)" aria-label="冒険者のHP" />
          <section v-if="player.abilities" class="ability-status"><h3>能力と得意技能</h3>
            <p>筋力 {{ player.abilities.strength }} · 器用さ {{ player.abilities.agility }} · 洞察 {{ player.abilities.insight }} · 対話力 {{ player.abilities.presence }}</p>
            <p class="hint">得意技能: {{ skillNames[player.specialty_skill ?? ''] ?? player.specialty_skill }}</p>
          </section>
          <section v-if="adventure && adventure.elapsed_actions != null" class="pressure-status"><h3>冒険の状況</h3>
            <p>行動数 {{ adventure.elapsed_actions }} · 警戒 {{ adventure.alert_level ?? 0 }} / 5</p>
          </section>
          <section v-if="adventure?.combat" class="combat">
            <h3>{{ adventure.combat.enemy_name }}</h3><p class="hint">{{ adventure.combat.active ? '戦闘中' : '戦闘なし' }}</p>
            <div class="hp-label"><span>敵のHP</span><span>{{ adventure.combat.current_hp }} / {{ adventure.combat.max_hp }}</span></div>
            <meter :value="adventure.combat.current_hp" min="0" :max="Math.max(1, adventure.combat.max_hp)" aria-label="敵のHP" />
          </section>
          <section><h3>持ち物</h3>
            <ul class="public-list inventory"><li v-for="item in inventory" :key="`${s.selected.campaign_id}:${s.campaign?.state_version}:${item.item_id}`">
              <div class="inventory-row">
                <span>{{ item.name }} × {{ item.quantity }}<small v-if="item.equipped">（装備中）</small></span>
                <button v-if="item.use" type="button" :disabled="!!item.use.reason"
                  :aria-label="`${item.name}を使用する`" :aria-describedby="item.use.reason ? `item-reason-${item.item_id}` : undefined"
                  @click="item.use.run">使用する</button>
              </div>
              <p v-if="item.use?.reason" :id="`item-reason-${item.item_id}`" class="hint">{{ item.use.reason }}</p>
            </li></ul>
            <p v-if="!player.inventory.length" class="hint">持ち物はありません。</p>
          </section>
        </template>
        <section><h3>発見したこと</h3>
          <ul class="public-list"><li v-for="(fact, i) in adventure?.discovered_facts" :key="i">{{ fact }}</li></ul>
          <p v-if="!adventure?.discovered_facts.length" class="hint">まだ何も発見していません。<br>冒険で見つけた手がかりがここに記録されます。</p>
        </section>
      </div>
    </aside>
  </main>
</template>
