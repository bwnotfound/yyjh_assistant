"""
Routine 执行器对话框：选择 routine yaml，启动/暂停/单步/停止。

线程模型:
    主线程: QDialog
    工作线程: QThread + _RunnerWorker
    通信:   signal/slot (跨线程自动 QueuedConnection)
    中断:   threading.Event (cancel_event / step_event)

日志显示控制 (UI 侧, 不影响 worker / runner):
    - 详细等级下拉框: 简洁(默认) / 标准 / 详细
        简洁: 隐藏 step 调度行(左侧 list 已高亮当前步), 隐藏轮次分隔行,
              只保留 warning/error 与关键 info(降级提示/已停止/完成等)
        标准: 显示 runner hooks 的 info+warning+error,
              加上 mover/ocr/profiles 的 INFO+WARNING+ERROR,
              不显示子步进度或 OCR 单次耗时这种 DEBUG
        详细: 在标准基础上加上所有 DEBUG, 包括 OCR 单次耗时、
              move 每段总耗时、_wait_via_ocr 各阶段耗时、子步进度
    - 时间戳复选框 (默认关): 每行前缀 HH:MM:SS.mmm
    - 切换设置时, 历史日志会从原始缓冲区"回填"重新渲染, 体验一致

logger 桥接:
    runner 通过 hooks.on_log 已经把业务 log 推到 UI; mover.py / ocr.py / profiles.py
    用的是 python logger, 默认进 stderr 而不进 UI。这里用一个 _LogBridgeHandler
    在 worker 运行期间动态 attach 到这些 logger, 让所有 DEBUG/INFO/WARNING/ERROR
    都流到 _RunnerSignals.log, 进入同一个缓冲区 + 过滤管线。
"""

from __future__ import annotations

import logging
import queue
import re
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Deque, Optional

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
    MovementConfig,
)
from app.core.routine import DEFAULT_ROUTINES_DIR, Routine, list_routines
from app.core.runner import RoutineCancelled, RoutineRunner, RunnerHooks
from config.common.map_registry import (
    DEFAULT_YAML_PATH as MAP_REGISTRY_PATH,
    MapRegistry,
)

log = logging.getLogger(__name__)


# =============================================================================
# logger 桥接
# =============================================================================

# 要桥接的 python logger 名 (子 logger 会跟随父级, 但显式列清楚更安全)
_BRIDGE_LOGGER_NAMES = (
    "app.core.mover",
    "app.core.ocr",
    "app.core.profiles",
    # routine 不在这里 —— 它已经走 hooks.on_log, 桥接会重复
)

# 桥接来的 entry 在 signal 上的 level 前缀, UI 端解析时去掉
_BRIDGE_LEVEL_PREFIX = "__BRIDGE__:"


