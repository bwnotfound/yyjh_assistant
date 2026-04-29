"""
``refine_profile.yaml`` 的加载 / 保存.

profile 包含:
    - ROI: 11 个归一化矩形 (x1, y1, x2, y2) ∈ [0, 1]
    - button: 3 个归一化坐标 (x, y)
    - ocr: OCR 后端配置 (backend + params)
    - equipment_material_map: 装备名 → 两种材料名
    - 时延参数 / 轮询参数
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)


# 项目根目录下的默认路径
DEFAULT_PROFILE_PATH = Path("config/common/refine_profile.yaml")
DEFAULT_LOG_DIR = Path("config/refine_logs")


# ROI 字段名, 集中管理避免拼错.
# extra_attr_1/2/3: 中框三行旧词条, 每行一个 slot (按上→中→下).
# new_attr_slot_1/2/3: 右框三行新词条 ROI, 跟 extra_attr_i 水平对齐 (按上→中→下).
#                      "新词条出现在哪个 slot 里有内容" 直接决定 replace_index.
_ROI_KEYS: tuple[str, ...] = (
    "equipment_name",
    "refine_count",
    "base_attrs",
    "extra_attr_1",
    "extra_attr_2",
    "extra_attr_3",
    "new_attr_slot_1",
    "new_attr_slot_2",
    "new_attr_slot_3",
    "material_1",
    "material_2",
    "cost_money",
    "balance_money",
    "bottom_buttons",
)
_BUTTON_KEYS: tuple[str, ...] = ("refine", "accept", "cancel")


def _to_roi(v: Any, name: str) -> tuple[float, float, float, float]:
    if v is None:
        raise ValueError(f"refine_profile.yaml 缺少 ROI: {name}")
    seq = list(v)
    if len(seq) != 4:
        raise ValueError(f"ROI {name} 必须是 4 元素 (x1,y1,x2,y2), 实际: {seq}")
    return (float(seq[0]), float(seq[1]), float(seq[2]), float(seq[3]))


def _to_pos(v: Any, name: str) -> tuple[float, float]:
    if v is None:
        raise ValueError(f"refine_profile.yaml 缺少按钮坐标: {name}")
    seq = list(v)
    if len(seq) != 2:
        raise ValueError(f"按钮 {name} 必须是 2 元素 (x,y), 实际: {seq}")
    return (float(seq[0]), float(seq[1]))


@dataclass
class RefineProfile:
    # ROI (归一化)
    roi: dict[str, tuple[float, float, float, float]]
    # 按钮 (归一化)
    button: dict[str, tuple[float, float]]
    # OCR 配置
    ocr: dict
    # 装备 -> [材料1名, 材料2名]
    equipment_material_map: dict[str, list[str]] = field(default_factory=dict)
    # 时延参数
    delay_after_refine_click: float = 1.5
    delay_after_decision_click: float = 1.5
    poll_interval: float = 0.3
    panel_wait_timeout: float = 8.0

    @classmethod
    def load(cls, path: Path) -> "RefineProfile":
        """从 yaml 加载. 缺字段会抛清晰的 ValueError."""
        if not path.exists():
            raise FileNotFoundError(f"refine_profile.yaml 不存在: {path}")
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

        roi_raw = data.get("roi", {}) or {}
        roi = {k: _to_roi(roi_raw.get(k), k) for k in _ROI_KEYS}

        btn_raw = data.get("button", {}) or {}
        button = {k: _to_pos(btn_raw.get(k), k) for k in _BUTTON_KEYS}

        # 装备-材料映射 (允许为空, 启动时再校验)
        em = data.get("equipment_material_map", {}) or {}
        equipment_material_map: dict[str, list[str]] = {}
        for k, v in em.items():
            mats = list(v) if v is not None else []
            if len(mats) != 2:
                log.warning(
                    "equipment_material_map[%s] 应该有 2 个材料, 实际 %d 个",
                    k,
                    len(mats),
                )
                # 补齐到 2, 不阻断加载
                mats = (mats + ["?", "?"])[:2]
            equipment_material_map[str(k)] = [str(m) for m in mats]

        return cls(
            roi=roi,
            button=button,
            ocr=data.get("ocr", {"backend": "cnocr", "params": {}}),
            equipment_material_map=equipment_material_map,
            delay_after_refine_click=float(data.get("delay_after_refine_click", 1.5)),
            delay_after_decision_click=float(
                data.get("delay_after_decision_click", 1.5)
            ),
            poll_interval=float(data.get("poll_interval", 0.3)),
            panel_wait_timeout=float(data.get("panel_wait_timeout", 8.0)),
        )

    def save_material_map(self, path: Path, mapping: dict[str, list[str]]) -> None:
        """只更新 equipment_material_map 段; 保留其他配置不动.

        用于材料映射编辑器, 避免污染 ROI / 按钮等其他字段.
        """
        if path.exists():
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        else:
            data = {}
        data["equipment_material_map"] = {k: list(v) for k, v in mapping.items()}
        path.write_text(
            yaml.safe_dump(
                data,
                allow_unicode=True,
                sort_keys=False,
                default_flow_style=False,
            ),
            encoding="utf-8",
        )
        # 同步更新 self
        self.equipment_material_map = dict(data["equipment_material_map"])
