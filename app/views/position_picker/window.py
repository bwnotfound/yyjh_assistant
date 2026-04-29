"""
取位置工具 - 全局快捷键 + 放大截图，记录鼠标在 MuMu render_wnd 客户区内的归一化坐标 + 颜色。

两种采样模式（共存）:
    1. 实时模式（默认开启）: 全局快捷键触发，按下时按当前鼠标位置采样
    2. 截图选点模式: 截一帧 → 弹放大窗 → 用户在放大图上点击采样
       适合小目标、动态画面下精确定点

坐标系（两种模式统一归一化语义）:
    - 鼠标屏幕坐标 → Win32 ScreenToClient(render_wnd) → 客户区像素 → 除以客户区尺寸归一化
    - 所有调用统一走 Win32 API（GetCursorPos / ScreenToClient / GetClientRect），
      和 render_wnd 在同一坐标系下，不受 Qt/进程 DPI awareness 影响。
    - 截图选点模式下,放大窗内点击的归一化坐标用截图原图尺寸算;
      最终采样调用同一个 _sample_at(nx, ny) 入口,语义和实时模式一致。

两种使用场景:
    - 普通模式（默认）: 非模态,可随时采样,"复制选中 JSON"导出
    - 选择模式 (selection_mode=True): 模态,外部通过 exec() 调用,
      采满 expected_count 条后可点"确认选择"关闭对话框;
      调用方用 result_records() 取回结果。
"""

from __future__ import annotations

import ctypes
import json
import logging
from ctypes import wintypes
from dataclasses import dataclass
from typing import Optional

from PIL import Image, ImageQt
from PySide6.QtCore import Qt, QObject, Signal
from PySide6.QtGui import (
    QColor,
    QGuiApplication,
    QMouseEvent,
    QPainter,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from utils import Mumu, get_client_size_logical

log = logging.getLogger(__name__)


# =============================================================================
# Win32 helpers
# =============================================================================


def _get_cursor_pos() -> tuple[int, int]:
    p = wintypes.POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(p))
    return p.x, p.y


def _screen_to_client(hwnd: int, x: int, y: int) -> tuple[int, int]:
    p = wintypes.POINT(x, y)
    ctypes.windll.user32.ScreenToClient(hwnd, ctypes.byref(p))
    return p.x, p.y


# =============================================================================
# 数据 & 信号桥
# =============================================================================


@dataclass
class PickRecord:
    nx: float
    ny: float
    r: int
    g: int
    b: int


@dataclass
class PickRect:
    """矩形选区记录 (rect_mode 下使用).

    四个值都是归一化坐标 [0,1], 已保证 nx1 <= nx2 / ny1 <= ny2.
    """

    nx1: float
    ny1: float
    nx2: float
    ny2: float

    @property
    def width_norm(self) -> float:
        return self.nx2 - self.nx1

    @property
    def height_norm(self) -> float:
        return self.ny2 - self.ny1


class _HotkeyBridge(QObject):
    """pynput 监听线程 → Qt 主线程的信号桥"""

    triggered = Signal()


# =============================================================================
# 主对话框
# =============================================================================

DEFAULT_HOTKEY = "<f8>"


