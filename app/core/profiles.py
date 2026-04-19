"""
movement_profile - 运动相关的几何常量与全局 UI 位置。

YAML 结构 (按可点击区域分辨率分组):

    profiles:
      "1920x1080":
        # 角色在屏幕上的位置（归一化）
        character_pos: [0.4417, 0.4944]

        # 每个视野档位对应的地图格屏幕尺寸（归一化）和一次点击最大移动格数
        vision_sizes:
          小: {block_size: [0.0833, 0.0741], move_max_num: 8, vision_delta_limit: 8}
          中: {block_size: [0.1052, 0.0935], move_max_num: 10, vision_delta_limit: 8}

        # 全局 UI 位置（归一化），用于跨地图传送 / 对话菜单等通用操作
        ui_positions:
          package_btn: [0.235, 0.924]
          ticket_btn:  [0.314, 0.793]
          blank_btn:   [0.894, 0.705]
          chat_btn_pos_list:
            - [0.594, 0.437]
            - [0.594, 0.530]
            ...
          table_btn_pos_list: [...]

        # 监视小地图坐标变动的 ROI (x0, y0, x1, y1) 归一化
        minimap_coord_roi: [0.8963, 0.3231, 0.9401, 0.3518]
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

log = logging.getLogger(__name__)


DEFAULT_MOVEMENT_YAML_PATH = Path("config/common/movement_profile.yaml")


# =============================================================================
# 数据类
# =============================================================================


@dataclass
class VisionSpec:
    """一个视野档位对应的几何参数（屏幕归一化）"""

    block_size: tuple[float, float]  # 一个地图格在屏幕上的宽高
    move_max_num: int  # 一次点击最多走几格（曼哈顿距离）
    vision_delta_limit: int  # 判断"靠近地图边界"的阈值

    def to_dict(self) -> dict:
        return {
            "block_size": list(self.block_size),
            "move_max_num": self.move_max_num,
            "vision_delta_limit": self.vision_delta_limit,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "VisionSpec":
        return cls(
            block_size=tuple(d["block_size"]),
            move_max_num=int(d.get("move_max_num", 8)),
            vision_delta_limit=int(d.get("vision_delta_limit", 8)),
        )


@dataclass
class UIPositions:
    """全局 UI 位置，归一化 (x, y)"""

    package_btn: Optional[tuple[float, float]] = None
    ticket_btn: Optional[tuple[float, float]] = None
    blank_btn: Optional[tuple[float, float]] = None
    chat_btn_pos_list: list[tuple[float, float]] = field(default_factory=list)
    table_btn_pos_list: list[tuple[float, float]] = field(default_factory=list)

    # buy 菜单相关
    buy_item_start_pos: Optional[tuple[float, float]] = None
    buy_item_span: Optional[tuple[float, float]] = None  # (col_span, row_span) 归一化
    buy_item_cols: int = 2
    buy_item_rows: int = 5
    buy_increase_btn: Optional[tuple[float, float]] = None
    buy_confirm_btn: Optional[tuple[float, float]] = None
    buy_exit_btn: Optional[tuple[float, float]] = None

    def to_dict(self) -> dict:
        d: dict = {}
        for name in (
            "package_btn",
            "ticket_btn",
            "blank_btn",
            "buy_item_start_pos",
            "buy_item_span",
            "buy_increase_btn",
            "buy_confirm_btn",
            "buy_exit_btn",
        ):
            val = getattr(self, name)
            if val is not None:
                d[name] = list(val)
        if self.chat_btn_pos_list:
            d["chat_btn_pos_list"] = [list(p) for p in self.chat_btn_pos_list]
        if self.table_btn_pos_list:
            d["table_btn_pos_list"] = [list(p) for p in self.table_btn_pos_list]
        d["buy_item_cols"] = self.buy_item_cols
        d["buy_item_rows"] = self.buy_item_rows
        return d

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> "UIPositions":
        if not d:
            return cls()

        def _pair(key: str) -> Optional[tuple[float, float]]:
            v = d.get(key)
            return tuple(v) if v else None

        def _pair_list(key: str) -> list[tuple[float, float]]:
            return [tuple(p) for p in (d.get(key) or [])]

        return cls(
            package_btn=_pair("package_btn"),
            ticket_btn=_pair("ticket_btn"),
            blank_btn=_pair("blank_btn"),
            chat_btn_pos_list=_pair_list("chat_btn_pos_list"),
            table_btn_pos_list=_pair_list("table_btn_pos_list"),
            buy_item_start_pos=_pair("buy_item_start_pos"),
            buy_item_span=_pair("buy_item_span"),
            buy_item_cols=int(d.get("buy_item_cols", 2)),
            buy_item_rows=int(d.get("buy_item_rows", 5)),
            buy_increase_btn=_pair("buy_increase_btn"),
            buy_confirm_btn=_pair("buy_confirm_btn"),
            buy_exit_btn=_pair("buy_exit_btn"),
        )

    def chat_btn(self, index_1based: int) -> tuple[float, float]:
        """ "chat_1" / "chat_2" ... 之类的解析"""
        idx = index_1based - 1
        if not (0 <= idx < len(self.chat_btn_pos_list)):
            raise ValueError(
                f"chat_btn 索引超界: {index_1based}，共有 {len(self.chat_btn_pos_list)} 项"
            )
        return self.chat_btn_pos_list[idx]

    def table_btn(self, index_1based: int) -> tuple[float, float]:
        idx = index_1based - 1
        if not (0 <= idx < len(self.table_btn_pos_list)):
            raise ValueError(
                f"table_btn 索引超界: {index_1based}，共有 {len(self.table_btn_pos_list)} 项"
            )
        return self.table_btn_pos_list[idx]

    def buy_item_pos(self, index_1based: int) -> tuple[float, float]:
        """商品栅格定位: index 从 1 开始，行优先"""
        if self.buy_item_start_pos is None or self.buy_item_span is None:
            raise ValueError("buy_item_start_pos / buy_item_span 未配置")
        idx = index_1based - 1
        row = idx // self.buy_item_cols
        col = idx % self.buy_item_cols
        sx, sy = self.buy_item_start_pos
        dx, dy = self.buy_item_span
        return (sx + col * dx, sy + row * dy)


@dataclass
class MovementProfile:
    """一个分辨率下的完整运动配置"""

    resolution: tuple[int, int]
    character_pos: tuple[float, float] = (0.4417, 0.4944)
    vision_sizes: dict[str, VisionSpec] = field(default_factory=dict)
    ui: UIPositions = field(default_factory=UIPositions)
    minimap_coord_roi: Optional[tuple[float, float, float, float]] = None

    @property
    def key(self) -> str:
        return f"{self.resolution[0]}x{self.resolution[1]}"

    def vision(self, name: str) -> VisionSpec:
        if name not in self.vision_sizes:
            raise KeyError(
                f"未配置视野档位「{name}」；" f"已有: {list(self.vision_sizes.keys())}"
            )
        return self.vision_sizes[name]

    def to_dict(self) -> dict:
        d: dict = {
            "character_pos": list(self.character_pos),
            "vision_sizes": {
                name: v.to_dict() for name, v in self.vision_sizes.items()
            },
            "ui_positions": self.ui.to_dict(),
        }
        if self.minimap_coord_roi is not None:
            d["minimap_coord_roi"] = list(self.minimap_coord_roi)
        return d

    @classmethod
    def from_dict(cls, key: str, d: dict) -> "MovementProfile":
        w_str, h_str = key.split("x")
        char_pos = tuple(d.get("character_pos", [0.4417, 0.4944]))
        vs_raw = d.get("vision_sizes") or {}
        visions = {name: VisionSpec.from_dict(v) for name, v in vs_raw.items()}
        ui = UIPositions.from_dict(d.get("ui_positions"))
        roi = d.get("minimap_coord_roi")
        return cls(
            resolution=(int(w_str), int(h_str)),
            character_pos=char_pos,
            vision_sizes=visions,
            ui=ui,
            minimap_coord_roi=(tuple(roi) if roi else None),
        )


@dataclass
class MovementRegistry:
    """对应整份 yaml 的顶层数据"""

    profiles: dict[str, MovementProfile] = field(default_factory=dict)
    path: Optional[Path] = None

    @classmethod
    def load(cls, path: Path = DEFAULT_MOVEMENT_YAML_PATH) -> "MovementRegistry":
        if not path.exists():
            log.info("movement_profile 不存在，新建空 registry: %s", path)
            return cls(path=path)
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        profiles_raw = data.get("profiles") or {}
        profiles = {
            key: MovementProfile.from_dict(key, pd) for key, pd in profiles_raw.items()
        }
        return cls(profiles=profiles, path=path)

    def save(self, path: Optional[Path | str] = None) -> Path:
        target = Path(path) if path else (self.path or DEFAULT_MOVEMENT_YAML_PATH)
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = {"profiles": {key: p.to_dict() for key, p in self.profiles.items()}}
        target.write_text(
            yaml.safe_dump(
                payload,
                allow_unicode=True,
                sort_keys=False,
                indent=2,
                default_flow_style=None,
            ),
            encoding="utf-8",
        )
        self.path = target
        log.info("movement_profile 已保存: %s", target)
        return target

    def ensure_profile(self, resolution: tuple[int, int]) -> MovementProfile:
        key = f"{resolution[0]}x{resolution[1]}"
        if key not in self.profiles:
            log.info("为分辨率 %s 新建 MovementProfile", key)
            self.profiles[key] = MovementProfile(
                resolution=resolution,
                vision_sizes={
                    # 旧代码里"小"的默认值（像素 → 归一化用 1920×1080 除）
                    "小": VisionSpec(
                        block_size=(160 / 1920, 80 / 1080),
                        move_max_num=8,
                        vision_delta_limit=8,
                    ),
                    "中": VisionSpec(
                        block_size=(202 / 1920, 101 / 1080),
                        move_max_num=10,
                        vision_delta_limit=8,
                    ),
                },
            )
        return self.profiles[key]
