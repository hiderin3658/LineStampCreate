#!/usr/bin/env python3
"""シート画像からグリッド・個数を自動推定し、build_stickers.py 用の設定JSONを生成する.

白背景に複数スタンプを格子配置したシート画像を解析し、
余白(ガター)検出で列数・行数・個数を推定して config/<name>.json を出力する。
推定後は build_stickers.py に渡すだけでスタンプ一式を生成できる。

使い方:
  python3 scripts/init_config.py input/Stamp_Cat2.png
  python3 scripts/init_config.py input/Stamp_Cat2.png --name Cat2
  python3 scripts/init_config.py input/Stamp_Cat2.png --force    # 既存configを上書き
  python3 scripts/init_config.py input/Stamp_Cat2.png --dry-run  # 書き込まず推定結果のみ表示
"""

from __future__ import annotations

import argparse
import copy
import json
import re
import sys
from pathlib import Path

import numpy as np
from PIL import Image, UnidentifiedImageError

# scripts/ を import パスに追加して build_stickers のロジックを再利用する
sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_stickers as bs  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent

# LINEで選択できる正規のスタンプ個数
VALID_COUNTS = (8, 16, 24, 32, 40)

# 設定の既定値（画像生成に関わる技術パラメータのみ。個人情報は含めない）
DEFAULTS = {
    "background": {"threshold": 240, "feather": 1, "text_counter_cleanup": True},
    "sticker": {"max_w": 370, "max_h": 320, "margin": 10},
    "main": {"source_index": 1, "size": [240, 240]},
    "tab": {"source_index": 1, "size": [96, 74]},
    "max_file_kb": 1024,
    "max_zip_mb": 60,
}


def derive_name(image_path: Path) -> str:
    """ファイル名から作品名を導出する（例: Stamp_Cat2.png -> Cat2）。"""
    stem = image_path.stem
    stem = re.sub(r"^[Ss]tamp[_-]?", "", stem)  # 先頭の "Stamp_" を除去
    return stem or image_path.stem


def validate_name(name: str, config_dir: Path) -> str | None:
    """name が config ディレクトリ内の安全なファイル名かを検証する。

    問題があればエラーメッセージを、なければ None を返す。
    パス区切り・絶対パス・`..` 等によるディレクトリ外への書き込みを防ぐ。
    """
    if not name or name in (".", ".."):
        return f"作品名が不正です（空または '.'）: {name!r}"
    # 生成されるパスが config ディレクトリ直下に収まるかで判定（トラバーサル/絶対パス対策）
    candidate = (config_dir / f"{name}.json").resolve()
    if candidate.parent != config_dir.resolve():
        return (f"作品名にパス区切りなどの使用できない文字が含まれています: {name!r}"
                "（半角英数字・ハイフン・アンダースコアを推奨）")
    return None


def detect_grid_size(image_path: Path, threshold: int):
    """画像を解析して (cols, rows, col_gutters, row_gutters, (w, h)) を推定する。"""
    img = Image.open(image_path).convert("RGBA")
    rgb = np.array(img)[:, :, :3].astype(np.int16)
    h, w = rgb.shape[:2]
    content = ~bs.white_mask(rgb, threshold)

    col_gutters = bs.find_gutters(content.mean(axis=0), w)
    row_gutters = bs.find_gutters(content.mean(axis=1), h)
    cols = len(col_gutters) + 1
    rows = len(row_gutters) + 1
    return cols, rows, len(col_gutters), len(row_gutters), (w, h)


def build_config(name: str, rel_input: str, cols: int, rows: int, threshold: int) -> dict:
    """推定結果と既定値から設定辞書を組み立てる。"""
    # ネストした辞書を共有しないよう deepcopy する（DEFAULTS の破壊防止）
    cfg = {
        "name": name,
        "input": rel_input,
        "grid": {"cols": cols, "rows": rows},
        "count": cols * rows,
        **copy.deepcopy(DEFAULTS),
    }
    cfg["background"]["threshold"] = threshold  # 検出に使ったしきい値を設定にも反映
    return cfg


def main() -> int:
    parser = argparse.ArgumentParser(description="シート画像から設定JSONを自動生成する")
    parser.add_argument("image", help="入力シート画像のパス（例: input/Stamp_Cat2.png）")
    parser.add_argument("--name", help="作品名（省略時はファイル名から推定）")
    parser.add_argument("--threshold", type=int, default=240, help="白背景判定のしきい値(0-255)")
    parser.add_argument("--force", action="store_true", help="既存の config を上書きする")
    parser.add_argument("--dry-run", action="store_true", help="ファイルを書き込まず推定結果のみ表示")
    args = parser.parse_args()

    image_path = Path(args.image)
    if not image_path.is_absolute():
        image_path = (ROOT / image_path).resolve()
    if not image_path.exists():
        print(f"エラー: 画像が見つかりません: {image_path}")
        return 1

    # 作品名を確定し、安全性を検証（書き込み先がconfigディレクトリ外にならないように）
    name = args.name or derive_name(image_path)
    config_dir = ROOT / "config"
    name_error = validate_name(name, config_dir)
    if name_error:
        print(f"エラー: {name_error}")
        return 1

    try:
        cols, rows, n_col, n_row, (w, h) = detect_grid_size(image_path, args.threshold)
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as e:
        print(f"エラー: 画像を読み込めません: {image_path} ({e})")
        return 1

    count = cols * rows

    # 入力パスはリポジトリルートからの相対で保存（移植性のため）
    try:
        rel_input = image_path.relative_to(ROOT).as_posix()
    except ValueError:
        rel_input = str(image_path)

    cfg = build_config(name, rel_input, cols, rows, args.threshold)

    print("========== グリッド自動推定 ==========")
    print(f"  画像: {image_path.name}  ({w}x{h}px)")
    print(f"  検出ガター: 縦{n_col}本 / 横{n_row}本")
    print(f"  推定グリッド: {cols}列 x {rows}行  = {count}個")
    if count not in VALID_COUNTS:
        print(f"  ⚠ 個数 {count} はLINEの規定({'/'.join(map(str, VALID_COUNTS))})外です。"
              "グリッド検出を見直すか、config の grid/count を手動調整してください。")
    if n_col == 0 or n_row == 0:
        print("  ⚠ 片方の軸でガターを検出できませんでした（列または行が1）。"
              "白背景・格子配置の画像か、--threshold を確認してください。")
    print("=====================================")

    if args.dry_run:
        print(json.dumps(cfg, ensure_ascii=False, indent=2))
        return 0

    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / f"{name}.json"
    if config_path.exists() and not args.force:
        print(f"エラー: {config_path.name} は既に存在します。"
              "上書きするには --force を付けるか、手動で編集してください。")
        return 1
    config_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"設定を書き出しました: {config_path}")
    print("次のコマンドでスタンプ一式を生成できます:")
    print(f"  python3 scripts/build_stickers.py config/{name}.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
