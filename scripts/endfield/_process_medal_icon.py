"""把 参考图/deco_medal_rare.webp（白色剪影：六边形 + 右上角加号）处理成三档金属奖章图标。

1) 连通块分析去掉加号（加号是独立小块；保留最大块=六边形主体）
2) 三档金属感渐变（左上亮 → 右下暗），亮度粗量化保持平面感而非拟物
3) 六边形最右侧 + 最右下侧加薄内阴影
输出 参考图/medal_grade_1.png ~ _3.png
"""
from __future__ import annotations

import sys
from collections import deque
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "参考图" / "deco_medal_rare.webp"
OUT_DIR = ROOT / "参考图"

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def main() -> None:
    im = Image.open(SRC).convert("RGBA")
    W, H = im.size
    alpha = np.array(im)[:, :, 3]
    mask = alpha > 40

    # 1) 连通块（手写 BFS，8-连通），保留最大块 = 六边形
    visited = np.zeros_like(mask)
    labels = np.zeros_like(mask, dtype=int)
    nl = 0
    sizes: dict[int, int] = {}
    for sy in range(H):
        for sx in range(W):
            if mask[sy, sx] and not visited[sy, sx]:
                nl += 1
                q = deque([(sy, sx)])
                visited[sy, sx] = True
                cnt = 0
                while q:
                    y, x = q.popleft()
                    labels[y, x] = nl
                    cnt += 1
                    for dy in (-1, 0, 1):
                        for dx in (-1, 0, 1):
                            if dy == 0 and dx == 0:
                                continue
                            ny, nx = y + dy, x + dx
                            if 0 <= ny < H and 0 <= nx < W and mask[ny, nx] and not visited[ny, nx]:
                                visited[ny, nx] = True
                                q.append((ny, nx))
                sizes[nl] = cnt
    biggest = max(sizes, key=lambda k: sizes[k])
    hex_mask = labels == biggest
    print(f"连通块 {nl} 个；最大块 #{biggest}={sizes[biggest]}px（六边形），其余（加号 {sum(v for k,v in sizes.items() if k!=biggest)}px）已去")
    clean_alpha = np.where(hex_mask, 255, 0).astype("uint8")

    # 2) 平涂基色（整体调亮）+ 右下实心图案区高光（仅 2/3 级；1 级不做高光）+ 右/右下边缘薄阴影
    yy, xx = np.mgrid[0:H, 0:W].astype(float)
    diag = yy / H + xx / W                           # 左上 0 → 右下 2
    right_region = diag >= 1.0                         # 右下半（实心图案所在，做高光）

    def shift(m: np.ndarray, dy: int, dx: int) -> np.ndarray:
        Hh, Ww = m.shape
        s = np.zeros_like(m)
        s[max(0, dy):min(Hh, Hh + dy), max(0, dx):min(Ww, Ww + dx)] = (
            m[max(0, -dy):min(Hh, Hh - dy), max(0, -dx):min(Ww, Ww - dx)]
        )
        return s

    K = 3  # 阴影厚度（薄）
    right_inner = hex_mask & ~shift(hex_mask, 0, -K)    # 右侧内侧
    br_inner = hex_mask & ~shift(hex_mask, -K, -K)      # 右下内侧
    shadow_mask = (right_inner | br_inner) & hex_mask

    FILL, HIGHLIGHT, SHADOW_FACTOR = 0.92, 1.10, 0.78
    grades = {
        1: (95, 97, 105),     # 1 档 · 深灰（无高光）
        2: (210, 214, 222),   # 2 档 · 银白
        3: (240, 200, 82),    # 3 档 · 金
    }
    for grade, base in grades.items():
        fill = np.array(base) * FILL
        hi = np.array(base) * HIGHLIGHT
        rgb = np.empty((H, W, 3), float)
        for c in range(3):
            rgb[:, :, c] = fill[c]
        if grade != 1:  # 1 级不做高光：左上镂空与右下图案都保持平涂
            hl = hex_mask & right_region & ~shadow_mask
            for c in range(3):
                rgb[:, :, c] = np.where(hl, hi[c], rgb[:, :, c])
        for c in range(3):
            rgb[:, :, c] = np.where(shadow_mask, rgb[:, :, c] * SHADOW_FACTOR, rgb[:, :, c])
        rgb = np.clip(rgb, 0, 255).astype("uint8")
        out = np.dstack([rgb, clean_alpha])
        Image.fromarray(out).save(OUT_DIR / f"medal_grade_{grade}.png")
        print(f"  saved medal_grade_{grade}.png  base RGB={base}")

    # 纯白预览版，核对六边形完整性（加号是否去净、形状无损）
    white_rgb = np.stack([np.full((H, W), 255, "uint8")] * 3, axis=2)
    Image.fromarray(np.dstack([white_rgb, clean_alpha])).save(
        OUT_DIR / "medal_grade_clean_preview.png"
    )
    print("done")


if __name__ == "__main__":
    main()
