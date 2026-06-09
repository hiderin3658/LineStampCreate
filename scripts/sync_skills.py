#!/usr/bin/env python3
"""正本のスキルを各AIコーディングツールのスキルディレクトリへ同期する.

SKILL.md は Agent Skills のオープン標準（フォーマット共通）だが、ツールごとに
読み込むディレクトリが異なるため、同じスキルを各ディレクトリに配置する必要がある。
このスクリプトは正本 `skills/<skill>/` を各ツールの `.{tool}/skills/<skill>/` へ複製する。

使い方:
  python3 scripts/sync_skills.py          # 各ツールのディレクトリへ同期（コピー）
  python3 scripts/sync_skills.py --check  # 同期済みか検証（差分があれば非ゼロ終了。CI向け）

正本だけを編集し、各ツールのコピーは直接編集しないこと（このスクリプトで再生成する）。
"""

from __future__ import annotations

import argparse
import filecmp
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = ROOT / "skills"

# 各ツールのプロジェクトレベル スキルディレクトリ（Agent Skills オープン標準・2026年時点）
TARGET_SKILL_DIRS = {
    "Claude Code": ROOT / ".claude" / "skills",
    "OpenAI Codex": ROOT / ".codex" / "skills",
    "Cursor": ROOT / ".cursor" / "skills",
    "Gemini CLI": ROOT / ".gemini" / "skills",
    "Windsurf": ROOT / ".windsurf" / "skills",
}


def iter_skills() -> list[Path]:
    """正本ディレクトリ配下の SKILL.md を持つスキルフォルダ一覧を返す。"""
    if not SRC_DIR.exists():
        return []
    return sorted(p for p in SRC_DIR.iterdir()
                  if p.is_dir() and (p / "SKILL.md").exists())


def skill_files(skill_dir: Path) -> list[Path]:
    """スキルフォルダ内の全ファイル（scripts/ references/ 等も含む）を返す。"""
    return [p for p in skill_dir.rglob("*") if p.is_file()]


def run(check: bool) -> int:
    skills = iter_skills()
    if not skills:
        print(f"エラー: 正本スキルが見つかりません: {SRC_DIR}/<skill>/SKILL.md")
        return 1

    drift = 0
    copied = 0
    for skill in skills:
        for src in skill_files(skill):
            rel = src.relative_to(SRC_DIR)  # 例: line-stamp-builder/SKILL.md
            for tool, base in TARGET_SKILL_DIRS.items():
                dst = base / rel
                same = dst.exists() and filecmp.cmp(src, dst, shallow=False)
                if same:
                    continue
                if check:
                    print(f"  差分: [{tool}] {rel}")
                    drift += 1
                else:
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)
                    copied += 1

    if check:
        if drift:
            print(f"✗ {drift}件の差分があります。`python3 scripts/sync_skills.py` で同期してください。")
            return 1
        print(f"✓ 全ツール（{len(TARGET_SKILL_DIRS)}）のスキルが正本と一致しています。")
        return 0

    tools = "／".join(TARGET_SKILL_DIRS.keys())
    print(f"✓ 同期完了: {copied}ファイルを更新")
    print(f"  正本: {SRC_DIR.relative_to(ROOT)}/  → {tools}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="正本スキルを各AIツールのスキルディレクトリへ同期")
    parser.add_argument("--check", action="store_true",
                        help="同期済みか検証する（差分があれば非ゼロ終了）")
    args = parser.parse_args()
    return run(args.check)


if __name__ == "__main__":
    sys.exit(main())
