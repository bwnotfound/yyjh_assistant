"""
Routine 执行器对话框：选择 routine yaml，启动/暂停/单步/停止。

线程模型:
    主线程: QDialog
    工作线程: QThread + _RunnerWorker
    通信:   signal/slot (跨线程自动 QueuedConnection)
    中断:   threading.Event (cancel_event / step_event)
"""

from __future__ import annotations

import logging
import queue
import threading
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, Qt, QThread, Signal
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from utils import Mumu

from app.core.profiles import (
    DEFAULT_MOVEMENT_YAML_PATH,
    MovementRegistry,
)
from app.core.routine import DEFAULT_ROUTINES_DIR, Routine, list_routines
from app.core.runner import RoutineCancelled, RoutineRunner, RunnerHooks
from config.common.map_registry import (
    DEFAULT_YAML_PATH as MAP_REGISTRY_PATH,
    MapRegistry,
)

log = logging.getLogger(__name__)


# =============================================================================
# Worker
# =============================================================================


class _RunnerSignals(QObject):
    log = Signal(str, str)  # level, msg
    progress = Signal(int, int, int, int)  # step_i, step_total, loop_i, loop_total
    substep = Signal(int, int)  # sub_i, sub_total; 0,0 = 清空
    finished = Signal(bool, str)  # ok, reason


class _RunnerWorker(QObject):
    """住在 QThread 里，接受主线程信号启动 / 停止。"""

    def __init__(
        self,
        mumu: Mumu,
        routine: Routine,
        map_registry: MapRegistry,
        movement_registry: MovementRegistry,
        step_mode: bool,
    ) -> None:
        super().__init__()
        self.mumu = mumu
        self.routine = routine
        self.map_registry = map_registry
        self.movement_registry = movement_registry
        self.step_mode = step_mode

        self.signals = _RunnerSignals()
        self.cancel_event = threading.Event()
        # 单步队列：maxsize=1 保证用户连点只保留 1 个 pending token
        self.step_queue: "queue.Queue[None]" = queue.Queue(maxsize=1)

    def step_once(self) -> None:
        # put_nowait 满了就丢弃（已有 1 个 pending token，再加没意义）
        try:
            self.step_queue.put_nowait(None)
        except queue.Full:
            pass

    def cancel(self) -> None:
        self.cancel_event.set()
        # 塞一个 token 让 _maybe_wait_step 能从 get 里退出，进而轮询检查 cancel
        try:
            self.step_queue.put_nowait(None)
        except queue.Full:
            pass

    def run(self) -> None:
        profile_key = f"{self.mumu.device_w}x{self.mumu.device_h}"
        had_profile = profile_key in self.movement_registry.profiles
        # ensure_profile: 没有就按默认常量创建一个，让 routine 里不依赖运动细节的
        # 步骤（sleep / click / wait_*）仍能跑起来；真正用到的字段（ui.xxx / vision）
        # 在具体 handler 里会各自报缺配置。
        mp = self.movement_registry.ensure_profile(
            (self.mumu.device_w, self.mumu.device_h)
        )
        if not had_profile:
            # 刚创建出来的默认 profile 写回磁盘，避免下次启动又走这个分支
            try:
                self.movement_registry.save()
                self.signals.log.emit(
                    "INFO",
                    f"分辨率 {profile_key} 的 movement_profile 不存在，已按默认值创建并保存",
                )
            except Exception as e:
                log.exception("保存新建 movement_profile 失败")
                self.signals.log.emit(
                    "WARNING",
                    f"新建 movement_profile 但保存失败: {type(e).__name__}: {e}",
                )

        hooks = RunnerHooks(
            on_log=lambda lvl, msg: self.signals.log.emit(lvl, msg),
            on_progress=lambda si, st, li, lt: self.signals.progress.emit(
                si, st, li, lt
            ),
            on_substep=lambda sub, total: self.signals.substep.emit(sub, total),
            cancel_event=self.cancel_event,
            step_queue=self.step_queue if self.step_mode else None,
        )
        try:
            runner = RoutineRunner(
                self.mumu, self.routine, self.map_registry, mp, hooks=hooks
            )
        except Exception as e:
            log.exception("runner 初始化失败")
            self.signals.finished.emit(False, f"{type(e).__name__}: {e}")
            return
        try:
            runner.run()
            self.signals.finished.emit(True, "执行完成")
        except RoutineCancelled:
            self.signals.finished.emit(False, "已停止")
        except Exception as e:
            log.exception("routine 执行异常")
            self.signals.finished.emit(False, f"{type(e).__name__}: {e}")


