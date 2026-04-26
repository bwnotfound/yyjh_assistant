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
          chat_btn:
            first:  [0.594, 0.437]
            second: [0.594, 0.530]
            count:  6
          table_btn:
            first:  [...]
            second: [...]
            count:  6
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

        # 屏幕上"地图能完整显示的矩形"(避开周围 UI 遮挡), 归一化
        # 用于 compute_character_screen_pos 几何算法
        map_view_area: [0.05, 0.04, 0.79, 0.94]
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
    vision_delta_limit: int  # 判断"靠近地图边界"的阈值（旧经验算法用，新几何算法不用）

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


@dataclass
class ClickDelays:
    """
    各类 UI 点击之后的等待秒数（"delay"= 点击发出后 sleep 的时长）。
    任一分类字段为 None 时按以下顺序回退:
        1. 显式设值
        2. 该分类的推荐默认值（_RECOMMENDED_DEFAULTS，仅特定分类有）
        3. ClickDelays.default

    分类含义:
      default            通用兜底
      button             _do_button 主点击（chat_btn / table_btn）
      blank_skip         跳对话时点 blank_btn
      buy_item           _do_buy 选商品
      buy_increase       _do_buy 数量 +1
      buy_confirm        _do_buy 确认购买
      buy_exit           _do_buy 退出购买
      click              _do_click 通用 click step
      open_package       _do_travel 点 package_btn（背包打开）
      ticket             _do_travel 点 ticket_btn（背包里点票券）
      travel_icon        _do_travel 点目标图标（等跳转对话框出现）
      travel_confirm     _do_travel 点跳转确认按钮
      travel_transition  _do_travel 切图过场（默认 3.0s）

      ── 飞行（轻功传送）相关 ──
      游戏机制（烟雨江湖）：飞行是两次点击 ——
        1) 点角色 → 进入"轻功施展模式"，屏幕弹黄色高亮可施展格
        2) 再点黄框内目标格 → 角色飞过去
        点黄框外则取消施展。

      fly                飞行段：点角色后等黄框出现的延迟（默认 0.8s）
                         即 mumu.click(char_pos, delay=fly) 的 delay
                         过短会让第二次点击时黄框还没渲染出来 → 被判为"点角色"再次切换状态
      fly_settle         飞行段：第二次点击（点目标格）后等画面稳定/落地的最长等待秒数
                         （默认 3.0s）。烟雨江湖飞行动画时长不固定，按经验值给 3s 兜底；
                         若注入了 coord_reader, 实际更早地通过 OCR 读到目标坐标即返回，
                         不会傻等满 3s。

      ── 普通 move 段 ──
      move_step          普通 move 原子段每次 click 后的固定 sleep 秒数（默认 0）。
                         OCR 闭环下 _wait_via_ocr 阶段 1 本身就在循环等坐标变化,
                         不需要 click 后先 sleep 给游戏反应时间。仅在没注入 OCR
                         (走 SSIM 兜底) 时若发现连续 click 间游戏漏点, 才需调大
                         此值. fly 段不受影响, 走 fly / fly_settle.
    """

    default: float = 0.5
    button: Optional[float] = None
    blank_skip: Optional[float] = None
    buy_item: Optional[float] = None
    buy_increase: Optional[float] = None
    buy_confirm: Optional[float] = None
    buy_exit: Optional[float] = None
    click: Optional[float] = None
    open_package: Optional[float] = None
    ticket: Optional[float] = None
    travel_icon: Optional[float] = None
    travel_confirm: Optional[float] = None
    travel_transition: Optional[float] = None
    fly: Optional[float] = None
    fly_settle: Optional[float] = None
    move_step: Optional[float] = None

    _SUB_FIELDS = (
        "button",
        "blank_skip",
        "buy_item",
        "buy_increase",
        "buy_confirm",
        "buy_exit",
        "click",
        "open_package",
        "ticket",
        "travel_icon",
        "travel_confirm",
        "travel_transition",
        "fly",
        "fly_settle",
        "move_step",
    )

    _RECOMMENDED_DEFAULTS = {
        "travel_transition": 3.0,
        # 飞行段需要较长等待，且不能简单回退到 default。
        # fly:        点角色后等黄框出现, 0.8s 在大部分机器上够用 (黄框是即时渲染的,
        #             这里主要是给 adb 注入点击 + 游戏注册点击 + 黄框 sprite 加载留余量)。
        # fly_settle: 第二次点击 (点目标格) 后等飞行落地。烟雨江湖飞行动画时长不固定,
        #             经验值 3s 兜底; 如果用户的飞行段距离很远/动画特别长, 在运动配置 GUI
        #             里调大该字段即可。注入 coord_reader 时不需要傻等满 3s, OCR 读到
        #             目标坐标会立刻返回。
        "fly": 0.8,
        "fly_settle": 3.0,
        # move_step:  普通 move 段 click 后的固定 sleep。OCR 闭环下不需要这个 sleep
        #             (phase1 本身就在循环等坐标变), 默认 0 让段间无人为停顿。
        "move_step": 0.0,
    }

    def resolve(self, kind: str) -> float:
        v = getattr(self, kind, None)
        if v is not None:
            return float(v)
        if kind in self._RECOMMENDED_DEFAULTS:
            return float(self._RECOMMENDED_DEFAULTS[kind])
        return float(self.default)

    def fallback_for(self, kind: str) -> float:
        if kind in self._RECOMMENDED_DEFAULTS:
            return float(self._RECOMMENDED_DEFAULTS[kind])
        return float(self.default)

    def to_dict(self) -> dict:
        d: dict = {"default": float(self.default)}
        for name in self._SUB_FIELDS:
            v = getattr(self, name)
            if v is not None:
                d[name] = float(v)
        return d

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> "ClickDelays":
        if not d:
            return cls()
        out = cls(default=float(d.get("default", 0.5)))
        for name in cls._SUB_FIELDS:
            if name in d and d[name] is not None:
                setattr(out, name, float(d[name]))
        return out


