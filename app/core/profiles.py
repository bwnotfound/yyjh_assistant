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

        # 全局 UI 位置（归一化）
        ui_positions:
          package_btn: [0.235, 0.924]
          ticket_btn:  [0.314, 0.793]
          blank_btn:   [0.894, 0.705]
          # 等距按钮组：录入第 1 个 + 第 2 个，运行时 first + (i-1)*(second-first)
          chat_btn:
            first:  [0.594, 0.437]
            second: [0.594, 0.530]
            count:  6
          table_btn:
            first:  [...]
            second: [...]
            count:  6
          # 商品 2D 栅格：录入第 1 个 + 第 N 个（默认 N = cols*rows，对角点误差最小）
          buy_item_grid:
            cols: 2
            rows: 4
            first:  [0.45, 0.30]
            second: [0.55, 0.55]
            second_index: 8
          buy_increase_btn: [...]
          buy_confirm_btn: [...]
          buy_exit_btn: [...]

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
class LinearButtonGroup:
    """
    一组等距排列的按钮（如对话菜单选项、场景交互项）。

    只录入第 1 个和第 2 个的归一化坐标，其余按等差推算：
        pos(i_1based) = first + (i_1based - 1) * (second - first)
    """

    first: tuple[float, float]
    second: tuple[float, float]
    count: int = 6

    def position(self, index_1based: int) -> tuple[float, float]:
        if not (1 <= index_1based <= self.count):
            raise ValueError(f"按钮索引 {index_1based} 超界（共 {self.count} 项）")
        idx = index_1based - 1
        dx = self.second[0] - self.first[0]
        dy = self.second[1] - self.first[1]
        return (self.first[0] + idx * dx, self.first[1] + idx * dy)

    def to_dict(self) -> dict:
        return {
            "first": list(self.first),
            "second": list(self.second),
            "count": int(self.count),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "LinearButtonGroup":
        return cls(
            first=tuple(d["first"]),
            second=tuple(d["second"]),
            count=int(d.get("count", 6)),
        )


@dataclass
class BuyItemGrid:
    """
    商品 2D 栅格：行优先，第 i (1-based) 个商品在 (col=(i-1)%cols, row=(i-1)//cols)。

    只录入第 1 个 + 第 second_index 个的屏幕坐标。运行时反解列/行间距：
        col_step_x = (second.x - first.x) / second_col
        row_step_y = (second.y - first.y) / second_row
    其中 second_col = (second_index-1) % cols, second_row = (second_index-1) // cols。

    second_index 的约束：与第 1 个**既不同行也不同列**（否则反解时会 0 除）。
    默认用 cols*rows（最远对角点，误差最小）。

    简化假设：列方向只影响 x，行方向只影响 y（游戏 UI 通常如此，无倾斜）。
    """

    cols: int = 2
    rows: int = 4
    first: tuple[float, float] = (0.0, 0.0)
    second: tuple[float, float] = (0.0, 0.0)
    second_index: int = 8

    @property
    def total(self) -> int:
        return self.cols * self.rows

    def position(self, index_1based: int) -> tuple[float, float]:
        if not (1 <= index_1based <= self.total):
            raise ValueError(
                f"商品索引 {index_1based} 超界"
                f"（共 {self.total} 项 = {self.cols} 列 × {self.rows} 行）"
            )
        if not (1 <= self.second_index <= self.total) or self.second_index == 1:
            raise ValueError(
                f"second_index={self.second_index} 非法（须 ∈ [2, {self.total}]）"
            )
        s_col = (self.second_index - 1) % self.cols
        s_row = (self.second_index - 1) // self.cols
        if s_col == 0:
            raise ValueError(
                f"second_index={self.second_index} 与第 1 个在同列，"
                f"无法反解列间距；请选与第 1 个不同列的位置（推荐对角）"
            )
        if s_row == 0:
            raise ValueError(
                f"second_index={self.second_index} 与第 1 个在同行，"
                f"无法反解行间距；请选与第 1 个不同行的位置（推荐对角）"
            )

        col_step_x = (self.second[0] - self.first[0]) / s_col
        row_step_y = (self.second[1] - self.first[1]) / s_row

        target_col = (index_1based - 1) % self.cols
        target_row = (index_1based - 1) // self.cols
        return (
            self.first[0] + target_col * col_step_x,
            self.first[1] + target_row * row_step_y,
        )

    def to_dict(self) -> dict:
        return {
            "cols": int(self.cols),
            "rows": int(self.rows),
            "first": list(self.first),
            "second": list(self.second),
            "second_index": int(self.second_index),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "BuyItemGrid":
        cols = int(d.get("cols", 2))
        rows = int(d.get("rows", 4))
        return cls(
            cols=cols,
            rows=rows,
            first=tuple(d["first"]),
            second=tuple(d["second"]),
            second_index=int(d.get("second_index", cols * rows)),
        )


def _load_button_group(
    d: dict, new_key: str, legacy_key: str
) -> Optional[LinearButtonGroup]:
    """优先读 new_key（新格式 dict）；只有 legacy_key 时按前两项推算并 log warning。"""
    if new_key in d and d[new_key]:
        return LinearButtonGroup.from_dict(d[new_key])
    if legacy_key in d and d[legacy_key]:
        legacy = d[legacy_key]
        if isinstance(legacy, list) and len(legacy) >= 2:
            log.warning(
                "movement_profile: 检测到弃用字段 %r，已按前两项自动迁移为 %r 等距推算；"
                "请在运动配置 GUI 中确认 first/second/count 并重新保存以清除该提示",
                legacy_key,
                new_key,
            )
            return LinearButtonGroup(
                first=tuple(legacy[0]),
                second=tuple(legacy[1]),
                count=max(2, len(legacy)),
            )
        log.warning(
            "movement_profile: 检测到弃用字段 %r 但内容不足以迁移（< 2 项），已忽略",
            legacy_key,
        )
    return None


def _load_buy_item_grid(d: dict) -> Optional[BuyItemGrid]:
    """
    优先读新字段 buy_item_grid（dict）。
    旧字段组合 buy_item_start_pos + buy_item_span (+ buy_item_cols/rows) 满足时
    自动迁移：first = start_pos，second = start_pos + ((cols-1)*col_span,
    (rows-1)*row_span)，second_index = cols*rows。
    """
    if "buy_item_grid" in d and d["buy_item_grid"]:
        return BuyItemGrid.from_dict(d["buy_item_grid"])

    legacy_start = d.get("buy_item_start_pos")
    legacy_span = d.get("buy_item_span")
    if not legacy_start or not legacy_span:
        return None

    cols = int(d.get("buy_item_cols", 2))
    rows = int(d.get("buy_item_rows", 4))
    sx, sy = float(legacy_start[0]), float(legacy_start[1])
    dx, dy = float(legacy_span[0]), float(legacy_span[1])
    log.warning(
        "movement_profile: 检测到弃用字段 buy_item_start_pos/buy_item_span，"
        "已自动迁移为 buy_item_grid（second 取对角第 %d 项）；"
        "请在运动配置 GUI 中确认并重新保存以清除该提示",
        cols * rows,
    )
    return BuyItemGrid(
        cols=cols,
        rows=rows,
        first=(sx, sy),
        second=(sx + (cols - 1) * dx, sy + (rows - 1) * dy),
        second_index=cols * rows,
    )


@dataclass
class UIPositions:
    """全局 UI 位置，归一化 (x, y)"""

    package_btn: Optional[tuple[float, float]] = None
    ticket_btn: Optional[tuple[float, float]] = None
    blank_btn: Optional[tuple[float, float]] = None

    # 等距排列的按钮组
    chat_btn_group: Optional[LinearButtonGroup] = None
    table_btn_group: Optional[LinearButtonGroup] = None

    # 商品 2D 栅格
    buy_item_grid: Optional[BuyItemGrid] = None

    # buy 菜单其他单点
    buy_increase_btn: Optional[tuple[float, float]] = None
    buy_confirm_btn: Optional[tuple[float, float]] = None
    buy_exit_btn: Optional[tuple[float, float]] = None

    def to_dict(self) -> dict:
        d: dict = {}
        for name in (
            "package_btn",
            "ticket_btn",
            "blank_btn",
            "buy_increase_btn",
            "buy_confirm_btn",
            "buy_exit_btn",
        ):
            val = getattr(self, name)
            if val is not None:
                d[name] = list(val)
        if self.chat_btn_group is not None:
            d["chat_btn"] = self.chat_btn_group.to_dict()
        if self.table_btn_group is not None:
            d["table_btn"] = self.table_btn_group.to_dict()
        if self.buy_item_grid is not None:
            d["buy_item_grid"] = self.buy_item_grid.to_dict()
        return d

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> "UIPositions":
        if not d:
            return cls()

        def _pair(key: str) -> Optional[tuple[float, float]]:
            v = d.get(key)
            return tuple(v) if v else None

        return cls(
            package_btn=_pair("package_btn"),
            ticket_btn=_pair("ticket_btn"),
            blank_btn=_pair("blank_btn"),
            chat_btn_group=_load_button_group(d, "chat_btn", "chat_btn_pos_list"),
            table_btn_group=_load_button_group(d, "table_btn", "table_btn_pos_list"),
            buy_item_grid=_load_buy_item_grid(d),
            buy_increase_btn=_pair("buy_increase_btn"),
            buy_confirm_btn=_pair("buy_confirm_btn"),
            buy_exit_btn=_pair("buy_exit_btn"),
        )

    def chat_btn(self, index_1based: int) -> tuple[float, float]:
        if self.chat_btn_group is None:
            raise ValueError(
                "ui.chat_btn 未配置（运动配置里需录入第 1 个和第 2 个按钮位置）"
            )
        return self.chat_btn_group.position(index_1based)

    def table_btn(self, index_1based: int) -> tuple[float, float]:
        if self.table_btn_group is None:
            raise ValueError(
                "ui.table_btn 未配置（运动配置里需录入第 1 个和第 2 个按钮位置）"
            )
        return self.table_btn_group.position(index_1based)

    def buy_item_pos(self, index_1based: int) -> tuple[float, float]:
        if self.buy_item_grid is None:
            raise ValueError(
                "ui.buy_item_grid 未配置（运动配置里需录入商品第 1 个和第 N 个位置）"
            )
        return self.buy_item_grid.position(index_1based)


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
