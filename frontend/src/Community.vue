<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue'
import { HttpError, type Game } from './game'
import type { StoryPublicDetail } from './story-contracts'
const props = defineProps<{ game: Game }>()
const emit = defineEmits<{ select: [story: StoryPublicDetail]; login: [storyId: string] }>()
const q = ref(''), tag = ref(''), stories = ref<StoryPublicDetail[]>([]), cursor = ref<string | null>(null)
const detail = ref<StoryPublicDetail | null>(null), error = ref(''), loading = ref(false)
const name = ref(''), accountError = ref(''), accountNotice = ref(''), accountBusy = ref(false), accountLoaded = ref(false)
const usage = ref<{ day: string; counts: Record<string, number>; limits: Record<string, number>; paused: boolean } | null>(null)
const reportReason = ref(''), reportNotice = ref(''), reportBusy = ref(false), reportPending = ref<{ storyId: string; body: string } | null>(null)
const reportOpen = ref(false)
let generation = 0, detailGeneration = 0, disposed = false, query = { q: '', tag: '' }
const labels: Record<string, string> = { turn: '行動', story_create: '作品の作成', authoring_job: 'AI作成の依頼', llm_game: 'ゲームのAI呼出', llm_authoring: '作成補助のAI呼出' }
async function search(more = false) {
  if (more && (!cursor.value || loading.value)) return
  const g = ++generation; loading.value = true; error.value = ''
  if (!more) { query = { q: q.value, tag: tag.value }; cursor.value = null }
  const params = new URLSearchParams({ ...query, limit: '20', ...(more && cursor.value ? { cursor: cursor.value } : {}) })
  try {
    const result = await props.game.request<{ stories: StoryPublicDetail[]; next_cursor: string | null }>(`/public/stories?${params}`, {}, false)
    if (disposed || g !== generation) return
    const values = more ? [...stories.value, ...result.stories] : result.stories
    stories.value = [...new Map(values.map(value => [value.story_id, value])).values()]; cursor.value = result.next_cursor
  } catch { if (!disposed && g === generation) error.value = '公開作品を取得できませんでした。もう一度検索してください。' }
  finally { if (!disposed && g === generation) loading.value = false }
}
async function open(story: StoryPublicDetail) {
  if (reportPending.value) { error.value = '未確認の通報を再送してから作品を切り替えてください。'; return }
  const g = ++detailGeneration; error.value = ''; detail.value = null; reportReason.value = ''; reportNotice.value = ''; reportOpen.value = false
  try {
    const result = await props.game.request<StoryPublicDetail>(`/public/stories/${encodeURIComponent(story.story_id)}`, {}, false)
    if (!disposed && g === detailGeneration) detail.value = result
  } catch { if (!disposed && g === detailGeneration) error.value = '作品を開けませんでした。公開状態が変わった可能性があります。' }
}
async function account() {
  if (props.game.state.auth !== 'ready' || accountBusy.value) return
  const principal = props.game.state.session?.principal_id
  accountBusy.value = true; accountError.value = ''; accountNotice.value = ''
  const result = await Promise.allSettled([
    props.game.request<{ display_name: string; principal_id: string }>('/profile', {}, false),
    props.game.request<NonNullable<typeof usage.value>>('/usage', {}, false),
  ])
  if (disposed || props.game.state.auth !== 'ready' || principal !== props.game.state.session?.principal_id) return
  if (result[0].status === 'fulfilled') { name.value = result[0].value.display_name; accountLoaded.value = true }
  if (result[1].status === 'fulfilled') usage.value = result[1].value
  if (result.some(value => value.status === 'rejected')) accountError.value = 'アカウント情報を取得できませんでした。再取得してください。'
  accountBusy.value = false
}
async function saveProfile() {
  if (!name.value.trim() || accountBusy.value) return
  const principal = props.game.state.session?.principal_id
  accountBusy.value = true; accountError.value = ''; accountNotice.value = ''
  try {
    const result = await props.game.request<{ display_name: string }>('/profile', { method: 'PUT', body: JSON.stringify({ display_name: name.value }) }, false)
    if (!disposed && props.game.state.session?.principal_id === principal) { name.value = result.display_name; accountNotice.value = '表示名を保存しました。' }
  } catch { if (!disposed) accountError.value = '表示名を保存できませんでした。入力を保持しています。' }
  finally { if (!disposed) accountBusy.value = false }
}
async function report() {
  if (!detail.value || props.game.state.auth !== 'ready' || reportBusy.value || (!reportPending.value && !reportReason.value.trim())) return
  reportPending.value ??= { storyId: detail.value.story_id, body: JSON.stringify({ request_id: crypto.randomUUID(), reason: reportReason.value }) }
  reportBusy.value = true; reportNotice.value = ''
  const principal = props.game.state.session?.principal_id
  try {
    const result = await props.game.request<{ report_id: string }>(`/stories/${encodeURIComponent(reportPending.value.storyId)}/reports`,
      { method: 'POST', body: reportPending.value.body }, false)
    if (!disposed && props.game.state.session?.principal_id === principal) {
      reportPending.value = null; reportReason.value = ''; reportNotice.value = `通報を受け付けました。受付番号: ${result.report_id}`
    }
  } catch (e) {
    if (!disposed) {
      const rejected = e instanceof HttpError && e.status >= 400 && e.status < 500 && ![408, 429].includes(e.status)
      if (rejected) reportPending.value = null
      reportNotice.value = rejected ? '通報が拒否されました。入力内容とログイン状態を確認してください。' : '通報の受付を確認できませんでした。同じ内容を再送できます。'
    }
  }
  finally { if (!disposed) reportBusy.value = false }
}
onMounted(() => { void search() })
onUnmounted(() => { disposed = true; ++generation; ++detailGeneration; name.value = ''; usage.value = null; reportReason.value = ''; reportPending.value = null })
</script>

