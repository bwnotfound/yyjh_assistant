"""
精炼采集主循环.

设计:
    - 单一入口 ``RefineCaptureRunner.run(target_count, expected_eq_name)``,
      在 QThread 里调用; 通过 ``threading.Event`` 中断.
    - 决策接口: ``policy: Callable[[ConfirmPanelState], bool]``,
      默认 ``always_cancel`` (不接受任何精炼结果, 用于纯采集).
      未来加自动接受时只需注入新策略, 主循环不变.
    - hooks 用回调+Signal 形式传递日志/状态/进度, GUI 在外面订阅, 跨线程安全
      由 QSignal 保证.
    - refine_no 自维护 (从 recorder.next_refine_no 启动), 跟游戏内"含本次"
      次数交叉校验, 不一致打 warning 但仍以自维护为准.

执行流程 (一轮):
    1. 等到结束界面 (status), 校验装备名 + 材料 + 银两
    2. 点 [精炼]
    3. 等到准备界面 (confirm), 校验装备名一致
    4. 调 policy 决定接受/取消
    5. 写 yaml
    6. 点 [接受]/[取消]
    7. 回到 1
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional, Protocol

from PIL import Image

from .data import ConfirmPanelState, RefineRecord, StatusPanelState
from .profile import RefineProfile
from .readers import ConfirmPanelReader, StatusPanelReader
from .recorder import RefineRecorder

log = logging.getLogger(__name__)


# =============================================================================
# 决策策略接口
# =============================================================================


class RefinePolicy(Protocol):
    """决策接口: 拿到准备界面状态, 返回是否接受."""

    def __call__(self, state: ConfirmPanelState) -> bool: ...


def always_cancel(_: ConfirmPanelState) -> bool:
    """默认策略: 永远取消. 用于纯采集场景."""
    return False


# =============================================================================
# 钩子
# =============================================================================


@dataclass
class RunnerHooks:
    on_log: Optional[Callable[[str, str], None]] = None  # (level, msg)
    on_status_state: Optional[Callable[[StatusPanelState], None]] = None
    on_confirm_state: Optional[Callable[[ConfirmPanelState], None]] = None
    on_record: Optional[Callable[[RefineRecord], None]] = None
    on_progress: Optional[Callable[[int, int], None]] = None  # (done, target)

    def log(self, level: str, msg: str) -> None:
        if self.on_log:
            try:
                self.on_log(level, msg)
                return
            except Exception:
                log.exception("on_log hook raised, fall back to logger")
        log.log(getattr(logging, level.upper(), logging.INFO), msg)


# =============================================================================
# 异常
# =============================================================================


class RefineCancelled(Exception):
    """主动中断信号."""


# =============================================================================
# Runner
# =============================================================================


class RefineCaptureRunner:
    def __init__(
        self,
        mumu,
        profile: RefineProfile,
        recorder: RefineRecorder,
        status_reader: StatusPanelReader,
        confirm_reader: ConfirmPanelReader,
        policy: RefinePolicy = always_cancel,
        hooks: Optional[RunnerHooks] = None,
        cancel_event: Optional[threading.Event] = None,
        verbose_state_log: bool = False,
    ) -> None:
        """
        verbose_state_log: True 时每帧识别都打印详细字段; False 仅在异常或采集事件时打印.
        """
        self.mumu = mumu
        self.profile = profile
        self.recorder = recorder
        self.status_reader = status_reader
        self.confirm_reader = confirm_reader
        self.policy = policy
        self.hooks = hooks or RunnerHooks()
        self.cancel_event = cancel_event or threading.Event()
        self.verbose_state_log = verbose_state_log
        # 自维护计数器: 已写入的最大 refine_no
        self._counter = recorder.next_refine_no() - 1

    # ---------- 中断 ----------

    def _check_cancel(self) -> None:
        if self.cancel_event.is_set():
            raise RefineCancelled()

    # ---------- 等待面板 ----------

    def _wait_for_status(
        self, expected_eq_name: Optional[str] = None
    ) -> StatusPanelState:
        deadline = time.time() + self.profile.panel_wait_timeout
        last_err: Optional[str] = None
        attempts = 0
        while time.time() < deadline:
            self._check_cancel()
            attempts += 1
            try:
                img = self.mumu.capture_window()
                state = self.status_reader.read(img)
                if state is not None:
                    if expected_eq_name and state.equipment_name != expected_eq_name:
                        last_err = (
                            f"装备名不匹配: 期望={expected_eq_name} "
                            f"实际={state.equipment_name}"
                        )
                    else:
                        if attempts > 1:
                            self.hooks.log(
                                "info",
                                f"结束界面识别成功 (第 {attempts} 次尝试)",
                            )
                        return state
            except Exception as e:
                last_err = f"{type(e).__name__}: {e}"
                log.exception("status_reader.read 异常")
            # 每隔几次给进度提示, 避免用户以为卡死
            if attempts == 2 or attempts % 5 == 0:
                remain = max(0.0, deadline - time.time())
                self.hooks.log(
                    "info",
                    f"⌛ 等待结束界面, 已尝试 {attempts} 次, 剩 {remain:.1f}s..."
                    + (f" 最后错误: {last_err}" if last_err else ""),
                )
            time.sleep(self.profile.poll_interval)
        raise TimeoutError(
            f"等待结束界面超时, 共尝试 {attempts} 次. 最后一次错误: {last_err}"
        )

    def _wait_for_confirm(self) -> ConfirmPanelState:
        deadline = time.time() + self.profile.panel_wait_timeout
        last_err: Optional[str] = None
        attempts = 0
        while time.time() < deadline:
            self._check_cancel()
            attempts += 1
            try:
                img = self.mumu.capture_window()
                state = self.confirm_reader.read(img)
                if state is not None:
                    if attempts > 1:
                        self.hooks.log(
                            "info",
                            f"准备界面识别成功 (第 {attempts} 次尝试)",
                        )
                    return state
            except Exception as e:
                last_err = f"{type(e).__name__}: {e}"
                log.exception("confirm_reader.read 异常")
            # 每隔几次给进度提示, 避免用户以为卡死.
            # 单次 read 在裁切兜底激活时可能要 ~3s (5 变体 × 3 slot ≈ 15 次 OCR),
            # 所以重试次数会比 _wait_for_status 少, 但每次都包含"已尽全力"的兜底.
            if attempts == 2 or attempts % 3 == 0:
                remain = max(0.0, deadline - time.time())
                self.hooks.log(
                    "info",
                    f"⌛ 等待准备界面, 已尝试 {attempts} 次, 剩 {remain:.1f}s..."
                    + (f" 最后错误: {last_err}" if last_err else ""),
                )
            time.sleep(self.profile.poll_interval)
        raise TimeoutError(
            f"等待准备界面超时, 共尝试 {attempts} 次. 最后一次错误: {last_err}"
        )

    # ---------- 单帧诊断 (GUI '刷新当前状态' 用) ----------

    def diagnose_current(self, image: Optional[Image.Image] = None):
        """识别当前界面 (不点击任何按钮). 返回 (kind, state) 或 (None, None)."""
        if image is None:
            image = self.mumu.capture_window()
        s = self.status_reader.read(image)
        if s is not None:
            return "status", s
        c = self.confirm_reader.read(image)
        if c is not None:
            return "confirm", c
        return None, None

    # ---------- 主循环 ----------

    def run(self, target_count: int, expected_eq_name: Optional[str] = None) -> int:
        """采集 target_count 次, 返回实际采集到的次数.

        启动条件: 当前必须在结束界面 (用户已经选好装备并打开精炼).
        """
        done = 0
        try:
            for _ in range(target_count):
                self._check_cancel()

                # 1. 等结束界面
                self.hooks.log(
                    "info",
                    f"等待结束界面 (#{done + 1}/{target_count})",
                )
                status = self._wait_for_status(expected_eq_name)
                if self.hooks.on_status_state:
                    self.hooks.on_status_state(status)
                self._log_status(status)

                if not status.can_refine:
                    self.hooks.log(
                        "warning",
                        f"材料或银两不足, 停止采集 (剩余可精炼次数={status.remaining_uses()})",
                    )
                    break

                # 2. 点 [精炼]
                self.hooks.log("info", "点击 [精炼]")
                self.mumu.click(self.profile.button["refine"])
                time.sleep(self.profile.delay_after_refine_click)

                # 3. 等准备界面
                confirm = self._wait_for_confirm()
                if self.hooks.on_confirm_state:
                    self.hooks.on_confirm_state(confirm)
                self._log_confirm(confirm)

                # 4. 装备名一致性校验
                if status.equipment_name != confirm.equipment_name:
                    self.hooks.log(
                        "warning",
                        f"两界面装备名不一致 ({status.equipment_name} vs "
                        f"{confirm.equipment_name}), 取消并跳过本次",
                    )
                    self.mumu.click(self.profile.button["cancel"])
                    time.sleep(self.profile.delay_after_decision_click)
                    continue

                # 5. 决策
                accept = bool(self.policy(confirm))
                decision = "accepted" if accept else "cancelled"

                # 6. 写 yaml — refine_no 决策:
                # 优先用 OCR 出来的"已精炼:N次"(置信度高时), 自维护计数器作 fallback.
                # 这样即使中间某次精炼解析失败 (refine_no 没写入 yaml), 下一次成功
                # 时 OCR 读到的真实次数会自然反映, 避免自维护计数器跟游戏内错位.
                # 防御: refine_no 只能前进不能倒退 — 如果 OCR 给的值 ≤ 已有自维护值,
                # 大概率是 OCR 抖动或读到了 stale 帧, 仍以自维护为准.
                CONFIDENCE_THRESHOLD = 0.6
                use_ocr = (
                    confirm.refine_count_confidence >= CONFIDENCE_THRESHOLD
                    and confirm.refine_count_inclusive > self._counter
                )
                if use_ocr:
                    if confirm.refine_count_inclusive > self._counter + 1:
                        self.hooks.log(
                            "info",
                            f"refine_no 跳号: 自维护={self._counter} → "
                            f"OCR={confirm.refine_count_inclusive} "
                            f"(中间漏了 {confirm.refine_count_inclusive - self._counter - 1} 条, "
                            f"以 OCR 值为准, 置信度={confirm.refine_count_confidence:.2f})",
                        )
                    self._counter = confirm.refine_count_inclusive
                else:
                    self._counter += 1
                    if (
                        confirm.refine_count_inclusive > 0
                        and confirm.refine_count_inclusive != self._counter
                    ):
                        self.hooks.log(
                            "warning",
                            f"refine_no 不一致: 自维护={self._counter} "
                            f"OCR={confirm.refine_count_inclusive} "
                            f"(置信度={confirm.refine_count_confidence:.2f} "
                            f"< {CONFIDENCE_THRESHOLD}, 以自维护为准)",
                        )
                rec = self.recorder.append_from_confirm(
                    confirm, refine_no=self._counter, decision=decision
                )
                if self.hooks.on_record:
                    self.hooks.on_record(rec)
                done += 1
                if self.hooks.on_progress:
                    self.hooks.on_progress(done, target_count)
                self._log_record(rec)

                # 7. 点决策按钮
                btn_pos = (
                    self.profile.button["accept"]
                    if accept
                    else self.profile.button["cancel"]
                )
                self.mumu.click(btn_pos)
                time.sleep(self.profile.delay_after_decision_click)
        except RefineCancelled:
            self.hooks.log("info", "采集被用户中断")
        return done

    # ---------- 日志 ----------

    def _log_status(self, s: StatusPanelState) -> None:
        if not self.verbose_state_log:
            return
        msg = (
            f"[结束界面] 装备={s.equipment_name} 已精炼={s.refine_count}次 "
            f"基础={dict(s.base_attrs)} 附加={[a.display for a in s.extra_attrs]} "
            f"材料={[m.display for m in s.materials]} "
            f"花费={s.cost.display} 持有={s.balance.display} "
            f"剩余可精炼≈{s.remaining_uses()} 次"
        )
        self.hooks.log("info", msg)

    def _log_confirm(self, s: ConfirmPanelState) -> None:
        if not self.verbose_state_log:
            return
        before = [a.display for a in s.extra_attrs_before]
        new = s.new_attr.display if s.new_attr else "(空)"
        msg = (
            f"[准备界面] 装备={s.equipment_name} 含本次精炼={s.refine_count_inclusive}次 "
            f"基础={dict(s.base_attrs)} 旧词条={before} 新词条={new} "
            f"替换索引={s.replace_index}"
        )
        self.hooks.log("info", msg)

    def _log_record(self, r: RefineRecord) -> None:
        new = r.new_attr
        unit = new.get("unit", "")
        msg = (
            f"#{r.refine_no} 新词条={new.get('name')} "
            f"{new.get('value')}{unit} → 替换 attrs_before[{r.replace_index}], "
            f"决策={r.decision}"
        )
        self.hooks.log("info", msg)