# =============================================================================
# Dialog
# =============================================================================


class RoutineRunnerDialog(QDialog):
    def __init__(
        self,
        mumu: Mumu,
        parent=None,
        routines_dir: Path = DEFAULT_ROUTINES_DIR,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("执行 Routine")
        self.resize(780, 620)

        self._mumu = mumu
        self._routines_dir = routines_dir
        self._routine: Optional[Routine] = None
        self._thread: Optional[QThread] = None
        self._worker: Optional[_RunnerWorker] = None

        self._build_ui()
        self._reload_routines()

    # ========================================================================
    # UI
    # ========================================================================

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(8)

        # 顶部: 选文件 + 参数
        top = QHBoxLayout()
        top.addWidget(QLabel("Routine:"))
        self._routine_combo = QComboBox()
        self._routine_combo.setMinimumWidth(220)
        self._routine_combo.currentIndexChanged.connect(self._on_routine_changed)
        top.addWidget(self._routine_combo)
        btn_reload = QPushButton("↻")
        btn_reload.setMaximumWidth(36)
        btn_reload.setToolTip("重新扫描 routines 目录")
        btn_reload.clicked.connect(self._reload_routines)
        top.addWidget(btn_reload)
        btn_open = QPushButton("打开其他…")
        btn_open.clicked.connect(self._on_open_file)
        top.addWidget(btn_open)
        top.addStretch(1)
        root.addLayout(top)

        # 参数行
        params = QHBoxLayout()
        params.addWidget(QLabel("循环次数:"))
        self._loop_count = QSpinBox()
        self._loop_count.setRange(0, 999999)
        self._loop_count.setSpecialValueText("∞")
        params.addWidget(self._loop_count)
        params.addWidget(QLabel("每轮间隔(s):"))
        self._loop_interval = QDoubleSpinBox()
        self._loop_interval.setRange(0, 600)
        self._loop_interval.setDecimals(1)
        params.addWidget(self._loop_interval)
        params.addSpacing(20)
        params.addWidget(QLabel("模式:"))
        self._mode_combo = QComboBox()
        self._mode_combo.addItems(["一口气跑", "单步（每步等确认）"])
        params.addWidget(self._mode_combo)
        params.addStretch(1)
        btn_save_params = QPushButton("保存参数到 YAML")
        btn_save_params.setToolTip("把循环次数/间隔写回 routine 文件")
        btn_save_params.clicked.connect(self._on_save_params)
        params.addWidget(btn_save_params)
        root.addLayout(params)

        # 中部: 步骤预览 + 日志 左右分栏
        middle = QHBoxLayout()
        left_col = QVBoxLayout()
        left_col.addWidget(QLabel("步骤预览"))
        self._steps_list = QListWidget()
        left_col.addWidget(self._steps_list, 1)
        lw = QWidget()
        lw.setLayout(left_col)
        lw.setMinimumWidth(280)
        lw.setMaximumWidth(360)
        middle.addWidget(lw)

        right_col = QVBoxLayout()
        right_col.addWidget(QLabel("日志"))
        self._log_view = QPlainTextEdit()
        self._log_view.setReadOnly(True)
        self._log_view.setMaximumBlockCount(2000)
        right_col.addWidget(self._log_view, 1)
        rw = QWidget()
        rw.setLayout(right_col)
        middle.addWidget(rw, 1)
        root.addLayout(middle, 1)

        # 进度条
        self._progress = QProgressBar()
        self._progress.setFormat("第 0/0 步 | 第 0/0 轮")
        root.addWidget(self._progress)

        # 底部按钮
        btns = QHBoxLayout()
        self._btn_start = QPushButton("开始")
        self._btn_start.clicked.connect(self._on_start)
        btns.addWidget(self._btn_start)
        self._btn_step = QPushButton("单步")
        self._btn_step.setEnabled(False)
        self._btn_step.clicked.connect(self._on_step)
        btns.addWidget(self._btn_step)
        self._btn_stop = QPushButton("停止")
        self._btn_stop.setEnabled(False)
        self._btn_stop.clicked.connect(self._on_stop)
        btns.addWidget(self._btn_stop)
        btns.addStretch(1)
        btn_close = QPushButton("关闭")
        btn_close.clicked.connect(self.close)
        btns.addWidget(btn_close)
        root.addLayout(btns)

    # ========================================================================
    # 文件加载
    # ========================================================================

    def _reload_routines(self) -> None:
        self._routine_combo.blockSignals(True)
        self._routine_combo.clear()
        for p in list_routines(self._routines_dir):
            self._routine_combo.addItem(p.name, p)
        self._routine_combo.blockSignals(False)
        if self._routine_combo.count() > 0:
            self._routine_combo.setCurrentIndex(0)
            self._on_routine_changed(0)

    def _on_routine_changed(self, idx: int) -> None:
        path = self._routine_combo.currentData()
        if not path:
            self._routine = None
            return
        self._load_routine_from(path)

    def _on_open_file(self) -> None:
        fn, _ = QFileDialog.getOpenFileName(
            self, "打开 Routine", "", "YAML (*.yaml *.yml)"
        )
        if not fn:
            return
        self._load_routine_from(Path(fn))

    def _load_routine_from(self, path: Path) -> None:
        try:
            self._routine = Routine.load(path)
        except Exception as e:
            log.exception("加载 routine 失败")
            QMessageBox.critical(self, "加载失败", f"{type(e).__name__}: {e}")
            return
        self._loop_count.setValue(self._routine.loop_count)
        self._loop_interval.setValue(self._routine.loop_interval)
        self._refresh_steps_preview()

    def _refresh_steps_preview(self) -> None:
        self._steps_list.clear()
        if self._routine is None:
            return
        if self._routine.starting_map:
            hdr = QListWidgetItem(f"  ▶ 起始地图: {self._routine.starting_map}")
            hdr.setFlags(Qt.ItemIsEnabled)  # 不可选中
            self._steps_list.addItem(hdr)
        for i, s in enumerate(self._routine.steps, start=1):
            desc = _describe_step(s)
            self._steps_list.addItem(QListWidgetItem(f"{i:>3}. {desc}"))

    def _on_save_params(self) -> None:
        if self._routine is None or self._routine.path is None:
            return
        self._routine.loop_count = self._loop_count.value()
        self._routine.loop_interval = self._loop_interval.value()
        try:
            self._routine.save()
        except Exception as e:
            QMessageBox.critical(self, "保存失败", f"{type(e).__name__}: {e}")
            return
        QMessageBox.information(self, "已保存", f"已写回 {self._routine.path}")

    # ========================================================================
    # 执行控制
    # ========================================================================

    def _on_start(self) -> None:
        if self._routine is None:
            QMessageBox.warning(self, "没选 routine", "请先选择一个 routine")
            return
        if self._thread is not None:
            return

        # 应用 UI 的 loop_count / interval
        self._routine.loop_count = self._loop_count.value()
        self._routine.loop_interval = self._loop_interval.value()
        step_mode = self._mode_combo.currentIndex() == 1

        # 加载两个 registry
        try:
            map_reg = MapRegistry.load(MAP_REGISTRY_PATH)
            mov_reg = MovementRegistry.load(DEFAULT_MOVEMENT_YAML_PATH)
        except Exception as e:
            QMessageBox.critical(self, "加载配置失败", f"{type(e).__name__}: {e}")
            return

        self._log_view.clear()
        self._append_log("INFO", "启动 routine")

        self._thread = QThread(self)
        self._worker = _RunnerWorker(
            self._mumu, self._routine, map_reg, mov_reg, step_mode
        )
        self._worker.moveToThread(self._thread)
        self._worker.signals.log.connect(self._append_log, Qt.QueuedConnection)
        self._worker.signals.progress.connect(self._on_progress, Qt.QueuedConnection)
        self._worker.signals.substep.connect(self._on_substep, Qt.QueuedConnection)
        self._worker.signals.finished.connect(self._on_finished, Qt.QueuedConnection)
        self._thread.started.connect(self._worker.run)
        self._thread.start()

        self._btn_start.setEnabled(False)
        self._btn_step.setEnabled(step_mode)
        self._btn_stop.setEnabled(True)

    def _on_step(self) -> None:
        if self._worker is not None:
            self._worker.step_once()
            # 节流: 点完禁用，等下一个 progress 信号（表示又到了一个 wait 点）再重启
            self._btn_step.setEnabled(False)

    def _on_stop(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
        self._btn_stop.setEnabled(False)

    def _on_finished(self, ok: bool, reason: str) -> None:
        self._append_log("INFO" if ok else "WARNING", reason)
        self._teardown_thread()
        self._btn_start.setEnabled(True)
        self._btn_step.setEnabled(False)
        self._btn_stop.setEnabled(False)

    def _teardown_thread(self) -> None:
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait(2000)
        self._thread = None
        self._worker = None

    def _on_progress(self, si: int, st: int, li: int, lt: int) -> None:
        self._progress.setMaximum(st)
        self._progress.setValue(si)
        # 记录当前步骤信息，给 substep 信号合成文案时用
        self._last_progress = (si, st, li, lt)
        self._update_progress_text()
        # 高亮当前步（若有 starting_map header 要跳过第 0 行）
        offset = 1 if (self._routine and self._routine.starting_map) else 0
        self._steps_list.setCurrentRow(si - 1 + offset)
        # 单步模式下: 这次 progress 说明 worker 到达了新的 wait 点，允许再次点击
        if self._worker is not None and self._worker.step_mode:
            self._btn_step.setEnabled(True)

    def _on_substep(self, sub: int, sub_total: int) -> None:
        """
        子步进度: 来自 MoveStep 内部每次原子段。
        sub_total == 0 表示清空（Step 结束）。
        单步模式下 sub > 1 的每次都重新启用按钮（worker 即将 wait）。
        """
        self._current_substep = (sub, sub_total)
        self._update_progress_text()
        # 单步模式下，第 2 段起每次 substep 回调都意味着 worker 又到 wait 点了
        # （第 1 段不等 wait，外层 Step 级 wait 已经消费）
        if self._worker is not None and self._worker.step_mode and sub > 1:
            self._btn_step.setEnabled(True)

    def _update_progress_text(self) -> None:
        if not hasattr(self, "_last_progress"):
            return
        si, st, li, lt = self._last_progress
        lt_str = str(lt) if lt > 0 else "∞"
        base = f"第 {si}/{st} 步 | 第 {li}/{lt_str} 轮"
        sub, sub_total = getattr(self, "_current_substep", (0, 0))
        if sub_total > 0:
            base += f"  [子步 {sub}/{sub_total}]"
        self._progress.setFormat(base)

    def _append_log(self, level: str, msg: str) -> None:
        colored_prefix = {
            "WARNING": "[warn] ",
            "ERROR": "[err]  ",
            "INFO": "[info] ",
            "info": "[info] ",
        }.get(level, f"[{level}] ")
        self._log_view.appendPlainText(colored_prefix + msg)
        self._log_view.moveCursor(QTextCursor.End)

    # ========================================================================
    # 关闭
    # ========================================================================

    def closeEvent(self, ev) -> None:
        if self._worker is not None:
            self._worker.cancel()
        self._teardown_thread()
        super().closeEvent(ev)


def _describe_step(s) -> str:
    t = s.TYPE
    at = f" @{s.at_map}" if s.at_map else ""
    if t == "travel":
        return f"travel → {s.to}{at}"
    if t == "move":
        p = s.path
        if len(p) <= 3:
            return f"move {p}{at}"
        return f"move {p[0]} → ... → {p[-1]} ({len(p)} 点){at}"
    if t == "button":
        return f"button {s.name}{at}"
    if t == "click":
        return f"click ({s.pos[0]:.3f}, {s.pos[1]:.3f}){at}"
    if t == "buy":
        return f"buy {len(s.items)} 项"
    if t == "sleep":
        return f"sleep {s.seconds}s"
    if t == "wait_pos_stable":
        return f"wait_pos_stable"
    if t == "wait_screen_stable":
        return f"wait_screen_stable"
    if t == "enter_map":
        return f"enter_map → {s.map}"
    return t
