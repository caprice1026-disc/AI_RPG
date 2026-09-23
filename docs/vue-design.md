# Vue画面の設計と画像素材

## 画面の方針

物語を中央に置き、現在地・目的・HP・所持品はサーバー由来の状態として表示する。デスクトップは冒険一覧、本文、状態の3列とし、狭い画面では補助パネルを折りたたむ。自由入力と登録済みの行動候補を併用する。認証や再送の問題は本文に紛れ込ませず、操作を選べる案内として表示する。

配色は夜の藍色 `#101b25`、パネル `#192934`、紙色 `#f3ead8`、真鍮色 `#d6b777`、HPの青緑 `#89bdb1`。場面の見出しに明朝系、操作と本文に可読性の高い書体を使う。UI・文字・状態値はVueで描画し、生成画像へ埋め込まない。

概念図にある仮の冒険名や数値は実装へ持ち込まず、シナリオと公開APIの値を使う。画像は雰囲気を補う背景であり、描かれた物体が実行可能な対象であることを保証しない。

## 画像生成の記録

Codex内蔵ImageGenを使用した。画像生成CLIやGeminiのAPIキーは使用していない。概念図を先に生成し、その図をスタイル参照として礼拝堂の横長イラストを生成した。

- 概念図: 作業記録 `AI_RPG-stage5-evidence-20260922/ui-concept.png`
- 配布する画像: `frontend/public/art/ruined-chapel.png`
- ビルド後: `src/ai_rpg/api/static/vue/art/ruined-chapel.png`
- 概念図のUIは参照用。アプリへ貼り付けず、操作可能なVueコンポーネントとして実装する。

### 概念図の最終プロンプト

```text
Use case: ui-mockup. Create one high-fidelity product design concept sheet for a Japanese AI text RPG web app named AI RPG. Main panel: 1440x900 desktop adventure in progress; secondary narrow panel on right: 390px mobile view of same scene. This is a real playable short adventure '廃礼拝堂の依頼', not a marketing website. Elegant atmospheric editorial fantasy journal: midnight navy #101b25 app background, slightly lighter #192934 panels, warm ivory #f3ead8 body text, muted brass #d6b777 accents, subdued teal #89bdb1 HP meter. Japanese readable sans-serif body with Mincho-style narrative headings. Generous spacing, subtle borders, restrained corners, no neon, no gradients on controls. Desktop top bar AI RPG, 冒険の記録, quiet logout icon. Left slim rail: 続きから adventure list, 新しい冒険. Center wide reading column: cinematic horizontal illustrated abandoned stone chapel in mist and warm lantern light, header 廃礼拝堂の入口; concise objective 依頼品を回収する; narrative passage 霧の向こうに、古びた礼拝堂が姿を現した。風に揺れる扉の奥から、かすかな物音が聞こえる。 Below passage two understated action buttons 中を調べる, あたりを見回す. Bottom sticky composer placeholder どのように行動しますか？ and gold 送信 button. Right quiet status panel heading 冒険者 with player name アオイ, 斥候, HP 12 / 12 meter, 持ち物 回復ポーション × 2, and 発見したこと. No invented currency, XP, percentages, avatars, levels, metrics, extra nav or competitive features. Mobile collapses navigation and character state into accessible header buttons, preserves large readable scene art, objective, narration and sticky input. Add a small login/entry state inset using matching theme, title あなたの言葉で、物語が動く。 and ログインして冒険を始める CTA, no extra badges. All functional text/buttons will be rebuilt in Vue; concept must show realistic implementable UI, no device mockup frames, no watermark. Distinctive polished calm literary game aesthetic, content more important than decoration.
```

### 配布画像の最終プロンプト

```text
Use case: illustration-story. Image 1 is a STYLE AND SUBJECT REFERENCE only, not an edit target. Generate a standalone wide landscape environment illustration for the AI RPG game, matching the chapel illustration inside that interface concept: abandoned small Gothic stone chapel at blue-hour twilight, weathered arched entrance with warm candlelight visible inside, broken iron gate, overgrown ferns and ivy, misty forest, one hanging amber lantern near the path. Refined painterly realistic fantasy concept art with subtle brush texture, blue-gray stone and deep midnight blues contrasted with restrained amber light. Quiet mystery, inviting exploration, not horror. Widescreen landscape 16:9, main chapel door at center-right, atmospheric path and foliage on left; keep key subject in central region so mobile cropping works. This image will be used as a scene banner and login illustration. No UI, no panels, no text, no logo, no frame, no watermark, no humans, no invented creatures. Preserve the calm literary fantasy aesthetic from the reference. Full-bleed artwork only.
```