def _load_button_group(
    d: dict, new_key: str, legacy_key: str
) -> Optional[LinearButtonGroup]:
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

    chat_btn_group: Optional[LinearButtonGroup] = None
    table_btn_group: Optional[LinearButtonGroup] = None

    buy_item_grid: Optional[BuyItemGrid] = None

    buy_increase_btn: Optional[tuple[float, float]] = None
    buy_confirm_btn: Optional[tuple[float, float]] = None
    buy_exit_btn: Optional[tuple[float, float]] = None

    # 自定义命名预设（用户在 routine 编辑器里通过「新建预设」录入的）
    # —— 与上面 6 个内置单点共享同一命名空间, 但隔离存储:
    #   - 内置点字段名固定, 给 travel/buy/button 等业务流程硬依赖
    #   - 自定义点放这个字典, 名字由用户起 (^[A-Za-z_][A-Za-z0-9_]*$)
    #     运行时 ClickStep 解析 preset 时如果不是内置名, 就来这里查
    custom: dict[str, tuple[float, float]] = field(default_factory=dict)

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
        if self.custom:
            # 序列化为 list 形式与其他坐标字段一致
            d["custom"] = {k: list(v) for k, v in self.custom.items()}
        return d

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> "UIPositions":
        if not d:
            return cls()

        def _pair(key: str) -> Optional[tuple[float, float]]:
            v = d.get(key)
            return tuple(v) if v else None

        custom_raw = d.get("custom") or {}
        custom: dict[str, tuple[float, float]] = {}
        for name, val in custom_raw.items():
            if isinstance(val, (list, tuple)) and len(val) == 2:
                custom[str(name)] = (float(val[0]), float(val[1]))

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
            custom=custom,
        )

    # 内置单点字段名 (与 to_dict 序列化的字段一致)。external 模块用这个判断
    # 一个 preset 名是不是落在内置字段, 而不是用 hasattr (会把 chat_btn_group
    # 这些非"单点"字段也算上)。
    BUILTIN_SINGLE_POINT_FIELDS: tuple[str, ...] = (
        "package_btn",
        "ticket_btn",
        "blank_btn",
        "buy_increase_btn",
        "buy_confirm_btn",
        "buy_exit_btn",
    )

    def resolve_single_point(self, name: str) -> Optional[tuple[float, float]]:
        """
        统一入口: 给定一个 preset 名, 返回 (x, y) 或 None。
        查找顺序: 内置单点字段 → custom 字典。
        不在这两处 (如 chat_btn / character_pos) 返回 None, 由上层另行处理。
        """
        if name in self.BUILTIN_SINGLE_POINT_FIELDS:
            v = getattr(self, name, None)
            if isinstance(v, tuple) and len(v) == 2:
                return v
            return None
        return self.custom.get(name)

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
    click_delays: ClickDelays = field(default_factory=ClickDelays)
    minimap_coord_roi: Optional[tuple[float, float, float, float]] = None
    # 屏幕上"地图能完整显示的矩形"(避开周围 UI 遮挡), 归一化 (vx0, vy0, vx1, vy1)。
    # 配置后启用 compute_character_screen_pos 几何算法做贴边修正；
    # 留空则各调用方按各自的回退策略 (旧经验算法 / 不做贴边修正)。
    map_view_area: Optional[tuple[float, float, float, float]] = None

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
            "click_delays": self.click_delays.to_dict(),
        }
        if self.minimap_coord_roi is not None:
            d["minimap_coord_roi"] = list(self.minimap_coord_roi)
        if self.map_view_area is not None:
            d["map_view_area"] = list(self.map_view_area)
        return d

    @classmethod
    def from_dict(cls, key: str, d: dict) -> "MovementProfile":
        w_str, h_str = key.split("x")
        char_pos = tuple(d.get("character_pos", [0.4417, 0.4944]))
        vs_raw = d.get("vision_sizes") or {}
        visions = {name: VisionSpec.from_dict(v) for name, v in vs_raw.items()}
        ui = UIPositions.from_dict(d.get("ui_positions"))
        click_delays = ClickDelays.from_dict(d.get("click_delays"))
        roi = d.get("minimap_coord_roi")
        view = d.get("map_view_area")
        return cls(
            resolution=(int(w_str), int(h_str)),
            character_pos=char_pos,
            vision_sizes=visions,
            ui=ui,
            click_delays=click_delays,
            minimap_coord_roi=(tuple(roi) if roi else None),
            map_view_area=(tuple(view) if view else None),
        )


