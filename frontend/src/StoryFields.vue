<script setup lang="ts">
import type { DraftNode } from './story-contracts'
export interface Choice { value: string; label: string }
export interface AuthorField {
  key: string; label: string; type?: 'text' | 'long' | 'lines' | 'number' | 'select' | 'multi' | 'check'
  options?: Choice[]; nullable?: boolean; min?: number; max?: number; hint?: string
}
const props = defineProps<{ value: DraftNode; fields: AuthorField[]; path: string }>()
function input(field: AuthorField, event: Event) {
  const target = event.target as HTMLInputElement & HTMLSelectElement
  props.value[field.key] = field.type === 'check' ? target.checked
    : field.type === 'multi' ? Array.from(target.selectedOptions).map(o => o.value)
      : field.type === 'number' ? target.value === '' ? null : Number(target.value)
        : field.type === 'lines' ? target.value.split('\n').filter(line => line.trim())
          : field.nullable && target.value === '' ? null : target.value
}
function text(field: AuthorField) {
  const value = props.value[field.key]
  return field.type === 'lines' && Array.isArray(value) ? value.join('\n') : String(value ?? '')
}
function values(field: AuthorField): unknown[] { const value = props.value[field.key]; return Array.isArray(value) ? value : [] }
</script>

<template>
  <div class="author-fields">
    <div v-for="field in fields" :key="field.key" class="author-field">
      <label :for="`${path}/${field.key}`" :class="{ 'check-label': field.type === 'check' }">
        <input v-if="field.type === 'check'" :id="`${path}/${field.key}`" type="checkbox" :checked="!!value[field.key]" @change="input(field, $event)">
        {{ field.label }}
      </label>
      <textarea v-if="field.type === 'long' || field.type === 'lines'" :id="`${path}/${field.key}`" :value="text(field)" rows="3" @input="input(field, $event)" />
      <select v-else-if="field.type === 'select' || field.type === 'multi'" :id="`${path}/${field.key}`" :multiple="field.type === 'multi'" @change="input(field, $event)">
        <option v-if="field.type === 'select'" value="" :selected="!value[field.key]">{{ field.nullable ? '指定なし' : '選択してください' }}</option>
        <option v-for="option in field.options" :key="option.value" :value="option.value"
          :selected="field.type === 'multi' ? values(field).includes(option.value) : value[field.key] === option.value">{{ option.label }}</option>
      </select>
      <input v-else-if="field.type !== 'check'" :id="`${path}/${field.key}`" :type="field.type === 'number' ? 'number' : 'text'"
        :value="text(field)" :min="field.min" :max="field.max" @input="input(field, $event)">
      <small v-if="field.hint" class="hint">{{ field.hint }}</small>
    </div>
  </div>
</template>
