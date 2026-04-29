"""
精炼模块的数据类定义.

设计要点:
    - 银两统一存为整数 ``wen`` (1 两 = 1000 文), 显示时再格式化.
    - Attribute 的 ``value`` 用 float 兼容百分比小数 (2.2%) 与整数 (126).
    - 写入 yaml 的 record 只保留必要字段, base_attrs / extra_attrs 用 dict/list
      直接序列化, 不依赖 dataclass-yaml 集成.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional


# =============================================================================
# 基础类型
# =============================================================================


@dataclass
class Attribute:
    """单条属性词条, 例如 (攻击, 126, '') 或 (免伤, 2.2, '%')."""

    name: str
    value: float
    unit: str = ""  # "" 或 "%"

    @property
    def display(self) -> str:
        v = self.value
        if v == int(v):
            v_s = str(int(v))
        else:
            # 去掉浮点尾部多余的 0
            v_s = f"{v:g}"
        return f"{self.name} {v_s}{self.unit}"

    def to_dict(self) -> dict:
        return {"name": self.name, "value": self.value, "unit": self.unit}

    @classmethod
    def from_dict(cls, d: dict) -> "Attribute":
        return cls(name=d["name"], value=float(d["value"]), unit=d.get("unit", ""))


@dataclass
class Money:
    """统一存为'文', 1 两 = 1000 文."""

    wen: int

    @property
    def liang(self) -> int:
        return self.wen // 1000

    @property
    def display(self) -> str:
        liang, w = divmod(int(self.wen), 1000)
        if liang == 0:
            return f"{w}文"
        if w == 0:
            return f"{liang}两"
        return f"{liang}两{w}文"


@dataclass
class MaterialState:
    """材料: 库存 / 单次消耗."""

    name: str
    stock: int
    cost_per_use: int

    @property
    def can_afford_uses(self) -> int:
        if self.cost_per_use <= 0:
            return 1_000_000  # 视为无限
        return self.stock // self.cost_per_use

    @property
    def display(self) -> str:
        return f"{self.name} {self.stock}/{self.cost_per_use} (够 {self.can_afford_uses} 次)"


# =============================================================================
# 面板状态
# =============================================================================


@dataclass
class StatusPanelState:
    """结束界面 (当前装备状态界面) 的解析结果."""

    equipment_name: str
    refine_count: int  # 已成功的次数
    base_attrs: dict[str, float] = field(default_factory=dict)
    extra_attrs: list[Attribute] = field(default_factory=list)
    materials: list[MaterialState] = field(default_factory=list)
    cost: Money = field(default_factory=lambda: Money(0))
    balance: Money = field(default_factory=lambda: Money(0))

    @property
    def can_refine(self) -> bool:
        """材料 + 银两是否足够再精炼一次."""
        if self.balance.wen < self.cost.wen:
            return False
        for m in self.materials:
            if m.can_afford_uses < 1:
                return False
        return True

    def remaining_uses(self) -> int:
        """按当前材料 / 银两估算还能精炼几次 (取最小约束)."""
        candidates = []
        if self.cost.wen > 0:
            candidates.append(self.balance.wen // self.cost.wen)
        for m in self.materials:
            candidates.append(m.can_afford_uses)
        if not candidates:
            return 0
        return int(min(candidates))


@dataclass
class ConfirmPanelState:
    """准备界面 (精炼结果待确认界面) 的解析结果."""

    equipment_name: str
    refine_count_inclusive: int  # 含本次的次数
    base_attrs: dict[str, float] = field(default_factory=dict)
    extra_attrs_before: list[Attribute] = field(default_factory=list)  # 1~3 条
    new_attr: Optional[Attribute] = None
    replace_index: int = -1  # 0-based, 指向 extra_attrs_before 的某项


# =============================================================================
# 精炼记录 (写入 yaml 的最小单位)
# =============================================================================


@dataclass
class RefineRecord:
    refine_no: int
    timestamp: str
    base_attrs: dict[str, float]
    attrs_before: list[dict]  # [{name, value, unit}, ...]
    new_attr: dict  # {name, value, unit}
    replace_index: int
    decision: str = "cancelled"  # cancelled | accepted

    def to_dict(self) -> dict:
        return {
            "refine_no": self.refine_no,
            "timestamp": self.timestamp,
            "base_attrs": dict(self.base_attrs),
            "attrs_before": list(self.attrs_before),
            "new_attr": dict(self.new_attr),
            "replace_index": self.replace_index,
            "decision": self.decision,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "RefineRecord":
        return cls(
            refine_no=int(d["refine_no"]),
            timestamp=str(d.get("timestamp", "")),
            base_attrs=dict(d.get("base_attrs", {})),
            attrs_before=list(d.get("attrs_before", [])),
            new_attr=dict(d.get("new_attr", {})),
            replace_index=int(d.get("replace_index", -1)),
            decision=str(d.get("decision", "cancelled")),
        )
