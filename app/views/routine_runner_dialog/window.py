"""
Routine 执行器对话框：选择 routine yaml，启动/暂停/单步/停止。

线程模型:
    主线程: QDialog
    工作线程: QThread + _RunnerWorker
    通信:   signal/slot (跨线程自动 QueuedConnection)
    中断:   threading.Event (cancel_event / step_event)

可观测性:
    - 日志区可选「显示时间戳」前缀（默认开），方便排查耗时
    - 工作线程启动时给 app.core.mover / app.core.ocr 临时挂一个 LogHandler，
      把这俩模块的 log.warning/log.info 桥接到 GUI 日志窗口
"""

from __future__ import annotations

import logging
import queue
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, Qt, QThread, Signal
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import (
    QCheckBox,
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


# 日志桥：把这些子模块的 record 转发到 GUI（routine 运行期间有效）
_BRIDGE_LOGGER_NAMES = ("app.core.mover", "app.core.ocr")


# =============================================================================
# 日志桥
# =============================================================================


class _HooksLogHandler(logging.Handler):
    """把 mover / ocr 内部的 log record 通过 worker.signals.log 转发到 GUI。"""

    def __init__(self, signals: "_RunnerSignals") -> None:
        super().__init__()
        self.signals = signals

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.signals.log.emit(record.levelname, record.getMessage())
        except Exception:
            pass


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
        try:
            self.step_queue.put_nowait(None)
        except queue.Full:
            pass

    def cancel(self) -> None:
        self.cancel_event.set()
        try:
            self.step_queue.put_nowait(None)
        except queue.Full:
            pass

    def _install_log_bridge(self) -> tuple[_HooksLogHandler, list[logging.Logger]]:
        """给 mover / ocr 装上 GUI 日志桥。返回 (handler, 改动过的 loggers)。"""
        handler = _HooksLogHandler(self.signals)
        bridged: list[logging.Logger] = []
        for name in _BRIDGE_LOGGER_NAMES:
            lg = logging.getLogger(name)
            lg.addHandler(handler)
            # 确保 INFO 能传出（NOTSET=0 时由父级决定，多数情况下也是 INFO 起步，
            # 但为了 routine 期间一定能看见，强制设到 INFO）
            if lg.level == 0 or lg.level > logging.INFO:
                lg.setLevel(logging.INFO)
            bridged.append(lg)
        return handler, bridged

    @staticmethod
    def _remove_log_bridge(
        handler: _HooksLogHandler, bridged: list[logging.Logger]
    ) -> None:
        for lg in bridged:
            try:
                lg.removeHandler(handler)
            except Exception:
                pass

    def run(self) -> None:
        profile_key = f"{self.mumu.device_w}x{self.mumu.device_h}"
        had_profile = profile_key in self.movement_registry.profiles
        mp = self.movement_registry.ensure_profile(
            (self.mumu.device_w, self.mumu.device_h)
        )
        if not had_profile:
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

        # 装日志桥（routine 跑完无论成功失败都拆）
        handler, bridged = self._install_log_bridge()
        try:
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
        finally:
            self._remove_log_bridge(handler, bridged)


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

        # 结构化保存日志，便于切换时间戳显示时整体重渲染
        self._log_entries: list[tuple[datetime, str, str]] = []

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
        # 日志区标题行：标签 + 时间戳 + 清空
        log_header = QHBoxLayout()
        log_header.addWidget(QLabel("日志"))
        log_header.addStretch(1)
        self._chk_show_timestamp = QCheckBox("显示时间戳")
        self._chk_show_timestamp.setChecked(True)
        self._chk_show_timestamp.setToolTip(
            "前缀格式 [HH:MM:SS.fff]；切换会立刻重渲染已有日志"
        )
        self._chk_show_timestamp.stateChanged.connect(self._on_timestamp_toggled)
        log_header.addWidget(self._chk_show_timestamp)
        btn_clear_log = QPushButton("清空")
        btn_clear_log.setMaximumWidth(60)
        btn_clear_log.clicked.connect(self._clear_log)
        log_header.addWidget(btn_clear_log)
        right_col.addLayout(log_header)

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
            hdr.setFlags(Qt.ItemIsEnabled)
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

        self._routine.loop_count = self._loop_count.value()
        self._routine.loop_interval = self._loop_interval.value()
        step_mode = self._mode_combo.currentIndex() == 1

        try:
            map_reg = MapRegistry.load(MAP_REGISTRY_PATH)
            mov_reg = MovementRegistry.load(DEFAULT_MOVEMENT_YAML_PATH)
        except Exception as e:
            QMessageBox.critical(self, "加载配置失败", f"{type(e).__name__}: {e}")
            return

        self._clear_log()
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
        self._last_progress = (si, st, li, lt)
        self._update_progress_text()
        offset = 1 if (self._routine and self._routine.starting_map) else 0
        self._steps_list.setCurrentRow(si - 1 + offset)
        if self._worker is not None and self._worker.step_mode:
            self._btn_step.setEnabled(True)

    def _on_substep(self, sub: int, sub_total: int) -> None:
        self._current_substep = (sub, sub_total)
        self._update_progress_text()
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

    # ---- 日志渲染 ----

    _MAX_LOG_ENTRIES = 2000

    @staticmethod
    def _format_level_prefix(level: str) -> str:
        prefix_map = {
            "WARNING": "[warn] ",
            "ERROR": "[err]  ",
            "INFO": "[info] ",
            "DEBUG": "[dbg]  ",
        }
        return prefix_map.get(level.upper(), f"[{level}] ")

    def _render_log_line(self, ts: datetime, level: str, msg: str) -> str:
        level_prefix = self._format_level_prefix(level)
        if self._chk_show_timestamp.isChecked():
            ts_str = ts.strftime("%H:%M:%S.%f")[:-3]  # ms 精度
            return f"[{ts_str}] {level_prefix}{msg}"
        return f"{level_prefix}{msg}"

    def _append_log(self, level: str, msg: str) -> None:
        now = datetime.now()
        self._log_entries.append((now, level, msg))
        # 同步限制 list 长度（QPlainTextEdit 有 setMaximumBlockCount，但 entries 也要 cap，
        # 否则切换重渲染时一次写太多行会卡）
        excess = len(self._log_entries) - self._MAX_LOG_ENTRIES
        if excess > 0:
            del self._log_entries[:excess]

        self._log_view.appendPlainText(self._render_log_line(now, level, msg))
        self._log_view.moveCursor(QTextCursor.End)

    def _on_timestamp_toggled(self, _state: int) -> None:
        # 整体重渲染。setPlainText 一次性替换比 N 次 appendPlainText 快得多
        lines = [
            self._render_log_line(ts, lvl, msg) for ts, lvl, msg in self._log_entries
        ]
        self._log_view.setPlainText("\n".join(lines))
        self._log_view.moveCursor(QTextCursor.End)

    def _clear_log(self) -> None:
        self._log_entries.clear()
        self._log_view.clear()

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
