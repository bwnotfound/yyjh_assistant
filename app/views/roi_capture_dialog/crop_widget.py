"""
CropWidget - 在放大显示的图像上画矩形选框。

设计:
  - 接受 PIL.Image 作为输入，按整数倍 NEAREST 放大显示（保留像素感）
  - 鼠标左键拖拽：画矩形选框；松开后选框固定，再拖一次替换旧框
  - 选框坐标在外部按 *原始图像坐标系* 提供（不是 display 坐标）
  - 选框区域之外覆盖一层半透明遮罩，视觉上突出选中区域

不负责截图、保存等逻辑，纯展示 + 选框组件。
"""

from __future__ import annotations

from typing import Optional

from PIL import Image, ImageQt
from PySide6.QtCore import Qt, QRect, Signal
from PySide6.QtGui import QColor, QMouseEvent, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QWidget


class CropWidget(QWidget):
    """放大显示 + 拖拽矩形选框。"""

    # 选框变化时发射；payload 是原始坐标系的 (x0, y0, x1, y1)，
    # 选框被清空时发射 (0, 0, 0, 0)
    selection_changed = Signal(tuple)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._pil_img: Optional[Image.Image] = None
        self._pixmap: Optional[QPixmap] = None
        self._scale: int = 4

        # 原始坐标系下的最终选框（鼠标松开后才赋值）
        self._selection: Optional[tuple[int, int, int, int]] = None

        # 拖拽中的临时矩形（display 坐标）
        self._drag_start: Optional[tuple[int, int]] = None
        self._drag_current: Optional[tuple[int, int]] = None

        self.setCursor(Qt.CrossCursor)

    # =========================================================================
    # 对外
    # =========================================================================

    def set_image(self, pil_img: Image.Image, scale: int = 4) -> None:
        """更换底图（清空选框）。"""
        self._pil_img = pil_img.convert("RGB")
        self._scale = max(1, int(scale))
        self._regenerate_pixmap()
        self._selection = None
        self._drag_start = None
        self._drag_current = None
        self.selection_changed.emit((0, 0, 0, 0))
        self.update()

    def set_scale(self, scale: int) -> None:
        """改变缩放倍数（保留选框）。"""
        if self._pil_img is None:
            self._scale = max(1, int(scale))
            return
        self._scale = max(1, int(scale))
        self._regenerate_pixmap()
        self.update()

    def selection(self) -> Optional[tuple[int, int, int, int]]:
        """返回原始坐标系下的选框；未拖过则为 None。"""
        return self._selection

    def reset_selection(self) -> None:
        """清空选框（视为"未裁剪"）。"""
        self._selection = None
        self._drag_start = None
        self._drag_current = None
        self.selection_changed.emit((0, 0, 0, 0))
        self.update()

    def has_image(self) -> bool:
        return self._pil_img is not None

    # =========================================================================
    # 内部
    # =========================================================================

    def _regenerate_pixmap(self) -> None:
        if self._pil_img is None:
            self._pixmap = None
            return
        w, h = self._pil_img.size
        if self._scale > 1:
            disp = self._pil_img.resize(
                (w * self._scale, h * self._scale),
                Image.Resampling.NEAREST,
            )
        else:
            disp = self._pil_img
        qimg = ImageQt.ImageQt(disp.convert("RGB"))
        self._pixmap = QPixmap.fromImage(qimg)
        self.setFixedSize(self._pixmap.size())

    def _current_display_rect(self) -> Optional[QRect]:
        """当前要绘制的矩形（display 坐标）。"""
        if self._drag_start is not None and self._drag_current is not None:
            x0, y0 = self._drag_start
            x1, y1 = self._drag_current
            return QRect(min(x0, x1), min(y0, y1), abs(x1 - x0), abs(y1 - y0))
        if self._selection is not None:
            s = self._scale
            ox0, oy0, ox1, oy1 = self._selection
            return QRect(ox0 * s, oy0 * s, (ox1 - ox0) * s, (oy1 - oy0) * s)
        return None

    # =========================================================================
    # 事件
    # =========================================================================

    def paintEvent(self, _ev) -> None:
        if self._pixmap is None:
            return
        p = QPainter(self)
        p.drawPixmap(0, 0, self._pixmap)

        rect = self._current_display_rect()
        if rect is None:
            return

        # 选框外四块用半透明遮罩
        overlay = QColor(0, 0, 0, 110)
        W, H = self.width(), self.height()
        # top / bottom / left / right
        if rect.top() > 0:
            p.fillRect(0, 0, W, rect.top(), overlay)
        if rect.bottom() < H:
            p.fillRect(0, rect.bottom(), W, H - rect.bottom(), overlay)
        if rect.left() > 0:
            p.fillRect(0, rect.top(), rect.left(), rect.height(), overlay)
        if rect.right() < W:
            p.fillRect(
                rect.right(),
                rect.top(),
                W - rect.right(),
                rect.height(),
                overlay,
            )

        # 红色实线框
        pen = QPen(QColor(220, 50, 50), 2)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        p.drawRect(rect)

    def mousePressEvent(self, ev: QMouseEvent) -> None:
        if self._pixmap is None or ev.button() != Qt.LeftButton:
            return
        x = max(0, min(self.width() - 1, ev.pos().x()))
        y = max(0, min(self.height() - 1, ev.pos().y()))
        self._drag_start = (x, y)
        self._drag_current = (x, y)
        # 拖拽开始时清掉旧选框（视觉上立刻看到新框替换旧框）
        self._selection = None
        self.update()

    def mouseMoveEvent(self, ev: QMouseEvent) -> None:
        if self._drag_start is None:
            return
        x = max(0, min(self.width() - 1, ev.pos().x()))
        y = max(0, min(self.height() - 1, ev.pos().y()))
        self._drag_current = (x, y)
        self.update()

    def mouseReleaseEvent(self, ev: QMouseEvent) -> None:
        if self._drag_start is None or ev.button() != Qt.LeftButton:
            return
        if self._drag_current is None or self._pil_img is None:
            self._drag_start = None
            self._drag_current = None
            self.update()
            return

        x0_d, y0_d = self._drag_start
        x1_d, y1_d = self._drag_current
        self._drag_start = None
        self._drag_current = None

        # display → original（向下取整：选框落到完整像素边界上）
        s = self._scale
        x0, x1 = sorted([x0_d // s, x1_d // s])
        y0, y1 = sorted([y0_d // s, y1_d // s])

        w, h = self._pil_img.size
        x0 = max(0, min(w, x0))
        x1 = max(0, min(w, x1))
        y0 = max(0, min(h, y0))
        y1 = max(0, min(h, y1))

        # 太小（点击而非拖拽）忽略
        if (x1 - x0) < 1 or (y1 - y0) < 1:
            self.update()
            return

        self._selection = (x0, y0, x1, y1)
        self.selection_changed.emit(self._selection)
        self.update()
