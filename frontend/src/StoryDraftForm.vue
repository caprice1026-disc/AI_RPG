<script setup lang="ts">
import { computed } from 'vue'
import StoryFields, { type AuthorField, type Choice } from './StoryFields.vue'
import StoryEffect from './StoryEffect.vue'
import { capabilityNames, node, rows, type AuthoringDraft, type DraftNode, type FieldPolicy } from './story-contracts'

const props = defineProps<{ draft: AuthoringDraft }>()
const scenario = computed(() => props.draft.scenario)
const initial = computed(() => node(scenario.value.initialization))
const world = computed(() => node(scenario.value.world))
const choices = (values: DraftNode[], key: string, label: string): Choice[] => values.filter(v => v[key]).map((v, i) => ({
  value: String(v[key]), label: String(v[label] || `名称未入力 ${i + 1}`),
}))
const scenes = computed(() => choices(rows(scenario.value.scenes), 'scene_ref', 'title'))
const flags = computed(() => choices(rows(scenario.value.flags), 'flag_ref', 'public_fact'))
const endings = computed(() => choices(rows(scenario.value.endings), 'ending_ref', 'title'))
const characters = computed(() => choices(rows(initial.value.characters), 'ref', 'label'))
const entities = computed(() => [...characters.value, ...choices(rows(initial.value.items), 'ref', 'label')])
const visibility = [{ value: 'public', label: 'プレイヤーに公開' }, { value: 'secret', label: '未発見の秘密' }, { value: 'author_only', label: '作者のみ' }]
const text = (key: string, label: string, type: AuthorField['type'] = 'text'): AuthorField => ({ key, label, type })
const select = (key: string, label: string, options: Choice[], nullable = false): AuthorField => ({ key, label, type: 'select', options, nullable })
const multi = (key: string, label: string, options: Choice[]): AuthorField => ({ key, label, type: 'multi', options })
const number = (key: string, label: string, min: number, max: number): AuthorField => ({ key, label, type: 'number', min, max })
const check = (key: string, label: string): AuthorField => ({ key, label, type: 'check' })
function add(parent: DraftNode, key: string, value: DraftNode) { parent[key] = [...rows(parent[key]), value] }
function remove(parent: DraftNode, key: string, i: number, path: string) {
  rows(parent[key]).splice(i, 1)
  // Keep index-based policy pointers attached to their surviving entries after removal.
  const policies: Record<string, FieldPolicy> = {}
  for (const [pointer, policy] of Object.entries(props.draft.field_policies)) {
    if (!pointer.startsWith(`${path}/`)) { policies[pointer] = policy; continue }
    const parts = pointer.slice(path.length + 1).split('/'), index = Number(parts[0])
    if (index === i) continue
    if (index > i) parts[0] = String(index - 1)
    policies[`${path}/${parts.join('/')}`] = policy
  }
  props.draft.field_policies = policies
}
function actionKind(action: DraftNode, event: Event) {
  const kind = (event.target as HTMLSelectElement).value
  const kept = { action_ref: action.action_ref, label: action.label, required_flags: action.required_flags ?? [], disabled_flags: action.disabled_flags ?? [] }
  Object.keys(action).forEach(key => delete action[key])
  Object.assign(action, kept, { kind }, kind === 'scenario_action' ? { public_fact: '', success: {} }
    : kind === 'skill_check' ? { check_ref: 'normal', skill_ref: 'perception', success: {}, failure: {} }
      : { target_ref: '', defeated: {} })
}
function itemKind(item: DraftNode, event: Event) {
  const kind = (event.target as HTMLSelectElement).value
  item.weapon = kind === 'weapon' ? { damage_expression: '1d4', damage_bonus: 0 } : null
  item.effect_ref = kind === 'healing_potion' ? kind : null
  if (kind !== 'weapon') item.equipped = false
}
const policyTargets = computed(() => {
  const targets: Choice[] = [
    { value: '/metadata/title', label: '公開タイトル' }, { value: '/metadata/synopsis', label: '公開紹介文' },
    { value: '/scenario/objective', label: '目的' }, { value: '/scenario/world', label: '世界の設定全体' },
    { value: '/scenario/initialization', label: '人物・アイテム全体' },
  ]
  for (const [group, label] of [['characters', '人物'], ['items', 'アイテム']] as const) rows(initial.value[group]).forEach((row, i) => targets.push({
    value: `/scenario/initialization/${group}/${i}`, label: `${label}「${row.label || i + 1}」の設定`,
  }))
  rows(world.value.protected_facts).forEach((fact, i) => targets.push({ value: `/scenario/world/protected_facts/${i}/statement`, label: `固定設定 ${i + 1}「${String(fact.statement || '').slice(0, 30)}」` }))
  for (const [group, label, name, fields] of [
    ['scenes', '場所', 'title', ['title', 'description', 'npc_notes', 'actions']],
    ['flags', '条件', 'public_fact', ['public_fact']],
    ['endings', '結末', 'title', ['title', 'summary', 'required_flags', 'reward']],
  ] as const) rows(scenario.value[group]).forEach((row, i) => fields.forEach(field => targets.push({
    value: `/scenario/${group}/${i}/${field}`, label: `${label}「${row[name] || i + 1}」の${({ title: '名称', description: '説明', npc_notes: '秘密', actions: '行動', public_fact: '事実', summary: '内容', required_flags: '条件', reward: '報酬' })[field]}`,
  })))
  for (const pointer of Object.keys(props.draft.field_policies)) if (!targets.some(t => t.value === pointer)) targets.push({ value: pointer, label: 'その他の保存済み設定' })
  return targets
})
function policy(pointer: string, event: Event) {
  const value = (event.target as HTMLSelectElement).value as FieldPolicy | ''
  if (value) props.draft.field_policies[pointer] = value
  else delete props.draft.field_policies[pointer]
}
</script>

