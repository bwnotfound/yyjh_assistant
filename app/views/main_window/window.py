"""主窗口：懒加载 Mumu，提供功能入口按钮。"""

from __future__ import annotations

import logging
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from utils import Mumu, MumuError

from app.views.debug_tools_dialog import DebugToolsDialog
from app.views.map_registry_dialog import MapRegistryDialog
from app.views.movement_profile_dialog import MovementProfileDialog
from app.views.routine_editor_dialog import RoutineEditorDialog
from app.views.routine_runner_dialog import RoutineRunnerDialog

log = logging.getLogger(__name__)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("烟雨江湖助手")
        self.resize(420, 440)

        self._mumu: Optional[Mumu] = None
        self._debug_tools_dlg: Optional[DebugToolsDialog] = None
        self._map_registry_dlg: Optional[MapRegistryDialog] = None
        self._movement_profile_dlg: Optional[MovementProfileDialog] = None
        self._routine_editor_dlg: Optional[RoutineEditorDialog] = None
        self._runner_dlg: Optional[RoutineRunnerDialog] = None

        self._build_ui()

    # ---------------- UI ----------------

    def _build_ui(self) -> None:
        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(12)

        hint = QLabel("先启动 MuMu 并进入游戏，再点击下方按钮。")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        btn_debug = QPushButton("调试工具")
        btn_debug.setMinimumHeight(40)
        btn_debug.clicked.connect(self._open_debug_tools)
        layout.addWidget(btn_debug)

        btn_map = QPushButton("添加地图信息")
        btn_map.setMinimumHeight(40)
        btn_map.clicked.connect(self._open_map_registry)
        layout.addWidget(btn_map)

        btn_mov = QPushButton("运动配置")
        btn_mov.setMinimumHeight(40)
        btn_mov.clicked.connect(self._open_movement_profile)
        layout.addWidget(btn_mov)

        btn_edit_routine = QPushButton("编辑 Routine")
        btn_edit_routine.setMinimumHeight(40)
        btn_edit_routine.clicked.connect(self._open_routine_editor)
        layout.addWidget(btn_edit_routine)

        btn_run = QPushButton("执行 Routine")
        btn_run.setMinimumHeight(40)
        btn_run.clicked.connect(self._open_runner)
        layout.addWidget(btn_run)

        layout.addStretch(1)

        self.statusBar().showMessage("就绪")
        self.setCentralWidget(central)

    # ---------------- Mumu 懒加载 ----------------

    def get_mumu(self) -> Mumu:
        """首次调用时构造 Mumu 实例；后续复用。失败抛 MumuError。"""
        if self._mumu is None:
            log.info("初始化 Mumu 实例 ...")
            self._mumu = Mumu()
            log.info("Mumu 就绪")
        return self._mumu

    def _try_get_mumu(self) -> Optional[Mumu]:
        """统一的获取 + 错误弹窗"""
        try:
            return self.get_mumu()
        except MumuError as e:
            QMessageBox.critical(self, "MuMu 连接失败", str(e))
        except Exception as e:
            log.exception("Mumu 初始化异常")
            QMessageBox.critical(self, "MuMu 初始化异常", f"{type(e).__name__}: {e}")
        return None

    # ---------------- 功能入口 ----------------

    def _open_debug_tools(self) -> None:
        mumu = self._try_get_mumu()
        if mumu is None:
            return
        if self._debug_tools_dlg is None:
            self._debug_tools_dlg = DebugToolsDialog(mumu, parent=self)
            self._debug_tools_dlg.setAttribute(Qt.WA_DeleteOnClose, False)
        self._debug_tools_dlg.show()
        self._debug_tools_dlg.raise_()
        self._debug_tools_dlg.activateWindow()

    def _open_map_registry(self) -> None:
        mumu = self._try_get_mumu()
        if mumu is None:
            return
        if self._map_registry_dlg is None:
            self._map_registry_dlg = MapRegistryDialog(mumu, parent=self)
            self._map_registry_dlg.setAttribute(Qt.WA_DeleteOnClose, False)
        self._map_registry_dlg.show()
        self._map_registry_dlg.raise_()
        self._map_registry_dlg.activateWindow()

    def _open_movement_profile(self) -> None:
        mumu = self._try_get_mumu()
        if mumu is None:
            return
        if self._movement_profile_dlg is None:
            self._movement_profile_dlg = MovementProfileDialog(mumu, parent=self)
            self._movement_profile_dlg.setAttribute(Qt.WA_DeleteOnClose, False)
        self._movement_profile_dlg.show()
        self._movement_profile_dlg.raise_()
        self._movement_profile_dlg.activateWindow()

    def _open_routine_editor(self) -> None:
        mumu = self._try_get_mumu()
        if mumu is None:
            return
        if self._routine_editor_dlg is None:
            self._routine_editor_dlg = RoutineEditorDialog(mumu, parent=self)
            self._routine_editor_dlg.setAttribute(Qt.WA_DeleteOnClose, False)
        self._routine_editor_dlg.show()
        self._routine_editor_dlg.raise_()
        self._routine_editor_dlg.activateWindow()

    def _open_runner(self) -> None:
        mumu = self._try_get_mumu()
        if mumu is None:
            return
        if self._runner_dlg is None:
            self._runner_dlg = RoutineRunnerDialog(mumu, parent=self)
            self._runner_dlg.setAttribute(Qt.WA_DeleteOnClose, False)
        self._runner_dlg.show()
        self._runner_dlg.raise_()
        self._runner_dlg.activateWindow()

    # ---------------- 资源 ----------------

    def closeEvent(self, ev) -> None:
        for dlg in (
            self._debug_tools_dlg,
            self._map_registry_dlg,
            self._movement_profile_dlg,
            self._routine_editor_dlg,
            self._runner_dlg,
        ):
            if dlg is not None:
                dlg.close()
        if self._mumu is not None:
            try:
                self._mumu.close()
            except Exception:
                log.exception("关闭 Mumu 失败")
        super().closeEvent(ev)
