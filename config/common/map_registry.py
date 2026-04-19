"""
map_registry.py - 地图跳转信息数据层

YAML 结构（按分辨率 profile 分组）：

    profiles:
      "1920x1080":
        bigmap_size_pixel: [2600, 1450]
        locations:
          洛阳:
            icon_on_bigmap_pixel: [2140, 880]    # 大地图绝对像素
            btn_offset_pixel: [-25, 250]          # 按钮相对图标的像素偏移
            recorded_at_corner: NW                # 元数据
      "2560x1440":
        bigmap_size_pixel: [3467, 1933]
        locations: {}

运行时一律通过 `ensure_profile(resolution)` 取当前分辨率 profile，操作的是
绝对像素（以该 profile 的 resolution 为基准）。派生的归一化值由
`CoordSystem` 按需计算，不落盘。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

import yaml

log = logging.getLogger(__name__)


# =============================================================================
# 常量
# =============================================================================

# 初始内置地名（旧 config 里出现过的，用户可在 UI 上增删）
DEFAULT_LOCATIONS = (
    "敦煌",
    "落霞镇",
    "嵩山",
    "泰山",
    "幽州",
    "长白山",
    "姑苏",
    "杭州",
    "泉州",
    "龙泉镇",
    "南岭",
    "衡山",
    "双王镇",
    "南阳渡",
    "太乙山",
    "凤鸣集",
    "明月峰",
    "成都",
    "峨眉山",
    "洛阳",
    "华山",
    "十方集",
)

DEFAULT_YAML_PATH = Path("config/common/map_switch_btn_position.yaml")

# ─── 游戏 UI 常量 ─────────────────────────────────────────────────────────────
# 跳转按钮 y 方向的"地板"：当按钮本应落在屏幕更下方时，游戏会把它顶到这个 y 位置。
# 该常量与分辨率无关（归一化值），放在 bigmap_constraints 顶层跨 profile 共享。
DEFAULT_BTN_FLOOR_Y = 0.9428
# 录入时判定"按钮刚好被地板顶住"的容差（归一化）；鼠标取点小抖动在此范围内。
DEFAULT_BTN_FLOOR_EPS = 0.015


# =============================================================================
# 角落枚举
# =============================================================================


class Corner(str, Enum):
    NW = "NW"
    NE = "NE"
    SW = "SW"
    SE = "SE"

    @property
    def label(self) -> str:
        return {"NW": "西北", "NE": "东北", "SW": "西南", "SE": "东南"}[self.value]

    @property
    def unit_anchor(self) -> tuple[int, int]:
        """该角对应相机左上角在 (bigmap - view) 上的单位化位置: (0 或 1, 0 或 1)"""
        return {
            "NW": (0, 0),
            "NE": (1, 0),
            "SW": (0, 1),
            "SE": (1, 1),
        }[self.value]


# =============================================================================
# 数据类
# =============================================================================


@dataclass
class LocationRecord:
    """单个地点在某个 profile（分辨率）下的录入信息，坐标为绝对像素"""

    icon_on_bigmap_pixel: Optional[tuple[float, float]] = None  # 大地图绝对位置
    btn_offset_pixel: Optional[tuple[float, float]] = None  # 按钮偏移
    recorded_at_corner: Optional[Corner] = None  # 录入时贴的角

    # ── 地图几何属性（与分辨率无关，但放在 profile 里方便整体编辑） ──
    map_size: Optional[tuple[int, int]] = None  # (width, height) 地图格数
    vision_size: Optional[str] = None  # "小" / "中" / "大"

    @property
    def is_recorded(self) -> bool:
        return (
            self.icon_on_bigmap_pixel is not None and self.btn_offset_pixel is not None
        )

    def to_dict(self) -> dict:
        d: dict = {}
        if self.icon_on_bigmap_pixel is not None:
            d["icon_on_bigmap_pixel"] = [
                round(self.icon_on_bigmap_pixel[0], 2),
                round(self.icon_on_bigmap_pixel[1], 2),
            ]
        if self.btn_offset_pixel is not None:
            d["btn_offset_pixel"] = [
                round(self.btn_offset_pixel[0], 2),
                round(self.btn_offset_pixel[1], 2),
            ]
        if self.recorded_at_corner is not None:
            d["recorded_at_corner"] = self.recorded_at_corner.value
        if self.map_size is not None:
            d["map_size"] = list(self.map_size)
        if self.vision_size is not None:
            d["vision_size"] = self.vision_size
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "LocationRecord":
        icon = d.get("icon_on_bigmap_pixel")
        btn = d.get("btn_offset_pixel")
        corner_raw = d.get("recorded_at_corner")
        map_size_raw = d.get("map_size")
        return cls(
            icon_on_bigmap_pixel=tuple(icon) if icon else None,
            btn_offset_pixel=tuple(btn) if btn else None,
            recorded_at_corner=Corner(corner_raw) if corner_raw else None,
            map_size=(tuple(map_size_raw) if map_size_raw else None),
            vision_size=d.get("vision_size"),
        )


@dataclass
class BigmapConstraints:
    """
    游戏 UI 的全局约束（与分辨率无关，归一化值）。
    目前只有一个: 跳转按钮 y 方向地板。若未来发现其他方向也有贴边行为，在此扩展。
    """

    btn_floor_y: float = DEFAULT_BTN_FLOOR_Y
    btn_floor_eps: float = DEFAULT_BTN_FLOOR_EPS

    def to_dict(self) -> dict:
        return {
            "btn_floor_y": self.btn_floor_y,
            "btn_floor_eps": self.btn_floor_eps,
        }

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> "BigmapConstraints":
        if not d:
            return cls()
        return cls(
            btn_floor_y=float(d.get("btn_floor_y", DEFAULT_BTN_FLOOR_Y)),
            btn_floor_eps=float(d.get("btn_floor_eps", DEFAULT_BTN_FLOOR_EPS)),
        )


@dataclass
class Profile:
    """一个分辨率下的完整录入信息"""

    resolution: tuple[int, int]  # (w, h)
    bigmap_size_pixel: tuple[int, int]  # 大地图像素尺寸（该分辨率下）
    locations: dict[str, LocationRecord] = field(default_factory=dict)
    # 明确被"非 UI 默认合并"途径放入的地名。
    # 用于区分 DEFAULT_LOCATIONS 里的 UI 占位 vs. 迁移/用户主动新建的空地名。
    # 被加到这里的名字，即使 record 为空，也会持久化为 `地名: {}`。
    explicit_names: set[str] = field(default_factory=set)

    @property
    def key(self) -> str:
        return f"{self.resolution[0]}x{self.resolution[1]}"

    def mark_explicit(self, name: str) -> None:
        self.explicit_names.add(name)

    def to_dict(self) -> dict:
        return {
            "bigmap_size_pixel": list(self.bigmap_size_pixel),
            "locations": {
                name: rec.to_dict()
                for name, rec in self.locations.items()
                # 持久化条件：有字段 或 明确被引用 或 不是纯 UI 占位默认名
                if rec.to_dict()
                or name in self.explicit_names
                or name not in DEFAULT_LOCATIONS
            },
        }

    @classmethod
    def from_dict(cls, key: str, d: dict) -> "Profile":
        w_str, h_str = key.split("x")
        bigmap = d.get("bigmap_size_pixel") or [2600, 1450]
        locs_raw = d.get("locations") or {}
        # 加载时：yaml 里明确出现的所有地名都视为 explicit（空 record 说明是故意保留的占位）
        locations = {n: LocationRecord.from_dict(r) for n, r in locs_raw.items()}
        explicit_names = set(locs_raw.keys())
        return cls(
            resolution=(int(w_str), int(h_str)),
            bigmap_size_pixel=(int(bigmap[0]), int(bigmap[1])),
            locations=locations,
            explicit_names=explicit_names,
        )


# =============================================================================
# 坐标系（运行时计算）
# =============================================================================


@dataclass(frozen=True)
class CoordSystem:
    """
    给定一个 Profile 和全局 BigmapConstraints，把"大地图绝对像素"和"可点击
    区域归一化"之间的换算封装起来。

    术语:
      res            : 可点击区域的参考分辨率 (Rw, Rh)
      bigmap_px      : 大地图像素尺寸 (Bw, Bh)
      bigmap_norm    : 大地图归一化尺寸 = (Bw/Rw, Bh/Rh)
      view size      : 可点击区域归一化 = (1, 1)
    """

    profile: Profile
    constraints: BigmapConstraints = field(default_factory=BigmapConstraints)

    @property
    def res(self) -> tuple[int, int]:
        return self.profile.resolution

    @property
    def bigmap_norm(self) -> tuple[float, float]:
        bw, bh = self.profile.bigmap_size_pixel
        rw, rh = self.profile.resolution
        return (bw / rw, bh / rh)

    # -------- 绝对像素 ↔ 归一化 --------

    def icon_abs_to_norm(self, p_px: tuple[float, float]) -> tuple[float, float]:
        """大地图像素 → 大地图归一化"""
        rw, rh = self.profile.resolution
        return (p_px[0] / rw, p_px[1] / rh)

    def icon_norm_to_abs(self, p_norm: tuple[float, float]) -> tuple[float, float]:
        rw, rh = self.profile.resolution
        return (p_norm[0] * rw, p_norm[1] * rh)

    def offset_abs_to_norm(self, o_px: tuple[float, float]) -> tuple[float, float]:
        rw, rh = self.profile.resolution
        return (o_px[0] / rw, o_px[1] / rh)

    # -------- 相机 & 录入 --------

    def camera_origin_at_corner(self, corner: Corner) -> tuple[float, float]:
        """贴角时相机左上角的归一化坐标"""
        bw_n, bh_n = self.bigmap_norm
        ax, ay = corner.unit_anchor
        # 相机左上角可移动范围 [0, bigmap - view]，view = 1
        return (ax * (bw_n - 1), ay * (bh_n - 1))

    def pick_to_bigmap_abs(
        self,
        pick_norm: tuple[float, float],
        corner: Corner,
    ) -> tuple[float, float]:
        """
        录入时把"贴在某个角的可点击区域归一化坐标"反推成"大地图绝对像素"。
        贴角时相机位置 Cx, Cy 由角唯一决定。
        """
        cx, cy = self.camera_origin_at_corner(corner)
        abs_norm = (cx + pick_norm[0], cy + pick_norm[1])
        return self.icon_norm_to_abs(abs_norm)

    # -------- 从某起点看某目标（运行时） --------

    def camera_origin_for_src(
        self, src_icon_abs_px: tuple[float, float]
    ) -> tuple[float, float]:
        """
        给定起点地点的大地图绝对像素位置，计算进入地图选择界面时相机左上角归一化坐标。
        规则: 相机尽量把起点放在可点击区域中心，碰边则贴边。
        """
        bw_n, bh_n = self.bigmap_norm
        sx_n, sy_n = self.icon_abs_to_norm(src_icon_abs_px)
        cx = _clamp(sx_n - 0.5, 0.0, bw_n - 1.0)
        cy = _clamp(sy_n - 0.5, 0.0, bh_n - 1.0)
        return (cx, cy)

    def target_in_view(
        self,
        src_rec: LocationRecord,
        tgt_rec: LocationRecord,
    ) -> Optional[tuple[tuple[float, float], tuple[float, float]]]:
        """
        从 src 起点看 tgt 目标，返回 (icon_norm_in_view, btn_norm_in_view)。
        任一点越出 [0, 1] 返回 None（不可见 / 不可点）。
        按钮 y 若超过 `btn_floor_y` 会被游戏顶到地板，此处同样 clamp 以匹配真实位置。
        """
        if not src_rec.is_recorded or not tgt_rec.is_recorded:
            return None
        cx, cy = self.camera_origin_for_src(src_rec.icon_on_bigmap_pixel)
        tx_n, ty_n = self.icon_abs_to_norm(tgt_rec.icon_on_bigmap_pixel)
        icon_in = (tx_n - cx, ty_n - cy)
        off_n = self.offset_abs_to_norm(tgt_rec.btn_offset_pixel)
        btn_raw = (icon_in[0] + off_n[0], icon_in[1] + off_n[1])

        # 按钮被屏幕下沿顶住：游戏行为是贴到 floor；点击坐标也要 clamp 过去
        floor = self.constraints.btn_floor_y
        btn_in = (btn_raw[0], min(btn_raw[1], floor))

        # 其他方向仍按 [0, 1] 判定
        if not _in_unit(icon_in):
            return None
        if not (0.0 - 1e-9 <= btn_in[0] <= 1.0 + 1e-9):
            return None
        if btn_in[1] < -1e-9:
            return None
        # btn_in[1] 在上面 clamp 之后必然 <= floor <= 1
        return icon_in, btn_in


def _clamp(v: float, lo: float, hi: float) -> float:
    if hi < lo:
        # bigmap 比 view 小的异常情况；强制为中心
        return (lo + hi) / 2
    return max(lo, min(hi, v))


def _in_unit(p: tuple[float, float], eps: float = 1e-9) -> bool:
    return -eps <= p[0] <= 1 + eps and -eps <= p[1] <= 1 + eps


# =============================================================================
# Registry（顶层容器）
# =============================================================================


@dataclass
class MapRegistry:
    """对应整份 yaml 的顶层数据"""

    profiles: dict[str, Profile] = field(default_factory=dict)
    constraints: BigmapConstraints = field(default_factory=BigmapConstraints)
    path: Optional[Path] = None

    # ---- IO ----

    @classmethod
    def load(cls, path: Path = DEFAULT_YAML_PATH) -> "MapRegistry":
        if not path.exists():
            log.info("registry 文件不存在，新建空 registry: %s", path)
            return cls(path=path)
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception:
            log.exception("读取 registry 失败: %s", path)
            raise
        profiles_raw = data.get("profiles") or {}
        profiles = {key: Profile.from_dict(key, pd) for key, pd in profiles_raw.items()}
        constraints = BigmapConstraints.from_dict(data.get("bigmap_constraints"))
        return cls(profiles=profiles, constraints=constraints, path=path)

    def save(self, path: Optional[Path | str] = None) -> Path:
        target = Path(path) if path else (self.path or DEFAULT_YAML_PATH)
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "bigmap_constraints": self.constraints.to_dict(),
            "profiles": {key: p.to_dict() for key, p in self.profiles.items()},
        }
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
        log.info("registry 已保存: %s", target)
        return target

    # ---- profile ----

    def ensure_profile(
        self,
        resolution: tuple[int, int],
        default_bigmap_pixel: tuple[int, int] = (2600, 1450),
    ) -> Profile:
        """
        新建或获取某个分辨率的 profile。
        不会注入 `DEFAULT_LOCATIONS` —— 默认地名只用于 UI 展示时合并，
        避免写回 yaml 时把一堆空 entry 持久化。
        """
        key = f"{resolution[0]}x{resolution[1]}"
        if key not in self.profiles:
            log.info("为分辨率 %s 新建 profile", key)
            self.profiles[key] = Profile(
                resolution=resolution,
                bigmap_size_pixel=default_bigmap_pixel,
            )
        return self.profiles[key]
