"""
Routine 编辑器内嵌的两个列表 widget：
  - PathListWidget: MoveStep.path 编辑（每行 gx/gy 整数；飞行点 (-1,-1) 单独标记）
                    支持按住 ⋮⋮ 手柄拖动重排
  - BuyItemsWidget: BuyStep.items 编辑（每行 商品idx/数量 整数）

两者通过 changed 信号通知外层（用于 dirty 标记 + 步骤摘要刷新）。
PathListWidget 通过 ocr_callback 委托外层做 OCR，自身不直接依赖 CoordReader。

PathListWidget 的拖拽实现要点:
  · _PathRowWidget instance 在 reorder 中持续存在 (pool 复用)，避免破坏 grabMouse
  · _DragHandle 在 mousePress 时 grabMouse，让所有后续鼠标事件都路由到它
  · mouseMove 时把全局坐标转 owner 坐标算"插入位置 idx" (按行 mid 为分界)
  · 实时 reorder _points 列表 + 调 _sync_rows 让各 widget 显示新数据
"""

from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)


# 飞行点标记
FLY = (-1, -1)


# OCR 回调签名: 无参,返回 (gx, gy) 整数 tuple;
# 失败请抛异常 (异常 message 会原样显示给用户)。
OcrCallback = Callable[[], tuple[int, int]]


# =============================================================================
# 拖拽手柄
# =============================================================================


class _DragHandle(QLabel):
    """
    每行最左的拖动手柄。按下后 grabMouse 进入持续拖动状态，
    后续 mouseMove 事件不论鼠标移到哪都会送到本 widget；松手时由 owner 收尾。
    """

    def __init__(self, row: "_PathRowWidget") -> None:
        super().__init__("⋮⋮")
        self._row = row
        self.setFixedWidth(22)
        self.setAlignment(Qt.AlignCenter)
        self.setCursor(Qt.OpenHandCursor)
        self.setStyleSheet(
            "QLabel { color: #888; font-size: 14px; "
            "background: transparent; border: none; }"
            "QLabel:hover { color: #333; }"
        )
        self.setToolTip("按住拖动以重排此行")

    def mousePressEvent(self, ev) -> None:
        if ev.button() != Qt.LeftButton:
            return
        idx = self._row.current_idx()
        if idx < 0:
            return
        self.grabMouse()
        self.setCursor(Qt.ClosedHandCursor)
        self._row.owner._begin_drag(idx)

    def mouseMoveEvent(self, ev) -> None:
        owner = self._row.owner
        if not owner._is_dragging():
            return
        # 把局部坐标转成 owner 容器内的 y
        gp = self.mapToGlobal(ev.pos())
        op = owner.mapFromGlobal(gp)
        owner._drag_update(op.y())

    def mouseReleaseEvent(self, ev) -> None:
        if ev.button() != Qt.LeftButton:
            return
        owner = self._row.owner
        if owner._is_dragging():
            self.releaseMouse()
            self.setCursor(Qt.OpenHandCursor)
            owner._end_drag()


# =============================================================================
# 单行 widget
# =============================================================================