# =============================================================================
# 几何工具：根据 view_area + map_size + 视野动态算角色 sprite 屏幕位置
# =============================================================================


def compute_character_screen_pos(
    pre_pos: tuple[int, int],
    map_size: tuple[int, int],
    block_size: tuple[float, float],
    character_pos: tuple[float, float],
    view_area: tuple[float, float, float, float],
) -> tuple[float, float]:
    """
    用几何方法算角色 sprite 在屏幕上的归一化位置。

    模型 (2.5D 等距投影 2:1, 地图相对屏幕顺时针 45°):
        地图顶点 ↔ 屏幕方位 ↔ view_area 边:
            N (0, 0)    ↔ 屏幕上 ↔ vy0
            S (mw, mh)  ↔ 屏幕下 ↔ vy1
            W (0, mh)   ↔ 屏幕左 ↔ vx0
            E (mw, 0)   ↔ 屏幕右 ↔ vx1

    camera 默认让玩家屏幕位置 = character_pos (cp). 当玩家**靠近**某顶点时, 该
    顶点屏幕投影会进入 view_area 内部 → camera 把 view_area 推到让该顶点贴对应
    view_area 边 → 玩家屏幕位置朝**那个方向**偏移:
        N 激活 (玩家靠近 (0, 0)):    py = vy0 + n_dist  → dy = py-cy < 0 (偏上)
        S 激活 (玩家靠近 (mw, mh)):  py = vy1 - s_dist  → dy > 0 (偏下)
        W 激活 (玩家靠近 (0, mh)):   px = vx0 + w_dist  → dx < 0 (偏左)
        E 激活 (玩家靠近 (mw, 0)):   px = vx1 - e_dist  → dx > 0 (偏右)

    激活条件 (默认 px=cx, py=cy 时该顶点屏幕投影**进入** view_area):
        N 激活 ⟺ n_dist < cy - vy0     (即 sy_N_default = cy - n_dist > vy0)
        S 激活 ⟺ s_dist < vy1 - cy
        W 激活 ⟺ w_dist < cx - vx0
        E 激活 ⟺ e_dist < vx1 - cx

    一般情况下 N+S 不会同时激活 (要求 vy1-vy0 > n_dist+s_dist = (mw+mh)*bh/2,
    即 view_area 高度 > 地图 y 投影长度, 说明地图比 view_area 还窄). W+E 同理。
    极端 case 下取折中位置避免硬错。

    Args:
        pre_pos: 玩家当前格 (gx, gy)
        map_size: 地图格数 (mw, mh)
        block_size: 一格在屏幕上的归一化尺寸 (bw, bh)
        character_pos: 无约束时玩家屏幕中心 (cx, cy), 不一定等于 view_area 中心
        view_area: 屏幕可视区域 (vx0, vy0, vx1, vy1)

    Returns:
        玩家在屏幕上的归一化位置 (px, py)
    """
    gx, gy = pre_pos
    mw, mh = map_size
    bw, bh = block_size
    cx, cy = character_pos
    vx0, vy0, vx1, vy1 = view_area

    if not (0 <= gx <= mw and 0 <= gy <= mh):
        log.warning(
            "compute_character_screen_pos: pre_pos=(%d, %d) 超出 "
            "map_size=(%d, %d) — 请检查 map_registry 里该地图的 map_size 配置。"
            "本次返回 character_pos=%s, 不做贴边修正。",
            gx,
            gy,
            mw,
            mh,
            character_pos,
        )
        return character_pos

    n_dist = (gx + gy) * bh / 2
    s_dist = (mw + mh - gx - gy) * bh / 2
    w_dist = (gx + mh - gy) * bw / 2
    e_dist = (mw - gx + gy) * bw / 2

    # Y 方向: 检查 N/S 谁在 view_area 内激活, 推 py 让它贴边
    py = cy
    n_active = n_dist < cy - vy0
    s_active = s_dist < vy1 - cy
    if n_active and s_active:
        log.warning(
            "compute_character_screen_pos: N+S 同时激活 (n_dist=%.4f, s_dist=%.4f, "
            "view_area y=[%.4f, %.4f]) — 极端 case, 检查配置。",
            n_dist,
            s_dist,
            vy0,
            vy1,
        )
        py = ((vy0 + n_dist) + (vy1 - s_dist)) / 2
    elif n_active:
        py = vy0 + n_dist
    elif s_active:
        py = vy1 - s_dist

    # X 方向: 同理 W/E
    px = cx
    w_active = w_dist < cx - vx0
    e_active = e_dist < vx1 - cx
    if w_active and e_active:
        log.warning(
            "compute_character_screen_pos: W+E 同时激活 (w_dist=%.4f, e_dist=%.4f, "
            "view_area x=[%.4f, %.4f]).",
            w_dist,
            e_dist,
            vx0,
            vx1,
        )
        px = ((vx0 + w_dist) + (vx1 - e_dist)) / 2
    elif w_active:
        px = vx0 + w_dist
    elif e_active:
        px = vx1 - e_dist

    return (px, py)


