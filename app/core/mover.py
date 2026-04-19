"""
Mover - 运动执行器

职责:
  1. 根据"起点格坐标 + 目标格坐标 + 地图尺寸 + 视野档位"计算点击位置
  2. 处理相机贴边时角色 sprite 位置的偏移
  3. 执行一段 path，含飞行标记
  4. 暴露 wait_pos_stable / wait_screen_stable

和 `CoordSystem` (大地图) 是同构但规模不同的两套"相机贴边修正"，这里就是小地图
(地图场景) 版本。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable, Optional

from PIL import Image

from utils import Mumu
from app.core.profiles import MovementProfile, VisionSpec

log = logging.getLogger(__name__)


# 飞行标记
FLY = (-1, -1)

# 等待屏幕变化时裁剪的 ROI（归一化），用于去掉边框干扰
_SCREEN_DIFF_CROP = (0.032, 0.080, 0.841, 0.902)


@dataclass
class MapContext:
    """执行移动时需要的上下文"""

    # 地图格数 (width, height)；为 None 表示"路径不会触碰屏幕边缘"，
    # 此时跳过贴边修正（角色永远在屏幕中心 character_pos 处）。
    map_size: Optional[tuple[int, int]]
    vision: VisionSpec
    minimap_coord_roi: Optional[tuple[float, float, float, float]] = None


class Mover:
    """
    有状态的移动执行器：维护"当前格坐标"作为 move 的起点。
    """

    def __init__(
        self,
        mumu: Mumu,
        profile: MovementProfile,
    ) -> None:
        self.mumu = mumu
        self.profile = profile
        self._cur_pos: Optional[tuple[int, int]] = None

    # ========================================================================
    # 状态
    # ========================================================================

    @property
    def current_pos(self) -> Optional[tuple[int, int]]:
        return self._cur_pos

    def set_current_pos(self, pos: tuple[int, int]) -> None:
        self._cur_pos = pos

    # ========================================================================
    # 几何：计算角色 sprite 在屏幕上的修正位置 + 点击坐标
    # ========================================================================

    def _character_screen_pos(
        self,
        pre_pos: tuple[int, int],
        ctx: MapContext,
    ) -> tuple[float, float]:
        """
        根据"移动前格坐标 + 地图尺寸 + 视野"算出角色 sprite 在屏幕上的位置。

        相机尽量把角色放屏幕中心，但当角色靠近地图四个角时，相机会贴边，
        导致角色 sprite 偏离屏幕中心。这里按四角的最小曼哈顿距离做修正。
        若 ctx.map_size 为 None，视为"路径不会触碰边缘"，不做修正。
        """
        cx, cy = self.profile.character_pos
        if ctx.map_size is None:
            return (cx, cy)

        bw, bh = ctx.vision.block_size
        vdl = ctx.vision.vision_delta_limit
        mw, mh = ctx.map_size

        corners = (
            (0, 0),
            (mw, 0),
            (0, mh),
            (mw - 1, mh - 1),
        )
        offsets = (
            (0.0, -0.5),  # NW: 角色偏向屏幕上
            (0.5, 0.0),  # NE: 角色偏向屏幕右
            (-0.5, 0.0),  # SW: 角色偏向屏幕左
            (0.0, 0.5),  # SE: 角色偏向屏幕下
        )

        min_delta = None
        min_idx: Optional[int] = None
        for i, corner in enumerate(corners):
            d = abs(corner[0] - pre_pos[0]) + abs(corner[1] - pre_pos[1])
            if d > vdl:
                continue
            if min_delta is None or d < min_delta:
                min_delta = d
                min_idx = i

        if min_idx is None:
            return (cx, cy)

        # SE 角的 corner 用 (mw-1, mh-1) 但相机贴边时 bigmap 终点算 (mw, mh)；
        # 这里按旧代码的经验值: SE 角额外 +2 offset，其他角按 vdl - delta
        offset_unit = vdl - min_delta
        if min_idx == 3:  # SE
            real_delta = abs(pre_pos[0] - mw) + abs(pre_pos[1] - mh)
            offset_unit = vdl - real_delta + 2

        ox, oy = offsets[min_idx]
        return (
            cx + ox * bw * offset_unit,
            cy + oy * bh * offset_unit,
        )

    def _tile_to_click_pos(
        self,
        from_pos: tuple[int, int],
        to_pos: tuple[int, int],
        ctx: MapContext,
    ) -> tuple[float, float]:
        """
        把"从 from_pos 走到 to_pos"翻译成屏幕上要点击的归一化坐标。
        2:1 斜视角投影公式。
        """
        char_x, char_y = self._character_screen_pos(from_pos, ctx)
        bw, bh = ctx.vision.block_size
        dx = to_pos[0] - from_pos[0]
        dy = to_pos[1] - from_pos[1]
        x = char_x + dx * bw / 2 - dy * bw / 2
        y = char_y + dx * bh / 2 + dy * bh / 2
        return (x, y)

    # ========================================================================
    # 路径规划：把长路径切成"一次点击可达"的子段
    # ========================================================================

    @staticmethod
    def split_path(
        path: list[tuple[int, int]],
        move_max_num: int,
    ) -> list[tuple[int, int]]:
        """
        输入 path 允许单段超出 move_max_num：要求这种长段必须沿同一维度。
        输出保证相邻两点的曼哈顿距离 ≤ move_max_num。飞行点 (-1,-1) 原样保留。
        """
        if not path:
            return []

        result: list[tuple[int, int]] = [path[0]]
        last_x, last_y = path[0]
        is_fly = False

        for tgt in path[1:]:
            tx, ty = tgt
            if tx == -1 and ty == -1:
                result.append(FLY)
                is_fly = True
                continue
            if is_fly:
                is_fly = False
                result.append((tx, ty))
                last_x, last_y = tx, ty
                continue

            # 正常段
            while abs(tx - last_x) + abs(ty - last_y) > move_max_num:
                if not (tx == last_x or ty == last_y):
                    raise ValueError(
                        f"路径段 {(last_x, last_y)} → {tgt} 超出 move_max_num={move_max_num}，"
                        f"但不是单维度移动，无法自动切段"
                    )
                if tx == last_x:
                    step = move_max_num if ty > last_y else -move_max_num
                    last_y += step
                else:
                    step = move_max_num if tx > last_x else -move_max_num
                    last_x += step
                result.append((last_x, last_y))

            if (last_x, last_y) != (tx, ty):
                result.append((tx, ty))
                last_x, last_y = tx, ty
        return result

    # ========================================================================
    # 执行
    # ========================================================================

    def execute_move_path(
        self,
        path: list[tuple[int, int]],
        ctx: MapContext,
        step_delay: float = 0.2,
        fly_delay: float = 0.8,
        per_segment: Optional[Callable[[int, int], None]] = None,
    ) -> None:
        """
        执行一段路径。第一个点作为起点（必须与当前位置一致或由调用者负责）。

        per_segment: 每个原子段执行前调用 per_segment(idx_1based, total)，
                     总段数按"飞行+着陆算一段"计算（A2 语义）。
                     None 时不触发；有 cancel 等需求由调用方在回调里抛异常中断。
        """
        if not path:
            return
        # 认可 path[0] 为当前位置
        if self._cur_pos is None:
            self._cur_pos = path[0]
        elif self._cur_pos != path[0]:
            log.warning(
                "execute_move_path: 当前位置 %s 与 path 起点 %s 不一致，覆盖为起点",
                self._cur_pos,
                path[0],
            )
            self._cur_pos = path[0]

        split = self.split_path(path, ctx.vision.move_max_num)

        # 把 split 归一成"原子段"列表：每段是 ("move", target) 或 ("fly", landing)
        # 飞行标记 FLY + 紧跟着陆点 → 合并为一个 ("fly", landing) 原子段
        atoms: list[tuple[str, tuple[int, int]]] = []
        i = 1
        while i < len(split):
            tgt = split[i]
            if tgt == FLY:
                # 下一个点是着陆点
                if i + 1 >= len(split):
                    raise ValueError("path 以飞行标记结尾但无着陆点")
                atoms.append(("fly", split[i + 1]))
                i += 2
            else:
                atoms.append(("move", tgt))
                i += 1

        total = len(atoms)
        log.info("path: %s → 切段 %d 原子步", path, total)

        for idx, (kind, target) in enumerate(atoms, start=1):
            if per_segment is not None:
                per_segment(idx, total)

            if kind == "fly":
                # 一个原子飞行段 = 点击角色唤起传送 + 等待落地（不点击）
                char_pos = self._character_screen_pos(self._cur_pos, ctx)
                self.mumu.click(char_pos, delay=fly_delay)
                self._cur_pos = target
                self._wait_screen_stable(ctx)
            else:
                click_pos = self._tile_to_click_pos(self._cur_pos, target, ctx)
                img_before = self.mumu.capture_window()
                self.mumu.click(click_pos, delay=step_delay)
                self._wait_pos_stable(ctx, img_before=img_before, max_wait=3.0)
                self._cur_pos = target
                self._wait_screen_stable(
                    ctx, threshold=0.1, max_wait=1.0, raw_diff=True
                )

    # ========================================================================
    # 等待原语
    # ========================================================================

    def _wait_pos_stable(
        self,
        ctx: MapContext,
        img_before: Optional[Image.Image] = None,
        threshold: float = 0.01,
        max_wait: float = 3.0,
        fps: float = 10.0,
    ) -> None:
        """监视小地图坐标数字是否"先变再稳" —— 先等它变，再等它稳。"""
        roi = ctx.minimap_coord_roi
        if roi is None:
            time.sleep(0.2)  # 没配 ROI 就傻等
            return

        def _crop(img: Image.Image) -> Image.Image:
            return self.mumu.crop_img(img, roi[:2], roi[2:])

        delay = 1.0 / fps
        deadline = time.perf_counter() + max_wait
        last = _crop(img_before) if img_before is not None else None

        # 阶段 1: 等到坐标变动
        while time.perf_counter() < deadline:
            time.sleep(delay)
            img = _crop(self.mumu.capture_window())
            if last is None:
                last = img
                continue
            d = self.mumu.diff_img(img, last)
            last = img
            if d is not None and d > threshold:
                break

        # 阶段 2: 等到不再变
        while time.perf_counter() < deadline:
            time.sleep(delay)
            img = _crop(self.mumu.capture_window())
            d = self.mumu.diff_img(img, last)
            last = img
            if d is not None and d < threshold:
                return

    def _wait_screen_stable(
        self,
        ctx: MapContext,
        threshold: float = 0.03,
        max_wait: float = 1.0,
        fps: float = 5.0,
        raw_diff: bool = False,
    ) -> None:
        """等到全屏画面稳定（diff < threshold）"""
        delay = 1.0 / fps
        deadline = time.perf_counter() + max_wait
        last: Optional[Image.Image] = None

        def _crop(img: Image.Image) -> Image.Image:
            w, h = img.size
            box = (
                int(w * _SCREEN_DIFF_CROP[0]),
                int(h * _SCREEN_DIFF_CROP[1]),
                int(w * _SCREEN_DIFF_CROP[2]),
                int(h * _SCREEN_DIFF_CROP[3]),
            )
            return img.crop(box)

        while time.perf_counter() < deadline:
            img = _crop(self.mumu.capture_window())
            if last is None:
                last = img
                time.sleep(delay)
                continue
            if raw_diff:
                d = _raw_diff(img, last)
            else:
                d = self.mumu.diff_img(img, last)
            last = img
            if d is not None and d < threshold:
                return
            time.sleep(delay)

    # 对外的 wait* 接口（routine 可直接调用）
    def wait_pos_stable(
        self,
        ctx: MapContext,
        threshold: float = 0.02,
        max_wait: float = 3.0,
        fps: float = 10.0,
    ) -> None:
        self._wait_pos_stable(ctx, threshold=threshold, max_wait=max_wait, fps=fps)

    def wait_screen_stable(
        self,
        ctx: MapContext,
        threshold: float = 0.05,
        max_wait: float = 5.0,
        fps: float = 10.0,
    ) -> None:
        self._wait_screen_stable(ctx, threshold=threshold, max_wait=max_wait, fps=fps)


def _raw_diff(img_a: Image.Image, img_b: Image.Image, step: int = 3) -> float:
    """逐像素求差率，抽样步长 step；相比 SSIM 对微小变化更敏感。"""
    if img_a.size != img_b.size:
        return 1.0
    w, h = img_a.size
    count = 0
    for i in range(0, w, step):
        for j in range(0, h, step):
            a = img_a.getpixel((i, j))
            b = img_b.getpixel((i, j))
            if abs(sum(a) - sum(b)) >= 5:
                count += 1
    total = ((w + step - 1) // step) * ((h + step - 1) // step)
    return count / total if total else 0.0
