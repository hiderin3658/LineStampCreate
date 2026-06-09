# skills/（スキルの正本）

このディレクトリは、各AIコーディングツールで共有する**スキルの正本（単一の真実）**です。

## 仕組み

`SKILL.md`（[Agent Skills オープン標準](https://agentskills.io/)）は **フォーマットは共通**ですが、
**各ツールがスキルを読み込むディレクトリは異なります**（統一の共通フォルダは存在しない）。
そのため、正本を各ツールのディレクトリへ複製して配置します。

| ツール | プロジェクトのスキルディレクトリ |
|--------|-------------------------------|
| Claude Code | `.claude/skills/` |
| OpenAI Codex | `.codex/skills/` |
| Cursor | `.cursor/skills/` |
| Gemini CLI | `.gemini/skills/` |
| Windsurf | `.windsurf/skills/` |

スキル本体（手順）は同一で、処理はリポジトリ直下の `scripts/` を呼ぶだけなので、
bashを実行できるエージェントであればツールを問わず同じように動作します。

## 編集と同期のルール

- **編集するのはこの `skills/<skill>/` の正本だけ**。各ツール配下のコピーは直接編集しない。
- 編集後は同期スクリプトで各ツールへ反映する：
  ```bash
  python3 scripts/sync_skills.py          # 各ツールのディレクトリへ同期
  python3 scripts/sync_skills.py --check  # 同期済みか検証（差分があれば非ゼロ終了）
  ```
- 対応ツールを増減する場合は `scripts/sync_skills.py` の `TARGET_SKILL_DIRS` を編集する。

## 収録スキル

- `line-stamp-builder/` … シート画像からLINEスタンプ用画像一式（透過PNG＋ZIP）を生成する。