# =============================================================================
# 几何对偶: 已知玩家屏幕实际位置, 反解 view_area 边界
# =============================================================================


@dataclass
class ViewAreaSolveResult:
    """单次反解观测能解出的 view_area 边界 + 异常说明"""

    vx0: Optional[float] = None
    vy0: Optional[float] = None
    vx1: Optional[float] = None
    vy1: Optional[float] = None
    notes: list[str] = field(default_factory=list)

    @property
    def has_any(self) -> bool:
        return any(v is not None for v in (self.vx0, self.vy0, self.vx1, self.vy1))


def solve_view_area_observation(
    pre_pos: tuple[int, int],
    screen_pos: tuple[float, float],
    map_size: tuple[int, int],
    block_size: tuple[float, float],
    character_pos: tuple[float, float],
    epsilon: float = 3e-3,
) -> ViewAreaSolveResult:
    """
    根据一次观测 (玩家格坐标 + 玩家屏幕实际位置) 反解 view_area 4 边界。

    几何 (与 compute_character_screen_pos 对偶):
        dy = py - cy < 0 (玩家偏上) → N 激活 → vy0 = py - n_dist
        dy > 0 (玩家偏下)            → S 激活 → vy1 = py + s_dist
        dx = px - cx < 0 (玩家偏左)  → W 激活 → vx0 = px - w_dist
        dx > 0 (玩家偏右)            → E 激活 → vx1 = px + e_dist
        dx≈0 / dy≈0 (未贴边) → 该方向无信号

    新物理下 N 和 S 不会同时激活 (除非地图比 view_area 还窄), W 和 E 同理 → dx/dy
    符号唯一对应一种约束, 不需要"方向矛盾"检查。原代码里基于 n_dist vs s_dist
    的"距哪边近"判定是错的 (混淆了"哪个顶点屏幕近"和"哪条 view_area 边近"),
    新版完全去掉。

    一致性检查 (任一不通过 → 写 notes 警告 + 不更新该边):
      · 反解值在 [0, 1] 内
      · 反解的边在 cp 对应方向 (vy0 < cy, vy1 > cy, vx0 < cx, vx1 > cx)
      · pre_pos 不能超出 map_size

    Args:
        pre_pos: 玩家当前格坐标 (gx, gy)
        screen_pos: 玩家在屏幕上的实际归一化位置 (px, py), 由用户精确指出
        map_size, block_size, character_pos: 含义见 compute_character_screen_pos
        epsilon: "未贴边" 阈值, 默认 1e-4 ≈ 浮点精度. 任何超出 epsilon 的偏差都
                  视为有效贴边信号; 反解越界由后续检查拦下并留 notes。

    Returns:
        ViewAreaSolveResult: 含本次能解出的边界 + 异常 notes
    """
    gx, gy = pre_pos
    px, py = screen_pos
    mw, mh = map_size
    bw, bh = block_size
    cx, cy = character_pos
    out = ViewAreaSolveResult()

    if not (0 <= gx <= mw and 0 <= gy <= mh):
        out.notes.append(
            f"⚠ pre_pos=({gx}, {gy}) 超出 map_size=({mw}, {mh})。请检查 map_size。"
        )
        return out

    n_dist = (gx + gy) * bh / 2
    s_dist = (mw + mh - gx - gy) * bh / 2
    w_dist = (gx + mh - gy) * bw / 2
    e_dist = (mw - gx + gy) * bw / 2

    # ---- Y 方向 ----
    dy = py - cy
    if abs(dy) < epsilon:
        out.notes.append(
            f"y 方向: 偏差 dy={dy:+.4f} 在容差 ±{epsilon} 内 (约 ±1-2 像素), "
            f"视为点击噪声未贴边, 本次不解 vy0/vy1。"
            f"如认为应该解出, 请重新精确点击。"
        )
    elif dy < 0:
        # 玩家偏上 → N 激活 → vy0 = py - n_dist
        cand = py - n_dist
        if 0.0 <= cand < cy:
            out.vy0 = cand
        else:
            # 反解越界或几何矛盾: 物理上 N 不可能激活 (n_dist 远超 cy-vy0 阈值),
            # 这个 dy 信号实际是点击噪声 (虽超过 epsilon 容差但物理无解).
            out.notes.append(
                f"y 方向: dy={dy:+.4f} 但反解 vy0={cand:.4f} 不在合理范围 [0, {cy:.4f}], "
                f"看似点击噪声 (n_dist={n_dist:.4f} 暗示玩家离 N 顶点太远, N 物理上不应激活), "
                f"本次不解 vy0。"
            )
    else:  # dy > 0
        # 玩家偏下 → S 激活 → vy1 = py + s_dist
        cand = py + s_dist
        if cy < cand <= 1.0:
            out.vy1 = cand
        else:
            out.notes.append(
                f"y 方向: dy={dy:+.4f} 但反解 vy1={cand:.4f} 不在合理范围 ({cy:.4f}, 1], "
                f"看似点击噪声 (s_dist={s_dist:.4f} 暗示玩家离 S 顶点太远, S 物理上不应激活), "
                f"本次不解 vy1。"
            )

    # ---- X 方向 ----
    dx = px - cx
    if abs(dx) < epsilon:
        out.notes.append(
            f"x 方向: 偏差 dx={dx:+.4f} 在容差 ±{epsilon} 内 (约 ±1-2 像素), "
            f"视为点击噪声未贴边, 本次不解 vx0/vx1。"
            f"如认为应该解出, 请重新精确点击。"
        )
    elif dx < 0:
        # 玩家偏左 → W 激活 → vx0 = px - w_dist
        cand = px - w_dist
        if 0.0 <= cand < cx:
            out.vx0 = cand
        else:
            out.notes.append(
                f"x 方向: dx={dx:+.4f} 但反解 vx0={cand:.4f} 不在合理范围 [0, {cx:.4f}], "
                f"看似点击噪声 (w_dist={w_dist:.4f} 暗示玩家离 W 顶点太远, W 物理上不应激活), "
                f"本次不解 vx0。"
            )
    else:  # dx > 0
        # 玩家偏右 → E 激活 → vx1 = px + e_dist
        cand = px + e_dist
        if cx < cand <= 1.0:
            out.vx1 = cand
        else:
            out.notes.append(
                f"x 方向: dx={dx:+.4f} 但反解 vx1={cand:.4f} 不在合理范围 ({cx:.4f}, 1], "
                f"看似点击噪声 (e_dist={e_dist:.4f} 暗示玩家离 E 顶点太远, E 物理上不应激活), "
                f"本次不解 vx1。"
            )

    return out


