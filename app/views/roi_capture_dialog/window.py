"""
ROI / 字符模板 截取工具

工作流:
  1. 点「① 取大致范围」 → 弹 PositionPickerDialog 取两个点
  2. 工具自动 capture + crop 出粗 ROI，在中间区域以放大形式显示
  3. 在放大图上鼠标拖拽画矩形 → 精细裁剪
  4. 从下拉栏选目标文件 → 保存

下拉栏统一列出待录清单（DEFAULT_SPECS），前缀:
  ✓ 已录入（目标 PNG 存在）
  ✗ 未录入

每项各自有保存目录、是否需要 yaml 元数据、是否强制精细裁剪等行为。
扩展待录清单：编辑 DEFAULT_SPECS。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

import yaml
from PIL import Image
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
)

from utils import Mumu

from app.core.profiles import MovementConfig
from app.views.position_picker import PositionPickerDialog
from app.views.roi_capture_dialog.crop_widget import CropWidget

log = logging.getLogger(__name__)


# =============================================================================
# 待录清单
# =============================================================================

ROI_OUTPUT_DIR = Path("tools/roi_captures")
CHAR_OUTPUT_DIR = Path("config/templates/minimap_coord")


@dataclass(frozen=True)
class NameSpec:
    """一项待录文件的描述。"""

    name: str  # 文件 base 名（不含扩展名）
    output_dir: Path  # 保存目录
    needs_yaml: bool = True  # 同时写一份 yaml 元数据
    kind_label: str = "ROI"  # 下拉栏类型标签
    default_timestamp: bool = True  # 选中时复选框默认状态
    require_crop: bool = False  # 必须精细裁剪后才允许保存
    # 若非 None：保存成功后，把 final_rect_norm 同步写入 movement_profile.yaml
    # 当前分辨率 profile 的同名字段
    sync_target: Optional[str] = None

    @property
    def png_path(self) -> Path:
        return self.output_dir / f"{self.name}.png"

    @property
    def is_recorded(self) -> bool:
        return self.png_path.exists()


def _char_spec(name: str, kind_label: str = "字符") -> NameSpec:
    return NameSpec(
        name=name,
        output_dir=CHAR_OUTPUT_DIR,
        needs_yaml=False,
        kind_label=kind_label,
        default_timestamp=False,
        require_crop=True,
    )


DEFAULT_SPECS: tuple[NameSpec, ...] = (
    NameSpec(
        name="minimap_coord_roi",
        output_dir=ROI_OUTPUT_DIR,
        needs_yaml=True,
        kind_label="ROI",
        default_timestamp=True,
        require_crop=False,
        sync_target="minimap_coord_roi",
    ),
    *(_char_spec(str(d)) for d in range(10)),
    _char_spec("lparen", "字符 ("),
    _char_spec("rparen", "字符 )"),
    _char_spec("comma", "字符 ,"),
)


SCALE_OPTIONS = (1, 2, 4, 6, 8)
DEFAULT_SCALE = 4


# =============================================================================
# Dialog
# =============================================================================


class RoiCaptureDialog(QDialog):
    def __init__(
        self,
        mumu: Mumu,
        parent=None,
        specs: tuple[NameSpec, ...] = DEFAULT_SPECS,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("ROI / 字符模板 截取工具")
        self.resize(840, 700)

        self._mumu = mumu
        self._specs = specs

        # 状态
        self._coarse_rect_norm: Optional[tuple[float, float, float, float]] = None
        self._coarse_image: Optional[Image.Image] = None

        self._build_ui()
        self._refresh_info()

    # =========================================================================
    # UI
    # =========================================================================

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(8)

        tip = QLabel(
            "工作流：\n"
            "  ① 点「取大致范围」 → 在游戏里指两个点（左上 / 右下，留点余量即可）\n"
            "  ② 在下方放大图上 *鼠标拖拽* 精细裁剪到目标的紧贴边界\n"
            "  ③ 从下拉栏选目标文件 → 保存\n"
            "下拉栏前缀：✓ 已录入 / ✗ 未录入。同一张粗截可反复 ② → ③ 逐个录字符模板。"
        )
        tip.setWordWrap(True)
        root.addWidget(tip)

        # 操作行
        op_row = QHBoxLayout()
        self._btn_pick = QPushButton("① 取大致范围")
        self._btn_pick.setMinimumHeight(32)
        self._btn_pick.clicked.connect(self._on_pick_corners)
        op_row.addWidget(self._btn_pick)

        op_row.addSpacing(16)
        op_row.addWidget(QLabel("缩放:"))
        self._scale_combo = QComboBox()
        for s in SCALE_OPTIONS:
            self._scale_combo.addItem(f"{s}×", s)
        self._scale_combo.setCurrentIndex(SCALE_OPTIONS.index(DEFAULT_SCALE))
        self._scale_combo.currentIndexChanged.connect(self._on_scale_changed)
        op_row.addWidget(self._scale_combo)

        self._btn_reset_sel = QPushButton("重置选框")
        self._btn_reset_sel.setEnabled(False)
        self._btn_reset_sel.clicked.connect(self._on_reset_selection)
        op_row.addWidget(self._btn_reset_sel)

        op_row.addStretch(1)
        root.addLayout(op_row)

        # 信息区
        info = QFormLayout()
        info.setHorizontalSpacing(20)
        self._lbl_coarse = QLabel("—")
        self._lbl_coarse.setTextInteractionFlags(Qt.TextSelectableByMouse)
        info.addRow("粗范围 (归一化):", self._lbl_coarse)

        self._lbl_final = QLabel("—")
        self._lbl_final.setTextInteractionFlags(Qt.TextSelectableByMouse)
        info.addRow("最终矩形 (归一化):", self._lbl_final)

        self._lbl_size = QLabel("—")
        info.addRow("最终 ROI 像素尺寸:", self._lbl_size)

        self._lbl_resolution = QLabel(f"{self._mumu.device_w} × {self._mumu.device_h}")
        info.addRow("当前游戏分辨率:", self._lbl_resolution)
        root.addLayout(info)

        # CropWidget（包在 ScrollArea）
        self._crop_widget = CropWidget(self)
        self._crop_widget.selection_changed.connect(self._on_selection_changed)

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(False)
        scroll.setAlignment(Qt.AlignCenter)
        scroll.setMinimumHeight(280)
        scroll.setStyleSheet(
            "QScrollArea { border: 1px solid #aaa; background: #2a2a2a; }"
        )
        scroll.setWidget(self._crop_widget)
        root.addWidget(scroll, 1)

        # 命名行
        name_row = QHBoxLayout()
        name_row.addWidget(QLabel("目标文件:"))
        self._name_combo = QComboBox()
        self._name_combo.setMinimumWidth(280)
        self._name_combo.setEditable(False)
        self._name_combo.currentIndexChanged.connect(self._on_combo_changed)
        name_row.addWidget(self._name_combo, 1)

        btn_refresh = QPushButton("↻")
        btn_refresh.setMaximumWidth(32)
        btn_refresh.setToolTip("重新扫描各目录，刷新已录入状态")
        btn_refresh.clicked.connect(self._reload_name_candidates)
        name_row.addWidget(btn_refresh)

        self._chk_timestamp = QCheckBox("追加时间戳")
        name_row.addWidget(self._chk_timestamp)
        root.addLayout(name_row)

        # 路径提示行
        self._lbl_target_path = QLabel("—")
        self._lbl_target_path.setStyleSheet("color: #888;")
        self._lbl_target_path.setWordWrap(True)
        root.addWidget(self._lbl_target_path)

        # 底部
        btns = QHBoxLayout()
        self._btn_save = QPushButton("保存")
        self._btn_save.setEnabled(False)
        self._btn_save.clicked.connect(self._on_save)
        btns.addWidget(self._btn_save)
        btns.addStretch(1)
        btn_close = QPushButton("关闭")
        btn_close.clicked.connect(self.close)
        btns.addWidget(btn_close)
        root.addLayout(btns)

        # 首次填充下拉栏（必须在 _btn_save 创建之后，因为 _on_combo_changed → _update_save_button 会访问它）
        self._reload_name_candidates()

    # =========================================================================
    # 下拉栏
    # =========================================================================

    @staticmethod
    def _spec_to_label(s: NameSpec) -> str:
        mark = "✓" if s.is_recorded else "✗"
        return f"{mark} [{s.kind_label}] {s.name}"

    def _reload_name_candidates(self) -> None:
        """重建下拉栏；保留刚才选中的 spec name。"""
        prev_name: Optional[str] = None
        prev_data = self._name_combo.currentData()
        if isinstance(prev_data, NameSpec):
            prev_name = prev_data.name

        self._name_combo.blockSignals(True)
        self._name_combo.clear()
        for s in self._specs:
            self._name_combo.addItem(self._spec_to_label(s), userData=s)
        if prev_name is not None:
            for i in range(self._name_combo.count()):
                d = self._name_combo.itemData(i)
                if isinstance(d, NameSpec) and d.name == prev_name:
                    self._name_combo.setCurrentIndex(i)
                    break
        self._name_combo.blockSignals(False)
        # 信号被 block 期间 _on_combo_changed 没触发，手动触发一次
        self._on_combo_changed()

    def _current_spec(self) -> Optional[NameSpec]:
        d = self._name_combo.currentData()
        return d if isinstance(d, NameSpec) else None

    def _on_combo_changed(self) -> None:
        spec = self._current_spec()
        if spec is None:
            self._lbl_target_path.setText("—")
            self._update_save_button()
            return

        # 更新路径提示
        suffix = " (已存在)" if spec.is_recorded else ""
        msg = f"将写入: {spec.png_path}{suffix}"
        if spec.needs_yaml:
            msg += " (含 .yaml 元数据)"
        if (
            spec.require_crop
            and self._crop_widget.selection() is None
            and self._coarse_image is not None
        ):
            msg += "    ⚠ 该项需要精细裁剪后才能保存"
        self._lbl_target_path.setText(msg)

        # 切换默认时间戳行为
        self._chk_timestamp.setChecked(spec.default_timestamp)
        self._update_save_button()

    def _select_next_unrecorded(self, start_idx: int) -> None:
        """从 start_idx 开始向后找第一个未录入项；找不到保持原选中。"""
        n = self._name_combo.count()
        for i in range(start_idx, n):
            d = self._name_combo.itemData(i)
            if isinstance(d, NameSpec) and not d.is_recorded:
                self._name_combo.setCurrentIndex(i)
                return

    def _update_save_button(self) -> None:
        if self._coarse_image is None:
            self._btn_save.setEnabled(False)
            return
        spec = self._current_spec()
        if spec is None:
            self._btn_save.setEnabled(False)
            return
        if spec.require_crop and self._crop_widget.selection() is None:
            self._btn_save.setEnabled(False)
            return
        self._btn_save.setEnabled(True)

    # =========================================================================
    # 取大致范围
    # =========================================================================

    def _on_pick_corners(self) -> None:
        dlg = PositionPickerDialog(
            self._mumu,
            parent=self,
            selection_mode=True,
            expected_count=2,
            selection_labels=["大致范围 左上角", "大致范围 右下角"],
        )
        dlg.setWindowTitle("ROI 截取 - 取大致范围")
        if dlg.exec() != QDialog.Accepted:
            return
        records = dlg.result_records()
        if len(records) != 2:
            QMessageBox.warning(
                self,
                "数量不符",
                f"需要 2 个点，实际记录 {len(records)} 个。",
            )
            return

        x0, y0 = records[0].nx, records[0].ny
        x1, y1 = records[1].nx, records[1].ny
        if x0 > x1:
            x0, x1 = x1, x0
        if y0 > y1:
            y0, y1 = y1, y0

        if abs(x1 - x0) < 1e-4 or abs(y1 - y0) < 1e-4:
            QMessageBox.warning(
                self,
                "矩形太小",
                "两点几乎重合，请重新取（大致框住目标 + 左右各留 5~10 像素余量即可）。",
            )
            return

        try:
            full = self._mumu.capture_window()
            cropped = self._mumu.crop_img(full, (x0, y0), (x1, y1))
        except Exception as e:
            log.exception("截取粗 ROI 失败")
            QMessageBox.critical(self, "截图失败", f"{type(e).__name__}: {e}")
            return

        self._coarse_rect_norm = (x0, y0, x1, y1)
        self._coarse_image = cropped

        scale = self._scale_combo.currentData()
        self._crop_widget.set_image(cropped, scale=scale)
        self._btn_pick.setText("重新粗截")
        self._btn_reset_sel.setEnabled(True)
        self._refresh_info()
        self._on_combo_changed()  # 路径提示 + save button 状态

    # =========================================================================
    # 缩放 / 选框
    # =========================================================================

    def _on_scale_changed(self) -> None:
        self._crop_widget.set_scale(self._scale_combo.currentData())

    def _on_reset_selection(self) -> None:
        self._crop_widget.reset_selection()

    def _on_selection_changed(self, _rect) -> None:
        self._refresh_info()
        self._on_combo_changed()  # 选框变更可能影响 require_crop 检查

    # =========================================================================
    # 信息显示
    # =========================================================================

    def _refresh_info(self) -> None:
        if self._coarse_rect_norm is None or self._coarse_image is None:
            self._lbl_coarse.setText("—")
            self._lbl_final.setText("—")
            self._lbl_size.setText("—")
            return

        cx0, cy0, cx1, cy1 = self._coarse_rect_norm
        self._lbl_coarse.setText(f"({cx0:.4f}, {cy0:.4f}, {cx1:.4f}, {cy1:.4f})")

        final_norm = self._final_rect_norm()
        if final_norm is None:
            self._lbl_final.setText("—")
            self._lbl_size.setText("—")
            return
        fx0, fy0, fx1, fy1 = final_norm
        self._lbl_final.setText(f"({fx0:.4f}, {fy0:.4f}, {fx1:.4f}, {fy1:.4f})")

        sel = self._crop_widget.selection()
        if sel is None:
            w, h = self._coarse_image.size
            suffix = "（未裁剪 = 粗范围）"
        else:
            x0, y0, x1, y1 = sel
            w, h = (x1 - x0), (y1 - y0)
            suffix = ""
        self._lbl_size.setText(f"{w} × {h} px {suffix}")

    def _final_rect_norm(
        self,
    ) -> Optional[tuple[float, float, float, float]]:
        if self._coarse_rect_norm is None or self._coarse_image is None:
            return None
        sel = self._crop_widget.selection()
        if sel is None:
            return self._coarse_rect_norm

        cx0, cy0, cx1, cy1 = self._coarse_rect_norm
        cw_norm = cx1 - cx0
        ch_norm = cy1 - cy0

        cw_pix, ch_pix = self._coarse_image.size
        ox0, oy0, ox1, oy1 = sel

        rx0 = ox0 / cw_pix
        ry0 = oy0 / ch_pix
        rx1 = ox1 / cw_pix
        ry1 = oy1 / ch_pix

        return (
            cx0 + rx0 * cw_norm,
            cy0 + ry0 * ch_norm,
            cx0 + rx1 * cw_norm,
            cy0 + ry1 * ch_norm,
        )

    # =========================================================================
    # 保存
    # =========================================================================

    def _on_save(self) -> None:
        if self._coarse_rect_norm is None or self._coarse_image is None:
            return
        spec = self._current_spec()
        if spec is None:
            QMessageBox.warning(self, "没选目标", "请从下拉栏选择目标文件。")
            return
        final_norm = self._final_rect_norm()
        if final_norm is None:
            return

        # 强制精细裁剪检查
        sel = self._crop_widget.selection()
        if spec.require_crop and sel is None:
            QMessageBox.warning(
                self,
                "需要精细裁剪",
                f"目标「{spec.name}」要求精细裁剪后才能保存。\n"
                "请先在放大图上拖拽画一个矩形选框。",
            )
            return

        if self._chk_timestamp.isChecked():
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            full_name = f"{spec.name}_{stamp}"
        else:
            full_name = spec.name

        if sel is None:
            final_image = self._coarse_image
        else:
            x0, y0, x1, y1 = sel
            final_image = self._coarse_image.crop((x0, y0, x1, y1))

        try:
            spec.output_dir.mkdir(parents=True, exist_ok=True)
            png_path = spec.output_dir / f"{full_name}.png"
            final_image.save(png_path)

            if spec.needs_yaml:
                yaml_path = spec.output_dir / f"{full_name}.yaml"
                meta = {
                    "rect_norm": [round(v, 6) for v in final_norm],
                    "resolution": [self._mumu.device_w, self._mumu.device_h],
                    "roi_pixel_size": list(final_image.size),
                    "captured_at": datetime.now().isoformat(timespec="seconds"),
                }
                yaml_path.write_text(
                    yaml.safe_dump(meta, allow_unicode=True, sort_keys=False),
                    encoding="utf-8",
                )
        except Exception as e:
            log.exception("保存失败")
            QMessageBox.critical(self, "保存失败", f"{type(e).__name__}: {e}")
            return

        log.info("保存: %s (size=%s)", png_path, final_image.size)

        # 同步到 movement_profile.yaml（如适用）
        sync_msg = self._maybe_sync_movement_profile(spec, final_norm)
        if sync_msg:
            QMessageBox.information(self, "已同步运动配置", sync_msg)

        # 刷新已录状态 + 自动跳到下一个未录入项
        cur_idx = self._name_combo.currentIndex()
        self._reload_name_candidates()
        self._select_next_unrecorded(start_idx=cur_idx + 1)

    def _maybe_sync_movement_profile(
        self,
        spec: NameSpec,
        rect_norm: tuple[float, float, float, float],
    ) -> Optional[str]:
        """
        若 spec.sync_target 非空，把 rect_norm 写入 movement_profile.yaml
        当前分辨率 profile 的同名字段。返回提示信息（None 表示无需同步）。
        """
        if not spec.sync_target:
            return None
        try:
            cfg = MovementConfig.load()
            old = getattr(cfg, spec.sync_target, None)
            new_val = tuple(rect_norm)
            setattr(cfg, spec.sync_target, new_val)
            path = cfg.save()
        except Exception as e:
            log.exception("同步 movement_profile 失败")
            return f"⚠ 同步失败: {type(e).__name__}: {e}"

        if old == new_val:
            return f"{path} 中 {spec.sync_target} 与原值一致，未实际改动。"
        old_str = "未配置" if old is None else f"{old}"
        return (
            f"已写入 {path}\n"
            f"  {spec.sync_target}:\n"
            f"    旧: {old_str}\n"
            f"    新: {new_val}"
        )
