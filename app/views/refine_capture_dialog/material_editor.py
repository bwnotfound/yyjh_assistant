"""装备-材料映射编辑器.

允许用户为每件装备配置 2 种材料的"显示名"(用于实时面板展示和日志打印),
不影响 OCR 识别本身—— OCR 是在固定 ROI (material_1 / material_2) 内读
'库存/消耗' 数字, 不依赖材料名是什么.

只更新 ``equipment_material_map`` 段, 不会动其他配置字段.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from app.core.refine.profile import RefineProfile

log = logging.getLogger(__name__)


class MaterialEditorDialog(QDialog):
    def __init__(self, profile: RefineProfile, profile_path: Path, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("装备-材料映射编辑")
        self.resize(540, 420)
        self.profile = profile
        self.profile_path = profile_path

        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel(
                "为每件装备配置 2 种材料的显示名 (任意文字, 仅用于面板和日志).\n"
                "OCR 不依赖此名字; 数字识别完全靠 ROI."
            )
        )

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["装备名", "材料1", "材料2"])
        h = self.table.horizontalHeader()
        h.setSectionResizeMode(QHeaderView.Stretch)
        layout.addWidget(self.table)

        btns = QHBoxLayout()
        b_add = QPushButton("+ 添加行")
        b_del = QPushButton("- 删除选中")
        b_save = QPushButton("保存")
        b_cancel = QPushButton("取消")
        b_add.clicked.connect(self._add_row)
        b_del.clicked.connect(self._del_row)
        b_save.clicked.connect(self._save)
        b_cancel.clicked.connect(self.reject)
        btns.addWidget(b_add)
        btns.addWidget(b_del)
        btns.addStretch(1)
        btns.addWidget(b_save)
        btns.addWidget(b_cancel)
        layout.addLayout(btns)

        self._load()

    def _load(self) -> None:
        m = self.profile.equipment_material_map
        self.table.setRowCount(len(m))
        for i, (eq, mats) in enumerate(m.items()):
            mats = list(mats) + ["", ""]
            self.table.setItem(i, 0, QTableWidgetItem(str(eq)))
            self.table.setItem(i, 1, QTableWidgetItem(str(mats[0])))
            self.table.setItem(i, 2, QTableWidgetItem(str(mats[1])))

    def _add_row(self) -> None:
        n = self.table.rowCount()
        self.table.insertRow(n)
        for c in range(3):
            self.table.setItem(n, c, QTableWidgetItem(""))

    def _del_row(self) -> None:
        rows = sorted({idx.row() for idx in self.table.selectedIndexes()}, reverse=True)
        for r in rows:
            self.table.removeRow(r)

    def _cell(self, r: int, c: int) -> str:
        item = self.table.item(r, c)
        return item.text().strip() if item else ""

    def _save(self) -> None:
        mapping: dict[str, list[str]] = {}
        for i in range(self.table.rowCount()):
            eq = self._cell(i, 0)
            m1 = self._cell(i, 1)
            m2 = self._cell(i, 2)
            if not eq and not m1 and not m2:
                continue  # 空行直接跳过
            if not eq:
                QMessageBox.warning(self, "校验失败", f"第 {i + 1} 行: 装备名不能为空")
                return
            if not m1 or not m2:
                QMessageBox.warning(
                    self,
                    "校验失败",
                    f"装备 [{eq}] 的两个材料都必须填写",
                )
                return
            if eq in mapping:
                QMessageBox.warning(self, "校验失败", f"装备名 [{eq}] 重复")
                return
            mapping[eq] = [m1, m2]

        try:
            self.profile.save_material_map(self.profile_path, mapping)
        except Exception as e:
            log.exception("保存材料映射失败")
            QMessageBox.critical(self, "保存失败", str(e))
            return
        self.accept()
