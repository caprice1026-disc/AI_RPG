<script setup lang="ts">
import { reactive, ref } from 'vue'
const fields = [
  { id: 'lost', label: '何をすればよいか迷った場面' },
  { id: 'different', label: '入力と違う結果になった場面' },
  { id: 'waiting', label: '待ち時間や使いにくさ' },
  { id: 'comment', label: '全体の感想' },
]
const values = reactive<Record<string, string>>({ lost: '', different: '', waiting: '', comment: '' })
const message = ref('')
function download() {
  const text = 'AI RPG プレイの感想\n\n' + fields.map(f => `${f.label}\n${values[f.id] || '（未記入）'}`).join('\n\n') + '\n'
  const url = URL.createObjectURL(new Blob(['\uFEFF', text], { type: 'text/plain;charset=utf-8' }))
  const link = document.createElement('a')
  link.href = url; link.download = 'ai-rpg-feedback.txt'; document.body.append(link); link.click(); link.remove()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
  message.value = '感想ファイルを作成しました。ダウンロードしたファイルを確認して、主催者に共有できます。'
}
</script>
<template>
  <details class="feedback">
    <summary>プレイの感想を残す <span class="hint">任意</span></summary>
    <p class="hint">書ける項目だけで構いません。ダウンロードしたファイルを主催者に共有できます。自動送信されません。冒険の履歴やアカウント情報は添付されません。</p>
    <form @submit.prevent="download">
      <label v-for="field in fields" :key="field.id" :for="`feedback-${field.id}`">{{ field.label }}
        <textarea :id="`feedback-${field.id}`" v-model="values[field.id]" rows="2" maxlength="4000" />
      </label>
      <button type="submit">感想をダウンロード</button>
      <p role="status" class="hint">{{ message }}</p>
    </form>
  </details>
</template>
