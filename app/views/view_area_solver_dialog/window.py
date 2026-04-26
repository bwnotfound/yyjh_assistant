"""
反解可视区域 (view_area) 工具

前提:
  · map_size 准确 (用户保证)
  · block_size 误差小 (用户保证)
  · character_pos 准确 (movement_profile.character_pos)

工作流程:
  1. 在游戏里把角色走到能"贴边"的位置 —— 越靠近地图角越好
  2. 在工具里选当前地图 + 视野档位
  3. 点 [取一次观测]:
       a. 工具截一帧并 OCR 取角色当前格坐标 (gx, gy)
       b. 弹出该截图，请你点击角色实际所在的屏幕像素位置 → 得 (px, py)
       c. 工具反解出对应方向的 view_area 边界
  4. 重复 3。一次贴 NW 解出 vx0/vy0；一次贴 SE 解出 vx1/vy1。
  5. 4 边界齐了点 [应用到运动配置]，写入 movement_profile.yaml

反解公式 (来自 compute_character_screen_pos 的反推):
  Y 方向:
    py > cy → N 约束激活 → vy0 = py - n_dist  (n_dist=(gx+gy)*bh/2)
    py < cy → S 约束激活 → vy1 = py + s_dist  (s_dist=(mw+mh-gx-gy)*bh/2)
    py ≈ cy → 该方向未贴边，本次观测对 y 边界无效
  X 方向:
    px > cx → W 约束 → vx0 = px - w_dist  (w_dist=(gx+mh-gy)*bw/2)
    px < cx → E 约束 → vx1 = px + e_dist  (e_dist=(mw-gx+gy)*bw/2)

一致性检查 (任一不通过 → 警告 + 不更新该边):
  · 偏移方向必须与"离哪条边近"一致 (py>cy 必须 n_dist<=s_dist)。否则可能
    character_pos / map_size 配错，或用户点偏了。
  · 反解值必须落在 [0, 1] 内。

多次观测同一边时取最新值，不做加权平均 —— 观测精度不齐时旧值会拖累新值。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from PIL import Image
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from utils import Mumu

from app.core.ocr import CoordReader, TemplateOCR
from app.core.profiles import (
    DEFAULT_MOVEMENT_YAML_PATH,
    MovementProfile,
    MovementRegistry,
    VisionSpec,
    ViewAreaSolveResult,
    compute_view_area_reachability,
    solve_view_area_observation,
)
from app.views.position_picker.window import ZoomedSnapshotPicker
from config.common.map_registry import (
    DEFAULT_YAML_PATH as MAP_REGISTRY_PATH,
    MapRegistry,
    Profile as MapProfile,
)

log = logging.getLogger(__name__)


MINIMAP_TEMPLATE_DIR = Path("config/templates/minimap_coord")


# =============================================================================
# UI 层观测记录 (反解逻辑本身在 app.core.profiles)
# =============================================================================


@dataclass
class _Observation:
    """一次观测的完整记录 (用于历史展示)"""

    map_name: str
    map_size: tuple[int, int]
    pre_pos: tuple[int, int]
    screen_pos: tuple[float, float]
    vision_name: str
    block_size: tuple[float, float]
    character_pos: tuple[float, float]
    result: ViewAreaSolveResult


@dataclass
class _PendingPick:
    """
    主窗"取一次观测"截图 + OCR 完成后的待反解上下文。

    子窗 (ZoomedSnapshotPicker) 是非模态的, 用户在子窗里每点一次都
    emit point_picked → 主窗用这份 pending 反解。pending 在主窗下次
    "取一次观测"时被新的覆盖 (新 OCR 结果 + 新截图)。
    """

    map_name: str
    map_size: tuple[int, int]
    pre_pos: tuple[int, int]
    vision_name: str
    block_size: tuple[float, float]
    character_pos: tuple[float, float]


# =============================================================================
# 主对话框
# =============================================================================


class ViewAreaSolverDialog(QDialog):
    """反解 view_area 的交互工具"""

    def __init__(self, mumu: Mumu, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("反解可视区域")
        self.resize(580, 640)

        self._mumu = mumu
        self._movement_registry: Optional[MovementRegistry] = None
        self._movement_profile: Optional[MovementProfile] = None
        self._map_profile: Optional[MapProfile] = None
        self._load_profiles()

        # 当前累积的反解结果 (取最新值)
        self._vx0: Optional[float] = None
        self._vy0: Optional[float] = None
        self._vx1: Optional[float] = None
        self._vy1: Optional[float] = None
        self._observations: list[_Observation] = []

        # OCR 链路 lazy
        self._template_ocr: Optional[TemplateOCR] = None
        self._coord_reader: Optional[CoordReader] = None

        # 放大子窗 + 待反解上下文 (主窗 OCR 完, 子窗用户点击, 用 pending 反解)
        self._zoom_picker: Optional[ZoomedSnapshotPicker] = None
        self._pending: Optional[_PendingPick] = None

        # 复用现有 view_area 作为初值（如果配过）
        if (
            self._movement_profile is not None
            and self._movement_profile.map_view_area is not None
        ):
            self._vx0, self._vy0, self._vx1, self._vy1 = (
                self._movement_profile.map_view_area
            )

        self._build_ui()
        self._refresh_state_display()

    # -------------------------------------------------------------------------
    # 配置加载 + OCR 链路 (与 click_preview_dialog 同款)
    # -------------------------------------------------------------------------

    def _load_profiles(self) -> None:
        key = f"{self._mumu.device_w}x{self._mumu.device_h}"
        try:
            self._movement_registry = MovementRegistry.load(DEFAULT_MOVEMENT_YAML_PATH)
            self._movement_profile = self._movement_registry.profiles.get(key)
        except Exception as e:
            log.warning("加载 movement_profile 失败: %s", e)
        try:
            map_reg = MapRegistry.load(MAP_REGISTRY_PATH)
            self._map_profile = map_reg.profiles.get(key)
        except Exception as e:
            log.warning("加载 map_registry 失败: %s", e)

    def _get_coord_reader(self) -> CoordReader:
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
                "请先用主界面「ROI 截取工具」录入。"
            )
        if self._template_ocr is None:
            try:
                self._template_ocr = TemplateOCR.from_dir(MINIMAP_TEMPLATE_DIR)
            except FileNotFoundError as e:
                raise RuntimeError(
                    f"OCR 字符模板目录无可用模板: {MINIMAP_TEMPLATE_DIR}\n原因: {e}"
                ) from e
            except Exception as e:
                raise RuntimeError(f"OCR 模板加载失败: {type(e).__name__}: {e}") from e
        self._coord_reader = CoordReader(
            mumu=self._mumu, ocr=self._template_ocr, roi_norm=roi
        )
        return self._coord_reader

    # -------------------------------------------------------------------------
    # UI
    # -------------------------------------------------------------------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(8)

        # 顶部说明
        tip = QLabel(
            "前提：map_size、block_size、character_pos 三者准确。\n"
            "几何 (2.5D 等距投影): 屏幕 4 边各对应地图 1 个顶点。玩家**靠近**某顶点时,"
            " camera 把该顶点推到对应 view_area 边贴边, 玩家屏幕朝那个方向偏移。\n"
            "策略: 走地图 4 个顶点附近, 各解 1 条边: 靠 (0,0)→vy0, 靠 (0,mh)→vx0, "
            "靠 (mw,0)→vx1, 靠 (mw,mh)→vy1。共 4 次观测齐 4 边。\n"
            "可跨地图混合观测 (view_area/block_size/character_pos 都是按分辨率+"
            "视野共用的屏幕几何, 与具体地图无关)。\n"
            "下方黄色框给出激活阈值; 选地图+视野后实时刷新。"
        )
        tip.setWordWrap(True)
        tip.setStyleSheet("color: #555; font-size: 11px;")
        root.addWidget(tip)

        # ---- 地图 + 视野选择 ----
        form = QFormLayout()
        form.setContentsMargins(0, 4, 0, 4)

        self._map_combo = QComboBox()
        if self._map_profile is not None:
            for name, rec in sorted(self._map_profile.locations.items()):
                if not rec.is_recorded and rec.map_size is None:
                    continue
                if rec.map_size is None:
                    continue  # 反解必须有 map_size
                self._map_combo.addItem(
                    f"{name}  (map_size={rec.map_size[0]}×{rec.map_size[1]})", name
                )
        if self._map_combo.count() == 0:
            self._map_combo.addItem("(无可用地图)", None)
        form.addRow("当前地图:", self._map_combo)

        self._vision_combo = QComboBox()
        if self._movement_profile is not None and self._movement_profile.vision_sizes:
            for vname in self._movement_profile.vision_sizes.keys():
                self._vision_combo.addItem(vname)
        else:
            for vname in ("小", "中", "大"):
                self._vision_combo.addItem(vname)
        form.addRow("视野档位:", self._vision_combo)

        cp_text = "未加载"
        if self._movement_profile is not None:
            cp = self._movement_profile.character_pos
            cp_text = f"({cp[0]:.4f}, {cp[1]:.4f})"
        form.addRow("character_pos:", QLabel(cp_text))

        root.addLayout(form)

        # 地图/视野变化 → 刷新可达性提示
        self._map_combo.currentIndexChanged.connect(
            lambda _: self._refresh_reachability_hint()
        )
        self._vision_combo.currentIndexChanged.connect(
            lambda _: self._refresh_reachability_hint()
        )

        # ---- 反解状态 ----
        state_box = QLabel("")
        state_box.setStyleSheet(
            "background: #f5f5f5; border: 1px solid #ddd; "
            "padding: 8px; font-family: monospace;"
        )
        state_box.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._state_label = state_box
        root.addWidget(self._state_label)

        # ---- 可达性提示 (要解出哪条边需要走到哪里) ----
        hint_box = QLabel("")
        hint_box.setStyleSheet(
            "background: #fffde7; border: 1px solid #f0e68c; "
            "padding: 6px; font-family: monospace; font-size: 11px;"
        )
        hint_box.setTextInteractionFlags(Qt.TextSelectableByMouse)
        hint_box.setWordWrap(True)
        self._hint_label = hint_box
        root.addWidget(self._hint_label)

        # ---- 按钮行 ----
        btn_row = QHBoxLayout()
        btn_obs = QPushButton("取一次观测")
        btn_obs.setMinimumHeight(36)
        btn_obs.clicked.connect(self._on_observe)
        btn_row.addWidget(btn_obs)

        btn_clear = QPushButton("清空所有")
        btn_clear.clicked.connect(self._on_clear)
        btn_row.addWidget(btn_clear)

        btn_apply = QPushButton("应用到运动配置")
        btn_apply.setMinimumHeight(36)
        btn_apply.clicked.connect(self._on_apply)
        btn_row.addWidget(btn_apply)

        btn_close = QPushButton("关闭")
        btn_close.clicked.connect(self.close)
        btn_row.addWidget(btn_close)
        root.addLayout(btn_row)

        # ---- 观测历史 ----
        root.addWidget(QLabel("观测历史:"))
        self._history_list = QListWidget()
        self._history_list.setStyleSheet("font-family: monospace; font-size: 11px;")
        root.addWidget(self._history_list, 1)

    def _refresh_state_display(self) -> None:
        def _fmt(v: Optional[float]) -> str:
            return f"{v:.4f}" if v is not None else "?"

        all_set = all(
            v is not None for v in (self._vx0, self._vy0, self._vx1, self._vy1)
        )
        ok_or_pending = "✓ 4 边界齐了，可以应用" if all_set else "尚未齐全"

        self._state_label.setText(
            f"vx0 = {_fmt(self._vx0)}    vy0 = {_fmt(self._vy0)}\n"
            f"vx1 = {_fmt(self._vx1)}    vy1 = {_fmt(self._vy1)}\n"
            f"观测次数: {len(self._observations)}    状态: {ok_or_pending}"
        )

        # 同步观测次数到子窗的 record_label, 让用户在子窗也能看到累计次数
        if self._zoom_picker is not None and self._zoom_picker.isVisible():
            self._zoom_picker.set_record_count(len(self._observations), None)

        # 反解新边后估计的 view_area 变了 → 阈值也变 → 提示也跟着刷新
        self._refresh_reachability_hint()

    def _current_view_area_estimate(self) -> tuple[float, float, float, float]:
        """
        当前 view_area 的最佳估计, 用于算可达性阈值。
        优先级: 已反解的边界 > movement_profile 已配的 > hardcode 保守默认。
        4 边混合时让用户每解一边阈值就更准, 渐进收敛。
        """
        fallback_va = (
            self._movement_profile.map_view_area
            if self._movement_profile is not None
            and self._movement_profile.map_view_area is not None
            else (0.05, 0.05, 0.95, 0.95)
        )
        return (
            self._vx0 if self._vx0 is not None else fallback_va[0],
            self._vy0 if self._vy0 is not None else fallback_va[1],
            self._vx1 if self._vx1 is not None else fallback_va[2],
            self._vy1 if self._vy1 is not None else fallback_va[3],
        )

    def _refresh_reachability_hint(self) -> None:
        """根据当前选的地图 + 视野 + 当前 view_area 估计, 刷新可达性提示。"""
        if not hasattr(self, "_hint_label"):  # _build_ui 还没建好时调
            return
        if self._movement_profile is None:
            self._hint_label.setText("(运动配置未加载, 无法估算可达性)")
            return

        map_name = self._map_combo.currentData()
        if map_name is None or self._map_profile is None:
            self._hint_label.setText("(选地图后显示要走到哪里能解每条边)")
            return
        rec = self._map_profile.locations.get(map_name)
        if rec is None or rec.map_size is None:
            self._hint_label.setText(
                "(当前地图无 map_size, 无法估算; 选有 map_size 的地图)"
            )
            return

        vname = self._vision_combo.currentText()
        spec = self._movement_profile.vision_sizes.get(vname)
        if spec is None:
            self._hint_label.setText(f"(视野档位「{vname}」未配置)")
            return

        text = compute_view_area_reachability(
            map_size=rec.map_size,
            block_size=spec.block_size,
            character_pos=self._movement_profile.character_pos,
            view_area_estimate=self._current_view_area_estimate(),
        )
        self._hint_label.setText(text)

    # -------------------------------------------------------------------------
    # 取观测
    # -------------------------------------------------------------------------

    def _on_observe(self) -> None:
        if self._movement_profile is None:
            QMessageBox.warning(
                self,
                "缺少运动配置",
                f"未找到分辨率 {self._mumu.device_w}×{self._mumu.device_h} 的运动配置。",
            )
            return

        map_name = self._map_combo.currentData()
        if map_name is None:
            QMessageBox.warning(
                self, "缺少当前地图", "请选择当前地图（且该地图须有 map_size）。"
            )
            return
        rec = self._map_profile.locations.get(map_name) if self._map_profile else None
        if rec is None or rec.map_size is None:
            QMessageBox.warning(
                self, "map_size 缺失", f"地图「{map_name}」没有 map_size，无法反解。"
            )
            return
        map_size = rec.map_size

        vname = self._vision_combo.currentText()
        spec = self._movement_profile.vision_sizes.get(vname)
        if spec is None:
            QMessageBox.warning(
                self, "视野档位未配置", f"运动配置里没有视野档位「{vname}」。"
            )
            return

        # 截一帧 + 同帧 OCR
        try:
            img = self._mumu.capture_window()
        except Exception as e:
            log.exception("截图失败")
            QMessageBox.critical(self, "截图失败", f"{type(e).__name__}: {e}")
            return
        try:
            reader = self._get_coord_reader()
        except RuntimeError as e:
            QMessageBox.warning(self, "OCR 不可用", str(e))
            return
        try:
            coord, raw_text, _ = reader.read_verbose(image=img)
        except Exception as e:
            log.exception("OCR 调用异常")
            QMessageBox.critical(self, "OCR 调用异常", f"{type(e).__name__}: {e}")
            return
        if coord is None:
            QMessageBox.warning(
                self,
                "OCR 读不到坐标",
                f"OCR 拼出的原始字符串: {raw_text!r}\n请确认游戏小地图正在显示坐标。",
            )
            return
        gx, gy = coord
        log.info("反解工具: OCR=(%d, %d) on %s%s", gx, gy, map_name, map_size)

        # 存待反解上下文 (子窗每次点击都用这份反解)
        self._pending = _PendingPick(
            map_name=map_name,
            map_size=map_size,
            pre_pos=(gx, gy),
            vision_name=vname,
            block_size=spec.block_size,
            character_pos=self._movement_profile.character_pos,
        )

        # 开/复用放大子窗 (show_recapture=False: 子窗换帧会让 OCR 过期, 强制
        # 走主窗"取一次观测"才能换图)
        prompt = (
            f"当前观测: {map_name}({map_size[0]}×{map_size[1]})  "
            f"OCR=({gx},{gy})  视野={vname}    "
            f"请精确点击「角色脚下中心」"
        )
        if self._zoom_picker is None:
            self._zoom_picker = ZoomedSnapshotPicker(
                img,
                self._mumu,
                parent=self,
                show_recapture=False,
                prompt=prompt,
            )
            self._zoom_picker.point_picked.connect(self._on_pick_in_zoom)
        else:
            self._zoom_picker.set_image(img)
            self._zoom_picker.set_prompt(prompt)

        self._zoom_picker.set_record_count(len(self._observations), None)
        self._zoom_picker.show()
        self._zoom_picker.raise_()
        self._zoom_picker.activateWindow()

    def _on_pick_in_zoom(self, nx: float, ny: float) -> None:
        """
        子窗每点一次都进这里反解。pending 存的是上一次"取一次观测"时
        OCR 的结果, 用户在子窗里多次点击都基于同一份 OCR 反解 (相当于
        在同一帧的不同位置反复定位角色精确像素), 直到主窗再次"取一次观测"
        换 pending。
        """
        if self._pending is None:
            log.warning("_on_pick_in_zoom 触发但 _pending 为空, 忽略")
            return

        screen_pos = (nx, ny)
        p = self._pending

        result = solve_view_area_observation(
            pre_pos=p.pre_pos,
            screen_pos=screen_pos,
            map_size=p.map_size,
            block_size=p.block_size,
            character_pos=p.character_pos,
        )

        # 更新累计值
        if result.vx0 is not None:
            self._vx0 = result.vx0
        if result.vy0 is not None:
            self._vy0 = result.vy0
        if result.vx1 is not None:
            self._vx1 = result.vx1
        if result.vy1 is not None:
            self._vy1 = result.vy1

        # 记录历史
        obs = _Observation(
            map_name=p.map_name,
            map_size=p.map_size,
            pre_pos=p.pre_pos,
            screen_pos=screen_pos,
            vision_name=p.vision_name,
            block_size=p.block_size,
            character_pos=p.character_pos,
            result=result,
        )
        self._observations.append(obs)
        self._append_history_item(obs)
        self._refresh_state_display()

    def _append_history_item(self, obs: _Observation) -> None:
        def _f(v: Optional[float]) -> str:
            return f"{v:.4f}" if v is not None else "—"

        head = (
            f"#{len(self._observations)} {obs.map_name}{obs.map_size} "
            f"OCR=({obs.pre_pos[0]},{obs.pre_pos[1]}) "
            f"屏幕=({obs.screen_pos[0]:.4f},{obs.screen_pos[1]:.4f}) "
            f"视野={obs.vision_name}"
        )
        solved = (
            f"  解出: vx0={_f(obs.result.vx0)}  vy0={_f(obs.result.vy0)}  "
            f"vx1={_f(obs.result.vx1)}  vy1={_f(obs.result.vy1)}"
        )
        item_text = head + "\n" + solved
        for note in obs.result.notes:
            item_text += "\n  " + note
        item = QListWidgetItem(item_text)
        # 异常 notes 标红
        if any("⚠" in n for n in obs.result.notes):
            item.setForeground(QColor(180, 60, 60))
        self._history_list.addItem(item)
        self._history_list.scrollToBottom()

    # -------------------------------------------------------------------------
    # 清空 / 应用
    # -------------------------------------------------------------------------

    def _on_clear(self) -> None:
        ans = QMessageBox.question(
            self,
            "确认清空",
            "清空当前累计的 4 边界 + 观测历史。运动配置 yaml 不会被改动。",
        )
        if ans != QMessageBox.Yes:
            return
        self._vx0 = self._vy0 = self._vx1 = self._vy1 = None
        self._observations.clear()
        self._history_list.clear()
        self._refresh_state_display()

    def _on_apply(self) -> None:
        if not all(v is not None for v in (self._vx0, self._vy0, self._vx1, self._vy1)):
            QMessageBox.warning(
                self,
                "尚未齐全",
                "4 个边界都解出来才能应用。继续观测直到 vx0/vy0/vx1/vy1 全部有值。",
            )
            return
        if not (self._vx0 < self._vx1 and self._vy0 < self._vy1):
            QMessageBox.warning(
                self,
                "矩形非法",
                f"反解结果不构成合法矩形：\n"
                f"  vx0={self._vx0:.4f}, vx1={self._vx1:.4f}\n"
                f"  vy0={self._vy0:.4f}, vy1={self._vy1:.4f}\n"
                "需要 vx0 < vx1 且 vy0 < vy1。请重新观测。",
            )
            return
        if (
            self._movement_profile is None
            or self._movement_registry is None
            or self._movement_registry.path is None
        ):
            QMessageBox.warning(self, "无法保存", "运动配置 registry 未加载。")
            return

        new_va = (
            float(self._vx0),
            float(self._vy0),
            float(self._vx1),
            float(self._vy1),
        )
        old_va = self._movement_profile.map_view_area
        if old_va is not None:
            ans = QMessageBox.question(
                self,
                "覆盖现有 map_view_area？",
                f"现有: {old_va}\n新值: {new_va}\n\n确认覆盖并写入 yaml？",
            )
            if ans != QMessageBox.Yes:
                return

        self._movement_profile.map_view_area = new_va
        try:
            self._movement_registry.save()
        except Exception as e:
            log.exception("保存 movement_profile 失败")
            QMessageBox.critical(self, "保存失败", f"{type(e).__name__}: {e}")
            return

        QMessageBox.information(
            self,
            "已写入",
            f"map_view_area = {new_va}\n已保存到 movement_profile.yaml。\n"
            "可以用「点击位置截图显示」勾选「自适应地图」验证效果。",
        )

    # ---------------- 关闭 ----------------

    def closeEvent(self, ev) -> None:
        # 关主窗一并关掉非模态子窗, 避免悬挂
        if self._zoom_picker is not None:
            self._zoom_picker.close()
        super().closeEvent(ev)
