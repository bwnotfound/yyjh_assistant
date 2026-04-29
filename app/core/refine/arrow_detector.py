"""
绿色 ⟳ 箭头检测.

游戏精炼界面中, 中框 (当前附加属性) 和右框 (新词条) 之间会有一个绿色的
循环箭头, 它的 y 位置指示新词条要替换中框中第几行的旧词条.

策略:
    HSV 颜色阈值 → 二值化 → 形态学闭运算 → 连通域分析 → 取最大连通域中心.

为什么 HSV 而不是 RGB:
    游戏背景是带噪点的米黄色纸张, 跟绿色在 H 通道上分得开, S/V 也都不低,
    HSV 阈值开宽一点几乎不会误检. RGB 直接做阈值会被纸张的浅色噪点误命中.

为什么不直接用模板匹配:
    箭头有动画 (旋转 + 微缩放), 单模板会漏掉中间帧;
    而且需要预先采样模板, 增加标定成本. 颜色 mask 零标定.
"""

from __future__ import annotations

import logging
from typing import Optional, Sequence

import cv2
import numpy as np

log = logging.getLogger(__name__)


# 默认 HSV 范围: OpenCV 的 H 是 0~179, S/V 是 0~255.
# 草绿~翠绿, 饱和度和明度都得有一定下限, 避免命中纸张里偏黄绿的噪点.
DEFAULT_HSV_LOWER = (40, 80, 80)
DEFAULT_HSV_UPPER = (85, 255, 255)


def detect_arrow_cy(
    region_rgb: np.ndarray,
    hsv_lower: Sequence[int] = DEFAULT_HSV_LOWER,
    hsv_upper: Sequence[int] = DEFAULT_HSV_UPPER,
    min_area: int = 30,
) -> Optional[float]:
    """检测绿色箭头, 返回其在 region_rgb 内的 y 中心坐标 (像素).

    region_rgb: H x W x 3 的 RGB 数组, 通常是 ``arrow_zone`` ROI 裁出来的小图.
    返回 None 表示没检测到合规箭头.
    """
    if region_rgb is None or region_rgb.size == 0:
        return None
    if region_rgb.ndim != 3 or region_rgb.shape[2] != 3:
        log.warning("detect_arrow_cy: 非 RGB 图像 shape=%s", region_rgb.shape)
        return None

    hsv = cv2.cvtColor(region_rgb, cv2.COLOR_RGB2HSV)
    lower = np.asarray(hsv_lower, dtype=np.uint8)
    upper = np.asarray(hsv_upper, dtype=np.uint8)
    mask = cv2.inRange(hsv, lower, upper)
    # 闭运算填掉箭头内部的小空洞 (尤其是循环箭头中间的空隙)
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=2)

    n, _labels, stats, centroids = cv2.connectedComponentsWithStats(
        mask, connectivity=8
    )
    if n <= 1:
        return None
    # stats[0] 是背景, 跳过
    areas = stats[1:, cv2.CC_STAT_AREA]
    if len(areas) == 0:
        return None
    idx = int(areas.argmax()) + 1
    if stats[idx, cv2.CC_STAT_AREA] < min_area:
        log.debug(
            "detect_arrow_cy: 最大连通域过小 (%d < %d), 视为未检测",
            stats[idx, cv2.CC_STAT_AREA],
            min_area,
        )
        return None
    return float(centroids[idx, 1])


def assign_arrow_to_row(arrow_cy: float, row_cys: Sequence[float]) -> int:
    """箭头 y 跟若干行 y 中心做最近邻匹配, 返回行索引 (0-based).

    row_cys 为空时返回 -1.
    """
    if not row_cys:
        return -1
    diffs = [abs(arrow_cy - cy) for cy in row_cys]
    return int(np.argmin(diffs))
