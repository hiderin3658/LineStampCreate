#!/usr/bin/env python3
"""LINEスタンプ生成パイプライン.

白背景のシート画像（複数スタンプを格子状に配置）を入力とし、
LINE Creators Market へアップロードできる画像一式（透過PNG＋ZIP）を生成する。

処理フロー:
  1) レイアウト解析  … 余白(ガター)検出で格子を求める（割り切れない場合は均等割りにフォールバック）
  2) セル分割        … 各スタンプを切り出す（文字も含む）
  3) 背景透過        … 縁から連結した白だけを透過（フラッドフィル方式。キャラ内部の白は保持）
  4) トリミング      … 不透明領域のバウンディングボックスで余白除去
  5) リサイズ＋余白  … アスペクト比維持で縮小し、規定余白を付与（偶数・最大370x320）
  6) 最適化          … PNG保存。1MB超なら減色して1MB以下を保証
  7) メイン/タブ生成 … 代表スタンプから240x240 / 96x74を生成
  8) リネーム＆ZIP化 … 01..NN.png / main.png / tab.png を直下に並べてZIP（60MB以下）

使い方:
  python3 scripts/build_stickers.py config/Cat1.json
"""

from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter

# プロジェクトルート（このファイルの1つ上の階層）
ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# numpy ベースのマスク操作ヘルパー（scipy非依存）
# ---------------------------------------------------------------------------
def white_mask(rgb: np.ndarray, threshold: int) -> np.ndarray:
    """全チャンネルが threshold 以上の画素を「白に近い」とみなす真偽マスクを返す。"""
    return np.all(rgb >= threshold, axis=2)


def flood_from_border(mask: np.ndarray, max_iter: int | None = None) -> np.ndarray:
    """maskのTrue領域のうち、画像の縁に連結した成分だけをTrueで返す（4近傍フラッドフィル）。

    境界の白画素を種として、白マスク上を1pxずつ伝播させて外側背景を特定する。
    背景は縁全体に接して広く連結しているため、通常は少ない反復で収束する。
    """
    h, w = mask.shape
    if max_iter is None:
        max_iter = h + w  # 安全のための上限（最長経路でも収束する回数）

    seed = np.zeros_like(mask)
    seed[0, :] |= mask[0, :]
    seed[-1, :] |= mask[-1, :]
    seed[:, 0] |= mask[:, 0]
    seed[:, -1] |= mask[:, -1]

    for _ in range(max_iter):
        grown = seed.copy()
        grown[1:, :] |= seed[:-1, :]   # 上から
        grown[:-1, :] |= seed[1:, :]   # 下から
        grown[:, 1:] |= seed[:, :-1]   # 左から
        grown[:, :-1] |= seed[:, 1:]   # 右から
        grown &= mask                  # 白マスク内に制限
        if np.array_equal(grown, seed):
            break
        seed = grown
    return seed


def erode(mask: np.ndarray, px: int) -> np.ndarray:
    """4近傍でpx回収縮させる（前景の最外周pxリングを削る。白フチ除去用）。"""
    for _ in range(max(0, px)):
        e = mask.copy()
        e[1:, :] &= mask[:-1, :]
        e[:-1, :] &= mask[1:, :]
        e[:, 1:] &= mask[:, :-1]
        e[:, :-1] &= mask[:, 1:]
        mask = e
    return mask