class PositionPickerDialog(QDialog):
    """
    取位置工具对话框。

    使用（选择模式）:
        dlg = PositionPickerDialog(mumu, selection_mode=True, expected_count=2,
                                    selection_labels=["图标中心", "跳转按钮"])
        if dlg.exec() == QDialog.Accepted:
            records = dlg.result_records()
    """

    def __init__(
        self,
        mumu: Mumu,
        parent=None,
        hotkey: str = DEFAULT_HOTKEY,
        selection_mode: bool = False,
        expected_count: int = 0,
        selection_labels: Optional[list[str]] = None,
    ) -> None:
        super().__init__(parent, Qt.Dialog | Qt.WindowStaysOnTopHint)
        self.setWindowTitle("取位置工具")
        self.resize(680, 480)

        self._mumu = mumu
        self._records: list[PickRecord] = []
        self._listener = None  # pynput GlobalHotKeys
        self._hotkey = hotkey

        self._selection_mode = selection_mode
        self._expected_count = expected_count
        self._selection_labels = selection_labels or []

        self._bridge = _HotkeyBridge()
        self._bridge.triggered.connect(self._on_pick)  # 在主线程执行

        # 截图选点子窗（lazy 创建，复用）
        self._zoom_picker: Optional[ZoomedSnapshotPicker] = None

        # 用于 showEvent 默认启动监听的 flag (避免 hide/show 多次重复触发)
        self._auto_listener_started = False

        self._build_ui()
        self._refresh_status()

    # ---------------- 对外 ----------------

    def result_records(self) -> list[PickRecord]:
        """选择模式下 exec() 返回后取结果"""
        return list(self._records)

    # ---------------- UI ----------------

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        # 顶部说明
        tip_text = (
            "两种采样方式（共存，记录写入同一张表）：\n"
            "  ① 实时模式：把鼠标悬停到 MuMu 画面目标位置，按快捷键\n"
            "  ② 截图选点：点「截图选点 (放大)」，在放大窗里精确点选"
        )
        if self._selection_mode:
            tip_text += (
                f"\n【选择模式】需要录入 {self._expected_count} 个点，"
                "按顺序记录后点「确认选择」。"
            )
        tip = QLabel(tip_text)
        tip.setWordWrap(True)
        layout.addWidget(tip)

        # 快捷键输入
        hk_row = QHBoxLayout()
        hk_row.addWidget(QLabel("快捷键:"))
        self._hotkey_edit = QLineEdit(self._hotkey)
        self._hotkey_edit.setPlaceholderText(
            "pynput 语法，例如 <f8> / <ctrl>+<shift>+c"
        )
        self._hotkey_edit.setMaximumWidth(240)
        hk_row.addWidget(self._hotkey_edit)
        hk_row.addStretch(1)
        layout.addLayout(hk_row)

        # 状态
        self._status_label = QLabel("")
        layout.addWidget(self._status_label)

        # 选择模式下：next label 高亮提示
        self._next_label = QLabel("")
        if self._selection_mode:
            self._next_label.setStyleSheet("font-weight: bold; color: #0066cc;")
            layout.addWidget(self._next_label)

        # 表格
        self._table = QTableWidget(0, 5 if self._selection_mode else 4)
        headers = ["#", "归一化 (x, y)", "RGB", "颜色"]
        if self._selection_mode:
            headers.insert(1, "用途")
        self._table.setHorizontalHeaderLabels(headers)
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionBehavior(QTableWidget.SelectRows)
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        if self._selection_mode:
            header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
            header.setSectionResizeMode(2, QHeaderView.Stretch)
            header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
            header.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        else:
            header.setSectionResizeMode(1, QHeaderView.Stretch)
            header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
            header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        layout.addWidget(self._table, 1)

        # 按钮栏
        btns = QHBoxLayout()
        self._btn_start = QPushButton("开始监听")
        self._btn_start.setCheckable(True)
        self._btn_start.toggled.connect(self._on_toggle_listener)
        btns.addWidget(self._btn_start)

        self._btn_zoom = QPushButton("截图选点 (放大)")
        self._btn_zoom.setToolTip(
            "截当前游戏画面一帧，弹出放大窗供精确点选；" "适合像素级目标 / 动态画面"
        )
        self._btn_zoom.clicked.connect(self._on_open_zoom_picker)
        btns.addWidget(self._btn_zoom)

        self._btn_undo = QPushButton("撤销最后一条")
        self._btn_undo.clicked.connect(self._on_undo)
        btns.addWidget(self._btn_undo)

        btn_clear = QPushButton("清空")
        btn_clear.clicked.connect(self._on_clear)
        btns.addWidget(btn_clear)

        if not self._selection_mode:
            btn_copy = QPushButton("复制选中 JSON")
            btn_copy.clicked.connect(self._on_copy_selected)
            btns.addWidget(btn_copy)

        btns.addStretch(1)

        if self._selection_mode:
            self._btn_confirm = QPushButton("确认选择")
            self._btn_confirm.setEnabled(False)
            self._btn_confirm.clicked.connect(self._on_confirm)
            btns.addWidget(self._btn_confirm)

            btn_cancel = QPushButton("取消")
            btn_cancel.clicked.connect(self.reject)
            btns.addWidget(btn_cancel)

        layout.addLayout(btns)

    def _refresh_status(self) -> None:
        parts = [
            f"render_wnd=0x{self._mumu.hwnd:X}",
            f"device={self._mumu.device_w}x{self._mumu.device_h}",
            f"监听={'ON' if self._listener else 'OFF'}",
            f"已记录={len(self._records)}",
        ]
        if self._selection_mode:
            parts.append(f"需要={self._expected_count}")
        self._status_label.setText(" | ".join(parts))

        if self._selection_mode:
            n = len(self._records)
            if n < self._expected_count:
                if n < len(self._selection_labels):
                    self._next_label.setText(
                        f"▶ 下一步: 录入 [{self._selection_labels[n]}]"
                    )
                else:
                    self._next_label.setText(
                        f"▶ 下一步: 录入第 {n + 1} / {self._expected_count} 个"
                    )
            else:
                self._next_label.setText(
                    f"✔ 已达到 {self._expected_count} 个，点「确认选择」提交。"
                )
            if hasattr(self, "_btn_confirm"):
                self._btn_confirm.setEnabled(n == self._expected_count)

        # 同步刷新放大窗状态栏（如果开着）
        if self._zoom_picker is not None and self._zoom_picker.isVisible():
            self._zoom_picker.set_record_count(
                len(self._records),
                self._expected_count if self._selection_mode else None,
            )

    # ---------------- 默认开启监听 ----------------

    def showEvent(self, ev) -> None:
        super().showEvent(ev)
        if not self._auto_listener_started:
            self._auto_listener_started = True
            # setChecked(True) 会触发 _on_toggle_listener(True),
            # 内部失败时按钮会自动复位并弹错
            self._btn_start.setChecked(True)

    # ---------------- 监听启停 ----------------

    def _on_toggle_listener(self, on: bool) -> None:
        if on:
            hk = self._hotkey_edit.text().strip() or DEFAULT_HOTKEY
            self._hotkey = hk
            if not self._start_listener(hk):
                self._btn_start.setChecked(False)
                return
            self._btn_start.setText(f"停止监听  [{hk}]")
            self._hotkey_edit.setEnabled(False)
        else:
            self._stop_listener()
            self._btn_start.setText("开始监听")
            self._hotkey_edit.setEnabled(True)
        self._refresh_status()

    def _start_listener(self, hotkey: str) -> bool:
        try:
            from pynput import keyboard as kb
        except ImportError:
            QMessageBox.critical(
                self,
                "缺少依赖",
                "需要 pynput 支持全局快捷键：\n    pip install pynput",
            )
            return False

        try:
            self._listener = kb.GlobalHotKeys(
                {hotkey: lambda: self._bridge.triggered.emit()}
            )
            self._listener.start()
        except Exception as e:
            log.exception("注册全局快捷键失败")
            QMessageBox.critical(
                self,
                "注册快捷键失败",
                f"{e}\n\n请检查快捷键语法（pynput 格式，如 <f8> 或 <ctrl>+<shift>+c）。",
            )
            self._listener = None
            return False
        return True

    def _stop_listener(self) -> None:
        if self._listener is not None:
            try:
                self._listener.stop()
            except Exception:
                log.exception("停止监听异常")
            self._listener = None

    # ---------------- 采样 (实时模式 + 截图模式共享) ----------------

    def _sample_at(
        self,
        nx: float,
        ny: float,
        image: Optional[Image.Image] = None,
    ) -> Optional[PickRecord]:
        """
        给定归一化坐标 (nx, ny)，返回 PickRecord (含该位置颜色)。

        image=None 时现截一帧；非 None 时复用传入的截图（截图模式下保证
        归一化坐标与采样像素来自同一帧）。归一化坐标越界返回 None。
        """
        if not (0 <= nx <= 1 and 0 <= ny <= 1):
            return None
        if image is None:
            image = self._mumu.capture_window()
        # 归一化坐标不受 Windows 客户区和 device/截图 像素尺寸差异影响
        px = min(image.size[0] - 1, max(0, int(nx * image.size[0])))
        py = min(image.size[1] - 1, max(0, int(ny * image.size[1])))
        pixel = image.getpixel((px, py))
        r, g, b = (int(pixel[0]), int(pixel[1]), int(pixel[2]))
        return PickRecord(nx=nx, ny=ny, r=r, g=g, b=b)

    def _sample_at_cursor(self) -> Optional[PickRecord]:
        """从当前鼠标位置采样 (实时模式入口)"""
        hwnd = self._mumu.hwnd
        cw, ch = get_client_size_logical(hwnd)
        if cw <= 0 or ch <= 0:
            return None
        mx, my = _get_cursor_pos()
        cx, cy = _screen_to_client(hwnd, mx, my)
        if not (0 <= cx < cw and 0 <= cy < ch):
            return None
        return self._sample_at(cx / cw, cy / ch)

    def _add_record(self, rec: PickRecord) -> None:
        """加一条记录,刷新表 + 状态"""
        self._records.append(rec)
        self._append_row(rec)
        log.info(
            "记录 #%d: norm=(%.4f, %.4f) RGB=(%d,%d,%d)",
            len(self._records),
            rec.nx,
            rec.ny,
            rec.r,
            rec.g,
            rec.b,
        )
        self._refresh_status()

    def _on_pick(self) -> None:
        """实时模式：被快捷键 signal 唤起，从当前鼠标位置采样"""
        if self._selection_mode and len(self._records) >= self._expected_count:
            self._status_label.setText(
                "已达到期望数量，多余采样忽略。如需修改请先撤销或清空。"
            )
            return
        try:
            rec = self._sample_at_cursor()
        except Exception:
            log.exception("采样失败")
            self._status_label.setText("采样失败，详见日志")
            return
        if rec is None:
            self._status_label.setText("鼠标不在 MuMu 游戏画面内（忽略）")
            return
        self._add_record(rec)

    # ---------------- 截图选点模式 ----------------

    def _on_open_zoom_picker(self) -> None:
        if self._selection_mode and len(self._records) >= self._expected_count:
            self._status_label.setText(
                "已达到期望数量，多余采样忽略。如需修改请先撤销或清空。"
            )
            return
        try:
            img = self._mumu.capture_window()
        except Exception as e:
            log.exception("截图失败")
            QMessageBox.critical(self, "截图失败", f"{type(e).__name__}: {e}")
            return

        if self._zoom_picker is None:
            self._zoom_picker = ZoomedSnapshotPicker(img, self._mumu, parent=self)
            self._zoom_picker.point_picked.connect(self._on_zoom_picked)
        else:
            self._zoom_picker.set_image(img)

        self._zoom_picker.set_record_count(
            len(self._records),
            self._expected_count if self._selection_mode else None,
        )
        self._zoom_picker.show()
        self._zoom_picker.raise_()
        self._zoom_picker.activateWindow()

    def _on_zoom_picked(self, nx: float, ny: float) -> None:
        """放大窗里点了一下,走采样链路 (用放大窗当前的同一帧图)"""
        if self._selection_mode and len(self._records) >= self._expected_count:
            return  # 静默忽略，避免 spam
        if self._zoom_picker is None:
            return
        img = self._zoom_picker.current_image()
        try:
            rec = self._sample_at(nx, ny, image=img)
        except Exception:
            log.exception("截图模式采样失败")
            self._status_label.setText("采样失败,详见日志")
            return
        if rec is None:
            return
        self._add_record(rec)

    # ---------------- 表格 ----------------

    def _append_row(self, rec: PickRecord) -> None:
        row = self._table.rowCount()
        self._table.insertRow(row)
        col = 0
        self._table.setItem(row, col, QTableWidgetItem(str(row + 1)))
        col += 1
        if self._selection_mode:
            label = (
                self._selection_labels[row]
                if row < len(self._selection_labels)
                else f"#{row + 1}"
            )
            self._table.setItem(row, col, QTableWidgetItem(label))
            col += 1
        self._table.setItem(row, col, QTableWidgetItem(f"({rec.nx:.4f}, {rec.ny:.4f})"))
        col += 1
        self._table.setItem(row, col, QTableWidgetItem(f"{rec.r}, {rec.g}, {rec.b}"))
        col += 1
        swatch = QTableWidgetItem("")
        swatch.setBackground(QColor(rec.r, rec.g, rec.b))
        self._table.setItem(row, col, swatch)

    def _on_undo(self) -> None:
        if not self._records:
            return
        self._records.pop()
        self._table.removeRow(self._table.rowCount() - 1)
        self._refresh_status()

    def _on_clear(self) -> None:
        self._records.clear()
        self._table.setRowCount(0)
        self._refresh_status()

    def _on_confirm(self) -> None:
        if len(self._records) != self._expected_count:
            QMessageBox.warning(
                self,
                "数量不符",
                f"当前记录 {len(self._records)} 条，需要 {self._expected_count} 条。",
            )
            return
        self.accept()

    def _on_copy_selected(self) -> None:
        rows = sorted({idx.row() for idx in self._table.selectedIndexes()})
        if not rows:
            QMessageBox.information(self, "提示", "请先在表格中选中若干行。")
            return
        payload = [
            {
                "pos": [round(self._records[r].nx, 4), round(self._records[r].ny, 4)],
                "color": [self._records[r].r, self._records[r].g, self._records[r].b],
            }
            for r in rows
        ]
        text = json.dumps(
            payload if len(payload) > 1 else payload[0],
            ensure_ascii=False,
            indent=2,
        )
        QGuiApplication.clipboard().setText(text)
        self._status_label.setText(f"已复制 {len(rows)} 条到剪贴板")

    # ---------------- 关闭 ----------------

    def closeEvent(self, ev) -> None:
        if self._zoom_picker is not None:
            self._zoom_picker.close()
        self._stop_listener()
        if self._btn_start.isChecked():
            self._btn_start.setChecked(False)
        super().closeEvent(ev)


