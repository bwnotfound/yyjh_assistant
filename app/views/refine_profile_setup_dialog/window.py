"""装备精炼 - ROI / 按钮坐标 配置工具.

完全复用项目已有的子窗 ``ZoomedSnapshotPicker``:
    - ROI 取点: ``ZoomedSnapshotPicker(mode='rect').exec_for_rect()``
              一次拖框 = 一个 ROI, 比"先取左上再取右下"两步法体验好.
    - 按钮取点: ``PositionPickerDialog(expected_count=1)``
              单点专用, 顺便看 RGB 防止点歪了.
    - 标定预览: ``ZoomedSnapshotPicker(mode='readonly')`` + PIL 把标注画到原图上,
              支持滚轮缩放 + 右键拖动平移 + 鼠标 hover 看像素色, 体验跟取点
              工具完全一致.

不依赖 RefineProfile dataclass, 直接读写 yaml 字典 - 这样 yaml 里 ROI/按钮字段
不齐时, 配置工具仍能继续录入, 不会因为 dataclass 字段缺失先挂.

保存策略: 只更新 ``roi`` 和 ``button`` 两段, 不动其他字段 (材料映射 / OCR 配置等).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml
from PIL import Image, ImageDraw, ImageFont
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QDialog,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from utils import Mumu

from app.views.position_picker import PositionPickerDialog
from app.views.position_picker.window import ZoomedSnapshotPicker

log = logging.getLogger(__name__)


# =============================================================================
# 待录字段清单
# =============================================================================


@dataclass(frozen=True)
class _RoiSpec:
    key: str
    panel: str  # "结束" | "准备" | "共享"
    desc: str

    @property
    def panel_color(self) -> str:
        return {"结束": "#3a7", "准备": "#37a", "共享": "#888"}[self.panel]


@dataclass(frozen=True)
class _BtnSpec:
    key: str
    panel: str
    desc: str

    @property
    def panel_color(self) -> str:
        return {"结束": "#3a7", "准备": "#37a"}[self.panel]


# 顺序: 共享(任一界面录) → 结束界面专属 → 准备界面专属
# 这样向导跑一遍最多只需切两次界面.
ROI_SPECS: tuple[_RoiSpec, ...] = (
    _RoiSpec("equipment_name", "共享", "装备图下方的装备名 '呼如木甲'"),
    _RoiSpec(
        "refine_count",
        "共享",
        "整行 '已精炼:N次' (把'已精炼'中文一起框进去, 防止数字位数不定时漏字符)",
    ),
    _RoiSpec("base_attrs", "共享", "右上'基础属性'下方两行 (防御/罡气)"),
    _RoiSpec("extra_attr_1", "共享", "中框 第1行 旧词条 (例: '攻击 126')"),
    _RoiSpec("extra_attr_2", "共享", "中框 第2行 旧词条 (例: '免伤 2.2%')"),
    _RoiSpec("extra_attr_3", "共享", "中框 第3行 旧词条 (例: '闪避 58')"),
    _RoiSpec(
        "bottom_buttons",
        "共享",
        "底部按钮文字区 (用于界面识别; 不要圈到上方银两)",
    ),
    _RoiSpec("material_1", "结束", "左下第一个材料数字 '844/5'"),
    _RoiSpec("material_2", "结束", "左下第二个材料数字 '188/1'"),
    _RoiSpec("cost_money", "结束", "右下 '花费 27两315文'"),
    _RoiSpec("balance_money", "结束", "右下 '拥有 7250两280文'"),
    _RoiSpec(
        "new_attr_slot_1",
        "准备",
        "右框新词条第1行位置 (跟 extra_attr_1 水平对齐)",
    ),
    _RoiSpec(
        "new_attr_slot_2",
        "准备",
        "右框新词条第2行位置 (跟 extra_attr_2 水平对齐)",
    ),
    _RoiSpec(
        "new_attr_slot_3",
        "准备",
        "右框新词条第3行位置 (跟 extra_attr_3 水平对齐)",
    ),
)

BTN_SPECS: tuple[_BtnSpec, ...] = (
    _BtnSpec("refine", "结束", "结束界面 '精炼' 按钮中心"),
    _BtnSpec("accept", "准备", "准备界面 '接受' 按钮中心"),
    _BtnSpec("cancel", "准备", "准备界面 '取消' 按钮中心"),
)


# =============================================================================
# Dialog
# =============================================================================


class RefineProfileSetupDialog(QDialog):
    def __init__(
        self,
        mumu: Mumu,
        profile_path: Path,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("装备精炼 - ROI / 按钮坐标 配置")
        self.resize(1080, 760)
        self.mumu = mumu
        self.profile_path = Path(profile_path)
        self._dirty = False
        # 内存中持有完整 yaml dict, 保存时整体回写 (保留 ROI/button 之外的字段)
        self._data: dict = {}
        self._build_ui()
        self._load()

    # =========================================================================
    # UI
    # =========================================================================

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)

        # 顶部: 文件路径 + 工具按钮
        top = QHBoxLayout()
        top.addWidget(QLabel("配置文件:"))
        self._lbl_path = QLabel(str(self.profile_path))
        self._lbl_path.setStyleSheet("color: #555;")
        top.addWidget(self._lbl_path, 1)

        b_reload = QPushButton("重载")
        b_reload.setToolTip("放弃未保存改动, 重新从磁盘读取")
        b_reload.clicked.connect(self._on_reload)
        top.addWidget(b_reload)

        b_wizard = QPushButton("向导: 按顺序录入未配置的项")
        b_wizard.setToolTip("从第一个空字段开始, 依次弹 picker, 一次性录完")
        b_wizard.clicked.connect(self._on_wizard)
        top.addWidget(b_wizard)
        root.addLayout(top)

        # 提示
        tip = QLabel(
            "  共享 ROI 在结束界面或准备界面任一界面录入即可."
            "  结束 / 准备 界面专属 ROI 必须切到对应界面再录."
            "  绿色 = 结束界面 / 蓝色 = 准备界面 / 灰色 = 共享.\n\n"
            "📌 ROI 取点: 弹出截图选点窗口后, 直接 [左键拖出矩形] 一次完成 (右键拖动平移, 滚轮缩放).\n"
            '⚠️ extra_attr_1/2/3 和 new_attr_slot_1/2/3 各 3 个槽位, 必须"逐行水平对齐":\n'
            "    extra_attr_i 框中框第 i 行旧词条; new_attr_slot_i 框右框第 i 行的位置.\n"
            "    建议找一件附加属性已满 3 条的装备 (各槽位都有视觉参照)."
        )
        tip.setStyleSheet("color: #555; padding: 4px;")
        tip.setWordWrap(True)
        root.addWidget(tip)

        # ROI 表
        gb_roi = QGroupBox(f"ROI ({len(ROI_SPECS)} 个) - 矩形区域 (左键拖框一次完成)")
        rl = QVBoxLayout(gb_roi)
        self._roi_table = self._build_roi_table()
        rl.addWidget(self._roi_table)
        root.addWidget(gb_roi)

        # 按钮表
        gb_btn = QGroupBox(f"按钮坐标 ({len(BTN_SPECS)} 个) - 单点")
        bl = QVBoxLayout(gb_btn)
        self._btn_table = self._build_btn_table()
        bl.addWidget(self._btn_table)
        root.addWidget(gb_btn)

        # 底部: 预览 / 保存 / 关闭
        bot = QHBoxLayout()
        b_preview = QPushButton("📷 截图预览所有标定")
        b_preview.setToolTip(
            "截当前游戏画面, 把所有已配置的 ROI / 按钮画上去, 直观验证. "
            "支持滚轮缩放 + 右键拖动平移."
        )
        b_preview.clicked.connect(self._on_preview)
        bot.addWidget(b_preview)
        bot.addStretch(1)
        self._lbl_dirty = QLabel("")
        self._lbl_dirty.setStyleSheet("color: #c40;")
        bot.addWidget(self._lbl_dirty)
        b_save = QPushButton("保存到 yaml")
        b_save.setStyleSheet("font-weight: bold;")
        b_save.clicked.connect(self._on_save)
        bot.addWidget(b_save)
        b_close = QPushButton("关闭")
        b_close.clicked.connect(self.close)
        bot.addWidget(b_close)
        root.addLayout(bot)

    def _build_roi_table(self) -> QTableWidget:
        t = QTableWidget(len(ROI_SPECS), 4)
        t.setHorizontalHeaderLabels(
            ["字段名", "界面", "当前值 (x1, y1, x2, y2)", "操作"]
        )
        t.verticalHeader().setVisible(False)
        h = t.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(2, QHeaderView.Stretch)
        h.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        for i, spec in enumerate(ROI_SPECS):
            t.setItem(i, 0, QTableWidgetItem(spec.key))
            t.item(i, 0).setToolTip(spec.desc)
            it_panel = QTableWidgetItem(spec.panel)
            it_panel.setForeground(QColor(spec.panel_color))
            t.setItem(i, 1, it_panel)
            t.setItem(i, 2, QTableWidgetItem("-"))
            btn = QPushButton("重取")
            btn.clicked.connect(lambda _=False, k=spec.key: self._pick_roi(k))
            t.setCellWidget(i, 3, btn)
        return t

    def _build_btn_table(self) -> QTableWidget:
        t = QTableWidget(len(BTN_SPECS), 5)
        t.setHorizontalHeaderLabels(["字段名", "界面", "说明", "当前值 (x, y)", "操作"])
        t.verticalHeader().setVisible(False)
        h = t.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(2, QHeaderView.Stretch)
        h.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        for i, spec in enumerate(BTN_SPECS):
            t.setItem(i, 0, QTableWidgetItem(spec.key))
            it_panel = QTableWidgetItem(spec.panel)
            it_panel.setForeground(QColor(spec.panel_color))
            t.setItem(i, 1, it_panel)
            t.setItem(i, 2, QTableWidgetItem(spec.desc))
            t.setItem(i, 3, QTableWidgetItem("-"))
            btn = QPushButton("重取")
            btn.clicked.connect(lambda _=False, k=spec.key: self._pick_btn(k))
            t.setCellWidget(i, 4, btn)
        return t

    # =========================================================================
    # 数据加载 / 显示
    # =========================================================================

    def _load(self) -> None:
        if self.profile_path.exists():
            try:
                self._data = (
                    yaml.safe_load(self.profile_path.read_text(encoding="utf-8")) or {}
                )
            except Exception as e:
                log.exception("读取 profile 失败")
                QMessageBox.critical(self, "读取失败", f"{type(e).__name__}: {e}")
                self._data = {}
        else:
            self._data = {}
        self._data.setdefault("roi", {})
        self._data.setdefault("button", {})
        self._refresh_table()
        self._dirty = False
        self._update_dirty_label()

    def _refresh_table(self) -> None:
        roi = self._data.get("roi", {}) or {}
        for i, spec in enumerate(ROI_SPECS):
            v = roi.get(spec.key)
            txt = _fmt_roi(v) if v else "(未配置)"
            it = QTableWidgetItem(txt)
            if not v:
                it.setForeground(QColor("#c00"))
            self._roi_table.setItem(i, 2, it)
        btn = self._data.get("button", {}) or {}
        for i, spec in enumerate(BTN_SPECS):
            v = btn.get(spec.key)
            txt = _fmt_pos(v) if v else "(未配置)"
            it = QTableWidgetItem(txt)
            if not v:
                it.setForeground(QColor("#c00"))
            self._btn_table.setItem(i, 3, it)

    def _update_dirty_label(self) -> None:
        self._lbl_dirty.setText("● 有未保存改动" if self._dirty else "")

    # =========================================================================
    # 取点
    # =========================================================================

    def _pick_roi(self, key: str) -> Optional[tuple[float, float, float, float]]:
        """ROI 取点: 截图后弹放大子窗, 用户左键拖出一个矩形即完成.

        体验:
            - 鼠标滚轮调整放大倍率 (1×~6×, 锚点在视图中心)
            - 右键拖动平移
            - 左键 press → drag → release 形成矩形, 释放即返回
        """
        spec = next(s for s in ROI_SPECS if s.key == key)
        try:
            img = self.mumu.capture_window()
        except Exception as e:
            log.exception("截图失败")
            QMessageBox.critical(self, "截图失败", f"{type(e).__name__}: {e}")
            return None
        picker = ZoomedSnapshotPicker(
            img,
            self.mumu,
            parent=self,
            show_recapture=True,
            mode="rect",
            prompt=f"[ROI: {key}] {spec.desc}  ▶ 左键拖出矩形完成",
        )
        picker.setWindowTitle(f"取 ROI [{key}] - {spec.desc}")
        rect = picker.exec_for_rect()
        if rect is None:
            return None  # 用户取消
        nx1, ny1, nx2, ny2 = rect
        if abs(nx2 - nx1) < 1e-4 or abs(ny2 - ny1) < 1e-4:
            QMessageBox.warning(self, "矩形太小", "拖出的矩形面积太小, 已忽略")
            return None
        norm_rect = (
            round(nx1, 4),
            round(ny1, 4),
            round(nx2, 4),
            round(ny2, 4),
        )
        self._data.setdefault("roi", {})[key] = list(norm_rect)
        self._dirty = True
        self._refresh_table()
        self._update_dirty_label()
        return norm_rect

    def _pick_btn(self, key: str) -> Optional[tuple[float, float]]:
        """按钮取点: 仍用 PositionPickerDialog (单点 + 看 RGB 验证位置).

        按钮位置一般颜色边界明显, 看一眼像素色能直观确认有没有点歪.
        """
        spec = next(s for s in BTN_SPECS if s.key == key)
        dlg = PositionPickerDialog(
            self.mumu,
            parent=self,
            selection_mode=True,
            expected_count=1,
            selection_labels=[f"按钮 [{key}]"],
        )
        dlg.setWindowTitle(f"取按钮 [{key}] - {spec.desc}")
        if dlg.exec() != QDialog.Accepted:
            return None
        recs = dlg.result_records()
        if len(recs) != 1:
            QMessageBox.warning(self, "数量不符", f"需要 1 个点, 实际 {len(recs)} 个")
            return None
        pos = (round(recs[0].nx, 4), round(recs[0].ny, 4))
        self._data.setdefault("button", {})[key] = list(pos)
        self._dirty = True
        self._refresh_table()
        self._update_dirty_label()
        return pos

    # =========================================================================
    # 向导
    # =========================================================================

    def _on_wizard(self) -> None:
        roi = self._data.get("roi", {}) or {}
        btn = self._data.get("button", {}) or {}
        todo: list[tuple[str, str]] = []  # (kind, key)
        for s in ROI_SPECS:
            if not roi.get(s.key):
                todo.append(("roi", s.key))
        for s in BTN_SPECS:
            if not btn.get(s.key):
                todo.append(("btn", s.key))
        if not todo:
            QMessageBox.information(self, "无需录入", "所有字段都已配置. 可以直接保存.")
            return
        ans = QMessageBox.question(
            self,
            "向导",
            f"将依次录入 {len(todo)} 个未配置字段.\n"
            f"每一步弹一个 picker; 关闭/取消任一 picker 即终止向导.\n继续?",
        )
        if ans != QMessageBox.Yes:
            return
        for i, (kind, key) in enumerate(todo, 1):
            log.info("向导: 第 %d/%d 步 - %s [%s]", i, len(todo), kind, key)
            ok = self._pick_roi(key) if kind == "roi" else self._pick_btn(key)
            if ok is None:
                QMessageBox.information(
                    self,
                    "向导终止",
                    f"已停止. 已完成 {i - 1}/{len(todo)} 项.",
                )
                return
        QMessageBox.information(
            self, "向导完成", f"已录入 {len(todo)} 项. 别忘了点保存."
        )

    # =========================================================================
    # 预览 (复用项目子窗 ZoomedSnapshotPicker readonly 模式)
    # =========================================================================

    def _on_preview(self) -> None:
        try:
            img = self.mumu.capture_window().convert("RGB")
        except Exception as e:
            log.exception("截图失败")
            QMessageBox.critical(self, "截图失败", f"{type(e).__name__}: {e}")
            return
        # PIL 把所有标注 (ROI 矩形 + 标签 + 按钮十字) 直接画到原图上, 然后子窗只
        # 负责展示/缩放/拖动, 不需要单独的 add_rect_marker 逻辑.
        annotated = _annotate_preview(
            img,
            roi_dict=self._data.get("roi", {}) or {},
            btn_dict=self._data.get("button", {}) or {},
            roi_specs=ROI_SPECS,
            btn_specs=BTN_SPECS,
        )
        viewer = ZoomedSnapshotPicker(
            annotated,
            self.mumu,
            parent=self,
            show_recapture=False,  # 已经标注的图重截会丢标注
            mode="readonly",
            prompt=(
                "矩形=ROI; 圆圈+十字=按钮坐标. "
                "颜色: 灰=共享 / 绿=结束界面 / 蓝=准备界面.   "
                "右键拖动平移 | 滚轮缩放"
            ),
        )
        viewer.setWindowTitle("ROI / 按钮 标定预览")
        viewer.exec()

    # =========================================================================
    # 保存 / 关闭
    # =========================================================================

    def _on_reload(self) -> None:
        if self._dirty:
            ans = QMessageBox.question(
                self, "有未保存改动", "重载会丢弃未保存的改动. 继续?"
            )
            if ans != QMessageBox.Yes:
                return
        self._load()

    def _on_save(self) -> None:
        for k, v in (self._data.get("roi") or {}).items():
            if not (
                isinstance(v, list) and len(v) == 4 and all(0 <= x <= 1 for x in v)
            ):
                QMessageBox.warning(self, "校验失败", f"ROI [{k}] 数据不合法: {v!r}")
                return
        for k, v in (self._data.get("button") or {}).items():
            if not (
                isinstance(v, list) and len(v) == 2 and all(0 <= x <= 1 for x in v)
            ):
                QMessageBox.warning(self, "校验失败", f"按钮 [{k}] 数据不合法: {v!r}")
                return

        self.profile_path.parent.mkdir(parents=True, exist_ok=True)
        if self.profile_path.exists():
            try:
                bak = self.profile_path.with_suffix(self.profile_path.suffix + ".bak")
                bak.write_bytes(self.profile_path.read_bytes())
            except OSError:
                log.exception("备份失败 (继续写主文件)")

        try:
            self.profile_path.write_text(
                yaml.safe_dump(
                    self._data,
                    allow_unicode=True,
                    sort_keys=False,
                    default_flow_style=False,
                ),
                encoding="utf-8",
            )
        except Exception as e:
            log.exception("保存 profile 失败")
            QMessageBox.critical(self, "保存失败", f"{type(e).__name__}: {e}")
            return
        self._dirty = False
        self._update_dirty_label()

        n_roi = sum(1 for s in ROI_SPECS if (self._data.get("roi") or {}).get(s.key))
        n_btn = sum(1 for s in BTN_SPECS if (self._data.get("button") or {}).get(s.key))
        QMessageBox.information(
            self,
            "已保存",
            f"已写入 {self.profile_path}\n"
            f"ROI: {n_roi} / {len(ROI_SPECS)} 已配置\n"
            f"按钮: {n_btn} / {len(BTN_SPECS)} 已配置",
        )

    def closeEvent(self, ev) -> None:
        if self._dirty:
            ans = QMessageBox.question(
                self,
                "有未保存改动",
                "有未保存改动. 关闭前保存吗?\nYes=保存并关闭, No=放弃改动并关闭, Cancel=不关",
                QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel,
            )
            if ans == QMessageBox.Cancel:
                ev.ignore()
                return
            if ans == QMessageBox.Yes:
                self._on_save()
                if self._dirty:  # 保存失败
                    ev.ignore()
                    return
        super().closeEvent(ev)


# =============================================================================
# 工具函数
# =============================================================================


def _fmt_roi(v) -> str:
    if not v or len(v) != 4:
        return "(无效)"
    return f"({v[0]:.4f}, {v[1]:.4f}, {v[2]:.4f}, {v[3]:.4f})"


def _fmt_pos(v) -> str:
    if not v or len(v) != 2:
        return "(无效)"
    return f"({v[0]:.4f}, {v[1]:.4f})"


# =============================================================================
# 预览图绘制 (PIL 把所有 ROI 矩形 + 标签 + 按钮十字画到原图上)
# =============================================================================


def _annotate_preview(
    img: Image.Image,
    roi_dict: dict,
    btn_dict: dict,
    roi_specs: tuple[_RoiSpec, ...],
    btn_specs: tuple[_BtnSpec, ...],
) -> Image.Image:
    """在截图上画出所有已配置的 ROI 框 + 按钮十字, 返回新图.

    颜色: 共享=灰, 结束=绿, 准备=蓝.
    """
    out = img.copy()
    draw = ImageDraw.Draw(out)
    w, h = out.size
    try:
        font = ImageFont.truetype("arial.ttf", 14)
    except Exception:
        font = ImageFont.load_default()

    color_map = {
        "共享": (160, 160, 160),
        "结束": (40, 180, 80),
        "准备": (50, 120, 220),
    }

    # ROI
    for spec in roi_specs:
        v = roi_dict.get(spec.key)
        if not v or len(v) != 4:
            continue
        x1 = int(v[0] * w)
        y1 = int(v[1] * h)
        x2 = int(v[2] * w)
        y2 = int(v[3] * h)
        c = color_map[spec.panel]
        draw.rectangle([x1, y1, x2, y2], outline=c, width=2)
        # 文字标签 (画在 ROI 左上角内侧)
        label = spec.key
        try:
            tb = draw.textbbox((0, 0), label, font=font)
            tw, th = tb[2] - tb[0], tb[3] - tb[1]
        except AttributeError:
            tw, th = font.getsize(label)
        bg_x1 = x1
        bg_y1 = max(0, y1 - th - 4)
        draw.rectangle([bg_x1, bg_y1, bg_x1 + tw + 6, bg_y1 + th + 4], fill=c)
        draw.text((bg_x1 + 3, bg_y1 + 2), label, fill=(255, 255, 255), font=font)

    # 按钮: 圆圈 + 十字
    for spec in btn_specs:
        v = btn_dict.get(spec.key)
        if not v or len(v) != 2:
            continue
        cx = int(v[0] * w)
        cy = int(v[1] * h)
        c = color_map[spec.panel]
        r = 14
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], outline=c, width=3)
        draw.line([cx - r - 6, cy, cx + r + 6, cy], fill=c, width=2)
        draw.line([cx, cy - r - 6, cx, cy + r + 6], fill=c, width=2)
        # 标签
        label = f"btn:{spec.key}"
        try:
            tb = draw.textbbox((0, 0), label, font=font)
            tw, th = tb[2] - tb[0], tb[3] - tb[1]
        except AttributeError:
            tw, th = font.getsize(label)
        bx1 = cx + r + 4
        by1 = cy - th // 2 - 2
        draw.rectangle([bx1, by1, bx1 + tw + 6, by1 + th + 4], fill=c)
        draw.text((bx1 + 3, by1 + 2), label, fill=(255, 255, 255), font=font)

    return out
