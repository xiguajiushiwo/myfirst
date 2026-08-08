# -*- coding: utf-8 -*-
"""托盘照片标准化：白底抠绿条 → 分出每根 → 按各自 PCB 直边掰直去畸变 → 合成标准整图。

**为什么**：真机拍照有镜头畸变(边缘条弯 20~30px、中间条弯 <10px)，"整盘一套固定坐标模板"
一形变就滑框 → 漏检(右上角)、误检(读到隔壁/标签)。本模块把每根条按它**自己的 PCB 笔直长边**
逐行掰直(不需棋盘格标定板)，四根各校各的，再并排合成到**固定标准布局**——下游 `recognize_side`
和模板/槽位/占位逻辑全部不变，只是改在标准化后的图上跑，框永远对得准。

对外只用 `canonicalize(image_path_or_bgr)`：返回标准化整图(BGR ndarray)。
"""
from __future__ import annotations

import os
from typing import Optional

import cv2
import numpy as np

# 分割：白底(S≈0)抠绿条(H 绿区、有饱和度)。可用 .env 覆盖。
_H_LO = int(os.environ.get("DESKEW_H_LO", "25"))
_H_HI = int(os.environ.get("DESKEW_H_HI", "95"))
_S_MIN = int(os.environ.get("DESKEW_S_MIN", "40"))
# 合成标准布局：每根标准宽/高(px)与根间空隙。四根共用，模板按此坐标系建。
_STICK_W = int(os.environ.get("DESKEW_STICK_W", "460"))
_STICK_H = int(os.environ.get("DESKEW_STICK_H", "2048"))
_GAP = int(os.environ.get("DESKEW_GAP", "40"))
_N_STICKS = int(os.environ.get("DESKEW_N_STICKS", "4"))
_MIN_AREA_FRAC = float(os.environ.get("DESKEW_MIN_AREA_FRAC", "0.01"))


def _segment(bgr: np.ndarray) -> np.ndarray:
    """白底抠绿条 → 二值掩膜(255=条)。闭运算补颗粒黑洞，开运算去背景噪点。"""
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    h, s, _ = cv2.split(hsv)
    mask = ((h >= _H_LO) & (h <= _H_HI) & (s >= _S_MIN)).astype(np.uint8) * 255
    k = cv2.getStructuringElement(cv2.MORPH_RECT, (15, 15))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, k, iterations=3)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, k, iterations=1)
    return mask


def _stick_contours(mask: np.ndarray, n: int = _N_STICKS) -> list[np.ndarray]:
    """找每根条的轮廓(按面积过滤碎块，左→右排序)。返回轮廓列表(供逐根建独立掩膜)。"""
    H, W = mask.shape
    cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    keep = [c for c in cnts if cv2.contourArea(c) >= _MIN_AREA_FRAC * H * W]
    keep.sort(key=lambda c: cv2.boundingRect(c)[0])   # 按左界 x 左→右
    return keep


def _edges_from_contour(cnt: np.ndarray, shape) -> tuple:
    """由**单根轮廓**填充出独占掩膜，逐行量左右边 x（不含邻根，杜绝右缘带邻根的毛病）。

    返回 (ys, left_x, right_x, (x,y,w,h))；行内像素太少的行跳过。
    """
    m = np.zeros(shape[:2], np.uint8)
    cv2.drawContours(m, [cnt], -1, 255, cv2.FILLED)
    x, y, w, h = cv2.boundingRect(cnt)
    ys, lx, rx = [], [], []
    for yy in range(y, y + h):
        row = np.where(m[yy] > 0)[0]
        if len(row) < 15:
            continue
        ys.append(yy); lx.append(float(row.min())); rx.append(float(row.max()))
    return np.array(ys, float), np.array(lx, float), np.array(rx, float), (x, y, w, h)


