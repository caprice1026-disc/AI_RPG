<script setup lang="ts">
import { computed, onActivated, onDeactivated, onUnmounted, ref, watch } from 'vue'
import { HttpError, type Game } from './game'
import type { Stories } from './stories'
import StoryFields from './StoryFields.vue'
import { clone, node, type StoryDraft } from './story-contracts'
import type { AuthoringJob, StoryOutline } from './story-ai-contracts'
const props = defineProps<{ editor: Stories; game: Game }>()
const s = props.editor.state
const job = ref<AuthoringJob | null>(null), outline = ref<StoryOutline | null>(null), outlineBaseline = ref('')
const jobs = ref<AuthoringJob[]>([]), loadingHistory = ref(false), historyError = ref('')
const instructions = ref(''), selected = ref<string[]>([]), error = ref(''), notice = ref(''), reading = ref(false)
type Operation = { path: string; method: string; body: string; kind: 'job' | 'apply'; storyId: string }
const pending = ref<Operation | null>(null)
let disposed = false, visible = true, generation = 0, reads = 0, timer: ReturnType<typeof setTimeout> | undefined
const outlineDirty = computed(() => !!outline.value && JSON.stringify(outline.value) !== outlineBaseline.value)
const running = computed(() => !!job.value && ['queued', 'running'].includes(job.value.state))
const stale = computed(() => !!job.value && (props.editor.dirty.value || job.value.base_revision !== s.current?.revision))
const locked = computed(() => s.busy || !!s.pendingOperation || !!pending.value || loadingHistory.value)
const statuses = { queued: '順番待ち', running: '生成中', succeeded: '完了', failed: '失敗', cancelled: 'キャンセル済み' }
const kinds = { check: '整合性チェック', fill: '不足部分の補完', outline: '構成案の作成', concretize: '構成案の具体化' }
const names: Record<string, string> = { metadata: '公開情報', title: '名称', synopsis: '紹介文', scenario: '冒険', world: '世界', objective: '目的', scenes: '場所',
  description: '説明', flags: '条件', public_fact: '事実', endings: '結末', summary: '内容', actions: '行動', initialization: '初期状態', characters: '人物', items: 'アイテム', label: '名前' }