# =============================================================================
# 截图选点 (放大) 子窗
# =============================================================================


class _ZoomImageLabel(QLabel):
    """承载放大后的截图,支持鼠标 hover (实时显示像素色) + click (采样).

    支持三种交互模式 (set_mode 切换, 默认 'point'):
        'point'    左键单击采样 → emit clicked(x_disp, y_disp), 累积红色 markers
        'rect'     左键 press→drag→release → emit rect_picked(x1,y1,x2,y2)
                   (widget 像素坐标, 已规范化), 落定矩形以红色边框累积绘制
        'readonly' 不响应左键; 仅 zoom + 右键 pan + hover. 用于"预览已配置 ROI"
                   这类场景, 配合 add_rect_marker() 预画标注.
    所有模式下右键拖动 pan 都可用.
    """

    clicked = Signal(int, int)
    hovered = Signal(int, int)
    # 拖框释放: widget 像素坐标 (x1, y1, x2, y2), 已保证 x1<x2 / y1<y2
    rect_picked = Signal(int, int, int, int)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setCursor(Qt.CrossCursor)
        # 标记位置以原图浮点像素坐标存储, 与放大倍率解耦;
        # 切换倍率时直接用 _zoom 反推 widget 坐标重绘, 不丢点。
        self._markers: list[tuple[float, float]] = []
        # 已落定的矩形 (原图浮点像素坐标), 切倍率时按 _zoom 反推 widget 坐标
        self._rect_markers: list[tuple[float, float, float, float]] = []
        self._zoom: int = 1

        # 右键拖动 pan 状态 (widget 1:1, 各倍率下手感恒定)
        self._scroll_area: Optional["QScrollArea"] = None
        self._panning: bool = False
        self._pan_anchor_global = None  # 鼠标 press 时屏幕坐标
        self._pan_anchor_scroll = (0, 0)  # 鼠标 press 时 scrollbar 值

        # 矩形拖动状态
        self._mode: str = "point"  # 'point' | 'rect' | 'readonly'
        self._rect_dragging: bool = False
        self._rect_press: Optional[tuple[int, int]] = None  # widget 坐标
        self._rect_curr: Optional[tuple[int, int]] = None  # widget 坐标

    def attach_scroll_area(self, scroll: "QScrollArea") -> None:
        """让 label 知道它所在的 QScrollArea, 用于实现右键拖动 pan"""
        self._scroll_area = scroll

    def set_zoom(self, zoom: int) -> None:
        """
        同步当前放大倍率给 paintEvent 用 (把原图坐标映射回 widget 坐标)。
        不主动 update, 由调用方通过 setPixmap 等触发统一刷新, 避免重复重绘。
        """
        self._zoom = max(1, int(zoom))

    def set_mode(self, mode: str) -> None:
        """切换交互模式. 'point'/'rect'/'readonly'."""
        if mode not in ("point", "rect", "readonly"):
            raise ValueError(f"未知 mode: {mode!r}, 必须是 point/rect/readonly")
        self._mode = mode

    def add_rect_marker(self, x1: float, y1: float, x2: float, y2: float) -> None:
        """外部往图上加一个矩形标注 (原图浮点像素坐标). 给 readonly 预览用."""
        xa, xb = (x1, x2) if x1 <= x2 else (x2, x1)
        ya, yb = (y1, y2) if y1 <= y2 else (y2, y1)
        self._rect_markers.append((xa, ya, xb, yb))
        self.update()

    def clear_markers(self) -> None:
        self._markers.clear()
        self._rect_markers.clear()
        self._rect_press = None
        self._rect_curr = None
        self._rect_dragging = False
        self.update()

    def mousePressEvent(self, ev: QMouseEvent) -> None:
        if ev.button() == Qt.RightButton and self._scroll_area is not None:
            # 进入右键拖动 pan 模式: 记下屏幕坐标 + 当前 scrollbar 值,
            # mouseMoveEvent 用 delta 实时滚动。widget 1:1, 各倍率下手感恒定。
            self._panning = True
            self._pan_anchor_global = ev.globalPosition().toPoint()
            sb_h = self._scroll_area.horizontalScrollBar()
            sb_v = self._scroll_area.verticalScrollBar()
            self._pan_anchor_scroll = (sb_h.value(), sb_v.value())
            self.setCursor(Qt.ClosedHandCursor)
            ev.accept()
            return
        if ev.button() == Qt.LeftButton:
            if self._mode == "readonly":
                return  # readonly 不响应左键
            x_disp = int(ev.position().x())
            y_disp = int(ev.position().y())
            if self._mode == "rect":
                # 矩形模式: 进入拖动状态, 不立刻 emit
                self._rect_dragging = True
                self._rect_press = (x_disp, y_disp)
                self._rect_curr = (x_disp, y_disp)
                self.update()
                ev.accept()
                return
            # point 模式 (原行为): 存原图浮点像素坐标 + emit clicked
            z = max(1, self._zoom)
            self._markers.append((x_disp / z, y_disp / z))
            self.clicked.emit(x_disp, y_disp)
            self.update()

    def mouseMoveEvent(self, ev: QMouseEvent) -> None:
        if self._panning and self._scroll_area is not None:
            # 用屏幕绝对坐标 (globalPosition) 算 delta, 不能用相对 widget 的
            # position(): 滚动时 widget 内容会移, 鼠标静止时 position 也会变,
            # 形成反馈循环。globalPosition 才是纯鼠标位移。
            cur = ev.globalPosition().toPoint()
            dx = cur.x() - self._pan_anchor_global.x()
            dy = cur.y() - self._pan_anchor_global.y()
            sb_h = self._scroll_area.horizontalScrollBar()
            sb_v = self._scroll_area.verticalScrollBar()
            sb_h.setValue(self._pan_anchor_scroll[0] - dx)
            sb_v.setValue(self._pan_anchor_scroll[1] - dy)
            ev.accept()
            return
        if self._rect_dragging:
            # 矩形拖动中: 更新当前点, 触发预览矩形重绘
            self._rect_curr = (int(ev.position().x()), int(ev.position().y()))
            self.update()
            return
        # 非 pan / 非 rect-drag 模式: emit hovered 给状态栏更新像素色信息
        x = int(ev.position().x())
        y = int(ev.position().y())
        self.hovered.emit(x, y)

    def mouseReleaseEvent(self, ev: QMouseEvent) -> None:
        if ev.button() == Qt.RightButton and self._panning:
            self._panning = False
            self._pan_anchor_global = None
            self.setCursor(Qt.CrossCursor)
            ev.accept()
            return
        if ev.button() == Qt.LeftButton and self._rect_dragging:
            # 完成矩形框选
            x0, y0 = self._rect_press
            x1 = int(ev.position().x())
            y1 = int(ev.position().y())
            self._rect_dragging = False
            self._rect_press = None
            self._rect_curr = None
            xa, xb = (x0, x1) if x0 <= x1 else (x1, x0)
            ya, yb = (y0, y1) if y0 <= y1 else (y1, y0)
            # 太小的框忽略 (<= 4 px), 视为误点
            if (xb - xa) < 4 or (yb - ya) < 4:
                self.update()
                ev.accept()
                return
            z = max(1, self._zoom)
            self._rect_markers.append((xa / z, ya / z, xb / z, yb / z))
            self.rect_picked.emit(xa, ya, xb, yb)
            self.update()
            ev.accept()
            return
        super().mouseReleaseEvent(ev)

    def paintEvent(self, ev) -> None:
        super().paintEvent(ev)
        if not (self._markers or self._rect_markers or self._rect_dragging):
            return
        painter = QPainter(self)
        z = max(1, self._zoom)

        # 已落定矩形 (红色边框)
        if self._rect_markers:
            pen = QPen(QColor(255, 60, 60), 2, Qt.SolidLine)
            painter.setPen(pen)
            for i, (rx0, ry0, rx1, ry1) in enumerate(self._rect_markers):
                wx0 = int(round(rx0 * z))
                wy0 = int(round(ry0 * z))
                wx1 = int(round(rx1 * z))
                wy1 = int(round(ry1 * z))
                painter.drawRect(wx0, wy0, wx1 - wx0, wy1 - wy0)
                # 多个矩形累积时编号
                if len(self._rect_markers) > 1:
                    painter.drawText(wx0 + 4, wy0 + 16, f"#{i + 1}")

        # 正在拖动的预览矩形 (蓝色虚线)
        if self._rect_dragging and self._rect_press and self._rect_curr:
            x0, y0 = self._rect_press
            x1, y1 = self._rect_curr
            xa, xb = (x0, x1) if x0 <= x1 else (x1, x0)
            ya, yb = (y0, y1) if y0 <= y1 else (y1, y0)
            pen = QPen(QColor(50, 130, 230), 2, Qt.DashLine)
            painter.setPen(pen)
            painter.drawRect(xa, ya, xb - xa, yb - ya)

        # 点 marker (原行为不变)
        if self._markers:
            pen = QPen(QColor(255, 40, 40), 2)
            painter.setPen(pen)
            # 半径随倍率适度增长: 1× 时 14, 6× 时 24, 大倍率下圈不会被像素淹没
            radius = 12 + 2 * z
            cross_gap = 3
            cross_arm = radius // 2 + 2
            for ox, oy in self._markers:
                cx = int(round(ox * z))
                cy = int(round(oy * z))
                painter.drawEllipse(cx - radius, cy - radius, 2 * radius, 2 * radius)
                # 中心十字 (留空隙不挡像素本身)
                painter.drawLine(cx - cross_arm, cy, cx - cross_gap, cy)
                painter.drawLine(cx + cross_gap, cy, cx + cross_arm, cy)
                painter.drawLine(cx, cy - cross_arm, cx, cy - cross_gap)
                painter.drawLine(cx, cy + cross_gap, cx, cy + cross_arm)


