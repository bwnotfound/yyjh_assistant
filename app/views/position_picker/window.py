"""
取位置工具 - 全局快捷键触发，记录鼠标在 MuMu render_wnd 客户区内的归一化坐标 + 颜色。

坐标系:
    - 鼠标屏幕坐标 → Win32 ScreenToClient(render_wnd) → 客户区像素 → 除以客户区尺寸归一化
    - 所有坐标调用统一走 Win32 API（GetCursorPos / ScreenToClient / GetClientRect），
      确保和 render_wnd 本身在同一套坐标系下，不受 Qt/进程 DPI awareness 影响。
"""

from __future__ import annotations

import ctypes
import json
import logging
from ctypes import wintypes
from dataclasses import dataclass
from typing import Optional

from PySide6.QtCore import Qt, QObject, Signal
from PySide6.QtGui import QColor, QGuiApplication
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
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


class _HotkeyBridge(QObject):
    """pynput 监听线程 → Qt 主线程的信号桥"""

    triggered = Signal()


# =============================================================================
# 对话框
# =============================================================================

DEFAULT_HOTKEY = "<f8>"


class PositionPickerDialog(QDialog):
    """
    取位置工具对话框。

    使用流程:
      1. 点"开始监听"
      2. 将鼠标移到 MuMu 窗口内的目标位置
      3. 按快捷键（默认 F8）记录一条：归一化坐标 + 像素 RGB
      4. 在表格选中若干行，"复制选中 JSON" 得到可粘贴的片段
    """

    def __init__(
        self,
        mumu: Mumu,
        parent=None,
        hotkey: str = DEFAULT_HOTKEY,
    ) -> None:
        super().__init__(parent, Qt.Dialog | Qt.WindowStaysOnTopHint)
        self.setWindowTitle("取位置工具")
        self.resize(680, 460)

        self._mumu = mumu
        self._records: list[PickRecord] = []
        self._listener = None  # pynput GlobalHotKeys
        self._hotkey = hotkey

        self._bridge = _HotkeyBridge()
        self._bridge.triggered.connect(self._on_pick)  # 在主线程执行

        self._build_ui()
        self._refresh_status()

    # ---------------- UI ----------------

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        # 顶部说明
        tip = QLabel(
            "操作：① 点「开始监听」 ② 把鼠标悬停到 MuMu 游戏画面上的目标位置 "
            "③ 按快捷键记录一条。"
        )
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

        # 表格
        self._table = QTableWidget(0, 4)
        self._table.setHorizontalHeaderLabels(["#", "归一化 (x, y)", "RGB", "颜色"])
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionBehavior(QTableWidget.SelectRows)
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
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

        btn_clear = QPushButton("清空")
        btn_clear.clicked.connect(self._on_clear)
        btns.addWidget(btn_clear)

        btn_copy = QPushButton("复制选中 JSON")
        btn_copy.clicked.connect(self._on_copy_selected)
        btns.addWidget(btn_copy)

        btns.addStretch(1)
        layout.addLayout(btns)

    def _refresh_status(self) -> None:
        parts = [
            f"render_wnd=0x{self._mumu.hwnd:X}",
            f"device={self._mumu.device_w}x{self._mumu.device_h}",
            f"监听={'ON' if self._listener else 'OFF'}",
            f"已记录={len(self._records)}",
        ]
        self._status_label.setText(" | ".join(parts))

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

    # ---------------- 采样 ----------------

    def _on_pick(self) -> None:
        """在主线程被 signal 唤起，完成一次采样"""
        try:
            rec = self._sample_once()
        except Exception:
            log.exception("采样失败")
            self._status_label.setText("采样失败，详见日志")
            return
        if rec is None:
            self._status_label.setText("鼠标不在 MuMu 游戏画面内（忽略）")
            return
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

    def _sample_once(self) -> Optional[PickRecord]:
        hwnd = self._mumu.hwnd
        cw, ch = get_client_size_logical(hwnd)
        if cw <= 0 or ch <= 0:
            return None

        mx, my = _get_cursor_pos()
        cx, cy = _screen_to_client(hwnd, mx, my)
        if not (0 <= cx < cw and 0 <= cy < ch):
            return None

        nx = cx / cw
        ny = cy / ch

        # 归一化坐标不受 Windows 客户区和 device/截图 像素尺寸差异影响
        img = self._mumu.capture_window()
        px = min(img.size[0] - 1, max(0, int(nx * img.size[0])))
        py = min(img.size[1] - 1, max(0, int(ny * img.size[1])))
        pixel = img.getpixel((px, py))
        r, g, b = (int(pixel[0]), int(pixel[1]), int(pixel[2]))
        return PickRecord(nx=nx, ny=ny, r=r, g=g, b=b)

    # ---------------- 表格 ----------------

    def _append_row(self, rec: PickRecord) -> None:
        row = self._table.rowCount()
        self._table.insertRow(row)
        self._table.setItem(row, 0, QTableWidgetItem(str(row + 1)))
        self._table.setItem(row, 1, QTableWidgetItem(f"({rec.nx:.4f}, {rec.ny:.4f})"))
        self._table.setItem(row, 2, QTableWidgetItem(f"{rec.r}, {rec.g}, {rec.b}"))
        swatch = QTableWidgetItem("")
        swatch.setBackground(QColor(rec.r, rec.g, rec.b))
        self._table.setItem(row, 3, swatch)

    def _on_clear(self) -> None:
        self._records.clear()
        self._table.setRowCount(0)
        self._refresh_status()

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
        self._stop_listener()
        if self._btn_start.isChecked():
            self._btn_start.setChecked(False)
        super().closeEvent(ev)