function fieldLabel(path: string) {
  let value: unknown = s.current?.draft
  return path.split('/').filter(Boolean).map(raw => {
    const part = raw.replace(/~1/g, '/').replace(/~0/g, '~')
    if (part.startsWith('@') && Array.isArray(value)) {
      const [key, ref] = part.slice(1).split('='); value = value.find(row => node(row)[key!] === ref)
      const row = node(value); return String(row.title || row.label || row.public_fact || '対象の項目')
    }
    value = node(value)[part]; return names[part] ?? part
  }).join(' / ')
}
function valueText(value: unknown): string {
  if (value === null || value === undefined) return 'なし'
  if (Array.isArray(value)) return value.length ? value.map(valueText).join('\n') : 'なし'
  if (typeof value === 'object') return Object.entries(node(value)).map(([key, child]) => `${names[key] ?? key}: ${valueText(child)}`).join('\n')
  return String(value)
}
watch(outlineDirty, value => { s.externalDirty = value }, { flush: 'sync' })
watch(pending, value => { s.externalPending = !!value }, { flush: 'sync' })
watch(() => s.current?.story_id, () => {
  ++generation; clearTimeout(timer); job.value = null; outline.value = null; outlineBaseline.value = ''
  pending.value = null; selected.value = []; instructions.value = ''; error.value = notice.value = ''; reading.value = false
  jobs.value = []; loadingHistory.value = false; historyError.value = ''; reads = 0
  void loadHistory()
}, { immediate: true })
function validContext() {
  const id = s.current?.story_id, principal = props.game.state.session?.principal_id
  return () => !disposed && id === s.current?.story_id && props.game.state.auth === 'ready' && principal === props.game.state.session?.principal_id
}
function accept(result: AuthoringJob, forceOutline = false) {
  if (result.story_id !== s.current?.story_id) return
  const index = jobs.value.findIndex(item => item.id === result.id)
  if (index >= 0) jobs.value[index] = result
  else jobs.value = [result, ...jobs.value].slice(0, 20)
  const different = job.value?.id !== result.id
  job.value = result
  if (different) { selected.value = []; outline.value = null; outlineBaseline.value = '' }
  if (result.outline && (forceOutline || different || !outlineDirty.value)) {
    outline.value = clone(result.outline); outlineBaseline.value = JSON.stringify(result.outline)
  }
  clearTimeout(timer)
  if (running.value && visible) {
    if (++reads <= 120) timer = setTimeout(() => { void refresh() }, 2000)
    else notice.value = '生成は継続中です。状態を再取得して確認してください。'
  }
}
async function loadHistory() {
  if (!s.current || pending.value || loadingHistory.value) return
  const valid = validContext(), g = ++generation, storyId = s.current.story_id
  clearTimeout(timer); reading.value = false; loadingHistory.value = true; historyError.value = ''; reads = 0
  try {
    const result = await props.game.request<{ jobs: AuthoringJob[] }>(`/stories/${encodeURIComponent(storyId)}/authoring-jobs`, {}, false)
    if (!valid() || g !== generation) return
    jobs.value = result.jobs.filter(item => item.story_id === storyId)
    const restored = jobs.value.find(item => item.id === job.value?.id) ?? job.value ?? jobs.value[0]
    if (restored) accept(restored)
  } catch {
    if (valid() && g === generation) {
      historyError.value = 'AI処理の履歴を取得できませんでした。「AI履歴を更新」で再試行できます。'
      if (job.value) accept(job.value)
    }
  } finally { if (valid() && g === generation) loadingHistory.value = false }
}
function chooseJob(event: Event) {
  const select = event.target as HTMLSelectElement, next = jobs.value.find(item => item.id === select.value)
  if (!next || next.id === job.value?.id) return
  if (locked.value || (outlineDirty.value && !window.confirm('未保存の構成案を破棄して、別のAI処理を表示しますか？'))) {
    select.value = job.value?.id ?? ''; return
  }
  ++generation; reading.value = false; reads = 0; error.value = notice.value = ''
  accept(next); void refresh()
}
async function refresh() {
  if (!job.value || reading.value || loadingHistory.value || pending.value) return
  const valid = validContext(), g = ++generation, id = job.value.id
  reading.value = true; error.value = ''
  try {
    const result = await props.game.request<AuthoringJob>(`/authoring-jobs/${encodeURIComponent(id)}`, {}, false)
    if (valid() && g === generation && job.value?.id === id && result.id === id) accept(result)
  } catch { if (valid() && g === generation) error.value = 'AI処理の状態を取得できませんでした。状態を再取得してください。' }
  finally { if (valid() && g === generation) reading.value = false }
}
async function send() {
  if (!pending.value || s.busy) return
  const request = pending.value, valid = validContext(); ++generation; clearTimeout(timer); reading.value = false
  s.busy = true; error.value = ''; notice.value = ''
  try {
    const result = await props.game.request<AuthoringJob | StoryDraft>(request.path, { method: request.method, body: request.body }, false)
    if (!valid()) return
    pending.value = null
    if (request.kind === 'apply') {
      props.editor.acceptDraft(result as StoryDraft)
      if (job.value?.proposal) {
        job.value.proposal.decision = 'applied'; job.value.proposal.applied_revision = (result as StoryDraft).revision
      }
      selected.value = []; notice.value = '選択した提案を新しい下書きとして保存しました。検証・試遊して確認してください。'
    } else { reads = 0; accept(result as AuthoringJob, true) }
  } catch (e) {
    if (valid()) {
      const rejected = e instanceof HttpError && e.status >= 400 && e.status < 500 && ![408, 429].includes(e.status)
      if (rejected) pending.value = null
      error.value = e instanceof HttpError && e.status === 409 ? '対象の版が変わりました。現在の入力を保持しています。原稿や構成案の状態を確認してください。'
        : rejected ? 'AI操作を受け付けられませんでした。入力、利用上限、権限を確認してください。'
          : 'AI操作の結果が未確認です。同じ要求を再試行してください。'
    }
  } finally { if (valid()) s.busy = false }
}
async function mutate(path: string, body: Record<string, unknown>, method = 'POST', kind: Operation['kind'] = 'job') {
  if (locked.value || !s.current) return
  pending.value = { path, method, body: JSON.stringify(body), kind, storyId: s.current.story_id }; await send()
}
async function start(kind: AuthoringJob['kind']) {
  if (locked.value || running.value || !s.current) return
  if (outlineDirty.value && !window.confirm('未保存の構成案があります。新しいAI処理を開始しますか？')) return
  if (!await props.editor.flushSave()) return
  if (kind === 'concretize' && (stale.value || outlineDirty.value || !job.value?.approved_outline_revision
    || job.value.approved_outline_revision !== job.value.outline_revision)) return
  await mutate(`/stories/${encodeURIComponent(s.current.story_id)}/authoring-jobs`, {
    request_id: crypto.randomUUID(), base_revision: s.current.revision, kind, instructions: instructions.value,
    ...(kind === 'concretize' ? { outline_job_id: job.value!.id, approved_outline_revision: job.value!.approved_outline_revision } : {}),
  })
}
async function saveOutline() {
  if (!job.value || !outline.value || !outlineDirty.value) return
  await mutate(`/authoring-jobs/${encodeURIComponent(job.value.id)}/outline`, {
    request_id: crypto.randomUUID(), expected_outline_revision: job.value.outline_revision, outline: clone(outline.value),
  }, 'PUT')
}
async function approve() {
  if (!job.value || outlineDirty.value || stale.value) return
  await mutate(`/authoring-jobs/${encodeURIComponent(job.value.id)}/outline/approve`, { request_id: crypto.randomUUID(), expected_outline_revision: job.value.outline_revision })
}
async function apply() {
  if (!job.value?.proposal || !selected.value.length || stale.value || locked.value || !await props.editor.flushSave() || stale.value) return
  await mutate(`/stories/${encodeURIComponent(s.current!.story_id)}/proposals/${encodeURIComponent(job.value.proposal.id)}/apply`, {
    request_id: crypto.randomUUID(), expected_revision: s.current!.revision, change_ids: [...selected.value],
  }, 'POST', 'apply')
}
onDeactivated(() => { visible = false; clearTimeout(timer) })
onActivated(() => { visible = true; void loadHistory() })
onUnmounted(() => { disposed = true; ++generation; clearTimeout(timer); s.externalDirty = s.externalPending = false })
</script>

