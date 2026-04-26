"""
运动配置对话框 —— 编辑 movement_profile.yaml

左列: 条目列表（各项 UI 位置 / ROI / character_pos / 各视野的 block_size）
右列: 选中条目的编辑器：
        POINT          单点 (x, y)
        LINEAR_GROUP   等距按钮组（first/second/count）
        BUY_GRID       2D 商品栅格（first/second/cols/rows/second_index）
        RECT           矩形 (x0, y0, x1, y1)
        VISION         VisionSpec（含"取屏幕点反算 block_size"功能）
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
    BuyItemGrid,
    ClickDelays,
    LinearButtonGroup,
    MovementProfile,
    MovementRegistry,
    VisionSpec,
)
from app.views.position_picker import PositionPickerDialog

log = logging.getLogger(__name__)


class EntryKind(Enum):
    POINT = "point"  # 单点 (x, y)
    LINEAR_GROUP = "linear_group"  # 等距按钮组
    BUY_GRID = "buy_grid"  # 2D 商品栅格
    RECT = "rect"  # 矩形 (x0, y0, x1, y1)
    VISION = "vision"  # VisionSpec
    CALIBRATE = "calibrate"  # 角色中心点 + 视野 block_size 联动反算
    DELAYS = "delays"  # 各类点击延时配置


@dataclass
class Entry:
    """左侧列表里的一项"""

    key: str  # 存储键
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
        self.resize(840, 640)

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
            ("buy_increase_btn", "购买-数量 +"),
            ("buy_confirm_btn", "购买-确认"),
            ("buy_exit_btn", "购买-退出"),
        ]:
            entries.append(_simple_pt(attr, label))

        # 自定义 click 预设 (来自 routine 编辑器「新建预设」)
        # 每个自建预设作为一个 POINT 类型 entry, getter/setter 操作 ui.custom 字典。
        # 这样在运动配置里改坐标 → 保存 → 所有引用该预设的 routine 立即生效。
        # 列表按名字排序保证稳定显示顺序。
        def _custom_pt(name: str):
            """name 闭包: 给定 custom 字典 key, 返回操作该 key 的 Entry"""

            def getter(n=name):
                return ui.custom.get(n)

            def setter(v, n=name):
                if v is None:
                    # 编辑器允许"清空"一个点位 (传 None) - 自定义预设清空 = 删除条目
                    # (与内置字段允许 None 留位的语义不同, 自定义预设只在有值时存在)
                    ui.custom.pop(n, None)
                else:
                    ui.custom[n] = (float(v[0]), float(v[1]))

            return Entry(
                key=f"ui.custom.{name}",
                label=f"自建: {name}",
                kind=EntryKind.POINT,
                getter=getter,
                setter=setter,
            )

        for cname in sorted(ui.custom.keys()):
            entries.append(_custom_pt(cname))

        # 等距按钮组
        for attr, label in [
            ("chat_btn_group", "对话菜单按钮（等距）"),
            ("table_btn_group", "场景交互按钮（等距）"),
        ]:
            entries.append(
                Entry(
                    key=f"ui.{attr}",
                    label=label,
                    kind=EntryKind.LINEAR_GROUP,
                    getter=lambda a=attr: getattr(ui, a),
                    setter=lambda v, a=attr: setattr(ui, a, v),
                )
            )

        # 商品栅格
        entries.append(
            Entry(
                key="ui.buy_item_grid",
                label="购买-商品栅格（2D）",
                kind=EntryKind.BUY_GRID,
                getter=lambda: ui.buy_item_grid,
                setter=lambda v: setattr(ui, "buy_item_grid", v),
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

        entries.append(
            Entry(
                key="map_view_area",
                label="地图可视区域（矩形）",
                kind=EntryKind.RECT,
                getter=lambda: prof.map_view_area,
                setter=lambda v: setattr(prof, "map_view_area", v),
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

        # 点击延时
        entries.append(
            Entry(
                key="click_delays",
                label="点击延时配置",
                kind=EntryKind.DELAYS,
                getter=lambda: prof.click_delays,
                setter=lambda v: setattr(prof, "click_delays", v),
            )
        )

        # 角色中心点 + 视野联动反算（独立工具入口；不存独立字段）
        entries.append(
            Entry(
                key="calibrate.character_and_vision",
                label="角色中心点 + 视野（联动反算）",
                kind=EntryKind.CALIBRATE,
                getter=lambda: None,
                setter=lambda v: None,
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
            if e.kind == EntryKind.CALIBRATE:
                # 工具入口，不存独立字段，不显示 ✓/✗
                mark = "🛠"
            else:
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
        if entry.kind == EntryKind.LINEAR_GROUP:
            return self._editor_linear_group(entry)
        if entry.kind == EntryKind.BUY_GRID:
            return self._editor_buy_grid(entry)
        if entry.kind == EntryKind.RECT:
            return self._editor_rect(entry)
        if entry.kind == EntryKind.VISION:
            return self._editor_vision(entry)
        if entry.kind == EntryKind.CALIBRATE:
            return self._editor_calibrate(entry)
        if entry.kind == EntryKind.DELAYS:
            return self._editor_delays(entry)
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

    def _editor_linear_group(self, entry: Entry) -> QWidget:
        """等距按钮组：录第 1 个 + 第 2 个，运行时按等差推算第 i 个"""
        w = QWidget()
        form = QFormLayout(w)
        grp: Optional[LinearButtonGroup] = entry.getter()

        form.addRow(f"<b>{entry.label}</b>", QLabel(""))
        hint = QLabel(
            "等距推算：录入第 1 个和第 2 个按钮位置，"
            "运行时按 first + (i-1) × (second - first) 算第 i 个。"
        )
        hint.setStyleSheet("color: #888;")
        hint.setWordWrap(True)
        form.addRow("", hint)

        # 第 1 个
        fx = self._mk_norm_spin(grp.first[0] if grp else 0.0)
        fy = self._mk_norm_spin(grp.first[1] if grp else 0.0)
        first_row = QHBoxLayout()
        first_row.addWidget(QLabel("x"))
        first_row.addWidget(fx)
        first_row.addWidget(QLabel("y"))
        first_row.addWidget(fy)
        btn_pick_first = QPushButton("取位置")

        def _pick_first():
            records = self._pick_points(1, [f"{entry.label} 第 1 个"])
            if records:
                fx.setValue(records[0].nx)
                fy.setValue(records[0].ny)

        btn_pick_first.clicked.connect(_pick_first)
        first_row.addWidget(btn_pick_first)
        first_row.addStretch(1)
        form.addRow("第 1 个", _wrap(first_row))

        # 第 2 个
        sx = self._mk_norm_spin(grp.second[0] if grp else 0.0)
        sy = self._mk_norm_spin(grp.second[1] if grp else 0.0)
        second_row = QHBoxLayout()
        second_row.addWidget(QLabel("x"))
        second_row.addWidget(sx)
        second_row.addWidget(QLabel("y"))
        second_row.addWidget(sy)
        btn_pick_second = QPushButton("取位置")

        def _pick_second():
            records = self._pick_points(1, [f"{entry.label} 第 2 个"])
            if records:
                sx.setValue(records[0].nx)
                sy.setValue(records[0].ny)

        btn_pick_second.clicked.connect(_pick_second)
        second_row.addWidget(btn_pick_second)
        second_row.addStretch(1)
        form.addRow("第 2 个", _wrap(second_row))

        # count
        count_spin = QSpinBox()
        count_spin.setRange(2, 20)
        count_spin.setValue(grp.count if grp else 6)
        form.addRow("count（按钮总数）", count_spin)

        # 操作
        row = QHBoxLayout()
        btn_pick_both = QPushButton("一次取两个")

        def _pick_both():
            records = self._pick_points(
                2,
                [f"{entry.label} 第 1 个", f"{entry.label} 第 2 个"],
            )
            if records:
                fx.setValue(records[0].nx)
                fy.setValue(records[0].ny)
                sx.setValue(records[1].nx)
                sy.setValue(records[1].ny)

        btn_pick_both.clicked.connect(_pick_both)
        row.addWidget(btn_pick_both)

        btn_apply = QPushButton("应用")

        def _apply():
            entry.setter(
                LinearButtonGroup(
                    first=(fx.value(), fy.value()),
                    second=(sx.value(), sy.value()),
                    count=count_spin.value(),
                )
            )
            self._reload_list()

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

    def _editor_buy_grid(self, entry: Entry) -> QWidget:
        """商品 2D 栅格：录第 1 个 + 第 second_index 个，反解列/行间距"""
        w = QWidget()
        form = QFormLayout(w)
        grid: Optional[BuyItemGrid] = entry.getter()

        form.addRow(f"<b>{entry.label}</b>", QLabel(""))
        hint = QLabel(
            "录入第 1 个商品和第 N 个商品（默认 N = cols × rows，对角误差最小）。"
            "约束：第 N 个必须与第 1 个既不同行也不同列；"
            "运行时假设列方向只影响 x、行方向只影响 y。"
        )
        hint.setStyleSheet("color: #888;")
        hint.setWordWrap(True)
        form.addRow("", hint)

        cols_spin = QSpinBox()
        cols_spin.setRange(1, 20)
        cols_spin.setValue(grid.cols if grid else 2)
        form.addRow("cols（列数）", cols_spin)

        rows_spin = QSpinBox()
        rows_spin.setRange(1, 20)
        rows_spin.setValue(grid.rows if grid else 4)
        form.addRow("rows（行数）", rows_spin)

        # 第 1 个
        fx = self._mk_norm_spin(grid.first[0] if grid else 0.0)
        fy = self._mk_norm_spin(grid.first[1] if grid else 0.0)
        first_row = QHBoxLayout()
        first_row.addWidget(QLabel("x"))
        first_row.addWidget(fx)
        first_row.addWidget(QLabel("y"))
        first_row.addWidget(fy)
        btn_pick_first = QPushButton("取位置")

        def _pick_first():
            records = self._pick_points(1, [f"{entry.label} 第 1 个商品"])
            if records:
                fx.setValue(records[0].nx)
                fy.setValue(records[0].ny)

        btn_pick_first.clicked.connect(_pick_first)
        first_row.addWidget(btn_pick_first)
        first_row.addStretch(1)
        form.addRow("第 1 个商品", _wrap(first_row))

        # second_index（对角默认）
        si_spin = QSpinBox()
        si_spin.setRange(2, 400)
        # 默认放最大值（对角点）
        default_si = (
            grid.second_index if grid else cols_spin.value() * rows_spin.value()
        )
        si_spin.setValue(default_si)

        def _refresh_si_max():
            new_max = max(2, cols_spin.value() * rows_spin.value())
            si_spin.setMaximum(new_max)
            # 如果当前值落在合法范围外，拉回上限
            if si_spin.value() > new_max:
                si_spin.setValue(new_max)

        cols_spin.valueChanged.connect(_refresh_si_max)
        rows_spin.valueChanged.connect(_refresh_si_max)
        _refresh_si_max()
        form.addRow("second_index（第 N 个，1-based）", si_spin)

        # 第 N 个（second）
        sx_spin = self._mk_norm_spin(grid.second[0] if grid else 0.0)
        sy_spin = self._mk_norm_spin(grid.second[1] if grid else 0.0)
        second_row = QHBoxLayout()
        second_row.addWidget(QLabel("x"))
        second_row.addWidget(sx_spin)
        second_row.addWidget(QLabel("y"))
        second_row.addWidget(sy_spin)
        btn_pick_second = QPushButton("取位置")

        def _pick_second():
            records = self._pick_points(
                1, [f"{entry.label} 第 {si_spin.value()} 个商品"]
            )
            if records:
                sx_spin.setValue(records[0].nx)
                sy_spin.setValue(records[0].ny)

        btn_pick_second.clicked.connect(_pick_second)
        second_row.addWidget(btn_pick_second)
        second_row.addStretch(1)
        form.addRow("第 N 个商品", _wrap(second_row))

        # 操作
        op_row = QHBoxLayout()
        btn_apply = QPushButton("应用")

        def _apply():
            cols = cols_spin.value()
            rows = rows_spin.value()
            si = si_spin.value()
            # 校验 second_index 不与第 1 个同行/同列
            s_col = (si - 1) % cols
            s_row = (si - 1) // cols
            if s_col == 0 or s_row == 0 or si == 1:
                QMessageBox.warning(
                    self,
                    "second_index 不合法",
                    f"第 {si} 个与第 1 个在同行或同列，无法反解列/行间距。\n"
                    f"请选与第 1 个对角的位置（推荐 N = cols × rows = {cols * rows}）。",
                )
                return
            entry.setter(
                BuyItemGrid(
                    cols=cols,
                    rows=rows,
                    first=(fx.value(), fy.value()),
                    second=(sx_spin.value(), sy_spin.value()),
                    second_index=si,
                )
            )
            self._reload_list()

        btn_apply.clicked.connect(_apply)
        op_row.addWidget(btn_apply)

        btn_clear = QPushButton("清空")

        def _clear():
            entry.setter(None)
            self._reload_list()
            self._on_sel_changed(self._list.currentItem(), None)

        btn_clear.clicked.connect(_clear)
        op_row.addWidget(btn_clear)
        op_row.addStretch(1)
        form.addRow("", _wrap(op_row))

        return w

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
        """
        VisionSpec 编辑器。
        在原有 block_size / move_max_num / vision_delta_limit 基础上，加一段
        "取屏幕点反算 block_size"：
            等距投影 screen_dx = (dx-dy)*bw/2, screen_dy = (dx+dy)*bh/2，反解
            bw = 2*(tx-cx)/(dx-dy), bh = 2*(ty-cy)/(dx+dy)
            约束：dx != dy 且 dx != -dy（否则其中一个无法独立解出）
            距离越远（|dx|+|dy| 越大）误差越小，默认 (8, 0) 纯东 8 格。
        """
        w = QWidget()
        form = QFormLayout(w)
        spec: Optional[VisionSpec] = entry.getter()

        form.addRow(f"<b>{entry.label}</b>", QLabel(""))

        bw = self._mk_norm_spin(spec.block_size[0] if spec else 0.08)
        bh = self._mk_norm_spin(spec.block_size[1] if spec else 0.08)
        form.addRow("block_size.x (bw)", bw)
        form.addRow("block_size.y (bh)", bh)

        mm = QSpinBox()
        mm.setRange(1, 100)
        mm.setValue(spec.move_max_num if spec else 8)
        form.addRow("move_max_num", mm)

        vdl = QSpinBox()
        vdl.setRange(0, 100)
        vdl.setValue(spec.vision_delta_limit if spec else 8)
        form.addRow("vision_delta_limit", vdl)

        # ---- 反算 block_size ----
        inv_hint = QLabel(
            "反算：在游戏里站定后，目测一个 (dx, dy) 格远的位置点击，"
            "自动反算 bw/bh 写入上方。约束 dx ≠ dy 且 dx ≠ -dy；"
            "距离越远误差越小（默认 (8, 0) 纯东方向 8 格）。"
        )
        inv_hint.setStyleSheet("color: #888;")
        inv_hint.setWordWrap(True)
        form.addRow("", inv_hint)

        dx_spin = QSpinBox()
        dx_spin.setRange(-50, 50)
        dx_spin.setValue(8)
        dy_spin = QSpinBox()
        dy_spin.setRange(-50, 50)
        dy_spin.setValue(0)
        inv_row = QHBoxLayout()
        inv_row.addWidget(QLabel("dx"))
        inv_row.addWidget(dx_spin)
        inv_row.addWidget(QLabel("dy"))
        inv_row.addWidget(dy_spin)

        btn_inv = QPushButton("取屏幕点反算")

        def _do_inverse():
            dx = dx_spin.value()
            dy = dy_spin.value()
            if dx == dy or dx == -dy:
                QMessageBox.warning(
                    self,
                    "约束未满足",
                    f"当前 dx={dx}, dy={dy}：dx == dy 或 dx == -dy 时\n"
                    f"bw 或 bh 无法独立解出。请改用其他方向（推荐 (8, 0) 或 (0, 8)）。",
                )
                return
            cx, cy = self._profile.character_pos
            records = self._pick_points(
                1,
                [f"{entry.label} 反算：" f"距角色 (dx={dx}, dy={dy}) 格远的格子中心"],
            )
            if not records:
                return
            tx, ty = records[0].nx, records[0].ny
            new_bw = 2 * (tx - cx) / (dx - dy)
            new_bh = 2 * (ty - cy) / (dx + dy)
            # 直接写入 spinbox（让用户 review）。
            # spinbox 范围 0~1 会自动 clamp，异常时弹警告告知
            bw.setValue(new_bw)
            bh.setValue(new_bh)
            if not (0 < new_bw < 1) or not (0 < new_bh < 1):
                QMessageBox.warning(
                    self,
                    "反算结果异常",
                    f"反算得到 bw={new_bw:.4f}, bh={new_bh:.4f}（已 clamp 到 0~1）。\n"
                    f"检查：character_pos=({cx:.4f}, {cy:.4f})，"
                    f"取的点=({tx:.4f}, {ty:.4f})，dx={dx}, dy={dy}。\n"
                    f"等距投影里 +x 是右下方向，+y 是左下方向。",
                )

        btn_inv.clicked.connect(_do_inverse)
        inv_row.addWidget(btn_inv)
        inv_row.addStretch(1)
        form.addRow("反算 block_size", _wrap(inv_row))

        # ---- 操作 ----
        row = QHBoxLayout()
        btn_apply = QPushButton("应用")

        def _apply():
            entry.setter(
                VisionSpec(
                    block_size=(bw.value(), bh.value()),
                    move_max_num=mm.value(),
                    vision_delta_limit=vdl.value(),
                )
            )
            self._reload_list()

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
    # 联动反算编辑器（角色中心点 + 视野 block_size 一起取）
    # ========================================================================

    @staticmethod
    def _fmt_pos(p: Optional[tuple[float, float]]) -> str:
        if p is None:
            return "—"
        return f"({p[0]:.4f}, {p[1]:.4f})"

    def _editor_calibrate(self, entry: Entry) -> QWidget:
        """
        联动反算工具：取两个屏幕点反算 character_pos 和某视野档位的 block_size。

        点 1 = 角色脚下 → 直接作为 character_pos
        点 2 = (dx, dy) 格远的目标格中心 → 配合 dx,dy 反算 block_size：
            bw = 2*(tx-cx)/(dx-dy), bh = 2*(ty-cy)/(dx+dy)

        约束 dx ≠ dy 且 dx ≠ -dy；距离越远误差越小，默认 (8, 0)。
        反算成功直接写入 profile，无需点"应用"。
        """
        w = QWidget()
        form = QFormLayout(w)

        form.addRow(f"<b>{entry.label}</b>", QLabel(""))
        hint = QLabel(
            "在游戏里站定 → 输入 dx, dy → 取两个屏幕点：\n"
            "  ① 角色脚下（写入 character_pos）\n"
            "  ② 距角色 (dx, dy) 格远的格子中心（配合 dx,dy 反算 block_size）\n"
            "约束：dx ≠ dy 且 dx ≠ -dy；距离越远误差越小（默认 dx=8, dy=0）。"
        )
        hint.setStyleSheet("color: #888;")
        hint.setWordWrap(True)
        form.addRow("", hint)

        # 视野档位选择
        vision_combo = QComboBox()
        for vname in self._profile.vision_sizes.keys():
            vision_combo.addItem(vname)
        # 兜底：若一个档位都没有，给三个候选
        if vision_combo.count() == 0:
            for vname in ("小", "中", "大"):
                vision_combo.addItem(vname)
        form.addRow("视野档位", vision_combo)

        # dxdy
        dx_spin = QSpinBox()
        dx_spin.setRange(-50, 50)
        dx_spin.setValue(8)
        dy_spin = QSpinBox()
        dy_spin.setRange(-50, 50)
        dy_spin.setValue(0)
        dxdy_row = QHBoxLayout()
        dxdy_row.addWidget(QLabel("dx"))
        dxdy_row.addWidget(dx_spin)
        dxdy_row.addWidget(QLabel("dy"))
        dxdy_row.addWidget(dy_spin)
        dxdy_row.addStretch(1)
        form.addRow("第 2 个点相对角色的 (dx, dy)", _wrap(dxdy_row))

        # 当前显示值
        cp_label = QLabel(self._fmt_pos(self._profile.character_pos))
        form.addRow("当前 character_pos", cp_label)

        bs_label = QLabel("—")

        def _refresh_bs_label():
            vname = vision_combo.currentText()
            spec = self._profile.vision_sizes.get(vname)
            bs_label.setText(self._fmt_pos(spec.block_size) if spec else "（未配）")

        _refresh_bs_label()
        vision_combo.currentTextChanged.connect(lambda _: _refresh_bs_label())
        form.addRow("当前 block_size", bs_label)

        # 反算
        btn = QPushButton("取两点 → 反算并应用")

        def _do_calibrate():
            dx = dx_spin.value()
            dy = dy_spin.value()
            if dx == dy or dx == -dy:
                QMessageBox.warning(
                    self,
                    "约束未满足",
                    f"当前 dx={dx}, dy={dy}：dx == dy 或 dx == -dy 时\n"
                    f"bw 或 bh 无法独立解出。请改用其他方向（推荐 (8, 0) 或 (0, 8)）。",
                )
                return
            vname = vision_combo.currentText()
            if not vname:
                QMessageBox.warning(self, "缺少视野档位", "请先设定一个视野档位")
                return

            records = self._pick_points(
                2,
                [
                    "① 角色脚下（character_pos）",
                    f"② 距角色 (dx={dx}, dy={dy}) 格远的格子中心",
                ],
            )
            if not records or len(records) < 2:
                return

            cx, cy = records[0].nx, records[0].ny
            tx, ty = records[1].nx, records[1].ny
            bw_new = 2 * (tx - cx) / (dx - dy)
            bh_new = 2 * (ty - cy) / (dx + dy)

            # 直接落库（联动产出 4 个数横跨两个字段，再要求点"应用"反而绕）
            self._profile.character_pos = (cx, cy)
            existing = self._profile.vision_sizes.get(vname)
            mm = existing.move_max_num if existing else 8
            vdl = existing.vision_delta_limit if existing else 8
            self._profile.vision_sizes[vname] = VisionSpec(
                block_size=(bw_new, bh_new),
                move_max_num=mm,
                vision_delta_limit=vdl,
            )

            # 显示更新
            cp_label.setText(self._fmt_pos(self._profile.character_pos))
            _refresh_bs_label()
            self._reload_list()

            # 异常提醒（spinbox 0~1 范围在 vision 编辑器才会 clamp，这里直接落库无 clamp，
            # 但仍提示用户：异常值多半是 dx/dy 符号或取点位置反了）
            anomaly = not (0 < bw_new < 1) or not (0 < bh_new < 1)
            if anomaly:
                QMessageBox.warning(
                    self,
                    "反算结果异常",
                    f"得到 bw={bw_new:.4f}, bh={bh_new:.4f}（已写入但值异常）。\n"
                    f"检查：取的点 ① ({cx:.4f}, {cy:.4f})，"
                    f"② ({tx:.4f}, {ty:.4f})，dx={dx}, dy={dy}。\n"
                    f"等距投影里 +x 是右下方向，+y 是左下方向。\n"
                    f"建议用「视野档位「{vname}」」编辑器手动修正，或重新执行反算。",
                )
            else:
                QMessageBox.information(
                    self,
                    "已应用",
                    f"character_pos = ({cx:.4f}, {cy:.4f})\n"
                    f"视野「{vname}」.block_size = ({bw_new:.4f}, {bh_new:.4f})\n"
                    f"（仍需点底部「保存到 YAML」才落盘）",
                )

        btn.clicked.connect(_do_calibrate)
        form.addRow("", btn)

        return w

    # ========================================================================
    # 点击延时编辑器
    # ========================================================================

    def _editor_delays(self, entry: Entry) -> QWidget:
        """
        ClickDelays 编辑器：default 是必填的通用延时，其他分类未设值（spinbox
        最小值「使用默认」）时回退到 default。
        """
        w = QWidget()
        form = QFormLayout(w)
        cd: ClickDelays = entry.getter()

        form.addRow(f"<b>{entry.label}</b>", QLabel(""))
        hint = QLabel(
            "每次点击之后等待的秒数。各分类未设值（值为「使用默认」）时回退到 default。\n"
            "数值越大游戏 UI 反应越稳定，但整体操作变慢。"
        )
        hint.setStyleSheet("color: #888;")
        hint.setWordWrap(True)
        form.addRow("", hint)

        # default：必填，没有"使用默认"语义
        default_spin = QDoubleSpinBox()
        default_spin.setDecimals(2)
        default_spin.setRange(0.0, 60.0)
        default_spin.setSingleStep(0.1)
        default_spin.setValue(cd.default)
        form.addRow("default（通用默认）", default_spin)

        # 其他分类：spinbox 拨到最小值显示"（使用默认 X.Xs）"，等价于该分类未显式设值。
        # 第 3 项 = 该分类的"推荐默认值"（仅 travel_transition 有；其他靠 default 兜底）。
        sub_specs = [
            ("button", "button（场景/对话按钮）"),
            ("blank_skip", "blank_skip（跳对话点空白）"),
            ("buy_item", "buy_item（选商品）"),
            ("buy_increase", "buy_increase（数量 +1）"),
            ("buy_confirm", "buy_confirm（确认购买）"),
            ("buy_exit", "buy_exit（退出购买）"),
            ("click", "click（通用 click step）"),
            ("open_package", "open_package（打开背包）"),
            ("ticket", "ticket（背包里点票券）"),
            ("travel_icon", "travel_icon（地图跳转-点目标图标）"),
            ("travel_confirm", "travel_confirm（地图跳转-点确认按钮）"),
            ("travel_transition", "travel_transition（地图跳转切图过场）"),
            ("move_step", "move_step（move 每段 click 后等待，OCR 闭环下默认 0）"),
        ]

        sub_spins: dict[str, QDoubleSpinBox] = {}
        for attr, label in sub_specs:
            spin = QDoubleSpinBox()
            spin.setDecimals(2)
            spin.setRange(-1.0, 60.0)  # -1 触发 specialValueText
            spin.setSingleStep(0.1)
            # specialValueText 显示该分类未显式设值时实际生效的兜底值
            fb = cd.fallback_for(attr)
            if attr in ClickDelays._RECOMMENDED_DEFAULTS:
                spin.setSpecialValueText(f"（使用推荐 {fb:.1f}s）")
            else:
                spin.setSpecialValueText(f"（使用默认 {fb:.2f}s）")
            v = getattr(cd, attr)
            spin.setValue(v if v is not None else -1.0)
            sub_spins[attr] = spin
            form.addRow(label, spin)

        # 操作
        row = QHBoxLayout()

        btn_apply = QPushButton("应用")

        def _apply():
            new_cd = ClickDelays(default=default_spin.value())
            for attr, spin in sub_spins.items():
                # 拨到最小值（specialValueText 显示）= 用默认 = None
                if spin.value() == spin.minimum():
                    setattr(new_cd, attr, None)
                else:
                    setattr(new_cd, attr, spin.value())
            entry.setter(new_cd)
            self._reload_list()

        btn_apply.clicked.connect(_apply)
        row.addWidget(btn_apply)

        btn_reset_subs = QPushButton("所有分类都用 default")

        def _reset_subs():
            for spin in sub_spins.values():
                spin.setValue(spin.minimum())

        btn_reset_subs.clicked.connect(_reset_subs)
        row.addWidget(btn_reset_subs)
        row.addStretch(1)
        form.addRow("", _wrap(row))

        return w

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
