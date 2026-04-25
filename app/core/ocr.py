"""
TemplateOCR - 模板匹配 OCR

适用场景：
  - 字体固定（屏幕字体不会变）
  - 字符集小（< 20 个字符）
  - 背景有纹理但前景字色稳定

管线：
  RGB → cvtColor(GRAY) → adaptiveThreshold(BINARY_INV) →
  per-glyph cv2.matchTemplate(NCC) → x-center NMS → join

主要类：
  - TemplateGlyph: 单字符模板的数据类
  - TemplateOCR : 加载模板 + 识别 ROI
  - CoordReader : 包装 mumu 截图 + crop + 正则解析为 (x, y)
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import cv2
import numpy as np
from PIL import Image

log = logging.getLogger(__name__)


# 模板文件名 → 字符的映射
# 文件名用纯字母/数字，避免 ( ) , 在路径里的转义麻烦
_NAME_TO_CHAR: dict[str, str] = {
    "0": "0",
    "1": "1",
    "2": "2",
    "3": "3",
    "4": "4",
    "5": "5",
    "6": "6",
    "7": "7",
    "8": "8",
    "9": "9",
    "lparen": "(",
    "rparen": ")",
    "comma": ",",
}


# =============================================================================
# 二值化
# =============================================================================


def binarize(
    img: np.ndarray,
    block_size: int = 11,
    C: int = 2,
) -> np.ndarray:
    """
    转灰度 + OTSU 全局阈值二值化（INV：暗字 → 亮前景）。

    为什么不用 adaptiveThreshold:
        adaptiveThreshold 用 block_size 的局部窗口算阈值，模板（小）和 ROI（大）
        在同一个 block_size 下行为不一致——模板上 11x11 窗口几乎覆盖整图，
        等于全局均值；ROI 上同样窗口是真正的局部。结果：同一字符在不同上下文
        （比如 "(11,21)" 中两个 1 vs "(14,22)" 中两个 2）二值化形态会变，
        matchTemplate score 因此漂移。

    OTSU 对整图做一次直方图分析找最优阈值，模板和 ROI 各自算一个全局阈值，
    但同一图内所有像素都用相同标准，字符形态稳定。

    block_size, C 参数保留以兼容旧签名，OTSU 模式下不使用。
    """
    if img.ndim == 3:
        gray = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
    elif img.ndim == 2:
        gray = img
    else:
        raise ValueError(f"图像维度异常: {img.shape}")
    _, bin_img = cv2.threshold(
        gray,
        0,
        255,
        cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU,
    )
    return bin_img


# =============================================================================
# 数据
# =============================================================================


@dataclass
class TemplateGlyph:
    """单个字符模板"""

    char: str
    bin_image: np.ndarray  # uint8, 0 或 255

    @property
    def height(self) -> int:
        return self.bin_image.shape[0]

    @property
    def width(self) -> int:
        return self.bin_image.shape[1]


# =============================================================================
# OCR
# =============================================================================


class TemplateOCR:
    """
    模板匹配 OCR。

    构造时给一组已二值化好的字符模板。recognize() 输入 RGB ROI，
    输出拼接后的字符串（可能包含识别噪声，调用方再用正则提取）。
    """

    def __init__(
        self,
        glyphs: list[TemplateGlyph],
        score_threshold: float = 0.78,
        binarize_block: int = 11,
        binarize_C: int = 2,
        nms_box_factor: float = 0.6,
    ) -> None:
        if not glyphs:
            raise ValueError("TemplateOCR 至少需要 1 个模板")
        self.glyphs = glyphs
        self.score_threshold = score_threshold
        self.binarize_block = binarize_block
        self.binarize_C = binarize_C
        # NMS 距离判据：两候选 cx 距离 < (w_a + w_b)/2 * factor 视为冲突。
        # 物理含义：两个候选的 bbox 在 x 方向重叠超过 (1 - factor) * 平均宽度。
        # factor=0.6 让宽度差异大的相邻字符（如 "1" 6px 紧贴 "0" 16px）不误杀，
        # 但同位置抖动响应（同一字符多次匹配、相似字形互相干扰）能去掉。
        self.nms_box_factor = nms_box_factor
        log.info(
            "TemplateOCR 就绪: glyphs=%d (chars=%s), score_threshold=%.2f, nms_box_factor=%.2f",
            len(glyphs),
            "".join(g.char for g in glyphs),
            self.score_threshold,
            self.nms_box_factor,
        )

    @classmethod
    def from_dir(
        cls,
        template_dir: Path,
        **kwargs,
    ) -> "TemplateOCR":
        """
        从目录加载模板。文件命名见 _NAME_TO_CHAR。
        模板加载时会按构造参数中的二值化方法预处理。
        """
        block = kwargs.get("binarize_block", 11)
        C = kwargs.get("binarize_C", 2)
        glyphs: list[TemplateGlyph] = []
        missing: list[str] = []
        for fname, char in _NAME_TO_CHAR.items():
            path = template_dir / f"{fname}.png"
            if not path.exists():
                missing.append(fname)
                continue
            arr = np.array(Image.open(path).convert("RGB"))
            bin_img = binarize(arr, block, C)
            glyphs.append(TemplateGlyph(char=char, bin_image=bin_img))
        if not glyphs:
            raise FileNotFoundError(
                f"模板目录 {template_dir} 没找到任何模板（期望文件名: "
                f"{list(_NAME_TO_CHAR)}）"
            )
        if missing:
            log.warning("模板缺失（识别可能受影响）: %s", missing)
        return cls(glyphs, **kwargs)

    def recognize(self, roi_rgb: np.ndarray) -> str:
        """识别 ROI（RGB）→ 拼接字符串。返回空串表示完全没匹配到。"""
        roi = binarize(roi_rgb, self.binarize_block, self.binarize_C)
        H, W = roi.shape

        # 收集所有 (中心x, 字符, score, width) 候选
        candidates: list[tuple[float, str, float, int]] = []
        for g in self.glyphs:
            if g.height > H or g.width > W:
                continue
            res = cv2.matchTemplate(roi, g.bin_image, cv2.TM_CCOEFF_NORMED)
            ys, xs = np.where(res >= self.score_threshold)
            for x, y in zip(xs, ys):
                cx = x + g.width / 2.0
                candidates.append((cx, g.char, float(res[y, x]), g.width))

        if not candidates:
            return ""

        # NMS: 按 score 降序，按"两 bbox 在 x 方向显著重叠"判定冲突
        candidates.sort(key=lambda c: c[2], reverse=True)
        kept: list[tuple[float, str, float, int]] = []
        for cand in candidates:
            cx, _, _, w = cand
            conflict = False
            for k in kept:
                kcx, _, _, kw = k
                if abs(cx - kcx) < (w + kw) / 2 * self.nms_box_factor:
                    conflict = True
                    break
            if not conflict:
                kept.append(cand)

        # 按 x 排序拼字符串
        kept.sort(key=lambda c: c[0])
        return "".join(c[1] for c in kept)

    def all_candidates_above_threshold(self, roi_rgb: np.ndarray) -> list[dict]:
        """
        诊断用：列出所有 score >= score_threshold 的候选位置（不做 NMS）。
        返回按 cx 升序排序的 list of dict。
        """
        roi = binarize(roi_rgb, self.binarize_block, self.binarize_C)
        H, W = roi.shape
        cands: list[dict] = []
        for g in self.glyphs:
            if g.height > H or g.width > W:
                continue
            res = cv2.matchTemplate(roi, g.bin_image, cv2.TM_CCOEFF_NORMED)
            ys, xs = np.where(res >= self.score_threshold)
            for x, y in zip(xs, ys):
                cands.append(
                    {
                        "char": g.char,
                        "score": float(res[y, x]),
                        "tl": (int(x), int(y)),
                        "size": (g.width, g.height),
                        "cx": float(x + g.width / 2.0),
                    }
                )
        cands.sort(key=lambda c: c["cx"])
        return cands

    def diagnose(self, roi_rgb: np.ndarray) -> dict:
        """
        调试用：跑一遍 matchTemplate 但不应用阈值，返回每个模板的最高响应。
        与 recognize 同管线（同二值化），输出可用于排查"为什么 recognize 失败"。
        """
        roi_bin = binarize(roi_rgb, self.binarize_block, self.binarize_C)
        H, W = roi_bin.shape
        glyph_results = []
        for g in self.glyphs:
            entry = {
                "char": g.char,
                "template_size": (g.width, g.height),
            }
            if g.height > H or g.width > W:
                entry["skipped"] = "template_larger_than_roi"
            else:
                res = cv2.matchTemplate(roi_bin, g.bin_image, cv2.TM_CCOEFF_NORMED)
                idx = int(res.argmax())
                y, x = divmod(idx, res.shape[1])
                entry["max_score"] = float(res[y, x])
                entry["max_loc"] = (int(x), int(y))
            glyph_results.append(entry)
        return {
            "roi_size": (W, H),
            "score_threshold": self.score_threshold,
            "glyph_results": glyph_results,
            "above_threshold_candidates": self.all_candidates_above_threshold(roi_rgb),
            "roi_bin": roi_bin,
        }


# =============================================================================
# CoordReader
# =============================================================================


class CoordReader:
    """
    从 mumu 截图里读出小地图坐标 (x, y)。

    工作流: capture → crop ROI → TemplateOCR.recognize → 正则解析。
    """

    _COORD_RE = re.compile(r"\((\d+)\s*,\s*(\d+)\)")

    def __init__(
        self,
        mumu,
        ocr: TemplateOCR,
        roi_norm: tuple[float, float, float, float],
    ) -> None:
        self.mumu = mumu
        self.ocr = ocr
        self.roi_norm = roi_norm

    def read(
        self,
        image: Optional[Image.Image] = None,
    ) -> Optional[tuple[int, int]]:
        """
        读出 (x, y)。解析失败返回 None。

        image: 预先截好的整屏图；为 None 则现场截。
        """
        coord, _text, _roi = self.read_verbose(image)
        return coord

    def read_verbose(
        self,
        image: Optional[Image.Image] = None,
    ) -> tuple[Optional[tuple[int, int]], str, Image.Image]:
        """
        和 read 同管线，但额外返回 OCR 拼接出的原始字符串和 ROI 截图，
        用于调试 trace。

        返回: (coord_or_None, raw_text, roi_pil)
        """
        if image is None:
            image = self.mumu.capture_window()
        cropped = self.mumu.crop_img(image, self.roi_norm[:2], self.roi_norm[2:])
        roi_rgb = np.array(cropped.convert("RGB"))
        text = self.ocr.recognize(roi_rgb)
        if not text:
            return None, "", cropped
        m = self._COORD_RE.search(text)
        if not m:
            log.debug("坐标解析失败，OCR 文本: %r", text)
            return None, text, cropped
        return (int(m.group(1)), int(m.group(2))), text, cropped

    def diagnose(self, image: Optional[Image.Image] = None) -> dict:
        """
        诊断：返回 dict，包含每个字符模板在 ROI 上的最高响应分数 + 当前 ROI 的
        原图与二值化结果（PIL.Image / np.ndarray），调用方可保存到磁盘。
        """
        if image is None:
            image = self.mumu.capture_window()
        cropped = self.mumu.crop_img(image, self.roi_norm[:2], self.roi_norm[2:])
        roi_rgb = np.array(cropped.convert("RGB"))
        info = self.ocr.diagnose(roi_rgb)
        info["roi_pil"] = cropped
        # 同时附一次完整识别结果（含阈值过滤后的字符串）方便对比
        info["recognize_text"] = self.ocr.recognize(roi_rgb)
        return info