<template>
  <section class="author-section" aria-label="AI作成補助">
    <h2>AI作成補助</h2><p class="hint">保存済みの原稿をもとに提案を作ります。自動保存でAIは呼び出されず、提案も選んで採用するまで原稿に反映されません。</p>
    <label for="ai-job-history">AI処理の履歴（最近20件）</label>
    <select id="ai-job-history" :value="job?.id ?? ''" :disabled="locked || !jobs.length" @change="chooseJob">
      <option value="" disabled>{{ jobs.length ? '履歴を選択' : '履歴はありません' }}</option>
      <option v-for="item in jobs" :key="item.id" :value="item.id">{{ kinds[item.kind] }} · {{ statuses[item.state] }} · 下書き {{ item.base_revision }} · {{ new Date(item.created_at).toLocaleString('ja-JP') }}</option>
    </select>
    <div class="ai-buttons"><button :disabled="locked" @click="loadHistory()">AI履歴を更新</button></div>
    <p v-if="loadingHistory" role="status">AI処理の履歴を読み込んでいます…</p>
    <p v-if="historyError" class="error" role="alert">{{ historyError }}</p>
    <label for="ai-instructions">AIに伝える条件・相談したいこと</label><textarea id="ai-instructions" v-model="instructions" maxlength="8000" rows="3" :disabled="locked" />
    <div class="ai-buttons"><button :disabled="locked || running" @click="start('check')">整合性をチェック</button>
      <button :disabled="locked || running" @click="start('fill')">不足部分の補完を提案</button>
      <button :disabled="locked || running" @click="start('outline')">条件から構成案を作る</button></div>
    <p v-if="error" class="error" role="alert">{{ error }}</p><p v-if="notice" role="status">{{ notice }}</p>
    <p v-if="pending && s.busy" role="status">AI操作中です。応答を待っています…</p>
    <button v-else-if="pending" @click="send()">同じAI要求を再試行</button>
    <template v-if="job">
      <p role="status">{{ kinds[job.kind] }}: {{ statuses[job.state] }} · 元の下書き {{ job.base_revision }}</p>
      <p v-if="job.state === 'failed'" class="error">AI処理に失敗しました。{{ job.error_code || '時間をおいて、新しい依頼として再試行してください。' }}</p>
      <p v-if="stale" class="notice">生成元から原稿が変わっています。古い提案は採用できません。現在の原稿で作り直すか、内容を参考に手動で編集してください。</p>
      <div class="ai-buttons"><button :disabled="reading || locked" @click="reads = 0; refresh()">AIの状態を再取得</button>
        <button v-if="running" :disabled="locked" @click="mutate(`/authoring-jobs/${encodeURIComponent(job.id)}/cancel`, {})">AI処理をキャンセル</button></div>
      <p class="hint">AI呼出 {{ job.physical_requests }} 回 · 入力 {{ job.input_tokens }} / 出力 {{ job.output_tokens }} トークン{{ job.usage_complete ? '' : '（利用量は未確定）' }}</p>

      <fieldset v-if="job.kind === 'outline' && outline" class="outline-form" :disabled="locked || job.state !== 'succeeded'">
        <legend>構成案・第{{ job.outline_revision }}版</legend>
        <StoryFields :value="node(outline)" path="/ai-outline" :fields="[
          { key: 'title', label: '構成案のタイトル' }, { key: 'premise', label: 'あらすじ・前提', type: 'long' },
          { key: 'scenes', label: '場所と展開（1行に1つ）', type: 'lines' }, { key: 'characters', label: '登場人物（1行に1人）', type: 'lines' },
          { key: 'endings', label: '結末案（1行に1つ）', type: 'lines' }, { key: 'undecided', label: 'まだ決めないこと（1行に1つ）', type: 'lines' },
        ]" />
        <p role="status">{{ outlineDirty ? '構成案に未保存の変更があります。' : job.approved_outline_revision === job.outline_revision ? 'この構成案は承認済みです。' : '構成案は未承認です。' }}</p>
        <div class="ai-buttons"><button :disabled="!outlineDirty" @click="saveOutline()">構成案の修正を保存</button>
          <button :disabled="outlineDirty || stale || job.approved_outline_revision === job.outline_revision" @click="approve()">この構成案を承認</button>
          <button :disabled="outlineDirty || stale || !job.approved_outline_revision || job.approved_outline_revision !== job.outline_revision" @click="start('concretize')">承認した構成案を具体化</button></div>
      </fieldset>

      <section v-if="job.proposal" aria-label="AIの変更提案">
        <h3>原稿への変更提案</h3>
        <p v-if="job.proposal.decision === 'applied'" role="status">下書き {{ job.proposal.applied_revision }} に採用済みです。</p>
        <p v-if="!job.proposal.changes.length" class="hint">採用できる変更提案はありません。</p>
        <ul><li v-for="(finding, i) in job.proposal.findings" :key="i">{{ finding.severity === 'error' ? 'エラー' : '指摘' }}: {{ finding.message }} <small>{{ finding.code }}</small></li></ul>
        <article v-for="change in job.proposal.changes" :key="change.id" class="proposal-change">
          <label class="check-label"><input v-model="selected" type="checkbox" :value="change.id" :disabled="locked || stale || job.proposal.decision === 'applied'">
            {{ fieldLabel(change.field_path) }} {{ change.operation === 'remove' ? 'を削除' : 'を変更' }}{{ change.policy === 'undecided' ? '（未決定の項目・採用には作者の判断が必要）' : '' }}
          </label>
          <div class="proposal-values"><div><h4>変更前</h4><p>{{ change.before_exists ? valueText(change.before) : '未入力' }}</p></div><div><h4>変更後</h4><p>{{ change.operation === 'remove' ? '削除' : valueText(change.after) }}</p></div></div>
          <p>理由: {{ change.reason }}</p>
        </article>
        <details><summary>提案内容の検証結果</summary>
          <p>エラー {{ job.proposal.validation.errors.length }} 件、警告 {{ job.proposal.validation.warnings.length }} 件</p>
          <ul><li v-for="(finding, i) in [...job.proposal.validation.errors, ...job.proposal.validation.warnings]" :key="i">{{ finding.message }}（{{ finding.code }}）</li></ul>
          <p class="hint">一部だけ採用した原稿は、採用後にもう一度検証してください。</p>
        </details>
        <button :disabled="locked || stale || !selected.length || job.proposal.decision === 'applied'" @click="apply()">選択した提案を採用</button>
      </section>
    </template>
  </section>
</template>

<style scoped>
.ai-buttons { display: flex; flex-wrap: wrap; gap: .7rem; margin: 1rem 0; }.outline-form { border: 1px solid var(--line); padding: 1rem; min-width: 0; }
.proposal-change { border: 1px solid var(--line); padding: 1rem; margin: 1rem 0; }.proposal-values { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 220px), 1fr)); gap: 1rem; }
.proposal-values p { white-space: pre-wrap; }.check-label { display: flex; align-items: flex-start; gap: .7rem; }.check-label input { width: 20px; height: 20px; flex: 0 0 20px; }
</style>
