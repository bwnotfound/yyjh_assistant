"""
点击位置截图显示工具

输入位置 → 截游戏当前画面 → 在对应屏幕位置画红圈 → 弹预览窗口。

三种输入模式（顶部下拉切换，下方输入栏跟随变化）:
  1. 绝对像素 (px, py)        : 截图像素坐标，参考截图实际尺寸
  2. 归一化比例 (nx, ny)      : [0, 1]² 屏幕归一化
  3. 格子数 offset (dx, dy)   : 等距投影后的目标点；dx 沿地图 +x（屏幕右下），
                                dy 沿地图 +y（屏幕左下）

格子模式下基准来源:
  - 自适应地图 (复选框开启,默认关闭):
        OCR 读出当前角色格坐标 → 配合选定地图的 map_size + 视野档位 →
        贴边修正后的角色屏幕位置作为投影基准。
        要求选定地图必须配置了 map_size，否则截图前弹窗提示。
  - 手动模式 (复选框关闭):
        基准 = character_pos     : 直接以 movement_profile.character_pos 为基准
                                    投影，不考虑贴边
        基准 = 手动输入起点格     : (sx, sy) + 视野 + 当前地图 → 走 mover 同款
                                    贴边修正

实现上把 mover.Mover._character_screen_pos 的等距投影 + 贴边修正逻辑就地复制了
一份（输入数据形态不同，mover 那边面向 MapContext，这边面向 UI 表单，整合反而
引入耦合）。OCR 链路构造参考 routine_editor_dialog._get_coord_reader。

Adaptive 模式下，OCR 截图与最终标注截图复用同一帧，避免角色位移导致圈不对位。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from PIL import Image, ImageDraw, ImageFont, ImageQt
from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from utils import Mumu

from app.core.ocr import CoordReader, TemplateOCR
from app.core.profiles import (
    DEFAULT_MOVEMENT_YAML_PATH,
    MovementProfile,
    MovementRegistry,
    VisionSpec,
    compute_character_screen_pos,
)
from config.common.map_registry import (
    DEFAULT_YAML_PATH as MAP_REGISTRY_PATH,
    MapRegistry,
    Profile as MapProfile,
)

log = logging.getLogger(__name__)


# 字符模板目录（与 routine_editor_dialog / roi_capture_dialog 保持一致）
MINIMAP_TEMPLATE_DIR = Path("config/templates/minimap_coord")


class ClickPreviewDialog(QDialog):
    def __init__(self, mumu: Mumu, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("点击位置截图显示")
        self.resize(600, 540)

        self._mumu = mumu
        self._movement_profile: Optional[MovementProfile] = None
        self._map_profile: Optional[MapProfile] = None
        self._load_profiles()

        # OCR 链路 lazy 构造 + cache
        self._template_ocr: Optional[TemplateOCR] = None
        self._coord_reader: Optional[CoordReader] = None

        # pixel/adaptive 模式下，_resolve_*() 会顺手截一张图（pixel 用来算 px→nx，
        # adaptive 把同一帧喂给 OCR），标注时复用避免再截。用完即设回 None，
        # 防止跨模式残留。
        self._cached_capture: Optional[Image.Image] = None

        self._build_ui()
        self._on_mode_changed(0)

    # =========================================================================
    # 配置加载
    # =========================================================================

    def _load_profiles(self) -> None:
        key = f"{self._mumu.device_w}x{self._mumu.device_h}"
        try:
            mov_reg = MovementRegistry.load(DEFAULT_MOVEMENT_YAML_PATH)
            self._movement_profile = mov_reg.profiles.get(key)
        except Exception as e:
            log.warning("加载 movement_profile 失败: %s", e)
        try:
            map_reg = MapRegistry.load(MAP_REGISTRY_PATH)
            self._map_profile = map_reg.profiles.get(key)
        except Exception as e:
            log.warning("加载 map_registry 失败: %s", e)

    # =========================================================================
    # OCR 链路（lazy）
    # =========================================================================

    def _get_coord_reader(self) -> CoordReader:
        """Lazy 构造 CoordReader。失败抛 RuntimeError，message 直接给用户看。"""
        if self._coord_reader is not None:
            return self._coord_reader
        if self._movement_profile is None:
            raise RuntimeError(
                f"未配置当前分辨率（{self._mumu.device_w}×{self._mumu.device_h}）"
                "的运动配置。请先在主界面「运动配置」里录入。"
            )
        roi = self._movement_profile.minimap_coord_roi
        if roi is None:
            raise RuntimeError(
                "运动配置里未录入 minimap_coord_roi（小地图坐标 ROI）。\n"
                "请先用主界面「ROI 截取工具」录入；该工具会自动同步到运动配置。"
            )
        if self._template_ocr is None:
            try:
                self._template_ocr = TemplateOCR.from_dir(MINIMAP_TEMPLATE_DIR)
            except FileNotFoundError as e:
                raise RuntimeError(
                    f"OCR 字符模板目录无可用模板: {MINIMAP_TEMPLATE_DIR}\n"
                    f"原因: {e}\n"
                    "请先用主界面「ROI 截取工具」录入字符模板（0~9 + ( ) ,）。"
                ) from e
            except Exception as e:
                raise RuntimeError(f"OCR 模板加载失败: {type(e).__name__}: {e}") from e
        self._coord_reader = CoordReader(
            mumu=self._mumu,
            ocr=self._template_ocr,
            roi_norm=roi,
        )
        return self._coord_reader

    # =========================================================================
    # UI 构造
    # =========================================================================

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(8)

        tip = QLabel(
            "选择输入模式 → 填入位置 → 点「截图并标注」，"
            "将在游戏截图上以红圈标出对应屏幕位置。"
        )
        tip.setWordWrap(True)
        root.addWidget(tip)

        # 模式选择
        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("输入模式:"))
        self._mode_combo = QComboBox()
        self._mode_combo.addItem("绝对像素 (px, py)", "pixel")
        self._mode_combo.addItem("归一化比例 (nx, ny)", "norm")
        self._mode_combo.addItem("格子数 offset (dx, dy)", "tile")
        self._mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        mode_row.addWidget(self._mode_combo, 1)
        root.addLayout(mode_row)

        # 各模式的输入面板叠在 stack 里
        self._stack = QStackedWidget()
        self._stack.addWidget(self._build_pixel_panel())
        self._stack.addWidget(self._build_norm_panel())
        self._stack.addWidget(self._build_tile_panel())
        root.addWidget(self._stack)

        root.addStretch(1)

        # 状态/解析结果展示
        self._lbl_status = QLabel("")
        self._lbl_status.setStyleSheet("color: #555; font-size: 11px;")
        self._lbl_status.setWordWrap(True)
        root.addWidget(self._lbl_status)

        # 底部按钮
        btns = QHBoxLayout()
        self._btn_capture = QPushButton("截图并标注")
        self._btn_capture.setMinimumHeight(36)
        self._btn_capture.clicked.connect(self._on_capture)
        btns.addWidget(self._btn_capture)
        btns.addStretch(1)
        btn_close = QPushButton("关闭")
        btn_close.clicked.connect(self.close)
        btns.addWidget(btn_close)
        root.addLayout(btns)

    def _build_pixel_panel(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        form.setContentsMargins(0, 8, 0, 0)

        hint = QLabel(
            f"截图分辨率参考: {self._mumu.device_w} × {self._mumu.device_h}"
            "（实际截图尺寸可能因 DPI 不同，本工具用截图实际尺寸归一化）"
        )
        hint.setStyleSheet("color: #888; font-size: 11px;")
        hint.setWordWrap(True)
        form.addRow("", hint)

        self._px_spin = QSpinBox()
        self._px_spin.setRange(0, 99999)
        form.addRow("px (横向像素):", self._px_spin)

        self._py_spin = QSpinBox()
        self._py_spin.setRange(0, 99999)
        form.addRow("py (纵向像素):", self._py_spin)
        return w

    def _build_norm_panel(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        form.setContentsMargins(0, 8, 0, 0)

        hint = QLabel("范围 [0, 1]：(0, 0) 左上角，(1, 1) 右下角")
        hint.setStyleSheet("color: #888; font-size: 11px;")
        form.addRow("", hint)

        self._nx_spin = QDoubleSpinBox()
        self._nx_spin.setRange(0.0, 1.0)
        self._nx_spin.setDecimals(4)
        self._nx_spin.setSingleStep(0.001)
        self._nx_spin.setValue(0.5)
        form.addRow("nx (归一化 x):", self._nx_spin)

        self._ny_spin = QDoubleSpinBox()
        self._ny_spin.setRange(0.0, 1.0)
        self._ny_spin.setDecimals(4)
        self._ny_spin.setSingleStep(0.001)
        self._ny_spin.setValue(0.5)
        form.addRow("ny (归一化 y):", self._ny_spin)
        return w

    def _build_tile_panel(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        form.setContentsMargins(0, 8, 0, 0)

        hint = QLabel(
            "等距投影 (2:1)：dx 沿地图 +x（屏幕右下），dy 沿地图 +y（屏幕左下）。\n"
            "公式: screen = base + (dx*bw/2 - dy*bw/2, dx*bh/2 + dy*bh/2)"
        )
        hint.setStyleSheet("color: #888; font-size: 11px;")
        hint.setWordWrap(True)
        form.addRow("", hint)

        # 自适应复选框（默认关闭）
        # 开启后自动 OCR 当前坐标 + 贴边修正，覆盖"基准 / 起点格"的手动输入
        self._tile_adaptive_check = QCheckBox("自适应地图（OCR 当前坐标 + 贴边修正）")
        self._tile_adaptive_check.toggled.connect(
            lambda _: self._refresh_tile_visibility()
        )
        form.addRow("", self._tile_adaptive_check)

        # 视野档位（始终启用；自适应和手动模式都需要）
        self._tile_vision_combo = QComboBox()
        if self._movement_profile is not None and self._movement_profile.vision_sizes:
            for vname in self._movement_profile.vision_sizes.keys():
                self._tile_vision_combo.addItem(vname)
        else:
            for vname in ("小", "中", "大"):
                self._tile_vision_combo.addItem(vname)
        form.addRow("视野档位:", self._tile_vision_combo)

        # 基准下拉（自适应模式下整体禁用）
        self._tile_base_combo = QComboBox()
        self._tile_base_combo.addItem("角色屏幕位置 (character_pos)", "character")
        self._tile_base_combo.addItem("手动输入起点格 (做贴边修正)", "manual_tile")
        self._tile_base_combo.currentIndexChanged.connect(
            lambda _: self._refresh_tile_visibility()
        )
        form.addRow("基准:", self._tile_base_combo)

        # 起点格（仅手动 manual_tile 模式启用）
        self._tile_src_x = QSpinBox()
        self._tile_src_x.setRange(0, 999)
        self._tile_src_y = QSpinBox()
        self._tile_src_y.setRange(0, 999)
        src_row = QHBoxLayout()
        src_row.addWidget(QLabel("gx"))
        src_row.addWidget(self._tile_src_x)
        src_row.addWidget(QLabel("gy"))
        src_row.addWidget(self._tile_src_y)
        src_row.addStretch(1)
        self._tile_src_w = _wrap(src_row)
        form.addRow("起点格:", self._tile_src_w)

        # 当前地图（adaptive 必填且必须有 map_size；手动 manual_tile 可选，
        # 没选/选了无 map_size 的退化为不做贴边修正）
        # 列出所有 location（含没 map_size 的，加 "(无 map_size)" 后缀），
        # 这样自适应模式下选错了能在截图前被弹窗拦下，符合"提醒先设置 map_size"语义。
        map_row = QHBoxLayout()
        self._tile_map_combo = QComboBox()
        self._tile_map_combo.addItem("(不指定)", None)
        if self._map_profile is not None:
            for name, rec in sorted(self._map_profile.locations.items()):
                # 跳过纯占位项（DEFAULT_LOCATIONS 里既无录入信息也无 map_size 的）
                if not rec.is_recorded and rec.map_size is None:
                    continue
                suffix = "" if rec.map_size is not None else "  (无 map_size)"
                self._tile_map_combo.addItem(f"{name}{suffix}", name)
        self._tile_map_combo.currentIndexChanged.connect(
            lambda _: self._refresh_map_size_label()
        )
        map_row.addWidget(self._tile_map_combo, 1)

        self._tile_map_size_label = QLabel("")
        self._tile_map_size_label.setStyleSheet("color: #555; font-size: 11px;")
        self._tile_map_size_label.setMinimumWidth(140)
        map_row.addWidget(self._tile_map_size_label)
        form.addRow("当前地图:", _wrap(map_row))

        # dx / dy
        self._tile_dx = QSpinBox()
        self._tile_dx.setRange(-99, 99)
        self._tile_dx.setValue(0)
        self._tile_dy = QSpinBox()
        self._tile_dy.setRange(-99, 99)
        self._tile_dy.setValue(0)
        d_row = QHBoxLayout()
        d_row.addWidget(QLabel("dx"))
        d_row.addWidget(self._tile_dx)
        d_row.addWidget(QLabel("dy"))
        d_row.addWidget(self._tile_dy)
        d_row.addStretch(1)
        form.addRow("offset (格数):", _wrap(d_row))

        # 初始化可见性
        self._refresh_tile_visibility()
        self._refresh_map_size_label()
        return w

    def _on_mode_changed(self, idx: int) -> None:
        self._stack.setCurrentIndex(idx)
        self._lbl_status.setText("")
        self._cached_capture = None

    def _refresh_tile_visibility(self) -> None:
        """根据 adaptive 复选框 + 基准下拉刷新各控件 enable 状态。

        所有 widget 的 enable/disable 决策都集中在这里，避免多源更新打架。
        """
        adaptive = self._tile_adaptive_check.isChecked()
        base_kind = self._tile_base_combo.currentData()

        # adaptive 模式下，基准下拉和起点格全部失效（其值被忽略）
        self._tile_base_combo.setEnabled(not adaptive)
        self._tile_src_w.setEnabled(not adaptive and base_kind == "manual_tile")

        # 当前地图：adaptive 必填；手动模式下只在 manual_tile 下有意义
        self._tile_map_combo.setEnabled(adaptive or base_kind == "manual_tile")

    def _refresh_map_size_label(self) -> None:
        name = self._tile_map_combo.currentData()
        if name is None or self._map_profile is None:
            self._tile_map_size_label.setText("")
            return
        rec = self._map_profile.locations.get(name)
        if rec is None or rec.map_size is None:
            self._tile_map_size_label.setText("(未配 map_size)")
            return
        self._tile_map_size_label.setText(
            f"map_size: {rec.map_size[0]} × {rec.map_size[1]}"
        )

    # =========================================================================
    # 解析输入 → 归一化坐标
    # =========================================================================

    def _resolve_norm(self) -> Optional[tuple[float, float, str]]:
        mode = self._mode_combo.currentData()
        if mode == "pixel":
            return self._resolve_pixel()
        if mode == "norm":
            nx = self._nx_spin.value()
            ny = self._ny_spin.value()
            return (nx, ny, f"norm=({nx:.4f}, {ny:.4f})")
        if mode == "tile":
            return self._resolve_tile()
        return None

    def _resolve_pixel(self) -> Optional[tuple[float, float, str]]:
        """像素模式下用截图实际尺寸归一化（DPI 缩放下 device_w/h 不准）"""
        try:
            img = self._mumu.capture_window()
        except Exception as e:
            log.exception("截图失败")
            QMessageBox.critical(self, "截图失败", f"{type(e).__name__}: {e}")
            return None
        px = self._px_spin.value()
        py = self._py_spin.value()
        w, h = img.size
        if px > w or py > h:
            QMessageBox.warning(
                self,
                "超出截图范围",
                f"输入像素 ({px}, {py}) 超出截图尺寸 {w}×{h}。",
            )
            return None
        nx = px / w if w > 0 else 0.0
        ny = py / h if h > 0 else 0.0
        self._cached_capture = img
        return (nx, ny, f"px=({px}, {py}) on {w}×{h} → norm=({nx:.4f}, {ny:.4f})")

    def _resolve_tile(self) -> Optional[tuple[float, float, str]]:
        if self._movement_profile is None:
            QMessageBox.warning(
                self,
                "缺少运动配置",
                f"未找到分辨率 {self._mumu.device_w}×{self._mumu.device_h} "
                "的运动配置，请先在主界面「运动配置」录入。",
            )
            return None

        vname = self._tile_vision_combo.currentText()
        spec = self._movement_profile.vision_sizes.get(vname)
        if spec is None:
            QMessageBox.warning(
                self,
                "视野档位未配置",
                f"运动配置里没有视野档位「{vname}」，请先去「运动配置」录入。",
            )
            return None

        bw, bh = spec.block_size
        dx = self._tile_dx.value()
        dy = self._tile_dy.value()

        if self._tile_adaptive_check.isChecked():
            base_pt = self._resolve_tile_adaptive(spec)
        else:
            base_pt = self._resolve_tile_manual(spec)
        if base_pt is None:
            return None
        cx, cy, base_label = base_pt

        nx = cx + dx * bw / 2 - dy * bw / 2
        ny = cy + dx * bh / 2 + dy * bh / 2
        label = (
            f"dx={dx}, dy={dy}, vision={vname}, "
            f"block_size=({bw:.4f}, {bh:.4f}), {base_label}, "
            f"base=({cx:.4f}, {cy:.4f}) → target=({nx:.4f}, {ny:.4f})"
        )

        if not (0.0 <= nx <= 1.0 and 0.0 <= ny <= 1.0):
            ans = QMessageBox.question(
                self,
                "目标超出屏幕",
                f"算出的归一化坐标 ({nx:.4f}, {ny:.4f}) 超出 [0, 1]² —— "
                "实际游戏不会响应该位置的点击。\n\n仍要继续显示标注吗？",
            )
            if ans != QMessageBox.Yes:
                return None
        return (nx, ny, label)

    def _resolve_tile_adaptive(
        self, spec: VisionSpec
    ) -> Optional[tuple[float, float, str]]:
        """
        Adaptive 分支：选定地图必须有 map_size；OCR 取当前坐标 → 贴边修正后
        作为投影基准。一次截图同时喂 OCR 和后续标注，避免角色位移。

        失败已弹错，返回 None。
        """
        map_name = self._tile_map_combo.currentData()
        if map_name is None:
            QMessageBox.warning(
                self,
                "缺少当前地图",
                "自适应模式下必须选择「当前地图」。\n"
                "如果当前地图未录入，请在主界面「添加地图信息」中先录入。",
            )
            return None
        rec = (
            self._map_profile.locations.get(map_name)
            if self._map_profile is not None
            else None
        )
        if rec is None or rec.map_size is None:
            QMessageBox.warning(
                self,
                "缺少 map_size",
                f"地图「{map_name}」尚未配置 map_size。\n\n"
                "请先在主界面「添加地图信息」里录入 map_size，"
                "或关闭「自适应地图」检查项后使用手动基准。",
            )
            return None
        map_size = rec.map_size

        # 一次截图同时给 OCR 和后续标注用
        try:
            img = self._mumu.capture_window()
        except Exception as e:
            log.exception("截图失败")
            QMessageBox.critical(self, "截图失败", f"{type(e).__name__}: {e}")
            return None

        try:
            reader = self._get_coord_reader()
        except RuntimeError as e:
            QMessageBox.warning(self, "OCR 不可用", str(e))
            return None

        try:
            coord, raw_text, _roi = reader.read_verbose(image=img)
        except Exception as e:
            log.exception("OCR 调用异常")
            QMessageBox.critical(self, "OCR 调用异常", f"{type(e).__name__}: {e}")
            return None

        if coord is None:
            QMessageBox.warning(
                self,
                "OCR 读不到坐标",
                f"OCR 拼出的原始字符串: {raw_text!r}\n\n"
                "可能原因:\n"
                "  · 游戏小地图当前未显示坐标数字\n"
                "  · ROI 框错位置（minimap_coord_roi 需要重录）\n"
                "  · 字符模板需要重新校准",
            )
            return None

        gx, gy = coord
        cx, cy = self._character_screen_pos((gx, gy), spec, map_size)
        base_label = (
            f"基准=自适应OCR({gx},{gy}) " f"on {map_name}({map_size[0]}×{map_size[1]})"
        )
        log.info(
            "adaptive: OCR=(%d, %d), map=%s%s, base=(%.4f, %.4f)",
            gx,
            gy,
            map_name,
            map_size,
            cx,
            cy,
        )
        self._cached_capture = img
        return (cx, cy, base_label)

    def _resolve_tile_manual(
        self, spec: VisionSpec
    ) -> Optional[tuple[float, float, str]]:
        """手动分支：character_pos 直投 / manual_tile + 起点格 + 可选地图。"""
        assert self._movement_profile is not None
        base_kind = self._tile_base_combo.currentData()
        if base_kind == "character":
            cx, cy = self._movement_profile.character_pos
            return (cx, cy, "基准=character_pos")

        # manual_tile
        sx = self._tile_src_x.value()
        sy = self._tile_src_y.value()
        map_name = self._tile_map_combo.currentData()
        map_size: Optional[tuple[int, int]] = None
        if map_name and self._map_profile is not None:
            rec = self._map_profile.locations.get(map_name)
            if rec is not None:
                map_size = rec.map_size
        cx, cy = self._character_screen_pos((sx, sy), spec, map_size)
        base_label = f"基准=起点格({sx},{sy}) on {map_name or '无地图(无贴边修正)'}"
        return (cx, cy, base_label)

    def _character_screen_pos(
        self,
        pre_pos: tuple[int, int],
        vision: VisionSpec,
        map_size: Optional[tuple[int, int]],
    ) -> tuple[float, float]:
        """
        分发: 配了 map_view_area 用几何算法 (compute_character_screen_pos),
        否则回退到 _character_screen_pos_legacy (与 mover.Mover 同款的
        vision_delta_limit 经验算法)。

        map_size=None 时不做贴边修正，返回 character_pos。
        """
        assert self._movement_profile is not None
        cp = self._movement_profile.character_pos
        if map_size is None:
            return cp

        view_area = self._movement_profile.map_view_area
        if view_area is not None:
            return compute_character_screen_pos(
                pre_pos, map_size, vision.block_size, cp, view_area
            )
        return self._character_screen_pos_legacy(pre_pos, vision, map_size)

    def _character_screen_pos_legacy(
        self,
        pre_pos: tuple[int, int],
        vision: VisionSpec,
        map_size: tuple[int, int],
    ) -> tuple[float, float]:
        """
        旧的 vision_delta_limit 经验算法 (与 mover.Mover._character_screen_pos
        同款，含 SE 角 +2 偏移和 (mw, mh) 边界细节)。仅在 map_view_area 未配置
        时作为回退。如果 mover 那边逻辑变了，这两份要同步更新。
        """
        assert self._movement_profile is not None
        cx, cy = self._movement_profile.character_pos
        bw, bh = vision.block_size
        vdl = vision.vision_delta_limit
        mw, mh = map_size

        corners = (
            (0, 0),
            (mw, 0),
            (0, mh),
            (mw - 1, mh - 1),
        )
        offsets = (
            (0.0, -0.5),  # NW
            (0.5, 0.0),  # NE
            (-0.5, 0.0),  # SW
            (0.0, 0.5),  # SE
        )

        min_delta = None
        min_idx: Optional[int] = None
        for i, corner in enumerate(corners):
            d = abs(corner[0] - pre_pos[0]) + abs(corner[1] - pre_pos[1])
            if d > vdl:
                continue
            if min_delta is None or d < min_delta:
                min_delta = d
                min_idx = i

        if min_idx is None:
            return (cx, cy)

        offset_unit = vdl - min_delta
        if min_idx == 3:  # SE 角的特殊处理（与 mover 一致）
            real_delta = abs(pre_pos[0] - mw) + abs(pre_pos[1] - mh)
            offset_unit = vdl - real_delta + 2

        ox, oy = offsets[min_idx]
        return (cx + ox * bw * offset_unit, cy + oy * bh * offset_unit)

    # =========================================================================
    # 截图 + 标注 + 弹窗
    # =========================================================================

    def _on_capture(self) -> None:
        resolved = self._resolve_norm()
        if resolved is None:
            return
        nx, ny, label = resolved

        # pixel/adaptive 模式 _resolve_*() 已经截过；其他模式现截
        img = self._cached_capture
        self._cached_capture = None
        if img is None:
            try:
                img = self._mumu.capture_window()
            except Exception as e:
                log.exception("截图失败")
                QMessageBox.critical(self, "截图失败", f"{type(e).__name__}: {e}")
                return

        annotated = _annotate_circle(img, (nx, ny), label_text=label)
        self._lbl_status.setText(label)
        log.info("点击位置预览: %s", label)
        _SnapshotPreview(annotated, title="点击位置预览", parent=self).exec()


# =============================================================================
# Helpers
# =============================================================================


def _wrap(layout) -> QWidget:
    w = QWidget()
    w.setLayout(layout)
    return w


def _annotate_circle(
    img: Image.Image,
    norm_pos: tuple[float, float],
    label_text: str = "",
) -> Image.Image:
    """
    在截图上画红圈 + 十字 + 文字。norm_pos 允许超出 [0,1]（用户已在
    _resolve_tile 里确认要继续展示），超出部分会被 PIL 裁掉。
    """
    w, h = img.size
    out = img.copy().convert("RGB")
    draw = ImageDraw.Draw(out)

    cx = int(norm_pos[0] * w)
    cy = int(norm_pos[1] * h)
    radius = max(14, min(w, h) // 50)

    # 红圆
    draw.ellipse(
        [(cx - radius, cy - radius), (cx + radius, cy + radius)],
        outline=(255, 40, 40),
        width=3,
    )
    # 中心十字
    cross = radius // 2
    draw.line([(cx - cross, cy), (cx + cross, cy)], fill=(255, 40, 40), width=2)
    draw.line([(cx, cy - cross), (cx, cy + cross)], fill=(255, 40, 40), width=2)

    # 标签
    try:
        font = ImageFont.truetype("msyh.ttc", size=max(14, min(w, h) // 60))
    except Exception:
        font = ImageFont.load_default()
    text = label_text or f"({norm_pos[0]:.4f}, {norm_pos[1]:.4f})"

    # 标签默认放圆右上方；越界则放左下
    tx, ty = cx + radius + 6, cy - radius
    try:
        bbox = draw.textbbox((tx, ty), text, font=font)
        text_w = bbox[2] - bbox[0]
        text_h = bbox[3] - bbox[1]
    except Exception:
        text_w, text_h = len(text) * 12, 16
    if tx + text_w > w:
        tx = max(0, cx - radius - text_w - 6)
    if ty < 0:
        ty = cy + radius + 6
    # 半透明黑底
    try:
        bg = (max(0, tx - 2), max(0, ty - 2), tx + text_w + 2, ty + text_h + 2)
        draw.rectangle(bg, fill=(0, 0, 0))
    except Exception:
        pass
    draw.text((tx, ty), text, fill=(255, 220, 50), font=font)
    return out


class _SnapshotPreview(QDialog):
    """简单截图预览（仿 map_registry_dialog 同名类，仅展示 + OK）"""

    def __init__(self, img: Image.Image, title: str, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)

        max_w, max_h = 1200, 800
        w, h = img.size
        scale = min(max_w / w, max_h / h, 1.0)
        disp_w, disp_h = int(w * scale), int(h * scale)
        if scale < 1.0:
            img = img.resize((disp_w, disp_h), Image.Resampling.LANCZOS)

        self.resize(disp_w + 24, disp_h + 72)

        lay = QVBoxLayout(self)
        lbl = QLabel()
        qimg = ImageQt.ImageQt(img)
        lbl.setPixmap(QPixmap.fromImage(qimg))
        lay.addWidget(lbl)

        btns = QDialogButtonBox(QDialogButtonBox.Ok)
        btns.accepted.connect(self.accept)
        lay.addWidget(btns)
