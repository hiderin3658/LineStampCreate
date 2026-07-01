#!/usr/bin/env python3
"""LINEスタンプ生成パイプライン.

白背景のシート画像（複数スタンプを格子状に配置）を入力とし、
LINE Creators Market へアップロードできる画像一式（透過PNG＋ZIP）を生成する。

処理フロー:
  1) レイアウト解析  … 余白(ガター)検出で格子を求める（割り切れない場合は均等割りにフォールバック）
  2) セル分割        … 各スタンプを切り出す（文字も含む）
  3) 背景透過        … 縁から連結した白だけを透過（フラッドフィル方式。キャラ内部の白は保持）
                       併せて文字グリフ内の閉じた白（漢字の囲み）だけを自動検出して透過
  4) トリミング      … 不透明領域のバウンディングボックスで余白除去
  5) リサイズ＋余白  … アスペクト比維持で縮小し、規定余白を付与（偶数・最大370x320）
  6) 最適化          … PNG保存。1MB超ならアルファを保持したままRGB減色して1MB以下を保証
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
from PIL import Image, ImageFilter, UnidentifiedImageError

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

    # バッファを使い回し（毎反復の copy() を避ける）
    grown = np.empty_like(mask)
    for _ in range(max_iter):
        grown[:] = seed
        grown[1:, :] |= seed[:-1, :]   # 上から
        grown[:-1, :] |= seed[1:, :]   # 下から
        grown[:, 1:] |= seed[:, :-1]   # 左から
        grown[:, :-1] |= seed[:, 1:]   # 右から
        grown &= mask                  # 白マスク内に制限
        if np.array_equal(grown, seed):
            break
        seed, grown = grown.copy(), seed
    return seed


def flood_fill(mask: np.ndarray, seed: np.ndarray, max_iter: int | None = None) -> np.ndarray:
    """mask(True=通行可)の上で seed を4近傍に伝播させ、seedと連結した成分をTrueで返す。

    flood_from_border と同じ反復膨張だが、種を引数で受け取る汎用版。
    連結成分の抽出（最大の塊だけ残す等）に使う。
    """
    h, w = mask.shape
    if max_iter is None:
        max_iter = h + w  # 安全のための上限（最長経路でも収束する回数）

    seed = seed & mask
    grown = np.empty_like(mask)
    for _ in range(max_iter):
        grown[:] = seed
        grown[1:, :] |= seed[:-1, :]
        grown[:-1, :] |= seed[1:, :]
        grown[:, 1:] |= seed[:, :-1]
        grown[:, :-1] |= seed[:, 1:]
        grown &= mask
        if np.array_equal(grown, seed):
            break
        seed, grown = grown.copy(), seed
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


def label_components(mask: np.ndarray) -> list[np.ndarray]:
    """maskのTrue画素を4近傍で連結成分に分割し、各成分の真偽マスクのリストで返す。

    scipy非依存。未処理画素を1つ種にしてフラッドフィルで1成分を取り出す操作を、
    全画素が振り分けられるまで繰り返す。文字グリフ・被写体などの塊の分離に使う。
    """
    remaining = mask.copy()
    components: list[np.ndarray] = []
    grown = np.empty_like(mask)
    while remaining.any():
        ys, xs = np.where(remaining)
        seed = np.zeros_like(mask)
        seed[ys[0], xs[0]] = True
        while True:
            grown[:] = seed
            grown[1:, :] |= seed[:-1, :]
            grown[:-1, :] |= seed[1:, :]
            grown[:, 1:] |= seed[:, :-1]
            grown[:, :-1] |= seed[:, 1:]
            grown &= remaining
            if np.array_equal(grown, seed):
                break
            seed = grown.copy()
        components.append(seed)
        remaining &= ~seed
    return components


# ---------------------------------------------------------------------------
# レイアウト解析（グリッド検出）
# ---------------------------------------------------------------------------
def find_gutters(profile: np.ndarray, length: int,
                 low_ratio: float = 0.01, min_gap: int = 8) -> list[tuple[int, int]]:
    """コンテンツ率プロファイルから、内部の低コンテンツ帯(ガター)を [(start, end), ...] で返す。

    一定幅(min_gap)以上の低コンテンツ区間のうち、画像外周の余白(端に接する帯)は除外する。
    グリッドの列数・行数の自動推定（ガター数＋1）にも利用する。
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
    return [(s, e) for (s, e) in segments
            if (e - s + 1) >= min_gap and s > 0 and e < length - 1]


