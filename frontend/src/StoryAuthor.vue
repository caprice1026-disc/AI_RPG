<script setup lang="ts">
import { computed, onActivated, ref, watch } from 'vue'
import StoryDraftForm from './StoryDraftForm.vue'
import StoryAI from './StoryAI.vue'
import StoryDebug from './StoryDebug.vue'
import type { Game } from './game'
import { capabilityNames, node, type Lifecycle, type Visibility } from './story-contracts'
import type { Stories } from './stories'
const props = defineProps<{ editor: Stories; game: Game }>()
const s = props.editor.state
const { dirty, reportCurrent, canPublish } = props.editor
const current = computed(() => s.stories.find(story => story.story_id === s.current?.story_id))
const visibility = ref<Visibility>('private'), lifecycle = ref<Exclude<Lifecycle, 'blocked'>>('active')
watch(current, value => { if (value) { visibility.value = value.visibility; lifecycle.value = value.lifecycle === 'blocked' ? 'withdrawn' : value.lifecycle } }, { immediate: true })
const visibilityLabels = { private: '自分のみ', unlisted: 'URLで限定共有', public: '一般公開' }
const lifecycleLabels = { active: '有効', withdrawn: '公開停止', blocked: '運営による停止', archived: 'アーカイブ' }
const sectionNames: Record<string, string> = { metadata: '基本情報・公開紹介', world: '世界・目的', scenes: '場所・行動・分岐',
  initialization: '人物・アイテム・配置', flags: '進行条件', endings: '結末', field_policies: 'AI裁量' }
const sectionLabel = (section: string) => Object.hasOwn(sectionNames, section) ? sectionNames[section] : section
const status = computed(() => ({ saved: '保存済み', waiting: '未保存・入力から2秒後に保存', saving: '保存中…', failed: '保存失敗・入力を保持しています', conflict: '競合・入力を保持しています' })[s.saveStatus])
const locked = computed(() => s.busy || !!s.pendingOperation || s.externalPending)
const shareLink = computed(() => s.published ? `${location.origin}/?story=${encodeURIComponent(s.published.story_id)}` : '')
function flatten(value: unknown, path = '', result: Record<string, string> = {}) {
  if (value && typeof value === 'object') Object.entries(value).forEach(([key, child]) => flatten(child, `${path}/${key}`, result))
  else result[path] = value == null ? '未入力' : String(value)
  return result
}
const differences = computed(() => {
  if (!s.remote || !s.current) return []
  const local = flatten(s.current.draft), remote = flatten(s.remote.draft)
  return [...new Set([...Object.keys(local), ...Object.keys(remote)])].filter(key => local[key] !== remote[key])
    .map(path => ({ path, local: local[path] ?? 'なし', remote: remote[path] ?? 'なし' }))
})
const fieldNames: Record<string, string> = { metadata: '公開情報', title: '名称', synopsis: '紹介文', scenario: '冒険', world: '世界', objective: '目的', scenes: '場所',
  description: '説明', flags: '条件', public_fact: '公開する事実', endings: '結末', summary: '内容', actions: '行動', notes: '作者メモ', field_policies: 'AI裁量',
  initialization: '初期状態', characters: '人物', items: 'アイテム', label: '名前', protected_facts: '固定設定', statement: '設定の内容' }
const fieldLabel = (path: string) => path.split('/').filter(Boolean).map(p => fieldNames[p] ?? (/^\d+$/.test(p) ? `${Number(p) + 1}番目` : p)).join(' / ')
const coverageNames: Record<string, string> = { typed_definition: '定義の形式', registered_actions: '登録行動の経路', freeform: '自由行動', narrative_meaning: '文章の意味', states_explored: '調査した状態数', state_limit: '探索上限', reachable_scenes: '到達した場所', reachable_endings: '到達した結末', combat: '戦闘' }
const coverageValues: Record<string, string> = { not_verified: '未検証', not_run: '未実施', partial: '一部のみ検証', finite_flags: '有限の条件状態を検証', abstract_outcomes: '結果を抽象化して検証', not_applicable: '対象なし' }
function coverage(value: unknown) {
  if (Array.isArray(value)) return `${value.length} 件`
  if (typeof value === 'boolean') return value ? '確認済み' : '未確認'
  return coverageValues[String(value)] ?? String(value)
}
onActivated(() => { void props.editor.load() })
</script>

