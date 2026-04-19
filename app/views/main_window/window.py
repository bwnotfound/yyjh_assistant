"""主窗口：懒加载 Mumu，提供功能入口按钮。"""

from __future__ import annotations

import logging
from typing import Optional
import threading

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

from ..position_picker import PositionPickerDialog

log = logging.getLogger(__name__)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("烟雨江湖助手")
        self.resize(420, 240)

        self._mumu: Optional[Mumu] = None
        self._picker: Optional[PositionPickerDialog] = None

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

        btn_picker = QPushButton("取位置工具")
        btn_picker.setMinimumHeight(40)
        btn_picker.clicked.connect(self._open_picker)
        layout.addWidget(btn_picker)

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

    # ---------------- 功能入口 ----------------

    def _open_picker(self) -> None:
        try:
            mumu = self.get_mumu()
        except MumuError as e:
            QMessageBox.critical(self, "MuMu 连接失败", str(e))
            return
        except Exception as e:
            log.exception("Mumu 初始化异常")
            QMessageBox.critical(self, "MuMu 初始化异常", f"{type(e).__name__}: {e}")
            return

        if self._picker is None:
            self._picker = PositionPickerDialog(mumu, parent=self)
            self._picker.setAttribute(Qt.WA_DeleteOnClose, False)
        self._picker.show()
        self._picker.raise_()
        self._picker.activateWindow()

    # ---------------- 资源 ----------------

    def closeEvent(self, ev) -> None:
        if self._picker is not None:
            try:
                self._picker.close()
            except Exception:
                log.exception("关闭 picker 失败")

        # 由于上面已经把 close 超时压到 ~0.6s,进程几乎不会被留下孤儿。
        # Mumu 清理放到后台 daemon 线程:即使它还没做完,UI 也能立刻关掉;
        mumu = self._mumu
        self._mumu = None
        if mumu is not None:
            threading.Thread(
                target=self._shutdown_mumu_bg,
                args=(mumu,),
                daemon=True,
            ).start()

        super().closeEvent(ev)

    @staticmethod
    def _shutdown_mumu_bg(mumu) -> None:
        try:
            mumu.close()
        except Exception:
            log.exception("后台关闭 Mumu 失败")
