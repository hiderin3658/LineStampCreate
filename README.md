# LineStampCreate

白背景に複数スタンプを格子配置した**シート画像1枚**から、[LINE Creators Market](https://creator.line.me/ja/) にそのままアップロードできる**画像一式（透過PNG＋ZIP）**と、各スタンプの**タグ設定案**を自動生成するツールです。

シート画像を渡すだけで、`01.png`〜`NN.png`（透過スタンプ）・`main.png`（メイン画像）・`tab.png`（タブ画像）・アップロード用ZIPまでを一括生成します。AIコーディングエージェント向けの**スキル**としても、Pythonスクリプトを直接叩く**CLI**としても使えます。

| 入力 | 出力 |
|------|------|
| 白背景に格子配置した1枚のシート画像 | 透過PNG一式＋LINE仕様準拠のZIP＋タグ設定案 |

---

## 特長

- **個数・グリッドは自動推定** — シートの余白（ガター）を検出して列×行を判定。8 / 16 / 24 / 32 / 40 個に対応。
- **賢い背景透過** — 縁から連結した白だけを透過するフラッドフィル方式。キャラ内部の白（毛・服のハイライト）は残す。
- **キャラのみ抽出（`isolate_subject`）** — 極小のタブ画像などで文字や離れた装飾（効果線・キラキラ等）が潰れる場合に、最大の連結成分＝キャラ本体だけを残して除去。
- **LINE仕様を自動で満たす** — 最大370×320px・偶数寸法・1画像1MB以下・ZIP60MB以下を保証（超過時は自動で減色）。
- **タグ設定案の作成** — 各スタンプの言葉・感情・場面に合わせて、公式の利用可能タグから候補を選び `output/<名前>/タグ設定.md` に出力（スクリプトではなく、スキル実行時にAIエージェントが作成する成果物）。タグはキャラではなくシチュエーションで決まるため、既定構成（vol1〜3）は正本の[シチュエーション別タグ対応表](docs/シチュエーション別タグ対応表.md)を転記でき、同一シリーズの全キャラでタグが揃う。
- **目視確認用プレビュー** — 全スタンプを市松模様背景に並べた `_preview.png` を生成し、透過漏れや切れを確認できる。
- **追加インストール最小** — Pillow と numpy のみ（pngquant / rembg などの外部依存なし）。
- **マルチツール対応** — Claude Code / OpenAI Codex / Cursor / Gemini CLI / Windsurf でスキルとして共有可能。

---

## 必要環境

- Python 3.10 以上（開発・検証は 3.12）
- [Pillow](https://python-pillow.org/) 10 以上
- [numpy](https://numpy.org/) 1.24 以上

### インストール

```bash
# 仮想環境（任意・推奨）
python3 -m venv .venv
source .venv/bin/activate   # Windows は .venv\Scripts\activate

# 依存パッケージ
pip install -r requirements.txt
```

---

## 使い方

スタンプのシート画像を `input/` に置き（例: `input/Stamp_Cat1.png`）、リポジトリのルートで次の2コマンドを実行します。

```bash
# 1) グリッド・個数を自動推定して設定ファイルを生成（既存configがある場合は --force が必要）
python3 scripts/init_config.py input/Stamp_Cat1.png
#   → config/Cat1.json が出力される（推定グリッドが表示される）

# 2) スタンプ一式とZIPを生成
python3 scripts/build_stickers.py config/Cat1.json
#   → output/Cat1/ に画像一式と Cat1_line_stickers.zip が出力される
```

生成後は **必ず `output/<名前>/_preview.png` を目視確認**してください（透過漏れ・白フチ・切れがないか）。問題なければ `output/<名前>/<名前>_line_stickers.zip` をLINE Creators Marketにアップロードします。

> 推定グリッドが意図と違う場合は、生成された `config/<名前>.json` の `grid`（`cols`/`rows`）を手で直してから手順2を実行してください。透過がうまくいかない場合は同ファイルの `background.threshold`（既定240）・`feather`（既定1）・`isolate_subject` を調整します。

### AIエージェントのスキルとして使う

対応エージェント（Claude Code / Cursor / Windsurf / OpenAI Codex / Gemini CLI）では、シート画像を渡して「**LINEスタンプを作って**」と依頼するだけで、スキル `line-stamp-builder` が上記の手順（生成 → 目視確認 → タグ設定案の作成）を案内・実行します。詳細は [`skills/line-stamp-builder/SKILL.md`](skills/line-stamp-builder/SKILL.md) を参照してください。各ツールへの配置は `python3 scripts/sync_skills.py` で同期できます（詳細は [`skills/README.md`](skills/README.md)）。

---

## 入力シート画像の条件

このツールは、次の条件を満たすシート画像を前提にしています。条件を外すとグリッド誤検出や透過失敗の原因になります。

- **白背景**（純白〜オフホワイト #FFFFFF〜#FCFCFC 程度）。
- **1キャラクター**を、表情・ポーズ違いで複数並べる。
- **格子（グリッド）配置**で、スタンプ同士の間に**はっきりした余白（ガター）**を空ける。
- 個数は **8 / 16 / 24 / 32 / 40** のいずれか（例: 4列×6行＝24）。
- 各スタンプは枠やガターに**接触させない**（後段で自動トリミングするため、少し余白を持たせる）。
- 採番は左上から右方向・行順（読み順）。

### シート画像をAIで作るときのプロンプト例

入力シートは画像生成AI（ChatGPT / DALL·E、Midjourney、Stable Diffusion 等）で用意できます。以下は**そのまま使うものではなく、キャラや表情を差し替えて使うテンプレート例**です。生成後はガターの幅・背景の白さ・個数を必ず確認してください。

```text
A single sheet image containing 24 LINE-style sticker illustrations of the
SAME original character, arranged in a clean 4-column x 6-row grid.

Character: <キャラの説明（例: a fluffy gray tabby cat, cute, round eyes）>
Style: flat, soft cel-shading, thick clean outlines, kawaii sticker art.

Requirements:
- Pure WHITE background (#FFFFFF), no patterns, no shadows behind characters.
- Each cell shows ONE expression/pose with a short Japanese word
  (e.g. こんにちは / ありがとう / おやすみ / ごめんね ...).
- Keep clear, even GUTTERS (white gaps) between every row and column.
- Do NOT let any character or text touch the grid lines or image edges.
- Consistent character design and size across all 24 cells.
- High resolution, crisp edges, no photographic background.
```

> 個数を変える場合は「24 / 4-column x 6-row」の部分を `16（4x4）` `40（5x8）` 等に置き換えてください。表情のバリエーション（あいさつ・感謝・喜怒哀楽・お願い・おやすみ 等）を具体的に列挙すると、使いやすいセットになります。

---

## 出力物

```
output/<名前>/
├── stickers/
│   ├── 01.png  …  最大370×320px・透過・偶数・1MB以下
│   │   …
│   └── NN.png
├── main.png                    … 240×240px（代表スタンプから生成）
├── tab.png                     … 96×74px（代表スタンプから生成）
├── _preview.png                … 目視確認用（市松模様背景に全スタンプを配置）
├── タグ設定.md                  … 各スタンプのタグ設定案（スキル実行時にAIが作成）
└── <名前>_line_stickers.zip    … アップロード用ZIP（中間フォルダなし・60MB以下）
```

---

## リポジトリ構成

```
LineStampCreate/
├── README.md
├── requirements.txt
├── LICENSE
├── input/                  … 入力シート画像（.gitignore 対象）
├── config/                 … 作品ごとの設定JSON（例: Cat1.json）
├── output/                 … 生成物（.gitignore 対象）
├── scripts/
│   ├── init_config.py      … グリッド・個数を自動推定して設定JSONを生成
│   ├── build_stickers.py   … 設定JSONからスタンプ一式＋ZIPを生成
│   └── sync_skills.py      … スキルを各AIツールのディレクトリへ同期
├── skills/                 … スキルの正本（SKILL.md・詳細は skills/README.md）
└── docs/                   … 設計書・登録手順・タグ資料・生成プロンプト（vol1〜3）
```

---

## ドキュメント

- [設計書](docs/設計書_LINEスタンプ生成パイプライン.md) … 処理パイプライン・設定スキーマ（`isolate_subject` 含む）・透過方式の詳細。
- [登録手順書](docs/手順書_LINEスタンプ登録から販売まで.md) … クリエイター登録〜審査〜販売開始の全手順。
- [タグ設定](docs/LINEスタンプ_タグ設定.md) … タグの考え方・登録手順・利用可能タグ全一覧。
- [シチュエーション別タグ対応表](docs/シチュエーション別タグ対応表.md) … vol1〜3の各シチュエーションに設定するタグの正本（キャラ非依存。スキルはこれを転記）。

---

## 注意事項

- 自動処理のみで審査通過を保証するものではありません。アップロード前に `_preview.png` で**必ず目視確認**してください。
- LINEの画面UI・仕様・審査基準は変更されることがあります。最新情報は[公式ガイドライン](https://creator.line.me/ja/guideline/sticker/)を確認してください。
- AI生成画像を使う場合は、登録時に「AIを使用しています」の申告が必要です。

---

## ライセンス

[MIT License](LICENSE)