<template>
  <section class="author-page" aria-label="マイ作品">
    <header class="author-heading"><h1>マイ作品</h1><button :disabled="s.loading || locked" @click="editor.load()">作品一覧を更新</button></header>
    <p v-if="s.loading" role="status">作品を読み込んでいます…</p>
    <p v-if="s.error" class="error" role="alert">{{ s.error }}</p>
    <div v-if="s.pendingOperation" class="notice" role="status">
      <p>操作の結果が未確認です。同じ内容で再試行して結果を確認できます。</p>
      <button :disabled="s.busy" @click="editor.retryOperation()">同じ操作を再試行</button>
    </div>
    <details :open="!s.current" class="author-section"><summary>作品一覧・新しい作品</summary>
      <ul class="author-list">
        <li v-for="story in s.stories" :key="story.story_id">
          <button :data-story="story.story_id" :disabled="locked" @click="editor.open(story.story_id)">{{ story.metadata.title || 'タイトル未入力' }}</button>
          <small>{{ visibilityLabels[story.visibility] }} · {{ lifecycleLabels[story.lifecycle] }} · 下書き {{ story.revision }}</small>
        </li>
      </ul>
      <p v-if="!s.loading && !s.stories.length" class="hint">まだ作品はありません。空の原稿かテンプレートから作成してください。</p>
      <button :disabled="locked" @click="editor.create()">空の原稿から作る</button>
      <p class="hint">未完成でも保存できます。空の原稿では「AIに伝える条件」に作りたい物語を書き、構成案から始められます。試遊・公開の前に検証が必要です。</p>
      <h2>テンプレートから作る</h2>
      <div class="template-list">
        <article v-for="template in s.templates" :key="template.template_id" class="author-row">
          <h3>{{ template.title }}</h3><p>{{ template.description }}</p>
          <p class="hint">テンプレート 第{{ template.version }}版 · {{ template.required_capabilities.map(c => capabilityNames[c] ?? `未対応: ${c}`).join('、') }}</p>
          <p v-if="template.sections.length" class="hint">編集項目: {{ template.sections.map(sectionLabel).join('、') }}</p>
          <details v-if="template.questions?.length || template.recommended_structure?.length">
            <summary>作成のヒント</summary>
            <template v-if="template.questions?.length">
              <h4>考えておきたいこと</h4>
              <ul><li v-for="(question, i) in template.questions" :key="i">
                <p>{{ question.prompt }}</p><p v-if="question.hint" class="hint">{{ question.hint }}</p>
                <small>入力先: {{ sectionLabel(question.target_section) }}</small>
              </li></ul>
            </template>
            <template v-if="template.recommended_structure?.length">
              <h4>推奨構成</h4><ol><li v-for="(part, i) in template.recommended_structure" :key="i">{{ part }}</li></ol>
            </template>
          </details>
          <p v-if="template.required_capabilities.some(c => !capabilityNames[c])" class="error">この画面では未対応の機能を含むため作成できません。</p>
          <button :data-template="template.template_id" :disabled="locked || template.required_capabilities.some(c => !capabilityNames[c])"
            @click="editor.create(template.template_id)">このテンプレートで作成</button>
        </article>
      </div>
    </details>

    <template v-if="s.current">
      <header class="author-savebar">
        <h2>{{ s.current.draft.metadata.title || 'タイトル未入力' }}</h2>
        <p role="status" aria-live="polite">{{ status }} · 下書き {{ s.current.revision }}<small v-if="s.saveStatus === 'saved'"> · {{ new Date(s.current.updated_at).toLocaleString('ja-JP') }}</small></p>
        <button :disabled="locked || s.saveStatus === 'saving' || s.saveStatus === 'conflict' || (!dirty && s.saveStatus !== 'failed')" @click="editor.save()">{{ s.saveStatus === 'failed' && s.retryExact ? '同じ保存要求を再試行' : '今すぐ保存' }}</button>
      </header>
      <p v-if="s.saveError" class="error" role="alert">{{ s.saveError }}</p>
      <section v-if="s.saveStatus === 'conflict'" class="notice" aria-label="原稿の競合">
        <button :disabled="locked" @click="editor.compareRemote()">保存済みの原稿と比較</button>
        <template v-if="s.remote">
          <p>保存済み: 下書き {{ s.remote.revision }}。この画面の入力はまだ保存されていません。</p>
          <div class="comparison-scroll"><table><thead><tr><th>項目</th><th>この画面の入力</th><th>保存済み</th></tr></thead>
            <tbody><tr v-for="diff in differences" :key="diff.path"><th>{{ fieldLabel(diff.path) }}</th><td>{{ diff.local }}</td><td>{{ diff.remote }}</td></tr></tbody>
          </table></div>
          <button :disabled="locked" @click="editor.keepLocal()">比較した版にこの入力を保存</button>
          <button :disabled="locked" @click="editor.useRemote()">入力を破棄して保存済みを使用</button>
        </template>
      </section>

      <fieldset class="author-form" :disabled="locked"><legend class="visually-hidden">作品の原稿</legend>
        <StoryDraftForm :draft="s.current.draft" />
      </fieldset>
      <StoryAI :editor="editor" :game="game" />
      <details class="author-section"><summary>履歴・複製</summary>
        <div class="author-buttons"><button :disabled="locked" @click="editor.history()">保存履歴を読み込む</button><button :disabled="locked" @click="editor.duplicate()">この作品を複製</button></div>
        <p class="hint">復元は新しい下書きとして保存されます。現在の入力を保存できない場合は復元を開始しません。</p>
        <ol class="author-list"><li v-for="revision in s.revisions" :key="revision.revision">
          <span>下書き {{ revision.revision }} · {{ new Date(revision.updated_at).toLocaleString('ja-JP') }} · {{ revision.draft.metadata.title || 'タイトル未入力' }}</span>
          <button :disabled="locked || revision.revision === s.current.revision" @click="editor.restore(revision.revision)">この履歴を復元</button>
        </li></ol>
      </details>

      <section class="author-section" aria-label="検証と試遊"><h2>検証・試遊</h2>
        <p>保存した原稿から試遊用の版を作ります。後から原稿を編集しても進行中の試遊は変わりません。</p>
        <div class="author-buttons"><button :disabled="locked || s.saveStatus === 'conflict'" @click="editor.validate()">原稿を検証</button>
          <button :disabled="locked || s.saveStatus === 'conflict' || (reportCurrent && !!s.report?.errors.length)" @click="editor.playtest()">保存して最新原稿を試遊</button>
          <button v-if="s.playtest" :disabled="locked" @click="editor.resumePlaytest()">作成済みの試遊を開く</button>
        </div>
        <p v-if="s.playtest" class="hint">試遊版: {{ s.playtest.story_version_id }}</p>
        <StoryDebug v-if="s.playtest" :game="game" :story-id="s.current.story_id" :playtest="s.playtest" />
        <template v-if="s.report">
          <p role="status">下書き {{ s.report.draft_revision }} の検証結果: エラー {{ s.report.errors.length }} 件、警告 {{ s.report.warnings.length }} 件</p>
          <p v-if="!reportCurrent" class="error">原稿が変わったため、再検証が必要です。</p>
          <ul class="validation-findings"><li v-for="(finding, i) in [...s.report.errors, ...s.report.warnings]" :key="`${finding.code}-${i}`">
            <strong>{{ finding.severity === 'error' ? 'エラー' : '警告' }}</strong> {{ finding.message }}
            <small>{{ fieldLabel(finding.field_path) }} · {{ finding.code }}</small>
            <p v-if="finding.suggestion">修正案: {{ finding.suggestion }}</p>
          </li></ul>
          <h3>検証できた範囲</h3>
          <dl class="coverage"><template v-for="(value, key) in s.report.coverage" :key="key"><dt>{{ coverageNames[key] ?? key }}</dt><dd>{{ coverage(value) }}</dd></template></dl>
          <p class="hint">自由入力のすべての経路や、文章の意味・面白さを保証するものではありません。</p>
        </template>
        <p v-else class="hint">この原稿の検証はまだ実施していません。</p>
      </section>

      <section class="author-section" aria-label="公開設定"><h2>公開</h2>
        <p>最新の検証と、同じ内容で作者本人が結末まで試遊した記録が必要です。</p>
        <label for="publish-visibility">公開範囲</label><select id="publish-visibility" v-model="s.visibility" :disabled="locked">
          <option v-for="(label, value) in visibilityLabels" :key="value" :value="value">{{ label }}</option>
        </select>
        <p class="hint">URLで限定共有は一覧に掲載されません。URLを知るログイン済みの人が遊べます。</p>
        <label v-for="warning in [...new Map((s.report?.warnings ?? []).map(w => [w.code, w])).values()]" :key="warning.code" class="check-label">
          <input v-model="s.warningCodes" type="checkbox" :value="warning.code" :disabled="locked || !reportCurrent">警告を確認: {{ warning.message }}（{{ warning.code }}）
        </label>
        <label class="check-label"><input id="author-completion" v-model="s.acknowledged" type="checkbox" :disabled="locked || !reportCurrent">
          作者本人がこの内容を少なくとも1つの結末まで試遊し、公開する内容を確認しました。
        </label>
        <button class="primary" :disabled="!canPublish" @click="editor.publish()">確認した原稿を公開</button>
        <article v-if="s.published" class="notice" aria-label="公開された作品">
          <h3>{{ s.published.metadata.title }}</h3><p>{{ s.published.metadata.synopsis }}</p>
          <p>第{{ s.published.release_number }}版 · {{ visibilityLabels[s.published.visibility] }} · {{ lifecycleLabels[s.published.lifecycle] }}</p>
          <p class="hint">公開版: {{ s.published.story_version_id }}</p>
          <a :href="shareLink" target="_blank" rel="noopener">作品の公開詳細を開く</a>
          <label for="story-share-link">共有リンク</label><input id="story-share-link" :value="shareLink" readonly @focus="($event.target as HTMLInputElement).select()">
        </article>
        <template v-if="current">
          <h3>作品の設定</h3><p>現在: {{ visibilityLabels[current.visibility] }} · {{ lifecycleLabels[current.lifecycle] }}</p>
          <p v-if="current.lifecycle === 'blocked'" class="error">運営による停止中です。作者画面から解除はできません。</p>
          <fieldset v-else class="author-form" :disabled="locked"><legend class="visually-hidden">公開範囲と状態</legend>
            <label for="story-visibility">作品の公開範囲</label><select id="story-visibility" v-model="visibility"><option v-for="(label, value) in visibilityLabels" :key="value" :value="value">{{ label }}</option></select>
            <label for="story-lifecycle">作品の状態</label><select id="story-lifecycle" v-model="lifecycle"><option value="active">有効</option><option value="withdrawn">公開停止</option><option value="archived">アーカイブ</option></select>
            <div class="author-buttons"><button @click="editor.settings(visibility, lifecycle)">設定を保存</button><button :disabled="current.lifecycle !== 'active'" @click="editor.settings(current.visibility, 'withdrawn')">公開を停止</button></div>
          </fieldset>
          <p class="hint">公開停止は新規プレイを止めます。開始済みの冒険はその版で続けられます。</p>
        </template>
      </section>
    </template>
  </section>