<template>
  <section class="community-page" aria-label="公開作品">
    <h1>公開作品を探す</h1>
    <form class="discovery-form" @submit.prevent="search()">
      <label for="story-search">作品名・紹介文<input id="story-search" v-model="q" maxlength="100" type="search"></label>
      <label for="story-tag">タグ<input id="story-tag" v-model="tag" maxlength="80"></label>
      <button class="primary" :disabled="loading">検索する</button>
    </form>
    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <p v-if="loading" role="status">作品を読み込んでいます…</p>
    <p v-if="!loading && !stories.length" class="hint">該当する公開作品はありません。</p>
    <ul class="discovery-list"><li v-for="story in stories" :key="story.story_id">
      <h2>{{ story.metadata.title }}</h2><p>{{ story.metadata.synopsis }}</p>
      <p class="hint">第{{ story.release_number }}版 · {{ story.metadata.tags.join('、') }}</p>
      <button :data-public-story="story.story_id" @click="open(story)">作品の詳細</button>
    </li></ul>
    <button v-if="cursor" :disabled="loading" @click="search(true)">次の作品を読み込む</button>

    <article v-if="detail" class="notice" aria-label="公開作品の詳細">
      <h2>{{ detail.metadata.title }}</h2><p>{{ detail.metadata.synopsis }}</p><p>第{{ detail.release_number }}版</p>
      <p v-for="warning in detail.metadata.content_warnings" :key="warning">注意: {{ warning }}</p>
      <p class="hint">{{ detail.metadata.tags.join('、') }}</p>
      <p><a :href="`/?story=${encodeURIComponent(detail.story_id)}`">この作品の共有リンク</a></p>
      <button v-if="game.state.auth === 'ready'" class="primary" @click="emit('select', detail)">この作品で冒険を始める</button>
      <button v-else class="primary" @click="emit('login', detail.story_id)">ログインしてこの作品を遊ぶ</button>
      <template v-if="game.state.auth === 'ready'">
        <button class="quiet" :aria-expanded="reportOpen" @click="reportOpen = !reportOpen">この作品を通報する</button>
        <form v-if="reportOpen" @submit.prevent="report()">
          <label for="report-reason">通報の理由</label><textarea id="report-reason" v-model="reportReason" maxlength="2000" rows="3" :disabled="reportBusy || !!reportPending" required />
          <p class="hint">理由と作品IDを運営へ送信します。冒険の会話や原稿は添付されません。</p>
          <button :disabled="reportBusy || (!reportPending && !reportReason.trim())">{{ reportPending ? '同じ通報を再送' : '通報を送信' }}</button>
          <p v-if="reportNotice" role="status">{{ reportNotice }}</p>
        </form>
      </template>
    </article>

    <details v-if="game.state.auth === 'ready'" class="account-section"><summary>自分の表示名・利用量</summary>
      <button :disabled="accountBusy" @click="account()">アカウント情報を取得</button>
      <p v-if="accountError" class="error" role="alert">{{ accountError }}</p><p v-if="accountNotice" role="status">{{ accountNotice }}</p>
      <form v-if="accountLoaded" @submit.prevent="saveProfile()">
        <label for="profile-name">表示名</label><input id="profile-name" v-model="name" maxlength="40" required :disabled="accountBusy">
        <button :disabled="accountBusy || !name.trim()">表示名を保存</button>
      </form>
      <section v-if="usage" aria-label="自分の利用量"><h2>{{ usage.day }} の利用量</h2>
        <p v-if="usage.paused" class="notice">現在、新しいAI処理の受付が停止されています。</p>
        <dl><template v-for="(limit, key) in usage.limits" :key="key"><dt>{{ labels[key] ?? key }}</dt><dd>{{ usage.counts[key] ?? 0 }} / {{ limit }}</dd></template></dl>
      </section>
    </details>
  </section>
</template>

<style scoped>
.community-page { min-width: 0; padding: 2rem; width: 100%; max-width: 1100px; margin-inline: auto; }
.discovery-form { display: flex; flex-wrap: wrap; align-items: flex-end; gap: 1rem; }.discovery-form label { flex: 1 1 220px; margin: 0; }
.discovery-list { list-style: none; padding: 0; display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 270px), 1fr)); gap: 1rem; }
.discovery-list li { padding: 1rem; border: 1px solid var(--line); min-width: 0; }
.account-section { border-top: 1px solid var(--line); margin-top: 2rem; padding-top: 1rem; }.account-section summary { cursor: pointer; margin-bottom: 1rem; color: var(--brass); }
dl { display: grid; grid-template-columns: 2fr 1fr; }dd { margin: 0; }
form button { margin-top: .75rem; }
</style>
