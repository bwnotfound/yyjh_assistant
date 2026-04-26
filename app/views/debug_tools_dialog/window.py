"""
调试工具列表对话框 —— 调试相关子工具的统一入口。

子工具：
  - 取位置工具 (PositionPickerDialog)
  - 点击位置截图显示 (ClickPreviewDialog)
  - 反解可视区域 (ViewAreaSolverDialog)

每个子工具按需懒加载并复用，本对话框关闭时一并关闭子对话框。
"""

from __future__ import annotations

import logging
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QLabel,
    QPushButton,
    QVBoxLayout,
)

from utils import Mumu

from app.views.click_preview_dialog import ClickPreviewDialog
from app.views.position_picker import PositionPickerDialog
from app.views.view_area_solver_dialog import ViewAreaSolverDialog

log = logging.getLogger(__name__)


class DebugToolsDialog(QDialog):
    def __init__(self, mumu: Mumu, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("调试工具")
        self.resize(360, 300)

        self._mumu = mumu
        self._picker: Optional[PositionPickerDialog] = None
        self._click_preview: Optional[ClickPreviewDialog] = None
        self._view_solver: Optional[ViewAreaSolverDialog] = None

        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(12)

        hint = QLabel("选择要使用的调试工具：")
        layout.addWidget(hint)

        btn_picker = QPushButton("取位置工具")
        btn_picker.setMinimumHeight(40)
        btn_picker.setToolTip("用全局快捷键采样鼠标位置 + 颜色")
        btn_picker.clicked.connect(self._open_picker)
        layout.addWidget(btn_picker)

        btn_click = QPushButton("点击位置截图显示")
        btn_click.setMinimumHeight(40)
        btn_click.setToolTip(
            "输入绝对像素 / 归一化比例 / 格子数 offset，"
            "在游戏截图上以红圈标出对应屏幕位置"
        )
        btn_click.clicked.connect(self._open_click_preview)
        layout.addWidget(btn_click)

        btn_solver = QPushButton("反解可视区域 (view_area)")
        btn_solver.setMinimumHeight(40)
        btn_solver.setToolTip(
            "在 map_size / block_size / character_pos 已准的前提下，"
            "通过 OCR + 用户在截图上点击角色实际位置，反解 view_area 4 边界"
        )
        btn_solver.clicked.connect(self._open_view_solver)
        layout.addWidget(btn_solver)

        layout.addStretch(1)

    def _open_picker(self) -> None:
        if self._picker is None:
            self._picker = PositionPickerDialog(self._mumu, parent=self)
            self._picker.setAttribute(Qt.WA_DeleteOnClose, False)
        self._picker.show()
        self._picker.raise_()
        self._picker.activateWindow()

    def _open_click_preview(self) -> None:
        if self._click_preview is None:
            self._click_preview = ClickPreviewDialog(self._mumu, parent=self)
            self._click_preview.setAttribute(Qt.WA_DeleteOnClose, False)
        self._click_preview.show()
        self._click_preview.raise_()
        self._click_preview.activateWindow()

    def _open_view_solver(self) -> None:
        if self._view_solver is None:
            self._view_solver = ViewAreaSolverDialog(self._mumu, parent=self)
            self._view_solver.setAttribute(Qt.WA_DeleteOnClose, False)
        self._view_solver.show()
        self._view_solver.raise_()
        self._view_solver.activateWindow()

    def closeEvent(self, ev) -> None:
        for dlg in (self._picker, self._click_preview, self._view_solver):
            if dlg is not None:
                dlg.close()
        super().closeEvent(ev)