class ZoomedSnapshotPicker(QDialog):
    """
    放大截图 + 鼠标点选 子对话框 (放大/平移/marker 保留/中心锚点缩放)。

    支持三种交互模式 (mode 构造参数, 默认 'point'):
      · 'point'    左键单击采样, emit point_picked(nx, ny). 非模态用法不变.
      · 'rect'     左键拖框, emit rect_picked(nx1, ny1, nx2, ny2).
                   也可用 exec_for_rect() 一次性 modal 取一个矩形并返回.
      · 'readonly' 不响应左键; 仅 zoom + 右键 pan + hover. 用于"预览已配置好的
                   ROI/坐标"这类只读浏览场景. 调用方可用 add_rect_marker() 等
                   在图上预先画好标注.

    可定制项 (供不同上下文复用):
      · show_recapture: True 时显示"重新截图"按钮; 反解工具等需要外部统一截图
        +OCR 的场景应传 False, 避免子窗换帧后外部数据 (如 OCR 结果) 过期。
      · prompt: 默认提示文本 (鼠标 hover 时被像素色信息覆盖, 离开图后恢复)。
    """

    point_picked = Signal(float, float)  # 归一化坐标 (nx, ny) ∈ [0, 1]²
    rect_picked = Signal(float, float, float, float)  # (nx1, ny1, nx2, ny2)

    # 缩放倍率范围 (1822×1058 在 6× 时约 277MB QPixmap，再大就吃内存)
    ZOOM_MIN = 1
    ZOOM_MAX = 6
    ZOOM_DEFAULT = 2

    DEFAULT_PROMPT = "左键单击采样 | 右键拖动平移 | 滚轮垂直滚动 | 鼠标移到图上看像素色"
    RECT_PROMPT = "左键拖出矩形采样 | 右键拖动平移 | 滚轮垂直滚动"
    READONLY_PROMPT = "右键拖动平移 | 滚轮垂直滚动 | 鼠标移到图上看像素色"

    def __init__(
        self,
        img: Image.Image,
        mumu: Mumu,
        parent=None,
        *,
        show_recapture: bool = True,
        prompt: Optional[str] = None,
        mode: str = "point",
    ) -> None:
        super().__init__(parent, Qt.Dialog)
        if mode not in ("point", "rect", "readonly"):
            raise ValueError(f"未知 mode: {mode!r}")
        self.setWindowTitle("截图选点 - 放大查看")
        self.resize(960, 720)

        self._mumu = mumu
        self._orig_image: Image.Image = img
        self._zoom: int = self.ZOOM_DEFAULT
        self._show_recapture = show_recapture
        self._mode = mode
        if prompt is not None:
            self._default_prompt = prompt
        elif mode == "rect":
            self._default_prompt = self.RECT_PROMPT
        elif mode == "readonly":
            self._default_prompt = self.READONLY_PROMPT
        else:
            self._default_prompt = self.DEFAULT_PROMPT

        # exec_for_rect() 用: 拖完一个矩形后存起来, 然后 accept() 关闭对话框
        self._captured_rect: Optional[tuple[float, float, float, float]] = None

        self._build_ui()
        # 子部件创建后, 把 mode 同步给 label
        self._img_label.set_mode(mode)
        self._refresh_image()

    # ---------------- 对外 ----------------

    def current_image(self) -> Image.Image:
        return self._orig_image

    def set_image(self, img: Image.Image) -> None:
        self._orig_image = img
        # 换图后旧标记位置语义上不再对应新画面, 清除避免误导
        self._img_label.clear_markers()
        self._refresh_image()

    def set_prompt(self, text: str) -> None:
        """更新底部状态栏的默认提示 (鼠标离开图时显示的文本)。"""
        self._default_prompt = text
        self._info_label.setText(text)

    def set_record_count(self, count: int, expected: Optional[int]) -> None:
        if expected is None:
            self._record_label.setText(f"已记录: {count}")
        else:
            done = "  ✔ 已达上限" if count >= expected else ""
            self._record_label.setText(f"已记录: {count} / {expected}{done}")

    def add_rect_marker(self, nx1: float, ny1: float, nx2: float, ny2: float) -> None:
        """在图上预先画一个矩形标记 (归一化坐标). 用于 readonly 预览场景."""
        w, h = self._orig_image.size
        self._img_label.add_rect_marker(nx1 * w, ny1 * h, nx2 * w, ny2 * h)

    def clear_markers(self) -> None:
        """外部清空所有 markers (点 + 矩形)."""
        self._img_label.clear_markers()

    def captured_rect_norm(
        self,
    ) -> Optional[tuple[float, float, float, float]]:
        """exec_for_rect() 用. 返回 None 表示用户没拖框就关掉了."""
        return self._captured_rect

    def exec_for_rect(self) -> Optional[tuple[float, float, float, float]]:
        """便利方法: 以 modal 方式打开, 等用户拖完一个矩形后自动关闭, 返回归一化矩形.

        用户没拖框就关掉对话框 → 返回 None.
        只能在 mode='rect' 下调用.
        """
        if self._mode != "rect":
            raise RuntimeError("exec_for_rect 只能在 mode='rect' 下调用")
        # 在拖框释放时立刻 accept
        self.rect_picked.connect(self._on_rect_picked_for_modal)
        self.exec()
        return self._captured_rect

    def _on_rect_picked_for_modal(
        self, nx1: float, ny1: float, nx2: float, ny2: float
    ) -> None:
        self._captured_rect = (nx1, ny1, nx2, ny2)
        self.accept()

    # ---------------- UI ----------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        # 顶部: 倍率 + (可选) 重新截图 + 已记录数
        top = QHBoxLayout()
        top.addWidget(QLabel("放大倍率:"))
        self._zoom_spin = QSpinBox()
        self._zoom_spin.setRange(self.ZOOM_MIN, self.ZOOM_MAX)
        self._zoom_spin.setValue(self._zoom)
        self._zoom_spin.setSuffix(" ×")
        self._zoom_spin.valueChanged.connect(self._on_zoom_changed)
        top.addWidget(self._zoom_spin)

        if self._show_recapture:
            top.addSpacing(16)
            btn_recap = QPushButton("重新截图")
            btn_recap.setToolTip("截当前游戏画面的最新一帧替换图")
            btn_recap.clicked.connect(self._on_recapture)
            top.addWidget(btn_recap)

        top.addStretch(1)

        self._record_label = QLabel("已记录: 0")
        self._record_label.setStyleSheet("color: #555;")
        top.addWidget(self._record_label)
        root.addLayout(top)

        # 中间: 滚动视图 + 放大图 (NEAREST 插值保持像素硬边缘)
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(False)  # 我们手控 label size
        self._img_label = _ZoomImageLabel()
        self._img_label.attach_scroll_area(self._scroll)  # 让 label 能驱动 pan
        self._img_label.clicked.connect(self._on_label_clicked)
        self._img_label.hovered.connect(self._on_label_hovered)
        # rect_picked: widget 像素 (x1,y1,x2,y2) → 归一化 → emit 自身的 rect_picked,
        # 屏蔽底层细节
        self._img_label.rect_picked.connect(self._on_label_rect_picked)
        self._scroll.setWidget(self._img_label)
        root.addWidget(self._scroll, 1)

        # 底部: 鼠标位置 + 颜色信息 (默认显示 prompt; hover 时显示像素色)
        self._info_label = QLabel(self._default_prompt)
        self._info_label.setStyleSheet("font-family: monospace; color: #444;")
        root.addWidget(self._info_label)

        # 关闭按钮
        bot = QHBoxLayout()
        bot.addStretch(1)
        btn_close = QPushButton("关闭")
        btn_close.clicked.connect(self.close)
        bot.addWidget(btn_close)
        root.addLayout(bot)

    # ---------------- 渲染 ----------------

    def _on_zoom_changed(self, value: int) -> None:
        new_zoom = int(value)
        if new_zoom == self._zoom:
            return

        # ---- 切换前: 算视图中心对应的原图像素坐标 ----
        # 切换 zoom 时希望"视图中心看到的内容"保持不变, 这样放大像 zoom-in
        # 一个特定区域而不是把图整个重新展示。
        sb_h = self._scroll.horizontalScrollBar()
        sb_v = self._scroll.verticalScrollBar()
        viewport = self._scroll.viewport()
        vw = viewport.width()
        vh = viewport.height()
        old_zoom = max(1, self._zoom)
        center_orig_x = (sb_h.value() + vw / 2) / old_zoom
        center_orig_y = (sb_v.value() + vh / 2) / old_zoom

        # ---- 切换 (widget size 同步变化, scrollbar 范围立即更新) ----
        self._zoom = new_zoom
        # 标记位置存的是原图坐标, 切倍率不需要清; _refresh_image 同步 zoom +
        # setPixmap, 触发 paintEvent 按新倍率重画圈
        self._refresh_image()

        # ---- 切换后: 把同一原图坐标重新放到视图中心 ----
        # 缩小到 widget < viewport 时 setValue 会被 clamp 到 0, 整图居中显示;
        # 这就是"缩小尽量保持"的兜底语义。
        new_widget_cx = center_orig_x * new_zoom
        new_widget_cy = center_orig_y * new_zoom
        sb_h.setValue(int(round(new_widget_cx - vw / 2)))
        sb_v.setValue(int(round(new_widget_cy - vh / 2)))

    def _refresh_image(self) -> None:
        # 先同步 zoom 给 label, paintEvent 用它把原图坐标映射回 widget
        self._img_label.set_zoom(self._zoom)
        w, h = self._orig_image.size
        scaled = self._orig_image.resize(
            (w * self._zoom, h * self._zoom),
            Image.Resampling.NEAREST,
        )
        qimg = ImageQt.ImageQt(scaled.convert("RGB"))
        pixmap = QPixmap.fromImage(qimg)
        self._img_label.setPixmap(pixmap)  # 触发 paintEvent, markers 按新 zoom 重画
        self._img_label.setFixedSize(pixmap.size())

    def _on_recapture(self) -> None:
        try:
            img = self._mumu.capture_window()
        except Exception as e:
            log.exception("重新截图失败")
            QMessageBox.critical(self, "截图失败", f"{type(e).__name__}: {e}")
            return
        self.set_image(img)
        # 短暂提示替换成功; 鼠标移上去会立刻被像素色覆盖
        self._info_label.setText("已替换为最新一帧。 " + self._default_prompt)

    # ---------------- 交互 ----------------

    def _on_label_clicked(self, x_disp: int, y_disp: int) -> None:
        # widget 像素 → 原图像素 → 归一化
        ow, oh = self._orig_image.size
        # 用 float 保留精度，最后才归一化
        ox = x_disp / self._zoom
        oy = y_disp / self._zoom
        nx = ox / ow
        ny = oy / oh
        if not (0 <= nx <= 1 and 0 <= ny <= 1):
            return
        log.info(
            "放大窗采样: 显示像素(%d,%d) zoom=%d → 原图(%.1f,%.1f) → 归一化(%.4f,%.4f)",
            x_disp,
            y_disp,
            self._zoom,
            ox,
            oy,
            nx,
            ny,
        )
        self.point_picked.emit(nx, ny)

    def _on_label_rect_picked(self, x1: int, y1: int, x2: int, y2: int) -> None:
        # widget 像素 → 原图像素 → 归一化 (x1<x2 / y1<y2 由底层保证)
        ow, oh = self._orig_image.size
        nx1 = (x1 / self._zoom) / ow
        ny1 = (y1 / self._zoom) / oh
        nx2 = (x2 / self._zoom) / ow
        ny2 = (y2 / self._zoom) / oh
        # 夹紧到 [0,1] (放大后 widget 边缘可能超出图片)
        nx1 = max(0.0, min(1.0, nx1))
        ny1 = max(0.0, min(1.0, ny1))
        nx2 = max(0.0, min(1.0, nx2))
        ny2 = max(0.0, min(1.0, ny2))
        log.info(
            "放大窗框选: zoom=%d → 归一化矩形 (%.4f,%.4f)~(%.4f,%.4f) 大小 %.4f×%.4f",
            self._zoom,
            nx1,
            ny1,
            nx2,
            ny2,
            nx2 - nx1,
            ny2 - ny1,
        )
        self.rect_picked.emit(nx1, ny1, nx2, ny2)

    def _on_label_hovered(self, x_disp: int, y_disp: int) -> None:
        ow, oh = self._orig_image.size
        ox = int(x_disp / self._zoom)
        oy = int(y_disp / self._zoom)
        if not (0 <= ox < ow and 0 <= oy < oh):
            # 鼠标在 widget 内但落在原图外 (放大后 widget 边缘可能超出原图坐标),
            # 直接恢复默认 prompt, 避免"鼠标不在图内"打断阅读
            self._info_label.setText(self._default_prompt)
            return
        pixel = self._orig_image.getpixel((ox, oy))
        r, g, b = int(pixel[0]), int(pixel[1]), int(pixel[2])
        nx = ox / ow
        ny = oy / oh
        self._info_label.setText(
            f"原图像素 ({ox}, {oy})    归一化 ({nx:.4f}, {ny:.4f})    "
            f"RGB ({r:3d}, {g:3d}, {b:3d})    #{r:02X}{g:02X}{b:02X}"
        )