def find_separators(profile: np.ndarray, n_expected: int, length: int,
                    low_ratio: float = 0.01, min_gap: int = 8) -> list[int]:
    """コンテンツ率プロファイルから、低コンテンツ帯(ガター)の中央位置を返す。

    n_expected本の内部区切りを期待し、見つかったガターを幅の広い順に採用する。
    期待数に満たない場合は空リストを返し、呼び出し側で均等割りにフォールバックする。
    """
    if n_expected <= 0:
        return []
    inner = find_gutters(profile, length, low_ratio, min_gap)
    # 幅の広い順に n_expected 本を採用し、位置順に並べ替え
    inner.sort(key=lambda se: -(se[1] - se[0]))
    chosen = sorted(inner[:n_expected], key=lambda se: se[0])
    if len(chosen) < n_expected:
        return []
    return [(s + e) // 2 for (s, e) in chosen]


def detect_grid(rgb: np.ndarray, cols: int, rows: int, threshold: int,
                content: np.ndarray | None = None):
    """セルの境界ボックス[(left, top, right, bottom), ...]を読み順で返す。

    余白検出に成功すればコンテンツ駆動の境界、失敗時は均等割りにフォールバックする。
    content を渡すとそれを「コンテンツ画素」として使う（透過オフ時はアルファ由来の
    マスクを渡す）。未指定なら白背景前提で rgb から非白をコンテンツとみなす。
    """
    h, w = rgb.shape[:2]
    if content is None:
        content = ~white_mask(rgb, threshold)
    col_profile = content.mean(axis=0)
    row_profile = content.mean(axis=1)

    # ガターは基本 low_ratio=0.01 で検出するが、効果線・装飾が薄く跨いで期待本数に
    # 満たない場合は段階的にしきい値を上げて再検出する（均等割りは最後の手段）。
    def seps_adaptive(profile, n_expected: int, length: int) -> list[int]:
        if n_expected <= 0:
            return []
        for low_ratio in (0.01, 0.02, 0.03, 0.05, 0.08):
            seps = find_separators(profile, n_expected, length, low_ratio=low_ratio)
            if seps:
                return seps
        return []

    col_seps = seps_adaptive(col_profile, cols - 1, w)
    row_seps = seps_adaptive(row_profile, rows - 1, h)

    used_fallback = []
    # 区切りが必要(2分割以上)なのに検出できなかった軸だけフォールバック
    if cols > 1 and not col_seps:
        col_seps = [round(w * i / cols) for i in range(1, cols)]
        used_fallback.append("列")
    if rows > 1 and not row_seps:
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


def remove_text_counters(img: Image.Image, white_th: int = 244, dark_th: int = 150,
                         text_dark_frac: float = 0.45, max_text_ratio: float = 0.12,
                         min_text_area: int = 80) -> Image.Image:
    """文字グリフ内部の『閉じた白』（漢字の囲み）だけを狙って透過する。

    背景透過は縁に連結した白しか消せないため、漢字の囲み（日・口・欲の谷など、
    縁から閉じた純白）が白く残る。一方その囲みは、白い装飾（鯉のぼり等）や被写体の
    白（猫の体・服の縞・目のハイライト）と画素単位では同じ純白で区別がつかない。

    そこで前景を連結成分に分け、『最大成分=被写体を除外』『小さく・大半が暗い成分=文字』
    だけを文字領域とみなし、その内部の閉じた白のみを消す。これにより白い装飾や被写体の
    白を巻き込まずに囲みだけを同化できる。
    """
    arr = np.array(img.convert("RGBA"))
    rgb = arr[:, :, :3].astype(np.int16)
    alpha = arr[:, :, 3].copy()
    opaque = alpha > 128
    if not opaque.any():
        return img

    lum = 0.299 * rgb[:, :, 0] + 0.587 * rgb[:, :, 1] + 0.114 * rgb[:, :, 2]
    dark = (lum < dark_th) & opaque

    h, w = opaque.shape
    cell_area = h * w
    components = label_components(opaque)
    largest = max(int(c.sum()) for c in components)

    text_mask = np.zeros_like(opaque)
    for c in components:
        area = int(c.sum())
        if area == largest:
            continue                            # 最大成分=被写体 → 除外
        if area < min_text_area or area > max_text_ratio * cell_area:
            continue                            # 微小ノイズ / 大きい絵 → 除外
        if dark[c].mean() < text_dark_frac:     # 成分の大半が暗い＝文字グリフ
            continue                            # 鯉・葉など白/有彩オブジェクト → 除外
        text_mask |= c

    if not text_mask.any():
        return img

    whiteish = np.all(rgb >= white_th, axis=2) & opaque
    transparent = alpha == 0
    open_white = flood_from_border(whiteish | transparent) & whiteish
    counters = whiteish & ~open_white & text_mask  # 文字内の閉じた純白＝囲み
    if not counters.any():
        return img

    alpha[counters] = 0
    arr[:, :, 3] = alpha
    out = Image.fromarray(arr, "RGBA")
    # 透過化した囲みの縁を僅かにぼかしてアンチエイリアス化
    a = out.getchannel("A").filter(ImageFilter.GaussianBlur(0.4))
    out.putalpha(a)
    return out


def trim(img: Image.Image) -> Image.Image:
    """不透明領域(alpha>0)のバウンディングボックスで切り詰める。

    完全に透明（コンテンツ無し）の場合は 1x1 の透明画像を返す（呼び出し側で空セルを検知できる）。
    """
    alpha = np.array(img.getchannel("A"))
    ys, xs = np.where(alpha > 0)
    if len(xs) == 0:
        return Image.new("RGBA", (1, 1), (0, 0, 0, 0))
    left, right = xs.min(), xs.max() + 1
    top, bottom = ys.min(), ys.max() + 1
    return img.crop((int(left), int(top), int(right), int(bottom)))


def is_empty(img: Image.Image) -> bool:
    """画像が実質的に空（不透明画素がほぼ無い）かを判定する。"""
    return img.size == (1, 1) or np.array(img.getchannel("A")).max() == 0


def isolate_subject(img: Image.Image) -> Image.Image:
    """最大の連結成分（＝キャラ本体）だけを残し、文字や離れた装飾を除去する。

    make_transparent はキャラ内部の白を保持するため、キャラは1つの大きな連結成分になる。
    一方、上部の文字や離れた効果線・キラキラ・吹き出しは別成分になる。前景を連結成分に
    分け、最大成分（キャラ本体）だけを残してそれ以外を透明化する。
    主にタブ画像をキャラのみにする用途。最大成分が前景のほぼ全部（＝文字・装飾が無い）
    なら元画像を返す。
    """
    rgba = np.array(img.convert("RGBA"))
    fg = rgba[:, :, 3] > 0
    if not fg.any():
        return img

    components = label_components(fg)
    if not components:
        return img
    largest = max(components, key=lambda c: int(c.sum()))
    # 最大成分が前景のほぼ全部＝文字・装飾が無い（実質1成分）→そのまま
    if int(largest.sum()) >= fg.sum() * 0.995:
        return img

    out = rgba.copy()
    out[:, :, 3] = np.where(largest, rgba[:, :, 3], 0)
    return trim(Image.fromarray(out, "RGBA"))


def crop_top_fraction(img: Image.Image, frac: float) -> Image.Image:
    """上部 frac（0〜1）分を透明化して切り落とし、顔まわりに寄せる。

    文字がキャラ本体と接触して1成分に融合する絵柄（文字を頭の上に重ねる構図）では
    isolate_subject で文字を分離できない。その場合に、文字帯のある上部を割合で切り、
    顔のクローズアップにする。主にタブ画像用。
    """
    if not frac or frac <= 0:
        return img
    arr = np.array(img.convert("RGBA"))
    h = arr.shape[0]
    cut = min(h - 1, int(round(h * frac)))
    arr[:cut, :, 3] = 0
    return trim(Image.fromarray(arr, "RGBA"))


def even(n: int) -> int:
    """偶数に切り上げる（LINE仕様: 寸法は偶数）。"""
    n = int(round(n))
    return n if n % 2 == 0 else n + 1


def _resize_center(content: Image.Image, new_w: int, new_h: int,
                   canvas_w: int, canvas_h: int) -> Image.Image:
    """コンテンツを (new_w, new_h) に縮小し、(canvas_w, canvas_h) の透明キャンバス中央へ配置。"""
    resized = content.resize((max(1, new_w), max(1, new_h)), Image.LANCZOS)
    canvas = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
    canvas.alpha_composite(resized, ((canvas_w - resized.width) // 2,
                                     (canvas_h - resized.height) // 2))
    return canvas


def fit_on_canvas(content: Image.Image, max_w: int, max_h: int, margin: int) -> Image.Image:
    """アスペクト比維持で縮小し、余白付きの透明キャンバス中央に配置する（スタンプ用）。

    キャンバスは「コンテンツ＋上下左右margin」を偶数化したサイズ（最大 max_w x max_h）。
    拡大はしない（画質劣化を避ける）。
    """
    inner_w = max_w - 2 * margin
    inner_h = max_h - 2 * margin
    cw, ch = content.size
    scale = min(inner_w / cw, inner_h / ch, 1.0)
    new_w = max(1, round(cw * scale))
    new_h = max(1, round(ch * scale))
    canvas_w = min(even(new_w + 2 * margin), max_w)
    canvas_h = min(even(new_h + 2 * margin), max_h)
    return _resize_center(content, new_w, new_h, canvas_w, canvas_h)


def fit_fixed(content: Image.Image, w: int, h: int) -> Image.Image:
    """固定サイズ(w x h)の透明キャンバス中央に、内側余白を持たせて配置する（メイン/タブ用）。"""
    pad = max(2, round(min(w, h) * 0.06))
    inner_w, inner_h = w - 2 * pad, h - 2 * pad
    cw, ch = content.size
    scale = min(inner_w / cw, inner_h / ch)  # 固定枠なので拡大も許可
    new_w = max(1, round(cw * scale))
    new_h = max(1, round(ch * scale))
    return _resize_center(content, new_w, new_h, w, h)


# ---------------------------------------------------------------------------
# 保存・最適化・プレビュー・ZIP
# ---------------------------------------------------------------------------
def file_kb(path: Path) -> float:
    return path.stat().st_size / 1024


def save_optimized(img: Image.Image, path: Path, max_kb: int) -> float:
    """PNG(72dpi)で保存し、max_kb超ならアルファを保持したままRGBを減色して上限以下にする。"""
    img.save(path, "PNG", optimize=True, dpi=(72, 72))
    kb = file_kb(path)
    if kb <= max_kb:
        return kb
    # 1MB超: 滑らかなアルファ(縁のアンチエイリアス)を保持するため、RGBのみ減色して再合成する
    alpha = img.getchannel("A")
    for colors in (256, 192, 128, 96, 64):
        rgb_q = img.convert("RGB").quantize(colors=colors, method=Image.FASTOCTREE,
                                            dither=Image.NONE)
        reduced = rgb_q.convert("RGBA")
        reduced.putalpha(alpha)
        reduced.save(path, "PNG", optimize=True, dpi=(72, 72))
        kb = file_kb(path)
        if kb <= max_kb:
            break
    return kb


def make_preview(stickers: list[Image.Image], cols: int, rows: int, path: Path) -> None:
    """市松模様背景に全スタンプを並べた確認用プレビュー画像を保存する（透過を可視化）。"""
    if not stickers:
        return
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
# 設定検証
# ---------------------------------------------------------------------------
def validate(cfg: dict, input_path: Path) -> list[str]:
    """設定値・入力を検証し、エラーメッセージのリストを返す（空なら問題なし）。"""
    errors: list[str] = []
    for key in ("name", "grid", "count", "background", "sticker", "main", "tab",
                "max_file_kb", "max_zip_mb"):
        if key not in cfg:
            errors.append(f"設定キー '{key}' がありません")
    if errors:
        return errors

    cols, rows = cfg["grid"].get("cols"), cfg["grid"].get("rows")
    count = cfg["count"]
    if not isinstance(cols, int) or cols < 1:
        errors.append(f"grid.cols は1以上の整数が必要です（現在: {cols}）")
    if not isinstance(rows, int) or rows < 1:
        errors.append(f"grid.rows は1以上の整数が必要です（現在: {rows}）")
    if isinstance(cols, int) and isinstance(rows, int) and cols >= 1 and rows >= 1:
        if not (1 <= count <= cols * rows):
            errors.append(f"count は 1〜{cols * rows}(=cols*rows) の範囲が必要です（現在: {count}）")
        for label in ("main", "tab"):
            idx = cfg[label].get("source_index")
            if not isinstance(idx, int) or not (1 <= idx <= count):
                errors.append(f"{label}.source_index は 1〜{count} の範囲が必要です（現在: {idx}）")
    th = cfg["background"].get("threshold")
    if not isinstance(th, int) or not (0 <= th <= 255):
        errors.append(f"background.threshold は 0〜255 が必要です（現在: {th}）")
    if not input_path.exists():
        errors.append(f"入力画像が見つかりません: {input_path}")
    return errors


# ---------------------------------------------------------------------------
# メイン処理
# ---------------------------------------------------------------------------
def main(config_path: Path) -> int:
    if not config_path.exists():
        print(f"エラー: 設定ファイルが見つかりません: {config_path}")
        return 1
    try:
        cfg = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"エラー: 設定ファイルのJSONが不正です: {e}")
        return 1

    input_path = (ROOT / cfg.get("input", "")).resolve()
    errors = validate(cfg, input_path)
    if errors:
        print("エラー: 設定または入力に問題があります:")
        for e in errors:
            print(f"  - {e}")
        return 1

    name = cfg["name"]
    cols, rows = cfg["grid"]["cols"], cfg["grid"]["rows"]
    count = cfg["count"]
    threshold = cfg["background"]["threshold"]
    feather = cfg["background"]["feather"]
    # 漢字の囲み（縁から閉じた純白）の透過。既定で有効、setごとに無効化可。
    clean_counters = cfg["background"].get("text_counter_cleanup", True)
    # 透過モード: "white"(既定)=白背景から透過を作る / "preserve"=入力の既存アルファを保持（透過処理オフ）
    preserve_alpha = cfg["background"].get("mode", "white") == "preserve"
    s_cfg = cfg["sticker"]
    main_cfg, tab_cfg = cfg["main"], cfg["tab"]
    max_kb = cfg["max_file_kb"]
    max_zip_mb = cfg["max_zip_mb"]

    out_dir = ROOT / "output" / name
    stickers_dir = out_dir / "stickers"
    stickers_dir.mkdir(parents=True, exist_ok=True)

    print(f"[1/8] 入力読込: {input_path}")
    try:
        sheet = Image.open(input_path).convert("RGBA")
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as e:
        print(f"エラー: 画像を読み込めません: {input_path} ({e})")
        return 1

    sheet_alpha = np.array(sheet.getchannel("A"))
    if not preserve_alpha and (sheet_alpha < 250).mean() > 0.05:
        # 白背景前提のチェック（既に大きく透過している入力は想定外）
        print("      ⚠ 入力画像が既に透過部分を多く含みます。透過済みなら background.mode=\"preserve\" を検討してください。")

    rgb = np.array(sheet)[:, :, :3].astype(np.int16)

    print(f"[2/8] レイアウト解析: {cols}列 x {rows}行" + ("（透過オフ＝アルファ保持）" if preserve_alpha else ""))
    # 透過オフ時は『透明＝コマの隙間』としてアルファでグリッド検出。通常は白背景から検出。
    content_mask = (sheet_alpha > 8) if preserve_alpha else None
    boxes, fallback = detect_grid(rgb, cols, rows, threshold, content=content_mask)
    if fallback:
        print(f"      ⚠ {'/'.join(fallback)}の余白検出に失敗→均等割りにフォールバック")
    boxes = boxes[:count]

    # 入力解像度チェック（作る前の警告）
    # fit_on_canvas は拡大しない（画質劣化を避ける）ため、1セルが規定枠より小さいと
    # スタンプが上限(max_w x max_h)に届かず、他セットより小さく仕上がる。
    inner_w = s_cfg["max_w"] - 2 * s_cfg["margin"]
    inner_h = s_cfg["max_h"] - 2 * s_cfg["margin"]
    min_cell_w = min(box[2] - box[0] for box in boxes)
    min_cell_h = min(box[3] - box[1] for box in boxes)
    if min_cell_w < inner_w or min_cell_h < inner_h:
        print(f"      ⚠ 入力解像度が低い可能性: 1セル最小 約{min_cell_w}x{min_cell_h}px が"
              f"規定枠 {inner_w}x{inner_h}px 未満です。")
        print(f"        拡大はしない仕様のため、スタンプが上限 {s_cfg['max_w']}x{s_cfg['max_h']}px に"
              f"届かず小さく仕上がる場合があります。")
        print(f"        規定サイズいっぱいにするには、入力シートを"
              f"1セルあたり {inner_w}x{inner_h}px 以上（目安: {inner_w * cols}x{inner_h * rows}px 以上）"
              f"の高解像度で用意してください。")

    print("[3/8] セル分割＋[4/8] 背景透過＋[5/8] リサイズ＋[6/8] 最適化")
    trimmed_list: list[Image.Image] = []   # メイン/タブ生成で再利用（透過処理の重複を避ける）
    final_stickers: list[Image.Image] = []
    sticker_files: list[Path] = []
    report: list[tuple[str, int, int, float]] = []
    empty_cells: list[int] = []
    for i, box in enumerate(boxes, start=1):
        cell = sheet.crop(box)
        if preserve_alpha:
            # 透過オフ: 入力の既存アルファをそのまま使う（背景透過・囲み白除去・フェザーを行わない）
            transparent = cell
        else:
            transparent = make_transparent(cell, threshold, feather)
            if clean_counters:
                # 文字の囲み（背景透過では消せない閉じた純白）を同化
                transparent = remove_text_counters(transparent, white_th=max(244, threshold))
        trimmed = trim(transparent)
        trimmed_list.append(trimmed)
        if is_empty(trimmed):
            empty_cells.append(i)

        sticker = fit_on_canvas(trimmed, s_cfg["max_w"], s_cfg["max_h"], s_cfg["margin"])
        final_stickers.append(sticker)

        fname = f"{i:02d}.png"
        fpath = stickers_dir / fname
        kb = save_optimized(sticker, fpath, max_kb)
        sticker_files.append(fpath)
        report.append((fname, sticker.width, sticker.height, kb))

    print("[7/8] メイン/タブ画像生成")
    # isolate_subject=true なら文字を除いてキャラ本体だけを枠に収める（タブ画像向け）
    main_src = trimmed_list[main_cfg["source_index"] - 1]
    if main_cfg.get("isolate_subject"):
        main_src = isolate_subject(main_src)
    if main_cfg.get("crop_top"):
        main_src = crop_top_fraction(main_src, main_cfg["crop_top"])
    main_img = fit_fixed(main_src, *main_cfg["size"])
    main_path = out_dir / "main.png"
    main_kb = save_optimized(main_img, main_path, max_kb)

    tab_src = trimmed_list[tab_cfg["source_index"] - 1]
    if tab_cfg.get("isolate_subject"):
        tab_src = isolate_subject(tab_src)
    if tab_cfg.get("crop_top"):
        tab_src = crop_top_fraction(tab_src, tab_cfg["crop_top"])
    tab_img = fit_fixed(tab_src, *tab_cfg["size"])
    tab_path = out_dir / "tab.png"
    tab_kb = save_optimized(tab_img, tab_path, max_kb)

    # 確認用プレビュー
    preview_path = out_dir / "_preview.png"
    make_preview(final_stickers, cols, rows, preview_path)

    print("[8/8] ZIP化")
    zip_path = out_dir / f"{name}_line_stickers.zip"
    zip_files = [main_path, tab_path, *sticker_files]
    zip_mb = build_zip(zip_files, zip_path)

    # ---- レポート ----
    print("\n========== 生成結果 ==========")
    over = [r for r in report if r[3] > max_kb]
    for fname, w, h, kb in report:
        flag = "  ⚠超過" if kb > max_kb else ""
        print(f"  {fname}: {w}x{h}px  {kb:.0f}KB{flag}")
    print(f"  main.png: {main_cfg['size'][0]}x{main_cfg['size'][1]}px  {main_kb:.0f}KB")
    print(f"  tab.png:  {tab_cfg['size'][0]}x{tab_cfg['size'][1]}px  {tab_kb:.0f}KB")
    print(f"  ZIP: {zip_path.name}  {zip_mb:.2f}MB")
    print(f"  プレビュー: {preview_path}")
    print("------------------------------")
    print(f"  スタンプ {len(report)}枚 / メイン1 / タブ1")
    if empty_cells:
        print(f"  ⚠ 空(透過失敗の可能性)のスタンプ: {empty_cells}")
    if over:
        print(f"  ⚠ 1MB超のスタンプ {len(over)}枚")
    if zip_mb > max_zip_mb:
        print(f"  ⚠ ZIPが{max_zip_mb}MBを超過")
    if not over and zip_mb <= max_zip_mb and not fallback and not empty_cells:
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
