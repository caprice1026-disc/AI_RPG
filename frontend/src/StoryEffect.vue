<script setup lang="ts">
import StoryFields, { type Choice } from './StoryFields.vue'
import { node, rows, type DraftNode } from './story-contracts'
defineProps<{ value: DraftNode; path: string; scenes: Choice[]; flags: Choice[]; endings: Choice[] }>()
</script>
<template>
  <StoryFields :value="value" :path="path" :fields="[
    { key: 'next_scene_ref', label: '移動先', type: 'select', options: scenes, nullable: true },
    { key: 'ending_ref', label: '到達する結末', type: 'select', options: endings, nullable: true },
    { key: 'add_flags', label: '成立する条件', type: 'multi', options: flags },
  ]" />
  <p class="hint">移動先と結末はどちらか一方を指定します。条件は複数選択できます。</p>
  <details v-if="value.ending_ref || rows(value.overrides).length">
    <summary>条件による結末の変更</summary>
    <div v-for="(override, i) in rows(value.overrides)" :key="i" class="author-row">
      <StoryFields :value="node(override)" :path="`${path}/overrides/${i}`" :fields="[
        { key: 'requires_flags', label: '必要な条件', type: 'multi', options: flags },
        { key: 'ending_ref', label: '変更先の結末', type: 'select', options: endings },
      ]" />
      <button type="button" class="quiet" @click="rows(value.overrides).splice(i, 1)">分岐を削除</button>
    </div>
    <button type="button" @click="value.overrides = [...rows(value.overrides), { requires_flags: [], ending_ref: '' }]">結末の分岐を追加</button>
  </details>
</template>