class _LogBridgeHandler(logging.Handler):
    """
    把 python logger 的 record 转发到 _RunnerSignals.log。

    Qt 的 signal/slot 跨线程是用 QueuedConnection 自动处理的, emit 在
    worker 线程被调用时也线程安全。这里只把 (level, msg) 投递过去, 真正
    的过滤/格式化都在 UI 端做, 保留原始信息以便回填。
    """

    def __init__(self, signals: "_RunnerSignals") -> None:
        super().__init__(level=logging.DEBUG)
        self._signals = signals
        # 加 logger 短名作前缀, 便于在 UI 里区分来源
        self.setFormatter(logging.Formatter("[%(name)s] %(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            level = record.levelname  # DEBUG / INFO / WARNING / ERROR / CRITICAL
            if level == "CRITICAL":
                level = "ERROR"
            self._signals.log.emit(_BRIDGE_LEVEL_PREFIX + level, msg)
        except Exception:
            # logging handler 不能让异常逃出去, 否则会污染业务调用栈
            self.handleError(record)


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
        movement_config: MovementConfig,
        step_mode: bool,
    ) -> None:
        super().__init__()
        self.mumu = mumu
        self.routine = routine
        self.map_registry = map_registry
        self.movement_config = movement_config
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

    def _attach_log_bridge(
        self,
    ) -> tuple["_LogBridgeHandler", list[tuple[logging.Logger, int]]]:
        """运行前 attach handler 到核心 logger, 临时把 level 拉到 DEBUG。"""
        bridge = _LogBridgeHandler(self.signals)
        saved: list[tuple[logging.Logger, int]] = []
        for name in _BRIDGE_LOGGER_NAMES:
            lg = logging.getLogger(name)
            saved.append((lg, lg.level))
            # logger 自身 level 必须 ≤ DEBUG, handler 才能收到 DEBUG 记录
            lg.setLevel(logging.DEBUG)
            lg.addHandler(bridge)
        return bridge, saved

    def _detach_log_bridge(
        self,
        bridge: "_LogBridgeHandler",
        saved: list[tuple[logging.Logger, int]],
    ) -> None:
        for lg, original in saved:
            try:
                lg.removeHandler(bridge)
            except Exception:
                pass
            lg.setLevel(original)

    def run(self) -> None:
        # MovementConfig 在新 schema 下是单一对象, 加载时若文件缺失会返回带默认值的实例。
        # 这里直接用主线程传进来的 movement_config 即可, 不需要再 ensure 或落盘。
        # (旧代码这里要按当前分辨率 ensure_profile + save, 因为 yaml 是按分辨率分桶的)
        mp = self.movement_config

        hooks = RunnerHooks(
            on_log=lambda lvl, msg: self.signals.log.emit(lvl, msg),
            on_progress=lambda si, st, li, lt: self.signals.progress.emit(
                si, st, li, lt
            ),
            on_substep=lambda sub, total: self.signals.substep.emit(sub, total),
            cancel_event=self.cancel_event,
            step_queue=self.step_queue if self.step_mode else None,
        )
        bridge, saved = self._attach_log_bridge()
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
            self._detach_log_bridge(bridge, saved)


# =============================================================================
# 日志详细等级
# =============================================================================

# 等级常量（同时也是 QComboBox 的 currentIndex）
VERBOSITY_BRIEF = 0
VERBOSITY_NORMAL = 1
VERBOSITY_DETAIL = 2

# 简洁档下用来识别"可隐藏"的 info 行的正则 (来自 runner hooks)
# 1) step 调度行: 来自 runner._execute_one, 形如 "  [3/12] move@黑水沟"
_RE_STEP_DISPATCH = re.compile(r"^\s*\[\d+/\d+\]\s+\w+")
# 2) 轮次分隔行: 来自 runner.run, 形如 "=== 第 1/3 轮开始 ===" / "=== 第 1 轮结束 ==="
_RE_LOOP_MARKER = re.compile(r"^\s*===\s*第\s")
# 3) 轮间等待: "等待 5.0s 后进入下一轮"
_RE_LOOP_WAIT = re.compile(r"^\s*等待\s+\S+s?\s+后进入下一轮")
# 4) sub-routine 进出: "  ↳ 进入 sub-routine [..]" / "  ↳ 退出 sub-routine [..]"
_RE_SUBROUTINE = re.compile(r"^\s*↳\s*(进入|退出)\s+sub-routine")


@dataclass
class _LogEntry:
    """原始日志条目，保留所有信息以便切换显示设置时回填。"""

    ts_ns: int
    level: str  # "INFO" | "WARNING" | "ERROR" | "DEBUG"
    msg: str

    # 简洁档过滤用的预计算 hint (仅对非 bridged INFO 有意义)
    is_step_dispatch: bool = False
    is_loop_marker: bool = False
    is_minor_info: bool = False  # 轮次等待 / sub-routine 进出
    is_bridged: bool = False  # 来自 mover/ocr/profiles 的 python logger


# 缓冲上限：详细档 OCR read_verbose ~10 次/秒, 桥接 DEBUG 量大,
# 8000 大约够 10+ 分钟; 与 _log_view 的 setMaximumBlockCount 取一致
_LOG_BUFFER_MAX = 8000


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
        self.resize(820, 620)

        self._mumu = mumu
        self._routines_dir = routines_dir
        self._routine: Optional[Routine] = None
        self._thread: Optional[QThread] = None
        self._worker: Optional[_RunnerWorker] = None

        # 原始日志缓冲区：所有 entry 都进这里，view 显示时按当前 verbosity / timestamp 设置过滤
        self._log_buffer: Deque[_LogEntry] = deque(maxlen=_LOG_BUFFER_MAX)

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

        # 日志面板表头：标题 + 详细等级下拉 + 时间戳勾选 + 清空按钮
        log_header = QHBoxLayout()
        log_header.addWidget(QLabel("日志"))
        log_header.addStretch(1)
        log_header.addWidget(QLabel("详细程度:"))
        self._verbosity_combo = QComboBox()
        self._verbosity_combo.addItems(["简洁", "标准", "详细"])
        self._verbosity_combo.setCurrentIndex(VERBOSITY_BRIEF)  # 默认简洁
        self._verbosity_combo.setToolTip(
            "简洁: 只显示警告/错误和关键节点\n"
            "标准: 显示 runner 业务事件 + mover/ocr/profiles 的 info/warn/err\n"
            "详细: 加上所有 DEBUG (OCR 单次耗时、每段总耗时、子步进度等)"
        )
        self._verbosity_combo.currentIndexChanged.connect(self._on_log_filter_changed)
        log_header.addWidget(self._verbosity_combo)
        self._timestamp_check = QCheckBox("时间戳")
        self._timestamp_check.setChecked(False)  # 默认不显示
        self._timestamp_check.setToolTip("每行前面加 HH:MM:SS.mmm 时间戳")
        self._timestamp_check.toggled.connect(self._on_log_filter_changed)
        log_header.addWidget(self._timestamp_check)
        btn_clear_log = QPushButton("清空")
        btn_clear_log.setToolTip(
            "清空日志面板与缓冲区(运行中也可用,后续日志会继续显示)"
        )
        btn_clear_log.clicked.connect(self._on_clear_log)
        log_header.addWidget(btn_clear_log)
        right_col.addLayout(log_header)

        self._log_view = QPlainTextEdit()
        self._log_view.setReadOnly(True)
        self._log_view.setMaximumBlockCount(_LOG_BUFFER_MAX)
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

        # 加载两个 registry / config
        try:
            map_reg = MapRegistry.load(MAP_REGISTRY_PATH)
            mov_cfg = MovementConfig.load(DEFAULT_MOVEMENT_YAML_PATH)
        except Exception as e:
            QMessageBox.critical(self, "加载配置失败", f"{type(e).__name__}: {e}")
            return

        # 清空缓冲区与 view
        self._log_buffer.clear()
        self._log_view.clear()
        self._record_log("INFO", "启动 routine")

        self._thread = QThread(self)
        self._worker = _RunnerWorker(
            self._mumu, self._routine, map_reg, mov_cfg, step_mode
        )
        self._worker.moveToThread(self._thread)
        self._worker.signals.log.connect(self._on_runner_log, Qt.QueuedConnection)
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
        self._record_log("INFO" if ok else "WARNING", reason)
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
        # 同时把 substep 进度作为 DEBUG 级别的日志写入缓冲区，
        # 这样切到"详细"档时能在日志里看到子步序列。
        # sub_total == 0 是"清空"通知，不入日志。
        if sub_total > 0:
            self._record_log("DEBUG", f"子步 {sub}/{sub_total}")

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

    # ========================================================================
    # 日志: 缓冲 + 过滤 + 渲染
    # ========================================================================

    def _on_runner_log(self, level: str, msg: str) -> None:
        """worker 发来的 log signal 入口 (含桥接来的 python logger 记录)。"""
        is_bridged = level.startswith(_BRIDGE_LEVEL_PREFIX)
        if is_bridged:
            level = level[len(_BRIDGE_LEVEL_PREFIX) :]
        self._record_log(level, msg, is_bridged=is_bridged)

    def _record_log(self, level: str, msg: str, *, is_bridged: bool = False) -> None:
        """
        统一入口: 入缓冲区 + 在当前显示设置下增量渲染到 view。
        """
        entry = _make_entry(level, msg, is_bridged=is_bridged)
        self._log_buffer.append(entry)
        if self._should_show(entry):
            self._log_view.appendPlainText(self._format_entry(entry))
            self._log_view.moveCursor(QTextCursor.End)

    def _on_log_filter_changed(self, *_args) -> None:
        """详细等级 / 时间戳变化时：清空 view 并从 buffer 全量重画。"""
        self._redraw_log_view()

    def _on_clear_log(self) -> None:
        """清空日志面板与缓冲区。运行中也可用,后续 entry 会继续追加。"""
        self._log_buffer.clear()
        self._log_view.clear()

    def _redraw_log_view(self) -> None:
        self._log_view.clear()
        # 一次性拼好再 setPlainText，比逐条 append 在大缓冲下更快
        lines = [
            self._format_entry(e) for e in self._log_buffer if self._should_show(e)
        ]
        if lines:
            self._log_view.setPlainText("\n".join(lines))
            self._log_view.moveCursor(QTextCursor.End)

    def _should_show(self, entry: _LogEntry) -> bool:
        verbosity = self._verbosity_combo.currentIndex()
        lvl = entry.level.upper()

        # 详细档：显示一切
        if verbosity == VERBOSITY_DETAIL:
            return True

        # 非详细档下 DEBUG 都不显示 (DEBUG 是详细档专属)
        if lvl == "DEBUG":
            return False

        if verbosity == VERBOSITY_NORMAL:
            # 标准档: 桥接的 INFO/WARNING/ERROR 都显示 (mover 的 click/fly 参数日志有用)
            return True

        # 简洁档 (verbosity == VERBOSITY_BRIEF)
        if lvl in ("WARNING", "ERROR"):
            return True
        # INFO:
        # - 桥接来的 INFO 一律不显示 (mover 的参数日志在简洁档下太吵)
        # - hooks 的 INFO 过滤掉调度/分隔/轮间等待/sub-routine 进出, 其余保留
        if entry.is_bridged:
            return False
        if entry.is_step_dispatch or entry.is_loop_marker or entry.is_minor_info:
            return False
        return True

    def _format_entry(self, entry: _LogEntry) -> str:
        prefix = _LEVEL_PREFIX.get(entry.level.upper(), f"[{entry.level}] ")
        if self._timestamp_check.isChecked():
            ts = _format_ts(entry.ts_ns)
            return f"{ts} {prefix}{entry.msg}"
        return f"{prefix}{entry.msg}"

    # ========================================================================
    # 关闭
    # ========================================================================

    def closeEvent(self, ev) -> None:
        if self._worker is not None:
            self._worker.cancel()
        self._teardown_thread()
        super().closeEvent(ev)


# =============================================================================
# 工具函数
# =============================================================================

_LEVEL_PREFIX = {
    "WARNING": "[warn]  ",
    "ERROR": "[err]   ",
    "INFO": "[info]  ",
    "DEBUG": "[debug] ",
}


def _format_ts(ts_ns: int) -> str:
    """ts_ns -> 'HH:MM:SS.mmm'"""
    sec = ts_ns / 1e9
    lt = time.localtime(sec)
    ms = int((ts_ns // 1_000_000) % 1000)
    return f"{lt.tm_hour:02d}:{lt.tm_min:02d}:{lt.tm_sec:02d}.{ms:03d}"


def _make_entry(level: str, msg: str, *, is_bridged: bool = False) -> _LogEntry:
    """构造 _LogEntry 并预计算"简洁档可隐藏"的 hint。"""
    lvl = level.upper()
    is_step_dispatch = False
    is_loop_marker = False
    is_minor_info = False
    # 这些 hint 只对 hooks 来源的 INFO 有意义
    # (识别 runner._execute_one / runner.run 的固定文案, 不会撞 mover 的日志)
    if lvl == "INFO" and not is_bridged:
        if _RE_STEP_DISPATCH.match(msg):
            is_step_dispatch = True
        elif _RE_LOOP_MARKER.match(msg):
            is_loop_marker = True
        elif _RE_LOOP_WAIT.match(msg) or _RE_SUBROUTINE.match(msg):
            is_minor_info = True
    return _LogEntry(
        ts_ns=time.time_ns(),
        level=lvl,
        msg=msg,
        is_step_dispatch=is_step_dispatch,
        is_loop_marker=is_loop_marker,
        is_minor_info=is_minor_info,
        is_bridged=is_bridged,
    )


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
        if getattr(s, "template", None):
            return f"button ★[{s.template}]{at}"
        if getattr(s, "delay_preset", None):
            return f"button {s.name} delay◆{s.delay_preset}{at}"
        return f"button {s.name}{at}"
    if t == "click":
        if getattr(s, "template", None):
            return f"click ★[{s.template}]{at}"
        head = (
            f"click [{s.preset}]"
            if getattr(s, "preset", None)
            else f"click ({s.pos[0]:.3f}, {s.pos[1]:.3f})"
        )
        if getattr(s, "delay_preset", None):
            return f"{head} delay◆{s.delay_preset}{at}"
        return f"{head}{at}"
    if t == "buy":
        return f"buy {len(s.items)} 项"
    if t == "sleep":
        if getattr(s, "preset", None):
            return f"sleep [{s.preset}]{at}"
        return f"sleep {s.seconds}s{at}"
    if t == "include":
        return f"include → {s.routine}{at}"
    if t == "wait_pos_stable":
        return f"wait_pos_stable"
    if t == "wait_screen_stable":
        return f"wait_screen_stable"
    if t == "enter_map":
        return f"enter_map → {s.map}{at}"
    return t
