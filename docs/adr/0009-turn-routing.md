# ADR-0009: Narrative優先の決定的Turnルーティング

- 状態: 採用
- 決定日: 2026-09-14

## 文脈と決定

MVPでは独立したRouter LLMを置かず、Applicationの決定的ルールで初期routeを選ぶ。保存済みChoiceは表示文へ解決した後、通常入力と同じ分類を行う。分類結果、ルールversion、根拠codeをTurnへ記録する。

### 初期Mechanical条件

入力が、現在のactorによる次のいずれかの**実行要求**を含む場合はMechanicalとする。

1. 対象への攻撃、命中、damageを伴う行動。
2. `mvp_v1` の技能を使う試行、成否判定、明示的なダイス要求。
3. 所有itemの使用または消費。
4. HPのdamage/healing、またはruleset状態変更を意図する行動。
5. 上記とNarrative要素が混在する複合入力。Mechanicalが演出部分もplayer intentとして受け取る。

「攻撃とは何か」「ポーションを見せて」のようなルールの質問、観察だけの描写依頼、itemへの言及だけでは実行要求としない。Mechanical候補だがactor、対象、item等が不足する場合もMechanicalへ渡し、Intent extractorが推測せず `ClarificationRequired` を返す。

### 初期Narrative条件と分類不能時

会話、台詞、移動の希望、場面・所持品・ルールの質問、感情、演出だけの行動で、上の状態変更を要求しないものはNarrativeとする。辞書やpatternで分類不能な入力も、次を**すべて満たす場合だけ**Narrativeへ渡す。

- schema上有効な非空入力である。
- 既知のMechanical動詞、技能名、ダイス式、HP/状態変更、選択済みMechanical Choiceを含まない。
- prompt injection検知やアクセス制御違反として入口で拒否すべき入力ではない。
- 不明なIDをCanonical変更へ使う要求ではない。

このfallbackは「Narrativeなら状態を変えてよい」という意味ではない。Narrative GMはCanonicalを変更できず、応答中に具体的な対応済みActionが必要だと判明した場合だけ `ResolutionRequired(actions)` を返す。その結果をschema、参照、権限で検証してrouteをMechanicalへ昇格する。自由記述だけの昇格、対応外Action、根拠のないIDは受理せず確認を返す。MechanicalからNarrativeへの再分類は同じTurn内では行わない。

## 採用理由

- 明白なゲーム操作を最初からEngine経路へ送り、LLMに状態変更権限を与えない。
- 分類不能な日常会話をエラーにせずNarrativeで扱いつつ、安全条件と昇格経路を明示できる。
- Router専用のprovider requestを使わず、呼び出し回数と遅延を抑えられる。

## 却下した案

- **すべてNarrativeへ送る**: 明白な攻撃にも余分な呼び出しが必要で、誤描写の範囲が広がる。
- **すべてMechanicalへ送る**: 雑談や質問に不要なIntent抽出を行い、確認質問が増える。
- **専用Router LLM**: 分類だけに予算と新たな不確実性を追加する。
- **分類不能を無条件にNarrativeへ送る**: 難読化された状態変更や不正参照を安全経路と誤認し得る。

## 後から交換できる境界

routerは `RouteDecision(route, rule_version, reason_codes)` を返すportとし、HTTP/LLM providerから独立させる。pattern実装をclassifierへ交換しても、Mechanicalの安全側条件、Narrativeの無変更権限、昇格schema、call budgetをcontract testで維持する。

## 影響

日本語の活用、英語、代表的な言い換え、否定文（「攻撃しない」）、引用・質問、複合入力、難読入力を回帰fixtureにする。否定や引用は単純keyword一致だけでMechanicalにしない。