<template>
  <p class="hint">公開紹介と作者用の秘密を分けて入力します。新しい場所・人物・条件は自動保存後に選択肢へ追加されます。複数選択は Ctrl / ⌘ キーで切り替えられます。</p>
  <details open class="author-section"><summary>基本情報・公開紹介</summary>
    <StoryFields :value="node(draft.metadata)" path="/metadata" :fields="[
      text('title', '作品タイトル'), text('synopsis', '公開紹介文', 'long'),
      text('tags', 'タグ（1行に1つ）', 'lines'), text('content_warnings', '内容の注意事項（1行に1つ）', 'lines'),
    ]" />
    <label for="author-notes">作者用メモ（非公開）</label><textarea id="author-notes" v-model="draft.notes" rows="3" />
  </details>

  <details open class="author-section"><summary>世界・目的</summary>
    <StoryFields :value="scenario" path="/scenario" :fields="[text('title', '冒険中のタイトル'), text('objective', '冒険の目的', 'long'),
      multi('required_capabilities', '使用する機能', Object.entries(capabilityNames).map(([value, label]) => ({ value, label })))]" />
    <template v-if="scenario.world">
      <StoryFields :value="world" path="/scenario/world" :fields="[
        text('region_name', '舞台の名前'), text('boundary', '行動できる範囲', 'long'),
        select('goal_scene_ref', '目的を達成する場所', scenes), select('goal_flag_ref', '目的の達成条件', flags),
        select('outside_ending_ref', '範囲外へ出たときの結末', endings),
        text('protected_terms', '大切な用語（1行に1つ）', 'lines'), text('impossible_destinations', '行けない場所（1行に1つ）', 'lines'),
      ]" />
      <h3>世界の固定設定・秘密</h3>
      <article v-for="(fact, i) in rows(world.protected_facts)" :key="i" class="author-row">
        <StoryFields :value="fact" :path="`/scenario/world/protected_facts/${i}`" :fields="[
          text('statement', '設定の内容', 'long'),
          select('kind', '設定の種類', [{ value: 'location', label: '所在' }, { value: 'motive', label: '動機' }, { value: 'rule', label: '世界の決まり' }, { value: 'existence', label: '存在' }]),
          select('visibility', '情報の公開範囲', visibility), select('scene_ref', '関係する場所', scenes, true),
          select('entity_ref', '関係する人物・物', entities, true), select('reveal_flag_ref', '秘密が判明する条件', flags, true),
          select('acquired_flag_ref', '入手済みとする条件', flags, true),
        ]" />
        <button type="button" class="quiet" @click="remove(world, 'protected_facts', i, '/scenario/world/protected_facts')">設定を削除</button>
      </article>
      <button type="button" @click="add(world, 'protected_facts', { kind: 'rule', statement: '', visibility: 'secret' })">固定設定を追加</button>
    </template>
    <button v-else type="button" @click="scenario.world = { region_name: '', boundary: '', goal_scene_ref: '', goal_flag_ref: '', outside_ending_ref: '', protected_facts: [] }">世界の範囲と自由行動を設定</button>
  </details>

  <details class="author-section"><summary>進行条件</summary>
    <p class="hint">「鍵を入手した」などの成立する事実を登録し、行動や結末で名前から選びます。</p>
    <article v-for="(flag, i) in rows(scenario.flags)" :key="i" class="author-row">
      <StoryFields :value="flag" :path="`/scenario/flags/${i}`" :fields="[text('public_fact', '成立したときに公開する事実', 'long')]" />
      <button type="button" class="quiet" @click="remove(scenario, 'flags', i, '/scenario/flags')">条件を削除</button>
    </article>
    <button type="button" @click="add(scenario, 'flags', { public_fact: '' })">条件を追加</button>
  </details>

  <details class="author-section"><summary>場所・行動・分岐</summary>
    <article v-for="(scene, i) in rows(scenario.scenes)" :key="i" class="author-row">
      <h3>{{ scene.title || `場所 ${i + 1}` }}</h3>
      <StoryFields :value="scene" :path="`/scenario/scenes/${i}`" :fields="[
        text('title', '場所の名前'), number('sequence', '順序（1が開始地点）', 1, 50), text('description', '公開する場所の説明', 'long'),
        text('npc_notes', 'この場所の秘密・人物メモ（1行に1つ）', 'lines'),
        multi('open_destinations', '自由行動で移動できる場所', scenes), multi('open_flags', '自由行動で成立できる条件', flags),
      ]" />
      <details v-for="(action, j) in rows(scene.actions)" :key="j" class="author-action">
        <summary>{{ action.label || `行動 ${j + 1}` }}</summary>
        <label :for="`action-kind-${i}-${j}`">行動の種類</label>
        <select :id="`action-kind-${i}-${j}`" :value="action.kind" @change="actionKind(action, $event)">
          <option value="scenario_action">行動する</option><option value="skill_check">技能判定</option><option value="attack">攻撃</option>
        </select>
        <StoryFields :value="action" :path="`/scenario/scenes/${i}/actions/${j}`" :fields="[
          text('label', '行動の名前'), multi('required_flags', '行動に必要な条件', flags), multi('disabled_flags', '行動できなくなる条件', flags),
          ...(action.kind === 'scenario_action' ? [text('public_fact', '行動後に公開する事実', 'long')] : []),
          ...(action.kind === 'skill_check' ? [select('skill_ref', '判定する技能', [{ value: 'perception', label: '観察' }, { value: 'persuasion', label: '説得' }, { value: 'stealth', label: '隠密' }]),
            select('check_ref', '難しさ', [{ value: 'easy', label: '易しい' }, { value: 'normal', label: '普通' }, { value: 'hard', label: '難しい' }])] : []),
          ...(action.kind === 'attack' ? [select('target_ref', '攻撃対象', characters)] : []),
        ]" />
        <template v-for="outcome in (action.kind === 'attack' ? ['defeated'] : action.kind === 'skill_check' ? ['success', 'failure'] : ['success'])" :key="outcome">
          <h4>{{ outcome === 'failure' ? '失敗したとき' : outcome === 'defeated' ? '相手を倒したとき' : '成功したとき' }}</h4>
          <StoryEffect v-if="action[outcome]" :value="node(action[outcome])" :path="`/scenario/scenes/${i}/actions/${j}/${outcome}`" :scenes="scenes" :flags="flags" :endings="endings" />
          <button v-else type="button" @click="action[outcome] = {}">結果を設定</button>
        </template>
        <button type="button" class="quiet" @click="remove(scene, 'actions', j, `/scenario/scenes/${i}/actions`)">行動を削除</button>
      </details>
      <button type="button" @click="add(scene, 'actions', { kind: 'scenario_action', label: '', public_fact: '', success: {} })">行動を追加</button>
      <details class="author-action"><summary>戦闘設定</summary>
        <template v-if="scene.combat">
          <StoryFields :value="node(scene.combat)" :path="`/scenario/scenes/${i}/combat`" :fields="[
            select('enemy_ref', '敵', characters), select('started_flag', '戦闘開始の条件', flags), select('defeat_ending_ref', '敗北したときの結末', endings),
            select('damage_expression', '敵のダメージ', [{ value: '1d4', label: '4面ダイス1個' }, { value: '1d6', label: '6面ダイス1個' }]), number('damage_bonus', '敵のダメージ加算', 0, 20),
          ]" />
          <button type="button" class="quiet" @click="scene.combat = null">戦闘設定を外す</button>
        </template>
        <button v-else type="button" @click="scene.combat = { enemy_ref: '', started_flag: '', defeat_ending_ref: '', damage_expression: '1d4', damage_bonus: 0 }">戦闘を設定</button>
      </details>
      <button type="button" class="quiet" @click="remove(scenario, 'scenes', i, '/scenario/scenes')">場所を削除</button>
    </article>
    <button type="button" @click="add(scenario, 'scenes', { sequence: Math.max(0, ...rows(scenario.scenes).map(s => Number(s.sequence) || 0)) + 1, title: '', description: '', actions: [] })">場所を追加</button>
  </details>

  <details class="author-section"><summary>人物・アイテム・配置</summary>
    <button v-if="!scenario.initialization" type="button" @click="scenario.initialization = { characters: [], items: [], placements: [] }">人物とアイテムを設定</button>
    <template v-else>
      <h3>人物・敵</h3>
      <article v-for="(character, i) in rows(initial.characters)" :key="i" class="author-row">
        <StoryFields :value="character" :path="`/scenario/initialization/characters/${i}`" :fields="[
          text('label', '人物の名前'), select('kind', '役割', [{ value: 'npc', label: '登場人物' }, { value: 'monster', label: '敵' }]),
          number('max_hp', '最大HP', 1, 1000), number('defense', '防御力', 0, 30), number('attack_bonus', '攻撃の加算', -20, 20),
        ]" />
        <button type="button" class="quiet" @click="remove(initial, 'characters', i, '/scenario/initialization/characters')">人物を削除</button>
      </article>
      <button type="button" @click="add(initial, 'characters', { label: '', kind: 'npc', max_hp: 6, defense: 9, attack_bonus: 0 })">人物を追加</button>
      <h3>アイテム</h3>
      <article v-for="(item, i) in rows(initial.items)" :key="i" class="author-row">
        <StoryFields :value="item" :path="`/scenario/initialization/items/${i}`" :fields="[
          text('label', 'アイテムの名前'), select('owner_ref', '最初の持ち主', [{ value: 'hero', label: '冒険者' }, ...characters], true), number('quantity', '個数', 1, 99),
        ]" />
        <label :for="`item-kind-${i}`">アイテムの種類</label>
        <select :id="`item-kind-${i}`" :value="item.weapon ? 'weapon' : item.effect_ref || 'none'" @change="itemKind(item, $event)">
          <option value="none">通常の物</option><option value="weapon">武器</option><option value="healing_potion">回復アイテム</option>
        </select>
        <template v-if="item.weapon">
          <StoryFields :value="node(item.weapon)" :path="`/scenario/initialization/items/${i}/weapon`" :fields="[
            select('damage_expression', '武器のダメージ', [{ value: '1d4', label: '4面ダイス1個' }, { value: '1d6', label: '6面ダイス1個' }]), number('damage_bonus', 'ダメージ加算', 0, 20),
          ]" />
          <StoryFields :value="item" :path="`/scenario/initialization/items/${i}`" :fields="[check('equipped', '最初から装備する')]" />
        </template>
        <button type="button" class="quiet" @click="remove(initial, 'items', i, '/scenario/initialization/items')">アイテムを削除</button>
      </article>
      <button type="button" @click="add(initial, 'items', { label: '', quantity: 1, owner_ref: null, equipped: false })">アイテムを追加</button>
      <h3>場所への配置</h3><p class="hint">持ち主がいるアイテムは配置しません。攻撃できる人物はプレイヤーに公開してください。</p>
      <article v-for="(placement, i) in rows(initial.placements)" :key="i" class="author-row">
        <StoryFields :value="placement" :path="`/scenario/initialization/placements/${i}`" :fields="[
          select('entity_ref', '配置する人物・物', entities), select('scene_ref', '配置する場所', scenes), select('visibility', '配置の公開範囲', visibility), check('attackable', '攻撃できる人物'),
        ]" />
        <button type="button" class="quiet" @click="remove(initial, 'placements', i, '/scenario/initialization/placements')">配置を削除</button>
      </article>
      <button type="button" @click="add(initial, 'placements', { entity_ref: '', scene_ref: '', visibility: 'public', attackable: false })">配置を追加</button>
    </template>
  </details>

  <details class="author-section"><summary>結末</summary>
    <article v-for="(ending, i) in rows(scenario.endings)" :key="i" class="author-row">
      <StoryFields :value="ending" :path="`/scenario/endings/${i}`" :fields="[
        text('title', '結末の名前'), text('summary', '結末の内容（到達するまで非公開）', 'long'), multi('required_flags', '到達に必要な条件', flags),
      ]" />
      <StoryFields v-if="ending.reward" :value="node(ending.reward)" :path="`/scenario/endings/${i}/reward`" :fields="[
        select('tier', '報酬の程度', [{ value: 'full', label: '完全な報酬' }, { value: 'reduced', label: '一部の報酬' }, { value: 'none', label: '報酬なし' }]), text('description', '報酬の説明', 'long'),
      ]" />
      <button v-else type="button" @click="ending.reward = { tier: 'none', description: '' }">報酬を設定</button>
      <button type="button" class="quiet" @click="remove(scenario, 'endings', i, '/scenario/endings')">結末を削除</button>
    </article>
    <button type="button" @click="add(scenario, 'endings', { title: '', summary: '', required_flags: [], reward: { tier: 'none', description: '報酬なし' } })">結末を追加</button>
  </details>

  <details class="author-section"><summary>AI裁量</summary>
    <p class="hint">固定はAIによる変更を許可せず、補完可能は補足を許可します。未決定は作者の確認まで保留します。この画面の保存だけでAI生成は実行されません。</p>
    <div v-for="target in policyTargets" :key="target.value" class="policy-row">
      <label :for="`policy-${target.value}`">{{ target.label }}</label>
      <select :id="`policy-${target.value}`" :value="draft.field_policies[target.value] || ''" @change="policy(target.value, $event)">
        <option value="">個別指定なし</option><option value="fixed">固定</option><option value="fillable">補完可能</option><option value="undecided">未決定</option>
      </select>
    </div>
  </details>
</template>