# =============================================================================
# 辅助: 反解可达性分析 (告诉用户当前 map+视野下走到哪能解出哪些边)
# =============================================================================


def compute_view_area_reachability(
    map_size: tuple[int, int],
    block_size: tuple[float, float],
    character_pos: tuple[float, float],
    view_area_estimate: tuple[float, float, float, float],
) -> str:
    """
    给定 map_size + block_size + character_pos + 估计 view_area, 返回人类可读
    的"反解可达性"提示。

    几何 (与 compute_character_screen_pos 一致):
        玩家**靠近**某顶点 → 该顶点屏幕投影进入 view_area → camera 推 view_area
        贴边 → 玩家屏幕偏移 → 反解出对应屏幕边

        屏幕 vy0 (上) ← N 顶点 (0, 0):    玩家**靠近** (0, 0) 时 N 激活
        屏幕 vy1 (下) ← S 顶点 (mw, mh):  玩家**靠近** (mw, mh) 时 S 激活
        屏幕 vx0 (左) ← W 顶点 (0, mh):   玩家**靠近** (0, mh) 时 W 激活
        屏幕 vx1 (右) ← E 顶点 (mw, 0):   玩家**靠近** (mw, 0) 时 E 激活

    单条边激活阈值 (玩家到对应顶点曼哈顿距离 < 阈值, 即玩家**靠近**该顶点):
        vy0: dist 玩家→(0, 0)   < 2*(cy-vy0)/bh  =: th_N
        vy1: dist 玩家→(mw, mh) < 2*(vy1-cy)/bh  =: th_S
        vx0: dist 玩家→(0, mh)  < 2*(cx-vx0)/bw  =: th_W
        vx1: dist 玩家→(mw, 0)  < 2*(vx1-cx)/bw  =: th_E

    操作策略:
        实际操作: 走地图 4 顶点附近, 各解 1 条边, 4 次观测齐 4 边。
            玩家走到 (0, 0) 附近 → 仅 N 激活 (W/E 都远) → 解 vy0
            玩家走到 (0, mh) 附近 → 仅 W 激活 → 解 vx0
            玩家走到 (mw, 0) 附近 → 仅 E 激活 → 解 vx1
            玩家走到 (mw, mh) 附近 → 仅 S 激活 → 解 vy1

        理论最少 2 次同时解 2 边: 玩家在地图边的中段, 同时让相邻两个顶点激活。
        但需要每条阈值都满足 (地图小时不一定有这种位置), 实操不稳, 不推荐。

    跨地图: view_area / block_size / character_pos 都是按分辨率+视野共用的
    屏幕几何, 不依赖具体地图。所以不同地图的观测可以混合, 只要分辨率和视野
    档位一致。
    """
    mw, mh = map_size
    bw, bh = block_size
    cx, cy = character_pos
    vx0, vy0, vx1, vy1 = view_area_estimate

    th_N = 2 * (cy - vy0) / bh
    th_S = 2 * (vy1 - cy) / bh
    th_W = 2 * (cx - vx0) / bw
    th_E = 2 * (vx1 - cx) / bw

    lines = []
    lines.append(
        f"地图 {mw}×{mh}, block={bw:.4f}×{bh:.4f}, "
        f"估计 view_area=({vx0:.3f}, {vy0:.3f}, {vx1:.3f}, {vy1:.3f})"
    )
    lines.append(
        "各屏幕边激活阈值 (玩家到对应顶点曼哈顿距离需 **小于** = 玩家**靠近**该顶点):"
    )
    lines.append(
        f"  vy0 (屏幕上): 离 (0, 0)    < {th_N:5.1f} 格   "
        f"vy1 (屏幕下): 离 ({mw}, {mh}) < {th_S:5.1f} 格"
    )
    lines.append(
        f"  vx0 (屏幕左): 离 (0, {mh})  < {th_W:5.1f} 格   "
        f"vx1 (屏幕右): 离 ({mw}, 0)   < {th_E:5.1f} 格"
    )
    lines.append(
        "策略: 走地图 4 个顶点附近 (各 1 次, 解 1 条边). 走到顶点上时仅对应顶点的"
    )
    lines.append("边激活 (其他顶点都远在 view_area 外)。共 4 次观测凑齐 4 边。")
    return "\n".join(lines)


