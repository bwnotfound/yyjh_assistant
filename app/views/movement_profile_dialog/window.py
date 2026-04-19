"""
运动配置对话框 —— 编辑 movement_profile.yaml

左列: 条目列表（各项 UI 位置 / ROI / character_pos / 各视野的 block_size）
右列: 选中条目的编辑器（单点 / 矩形 / 数值）+ "从游戏里取" 按钮（唤起 PositionPicker）
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from utils import Mumu

from app.core.profiles import (
    DEFAULT_MOVEMENT_YAML_PATH,
    MovementProfile,
    MovementRegistry,
    UIPositions,
    VisionSpec,
)
from app.views.position_picker import PositionPickerDialog

log = logging.getLogger(__name__)


class EntryKind(Enum):
    POINT = "point"  # 单点 (x, y)
    POINT_LIST = "point_list"  # 点列表（chat / table / ...）
    RECT = "rect"  # 矩形 (x0, y0, x1, y1)
    VISION = "vision"  # VisionSpec


@dataclass
class Entry:
    """左侧列表里的一项"""

    key: str  # 存储键（如 "ui.package_btn"）
    label: str  # 显示名
    kind: EntryKind
    getter: Callable  # () -> value
    setter: Callable  # (value) -> None
    sub_vision: Optional[str] = None  # VISION 类专属


class MovementProfileDialog(QDialog):
    def __init__(
        self,
        mumu: Mumu,
        parent=None,
        yaml_path: Path = DEFAULT_MOVEMENT_YAML_PATH,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("运动配置")
        self.resize(820, 560)

        self._mumu = mumu
        self._yaml_path = yaml_path
        self._registry: MovementRegistry = MovementRegistry.load(yaml_path)
        self._profile: MovementProfile = self._registry.ensure_profile(
            (mumu.device_w, mumu.device_h)
        )

        self._entries: list[Entry] = self._build_entries()
        self._build_ui()
        self._reload_list()

    # ========================================================================
    # 数据条目定义
    # ========================================================================

    def _build_entries(self) -> list[Entry]:
        prof = self._profile
        ui = prof.ui
        entries: list[Entry] = []

        def _simple_pt(attr: str, label: str, obj=ui):
            def getter(o=obj, a=attr):
                return getattr(o, a)

            def setter(v, o=obj, a=attr):
                setattr(o, a, v)

            return Entry(
                key=f"{obj.__class__.__name__}.{attr}",
                label=label,
                kind=EntryKind.POINT,
                getter=getter,
                setter=setter,
            )

        # character_pos
        entries.append(
            Entry(
                key="character_pos",
                label="角色屏幕位置",
                kind=EntryKind.POINT,
                getter=lambda: prof.character_pos,
                setter=lambda v: setattr(prof, "character_pos", v),
            )
        )

        # UI 单点位置
        for attr, label in [
            ("package_btn", "背包按钮"),
            ("ticket_btn", "车票按钮"),
            ("blank_btn", "空白点（跳过对话）"),
            ("buy_item_start_pos", "购买-商品首格中心"),
            ("buy_item_span", "购买-商品列行间距 (col_span, row_span)"),
            ("buy_increase_btn", "购买-数量 +"),
            ("buy_confirm_btn", "购买-确认"),
            ("buy_exit_btn", "购买-退出"),
        ]:
            entries.append(_simple_pt(attr, label))

        # 点列表
        for attr, label in [
            ("chat_btn_pos_list", "对话菜单项列表"),
            ("table_btn_pos_list", "场景交互项列表"),
        ]:
            entries.append(
                Entry(
                    key=f"ui.{attr}",
                    label=label,
                    kind=EntryKind.POINT_LIST,
                    getter=lambda a=attr: getattr(ui, a),
                    setter=lambda v, a=attr: setattr(ui, a, list(v)),
                )
            )

        # 矩形 ROI
        entries.append(
            Entry(
                key="minimap_coord_roi",
                label="小地图坐标 ROI（矩形）",
                kind=EntryKind.RECT,
                getter=lambda: prof.minimap_coord_roi,
                setter=lambda v: setattr(prof, "minimap_coord_roi", v),
            )
        )

        # 各视野档位
        for vname in ("小", "中", "大"):
            entries.append(
                Entry(
                    key=f"vision.{vname}",
                    label=f"视野档位「{vname}」",
                    kind=EntryKind.VISION,
                    getter=lambda n=vname: prof.vision_sizes.get(n),
                    setter=lambda v, n=vname: self._set_vision(n, v),
                    sub_vision=vname,
                )
            )

        return entries

    def _set_vision(self, name: str, value: Optional[VisionSpec]) -> None:
        if value is None:
            self._profile.vision_sizes.pop(name, None)
        else:
            self._profile.vision_sizes[name] = value

    # ========================================================================
    # UI 构造
    # ========================================================================

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(8)

        top = QHBoxLayout()
        top.addWidget(QLabel(f"当前分辨率: {self._profile.key}"))
        top.addStretch(1)
        root.addLayout(top)

        middle = QHBoxLayout()

        # 左：列表
        left_col = QVBoxLayout()
        left_col.addWidget(QLabel("配置项（✓ 已配 / ✗ 未配）"))
        self._list = QListWidget()
        self._list.currentItemChanged.connect(self._on_sel_changed)
        left_col.addWidget(self._list, 1)
        left_w = QWidget()
        left_w.setLayout(left_col)
        left_w.setMinimumWidth(260)
        left_w.setMaximumWidth(320)
        middle.addWidget(left_w)

        # 右：编辑器
        self._editor_host = QVBoxLayout()
        self._editor_placeholder = QLabel("请在左侧选一项")
        self._editor_placeholder.setAlignment(Qt.AlignCenter)
        self._editor_host.addWidget(self._editor_placeholder)
        self._editor_host.addStretch(1)
        right_w = QWidget()
        right_w.setLayout(self._editor_host)
        middle.addWidget(right_w, 1)

        root.addLayout(middle, 1)

        # 底部
        bottom = QHBoxLayout()
        btn_reload = QPushButton("重新加载")
        btn_reload.clicked.connect(self._on_reload)
        bottom.addWidget(btn_reload)
        bottom.addStretch(1)
        btn_save = QPushButton("保存到 YAML")
        btn_save.clicked.connect(self._on_save)
        bottom.addWidget(btn_save)
        btn_close = QPushButton("关闭")
        btn_close.clicked.connect(self.close)
        bottom.addWidget(btn_close)
        root.addLayout(bottom)

    def _reload_list(self) -> None:
        prev_row = self._list.currentRow()
        self._list.blockSignals(True)
        self._list.clear()
        for i, e in enumerate(self._entries):
            mark = "✓" if self._entry_is_set(e) else "✗"
            it = QListWidgetItem(f"{mark} {e.label}")
            it.setData(Qt.UserRole, i)
            self._list.addItem(it)
        self._list.blockSignals(False)
        if 0 <= prev_row < self._list.count():
            self._list.setCurrentRow(prev_row)
        elif self._list.count() > 0:
            self._list.setCurrentRow(0)

    def _entry_is_set(self, e: Entry) -> bool:
        v = e.getter()
        if v is None:
            return False
        if e.kind == EntryKind.POINT_LIST:
            return bool(v)
        return True

    def _on_sel_changed(self, cur: QListWidgetItem, _prev) -> None:
        # 清空现有编辑器
        while self._editor_host.count():
            item = self._editor_host.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)

        if cur is None:
            ph = QLabel("请在左侧选一项")
            ph.setAlignment(Qt.AlignCenter)
            self._editor_host.addWidget(ph)
            self._editor_host.addStretch(1)
            return
        entry = self._entries[cur.data(Qt.UserRole)]
        editor = self._build_editor(entry)
        self._editor_host.addWidget(editor)
        self._editor_host.addStretch(1)

    # ========================================================================
    # 编辑器（按 kind 分派）
    # ========================================================================

    def _build_editor(self, entry: Entry) -> QWidget:
        if entry.kind == EntryKind.POINT:
            return self._editor_point(entry)
        if entry.kind == EntryKind.POINT_LIST:
            return self._editor_point_list(entry)
        if entry.kind == EntryKind.RECT:
            return self._editor_rect(entry)
        if entry.kind == EntryKind.VISION:
            return self._editor_vision(entry)
        raise ValueError(f"未知 kind: {entry.kind}")

    def _mk_norm_spin(self, v: float) -> QDoubleSpinBox:
        s = QDoubleSpinBox()
        s.setDecimals(4)
        s.setRange(0.0, 1.0)
        s.setSingleStep(0.001)
        s.setValue(v)
        return s

    def _editor_point(self, entry: Entry) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        v = entry.getter()
        sx = self._mk_norm_spin(v[0] if v else 0.0)
        sy = self._mk_norm_spin(v[1] if v else 0.0)
        form.addRow(f"<b>{entry.label}</b>", QLabel(""))
        form.addRow("x (归一化)", sx)
        form.addRow("y (归一化)", sy)

        def _apply():
            entry.setter((sx.value(), sy.value()))
            self._reload_list()

        row = QHBoxLayout()
        btn_pick = QPushButton("从游戏里取")

        def _pick():
            records = self._pick_points(1, [entry.label])
            if records:
                sx.setValue(records[0].nx)
                sy.setValue(records[0].ny)

        btn_pick.clicked.connect(_pick)
        row.addWidget(btn_pick)
        btn_apply = QPushButton("应用")
        btn_apply.clicked.connect(_apply)
        row.addWidget(btn_apply)
        btn_clear = QPushButton("清空")

        def _clear():
            entry.setter(None)
            self._reload_list()
            self._on_sel_changed(self._list.currentItem(), None)

        btn_clear.clicked.connect(_clear)
        row.addWidget(btn_clear)
        row.addStretch(1)
        form.addRow("", _wrap(row))
        return w

    def _editor_point_list(self, entry: Entry) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        pts = entry.getter() or []

        form.addRow(f"<b>{entry.label}</b>", QLabel(""))

        # 简单展示文本
        txt = QLabel(self._format_point_list(pts))
        txt.setTextInteractionFlags(Qt.TextSelectableByMouse)
        form.addRow("已配置", txt)

        n_spin = QSpinBox()
        n_spin.setRange(1, 20)
        n_spin.setValue(max(1, len(pts) or 5))
        form.addRow("录入点数", n_spin)

        def _pick_all():
            n = n_spin.value()
            labels = [f"{entry.label} #{i+1}" for i in range(n)]
            records = self._pick_points(n, labels)
            if records is None:
                return
            new_pts = [(r.nx, r.ny) for r in records]
            entry.setter(new_pts)
            self._reload_list()
            self._on_sel_changed(self._list.currentItem(), None)

        btn_pick = QPushButton("重录整个列表")
        btn_pick.clicked.connect(_pick_all)
        form.addRow("", btn_pick)

        btn_clear = QPushButton("清空列表")

        def _clear():
            entry.setter([])
            self._reload_list()
            self._on_sel_changed(self._list.currentItem(), None)

        btn_clear.clicked.connect(_clear)
        form.addRow("", btn_clear)

        return w

    @staticmethod
    def _format_point_list(pts: list[tuple[float, float]]) -> str:
        if not pts:
            return "（空）"
        return "\n".join(
            f"  #{i+1}: ({x:.4f}, {y:.4f})" for i, (x, y) in enumerate(pts)
        )

    def _editor_rect(self, entry: Entry) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        v = entry.getter()
        x0 = self._mk_norm_spin(v[0] if v else 0.0)
        y0 = self._mk_norm_spin(v[1] if v else 0.0)
        x1 = self._mk_norm_spin(v[2] if v else 0.0)
        y1 = self._mk_norm_spin(v[3] if v else 0.0)
        form.addRow(f"<b>{entry.label}</b>", QLabel(""))
        form.addRow("x0", x0)
        form.addRow("y0", y0)
        form.addRow("x1", x1)
        form.addRow("y1", y1)

        def _apply():
            entry.setter((x0.value(), y0.value(), x1.value(), y1.value()))
            self._reload_list()

        row = QHBoxLayout()
        btn_pick = QPushButton("从游戏里取（左上→右下）")

        def _pick():
            records = self._pick_points(
                2, [f"{entry.label} 左上", f"{entry.label} 右下"]
            )
            if records:
                x0.setValue(records[0].nx)
                y0.setValue(records[0].ny)
                x1.setValue(records[1].nx)
                y1.setValue(records[1].ny)

        btn_pick.clicked.connect(_pick)
        row.addWidget(btn_pick)
        btn_apply = QPushButton("应用")
        btn_apply.clicked.connect(_apply)
        row.addWidget(btn_apply)
        btn_clear = QPushButton("清空")

        def _clear():
            entry.setter(None)
            self._reload_list()
            self._on_sel_changed(self._list.currentItem(), None)

        btn_clear.clicked.connect(_clear)
        row.addWidget(btn_clear)
        row.addStretch(1)
        form.addRow("", _wrap(row))
        return w

    def _editor_vision(self, entry: Entry) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        spec: Optional[VisionSpec] = entry.getter()

        form.addRow(f"<b>{entry.label}</b>", QLabel(""))

        bw = self._mk_norm_spin(spec.block_size[0] if spec else 0.08)
        bh = self._mk_norm_spin(spec.block_size[1] if spec else 0.08)
        form.addRow("block_size.x", bw)
        form.addRow("block_size.y", bh)

        mm = QSpinBox()
        mm.setRange(1, 100)
        mm.setValue(spec.move_max_num if spec else 8)
        form.addRow("move_max_num", mm)

        vdl = QSpinBox()
        vdl.setRange(0, 100)
        vdl.setValue(spec.vision_delta_limit if spec else 8)
        form.addRow("vision_delta_limit", vdl)

        def _apply():
            entry.setter(
                VisionSpec(
                    block_size=(bw.value(), bh.value()),
                    move_max_num=mm.value(),
                    vision_delta_limit=vdl.value(),
                )
            )
            self._reload_list()

        row = QHBoxLayout()
        btn_apply = QPushButton("应用")
        btn_apply.clicked.connect(_apply)
        row.addWidget(btn_apply)
        btn_clear = QPushButton("清空（删除该档位）")

        def _clear():
            entry.setter(None)
            self._reload_list()
            self._on_sel_changed(self._list.currentItem(), None)

        btn_clear.clicked.connect(_clear)
        row.addWidget(btn_clear)
        row.addStretch(1)
        form.addRow("", _wrap(row))
        return w

    # ========================================================================
    # 取位置
    # ========================================================================

    def _pick_points(self, n: int, labels: list[str]):
        dlg = PositionPickerDialog(
            self._mumu,
            parent=self,
            selection_mode=True,
            expected_count=n,
            selection_labels=labels,
        )
        if dlg.exec() == QDialog.Accepted:
            return dlg.result_records()
        return None

    # ========================================================================
    # 保存 / 加载
    # ========================================================================

    def _on_save(self) -> None:
        try:
            path = self._registry.save(self._yaml_path)
        except Exception as e:
            log.exception("保存 movement_profile 失败")
            QMessageBox.critical(self, "保存失败", f"{type(e).__name__}: {e}")
            return
        QMessageBox.information(self, "保存成功", f"已写入: {path}")

    def _on_reload(self) -> None:
        if (
            QMessageBox.question(self, "重新加载", "丢弃未保存改动？")
            != QMessageBox.Yes
        ):
            return
        self._registry = MovementRegistry.load(self._yaml_path)
        self._profile = self._registry.ensure_profile(
            (self._mumu.device_w, self._mumu.device_h)
        )
        self._entries = self._build_entries()
        self._reload_list()


def _wrap(lay) -> QWidget:
    w = QWidget()
    w.setLayout(lay)
    return w
