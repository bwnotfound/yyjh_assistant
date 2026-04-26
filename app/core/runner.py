"""
RoutineRunner - 把 Routine 的每个 Step 分派到具体执行。

支持:
  - 中断 (cancel_event)
  - 暂停 / 单步  (step_event)
  - 日志回调 (on_log)
  - 进度回调 (on_progress: step_idx, total, loop_idx, loop_total)
  - include step: 串联执行另一份 routine 文件，带防环检测

执行方式: 同步运行于调用线程。GUI 的 runner_dialog 用 QThread 包装调度。
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from utils import Mumu

from app.core.mover import MapContext, Mover
from app.core.ocr import CoordReader, TemplateOCR
from app.core.profiles import MovementProfile
from app.core.routine import (
    AnyStep,
    ButtonStep,
    BuyStep,
    ClickStep,
    DEFAULT_ROUTINES_DIR,
    EnterMapStep,
    IncludeStep,
    MoveStep,
    Routine,
    SleepStep,
    TravelStep,
    WaitPosStableStep,
    WaitScreenStableStep,
)
from config.common.map_registry import (
    CoordSystem,
    LocationRecord,
    MapRegistry,
)

log = logging.getLogger(__name__)


# 字符模板目录（与各 view 的 OCR 链路保持一致：click_preview / map_size_solver /
# view_area_solver / routine_editor 等都用同一个目录）
MINIMAP_TEMPLATE_DIR = Path("config/templates/minimap_coord")


class RoutineCancelled(Exception):
    """调用方设置 cancel_event 后从执行器抛出"""


@dataclass
class RunnerHooks:
    """回调集合。None 字段会被忽略。"""

    on_log: Optional[Callable[[str, str], None]] = None  # (level, msg)
    on_progress: Optional[Callable[[int, int, int, int], None]] = None
    # ↑ (step_idx_1based, total_steps, loop_idx_1based, total_loops_or_0)

    # 子步进度：(sub_idx_1based, sub_total)；sub_total == 0 表示清空子步显示
    on_substep: Optional[Callable[[int, int], None]] = None

    cancel_event: Optional[threading.Event] = None
    # 单步队列：put 一个 token 放行一步；None = 不等待，连续执行。
    # 用 Queue(maxsize=1) 而非 Event，避免"set/clear"的线程竞态 —— 已入队的 token
    # 绝不会被意外丢弃，且每次 put 最多保留 1 个未消费 token，防止连点累积。
    step_queue: Optional["queue.Queue[None]"] = None


def _log(hooks: RunnerHooks, level: str, msg: str) -> None:
    # 先写进 python logger
    getattr(
        log,
        (
            level.lower()
            if level.lower() in ("info", "warning", "error", "debug")
            else "info"
        ),
    )(msg)
    if hooks.on_log:
        try:
            hooks.on_log(level, msg)
        except Exception:
            log.exception("on_log 回调异常")


def _progress(hooks: RunnerHooks, si: int, st: int, li: int, lt: int) -> None:
    if hooks.on_progress:
        try:
            hooks.on_progress(si, st, li, lt)
        except Exception:
            log.exception("on_progress 回调异常")


def _substep(hooks: RunnerHooks, sub: int, sub_total: int) -> None:
    if hooks.on_substep:
        try:
            hooks.on_substep(sub, sub_total)
        except Exception:
            log.exception("on_substep 回调异常")


def _check_cancel(hooks: RunnerHooks) -> None:
    if hooks.cancel_event is not None and hooks.cancel_event.is_set():
        raise RoutineCancelled()


def _maybe_wait_step(hooks: RunnerHooks) -> None:
    """
    单步模式：阻塞直到队列里拿到 1 个 token；支持 cancel。

    Queue(maxsize=1) 保证语义清晰：
      - 主线程 put 多次只保留 1 个 pending token（后续 put 吃 queue.Full 异常）
      - 每次 wait 必然消费 1 个 token，不会受 set/clear 时序影响
    """
    q = hooks.step_queue
    if q is None:
        return
    while True:
        try:
            q.get(timeout=0.1)
            return
        except queue.Empty:
            _check_cancel(hooks)


# =============================================================================
# RoutineRunner
# =============================================================================


class RoutineRunner:
    def __init__(
        self,
        mumu: Mumu,
        routine: Routine,
        map_registry: MapRegistry,
        movement_profile: MovementProfile,
        hooks: Optional[RunnerHooks] = None,
    ) -> None:
        self.mumu = mumu
        self.routine = routine
        self.map_registry = map_registry
        self.movement_profile = movement_profile
        self.hooks = hooks or RunnerHooks()

        self._map_profile = map_registry.profiles.get(movement_profile.key)
        if self._map_profile is None:
            raise ValueError(
                f"map_registry 里没有分辨率 {movement_profile.key} 的 profile"
            )
        self._coord = CoordSystem(self._map_profile, map_registry.constraints)
        coord_reader = self._build_coord_reader()
        self._mover = Mover(mumu, movement_profile, coord_reader=coord_reader)
        self._current_map: Optional[str] = routine.starting_map

        # include 调用栈：用解析出来的绝对路径做 key 防环
        self._include_stack: list[Path] = []
        if routine.path is not None:
            self._include_stack.append(routine.path.resolve())

    # ========================================================================
    # OCR 链路构造
    # ========================================================================

    def _build_coord_reader(self) -> Optional[CoordReader]:
        """
        默认尝试启用 OCR 模式 (Mover._wait_via_ocr): 用模板匹配从小地图 ROI 读
        坐标值, 走完一格立即检测到 coord==target 即返回, 比 SSIM 模式快得多。

        以下任一缺失则 fallback 到 SSIM 模式 (画面变化判定), 同时给出明确告警:
          - movement_profile 未配置 minimap_coord_roi
          - 模板目录不存在 / 无可用模板
          - 模板加载抛任何异常
        """
        roi = self.movement_profile.minimap_coord_roi
        if roi is None:
            _log(
                self.hooks,
                "warning",
                "OCR 模式不可用: movement_profile 未配置 minimap_coord_roi, "
                "fallback 到 SSIM 模式 (慢, 走路靠画面变化判定)。"
                "请用主界面「ROI 截取工具」录入小地图坐标 ROI 以启用 OCR。",
            )
            return None
        try:
            template_ocr = TemplateOCR.from_dir(MINIMAP_TEMPLATE_DIR)
        except FileNotFoundError as e:
            _log(
                self.hooks,
                "warning",
                f"OCR 模式不可用: 模板目录 {MINIMAP_TEMPLATE_DIR} 缺失或无可用模板 "
                f"({e}), fallback 到 SSIM 模式。请录入字符模板 (0~9 + ( ) ,)。",
            )
            return None
        except Exception as e:
            _log(
                self.hooks,
                "warning",
                f"OCR 模板加载失败: {type(e).__name__}: {e}, " f"fallback 到 SSIM 模式",
            )
            return None
        _log(
            self.hooks,
            "info",
            f"OCR 坐标读取链路就绪: roi={roi}, template_dir={MINIMAP_TEMPLATE_DIR}",
        )
        return CoordReader(self.mumu, template_ocr, roi)

    # ========================================================================
    # 主循环
    # ========================================================================

    def run(self) -> None:
        total_steps = len(self.routine.steps)
        loop_total = self.routine.loop_count  # 0 = 无限
        loop_idx = 0

        try:
            while True:
                loop_idx += 1
                _log(
                    self.hooks,
                    "info",
                    f"=== 第 {loop_idx}/{loop_total or '∞'} 轮开始 ===",
                )

                for si, step in enumerate(self.routine.steps, start=1):
                    _check_cancel(self.hooks)
                    _progress(self.hooks, si, total_steps, loop_idx, loop_total)
                    _maybe_wait_step(self.hooks)
                    self._execute_one(step, si, total_steps)

                _log(self.hooks, "info", f"=== 第 {loop_idx} 轮结束 ===")

                if loop_total != 0 and loop_idx >= loop_total:
                    break

                interval = self.routine.loop_interval
                if interval > 0:
                    _log(self.hooks, "info", f"等待 {interval}s 后进入下一轮")
                    self._cancellable_sleep(interval)
        except RoutineCancelled:
            _log(self.hooks, "warning", "routine 被中断")
            raise

    def _cancellable_sleep(self, seconds: float) -> None:
        end = time.perf_counter() + seconds
        while time.perf_counter() < end:
            _check_cancel(self.hooks)
            time.sleep(min(0.1, end - time.perf_counter()))

    # ========================================================================
    # 分派
    # ========================================================================

    def _execute_one(
        self,
        step: AnyStep,
        si: int,
        st: int,
        depth: int = 0,
    ) -> None:
        """
        depth: include 嵌套深度，仅用于日志缩进。
               0 = 最外层 routine，1+ = 通过 include 进入的子 routine。
        """
        indent = "  " * depth
        at = f"@{step.at_map}" if step.at_map else ""
        _log(self.hooks, "info", f"{indent}[{si}/{st}] {step.TYPE}{at}")

        if step.at_map is not None:
            self._current_map = step.at_map

        dispatch = {
            "travel": self._do_travel,
            "move": self._do_move,
            "button": self._do_button,
            "click": self._do_click,
            "buy": self._do_buy,
            "sleep": self._do_sleep,
            "wait_pos_stable": self._do_wait_pos_stable,
            "wait_screen_stable": self._do_wait_screen_stable,
            "enter_map": self._do_enter_map,
            "include": self._do_include,
        }
        handler = dispatch.get(step.TYPE)
        if handler is None:
            raise ValueError(f"未知步骤类型: {step.TYPE}")
        handler(step)

    # ========================================================================
    # 具体 handler
    # ========================================================================

    def _do_travel(self, step: TravelStep) -> None:
        tgt = step.to
        if tgt not in self._map_profile.locations:
            raise ValueError(f"travel 目标「{tgt}」不在 map_registry 里")

        ui = self.movement_profile.ui
        if ui.package_btn is None or ui.ticket_btn is None:
            raise ValueError("ui.package_btn / ticket_btn 未配置，无法传送")

        tgt_rec = self._map_profile.locations[tgt]
        if not tgt_rec.is_recorded:
            raise ValueError(f"「{tgt}」未在 map_registry 中录入 icon/btn")

        # 第一段: 打开背包 → 车票 → 大地图
        self.mumu.click(ui.package_btn, delay=1.0)
        self.mumu.click(ui.ticket_btn, delay=1.0)

        # 第二段: 点目标图标 + 跳转按钮
        # 这里没有"起点地图"的概念 —— ticket 界面无论当前在哪，都能看到全大地图
        # 但由于相机会根据当前所在地点定位，所以我们需要拿 current_map 做起点
        src_rec = self._resolve_current_map_record()
        pair = self._coord.target_in_view(src_rec, tgt_rec)
        if pair is None:
            raise RuntimeError(
                f"以当前地图 ({self._current_map}) 为起点时，"
                f"「{tgt}」不在大地图可点击区域内 —— 数据可能有误"
            )
        icon_norm, btn_norm = pair
        self.mumu.click(icon_norm, delay=0.6)
        self.mumu.click(btn_norm, delay=0.6)

        # 切图过场
        time.sleep(3.0)
        self._current_map = tgt
        self._mover.set_current_pos(None)  # 到了新地图，位置未知

    def _resolve_current_map_record(self) -> LocationRecord:
        """
        传送需要知道"当前所在地图"。策略:
          1. 如果 step.at_map 标了，用它
          2. 否则用上一次 travel 到达的地图
          3. 都没有则抛错
        """
        if self._current_map and self._current_map in self._map_profile.locations:
            return self._map_profile.locations[self._current_map]
        raise RuntimeError(
            "无法确定当前地图 —— 请在步骤里加 at_map 或先 travel 到一张地图"
        )

    def _do_move(self, step: MoveStep) -> None:
        ctx = self._make_map_ctx()
        hooks = self.hooks

        def per_segment(sub: int, sub_total: int) -> None:
            # 每个原子段执行前：① 检查 cancel ② 更新子进度 ③ 如单步模式等待
            # 第 1 段复用 Step 级的 wait（外层 _execute_one 前已 wait），不重复
            _check_cancel(hooks)
            _substep(hooks, sub, sub_total)
            if sub > 1:
                _maybe_wait_step(hooks)

        try:
            self._mover.execute_move_path(step.path, ctx, per_segment=per_segment)
        finally:
            # 无论成功/异常都清空子步显示
            _substep(hooks, 0, 0)

    def _make_map_ctx(self) -> MapContext:
        if self._current_map is None:
            raise RuntimeError("move 步骤前必须先 travel 或设置 at_map")
        rec = self._map_profile.locations.get(self._current_map)
        if rec is None:
            raise RuntimeError(f"map_registry 没有「{self._current_map}」")

        vision_size = rec.vision_size
        if vision_size is None:
            # 降级: 用 movement_profile 里第一个可用视野档
            if not self.movement_profile.vision_sizes:
                raise RuntimeError(
                    "movement_profile 没有配置任何视野档位，无法执行 move"
                )
            vision_size = next(iter(self.movement_profile.vision_sizes.keys()))
            _log(
                self.hooks,
                "warning",
                f"「{self._current_map}」未配置 vision_size，"
                f"降级使用「{vision_size}」档（请在 GUI 里补全以保证精度）",
            )

        if rec.map_size is None:
            _log(
                self.hooks,
                "warning",
                f"「{self._current_map}」未配置 map_size，按『不触边』处理（无贴边修正）",
            )

        return MapContext(
            map_size=rec.map_size,
            vision=self.movement_profile.vision(vision_size),
            minimap_coord_roi=self.movement_profile.minimap_coord_roi,
        )

    def _do_button(self, step: ButtonStep) -> None:
        ui = self.movement_profile.ui
        name = step.name.strip()
        prefix, _, idx_raw = name.partition("_")
        try:
            idx = int(idx_raw)
        except ValueError:
            raise ValueError(f"button 名称非法: {name!r}（期望 table_N / chat_N）")

        if prefix == "table":
            pos = ui.table_btn(idx)
        elif prefix == "chat":
            pos = ui.chat_btn(idx)
        else:
            raise ValueError(f"button 前缀未知: {prefix!r}")

        self.mumu.click(pos)
        for _ in range(step.skip):
            if ui.blank_btn is None:
                raise ValueError("需要 skip 对话但 ui.blank_btn 未配置")
            self.mumu.click(ui.blank_btn, delay=0.4)
        if step.delay > 0:
            self._cancellable_sleep(step.delay)

    def _do_click(self, step: ClickStep) -> None:
        ui = self.movement_profile.ui
        pos = self._resolve_click_target(step)
        self.mumu.click(pos)
        for _ in range(step.skip):
            if ui.blank_btn is None:
                raise ValueError("需要 skip 对话但 ui.blank_btn 未配置")
            self.mumu.click(ui.blank_btn, delay=0.4)
        if step.delay > 0:
            self._cancellable_sleep(step.delay)

    def _resolve_click_target(self, step: ClickStep) -> tuple[float, float]:
        """
        解析 ClickStep 实际要点击的坐标:
          - preset 为空 → 直接用 step.pos
          - preset 是 "character_pos" → 从 MovementProfile 顶层取
          - preset 在 UIPositions 内置单点字段中 → 从对应字段取
          - preset 在 UIPositions.custom 中 → 从用户自建预设取
          - 都查不到 → 报错指引用户去运动配置
        """
        if not step.preset:
            return step.pos

        name = step.preset
        if name == "character_pos":
            return self.movement_profile.character_pos

        ui = self.movement_profile.ui
        # 用统一入口 resolve_single_point: 内置字段 → custom 字典
        pos = ui.resolve_single_point(name)
        if pos is not None:
            return pos

        # 仍未命中: 可能是 chat_btn / table_btn 这种非单点字段被错填到 click step,
        # 或者预设名拼写错误 / 没在 movement_profile 录入
        val = getattr(ui, name, None)
        if val is not None and not (isinstance(val, tuple) and len(val) == 2):
            raise ValueError(
                f"click 预设 {name!r} 不是单点坐标 (got {val!r})；"
                f"等距按钮组 / 商品栅格请用 button / buy 步骤"
            )
        raise ValueError(
            f"click 预设 {name!r} 在当前 movement_profile 里未配置；"
            f"请去主界面「运动配置」录入, 或在 routine 编辑器里「新建预设」"
        )

    def _do_buy(self, step: BuyStep) -> None:
        ui = self.movement_profile.ui
        for required in (
            "buy_item_start_pos",
            "buy_item_span",
            "buy_increase_btn",
            "buy_confirm_btn",
            "buy_exit_btn",
        ):
            if getattr(ui, required) is None:
                raise ValueError(f"ui.{required} 未配置")

        for item_idx, qty in step.items:
            self.mumu.click(ui.buy_item_pos(item_idx), delay=0.25)
            for _ in range(max(0, qty - 1)):
                self.mumu.click(ui.buy_increase_btn, delay=0.2)
            self.mumu.click(ui.buy_confirm_btn, delay=0.8)
        self.mumu.click(ui.buy_exit_btn, delay=1.0)

    def _do_sleep(self, step: SleepStep) -> None:
        # preset 模式: 运行时从 movement_profile.click_delays 动态解析,
        # 这样多个 routine 共享同一份延时配置。
        if step.preset:
            seconds = self.movement_profile.click_delays.resolve(step.preset)
            _log(
                self.hooks,
                "info",
                f"sleep [{step.preset}] → {seconds:.2f}s",
            )
        else:
            seconds = step.seconds
        self._cancellable_sleep(seconds)

    def _do_wait_pos_stable(self, step: WaitPosStableStep) -> None:
        ctx = self._make_map_ctx()
        self._mover.wait_pos_stable(
            ctx,
            threshold=step.threshold,
            max_wait=step.max_wait,
            fps=step.fps,
        )

    def _do_wait_screen_stable(self, step: WaitScreenStableStep) -> None:
        ctx = (
            self._make_map_ctx()
            if self._current_map
            else MapContext(
                (0, 0),
                self.movement_profile.vision_sizes.get("小")
                or next(iter(self.movement_profile.vision_sizes.values())),
            )
        )
        self._mover.wait_screen_stable(
            ctx,
            threshold=step.threshold,
            max_wait=step.max_wait,
            fps=step.fps,
        )

    def _do_enter_map(self, step: EnterMapStep) -> None:
        """只更新上下文：之后的 move 会以这张地图为准"""
        self._current_map = step.map
        # 新地图 → 上次移动记录的位置失效
        self._mover.set_current_pos(None)

    def _do_include(self, step: IncludeStep) -> None:
        """串联执行另一个 routine 文件"""
        sub_path = self._resolve_routine_path(step.routine)
        if not sub_path.exists():
            raise ValueError(
                f"include 找不到 routine 文件: {step.routine!r} "
                f"(尝试解析为 {sub_path})"
            )

        # 防环
        if sub_path in self._include_stack:
            chain = " → ".join(p.name for p in self._include_stack)
            raise RuntimeError(f"include 出现递归: {chain} → {sub_path.name}")

        try:
            sub = Routine.load(sub_path)
        except Exception as e:
            raise RuntimeError(
                f"include 加载子 routine 失败 ({sub_path}): " f"{type(e).__name__}: {e}"
            ) from e

        depth = len(self._include_stack)  # 父 routine 已在栈里，所以这就是子的 depth
        indent = "  " * depth
        _log(
            self.hooks,
            "info",
            f"{indent}↳ 进入 sub-routine [{sub.name}] "
            f"({len(sub.steps)} 步, {sub_path.name})",
        )
        self._include_stack.append(sub_path)
        try:
            sub_total = len(sub.steps)
            for sub_si, sub_step in enumerate(sub.steps, start=1):
                _check_cancel(self.hooks)
                _maybe_wait_step(self.hooks)
                # 子 routine 不刷新顶层 progress 信号，只通过日志缩进展示
                self._execute_one(sub_step, sub_si, sub_total, depth=depth)
        finally:
            self._include_stack.pop()
            _log(
                self.hooks,
                "info",
                f"{indent}↳ 退出 sub-routine [{sub.name}]",
            )

    def _resolve_routine_path(self, name: str) -> Path:
        """
        将 IncludeStep.routine 字段解析为实际文件路径。

        优先级:
          1. 已存在的绝对/相对路径 (按当前工作目录解析)
          2. 与父 routine 同目录: <parent_dir>/<name>(.yaml|.yml)
          3. config/routines/ 下: <DEFAULT>/<name>(.yaml|.yml)

        全部找不到时返回 candidate 列表里最常见的猜测，让上层报错时给出明确路径。
        """
        p = Path(name)
        if p.is_absolute() or p.exists():
            return p.resolve() if p.exists() else p

        parent_dir = (
            self.routine.path.parent.resolve()
            if self.routine.path is not None
            else None
        )
        default_dir = DEFAULT_ROUTINES_DIR.resolve()

        candidate_dirs: list[Path] = []
        if parent_dir is not None:
            candidate_dirs.append(parent_dir)
        if default_dir not in candidate_dirs:
            candidate_dirs.append(default_dir)

        # 已带扩展名就直接试，否则补 .yaml/.yml
        if p.suffix in (".yaml", ".yml"):
            stems = [p.name]
        else:
            stems = [f"{name}.yaml", f"{name}.yml", name]

        for d in candidate_dirs:
            for s in stems:
                c = d / s
                if c.exists():
                    return c.resolve()

        # 都没命中：返回第一个候选位置作为"期望路径"，由上层在异常里展示
        return candidate_dirs[0] / stems[0]
