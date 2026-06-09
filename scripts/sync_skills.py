#!/usr/bin/env python3
"""正本のスキルを各AIコーディングツールのスキルディレクトリへ同期(ミラー)する.

SKILL.md は Agent Skills のオープン標準（フォーマット共通）だが、ツールごとに
読み込むディレクトリが異なるため、同じスキルを各ディレクトリに配置する必要がある。
このスクリプトは正本 `skills/<skill>/` を各ツールの `.{tool}/skills/<skill>/` へ
ミラーする（新規/変更はコピー、正本から消えた古いファイルは削除）。

使い方:
  python3 scripts/sync_skills.py          # 各ツールのディレクトリへ同期(ミラー)
  python3 scripts/sync_skills.py --check  # 同期済みか検証（差分があれば非ゼロ終了。CI向け）

正本だけを編集し、各ツールのコピーは直接編集しないこと（このスクリプトで再生成する）。
注: シンボリックリンクは実体としてコピーされる。スキルには通常ファイルのみを置く想定。
"""

from __future__ import annotations

import argparse
import filecmp
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = ROOT / "skills"

# 同期対象から除外するファイル名（OS/エディタが生成する不要ファイル）
IGNORE_NAMES = {".DS_Store", "Thumbs.db"}

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


def files_under(directory: Path) -> list[Path]:
    """ディレクトリ配下の全ファイル（除外名を除く）を返す。"""
    if not directory.exists():
        return []
    return [p for p in directory.rglob("*") if p.is_file() and p.name not in IGNORE_NAMES]


def prune_empty_dirs(root: Path) -> None:
    """ファイル削除後に残った空ディレクトリを下位から削除する。"""
    if not root.exists():
        return
    for d in sorted((p for p in root.rglob("*") if p.is_dir()), reverse=True):
        try:
            if not any(d.iterdir()):
                d.rmdir()
        except OSError:
            pass


def validate_targets() -> str | None:
    """同期先が正本ディレクトリの内側でないことを検証する（自己コピー・暴走防止）。"""
    src = SRC_DIR.resolve()
    for tool, base in TARGET_SKILL_DIRS.items():
        if base.resolve() == src or base.resolve().is_relative_to(src):
            return f"同期先 [{tool}] {base} が正本 {SRC_DIR} の内側です。設定を見直してください。"
    return None


def run(check: bool) -> int:
    skills = iter_skills()
    if not skills:
        print(f"エラー: 正本スキルが見つかりません: {SRC_DIR}/<skill>/SKILL.md")
        return 1

    target_error = validate_targets()
    if target_error:
        print(f"エラー: {target_error}")
        return 1

    drift = 0      # --check 時の不一致件数（変更/欠落/ステール）
    copied = 0     # コピーしたファイル数
    removed = 0    # 削除したステールファイル数
    errors = 0     # 処理に失敗したファイル数

    for skill in skills:
        # 正本側のファイル（rel は "line-stamp-builder/SKILL.md" のようにスキル名を含む）
        canon_rel = {p.relative_to(SRC_DIR) for p in files_under(skill)}

        for tool, base in TARGET_SKILL_DIRS.items():
            # 1) 正本のファイルをコピー/比較
            for rel in sorted(canon_rel):
                src, dst = SRC_DIR / rel, base / rel
                try:
                    same = dst.exists() and filecmp.cmp(src, dst, shallow=False)
                except OSError as e:
                    print(f"  エラー: [{tool}] {rel} の比較に失敗: {e}")
                    errors += 1
                    continue
                if same:
                    continue
                if check:
                    print(f"  差分: [{tool}] {rel}")
                    drift += 1
                else:
                    try:
                        dst.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(src, dst)
                        copied += 1
                    except OSError as e:
                        print(f"  エラー: [{tool}] {rel} のコピーに失敗: {e}")
                        errors += 1

            # 2) 正本から消えたステールファイルを検出/削除（このスキルフォルダ配下のみ）
            target_skill_dir = base / skill.name
            for tf in files_under(target_skill_dir):
                rel = tf.relative_to(base)
                if rel in canon_rel:
                    continue
                if check:
                    print(f"  ステール: [{tool}] {rel}")
                    drift += 1
                else:
                    try:
                        tf.unlink()
                        removed += 1
                    except OSError as e:
                        print(f"  エラー: [{tool}] {rel} の削除に失敗: {e}")
                        errors += 1
            if not check:
                prune_empty_dirs(target_skill_dir)

    if check:
        if drift or errors:
            print(f"✗ 不一致 {drift}件 / エラー {errors}件。"
                  "`python3 scripts/sync_skills.py` で同期してください。")
            return 1
        print(f"✓ 全ツール（{len(TARGET_SKILL_DIRS)}）のスキルが正本と一致しています。")
        return 0

    tools = "／".join(TARGET_SKILL_DIRS.keys())
    print(f"✓ 同期完了: コピー{copied}件 / 削除{removed}件 / エラー{errors}件")
    print(f"  正本: {SRC_DIR.relative_to(ROOT)}/  → {tools}")
    return 1 if errors else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="正本スキルを各AIツールのスキルディレクトリへ同期(ミラー)")
    parser.add_argument("--check", action="store_true",
                        help="同期済みか検証する（差分やステールがあれば非ゼロ終了）")
    args = parser.parse_args()
    return run(args.check)


if __name__ == "__main__":
    sys.exit(main())