# ---------------------------------------------------------------------------
# レイアウト解析（グリッド検出）
# ---------------------------------------------------------------------------
def find_separators(profile: np.ndarray, n_expected: int, length: int,
                    low_ratio: float = 0.01, min_gap: int = 8) -> list[int]:
    """コンテンツ率プロファイルから、低コンテンツ帯(ガター)の中央位置を返す。

    n_expected本の内部区切りを期待し、見つかったガターを幅の広い順に採用する。
    期待数に満たない場合は空リストを返し、呼び出し側で均等割りにフォールバックする。
    """
    low = profile < low_ratio
    segments: list[tuple[int, int]] = []
    start = None
    for i, v in enumerate(low):
        if v and start is None:
            start = i
        elif not v and start is not None:
            segments.append((start, i - 1))
            start = None
    if start is not None:
        segments.append((start, len(low) - 1))

    # 端のガター(画像外周の余白)は区切りではないので除外
    inner = [(s, e) for (s, e) in segments
             if (e - s + 1) >= min_gap and s > 0 and e < length - 1]
    # 幅の広い順に n_expected 本を採用し、位置順に並べ替え
    inner.sort(key=lambda se: -(se[1] - se[0]))
    chosen = sorted(inner[:n_expected], key=lambda se: se[0])
    if len(chosen) < n_expected:
        return []
    return [(s + e) // 2 for (s, e) in chosen]


def detect_grid(rgb: np.ndarray, cols: int, rows: int, threshold: int):
    """セルの境界ボックス[(left, top, right, bottom), ...]を読み順で返す。

    余白検出に成功すればコンテンツ駆動の境界、失敗時は均等割りにフォールバックする。
    """
    h, w = rgb.shape[:2]
    content = ~white_mask(rgb, threshold)
    col_profile = content.mean(axis=0)
    row_profile = content.mean(axis=1)

    col_seps = find_separators(col_profile, cols - 1, w)
    row_seps = find_separators(row_profile, rows - 1, h)

    used_fallback = []
    if not col_seps:
        col_seps = [round(w * i / cols) for i in range(1, cols)]
        used_fallback.append("列")
    if not row_seps:
        row_seps = [round(h * i / rows) for i in range(1, rows)]
        used_fallback.append("行")

    x_bounds = [0, *col_seps, w]
    y_bounds = [0, *row_seps, h]

    boxes = []
    for r in range(rows):
        for c in range(cols):
            boxes.append((x_bounds[c], y_bounds[r], x_bounds[c + 1], y_bounds[r + 1]))
    return boxes, used_fallback


# ---------------------------------------------------------------------------
# 背景透過・トリミング・リサイズ
# ---------------------------------------------------------------------------
def make_transparent(cell: Image.Image, threshold: int, feather: int) -> Image.Image:
    """セル画像の外側白背景を透過する。キャラ内部の白は保持する。"""
    rgba = np.array(cell.convert("RGBA"))
    rgb = rgba[:, :, :3].astype(np.int16)

    wmask = white_mask(rgb, threshold)
    background = flood_from_border(wmask)   # 縁に連結した白＝背景
    foreground = ~background

    if feather > 0:
        # 白フチ(ハロー)を削るため前景を収縮
        foreground = erode(foreground, feather)

    alpha = np.where(foreground, 255, 0).astype(np.uint8)
    rgba[:, :, 3] = alpha
    out = Image.fromarray(rgba, "RGBA")

    if feather > 0:
        # アルファ境界を僅かにぼかしてアンチエイリアス化
        a = out.getchannel("A").filter(ImageFilter.GaussianBlur(0.6))
        out.putalpha(a)
    return out


def trim(img: Image.Image) -> Image.Image:
    """不透明領域(alpha>0)のバウンディングボックスで切り詰める。"""
    alpha = np.array(img.getchannel("A"))
    ys, xs = np.where(alpha > 0)
    if len(xs) == 0:
        return img
    left, right = xs.min(), xs.max() + 1
    top, bottom = ys.min(), ys.max() + 1
    return img.crop((int(left), int(top), int(right), int(bottom)))


def even(n: int) -> int:
    """偶数に切り上げる（LINE仕様: 寸法は偶数）。"""
    n = int(round(n))
    return n if n % 2 == 0 else n + 1


def fit_on_canvas(content: Image.Image, max_w: int, max_h: int, margin: int) -> Image.Image:
    """コンテンツをアスペクト比維持で縮小し、余白付きの透明キャンバス中央に配置する。

    キャンバスは「コンテンツ＋上下左右margin」を偶数化したサイズ（最大 max_w x max_h）。
    拡大はしない（画質劣化を避ける）。
    """
    inner_w = max_w - 2 * margin
    inner_h = max_h - 2 * margin
    cw, ch = content.size
    scale = min(inner_w / cw, inner_h / ch, 1.0)
    new_w = max(1, round(cw * scale))
    new_h = max(1, round(ch * scale))
    resized = content.resize((new_w, new_h), Image.LANCZOS)

    canvas_w = min(even(new_w + 2 * margin), max_w)
    canvas_h = min(even(new_h + 2 * margin), max_h)
    canvas = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
    canvas.alpha_composite(resized, ((canvas_w - new_w) // 2, (canvas_h - new_h) // 2))
    return canvas


def fit_fixed(content: Image.Image, w: int, h: int) -> Image.Image:
    """固定サイズ(w x h)の透明キャンバス中央に、内側余白を持たせて配置する（メイン/タブ用）。"""
    pad = max(2, round(min(w, h) * 0.06))
    inner_w, inner_h = w - 2 * pad, h - 2 * pad
    cw, ch = content.size
    scale = min(inner_w / cw, inner_h / ch)  # 固定枠なので拡大も許可
    new_w = max(1, round(cw * scale))
    new_h = max(1, round(ch * scale))
    resized = content.resize((new_w, new_h), Image.LANCZOS)
    canvas = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    canvas.alpha_composite(resized, ((w - new_w) // 2, (h - new_h) // 2))
    return canvas


# ---------------------------------------------------------------------------
# 保存・最適化・プレビュー・ZIP
# ---------------------------------------------------------------------------
def file_kb(path: Path) -> float:
    return path.stat().st_size / 1024


def save_optimized(img: Image.Image, path: Path, max_kb: int) -> float:
    """PNG(72dpi)で保存し、max_kb超なら段階的に減色して上限以下にする。"""
    img.save(path, "PNG", optimize=True, dpi=(72, 72))
    if file_kb(path) <= max_kb:
        return file_kb(path)
    for colors in (256, 192, 128, 96, 64):
        # FASTOCTREEはRGBAのアルファを保持したまま減色できる
        q = img.convert("RGBA").quantize(colors=colors, method=Image.FASTOCTREE, dither=Image.NONE)
        q.save(path, "PNG", optimize=True, dpi=(72, 72))
        if file_kb(path) <= max_kb:
            break
    return file_kb(path)


def make_preview(stickers: list[Image.Image], cols: int, rows: int, path: Path) -> None:
    """市松模様背景に全スタンプを並べた確認用プレビュー画像を保存する（透過を可視化）。"""
    cell_w = max(s.width for s in stickers) + 16
    cell_h = max(s.height for s in stickers) + 16
    board_w, board_h = cell_w * cols, cell_h * rows

    # 市松模様の背景
    sq = 16
    bg = np.zeros((board_h, board_w, 3), dtype=np.uint8)
    yy, xx = np.mgrid[0:board_h, 0:board_w]
    checker = ((xx // sq + yy // sq) % 2).astype(bool)
    bg[checker] = 235
    bg[~checker] = 205
    board = Image.fromarray(bg, "RGB").convert("RGBA")

    for i, s in enumerate(stickers):
        r, c = divmod(i, cols)
        x = c * cell_w + (cell_w - s.width) // 2
        y = r * cell_h + (cell_h - s.height) // 2
        board.alpha_composite(s, (x, y))
    board.convert("RGB").save(path, "PNG")


def build_zip(files: list[Path], zip_path: Path) -> float:
    """指定ファイルを中間フォルダなしで直下に並べてZIP化し、サイズ(MB)を返す。"""
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in files:
            zf.write(f, arcname=f.name)
    return zip_path.stat().st_size / (1024 * 1024)


# ---------------------------------------------------------------------------
# メイン処理
# ---------------------------------------------------------------------------
def main(config_path: Path) -> int:
    cfg = json.loads(config_path.read_text(encoding="utf-8"))
    name = cfg["name"]
    cols = cfg["grid"]["cols"]
    rows = cfg["grid"]["rows"]
    count = cfg["count"]
    threshold = cfg["background"]["threshold"]
    feather = cfg["background"]["feather"]
    s_cfg = cfg["sticker"]
    max_kb = cfg["max_file_kb"]
    max_zip_mb = cfg["max_zip_mb"]

    input_path = (ROOT / cfg["input"]).resolve()
    out_dir = ROOT / "output" / name
    stickers_dir = out_dir / "stickers"
    stickers_dir.mkdir(parents=True, exist_ok=True)

    print(f"[1/8] 入力読込: {input_path}")
    sheet = Image.open(input_path).convert("RGBA")
    rgb = np.array(sheet)[:, :, :3].astype(np.int16)

    print(f"[2/8] レイアウト解析: {cols}列 x {rows}行")
    boxes, fallback = detect_grid(rgb, cols, rows, threshold)
    if fallback:
        print(f"      ⚠ {'/'.join(fallback)}の余白検出に失敗→均等割りにフォールバック")
    if len(boxes) < count:
        print(f"      ✗ セル数({len(boxes)})が個数({count})未満です")
        return 1
    boxes = boxes[:count]

    print(f"[3/8] セル分割＋[4/8] 背景透過＋[5/8] リサイズ＋[6/8] 最適化")
    final_stickers: list[Image.Image] = []
    sticker_files: list[Path] = []
    report: list[tuple[str, int, int, float]] = []
    for i, box in enumerate(boxes, start=1):
        cell = sheet.crop(box)
        transparent = make_transparent(cell, threshold, feather)
        trimmed = trim(transparent)
        sticker = fit_on_canvas(trimmed, s_cfg["max_w"], s_cfg["max_h"], s_cfg["margin"])
        final_stickers.append(sticker)

        fname = f"{i:02d}.png"
        fpath = stickers_dir / fname
        kb = save_optimized(sticker, fpath, max_kb)
        sticker_files.append(fpath)
        report.append((fname, sticker.width, sticker.height, kb))

    print(f"[7/8] メイン/タブ画像生成")
    main_src = trim(make_transparent(sheet.crop(boxes[cfg["main"]["source_index"] - 1]),
                                     threshold, feather))
    main_img = fit_fixed(main_src, *cfg["main"]["size"])
    main_path = out_dir / "main.png"
    main_kb = save_optimized(main_img, main_path, max_kb)

    tab_src = trim(make_transparent(sheet.crop(boxes[cfg["tab"]["source_index"] - 1]),
                                    threshold, feather))
    tab_img = fit_fixed(tab_src, *cfg["tab"]["size"])
    tab_path = out_dir / "tab.png"
    tab_kb = save_optimized(tab_img, tab_path, max_kb)

    # 確認用プレビュー
    preview_path = out_dir / "_preview.png"
    make_preview(final_stickers, cols, rows, preview_path)

    print(f"[8/8] ZIP化")
    zip_path = out_dir / f"{name}_line_stickers.zip"
    zip_files = [main_path, tab_path, *sticker_files]
    zip_mb = build_zip(zip_files, zip_path)

    # ---- レポート ----
    print("\n========== 生成結果 ==========")
    over = [r for r in report if r[3] > max_kb]
    for fname, w, h, kb in report:
        flag = "  ⚠超過" if kb > max_kb else ""
        print(f"  {fname}: {w}x{h}px  {kb:.0f}KB{flag}")
    print(f"  main.png: {cfg['main']['size'][0]}x{cfg['main']['size'][1]}px  {main_kb:.0f}KB")
    print(f"  tab.png:  {cfg['tab']['size'][0]}x{cfg['tab']['size'][1]}px  {tab_kb:.0f}KB")
    print(f"  ZIP: {zip_path.name}  {zip_mb:.2f}MB")
    print(f"  プレビュー: {preview_path}")
    print("------------------------------")
    print(f"  スタンプ {len(report)}枚 / メイン1 / タブ1")
    if over:
        print(f"  ⚠ 1MB超のスタンプ {len(over)}枚")
    if zip_mb > max_zip_mb:
        print(f"  ⚠ ZIPが{max_zip_mb}MBを超過")
    if not over and zip_mb <= max_zip_mb and not fallback:
        print("  ✓ すべてLINE仕様の上限内です")
    print("==============================")
    print(f"\n出力先: {out_dir}")
    print("※ アップロード前に _preview.png で透過・切り出しを目視確認してください。")
    return 0


if __name__ == "__main__":
    config_arg = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "config" / "Cat1.json"
    if not config_arg.is_absolute():
        config_arg = (ROOT / config_arg).resolve()
    sys.exit(main(config_arg))