class _PathRowWidget(QWidget):
    """
    单行布局:
      普通点: [⋮⋮] [#i] [gx] [N spinbox] [gy] [N spinbox] ............ [🗑]
      飞行点: [⋮⋮] [#i] [✈ 飞行 (-1, -1)] ........................... [🗑]

    通过 set_data(idx, point) 切换两种状态显示。widget instance 在 reorder
    中持续存在,避免破坏拖拽手柄的 grabMouse。
    """

    def __init__(self, owner: "PathListWidget") -> None:
        super().__init__()
        # 给一个 objectName,让 set_drag_highlight 用 #PathRow 选择器精确选中,
        # 避免 stylesheet 级联影响子 widget。
        self.setObjectName("PathRow")
        self.owner = owner  # 公开,供 _DragHandle 直接访问
        self._idx = -1
        self._is_fly = False
        self._build()

    def _build(self) -> None:
        h = QHBoxLayout(self)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(4)

        self._handle = _DragHandle(self)
        h.addWidget(self._handle)

        self._idx_lbl = QLabel("")
        self._idx_lbl.setMinimumWidth(28)
        h.addWidget(self._idx_lbl)

        # 普通点字段（与飞行点 label 互斥显示）
        self._gx_lbl = QLabel("gx")
        h.addWidget(self._gx_lbl)
        self._gx = QSpinBox()
        self._gx.setRange(0, 999)
        self._gx.valueChanged.connect(self._on_gx_changed)
        h.addWidget(self._gx)

        self._gy_lbl = QLabel("gy")
        h.addWidget(self._gy_lbl)
        self._gy = QSpinBox()
        self._gy.setRange(0, 999)
        self._gy.valueChanged.connect(self._on_gy_changed)
        h.addWidget(self._gy)

        # 飞行点 label
        self._fly_lbl = QLabel("✈ 飞行 (-1, -1)")
        self._fly_lbl.setStyleSheet("color: #0066cc; font-weight: bold;")
        h.addWidget(self._fly_lbl)

        h.addStretch(1)

        self._btn_del = QPushButton("🗑")
        self._btn_del.setMaximumWidth(28)
        self._btn_del.setToolTip("删除此行")
        self._btn_del.clicked.connect(self._on_delete)
        h.addWidget(self._btn_del)

    # ---- 公开 ----

    def current_idx(self) -> int:
        return self._idx

    def set_data(self, idx: int, point: tuple[int, int]) -> None:
        self._idx = idx
        self._idx_lbl.setText(f"#{idx + 1}")
        is_fly = point == FLY
        self._is_fly = is_fly
        # 切换可见性
        self._gx_lbl.setVisible(not is_fly)
        self._gx.setVisible(not is_fly)
        self._gy_lbl.setVisible(not is_fly)
        self._gy.setVisible(not is_fly)
        self._fly_lbl.setVisible(is_fly)
        # 同步 spinbox 值（不能触发 valueChanged，否则会写回 _points 形成循环）
        if not is_fly:
            self._gx.blockSignals(True)
            self._gy.blockSignals(True)
            try:
                self._gx.setValue(point[0])
                self._gy.setValue(point[1])
            finally:
                self._gx.blockSignals(False)
                self._gy.blockSignals(False)

    def set_drag_highlight(self, on: bool) -> None:
        """拖拽期间被拖行整体淡蓝高亮"""
        if on:
            self.setStyleSheet(
                "#PathRow { background-color: rgba(0, 100, 255, 50); "
                "border-radius: 3px; }"
            )
        else:
            self.setStyleSheet("")

    # ---- 内部回调 ----

    def _on_gx_changed(self, v: int) -> None:
        if self._idx < 0 or self._is_fly:
            return
        cur = self.owner._points[self._idx]
        self.owner._points[self._idx] = (v, cur[1])
        self.owner.changed.emit()

    def _on_gy_changed(self, v: int) -> None:
        if self._idx < 0 or self._is_fly:
            return
        cur = self.owner._points[self._idx]
        self.owner._points[self._idx] = (cur[0], v)
        self.owner.changed.emit()

    def _on_delete(self) -> None:
        if self._idx >= 0:
            self.owner._delete(self._idx)


# =============================================================================
# PathListWidget
# =============================================================================


