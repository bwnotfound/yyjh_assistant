"""烟雨江湖助手 - 入口"""

from __future__ import annotations

import logging
import sys

from PySide6.QtWidgets import QApplication

from app.views.main_window import MainWindow


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="[%(levelname)s %(name)s] %(message)s",
    )
    app = QApplication(sys.argv)
    app.setApplicationName("烟雨江湖助手")

    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
