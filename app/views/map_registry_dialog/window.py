"""
地图信息配置对话框。

左侧: 地名列表，带 ✓/✗ 前缀显示是否已录入
顶部: 新增地名 / 分辨率 / 大地图像素尺寸（可改）
中部: 选中地点的信息编辑（当前角、录入、两种验证、删除）
底部: 保存 / 重新加载
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Optional

from PIL import Image, ImageDraw, ImageFont, ImageQt
from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from utils import Mumu

from config.common.map_registry import (
    DEFAULT_LOCATIONS,
    DEFAULT_YAML_PATH,
    Corner,
    CoordSystem,
    LocationRecord,
    MapRegistry,
    Profile,
)
from app.views.position_picker import PickRecord, PositionPickerDialog

log = logging.getLogger(__name__)


# 点击验证时 icon → btn 之间的延时（秒），等待跳转按钮弹出
CLICK_VERIFY_DELAY = 0.6
SCREENSHOT_VERIFY_DELAY = 0.6


class MapRegistryDialog(QDialog):
    def __init__(
        self,
        mumu: Mumu,
        parent=None,
        yaml_path: Path = DEFAULT_YAML_PATH,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("地图信息配置")
        self.resize(900, 600)

        self._mumu = mumu
        self._yaml_path = yaml_path
        self._registry: MapRegistry = MapRegistry.load(yaml_path)
        self._profile: Profile = self._registry.ensure_profile(
            (mumu.device_w, mumu.device_h)
        )

        self._build_ui()
        self._reload_location_list()

    # =========================================================================
    # UI 构造
    # =========================================================================

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(8)

        # ---- 顶部：新增 / profile 信息 ----
        top = QHBoxLayout()

        top.addWidget(QLabel("新增地名:"))
        self._new_name_edit = QLineEdit()
        self._new_name_edit.setPlaceholderText("输入地点名后点「添加」")
        self._new_name_edit.setMaximumWidth(200)
        self._new_name_edit.returnPressed.connect(self._on_add_name)
        top.addWidget(self._new_name_edit)
        btn_add = QPushButton("添加")
        btn_add.clicked.connect(self._on_add_name)
        top.addWidget(btn_add)

        top.addSpacing(20)
        top.addWidget(QLabel(f"分辨率: {self._profile.key}"))

        top.addSpacing(20)
        top.addWidget(QLabel("大地图像素尺寸:"))
        self._bigmap_w = QSpinBox()
        self._bigmap_w.setRange(100, 99999)
        self._bigmap_w.setValue(self._profile.bigmap_size_pixel[0])
        self._bigmap_w.valueChanged.connect(self._on_bigmap_size_changed)
        top.addWidget(self._bigmap_w)
        top.addWidget(QLabel("×"))
        self._bigmap_h = QSpinBox()
        self._bigmap_h.setRange(100, 99999)
        self._bigmap_h.setValue(self._profile.bigmap_size_pixel[1])
        self._bigmap_h.valueChanged.connect(self._on_bigmap_size_changed)
        top.addWidget(self._bigmap_h)

        top.addStretch(1)
        root.addLayout(top)

        # 分隔线
        root.addWidget(_hline())

        # ---- 中部：左侧列表 + 右侧详情 ----
        middle = QHBoxLayout()

        # 左侧
        left = QVBoxLayout()
        left.addWidget(QLabel("地点列表（✓ 已录入 / ✗ 未录入）"))
        self._list = QListWidget()
        self._list.currentItemChanged.connect(self._on_list_sel_changed)
        left.addWidget(self._list, 1)
        left_w = QWidget()
        left_w.setLayout(left)
        left_w.setMinimumWidth(220)
        left_w.setMaximumWidth(280)
        middle.addWidget(left_w)

        # 右侧
        right = QVBoxLayout()
        self._detail_widget = self._build_detail_widget()
        right.addWidget(self._detail_widget)
        right.addStretch(1)
        middle.addLayout(right, 1)

        root.addLayout(middle, 1)

        # ---- 底部：保存/加载/关闭 ----
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

    def _build_detail_widget(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        form.setContentsMargins(8, 0, 0, 0)

        self._lbl_name = QLabel("—")
        self._lbl_name.setStyleSheet("font-size: 16px; font-weight: bold;")
        form.addRow("地点:", self._lbl_name)

        self._corner_combo = QComboBox()
        for c in Corner:
            # 注意：Corner 是 str 子类枚举，直接传对象会被 QVariant 窄化成 str。
            # 所以 userData 统一存 value，读取时用 Corner(...) 包回来。
            self._corner_combo.addItem(f"{c.label} ({c.value})", c.value)
        form.addRow("录入角落:", self._corner_combo)

        self._lbl_icon = QLabel("—")
        form.addRow("图标大地图像素:", self._lbl_icon)

        self._lbl_offset = QLabel("—")
        form.addRow("按钮偏移像素:", self._lbl_offset)

        # 地图几何（可编辑）
        map_size_row = QHBoxLayout()
        self._map_w = QSpinBox()
        self._map_w.setRange(0, 999)
        self._map_w.setSpecialValueText("—")
        self._map_w.valueChanged.connect(self._on_map_geom_changed)
        self._map_h = QSpinBox()
        self._map_h.setRange(0, 999)
        self._map_h.setSpecialValueText("—")
        self._map_h.valueChanged.connect(self._on_map_geom_changed)
        map_size_row.addWidget(self._map_w)
        map_size_row.addWidget(QLabel("×"))
        map_size_row.addWidget(self._map_h)
        map_size_row.addStretch(1)
        form.addRow("地图格数 (w×h):", _wrap(map_size_row))

        self._vision_combo = QComboBox()
        self._vision_combo.addItem("— 未设置 —", None)
        for v in ("小", "中", "大"):
            self._vision_combo.addItem(v, v)
        self._vision_combo.currentIndexChanged.connect(self._on_map_geom_changed)
        form.addRow("视野档位:", self._vision_combo)

        self._lbl_warn = QLabel("")
        self._lbl_warn.setStyleSheet("color: #c00;")
        self._lbl_warn.setWordWrap(True)
        form.addRow("提示:", self._lbl_warn)

        # 操作按钮
        btn_row = QHBoxLayout()
        self._btn_record = QPushButton("录入 / 重录")
        self._btn_record.clicked.connect(self._on_record)
        btn_row.addWidget(self._btn_record)

        self._btn_delete = QPushButton("删除录入")
        self._btn_delete.clicked.connect(self._on_delete_record)
        btn_row.addWidget(self._btn_delete)

        self._btn_rename = QPushButton("重命名")
        self._btn_rename.clicked.connect(self._on_rename)
        btn_row.addWidget(self._btn_rename)

        self._btn_remove_loc = QPushButton("从列表删除")
        self._btn_remove_loc.clicked.connect(self._on_remove_location)
        btn_row.addWidget(self._btn_remove_loc)
        btn_row.addStretch(1)
        form.addRow("", _wrap(btn_row))

        # 验证按钮
        verify_row = QHBoxLayout()
        verify_row.addWidget(QLabel("模拟起点:"))
        self._src_combo = QComboBox()
        self._src_combo.setMinimumWidth(140)
        verify_row.addWidget(self._src_combo)

        self._btn_verify_snap = QPushButton("截图画圈验证")
        self._btn_verify_snap.clicked.connect(self._on_verify_snapshot)
        verify_row.addWidget(self._btn_verify_snap)

        self._btn_verify_click = QPushButton("游戏内点击验证")
        self._btn_verify_click.clicked.connect(self._on_verify_click)
        verify_row.addWidget(self._btn_verify_click)
        verify_row.addStretch(1)
        form.addRow("", _wrap(verify_row))

        return w

    # =========================================================================
    # 列表 / 详情同步
    # =========================================================================

    def _reload_location_list(self) -> None:
        """全量刷新左侧列表，保持当前选中地名"""
        # 先把内置默认地名合并进来做 UI 展示（不改变已录入字段）
        # 这些纯占位的默认名在 save 时被 Profile.to_dict 过滤掉，不污染 yaml
        for name in DEFAULT_LOCATIONS:
            self._profile.locations.setdefault(name, LocationRecord())

        prev = self._current_name()
        self._list.blockSignals(True)
        self._list.clear()
        for name in sorted(self._profile.locations.keys()):
            rec = self._profile.locations[name]
            mark = "✓" if rec.is_recorded else "✗"
            item = QListWidgetItem(f"{mark} {name}")
            item.setData(Qt.UserRole, name)
            self._list.addItem(item)
        self._list.blockSignals(False)

        # 恢复选中
        if prev is not None:
            self._select_by_name(prev)
        elif self._list.count() > 0:
            self._list.setCurrentRow(0)

        # 模拟起点下拉：仅显示已录入的地点
        self._reload_src_combo()

    def _reload_src_combo(self) -> None:
        cur = self._src_combo.currentData()
        self._src_combo.blockSignals(True)
        self._src_combo.clear()
        for name in sorted(self._profile.locations.keys()):
            if self._profile.locations[name].is_recorded:
                self._src_combo.addItem(name, name)
        self._src_combo.blockSignals(False)
        if cur is not None:
            idx = self._src_combo.findData(cur)
            if idx >= 0:
                self._src_combo.setCurrentIndex(idx)

    def _current_name(self) -> Optional[str]:
        item = self._list.currentItem()
        return None if item is None else item.data(Qt.UserRole)

    def _select_by_name(self, name: str) -> None:
        for i in range(self._list.count()):
            if self._list.item(i).data(Qt.UserRole) == name:
                self._list.setCurrentRow(i)
                return

    def _on_list_sel_changed(self, cur: Optional[QListWidgetItem], _prev) -> None:
        self._refresh_detail()

    def _refresh_detail(self) -> None:
        name = self._current_name()
        if name is None:
            self._lbl_name.setText("—")
            self._lbl_icon.setText("—")
            self._lbl_offset.setText("—")
            self._lbl_warn.setText("")
            self._set_detail_enabled(False)
            return

        rec = self._profile.locations[name]
        self._lbl_name.setText(name)

        # 角落
        if rec.recorded_at_corner is not None:
            idx = self._corner_combo.findData(rec.recorded_at_corner.value)
            if idx >= 0:
                self._corner_combo.setCurrentIndex(idx)

        if rec.is_recorded:
            self._lbl_icon.setText(
                f"({rec.icon_on_bigmap_pixel[0]:.1f}, {rec.icon_on_bigmap_pixel[1]:.1f})"
            )
            self._lbl_offset.setText(
                f"({rec.btn_offset_pixel[0]:.1f}, {rec.btn_offset_pixel[1]:.1f})"
            )
        else:
            self._lbl_icon.setText("未录入")
            self._lbl_offset.setText("未录入")

        # 地图格数 / 视野 —— 允许未录入也能编辑（属于静态属性，与 icon/btn 独立）
        self._map_w.blockSignals(True)
        self._map_h.blockSignals(True)
        self._vision_combo.blockSignals(True)
        if rec.map_size is not None:
            self._map_w.setValue(rec.map_size[0])
            self._map_h.setValue(rec.map_size[1])
        else:
            self._map_w.setValue(0)  # 0 = 未设置（specialValueText 显示 "—"）
            self._map_h.setValue(0)
        vs_idx = self._vision_combo.findData(rec.vision_size)
        self._vision_combo.setCurrentIndex(vs_idx if vs_idx >= 0 else 0)
        self._map_w.blockSignals(False)
        self._map_h.blockSignals(False)
        self._vision_combo.blockSignals(False)

        # 越界警告
        self._lbl_warn.setText(self._check_warnings(name, rec))

        self._set_detail_enabled(True)

    def _on_map_geom_changed(self, *_args) -> None:
        """用户改了 map_size / vision_size → 即时写回 LocationRecord"""
        name = self._current_name()
        if name is None:
            return
        rec = self._profile.locations[name]
        w, h = self._map_w.value(), self._map_h.value()
        rec.map_size = (w, h) if (w > 0 and h > 0) else None
        rec.vision_size = self._vision_combo.currentData()

    def _set_detail_enabled(self, on: bool) -> None:
        has_name = on
        has_record = (
            on
            and self._current_name() is not None
            and self._profile.locations[self._current_name()].is_recorded
        )
        self._btn_record.setEnabled(has_name)
        self._btn_delete.setEnabled(has_record)
        self._btn_rename.setEnabled(has_name)
        self._btn_remove_loc.setEnabled(has_name)
        self._btn_verify_snap.setEnabled(has_record)
        self._btn_verify_click.setEnabled(has_record)

    # =========================================================================
    # 越界检测
    # =========================================================================

    def _cs(self) -> CoordSystem:
        return CoordSystem(self._profile, self._registry.constraints)

    def _check_warnings(self, name: str, rec: LocationRecord) -> str:
        if not rec.is_recorded:
            return ""
        warnings: list[str] = []

        cs = self._cs()
        bw_n, bh_n = cs.bigmap_norm
        if bw_n < 1 or bh_n < 1:
            warnings.append("大地图尺寸小于一个可点击区域，无法正常进入地图选择界面。")

        # icon 绝对位置应该在 [0, bigmap] 内
        ix, iy = rec.icon_on_bigmap_pixel
        bw_px, bh_px = self._profile.bigmap_size_pixel
        if not (0 <= ix <= bw_px and 0 <= iy <= bh_px):
            warnings.append(
                f"图标位置 ({ix:.0f},{iy:.0f}) 超出大地图范围 "
                f"({bw_px}×{bh_px})，请检查或重录。"
            )

        # 站在自己身上时：icon + offset 必须都在 [0,1] 视图内
        self_rec = rec
        pair = cs.target_in_view(self_rec, self_rec)
        if pair is None:
            warnings.append(
                "以自身为起点时，按钮偏移导致跳转按钮超出可点击区域 —— "
                "应该不会发生，请检查录入数据。"
            )
        return "\n".join(warnings)

    # =========================================================================
    # 新增 / 删除 / 重命名
    # =========================================================================

    def _on_add_name(self) -> None:
        name = self._new_name_edit.text().strip()
        if not name:
            return
        if name in self._profile.locations:
            QMessageBox.information(self, "已存在", f"地点「{name}」已经在列表中。")
            return
        self._profile.locations[name] = LocationRecord()
        self._profile.mark_explicit(name)
        self._new_name_edit.clear()
        self._reload_location_list()
        self._select_by_name(name)

    def _on_remove_location(self) -> None:
        name = self._current_name()
        if name is None:
            return
        ans = QMessageBox.question(
            self,
            "确认删除",
            f"从列表中删除「{name}」？这同时会移除已录入的信息。",
        )
        if ans != QMessageBox.Yes:
            return
        self._profile.locations.pop(name, None)
        self._profile.explicit_names.discard(name)
        self._reload_location_list()

    def _on_rename(self) -> None:
        name = self._current_name()
        if name is None:
            return
        new_name, ok = QInputDialog.getText(self, "重命名地点", "新名称:", text=name)
        new_name = (new_name or "").strip()
        if not ok or not new_name or new_name == name:
            return
        if new_name in self._profile.locations:
            QMessageBox.information(self, "冲突", f"「{new_name}」已存在。")
            return
        self._profile.locations[new_name] = self._profile.locations.pop(name)
        # explicit 标记跟随
        if name in self._profile.explicit_names:
            self._profile.explicit_names.discard(name)
            self._profile.mark_explicit(new_name)
        self._reload_location_list()
        self._select_by_name(new_name)

    def _on_delete_record(self) -> None:
        name = self._current_name()
        if name is None:
            return
        ans = QMessageBox.question(
            self,
            "确认",
            f"清空「{name}」的录入信息（保留在列表里）？",
        )
        if ans != QMessageBox.Yes:
            return
        self._profile.locations[name] = LocationRecord()
        self._reload_location_list()
        self._select_by_name(name)

    # =========================================================================
    # 录入
    # =========================================================================

    def _on_record(self) -> None:
        name = self._current_name()
        if name is None:
            return
        corner = Corner(self._corner_combo.currentData())

        dlg = PositionPickerDialog(
            self._mumu,
            parent=self,
            selection_mode=True,
            expected_count=2,
            selection_labels=[
                f"{name} 图标中心",
                f"{name} 跳转按钮中心",
            ],
        )
        dlg.setWindowTitle(f"录入「{name}」({corner.label}角)")
        if dlg.exec() != QDialog.Accepted:
            return
        records = dlg.result_records()
        if len(records) != 2:
            QMessageBox.warning(
                self,
                "数量不符",
                f"需要 2 个位置（图标 + 按钮），实际记录 {len(records)}。",
            )
            return

        icon_pick: PickRecord = records[0]
        btn_pick: PickRecord = records[1]

        # ── 硬拒绝：按钮被屏幕下沿顶住 ──────────────────────────────
        # 游戏里按钮 y 无法超过 btn_floor_y；若采样值正好落在 floor±eps，
        # 说明这条 offset 是"被压扁的"，不能作为真实偏移存盘。
        # 例外: 贴在 SW/SE 角时相机下沿已对齐 bigmap 下沿，按钮位置即真实位置，
        # 不可能被"屏幕下沿"顶住 —— 此时跳过检查。
        if corner not in (Corner.SW, Corner.SE):
            floor = self._registry.constraints.btn_floor_y
            eps = self._registry.constraints.btn_floor_eps
            if floor - eps <= btn_pick.ny <= floor + eps:
                QMessageBox.critical(
                    self,
                    "按钮被屏幕底部顶住",
                    f"采样的按钮 y = {btn_pick.ny:.4f}，"
                    f"落在游戏下沿地板 {floor:.4f} ± {eps:.4f} 范围内。\n\n"
                    f"这意味着按钮实际位置本应更靠下，但被游戏 UI 顶到固定位置，"
                    f"记录下来的偏移会偏小，运行时会点错。\n\n"
                    f"请换成 SW/SE 角（相机贴底，按钮不会被顶），或让按钮"
                    f"完整显示在可点击区域内后重新录入。",
                )
                return

        # 把贴角录入的归一化 pick 反推成大地图绝对像素
        cs = self._cs()
        icon_abs = cs.pick_to_bigmap_abs((icon_pick.nx, icon_pick.ny), corner)

        # offset: 直接用归一化差 × 分辨率
        rw, rh = self._profile.resolution
        off_px = (
            (btn_pick.nx - icon_pick.nx) * rw,
            (btn_pick.ny - icon_pick.ny) * rh,
        )

        self._profile.locations[name] = LocationRecord(
            icon_on_bigmap_pixel=icon_abs,
            btn_offset_pixel=off_px,
            recorded_at_corner=corner,
        )
        log.info(
            "录入「%s」@%s: icon_abs=(%.1f,%.1f) offset_px=(%.1f,%.1f)",
            name,
            corner.value,
            icon_abs[0],
            icon_abs[1],
            off_px[0],
            off_px[1],
        )
        self._reload_location_list()
        self._select_by_name(name)

    # =========================================================================
    # 大地图尺寸 / 保存加载
    # =========================================================================

    def _on_bigmap_size_changed(self, _v: int) -> None:
        self._profile.bigmap_size_pixel = (
            self._bigmap_w.value(),
            self._bigmap_h.value(),
        )
        # 大小变了，warning 要重算
        self._refresh_detail()

    def _on_save(self) -> None:
        try:
            path = self._registry.save(self._yaml_path)
        except Exception as e:
            log.exception("保存 registry 失败")
            QMessageBox.critical(self, "保存失败", f"{type(e).__name__}: {e}")
            return
        QMessageBox.information(self, "保存成功", f"已写入: {path}")

    def _on_reload(self) -> None:
        ans = QMessageBox.question(
            self,
            "重新加载",
            "丢弃当前未保存改动，从磁盘重新加载？",
        )
        if ans != QMessageBox.Yes:
            return
        self._registry = MapRegistry.load(self._yaml_path)
        self._profile = self._registry.ensure_profile(
            (self._mumu.device_w, self._mumu.device_h)
        )
        self._bigmap_w.blockSignals(True)
        self._bigmap_h.blockSignals(True)
        self._bigmap_w.setValue(self._profile.bigmap_size_pixel[0])
        self._bigmap_h.setValue(self._profile.bigmap_size_pixel[1])
        self._bigmap_w.blockSignals(False)
        self._bigmap_h.blockSignals(False)
        self._reload_location_list()

    # =========================================================================
    # 验证
    # =========================================================================

    def _get_verify_pair(
        self,
    ) -> Optional[tuple[str, str, tuple[float, float], tuple[float, float]]]:
        """计算 (src_name, tgt_name, icon_norm, btn_norm)，越界/未录入返回 None 并弹框"""
        tgt = self._current_name()
        src = self._src_combo.currentData()
        if not tgt:
            QMessageBox.warning(self, "缺失", "请先在列表中选一个目标地点。")
            return None
        if not src:
            QMessageBox.warning(
                self, "缺失", "请先选择模拟起点（需要有已录入的地点）。"
            )
            return None

        src_rec = self._profile.locations[src]
        tgt_rec = self._profile.locations[tgt]
        if not tgt_rec.is_recorded:
            QMessageBox.warning(self, "未录入", f"「{tgt}」尚未录入。")
            return None

        cs = self._cs()
        pair = cs.target_in_view(src_rec, tgt_rec)
        if pair is None:
            QMessageBox.warning(
                self,
                "不可见",
                f"以「{src}」为起点时，「{tgt}」的图标或跳转按钮不在可点击区域内。",
            )
            return None
        icon_norm, btn_norm = pair
        return src, tgt, icon_norm, btn_norm

    def _on_verify_click(self) -> None:
        pair = self._get_verify_pair()
        if pair is None:
            return
        src, tgt, icon_norm, btn_norm = pair
        ans = QMessageBox.question(
            self,
            "游戏内点击验证",
            f"将在游戏里依次点击：\n"
            f"  1. 「{tgt}」图标 ({icon_norm[0]:.3f}, {icon_norm[1]:.3f})\n"
            f"  2. 「{tgt}」跳转按钮 ({btn_norm[0]:.3f}, {btn_norm[1]:.3f})\n\n"
            f"⚠ 将触发实际游戏内跳转。请先确认当前游戏处于「{src}」的地图选择界面。\n\n"
            "继续？",
        )
        if ans != QMessageBox.Yes:
            return

        try:
            self._mumu.click(icon_norm)
            time.sleep(CLICK_VERIFY_DELAY)
            self._mumu.click(btn_norm)
        except Exception as e:
            log.exception("点击验证失败")
            QMessageBox.critical(self, "点击失败", f"{type(e).__name__}: {e}")

    def _on_verify_snapshot(self) -> None:
        pair = self._get_verify_pair()
        if pair is None:
            return
        src, tgt, icon_norm, btn_norm = pair

        ans = QMessageBox.question(
            self,
            "截图画圈验证",
            f"将先点击「{tgt}」图标以唤出跳转按钮，再截图标注。\n"
            f"请确认当前游戏处于「{src}」的地图选择界面。\n\n"
            "继续？",
        )
        if ans != QMessageBox.Yes:
            return

        try:
            self._mumu.click(icon_norm)
            time.sleep(SCREENSHOT_VERIFY_DELAY)
            img = self._mumu.capture_window()
        except Exception as e:
            log.exception("截图验证失败")
            QMessageBox.critical(self, "失败", f"{type(e).__name__}: {e}")
            return

        annotated = _annotate(img, icon_norm, btn_norm, tgt)
        _SnapshotPreview(
            annotated, title=f"验证「{src}」→「{tgt}」", parent=self
        ).exec()


# =============================================================================
# Helpers / Widgets
# =============================================================================


def _hline() -> QFrame:
    f = QFrame()
    f.setFrameShape(QFrame.HLine)
    f.setFrameShadow(QFrame.Sunken)
    return f


def _wrap(lay) -> QWidget:
    w = QWidget()
    w.setLayout(lay)
    return w


def _annotate(
    img: Image.Image,
    icon_norm: tuple[float, float],
    btn_norm: tuple[float, float],
    tgt_name: str,
) -> Image.Image:
    """在截图上画 icon（蓝）和 btn（红）圆 + 连线 + 标签"""
    w, h = img.size
    out = img.copy().convert("RGB")
    draw = ImageDraw.Draw(out)

    ix, iy = int(icon_norm[0] * w), int(icon_norm[1] * h)
    bx, by = int(btn_norm[0] * w), int(btn_norm[1] * h)

    radius = max(12, min(w, h) // 60)

    # 连线
    draw.line([(ix, iy), (bx, by)], fill=(255, 255, 0), width=2)

    # icon 蓝圈
    draw.ellipse(
        [(ix - radius, iy - radius), (ix + radius, iy + radius)],
        outline=(0, 100, 255),
        width=3,
    )
    # btn 红圈
    draw.ellipse(
        [(bx - radius, by - radius), (bx + radius, by + radius)],
        outline=(255, 40, 40),
        width=3,
    )

    # 文本
    try:
        font = ImageFont.truetype("msyh.ttc", size=max(16, min(w, h) // 50))
    except Exception:
        font = ImageFont.load_default()
    draw.text(
        (ix + radius + 4, iy - radius),
        f"icon: {tgt_name}",
        fill=(0, 100, 255),
        font=font,
    )
    draw.text((bx + radius + 4, by - radius), "btn", fill=(255, 40, 40), font=font)
    return out


class _SnapshotPreview(QDialog):
    def __init__(self, img: Image.Image, title: str, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        # 限制预览窗口尺寸
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
