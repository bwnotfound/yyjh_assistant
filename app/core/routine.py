"""
Routine 数据模型。

一份 routine 描述一个"完整的自动化脚本"：从某张地图开始，依次执行若干步骤。
每种步骤是一个 dataclass，YAML 里用 `type` 字段判别。

示例:
    name: 自动购买
    description: 跑各地杂货铺
    loop_count: 0            # 0 = 无限
    loop_interval: 12        # 每轮之间秒数
    steps:
      - type: travel
        to: 洛阳
      - type: move
        at_map: 洛阳
        path:
          - [10, 18]
          - [14, 22]
          - [-1, -1]
          - [20, 30]
      - type: button
        name: table_2
        skip: 1
      - type: include
        routine: 子流程         # 引用 config/routines/子流程.yaml
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Union

import yaml

log = logging.getLogger(__name__)


DEFAULT_ROUTINES_DIR = Path("config/routines")


# =============================================================================
# 步骤基类 + 各具体类型
# =============================================================================


@dataclass
class Step:
    """步骤基类。每个子类有固定的 `TYPE` 字符串，对应 YAML 的 type 字段。"""

    TYPE: str = field(default="", init=False, repr=False)
    at_map: Optional[str] = None  # 可选的 sanity check / 文档注释

    def to_dict(self) -> dict:
        d = {"type": self.TYPE}
        if self.at_map is not None:
            d["at_map"] = self.at_map
        return d


@dataclass
class TravelStep(Step):
    """大地图传送到某个已录入的地点"""

    to: str = ""

    def __post_init__(self) -> None:
        self.TYPE = "travel"
        if not self.to:
            raise ValueError("travel 步骤必须指定 to")

    def to_dict(self) -> dict:
        return {**super().to_dict(), "to": self.to}


@dataclass
class MoveStep(Step):
    """在当前地图内走一段路径。path 的第一个点即起点；`[-1, -1]` 表示飞行。"""

    path: list[tuple[int, int]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.TYPE = "move"
        if not self.path:
            raise ValueError("move 步骤 path 不能为空")

    def to_dict(self) -> dict:
        return {**super().to_dict(), "path": [list(p) for p in self.path]}


@dataclass
class ButtonStep(Step):
    """
    点击菜单按钮。两种模式 (name 与 template 互斥, 必有其一):
      - 直接指定: name = "chat_N" / "table_N", 用 step 自己的 skip/delay
      - 模板模式: template = "进入小屋" 等, 整套 (name+skip+delay) 由
                  movement_profile.button_templates[name] 提供, step 自己的
                  name/skip/delay 在运行时被忽略 (但仍持久化以便切回时兜底;
                  to_dict 在 template 模式下只写 type/at_map/template, 避免歧义)

    delay 字段两种模式 (delay_preset 与 delay 互斥):
      - 字面值: delay_preset = None, 用 delay (float)
      - 预设:   delay_preset = "click" / "切场动画" / ..., 运行时从
                movement_profile.click_delays.resolve() 解析为秒数

    name 语法:
      - "table_N" -> ui.table_btn_pos_list[N-1]
      - "chat_N"  -> ui.chat_btn_pos_list[N-1]
    """

    name: str = ""
    template: Optional[str] = None
    skip: int = 0  # 按完后再点 skip 次 blank 按钮跳过对话
    delay: float = 0.0  # 整个动作完成后的等待秒数 (delay_preset 空时使用)
    delay_preset: Optional[str] = None  # ClickDelays 字段名 / custom 名

    def __post_init__(self) -> None:
        self.TYPE = "button"
        if self.name and self.template:
            raise ValueError(
                f"button 步骤的 name ({self.name!r}) 和 template "
                f"({self.template!r}) 互斥, 不能同时指定"
            )
        if not self.name and not self.template:
            raise ValueError("button 步骤必须指定 name 或 template 之一")
        if self.delay and self.delay_preset:
            raise ValueError(
                f"button 步骤的 delay ({self.delay}) 和 delay_preset "
                f"({self.delay_preset!r}) 互斥, 不能同时指定"
            )

    def to_dict(self) -> dict:
        # template 模式: 整个 button 行为由模板决定, step 自己的 name/skip/delay
        # 都被忽略, 所以 yaml 里只写 type/at_map/template, 避免歧义
        if self.template:
            d = super().to_dict()
            d["template"] = self.template
            return d
        # 非 template 模式: 像以前一样
        d = {**super().to_dict(), "name": self.name}
        if self.skip:
            d["skip"] = self.skip
        if self.delay_preset:
            d["delay_preset"] = self.delay_preset
        elif self.delay:
            d["delay"] = self.delay
        return d


@dataclass
class ClickStep(Step):
    """
    在归一化坐标处点击。

    三种模式 (preset / template 互斥, 都不填则用 pos):
      - 自定义:    preset=None, template=None, 用 pos = (x, y) + 自己的 skip/delay
      - 位置预设:  preset="blank_btn" / 自建预设, 仅位置由预设解析,
                   skip / delay 仍走 ClickStep 自己的字段
      - click 模板: template="跳3次对话" 等, 整个 click 行为 (位置+skip+delay)
                   全部由 movement_profile.click_templates[name] 提供,
                   ClickStep 自己的 pos / preset / skip / delay 在运行时被忽略
                   (但仍持久化以便切回时兜底; to_dict 在 template 模式下只
                   写出 type/at_map/template, 避免 yaml 里出现冗余字段)

    合法 preset 名见 CLICK_PRESETS;
    合法 template 名取决于 movement_profile.click_templates 的 key。
    """

    pos: tuple[float, float] = (0.0, 0.0)
    preset: Optional[str] = None
    template: Optional[str] = None
    delay: float = 0.0
    delay_preset: Optional[str] = None  # ClickDelays 字段名 / custom 名
    skip: int = 0  # 点完后再点 skip 次 blank

    def __post_init__(self) -> None:
        self.TYPE = "click"
        if self.preset and self.template:
            raise ValueError(
                f"click 步骤的 preset ({self.preset!r}) 和 template "
                f"({self.template!r}) 互斥, 不能同时指定"
            )
        if self.delay and self.delay_preset:
            raise ValueError(
                f"click 步骤的 delay ({self.delay}) 和 delay_preset "
                f"({self.delay_preset!r}) 互斥, 不能同时指定"
            )

    def to_dict(self) -> dict:
        # template 模式: 整个 click 行为由模板决定, step 自己的 pos/preset/skip/delay
        # 都被忽略, 所以 yaml 里只写 type/at_map/template, 避免歧义
        if self.template:
            d = super().to_dict()
            d["template"] = self.template
            return d
        # 非 template 模式: 像以前一样
        d = {**super().to_dict(), "pos": list(self.pos)}
        if self.preset:
            d["preset"] = self.preset
        if self.delay_preset:
            d["delay_preset"] = self.delay_preset
        elif self.delay:
            d["delay"] = self.delay
        if self.skip:
            d["skip"] = self.skip
        return d


# Click 步骤支持的预设位置清单 (preset_name, label)。
# 预设名严格对应 movement_profile 里的字段名:
#   - UIPositions 单点字段: package_btn / ticket_btn / blank_btn /
#                           buy_increase_btn / buy_confirm_btn / buy_exit_btn
#   - MovementProfile 顶层字段: character_pos
# 等距按钮组 (chat / table) 和商品栅格 (buy_item_grid) 不在此列 ——
# 它们已分别由 ButtonStep / BuyStep 覆盖，避免 UX 重复。
CLICK_PRESETS: list[tuple[str, str]] = [
    ("blank_btn", "空白处（跳对话）"),
    ("package_btn", "背包按钮"),
    ("ticket_btn", "车票按钮"),
    ("buy_increase_btn", "购买-数量 +1"),
    ("buy_confirm_btn", "购买-确认"),
    ("buy_exit_btn", "购买-退出"),
    ("character_pos", "角色身上"),
]
CLICK_PRESET_NAMES: set[str] = {name for name, _ in CLICK_PRESETS}


@dataclass
class BuyStep(Step):
    """在购买界面依次购买。items: [[商品索引, 数量], ...]"""

    items: list[tuple[int, int]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.TYPE = "buy"
        if not self.items:
            raise ValueError("buy 步骤 items 不能为空")

    def to_dict(self) -> dict:
        return {**super().to_dict(), "items": [list(i) for i in self.items]}


@dataclass
class SleepStep(Step):
    """
    定时等待。两种模式:
      - 自定义: preset = None, 用 seconds 字段
      - 预设:   preset = ClickDelays 字段名 (内置 16 个 + custom), 运行时由
                movement_profile.click_delays.resolve() 动态解析为秒数。
                seconds 字段在 preset 模式下被忽略, 但仍持久化以便切回自定义时兜底。

    preset 名的合法性在 dataclass 层不强校验 — 因为 ClickDelays.custom 可以
    包含任意自建预设, 加载时未必能拿到 movement_profile 引用。具体合法性由
    routine 编辑器的保存校验和 runner 的 resolve 处理 (resolve 找不到时回退
    到 click_delays.default)。
    """

    seconds: float = 0.0
    preset: Optional[str] = None

    def __post_init__(self) -> None:
        self.TYPE = "sleep"

    def to_dict(self) -> dict:
        d = {**super().to_dict(), "seconds": self.seconds}
        if self.preset:
            d["preset"] = self.preset
        return d


# Sleep 步骤支持的预设清单 (preset_name, label)。
# preset_name 严格对应 movement_profile.click_delays 的字段名 —— 运行时通过
# ClickDelays.resolve(name) 解析为秒数。
# 排序按 routine 实际使用频率 + 业务相关分组, UI 下拉直接按这个顺序展示。
SLEEP_PRESETS: list[tuple[str, str]] = [
    # ── 通用 ──
    ("default", "默认延时（兜底）"),
    # ── 切图 / 跳对话 (routine 里 sleep 最常用的两类) ──
    ("travel_transition", "切图过场 (~3s)"),
    ("blank_skip", "点空白跳对话"),
    # ── 通用点击后 ──
    ("button", "按钮点击后"),
    ("click", "click step 后"),
    # ── 购买流程 ──
    ("buy_item", "购买：选商品后"),
    ("buy_increase", "购买：数量 +1 后"),
    ("buy_confirm", "购买：确认后"),
    ("buy_exit", "购买：退出后"),
    # ── Travel 流程 ──
    ("open_package", "打开背包后"),
    ("ticket", "点车票后"),
    ("travel_icon", "点目标图标后"),
    ("travel_confirm", "跳转确认后"),
    # ── 飞行段 (普通 routine 不太用，但留着) ──
    ("fly", "飞行：点角色后 (~0.8s)"),
    ("fly_settle", "飞行：落地等待 (~3s)"),
    ("move_step", "move 段间隔"),
]
SLEEP_PRESET_NAMES: set[str] = {name for name, _ in SLEEP_PRESETS}


@dataclass
class WaitPosStableStep(Step):
    """等待小地图坐标数字稳定（移动完成的指示）"""

    threshold: float = 0.02
    max_wait: float = 3.0
    fps: float = 10.0

    def __post_init__(self) -> None:
        self.TYPE = "wait_pos_stable"

    def to_dict(self) -> dict:
        d = super().to_dict()
        d["threshold"] = self.threshold
        d["max_wait"] = self.max_wait
        d["fps"] = self.fps
        return d


@dataclass
class WaitScreenStableStep(Step):
    """等待画面稳定（切图 / 过场动画结束指示）"""

    threshold: float = 0.05
    max_wait: float = 5.0
    fps: float = 10.0

    def __post_init__(self) -> None:
        self.TYPE = "wait_screen_stable"

    def to_dict(self) -> dict:
        d = super().to_dict()
        d["threshold"] = self.threshold
        d["max_wait"] = self.max_wait
        d["fps"] = self.fps
        return d


@dataclass
class EnterMapStep(Step):
    """
    宣告"当前地图已切换为某地图"。runner 只更新 _current_map，不做实际操作。
    用途: 走路过地图边界后，告诉后续 move 步骤新的地图上下文。
    """

    map: str = ""

    def __post_init__(self) -> None:
        self.TYPE = "enter_map"
        if not self.map:
            raise ValueError("enter_map 步骤必须指定 map")

    def to_dict(self) -> dict:
        return {**super().to_dict(), "map": self.map}


@dataclass
class IncludeStep(Step):
    """
    串联执行另一个 routine 文件。

    `routine` 字段:
      - 文件名 (不含扩展名)，runner 会在 config/routines/ 下找 <name>.yaml
      - 也可写绝对路径或相对当前工作目录的路径

    被 include 的子 routine:
      * loop_count / loop_interval / starting_map 字段被忽略
        (子 routine 在父级里只是一段 step 序列；要循环就在父级里多写几次 include)
      * at_map / 当前位置上下文继承父级
      * 防环: runner 维护 include 调用栈，递归引用直接抛错
    """

    routine: str = ""

    def __post_init__(self) -> None:
        self.TYPE = "include"
        if not self.routine:
            raise ValueError("include 步骤必须指定 routine")

    def to_dict(self) -> dict:
        return {**super().to_dict(), "routine": self.routine}


AnyStep = Union[
    TravelStep,
    MoveStep,
    ButtonStep,
    ClickStep,
    BuyStep,
    SleepStep,
    WaitPosStableStep,
    WaitScreenStableStep,
    EnterMapStep,
    IncludeStep,
]


# 类型字符串 → 构造器
_STEP_REGISTRY: dict[str, type] = {
    "travel": TravelStep,
    "move": MoveStep,
    "button": ButtonStep,
    "click": ClickStep,
    "buy": BuyStep,
    "sleep": SleepStep,
    "wait_pos_stable": WaitPosStableStep,
    "wait_screen_stable": WaitScreenStableStep,
    "enter_map": EnterMapStep,
    "include": IncludeStep,
}


def step_from_dict(d: dict) -> AnyStep:
    """根据 type 字段派发构造。"""
    t = d.get("type")
    if t not in _STEP_REGISTRY:
        raise ValueError(f"未知步骤类型: {t!r}；可选 {list(_STEP_REGISTRY)}")
    cls = _STEP_REGISTRY[t]
    kwargs = {k: v for k, v in d.items() if k != "type"}
    # 元组化几个常见字段，避免后续手动转
    if "pos" in kwargs and isinstance(kwargs["pos"], list):
        kwargs["pos"] = tuple(kwargs["pos"])
    if "path" in kwargs and isinstance(kwargs["path"], list):
        kwargs["path"] = [tuple(p) for p in kwargs["path"]]
    if "items" in kwargs and isinstance(kwargs["items"], list):
        kwargs["items"] = [tuple(i) for i in kwargs["items"]]
    return cls(**kwargs)


# =============================================================================
# Routine
# =============================================================================


@dataclass
class Routine:
    name: str
    steps: list[AnyStep] = field(default_factory=list)
    description: str = ""
    loop_count: int = 1  # 0 = 无限
    loop_interval: float = 0.0  # 每轮之间秒数 (loop_interval_preset 空时使用)
    loop_interval_preset: Optional[str] = None  # ClickDelays 字段名 / custom 名
    # 起始地图: travel 步骤计算相机位置需要知道"当前在哪张地图"。
    # 若 routine 第一步就是 travel，必须设置此字段；否则可由首次 move 的 at_map 隐式推出。
    starting_map: Optional[str] = None
    path: Optional[Path] = None

    def __post_init__(self) -> None:
        if self.loop_interval and self.loop_interval_preset:
            raise ValueError(
                f"Routine 的 loop_interval ({self.loop_interval}) 和 "
                f"loop_interval_preset ({self.loop_interval_preset!r}) 互斥"
            )

    def to_dict(self) -> dict:
        d: dict = {"name": self.name}
        if self.description:
            d["description"] = self.description
        d["loop_count"] = self.loop_count
        if self.loop_interval_preset:
            d["loop_interval_preset"] = self.loop_interval_preset
        else:
            d["loop_interval"] = self.loop_interval
        if self.starting_map is not None:
            d["starting_map"] = self.starting_map
        d["steps"] = [s.to_dict() for s in self.steps]
        return d

    @classmethod
    def from_dict(cls, d: dict, path: Optional[Path] = None) -> "Routine":
        steps_raw = d.get("steps") or []
        steps = [step_from_dict(s) for s in steps_raw]
        return cls(
            name=d.get("name") or (path.stem if path else "unnamed"),
            description=d.get("description", ""),
            loop_count=int(d.get("loop_count", 1)),
            loop_interval=float(d.get("loop_interval", 0.0)),
            loop_interval_preset=d.get("loop_interval_preset"),
            starting_map=d.get("starting_map"),
            steps=steps,
            path=path,
        )

    @classmethod
    def load(cls, path: Path) -> "Routine":
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return cls.from_dict(data, path=path)

    def save(self, path: Optional[Path | str] = None) -> Path:
        target = Path(path) if path else self.path
        if target is None:
            raise ValueError("routine 未指定保存路径")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            yaml.safe_dump(
                self.to_dict(),
                allow_unicode=True,
                sort_keys=False,
                indent=2,
                default_flow_style=None,
            ),
            encoding="utf-8",
        )
        self.path = target
        log.info("routine 已保存: %s", target)
        return target

    def summary(self) -> str:
        """单行概述（给 UI 预览用）"""
        return f"{self.name} ({len(self.steps)} 步)"


# =============================================================================
# 索引
# =============================================================================


def list_routines(dir_path: Path = DEFAULT_ROUTINES_DIR) -> list[Path]:
    if not dir_path.exists():
        return []
    return sorted(dir_path.glob("*.yaml"))