class PathListWidget(QWidget):
    """
    MoveStep.path 编辑器。

    顶部按钮: [+ 点] [+ ✈ 飞行] [📷 OCR]
    每行: [⋮⋮ 拖动手柄] [#i] [gx/gy 输入或飞行标记] [🗑 删除]

    重排: 按住 ⋮⋮ 手柄拖动,鼠标跨过相邻行 mid 时整行实时跟随。

    ocr_callback: 无参可调用,返回 (gx, gy)。失败请抛异常 (message 会显示给用户)。
                  None 表示外层未提供,OCR 按钮会禁用。
    """

    changed = Signal()

    def __init__(
        self,
        points: Optional[list[tuple[int, int]]] = None,
        parent=None,
        ocr_callback: Optional[OcrCallback] = None,
    ) -> None:
        super().__init__(parent)
        self._points: list[tuple[int, int]] = list(points or [])
        self._ocr_callback = ocr_callback
        # widget pool: 长度始终 == len(self._points), instance 不在 reorder 中重建
        self._row_widgets: list[_PathRowWidget] = []
        self._drag_src_idx: Optional[int] = None
        self._build_ui()

    # -------- 公开 --------

    def points(self) -> list[tuple[int, int]]:
        return list(self._points)

    def set_points(self, points: list[tuple[int, int]]) -> None:
        self._points = list(points or [])
        self._sync_rows()

    # -------- 构造 --------

    def _build_ui(self) -> None:
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(2)

        # 顶部按钮
        top = QHBoxLayout()
        top.addWidget(QLabel("path（按顺序执行；按住 ⋮⋮ 拖动重排）"))
        top.addStretch(1)
        btn_add = QPushButton("+ 点")
        btn_add.clicked.connect(self._add_normal)
        top.addWidget(btn_add)
        btn_fly = QPushButton("+ ✈ 飞行")
        btn_fly.setToolTip("飞行点 (-1, -1) ：在当前位置点击施展轻功传送到下一个点")
        btn_fly.clicked.connect(self._add_fly)
        top.addWidget(btn_fly)
        btn_ocr = QPushButton("📷 OCR")
        if self._ocr_callback is None:
            btn_ocr.setEnabled(False)
            btn_ocr.setToolTip("OCR 不可用：编辑器初始化时未提供回调")
        else:
            btn_ocr.setToolTip(
                "调用 OCR 读取游戏内当前小地图坐标，作为新一行追加到 path 末尾"
            )
        btn_ocr.clicked.connect(self._add_from_ocr)
        top.addWidget(btn_ocr)
        self._layout.addLayout(top)

        # 行容器
        self._rows_layout = QVBoxLayout()
        self._rows_layout.setSpacing(2)
        self._layout.addLayout(self._rows_layout)

        self._sync_rows()

    def _sync_rows(self) -> None:
        """
        把 widget pool 长度对齐到 _points,然后给所有 widget 调 set_data 更新显示。
        widget instance 在 reorder 中保持不变,避免破坏 grabMouse。
        """
        # 新增 widget
        while len(self._row_widgets) < len(self._points):
            w = _PathRowWidget(self)
            self._rows_layout.addWidget(w)
            self._row_widgets.append(w)
        # 移除多余 widget
        while len(self._row_widgets) > len(self._points):
            w = self._row_widgets.pop()
            self._rows_layout.removeWidget(w)
            w.setParent(None)
            w.deleteLater()
        # 同步显示数据
        for i, p in enumerate(self._points):
            self._row_widgets[i].set_data(i, p)
        # 同步拖拽高亮
        self._refresh_drag_highlight()

    # -------- 增删 --------

    def _add_normal(self) -> None:
        self._points.append((0, 0))
        self._sync_rows()
        self.changed.emit()

    def _add_fly(self) -> None:
        self._points.append(FLY)
        self._sync_rows()
        self.changed.emit()

    def _add_from_ocr(self) -> None:
        """
        触发外部 OCR 回调，把读到的 (gx, gy) 追加到 path 末尾。
        失败时把异常 message 弹给用户，path 不变。
        """
        if self._ocr_callback is None:
            QMessageBox.information(
                self,
                "OCR 未启用",
                "未提供 OCR 回调（编辑器构造时未传入）。",
            )
            return
        try:
            coord = self._ocr_callback()
        except Exception as e:
            QMessageBox.warning(
                self,
                "OCR 失败",
                f"{type(e).__name__}: {e}",
            )
            return

        if (
            not isinstance(coord, tuple)
            or len(coord) != 2
            or not all(isinstance(v, int) for v in coord)
        ):
            QMessageBox.warning(
                self,
                "OCR 返回值异常",
                f"期望 (gx, gy) 整数 tuple，实际收到: {coord!r}",
            )
            return

        self._points.append((int(coord[0]), int(coord[1])))
        self._sync_rows()
        self.changed.emit()

    def _delete(self, idx: int) -> None:
        if 0 <= idx < len(self._points):
            self._points.pop(idx)
            self._sync_rows()
            self.changed.emit()

    # -------- 拖拽 --------

    def _is_dragging(self) -> bool:
        return self._drag_src_idx is not None

    def _begin_drag(self, idx: int) -> None:
        if not (0 <= idx < len(self._points)):
            return
        self._drag_src_idx = idx
        self._refresh_drag_highlight()

    def _drag_update(self, owner_y: int) -> None:
        """根据鼠标 y 在 owner 内的位置实时 reorder。"""
        if self._drag_src_idx is None:
            return
        target = self._insert_idx_at_y(owner_y)
        src = self._drag_src_idx
        # target == src 或 target == src+1 都等价于"插到自己附近 = 不变"
        if target == src or target == src + 1:
            return
        item = self._points.pop(src)
        # pop 后原 target 之后的元素左移一位,所以 target > src 时要 -1
        if target > src:
            target -= 1
        self._points.insert(target, item)
        self._drag_src_idx = target
        self._sync_rows()
        self.changed.emit()

    def _end_drag(self) -> None:
        self._drag_src_idx = None
        self._refresh_drag_highlight()

    def _insert_idx_at_y(self, y: int) -> int:
        """
        把 owner 内 y 坐标转成"插入位置 idx",范围 [0, n]。
        - y 在第 i 行 mid 之上 → i  (插到第 i 行之前)
        - y 超过最后一行 mid     → n  (插到最后)
        """
        n = len(self._points)
        if n == 0:
            return 0
        for i, w in enumerate(self._row_widgets):
            geom = w.geometry()
            mid = geom.top() + geom.height() / 2
            if y < mid:
                return i
        return n

    def _refresh_drag_highlight(self) -> None:
        for i, w in enumerate(self._row_widgets):
            w.set_drag_highlight(i == self._drag_src_idx)