# =============================================================================
# 几何对偶: 已知玩家屏幕实际位置 + view_area, 反解 map_size
# =============================================================================
#
# 物理图像 (与 compute_character_screen_pos 一致):
#   地图相对屏幕顺时针 45°, 4 个地图顶点各自对应屏幕 1 个方位:
#     N 顶点 (0, 0)    ↔ 屏幕上 (vy0)
#     S 顶点 (mw, mh)  ↔ 屏幕下 (vy1)
#     W 顶点 (0, mh)   ↔ 屏幕左 (vx0)
#     E 顶点 (mw, 0)   ↔ 屏幕右 (vx1)
#
#   玩家**靠近**某顶点时 → 该顶点屏幕投影进入 view_area → camera 把 view_area 推
#   到让该顶点贴对应 view_area 边 → 玩家屏幕位置朝那个方向偏移.
#
# 两个轴可以独立同时激活:
#   X 轴和 Y 轴的激活互相独立. 当地图较窄或玩家位于两条相邻边的中段时,
#   一次观测可同时给出 X 和 Y 两个方向的有效偏移. 例如玩家在地图正南边
#   (gx=mw/2, gy=mh) 附近时, 既靠近 W 顶点 (0,mh) 又靠近 S 顶点 (mw,mh)
#   → dx<0 和 dy>0 同时显著, 各自独立反解 mh 和 mw+mh, 进而推出 mw.
#
# 各轴反解规则:
#   X 轴 (|dx| > epsilon):
#     dx < 0  →  W 激活, 解 mh = 2*(px-vx0)/bw - gx + gy
#     dx > 0  →  E 激活, 解 mw = 2*(vx1-px)/bw + gx - gy
#   Y 轴 (|dy| > epsilon):
#     dy < 0  →  N 激活, 公式 py = vy0 + (gx+gy)*bh/2 不含 mw/mh, 无信息
#     dy > 0  →  S 激活, 解 mw+mh = 2*(vy1-py)/bh + gx + gy
#                       (无法独立分离 mw / mh, 仅作参考校验)
#
# direction 字段表示此次观测激活的轴组合, 形如 "W" / "E+S" / "W+N" 等;
# 都不显著为 "none". 不存在 "ambiguous" —— X+Y 都激活时按上述规则各自反解.