def _gold_at_bottom(strip_bgr: np.ndarray) -> bool:
    """判金手指(黄亮)在这根标准图的下端还是上端：比较上/下 12% 带的'黄且亮'像素占比。"""
    h = strip_bgr.shape[0]
    band = max(1, int(h * 0.12))
    hsv = cv2.cvtColor(strip_bgr, cv2.COLOR_BGR2HSV)
    hh, ss, vv = cv2.split(hsv)
    gold = ((hh >= 15) & (hh <= 40) & (ss >= 60) & (vv >= 120)).astype(np.uint8)
    top = gold[:band].mean()
    bot = gold[-band:].mean()
    return bot >= top


def _unbow_one(bgr: np.ndarray, cnt: np.ndarray, out_w: int, out_h: int,
               deg: int = 2) -> Optional[np.ndarray]:
    """把一根条按其左右 PCB 边曲线逐行水平掰直，重采样到 out_w×out_h 标准竖图，金手指统一朝下。"""
    ys, lx, rx, (x, y, w, h) = _edges_from_contour(cnt, bgr.shape)
    if len(ys) < 30:
        return None
    fl = np.polyfit(ys, lx, deg)      # 左边曲线(2次可表达桶形弧)
    fr = np.polyfit(ys, rx, deg)      # 右边曲线
    y0, y1 = ys.min(), ys.max()
    map_x = np.zeros((out_h, out_w), np.float32)
    map_y = np.zeros((out_h, out_w), np.float32)
    for i in range(out_h):
        sy = y0 + (y1 - y0) * i / (out_h - 1)         # 竖向均匀采样整根高度
        l = np.polyval(fl, sy)
        r = np.polyval(fr, sy)
        map_x[i, :] = np.linspace(l, r, out_w)        # 该行从左边→右边线性铺满标准宽
        map_y[i, :] = sy
    out = cv2.remap(bgr, map_x, map_y, cv2.INTER_LINEAR,
                    borderMode=cv2.BORDER_CONSTANT, borderValue=(255, 255, 255))
    if not _gold_at_bottom(out):                       # 方向统一：金手指朝下
        out = cv2.rotate(out, cv2.ROTATE_180)
    return out


def canonicalize(image, n_sticks: int = _N_STICKS,
                 stick_w: int = _STICK_W, stick_h: int = _STICK_H,
                 gap: int = _GAP) -> np.ndarray:
    """把托盘照片标准化：抠条→逐根掰直→按固定间隔并排合成标准整图(BGR)。

    image 可为路径或 BGR ndarray。抠不到足够根数时返回原图缩放兜底(不崩，让下游照旧跑)。
    输出尺寸固定 = n*stick_w + (n-1)*gap 宽 × stick_h 高，模板按此坐标系建立。
    """
    bgr = cv2.imread(image) if isinstance(image, str) else image
    if bgr is None:
        raise ValueError(f"读不到图像：{image!r}")
    mask = _segment(bgr)
    cnts = _stick_contours(mask, n_sticks)
    canvas_w = n_sticks * stick_w + (n_sticks - 1) * gap
    canvas = np.full((stick_h, canvas_w, 3), 255, np.uint8)
    placed = 0
    for i, cnt in enumerate(cnts[:n_sticks]):
        strip = _unbow_one(bgr, cnt, stick_w, stick_h)
        if strip is None:
            continue
        x = i * (stick_w + gap)
        canvas[:, x:x + stick_w] = strip
        placed += 1
    if placed < n_sticks:
        # 抠条不全：不硬塞，返回原图等比缩放到标准画布(下游仍能跑，只是没校正)
        h0, w0 = bgr.shape[:2]
        scale = min(canvas_w / w0, stick_h / h0)
        rs = cv2.resize(bgr, (int(w0 * scale), int(h0 * scale)))
        fb = np.full((stick_h, canvas_w, 3), 255, np.uint8)
        fb[:rs.shape[0], :rs.shape[1]] = rs
        return fb
    return canvas


def canonicalize_to_file(src: str, dst: str, **kw) -> str:
    """标准化并存盘，返回 dst。"""
    out = canonicalize(src, **kw)
    cv2.imwrite(dst, out)
    return dst