</template>

<style scoped>
.author-page { min-width: 0; padding: 1.5rem clamp(1rem, 3vw, 3rem); max-width: 1100px; width: 100%; }
.author-heading, .author-buttons { display: flex; flex-wrap: wrap; gap: .8rem; align-items: center; margin-bottom: 1rem; }
.author-heading h1 { flex: 1; }
.author-savebar { border-bottom: 1px solid var(--brass); padding-bottom: 1rem; margin-bottom: 1rem; }
.author-list { padding: 0; list-style: none; }.author-list li { display: flex; flex-wrap: wrap; gap: .6rem; align-items: center; margin-block: .7rem; }
.author-list small { color: var(--muted); }.template-list { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 250px), 1fr)); gap: 1rem; }
.author-form { border: 0; padding: 0; margin: 0; min-width: 0; }
.author-page :deep(.author-section) { padding: 1rem 0; border-bottom: 1px solid var(--line); }
.author-page :deep(summary) { cursor: pointer; padding: .5rem 0; color: var(--brass); }
.author-page :deep(.author-row) { border: 1px solid var(--line); padding: 1rem; margin-block: .8rem; border-radius: 4px; min-width: 0; }
.author-page :deep(.author-action) { border-left: 2px solid var(--line); margin: 1rem 0; padding-left: 1rem; }
.author-page :deep(.author-fields) { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 270px), 1fr)); gap: 0 1rem; }
.author-page :deep(.author-field) { min-width: 0; }.author-page :deep(.check-label) { display: flex; gap: .7rem; align-items: flex-start; }
.author-page :deep(input[type=checkbox]) { width: 20px; height: 20px; flex: 0 0 20px; margin-top: .25rem; }
.author-page :deep(select[multiple]) { min-height: 110px; }.author-page :deep(.policy-row) { margin-bottom: .6rem; }
.comparison-scroll { overflow: auto; }table { border-collapse: collapse; width: 100%; margin: 1rem 0; }th, td { padding: .5rem; border: 1px solid var(--line); text-align: left; white-space: pre-wrap; overflow-wrap: anywhere; }
.validation-findings { padding-left: 1.3rem; }.validation-findings small { display: block; color: var(--muted); }
.coverage { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 2fr); }.coverage dd { margin: 0; }
</style>