# =============================================================================
# BuyItemsWidget （未变）
# =============================================================================


class BuyItemsWidget(QWidget):
    """
    BuyStep.items 编辑器。每行: [#i] [商品idx] [数量] [↑] [↓] [🗑]
    """

    changed = Signal()

    def __init__(
        self,
        items: Optional[list[tuple[int, int]]] = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._items: list[tuple[int, int]] = list(items or [])
        self._build_ui()

    def items(self) -> list[tuple[int, int]]:
        return list(self._items)

    def set_items(self, items: list[tuple[int, int]]) -> None:
        self._items = list(items or [])
        self._refresh_rows()

    def _build_ui(self) -> None:
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(2)

        top = QHBoxLayout()
        top.addWidget(QLabel("items（每行: 商品idx 数量）"))
        top.addStretch(1)
        btn_add = QPushButton("+ 商品")
        btn_add.clicked.connect(self._add)
        top.addWidget(btn_add)
        self._layout.addLayout(top)

        self._rows_layout = QVBoxLayout()
        self._rows_layout.setSpacing(2)
        self._layout.addLayout(self._rows_layout)

        self._refresh_rows()

    def _refresh_rows(self) -> None:
        while self._rows_layout.count():
            item = self._rows_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
        for i, (idx, qty) in enumerate(self._items):
            row = self._build_row(i, idx, qty)
            self._rows_layout.addWidget(row)

    def _build_row(self, row_idx: int, item_idx: int, qty: int) -> QWidget:
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(4)

        idx_lbl = QLabel(f"#{row_idx + 1}")
        idx_lbl.setMinimumWidth(28)
        h.addWidget(idx_lbl)

        s_idx = QSpinBox()
        s_idx.setRange(1, 99)
        s_idx.setValue(item_idx)

        s_qty = QSpinBox()
        s_qty.setRange(1, 99)
        s_qty.setValue(qty)

        def _on_idx_changed(v: int, i=row_idx, qty_ref=s_qty):
            self._items[i] = (v, qty_ref.value())
            self.changed.emit()

        def _on_qty_changed(v: int, i=row_idx, idx_ref=s_idx):
            self._items[i] = (idx_ref.value(), v)
            self.changed.emit()

        s_idx.valueChanged.connect(_on_idx_changed)
        s_qty.valueChanged.connect(_on_qty_changed)

        h.addWidget(QLabel("商品idx"))
        h.addWidget(s_idx)
        h.addWidget(QLabel("数量"))
        h.addWidget(s_qty)
        h.addStretch(1)

        btn_up = QPushButton("↑")
        btn_up.setMaximumWidth(28)
        btn_up.setEnabled(row_idx > 0)
        btn_up.clicked.connect(lambda _=False, i=row_idx: self._move_up(i))
        h.addWidget(btn_up)

        btn_down = QPushButton("↓")
        btn_down.setMaximumWidth(28)
        btn_down.setEnabled(row_idx < len(self._items) - 1)
        btn_down.clicked.connect(lambda _=False, i=row_idx: self._move_down(i))
        h.addWidget(btn_down)

        btn_del = QPushButton("🗑")
        btn_del.setMaximumWidth(28)
        btn_del.clicked.connect(lambda _=False, i=row_idx: self._delete(i))
        h.addWidget(btn_del)

        return w

    def _add(self) -> None:
        self._items.append((1, 1))
        self._refresh_rows()
        self.changed.emit()

    def _delete(self, idx: int) -> None:
        if 0 <= idx < len(self._items):
            self._items.pop(idx)
            self._refresh_rows()
            self.changed.emit()

    def _move_up(self, idx: int) -> None:
        if idx > 0:
            self._items[idx], self._items[idx - 1] = (
                self._items[idx - 1],
                self._items[idx],
            )
            self._refresh_rows()
            self.changed.emit()

    def _move_down(self, idx: int) -> None:
        if idx < len(self._items) - 1:
            self._items[idx], self._items[idx + 1] = (
                self._items[idx + 1],
                self._items[idx],
            )
            self._refresh_rows()
            self.changed.emit()