@dataclass
class MapSizeSolveResult:
    """单次反解 map_size 观测的结果"""

    # E 激活时反解出 mw (单地图维度, 浮点)
    mw: Optional[float] = None
    # W 激活时反解出 mh
    mh: Optional[float] = None
    # S 激活时反解出 mw + mh 的和 (无法独立分离, 仅作参考/校验)
    mw_plus_mh: Optional[float] = None
    # 此次激活的轴组合: "W" / "E" / "S" / "N" / "W+S" / "W+N" / "E+S" / "E+N" / "none"
    # N 不携带 mw/mh 信息, 出现在组合里只是诚实记录该轴也激活了 (调试用)
    direction: str = "none"
    notes: list[str] = field(default_factory=list)

    @property
    def has_any(self) -> bool:
        return any(v is not None for v in (self.mw, self.mh, self.mw_plus_mh))


def solve_map_size_observation(
    pre_pos: tuple[int, int],
    screen_pos: tuple[float, float],
    view_area: tuple[float, float, float, float],
    block_size: tuple[float, float],
    character_pos: tuple[float, float],
    epsilon: float = 3e-3,
) -> MapSizeSolveResult:
    """
    根据一次观测 (玩家格坐标 + 玩家屏幕实际位置) 反解地图格数。

    几何 (与 compute_character_screen_pos 对偶, 见本节顶部模块注释):
      X 轴和 Y 轴独立判定, 互不影响. 一次观测可同时给出两个轴的有效偏移
      (例如玩家在地图正南边附近时, dx<0 给 mh, dy>0 给 mw+mh, 进而推 mw).

      X 轴判定 (|dx| > epsilon 时):
        dx < 0  →  W 激活, 反解 mh = 2*(px-vx0)/bw - gx + gy
        dx > 0  →  E 激活, 反解 mw = 2*(vx1-px)/bw + gx - gy
      Y 轴判定 (|dy| > epsilon 时):
        dy < 0  →  N 激活, 无 mw/mh 信息 (公式不含 mw/mh)
        dy > 0  →  S 激活, 反解 mw+mh = 2*(vy1-py)/bh + gx + gy
                   (mw 和 mh 无法独立分离, 仅作参考校验)

    一致性检查 (任一不通过 → 写 notes 警告 + 不更新该值):
      · 反解值必须 > 0
      · 反解值必须 ≥ 当前玩家对应坐标 - 0.5 格容差 (玩家正好在地图边沿是合法
        物理状态: gx=mw 或 gy=mh, 反解出来可能是 mw-0.x 或 mw+0.x; 真正"在地图外"
        必然差 ≥ 1 格, 落不进 (-0.5, +∞) 容差区间)
      · 反解值不应过大 (> 999), 过大说明几乎没贴边, 几何参数可能有误

    Args:
        pre_pos: 玩家当前格 (gx, gy), OCR 读出
        screen_pos: 玩家在屏幕上的实际归一化位置 (px, py), 用户在放大截图上指出
        view_area: (vx0, vy0, vx1, vy1) 已配置的可视区域
        block_size: (bw, bh) 当前视野的格屏幕尺寸 (归一化)
        character_pos: (cx, cy) 无约束时玩家屏幕中心
        epsilon: 偏移容差, < epsilon 视为该方向未贴边. 默认 3e-3 (≈ 屏幕 0.3%,
                 1920x1080 上约 5~6 像素), 与 solve_view_area_observation 一致.

    Returns:
        MapSizeSolveResult: 含本次能解出的 mw/mh/sum + 异常 notes
    """
    gx, gy = pre_pos
    px, py = screen_pos
    bw, bh = block_size
    cx, cy = character_pos
    vx0, vy0, vx1, vy1 = view_area
    out = MapSizeSolveResult()

    dx = px - cx
    dy = py - cy
    x_signal = abs(dx) >= epsilon
    y_signal = abs(dy) >= epsilon

    if not x_signal and not y_signal:
        out.direction = "none"
        out.notes.append(
            f"未检测到贴边偏移 (dx={dx:+.4f}, dy={dy:+.4f}, epsilon=±{epsilon}). "
            f"请把角色走到地图边缘附近再观测; 走到屏幕左/右两端可分别解出 mh/mw."
        )
        return out

    # 累积激活轴标签, 最后拼成 "W+S" 之类
    direction_parts: list[str] = []

    # ---- X 轴反解 ----
    if x_signal:
        if dx < 0:
            # W 激活: mh = 2*(px-vx0)/bw - gx + gy
            direction_parts.append("W")
            cand_mh = 2 * (px - vx0) / bw - gx + gy
            if cand_mh <= 0:
                out.notes.append(
                    f"W 激活但反解 mh={cand_mh:.2f} 非正, 看似异常 "
                    f"(px={px:.4f} 距 vx0={vx0:.4f} 太近). 不更新 mh."
                )
            elif cand_mh < gy - 0.5:
                # 玩家整数格坐标与浮点反解之间存在 ±0.X 格误差: 玩家正好站在
                # 地图最下边沿 (gy=mh) 是合法物理状态, 反解可能出 14.002 也可能
                # 13.998. 用 0.5 格容差区分"边界状态"和"真正越界" —— 真正越界
                # 至少差 1 格 (整数坐标 vs 反解出的小数), 不会落在 (gy-0.5, gy] 区间.
                out.notes.append(
                    f"W 激活但反解 mh={cand_mh:.2f} < gy={gy} - 0.5 容差 "
                    f"(玩家不可能在地图外). 不更新 mh; 检查 view_area / 点击精度."
                )
            elif cand_mh > 999:
                out.notes.append(
                    f"W 激活但反解 mh={cand_mh:.2f} > 999, 看似几乎没贴边. "
                    f"几何参数可能有误差; 不更新 mh."
                )
            else:
                out.mh = cand_mh
        else:
            # E 激活: mw = 2*(vx1-px)/bw + gx - gy
            direction_parts.append("E")
            cand_mw = 2 * (vx1 - px) / bw + gx - gy
            if cand_mw <= 0:
                out.notes.append(
                    f"E 激活但反解 mw={cand_mw:.2f} 非正, 看似异常 "
                    f"(px={px:.4f} 距 vx1={vx1:.4f} 太近). 不更新 mw."
                )
            elif cand_mw < gx - 0.5:
                # 0.5 格容差: 见 W 分支同款说明 (玩家正好在 gx=mw 边沿是合法状态).
                out.notes.append(
                    f"E 激活但反解 mw={cand_mw:.2f} < gx={gx} - 0.5 容差 "
                    f"(玩家不可能在地图外). 不更新 mw; 检查 view_area / 点击精度."
                )
            elif cand_mw > 999:
                out.notes.append(
                    f"E 激活但反解 mw={cand_mw:.2f} > 999, 看似几乎没贴边. "
                    f"几何参数可能有误差; 不更新 mw."
                )
            else:
                out.mw = cand_mw

    # ---- Y 轴反解 ----
    if y_signal:
        if dy < 0:
            # N 激活: 公式不含 mw/mh, 无信息. 仍在 direction 里记录, 提示用户
            # 此次 Y 轴贡献为 0 (避免 UI 上以为 Y 轴在帮忙).
            direction_parts.append("N")
            out.notes.append(
                f"N 激活 (dy={dy:+.4f}): 但 N 顶点 (0, 0) 的激活公式 "
                f"py = vy0 + (gx+gy)*bh/2 不含 mw/mh, Y 轴本次无信息可解. "
                f"X 轴若同时激活则照常反解; 仅 N 激活的话请改走屏幕**左/右/下**端."
            )
        else:
            # S 激活: mw+mh = 2*(vy1-py)/bh + gx + gy
            direction_parts.append("S")
            cand_sum = 2 * (vy1 - py) / bh + gx + gy
            if cand_sum <= 0:
                out.notes.append(
                    f"S 激活但反解 mw+mh={cand_sum:.2f} 非正, 看似异常. 不更新."
                )
            elif cand_sum < gx + gy - 0.5:
                # 0.5 格容差: 见 W 分支同款说明.
                out.notes.append(
                    f"S 激活但反解 mw+mh={cand_sum:.2f} < gx+gy={gx+gy} - 0.5 容差 "
                    f"(玩家不可能在地图外). 不更新; 检查 view_area / 点击精度."
                )
            else:
                out.mw_plus_mh = cand_sum
                out.notes.append(
                    f"S 激活: 解出 mw+mh≈{cand_sum:.2f} (无法独立分离). "
                    f"配合 X 轴反解或之前的观测可推出另一维度."
                )

    out.direction = "+".join(direction_parts) if direction_parts else "none"
    return out


# =============================================================================
# Registry
# =============================================================================


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
