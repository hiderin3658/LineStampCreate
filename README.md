# LineStampCreate

白背景に複数スタンプを格子配置した**シート画像1枚**から、LINE Creators Market にアップロードできる画像一式（透過PNG＋ZIP）と、各スタンプの**タグ設定案**を自動生成するリポジトリ。

## できること

- **グリッド自動推定**: 余白（ガター）検出で列×行・個数を自動推定し、設定ファイルを生成
- **スタンプ一式の生成**: `01.png`〜`NN.png`（最大370×320・透過）／`main.png`（240×240）／`tab.png`（96×74）／アップロード用ZIP
- **キャラのみ抽出（`isolate_subject`）**: 極小のタブ画像などで文字や離れた装飾（効果線・キラキラ等）が潰れる場合に、最大の連結成分＝キャラ本体だけを残して除去
- **タグ設定案の作成**: 各スタンプの言葉・感情・場面に合わせて、公式の利用可能タグから候補を選び `output/<名前>/タグ設定.md` に出力

## 使い方

前提: Python3＋Pillow＋numpy。入力は「白背景・1キャラ・格子配置・読み順採番」のシート画像。

```bash
# 1. グリッドを自動推定して設定ファイルを生成（既存configがある場合は --force が必要）
python3 scripts/init_config.py input/Stamp_Cat1.png

# 2. スタンプ一式（透過PNG・main・tab・ZIP）を生成
python3 scripts/build_stickers.py config/Cat1.json
```

生成後は `output/<名前>/_preview.png` を目視確認し、問題があれば `config/<名前>.json` の `background.threshold`／`feather`／`isolate_subject` を調整して再生成する。

AIコーディングツール（Claude Code / Cursor / Windsurf / OpenAI Codex / Gemini CLI）では、シート画像を渡して「LINEスタンプを作って」と依頼するだけで、スキル `line-stamp-builder` が上記の手順（生成→目視確認→タグ設定案の作成）を案内する。

## ディレクトリ構成

| パス | 内容 |
|------|------|
| `input/` | 入力のシート画像（`Stamp_*.png`） |
| `config/` | 作品ごとの生成設定（`<名前>.json`） |
| `scripts/` | 生成スクリプト（`init_config.py`／`build_stickers.py`）とスキル同期（`sync_skills.py`） |
| `output/<名前>/` | 生成物（透過PNG・main・tab・ZIP・`_preview.png`・`タグ設定.md`） |
| `skills/` | 各AIツール共通スキルの**正本**（詳細は `skills/README.md`） |
| `docs/` | 設計書・登録手順書・タグ設定ガイド |

## スキル（AIツール共通）

`skills/line-stamp-builder/SKILL.md` が正本で、`scripts/sync_skills.py` で各ツールのスキルディレクトリ（`.claude/` `.codex/` `.cursor/` `.gemini/` `.windsurf/`）へミラー同期する。**編集は正本のみ**に行い、編集後は同期する。

```bash
python3 scripts/sync_skills.py          # 各ツールへミラー同期
python3 scripts/sync_skills.py --check  # 同期済みか検証
```

## ドキュメント

- `docs/設計書_LINEスタンプ生成パイプライン.md` … 設定スキーマ（`isolate_subject` 含む）・透過方式の詳細
- `docs/手順書_LINEスタンプ登録から販売まで.md` … クリエイター登録〜審査〜販売の全手順
- `docs/LINEスタンプ_タグ設定.md` … タグの考え方・登録手順・利用可能タグ全一覧
