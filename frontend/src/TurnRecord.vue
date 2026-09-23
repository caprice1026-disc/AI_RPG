<script setup lang="ts">
import { computed } from 'vue'
import type { Result, StoryRecord } from './contracts'
const props = defineProps<{ record: StoryRecord; names: Record<string, string>; enabled: boolean; hiddenChoiceLabels?: string[] }>()
defineEmits<{ choice: [id: string, label: string] }>()
const choices = computed(() => props.record.turn.choices.filter(choice => !props.hiddenChoiceLabels?.includes(choice.label)))
const reasons = { target_unavailable: '対象を見つけられませんでした。', resource_unavailable: '必要な持ち物がありません。', rule_precondition: '今はその行動を実行できません。' }
function lines(result: Result): string[] {
  return result.kind === 'not_applicable' ? [reasons[result.reason]] : [
    ...result.facts, ...result.dice.map(d => `${d.expression}: ${d.rolls.join(', ')} ${d.modifier >= 0 ? '+' : ''}${d.modifier} = ${d.total}`),
  ]
}
function humanize(text: string, names: Record<string, string>) {
  return text.replace(/@[a-z][a-z0-9_]*/g, ref => names[ref.slice(1)] ?? ref)
}
</script>

<template>
  <li class="turn-record" :data-turn-id="record.turn.turn_id">
    <p v-if="record.player_input !== undefined" class="player-input"><span class="speaker">あなた</span>{{ record.player_input }}</p>
    <section v-if="record.turn.action_results.length" class="result-block" aria-label="行動の結果">
      <h3>行動の結果</h3>
      <template v-for="action in record.turn.action_results" :key="action.action_id">
        <p v-for="(line, i) in lines(action.result)" :key="i">{{ humanize(line, names) }}</p>
      </template>
    </section>
    <section v-if="record.turn.enemy_reactions.length" class="result-block enemy-reaction" aria-label="敵の反撃">
      <h3>敵の反撃</h3>
      <template v-for="reaction in record.turn.enemy_reactions" :key="reaction.reaction_id">
        <p v-for="(line, i) in lines(reaction.result)" :key="i">{{ humanize(line, names) }}</p>
      </template>
    </section>
    <p v-if="record.turn.narration" class="narration">{{ humanize(record.turn.narration, names) }}</p>
    <p v-if="record.turn.recovery.fallback" class="hint fallback">描写の生成に問題があり、保存済みの結果を表示しています。判定と状態は保存済みです。行動をやり直す必要はありません。</p>
    <div v-if="choices.length" class="choices" aria-label="物語の選択肢">
      <button v-for="choice in choices" :key="choice.id" :disabled="!enabled"
        @click="$emit('choice', choice.id, choice.label)">{{ humanize(choice.label, names) }}</button>
    </div>
  </li>
</template>
