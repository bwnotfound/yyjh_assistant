"""
Mover - 运动执行器

职责:
  1. 根据"起点格坐标 + 目标格坐标 + 地图尺寸 + 视野档位"计算点击位置
  2. 处理相机贴边时角色 sprite 位置的偏移
  3. 执行一段 path，含飞行标记
  4. 暴露 wait_pos_stable / wait_screen_stable
  5. 若注入 CoordReader，每段移动后用 OCR 闭环校验是否到达 target，
     未到达抛 MoveNotConverged

和 `CoordSystem` (大地图) 是同构但规模不同的两套"相机贴边修正"，这里是小地图
(地图场景) 版本。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Callable, Optional

import cv2
import numpy as np
from PIL import Image

from utils import Mumu
from app.core.ocr import CoordReader
from app.core.profiles import MovementProfile, VisionSpec

log = logging.getLogger(__name__)


# 飞行标记
FLY = (-1, -1)

# 等待屏幕变化时裁剪的 ROI（归一化），用于去掉边框干扰
_SCREEN_DIFF_CROP = (0.032, 0.080, 0.841, 0.902)


# =============================================================================
# 异常 & 状态
# =============================================================================


class MoveNotConverged(RuntimeError):
    """单段移动未能在限定条件下确认到达 target。"""


class WaitStatus(Enum):
    OK = "ok"  # 已到达（OCR 模式确认 == target；SSIM 模式仅"画面稳定"）
    NO_CHANGE = "no_change"  # 阶段1超时未检测到变化
    NOT_STABLE = "not_stable"  # 阶段2超时未稳定
    WRONG_DESTINATION = "wrong_destination"  # OCR 模式：稳定但坐标 != target


@dataclass
class WaitOutcome:
    status: WaitStatus
    final_coord: Optional[
        tuple[int, int]
    ]  # OCR 模式下最后一次稳定坐标；SSIM 模式恒为 None


# =============================================================================
# MapContext
# =============================================================================


@dataclass
class MapContext:
    """执行移动时需要的上下文"""

    # 地图格数 (width, height)；为 None 表示"路径不会触碰屏幕边缘"，
    # 此时跳过贴边修正（角色永远在屏幕中心 character_pos 处）。
    map_size: Optional[tuple[int, int]]
    vision: VisionSpec
    minimap_coord_roi: Optional[tuple[float, float, float, float]] = None


# =============================================================================
# Mover
# =============================================================================


class Mover:
    """
    有状态的移动执行器：维护"当前格坐标"作为 move 的起点。
    """

    def __init__(
        self,
        mumu: Mumu,
        profile: MovementProfile,
        coord_reader: Optional[CoordReader] = None,
    ) -> None:
        self.mumu = mumu
        self.profile = profile
        self.coord_reader = coord_reader
        self._cur_pos: Optional[tuple[int, int]] = None

    # ========================================================================
    # 状态
    # ========================================================================

    @property
    def current_pos(self) -> Optional[tuple[int, int]]:
        return self._cur_pos

    def set_current_pos(self, pos: Optional[tuple[int, int]]) -> None:
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

        每个 move 原子段结束后会调用 _wait_until_arrived；
        若注入了 coord_reader，未确认到达 target 时抛 MoveNotConverged。
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
                # 飞行段：点角色 → 等画面稳定（无法用 OCR 校验，飞行落地即坐标变）
                char_pos = self._character_screen_pos(self._cur_pos, ctx)
                self.mumu.click(char_pos, delay=fly_delay)
                self._cur_pos = target
                self._wait_screen_stable(ctx)
            else:
                src = self._cur_pos
                click_pos = self._tile_to_click_pos(src, target, ctx)
                # click 参数 log: 出错时直接看是 tile_size 算小了还是 click_pos 偏了
                bw, bh = ctx.vision.block_size
                char_pos = self._character_screen_pos(src, ctx)
                log.info(
                    "click: src=%s → target=%s, delta=(%+d,%+d), "
                    "vision=%s block_size=(%.4f, %.4f), "
                    "char_screen=(%.4f, %.4f), click_screen=(%.4f, %.4f)",
                    src,
                    target,
                    target[0] - src[0],
                    target[1] - src[1],
                    getattr(ctx.vision, "name", "?"),
                    bw,
                    bh,
                    char_pos[0],
                    char_pos[1],
                    click_pos[0],
                    click_pos[1],
                )
                img_before = self.mumu.capture_window()
                self.mumu.click(click_pos, delay=step_delay)

                outcome = self._wait_until_arrived(ctx, target, img_before=img_before)

                if outcome.status == WaitStatus.OK:
                    # OCR 模式: 用真实坐标更新 cur_pos；SSIM 模式 final_coord 为 None，回退到 target
                    self._cur_pos = outcome.final_coord or target
                else:
                    raise MoveNotConverged(
                        f"段 {src} → {target} 未确认到达："
                        f"status={outcome.status.value}, "
                        f"final_coord={outcome.final_coord}"
                    )
                # 注：旧版本曾在这里调 _wait_screen_stable 等"菜单/采集动画落定"。
                # 但游戏的真实语义是"OCR 坐标到达 = 走路动画结束"，无需额外等待。
                # 如果某些 routine 后续步骤（button/click）确实需要等画面静止，
                # 在 routine 里显式插 wait_screen_stable 步骤即可。

    # ========================================================================
    # 到达判定（OCR 优先，SSIM 兜底）
    # ========================================================================

    def _wait_until_arrived(
        self,
        ctx: MapContext,
        target: tuple[int, int],
        img_before: Optional[Image.Image] = None,
        *,
        phase1_max_wait: float = 3.0,
        phase2_max_wait: float = 5.0,
        fps: float = 10.0,
        stable_frames: int = 8,
    ) -> WaitOutcome:
        """
        判定从 self._cur_pos 走到 target 是否完成。

        OCR 模式（注入了 coord_reader）:
          阶段1: 等坐标变化（≠ self._cur_pos）
          阶段2: 等坐标稳定（连续 stable_frames 帧相同）
          稳定后比对 target；不等抛 WRONG_DESTINATION

        SSIM 模式（无 coord_reader）:
          原 _wait_pos_stable 思路，但两阶段独立 deadline + 阶段2要求连续稳定
        """
        if self.coord_reader is not None:
            return self._wait_via_ocr(
                target, phase1_max_wait, phase2_max_wait, fps, stable_frames
            )
        return self._wait_via_ssim(
            ctx, img_before, phase1_max_wait, phase2_max_wait, fps, stable_frames
        )

    def _wait_via_ocr(
        self,
        target: tuple[int, int],
        phase1_max_wait: float,
        phase2_max_wait: float,
        fps: float,
        stable_frames: int,  # 保留参数兼容旧签名，新逻辑下不再使用
    ) -> WaitOutcome:
        """
        到达判定语义（按游戏的真实行为）:
          OCR 读到 coord == target 即视为到达，立即返回 OK。
          中间格子的坐标只做 trace 展示，不参与判定（每格切换瞬间游戏内坐标
          已对齐到该格中心）。

        阶段划分仅用于错误归类:
          phase1 = 还没看到 coord != start_pos（用来判定 click 是否生效）
          phase2 = 已经看到坐标变化，但还没读到 target

        终止状态:
          OK                ─ 任意阶段读到 coord == target
          NO_CHANGE         ─ phase1 超时仍未观察到坐标变化
          WRONG_DESTINATION ─ phase2 超时仍未读到 target，final_coord = 最后一次 OCR 读到的坐标
        """
        assert self.coord_reader is not None
        delay = 1.0 / fps
        start_pos = self._cur_pos
        t_start = time.perf_counter()

        # 整段移动的 OCR trace：(phase, t_rel, raw_text, coord)
        trace: list[tuple[str, float, str, Optional[tuple[int, int]]]] = []
        # 关键帧 ROI（PIL.Image），异常退出时 dump 出来肉眼验证
        keyframes: dict[str, Image.Image] = {}

        # ---- 阶段 1: 等坐标变化（顺便检查 coord 是否已经 == target） ----
        deadline = time.perf_counter() + phase1_max_wait
        moved_started = False
        ocr_calls = 0
        ocr_success = 0
        debug_dumped = False  # 本段移动只在第一次 OCR 失败时 dump 模板诊断一次
        while time.perf_counter() < deadline:
            time.sleep(delay)
            ocr_calls += 1
            coord, text, roi_pil = self.coord_reader.read_verbose()
            trace.append(("phase1", time.perf_counter() - t_start, text, coord))
            if coord is None:
                if not debug_dumped:
                    self._dump_ocr_debug(start_pos, target)
                    debug_dumped = True
                continue
            ocr_success += 1
            if "phase1_first_read" not in keyframes:
                keyframes["phase1_first_read"] = roi_pil
            # 直接到达（少见但要兜住：start_pos 错或 click 极快）
            if coord == target:
                keyframes["arrived"] = roi_pil
                return WaitOutcome(WaitStatus.OK, coord)
            if coord != start_pos:
                moved_started = True
                keyframes["phase1_first_change"] = roi_pil
                break

        if not moved_started:
            log.warning(
                "OCR 阶段1超时未变化: start=%s, target=%s, ocr=%d/%d 成功",
                start_pos,
                target,
                ocr_success,
                ocr_calls,
            )
            self._log_ocr_trace(trace, "no_change", start_pos, target)
            self._dump_ocr_keyframes(keyframes, "no_change")
            return WaitOutcome(WaitStatus.NO_CHANGE, None)

        # ---- 阶段 2: 持续等待 coord == target ----
        deadline = time.perf_counter() + phase2_max_wait
        last_coord: Optional[tuple[int, int]] = None
        last_roi: Optional[Image.Image] = keyframes.get("phase1_first_change")
        while time.perf_counter() < deadline:
            time.sleep(delay)
            coord, text, roi_pil = self.coord_reader.read_verbose()
            trace.append(("phase2", time.perf_counter() - t_start, text, coord))
            if coord is None:
                continue
            last_coord = coord
            last_roi = roi_pil
            if coord == target:
                keyframes["arrived"] = roi_pil
                return WaitOutcome(WaitStatus.OK, coord)

        # 阶段2 超时，没读到 target —— 这是真实的"角色没走到目标"
        log.warning(
            "OCR 阶段2超时未到达: target=%s, last_seen=%s",
            target,
            last_coord,
        )
        if last_roi is not None:
            keyframes["phase2_last"] = last_roi
        self._log_ocr_trace(trace, "wrong_destination", start_pos, target)
        self._dump_ocr_keyframes(keyframes, "wrong_destination")
        return WaitOutcome(WaitStatus.WRONG_DESTINATION, last_coord)

    def _wait_via_ssim(
        self,
        ctx: MapContext,
        img_before: Optional[Image.Image],
        phase1_max_wait: float,
        phase2_max_wait: float,
        fps: float,
        stable_frames: int,
        threshold: float = 0.01,
    ) -> WaitOutcome:
        roi = ctx.minimap_coord_roi
        if roi is None:
            # 没标定 ROI 也没 OCR：完全无法判断，傻等一下当作成功
            time.sleep(0.2)
            return WaitOutcome(WaitStatus.OK, None)

        def _crop(img: Image.Image) -> Image.Image:
            return self.mumu.crop_img(img, roi[:2], roi[2:])

        delay = 1.0 / fps
        last = _crop(img_before) if img_before is not None else None

        # ---- 阶段 1: 等画面变 ----
        deadline = time.perf_counter() + phase1_max_wait
        changed = False
        while time.perf_counter() < deadline:
            time.sleep(delay)
            img = _crop(self.mumu.capture_window())
            if last is None:
                last = img
                continue
            d = self.mumu.diff_img(img, last)
            last = img
            if d is not None and d > threshold:
                changed = True
                break

        if not changed:
            return WaitOutcome(WaitStatus.NO_CHANGE, None)

        # ---- 阶段 2: 连续 stable_frames 帧 diff < threshold ----
        deadline = time.perf_counter() + phase2_max_wait
        stable_count = 0
        while time.perf_counter() < deadline:
            time.sleep(delay)
            img = _crop(self.mumu.capture_window())
            d = self.mumu.diff_img(img, last)
            last = img
            if d is None:
                continue
            if d < threshold:
                stable_count += 1
                if stable_count >= stable_frames:
                    return WaitOutcome(WaitStatus.OK, None)
            else:
                stable_count = 0

        return WaitOutcome(WaitStatus.NOT_STABLE, None)

    # ========================================================================
    # OCR 调试 dump
    # ========================================================================

    def _dump_ocr_debug(
        self,
        start_pos: Optional[tuple[int, int]],
        target: tuple[int, int],
    ) -> None:
        """
        OCR 第一次 read 返回 None 时调用一次。
        把 ROI 截图、二值化结果保存到 debug/ocr/，并在日志里输出每个字符模板的最高响应分数。
        """
        if self.coord_reader is None:
            return
        try:
            info = self.coord_reader.diagnose()
        except Exception as e:
            log.warning("OCR diagnose 失败: %s: %s", type(e).__name__, e)
            return

        debug_dir = Path("debug/ocr")
        try:
            debug_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            log.warning("无法创建 debug 目录 %s: %s", debug_dir, e)
            return

        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        roi_path = debug_dir / f"{ts}_roi.png"
        bin_path = debug_dir / f"{ts}_roi_bin.png"
        try:
            roi_pil = info.get("roi_pil")
            if roi_pil is not None:
                roi_pil.save(roi_path)
            roi_bin = info.get("roi_bin")
            if roi_bin is not None:
                cv2.imwrite(str(bin_path), roi_bin)
        except Exception as e:
            log.warning("保存 OCR debug 图像失败: %s", e)

        # 输出诊断 log
        W, H = info.get("roi_size", (0, 0))
        thresh = info.get("score_threshold", 0.0)
        log.info(
            "OCR debug @ start=%s target=%s | ROI %dx%d, threshold=%.2f",
            start_pos,
            target,
            W,
            H,
            thresh,
        )
        log.info("  ROI 截图: %s", roi_path)
        log.info("  二值化:   %s", bin_path)

        # 模板响应排序（最高在前）
        results = info.get("glyph_results", [])
        results = [r for r in results if "max_score" in r]
        results.sort(key=lambda r: r["max_score"], reverse=True)
        top_n = min(13, len(results))
        log.info("  模板最高响应（top %d，按 score 降序）:", top_n)
        for r in results[:top_n]:
            log.info(
                "    %r: score=%.3f @ (%d,%d), tmpl_size=%sx%s",
                r["char"],
                r["max_score"],
                r["max_loc"][0],
                r["max_loc"][1],
                r["template_size"][0],
                r["template_size"][1],
            )
        # 跳过的模板（太大的）
        skipped = [r for r in info.get("glyph_results", []) if "skipped" in r]
        if skipped:
            log.warning(
                "  以下模板被跳过 (template_larger_than_roi): %s",
                [(r["char"], r["template_size"]) for r in skipped],
            )

        # recognize 的实际输出（看 NMS 后的字符串到底是什么）
        text = info.get("recognize_text")
        log.info("  recognize() 返回: %r", text)

        # 所有 >= threshold 的候选（NMS 之前），按 cx 排序，看假冒位置
        above = info.get("above_threshold_candidates", [])
        log.info(
            "  >= threshold 的候选共 %d 个 (NMS 前，按 cx 升序):",
            len(above),
        )
        for c in above:
            log.info(
                "    cx=%5.1f tl=(%2d,%2d) %r score=%.3f size=%sx%s",
                c["cx"],
                c["tl"][0],
                c["tl"][1],
                c["char"],
                c["score"],
                c["size"][0],
                c["size"][1],
            )

    def _log_ocr_trace(
        self,
        trace: list[tuple[str, float, str, Optional[tuple[int, int]]]],
        status: str,
        start_pos: Optional[tuple[int, int]],
        target: tuple[int, int],
    ) -> None:
        """把 _wait_via_ocr 的整段识别 trace 输出到日志。"""
        if not trace:
            return
        log.info(
            "=== OCR trace dump (status=%s, start=%s, target=%s, %d 帧) ===",
            status,
            start_pos,
            target,
            len(trace),
        )
        for phase, t, text, coord in trace:
            log.info(
                "  [%s] +%6.3fs text=%-22r coord=%s",
                phase,
                t,
                text,
                coord,
            )

    def _dump_ocr_keyframes(
        self,
        keyframes: dict,
        tag: str,
    ) -> None:
        """把 trace 期间记下的关键帧 ROI 保存到 debug/ocr/，并对每帧跑一次诊断。"""
        if not keyframes:
            return
        debug_dir = Path("debug/ocr")
        try:
            debug_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            log.warning("无法创建 debug 目录 %s: %s", debug_dir, e)
            return
        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        for name, roi in keyframes.items():
            path = debug_dir / f"{ts}_{tag}_{name}.png"
            try:
                roi.save(path)
            except Exception as e:
                log.warning("保存关键帧失败 %s: %s", path, e)
                continue
            log.info("  关键帧 [%s]: %s", name, path)
            self._diagnose_keyframe_roi(name, roi)

    def _diagnose_keyframe_roi(self, name: str, roi: Image.Image) -> None:
        """对单帧 ROI 跑 OCR 诊断，输出 above_threshold 候选 + recognize 结果。"""
        if self.coord_reader is None:
            return
        try:
            roi_rgb = np.array(roi.convert("RGB"))
            ocr = self.coord_reader.ocr
            text = ocr.recognize(roi_rgb)
            info = ocr.diagnose(roi_rgb)
            above = info.get("above_threshold_candidates", [])
            log.info(
                "    [%s] recognize=%r, >= threshold (%.2f) 候选 %d 个 (按 cx 升序):",
                name,
                text,
                info.get("score_threshold", 0.0),
                len(above),
            )
            for c in above:
                log.info(
                    "      cx=%5.1f tl=(%2d,%2d) %r score=%.3f tmpl=%sx%s",
                    c["cx"],
                    c["tl"][0],
                    c["tl"][1],
                    c["char"],
                    c["score"],
                    c["size"][0],
                    c["size"][1],
                )
            # 同时输出每个字符模板的最高响应（无视 threshold）—— 看到接近 0.85 的"
            # 边缘候选"能直接判断是否要降阈值
            glyph_results = info.get("glyph_results", [])
            glyph_results = sorted(
                [g for g in glyph_results if "max_score" in g],
                key=lambda g: g["max_score"],
                reverse=True,
            )
            log.info(
                "    [%s] 各模板最高响应 (无视 threshold，按 score 降序):",
                name,
            )
            for g in glyph_results:
                log.info(
                    "      %r: max=%.3f @ (%d,%d), tmpl=%sx%s",
                    g["char"],
                    g["max_score"],
                    g["max_loc"][0],
                    g["max_loc"][1],
                    g["template_size"][0],
                    g["template_size"][1],
                )
        except Exception as e:
            log.warning("    [%s] 诊断失败: %s: %s", name, type(e).__name__, e)

    # ========================================================================
    # 等待原语（旧公共接口；wait_pos_stable / wait_screen_stable 行为不变）
    # ========================================================================

    def _wait_pos_stable(
        self,
        ctx: MapContext,
        img_before: Optional[Image.Image] = None,
        threshold: float = 0.01,
        max_wait: float = 3.0,
        fps: float = 10.0,
    ) -> None:
        """监视小地图坐标 ROI 的"先变再稳"。SSIM 实现，公共接口用。"""
        roi = ctx.minimap_coord_roi
        if roi is None:
            time.sleep(0.2)
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

    # 对外的 wait* 接口（routine 可直接调用；行为同旧版）
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
