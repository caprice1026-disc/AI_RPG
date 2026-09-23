<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { createGame } from './game'
import TurnRecord from './TurnRecord.vue'
import Feedback from './Feedback.vue'

const game = createGame(), s = game.state
const { canStart, canAct } = game
const art = '/static/vue/art/ruined-chapel.png'
const navOpen = ref(false), stateOpen = ref(false), actionInput = ref<HTMLTextAreaElement>()
const timeline = ref<HTMLOListElement>()
const start = reactive({ scenario_ref: '', preset_ref: '', player_name: '' })
const scenario = computed(() => s.catalog.scenarios.find(x => x.scenario_ref === start.scenario_ref))
const preset = computed(() => s.catalog.presets.find(x => x.preset_ref === start.preset_ref))
const adventure = computed(() => s.campaign?.adventure), player = computed(() => s.campaign?.player)
const actions = computed(() => (adventure.value?.available_actions ?? []).map(action => ({ ...action,
  run: game.bindAction({ kind: 'scenario_action', action_ref: action.action_ref }, action.label),
})))
const names = computed(() => s.names)
const loginError = ref('')
const loginMessages: Record<string, string> = {
  LOGIN_REJECTED: 'ログインを完了できませんでした。もう一度お試しください。',
  IDENTITY_NOT_REGISTERED: 'このアカウントは参加登録されていません。主催者に登録状況を確認してください。',
  AUTHENTICATION_UNAVAILABLE: '認証サービスを利用できません。時間をおいてお試しください。',
}
watch(() => s.catalog, () => {
  if (!s.catalog.scenarios.some(x => x.scenario_ref === start.scenario_ref)) start.scenario_ref = s.catalog.scenarios[0]?.scenario_ref ?? ''
  if (!s.catalog.presets.some(x => x.preset_ref === start.preset_ref)) start.preset_ref = s.catalog.presets[0]?.preset_ref ?? ''
})
watch(() => s.auth, auth => { if (auth !== 'ready') { start.player_name = ''; navOpen.value = false; stateOpen.value = false } })
function revealLatest() {
  const turnId = s.campaign?.latest_turn?.turn_id
  const record = Array.from(timeline.value?.children ?? []).find(node => (node as HTMLElement).dataset.turnId === turnId)
  record?.scrollIntoView({ block: 'start', behavior: 'auto' })
}
watch([() => s.selected?.campaign_id, () => s.campaign?.latest_turn?.turn_id,
  () => s.campaign?.latest_turn?.narration_status, () => s.campaign?.latest_turn?.narration], async () => {
  await nextTick()
  revealLatest()
})
onMounted(() => {
  const query = new URLSearchParams(location.search)
  if (query.has('login_error')) {
    loginError.value = loginMessages[query.get('login_error')!] ?? loginMessages.LOGIN_REJECTED!
    history.replaceState(history.state, '', location.pathname)
  }
  void game.boot()
})
onUnmounted(game.dispose)
function sendText() { if (s.draft.trim()) void game.submit({ kind: 'text', text: s.draft }, s.draft) }
function heal() { s.draft = '回復ポーションを使って、自分の傷を回復する。'; actionInput.value?.focus() }
</script>

