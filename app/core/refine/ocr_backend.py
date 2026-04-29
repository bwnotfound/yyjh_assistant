"""
OCR 后端抽象层.

设计:
    - 定义 ``OCRBackend`` 协议, 统一返回 ``OCRLine`` 列表.
    - cnocr 是默认实现, 通过 ``build_ocr_backend(config)`` 工厂构造,
      所有可调参数 (模型名 / cpu/cuda / 阈值) 走 yaml 注入, 不硬编码.
    - 后续要切 PaddleOCR / RapidOCR / 其他, 加一个新的 BackendXxx 类
      并在工厂函数里加分支即可, 上层 readers / runner 不用改.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

import numpy as np

log = logging.getLogger(__name__)


# =============================================================================
# 数据
# =============================================================================


@dataclass
class OCRLine:
    """单行 OCR 结果."""

    text: str
    bbox: tuple[int, int, int, int]  # (x1, y1, x2, y2), 像素坐标
    score: float

    @property
    def cx(self) -> float:
        return (self.bbox[0] + self.bbox[2]) / 2

    @property
    def cy(self) -> float:
        return (self.bbox[1] + self.bbox[3]) / 2

    @property
    def width(self) -> int:
        return self.bbox[2] - self.bbox[0]

    @property
    def height(self) -> int:
        return self.bbox[3] - self.bbox[1]


# =============================================================================
# 协议
# =============================================================================


class OCRBackend(Protocol):
    """OCR 后端协议.

    实现类必须提供 recognize(img_rgb) -> list[OCRLine].
    """

    def recognize(self, img_rgb: np.ndarray) -> list[OCRLine]: ...


# =============================================================================
# cnocr 实现
# =============================================================================


class CnOcrBackend:
    """基于 cnocr 的实现.

    所有构造参数都从 ``refine_profile.yaml`` 的 ``ocr.params`` 段注入.
    可调参数:
        det_model_name      文字检测模型 (cnocr 内置, 见 cnocr 文档)
        rec_model_name      文字识别模型
        context             "cpu" 或 "cuda"
        rec_score_threshold 单行 score 过滤阈值, 低于此值的行丢掉
    """

    def __init__(
        self,
        det_model_name: str = "ch_PP-OCRv4_det",
        rec_model_name: str = "scene-densenet_lite_136-gru",
        context: str = "cpu",
        rec_score_threshold: float = 0.4,
        **extra_kwargs,
    ) -> None:
        from cnocr import CnOcr  # 延迟 import, 避免没装时影响其他模块

        kwargs = dict(
            det_model_name=det_model_name,
            rec_model_name=rec_model_name,
            context=context,
        )
        kwargs.update(extra_kwargs)
        self._ocr = CnOcr(**kwargs)
        self._score_threshold = rec_score_threshold
        log.info(
            "CnOcrBackend 就绪: det=%s rec=%s ctx=%s th=%.2f",
            det_model_name,
            rec_model_name,
            context,
            rec_score_threshold,
        )

    def recognize(self, img_rgb: np.ndarray) -> list[OCRLine]:
        """对整图 OCR, 返回所有行 (按 y 升序, 同 y 按 x 升序).

        cnocr 返回的格式形如:
            [{"text": "...", "score": 0.97, "position": np.ndarray (4, 2)}, ...]
        其中 position 是 4 个角点 (x, y) 的多边形.
        """
        result = self._ocr.ocr(img_rgb)
        out: list[OCRLine] = []
        for d in result:
            score = float(d.get("score", 1.0))
            if score < self._score_threshold:
                continue
            text = str(d.get("text", "")).strip()
            if not text:
                continue
            pts = np.asarray(d["position"], dtype=np.float32)
            x1, y1 = pts[:, 0].min(), pts[:, 1].min()
            x2, y2 = pts[:, 0].max(), pts[:, 1].max()
            out.append(
                OCRLine(
                    text=text,
                    bbox=(int(x1), int(y1), int(x2), int(y2)),
                    score=score,
                )
            )
        out.sort(key=lambda l: (l.cy, l.cx))
        return out


# =============================================================================
# 工厂
# =============================================================================


def build_ocr_backend(config: dict) -> OCRBackend:
    """根据 ``refine_profile.yaml`` 的 ``ocr`` 段构造后端.

    config 示例:
        {"backend": "cnocr", "params": {"context": "cpu", ...}}
    """
    backend = (config or {}).get("backend", "cnocr").lower()
    params = (config or {}).get("params", {}) or {}
    if backend == "cnocr":
        return CnOcrBackend(**params)
    raise ValueError(f"未知的 OCR 后端: {backend!r}")
