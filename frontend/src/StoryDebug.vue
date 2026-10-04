<script setup lang="ts">
import { onUnmounted, ref, watch } from 'vue'
import type { Game } from './game'
import { node, type DraftNode, type StoryPlaytest } from './story-contracts'
const props = defineProps<{ game: Game; storyId: string; playtest: StoryPlaytest }>()
interface Debug { campaign_id: string; story_version_id: string; current_scene_ref: string; status: string; flags: string[]; actions: DraftNode[]; endings: DraftNode[] }
const result = ref<Debug | null>(null), error = ref(''), busy = ref(false)
let generation = 0, disposed = false
watch(() => [props.storyId, props.playtest.campaign_id], () => { ++generation; result.value = null; error.value = ''; busy.value = false })
const flags = (value: unknown) => Array.isArray(value) && value.length ? value.join('、') : 'なし'
async function refresh() {
  if (busy.value) return
  const g = ++generation, id = props.playtest.campaign_id, version = props.playtest.story_version_id
  const principal = props.game.state.session?.principal_id
  busy.value = true; error.value = ''
  try {
    const value = await props.game.request<Debug>(`/stories/${encodeURIComponent(props.storyId)}/playtests/${encodeURIComponent(id)}/debug`, {}, false)
    if (disposed || g !== generation || props.game.state.auth !== 'ready' || principal !== props.game.state.session?.principal_id) return
    if (value.campaign_id !== id || value.story_version_id !== version) throw new Error('Snapshot mismatch')
    result.value = value
  } catch { if (!disposed && g === generation) error.value = '作者用の試遊情報を取得できませんでした。試遊の権限と保存状態を確認してください。' }
  finally { if (!disposed && g === generation) busy.value = false }
}
onUnmounted(() => { disposed = true; ++generation; result.value = null })
</script>
<template>
  <details class="author-debug"><summary>作者用の試遊情報</summary>
    <p class="hint">この画面で最後に作成した試遊の、読み取り専用の情報です。原稿やプレイ状態は変更しません。</p>
    <button :disabled="busy" @click="refresh()">試遊情報を再取得</button>
    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <template v-if="result">
      <p>試遊版: {{ result.story_version_id }} · {{ result.status === 'completed' ? '完了' : '進行中' }}</p>
      <p>現在地: {{ result.current_scene_ref }}</p><p>成立した条件: {{ flags(result.flags) }}</p>
      <h3>現在地の行動</h3><ul><li v-for="(action, i) in result.actions" :key="i">
        {{ node(action.definition).label }}<p class="hint">不足する条件: {{ flags(action.missing_flags) }} / 行動を妨げる条件: {{ flags(action.blocking_flags) }}</p>
      </li></ul>
      <h3>結末の条件</h3><ul><li v-for="(ending, i) in result.endings" :key="i">
        {{ node(ending.definition).title }}<p>{{ node(ending.definition).summary }}</p><p class="hint">不足する条件: {{ flags(ending.missing_flags) }}</p>
      </li></ul>
    </template>
  </details>
</template>