<template>
  <a class="skip-link" href="#main">本文へ移動</a>
  <header class="topbar">
    <button class="brand" aria-label="AI RPG ホーム" @click="game.goHome()">AI RPG</button>
    <span class="tagline">冒険の記録</span>
    <div class="account">
      <span v-if="s.session?.mode === 'development'" class="hint">開発モード</span>
      <button v-if="s.auth === 'ready' && s.session?.mode === 'session'" class="quiet" @click="game.logout()">ログアウト</button>
    </div>
  </header>

  <main v-if="s.auth !== 'ready'" id="main" class="landing">
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
        <p class="hint">登録済みのアカウントでログインしてください。</p>
        <button class="quiet" @click="game.boot()">ログイン状態を再確認</button>
      </template>
    </div>
  </main>

  <main v-else id="main" class="shell" :class="{ playing: s.selected }">
    <aside class="adventure-rail" :class="{ expanded: navOpen }" aria-label="冒険の記録">
      <button class="mobile-toggle" :aria-expanded="navOpen" aria-controls="adventure-nav" @click="navOpen = !navOpen">☰ 冒険の記録</button>
      <nav id="adventure-nav" aria-label="続きから">
        <h2>続きから</h2>
        <p v-if="s.homeLoading" class="hint" role="status">冒険を読み込んでいます…</p>
        <p v-else-if="!s.adventures.length" class="hint">まだ冒険の記録はありません。</p>
        <ul class="adventure-list">
          <li v-for="item in s.adventures" :key="item.campaign_id">
            <button :data-adventure="item.campaign_id" :aria-current="s.selected?.campaign_id === item.campaign_id ? 'page' : undefined"
              @click="game.selectAdventure(item); navOpen = false">
              {{ item.title }}<small>{{ item.player_name }} · {{ item.status === 'completed' ? '完了' : '冒険中' }}</small>
            </button>
          </li>
        </ul>
        <button class="new-adventure" @click="game.goHome(); navOpen = false">＋ 新しい冒険</button>
        <p v-if="s.homeError" class="error" role="alert">{{ s.homeError }}</p>
        <button class="quiet" :disabled="s.homeLoading" @click="game.refreshHome()">一覧を更新</button>
      </nav>
    </aside>

    <section v-if="!s.selected" class="start-page">
      <img :src="art" class="start-art" alt="霧の中にたたずむ廃礼拝堂">
      <div class="start-content">
        <h1>新しい物語を、ここから。</h1>
        <p class="hint">舞台と冒険者を選んで、最初の一歩を。</p>
        <form id="start-form" @submit.prevent="game.startAdventure(start)">
          <label for="scenario">シナリオ</label>
          <select id="scenario" v-model="start.scenario_ref" :disabled="!canStart" required aria-describedby="scenario-description">
            <option v-for="item in s.catalog.scenarios" :key="item.scenario_ref" :value="item.scenario_ref">{{ item.title }}</option>
          </select>
          <p id="scenario-description" class="hint">{{ scenario?.objective }}</p>
          <label for="preset">冒険者のタイプ</label>
          <select id="preset" v-model="start.preset_ref" :disabled="!canStart" required aria-describedby="preset-description">
            <option v-for="item in s.catalog.presets" :key="item.preset_ref" :value="item.preset_ref">{{ item.name }}</option>
          </select>
          <p id="preset-description" class="hint">{{ preset?.description }}<template v-if="preset">（HP {{ preset.max_hp }}）</template></p>
          <label for="player-name">冒険者の名前</label>
          <input id="player-name" v-model="start.player_name" :disabled="!canStart" required maxlength="40" autocomplete="off" placeholder="1〜40文字">
          <button class="primary" type="submit" :disabled="!canStart || !start.player_name.trim()">{{ s.busy ? '冒険を準備しています…' : '冒険を始める' }}</button>
        </form>
        <div v-if="s.pending" class="notice" role="status">
          <p>前の要求の結果が未確認です。保存した内容で再開できます。</p>
          <button :disabled="s.busy || s.tracking" @click="game.retry(s.pending)">{{ s.pending.kind === 'start' && s.pending.adventure ? '作成済みの冒険を開く' : '同じ要求を再送' }}</button>
        </div>
        <p v-if="s.error" class="error" role="alert">{{ s.error }}</p>
        <details class="guidance"><summary>遊び方と保存について</summary>
          <p>候補から行動を選ぶか、自由に言葉を入力してください。受け付けられた行動は自動で保存されます。ページを閉じても「続きから」で再開できます。</p>
          <p>戦闘ではHPが減ることがあります。持ち物で回復したり、危険なときは撤退する行動を選んだりできます。</p>
        </details>
        <Feedback />
      </div>
    </section>

    <section v-else class="story" aria-label="冒険の物語">
      <div class="story-scroll">
        <header class="scene-heading">
          <p class="eyebrow">{{ adventure?.title || '冒険の記録' }}</p>
          <h1>{{ adventure?.current_scene?.title || (s.loading ? '冒険を読み込んでいます…' : '物語') }}</h1>
          <p v-if="adventure?.objective" class="objective">{{ adventure.objective }}</p>
        </header>
        <img v-if="adventure" :src="art" class="scene-art" alt="月明かりに浮かぶ廃礼拝堂の情景">
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
          <p v-if="!s.records.length && s.historyLoaded && !s.loading" class="hint">まだ行動の記録はありません。最初の一歩を選んでみましょう。</p>
          <div v-if="s.pending?.kind === 'turn' && s.pending.campaignId === s.selected.campaign_id && !s.pending.turnId" class="player-input">
            <span class="speaker">確認待ち</span>{{ s.pending.displayText }}
          </div>
          <section v-if="adventure?.status === 'completed'" class="ending" aria-label="冒険の結末">
            <p class="eyebrow">冒険の結末</p><h2>{{ adventure.ending?.title || '冒険を終えました' }}</h2>
            <p>{{ adventure.ending?.summary || '保存された記録を振り返れます。' }}</p>
            <button class="primary" @click="game.goHome()">新しい冒険へ</button>
          </section>
          <div v-if="adventure?.available_actions.length" class="registered-actions" aria-label="行動候補">
            <button v-for="action in actions" :key="`${s.selected.campaign_id}:${s.campaign?.state_version}:${action.action_ref}`" :disabled="!canAct"
              @click="action.run">{{ action.label }}</button>
          </div>
          <details class="guidance"><summary>遊び方と保存について</summary>
            <p>候補から選ぶか、自由に入力できます。受け付けられた行動は自動保存され、再読み込みしても再開できます。結果が未確認なら「同じ要求を再送」で確認してください。</p>
            <p>HPが減ったら持ち物や回復を検討し、危険な場面では撤退も選べます。</p>
          </details>
          <Feedback />
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
        <label class="visually-hidden" for="action-text">行動を入力</label>
        <div class="composer-row">
          <textarea id="action-text" ref="actionInput" v-model="s.draft" rows="2" maxlength="8000" :disabled="!canAct"
            placeholder="どのように行動しますか？" @keydown.ctrl.enter.prevent="sendText" />
          <button class="primary" type="submit" :disabled="!canAct || !s.draft.trim()">{{ s.busy || s.tracking ? '処理中…' : '送信' }}</button>
        </div>
        <p class="composer-hint">行動を選ぶか、自由に入力 · 受付後に自動保存</p>
      </form>
    </section>

    <aside v-if="s.selected" class="character-panel" :class="{ expanded: stateOpen }" aria-label="冒険者の状態">
      <button class="mobile-toggle" :aria-expanded="stateOpen" aria-controls="character-state" @click="stateOpen = !stateOpen">冒険者の状態<template v-if="player"> · HP {{ player.current_hp }} / {{ player.max_hp }}</template></button>
      <div id="character-state">
        <h2>冒険者</h2>
        <p class="player-name">{{ player?.name || '—' }}</p>
        <template v-if="player">
          <div class="hp-label"><span>HP</span><span>{{ player.current_hp }} / {{ player.max_hp }}</span></div>
          <meter :value="player.current_hp" min="0" :max="Math.max(1, player.max_hp)" aria-label="冒険者のHP" />
          <section v-if="adventure?.combat" class="combat">
            <h3>{{ adventure.combat.enemy_name }}</h3><p class="hint">{{ adventure.combat.active ? '戦闘中' : '戦闘なし' }}</p>
            <div class="hp-label"><span>敵のHP</span><span>{{ adventure.combat.current_hp }} / {{ adventure.combat.max_hp }}</span></div>
            <meter :value="adventure.combat.current_hp" min="0" :max="Math.max(1, adventure.combat.max_hp)" aria-label="敵のHP" />
          </section>
          <section><h3>持ち物</h3>
            <ul class="public-list"><li v-for="item in player.inventory" :key="item.item_id">{{ item.name }} × {{ item.quantity }}<small v-if="item.equipped">（装備中）</small></li></ul>
            <p v-if="!player.inventory.length" class="hint">持ち物はありません。</p>
            <button v-if="player.inventory.some(item => item.name.includes('回復ポーション') && item.quantity > 0)" class="quiet" :disabled="!canAct" @click="heal">回復する行動を入力</button>
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
